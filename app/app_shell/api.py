"""Routes for the make-goods app. The agent (loaded from the MLflow registry) is served at /invocations.

Every request runs as the signed-in person (auth.UserMiddleware): Unity Catalog row filters decide what the queue,
the rule functions and the analytics return, and every Lakebase list is limited to their scope. The UI is for
sellers, sales managers and ad ops: it shows business wording (business_view) and no platform or model details.
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from mlflow.types.responses import ResponsesAgentRequest
from pydantic import BaseModel

# agent_server comes from the registered model's code (or local code in development) - import after load_agent()
from agent_server.config import fq
from agent_server.mail import mail_mode
from agent_server.uc_tools import _execute, acting_as, write_as_app

from app_shell import business_view
from app_shell.auth import SECURITY, can_act, forget_scope, scope_of, user_of

AGENT = None  # the loaded MakeGoodsAgent (set by start_server)
AGENT_INFO: dict = {}

logger = logging.getLogger(__name__)
router = APIRouter()
STATIC = Path(__file__).resolve().parent.parent / "static"
_ROLES = {"seller", "manager", "admin"}


def _user(request: Request) -> str:
    u = user_of(request)
    return u.get("email") or "local-reviewer"


def _memory():
    if not AGENT or not AGENT.memory:
        raise HTTPException(503, "the make-goods workspace is starting; try again in a moment")
    return AGENT.memory


async def _run(sql: str, params: Optional[dict] = None) -> list[dict]:
    return await asyncio.to_thread(_execute, sql, params)


_switch_lock = asyncio.Lock()


async def _write_access(sql: str, params: dict) -> None:
    """A change to the access map, retried when another change to it committed at the same moment."""
    for attempt in range(4):
        try:
            await _run(sql, params)
            return
        except RuntimeError as e:
            if "CONCURRENT" not in str(e) or attempt == 3:
                raise
            await asyncio.sleep(0.5 * (attempt + 1))


@router.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@router.get("/api/config")
async def config(request: Request):
    rows = await _run(f"SELECT str_value FROM {fq('mg_rules_config')} WHERE rule_key = 'as_of_date'")
    sc = await asyncio.to_thread(scope_of, request)
    managers = [p for p in PERSONAS if p.startswith("MGR")]  # the demo personas include one sales manager
    persona = "admin" if not sc.view_as_until else (sc.seller_id if sc.role == "seller" else managers[0] if managers else None)
    return {"as_of": rows[0]["str_value"] if rows else None, "user": _user(request), "role": sc.role, "persona": persona,
            "granted_role": sc.granted_role, "seller_id": sc.seller_id, "markets": sc.markets,
            "display_name": sc.display_name, "view_as_until": sc.view_as_until,
            "mail_mode": mail_mode(), "sandbox_inbox": mail_mode() == "sandbox",
            "app_title": os.getenv("MG_APP_TITLE") or "Make-Goods"}


# ---------------------------------------------------------------- the work queue
@router.get("/api/queue")
async def queue(request: Request, market: Optional[str] = None, limit: int = 1000):
    mem = AGENT.memory if AGENT else None
    decided = set(await mem.decided_spots()) if mem else set()
    stages = await mem.spot_stages() if mem else {}
    where = "WHERE lower(q.market) = lower(:market)" if market else ""
    rows = await _run(f"""
        SELECT q.spot_id, q.advertiser_id, q.advertiser_name, q.call_letters, q.market, q.network_affiliation, q.broadcast_date,
               q.daypart, q.program_name, q.preemption_reason, q.revenue_at_risk_usd, q.segment_code, q.makegood_deadline,
               q.days_to_deadline, q.queue_status, q.seller_id, q.seller_name,
               p.package_label AS best_option, p.avail_ids AS best_avail_ids, p.dollar_label AS best_dollar_label,
               p.dollar_match AS best_dollar_match, p.rules_passed AS best_rules_passed, p.rules_total AS best_rules_total,
               p.is_full AS best_is_full, p.coverage_pct AS best_coverage_pct
        FROM {fq('gold_make_good_candidates')} q
        LEFT JOIN (SELECT * FROM {fq('mg_proposals')} WHERE package_label = 'A'
                   QUALIFY ROW_NUMBER() OVER (PARTITION BY spot_id ORDER BY computed_at DESC) = 1) p ON p.spot_id = q.spot_id
        {where}
        ORDER BY q.days_to_deadline, q.call_letters, q.revenue_at_risk_usd DESC LIMIT {int(limit)}""",
                      {"market": market} if market else None)
    out = []
    for r in rows:
        if r["spot_id"] in decided:
            continue
        st = stages.get(r["spot_id"]) or {}
        out.append({**r, "stage": st.get("stage"), "thread_id": st.get("thread_id")})
    return out


_insights_cache: dict[str, Any] = {}


@router.get("/api/insights")
async def insights(request: Request):
    """Overview tab: KPIs and chart datasets for what this person can see. History is cached 5 minutes per person."""
    q = await queue(request)
    key = scope_of(request).principal or user_of(request).get("email") or "local"
    hit = _insights_cache.get(key)
    if not hit or time.time() - hit[0] > 300:
        preempt, refunds, matchers = await asyncio.gather(*(_run(sql) for sql in (
            f"""SELECT CAST(broadcast_date AS STRING) AS date,
                       CASE WHEN preemption_reason IN ('Network Override', 'Traffic Error') THEN 'Other' ELSE preemption_reason END AS reason,
                       count(*) AS spots
                FROM {fq('gold_spot_access')}
                WHERE status <> 'Aired' AND broadcast_date <= (SELECT CAST(str_value AS DATE) FROM {fq('mg_rules_config')} WHERE rule_key = 'as_of_date')
                GROUP BY ALL ORDER BY date""",
            f"""SELECT original_daypart AS daypart, round(sum(refund_amount_usd)) AS refund_usd, count(*) AS offers,
                       count_if(is_downgrade) AS downgrades
                FROM {fq('gold_make_good_outcomes')} GROUP BY ALL ORDER BY refund_usd DESC""",
            f"""SELECT matched_by, count(*) AS offers, round(avg(CASE WHEN offer_status = 'Accepted' THEN 1.0 ELSE 0 END), 3) AS accept_rate,
                       round(sum(refund_amount_usd)) AS refund_usd, round(avg(CASE WHEN is_downgrade THEN 1.0 ELSE 0 END), 3) AS downgrade_rate
                FROM {fq('gold_make_good_outcomes')} GROUP BY ALL ORDER BY accept_rate DESC""")))
        _insights_cache[key] = (time.time(), preempt, refunds, matchers)
    _, preempt, refunds, matchers = _insights_cache[key]

    by_market: dict[str, dict] = {}
    by_deadline: dict[int, dict] = {}
    for r in q:
        m = by_market.setdefault(r["market"], {"market": r["market"], "revenue_at_risk_usd": 0.0, "open_spots": 0})
        m["revenue_at_risk_usd"] += r["revenue_at_risk_usd"] or 0
        m["open_spots"] += 1
        d = by_deadline.setdefault(r["days_to_deadline"], {"days_to_deadline": r["days_to_deadline"], "spots": 0, "revenue_at_risk_usd": 0.0})
        d["spots"] += 1
        d["revenue_at_risk_usd"] += r["revenue_at_risk_usd"] or 0
    return {
        "kpis": {"open_spots": len(q), "revenue_at_risk_usd": sum(r["revenue_at_risk_usd"] or 0 for r in q),
                 "due_3d": sum(1 for r in q if r["days_to_deadline"] <= 3),
                 "refunded_usd": sum(r["refund_usd"] or 0 for r in refunds)},
        "preemptions": preempt,
        "at_risk_by_market": sorted(by_market.values(), key=lambda m: -m["revenue_at_risk_usd"])[:10],
        "refunds_by_daypart": refunds,
        "acceptance_by_matcher": matchers,
        "deadlines": sorted(by_deadline.values(), key=lambda d: d["days_to_deadline"]),
    }


@router.get("/api/spots/{spot_id}/brief")
async def spot_brief(spot_id: str, request: Request):
    """Everything about one missed spot in one place: contract, client, rules, preferences, offers so far."""
    ctx = await _run(f"SELECT * FROM {fq('spot_context')}(:spot)", {"spot": spot_id})
    if not ctx:
        raise HTTPException(404, "spot not found among your accounts")
    s = ctx[0]
    history, proposals = await asyncio.gather(
        _run(f"SELECT offered_ts, offered_avail_id, offered_program, offered_daypart, offer_status, refund_amount_usd "
             f"FROM {fq('gold_make_good_outcomes')} WHERE advertiser_id = :adv ORDER BY offered_ts DESC LIMIT 8", {"adv": s["advertiser_id"]}),
        _run(f"SELECT * FROM {fq('mg_proposals')} WHERE spot_id = :spot "
             f"QUALIFY dense_rank() OVER (ORDER BY computed_at DESC) = 1 ORDER BY package_label", {"spot": spot_id}))
    mem = AGENT.memory if AGENT else None
    rules = await mem.rules(s["advertiser_id"]) if mem else []
    evidence = await mem.pref_evidence(s["advertiser_id"]) if mem else {}
    return {"spot": s, "client_rules": rules, "preference_evidence": evidence, "recent_offers": history, "proposals": proposals}


# ---------------------------------------------------------------- lifecycle lists (Lakebase, scoped)
@router.get("/api/reviews")
async def reviews(request: Request, status: str = "pending"):
    return await _memory().list_reviews(status, scope=scope_of(request)) if AGENT and AGENT.memory else []


@router.get("/api/decisions")
async def decisions(request: Request):
    return await _memory().list_decisions(100, scope=scope_of(request)) if AGENT and AGENT.memory else []


@router.get("/api/offers")
async def offers(request: Request, status: Optional[str] = None):
    statuses = status.split(",") if status else None
    rows = await _memory().list_offers(scope_of(request), statuses)
    return [{k: v for k, v in r.items() if k not in ("body_html",)} for r in rows]


@router.get("/api/approvals")
async def approvals(request: Request):
    sc = scope_of(request)
    if sc.role not in ("manager", "admin"):
        return []
    return await _memory().list_offers(sc, ["awaiting_manager"])


@router.get("/api/threads/{thread_id}")
async def thread(thread_id: str, request: Request):
    mem = _memory()
    row = await mem.get_review(thread_id)
    if not row or not scope_of(request).allows(row.get("market"), row.get("seller_id")):
        raise HTTPException(404, "not found")
    offer = await mem.get_offer(row["offer_id"]) if row.get("offer_id") else None
    from agent_server.agent import thread_state  # the loaded model's code

    state = await thread_state(thread_id)
    state["trail"] = [business_view.step(x) for x in state.get("trail") or []]
    return {"review": row, "offer": offer and {k: v for k, v in offer.items() if k != "body_html"}, "state": state,
            "events": await mem.list_events(thread_id),
            "mail": [{k: m[k] for k in ("direction", "from_addr", "to_addr", "subject", "body_text", "logged_at", "matched_by")}
                     for m in await mem.mail_for_thread(thread_id)]}


# ---------------------------------------------------------------- the agent
class ChatIn(BaseModel):
    message: str = ""
    thread_id: Optional[str] = None
    spot_id: Optional[str] = None
    mode: Optional[str] = None
    review: Optional[dict[str, Any]] = None


async def _authorize(request: Request, thread_id: str, review: dict) -> None:
    """The person may act on this thread, and their role may take the step it is paused at."""
    sc = scope_of(request)
    mem = AGENT.memory if AGENT else None
    row = await mem.get_review(thread_id) if mem else None
    snapshot_type = None
    if AGENT and mem:
        from agent_server.agent import get_graph  # the loaded model's code
        snap = await get_graph(False).aget_state({"configurable": {"thread_id": thread_id}})
        snapshot_type = snap.interrupts[0].value.get("type") if snap and snap.interrupts else None
    why = can_act(sc, snapshot_type, review.get("action"), row)
    if why:
        raise HTTPException(403, why)


@router.post("/api/chat")
async def chat(body: ChatIn, request: Request):
    """Server-sent events: each finished graph step, each agent message, then the final custom outputs."""
    reviewer = _user(request)
    u = user_of(request)
    ci: dict[str, Any] = {"thread_id": body.thread_id or str(uuid4())}
    if body.spot_id and body.mode:
        ci.update(spot_id=body.spot_id, mode=body.mode)
    if body.review:
        await _authorize(request, ci["thread_id"], body.review)
        sc = scope_of(request)
        ci["review"] = {**body.review, "reviewer": reviewer, "reviewer_name": sc.display_name}
    elif body.thread_id and AGENT and AGENT.memory:
        row = await AGENT.memory.get_review(body.thread_id)
        if row and not scope_of(request).allows(row.get("market"), row.get("seller_id")):
            raise HTTPException(403, "this make-good belongs to a client you can't see")
    req = ResponsesAgentRequest(input=[{"role": "user", "content": body.message}], custom_inputs=ci)

    async def events():
        acting_as(u.get("token"), u.get("email"))  # the stream runs as the person too
        try:
            async for kind, value in AGENT.run_events(req):
                if kind == "step":
                    value = [business_view.step(s) for s in value]
                elif kind == "text":
                    value = business_view.text(value)
                yield f"data: {json.dumps({'type': kind, 'data': value}, default=str)}\n\n"
        except Exception as e:  # surface the failure in the UI instead of a silent hang
            logger.exception("agent run failed")
            yield f"data: {json.dumps({'type': 'error', 'data': business_view.error(e)})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


# ---------------------------------------------------------------- client mail
@router.post("/api/mail/poll")
async def mail_poll(request: Request):
    """'Check replies': pick up client replies now for the offers this person can see."""
    from agent_server.watcher import check_replies

    mine = {o["offer_id"] for o in await _memory().list_offers(scope_of(request), ["sent"])}
    picked = await check_replies(_memory(), AGENT.run_events, mine) if mine else []
    return {"checked": len(mine), "picked_up": picked}


class ReplyIn(BaseModel):
    text: str


@router.get("/api/inbox")
async def inbox(request: Request):
    """Demo client inbox (sandbox mail): the offers as the client receives them."""
    if mail_mode() != "sandbox":
        raise HTTPException(404, "the client inbox is only available with demo mail")
    rows = await _memory().list_offers(scope_of(request), ["sent", "replied", "booked", "countered", "refunded"])
    return [{k: r[k] for k in ("offer_id", "thread_id", "advertiser_name", "contact_name", "sent_to", "subject", "body_text",
                               "status", "sent_at", "mail_thread_id")} for r in rows]


RECOMMEND = {  # what the seller should do next, by where the offer is and what the client said
    "accept": ("book", "Book the option the client picked"),
    "decline": ("refund", "The client declined: refund the spot"),
    "counter": ("counter", "Search again with what the client asked for"),
    "question": ("open", "Answer the client's question, then enter their answer"),
    "unclear": ("open", "The reply is unclear: open the conversation and confirm with the client"),
}


@router.get("/api/inbox/threads")
async def inbox_threads(request: Request):
    """One entry per offer this person can see: where it is, the client's latest reply and how it was read, and the
    recommended next step (the actions themselves go through /api/chat on the offer's thread)."""
    mem = _memory()
    offers = await mem.list_offers(scope_of(request), ["sent", "replied", "booked", "refunded", "countered", "withdrawn"])
    emails = await _run(f"SELECT email_id, offer_id, received_at, body, intent, option_label, requested_change, question "
                        f"FROM {fq('mg_client_emails_mine')} WHERE offer_id IS NOT NULL")
    by_offer: dict[str, list] = {}
    for e in emails:
        by_offer.setdefault(e["offer_id"], []).append(e)
    out = []
    for o in offers:
        reply = o.get("reply") or {}
        replies = sorted(by_offer.get(o["offer_id"], []), key=lambda e: str(e["received_at"]))
        if o["status"] == "replied":
            action, label = RECOMMEND.get(reply.get("decision"), ("open", "Open the conversation"))
        elif o["status"] == "sent":
            action, label = "wait", "Waiting for the client's reply"
        else:
            action, label = None, {"booked": "Booked", "refunded": "Refunded", "countered": "Searching again",
                                   "withdrawn": "Withdrawn"}.get(o["status"], o["status"])
        out.append({k: o.get(k) for k in ("offer_id", "thread_id", "spot_id", "advertiser_name", "market", "seller_name",
                                          "contact_name", "sent_to", "subject", "body_text", "status", "sent_at", "updated_at")}
                   | {"reply": reply, "replies": replies, "packages": [p.get("package_label") for p in o.get("packages") or []],
                      "recommended": {"action": action, "label": label}})
    order = {"replied": 0, "sent": 1}
    return sorted(out, key=lambda x: (order.get(x["status"], 2), str(x["updated_at"] or "")), reverse=False)


@router.get("/api/client-emails")
async def client_emails(request: Request):
    """Client replies as stored in the email table, with what was read from each (yes / no / needs more information),
    limited to the clients this person may see."""
    return await _run(f"SELECT email_id, received_at, advertiser_name, market, spot_id, from_addr, subject, body, "
                      f"offered_options, source, intent, option_label, requested_change, question "
                      f"FROM {fq('mg_client_emails_mine')} ORDER BY received_at DESC LIMIT 60")


@router.post("/api/inbox/{offer_id}/reply")
async def inbox_reply(offer_id: str, body: ReplyIn, request: Request):
    """Reply as the client (sandbox mail); the reply watcher picks it up exactly as it would from a mailbox."""
    if mail_mode() != "sandbox":
        raise HTTPException(404, "the client inbox is only available with demo mail")
    mem = _memory()
    o = await mem.get_offer(offer_id)
    if not o or not scope_of(request).allows(o.get("market"), o.get("seller_id")) or o["status"] != "sent":
        raise HTTPException(404, "no open offer with that id")
    await mem.log_mail({"message_id": f"sbx-in-{uuid4().hex[:16]}", "offer_id": offer_id, "thread_id": o["thread_id"],
                        "mail_thread_id": o["mail_thread_id"], "direction": "in", "from_addr": o["sent_to"],
                        "to_addr": "sales", "subject": f"Re: {o['subject']}", "body_text": body.text[:4000]})
    from agent_server.watcher import check_replies

    return {"picked_up": await check_replies(mem, AGENT.run_events, {offer_id})}


# ---------------------------------------------------------------- clients: rules and learned preferences
@router.get("/api/clients")
async def clients(request: Request):
    rows = await _run(f"SELECT advertiser_id, first(advertiser_name) AS advertiser_name, count(*) AS open_spots, "
                      f"round(sum(revenue_at_risk_usd)) AS at_risk_usd, collect_set(market) AS markets, first(seller_name) AS seller "
                      f"FROM {fq('gold_make_good_candidates')} GROUP BY advertiser_id ORDER BY at_risk_usd DESC LIMIT 60")
    mem = AGENT.memory if AGENT else None
    out = []
    from agent_server import rules as R

    for r in rows:
        rules = await mem.rules(r["advertiser_id"]) if mem else []
        ev = await mem.pref_evidence(r["advertiser_id"]) if mem else {}
        out.append({**r, "rules": rules, "preference_evidence": ev, "suggested_rules": R.suggest(ev, rules)})
    return out


@router.delete("/api/clients/{advertiser_id}/rules/{rule_id}")
async def delete_rule(advertiser_id: str, rule_id: str, request: Request):
    vis = await _run(f"SELECT 1 FROM {fq('advertiser_lookup')}(:adv) LIMIT 1", {"adv": advertiser_id})
    if not vis:
        raise HTTPException(404, "client not found among your accounts")
    mem = _memory()
    rule = next((r for r in await mem.rules(advertiser_id) if r.get("rule_id") == rule_id), None)
    if not rule:
        raise HTTPException(404, "no such rule")
    await mem.delete_rule(advertiser_id, rule_id)
    # the Unity Catalog copy is append-only (latest row per rule_id wins): record the rule as switched off
    await write_as_app(
        f"INSERT INTO {fq('mg_advertiser_rules')} (rule_id, advertiser_id, advertiser_name, rule_type, rule_values, strength, "
        f"label, source_text, created_by, created_at, active) VALUES (:rid, :adv, :name, :t, from_json(:v, 'ARRAY<STRING>'), "
        f":st, :label, :src, :by, current_timestamp(), false)",
        {"rid": rule_id, "adv": advertiser_id, "name": rule.get("advertiser_name"), "t": rule["type"],
         "v": json.dumps(rule["values"]), "st": rule.get("strength"), "label": rule.get("label"),
         "src": rule.get("source_text"), "by": _user(request)})
    return {"deleted": rule_id}


# ---------------------------------------------------------------- business results
@router.get("/api/metrics/business")
async def business(request: Request):
    mv = fq("makegoods_resolution_metrics")
    measures = ("MEASURE(`Decisions`) AS decisions, MEASURE(`Made Good Value`) AS made_good_usd, "
                "MEASURE(`Refunded Amount`) AS refunded_usd, MEASURE(`Recovery Rate`) AS recovery_rate, "
                "MEASURE(`Value Match`) AS value_match, MEASURE(`Median Days to Resolve`) AS median_days, "
                "MEASURE(`P90 Days to Resolve`) AS p90_days")
    total, by_market, by_seller, history = await asyncio.gather(
        _run(f"SELECT {measures} FROM {mv}"),
        _run(f"SELECT `Market` AS market, {measures} FROM {mv} GROUP BY ALL ORDER BY made_good_usd DESC NULLS LAST"),
        _run(f"SELECT `Seller` AS seller_id, {measures} FROM {mv} GROUP BY ALL ORDER BY made_good_usd DESC NULLS LAST"),
        _run(f"SELECT MEASURE(`Offers`) AS offers, MEASURE(`Acceptance Rate`) AS accept_rate, "
             f"MEASURE(`Refunded Amount`) AS refunded_usd FROM {fq('makegoods_offer_outcome_metrics')}"))
    names = {r["seller_id"]: r["seller_name"] for r in await _run(f"SELECT seller_id, seller_name FROM {fq('ref_sellers')}")}
    for r in by_seller:
        r["seller_id_name"] = names.get(r["seller_id"])
    mem = AGENT.memory if AGENT else None
    offers = await mem.list_offers(scope_of(request)) if mem else []
    funnel: dict[str, int] = {}
    for o in offers:
        funnel[o["status"]] = funnel.get(o["status"], 0) + 1
    decisions = await mem.list_decisions(50, scope=scope_of(request)) if mem else []
    return {"total": total[0] if total else {}, "by_market": by_market, "by_seller": by_seller,
            "history": history[0] if history else {}, "offer_funnel": funnel, "decisions": decisions}


# ---------------------------------------------------------------- admin: access
class AccessIn(BaseModel):
    principal: str
    role: str
    display_name: Optional[str] = None
    seller_id: Optional[str] = None
    markets: list[str] = []


class ViewAsIn(BaseModel):
    persona: str  # "admin" (your own access) or a seller / manager id from PERSONAS
    minutes: int = 120


# the people an admin can see the app as in a demo: one seller and their sales manager
PERSONAS = ("SLR-04", "MGR-02")


def _admin(request: Request):
    sc = scope_of(request)
    if sc.granted_role != "admin":
        raise HTTPException(403, "admins only")
    return sc


@router.get("/api/admin/access")
async def admin_access(request: Request):
    _admin(request)
    return await _run(f"SELECT * FROM {SECURITY}.mg_user_access ORDER BY role, principal")


@router.get("/api/admin/personas")
async def admin_personas(request: Request):
    _admin(request)
    ids = ", ".join(f"'{p}'" for p in PERSONAS)
    rows = await _run(f"SELECT seller_id, seller_name, role, region, markets FROM {fq('ref_sellers')} WHERE seller_id IN ({ids})")
    people = sorted(rows, key=lambda r: PERSONAS.index(r["seller_id"]))
    return [{"persona": "admin", "label": "Me, as admin", "detail": "sees every market and client"}] + [
        {"persona": r["seller_id"], "label": r["seller_name"],
         "detail": ("seller: their own clients" if r["role"] == "seller" else f"sales manager: {r['region']} markets")
                   + f" ({', '.join((r['markets'] or [])[:3])}{', …' if len(r['markets'] or []) > 3 else ''})"} for r in people]


@router.post("/api/admin/access")
async def admin_grant(body: AccessIn, request: Request):
    _admin(request)
    if body.role not in _ROLES:
        raise HTTPException(400, "role must be seller, manager or admin")
    who = _user(request)
    params = {"p": body.principal.strip(), "n": body.display_name, "r": body.role, "s": body.seller_id,
              "m": json.dumps(body.markets), "by": who}
    await _run(f"""MERGE INTO {SECURITY}.mg_user_access t
        USING (SELECT :p AS principal, :n AS display_name, :r AS role, :s AS seller_id,
                      from_json(:m, 'ARRAY<STRING>') AS markets) s ON t.principal = s.principal
        WHEN MATCHED THEN UPDATE SET display_name = s.display_name, role = s.role, seller_id = s.seller_id,
             markets = s.markets, updated_by = :by, updated_at = current_timestamp()
        WHEN NOT MATCHED THEN INSERT (principal, display_name, role, seller_id, markets, updated_by, updated_at)
             VALUES (s.principal, s.display_name, s.role, s.seller_id, s.markets, :by, current_timestamp())""", params)
    await _audit(who, body.principal, "grant", body.model_dump())
    return {"ok": True}


@router.post("/api/admin/view-as")
async def admin_view_as(body: ViewAsIn, request: Request):
    sc = _admin(request)
    if body.persona == "admin":
        return await admin_view_as_end(request)
    if body.persona not in PERSONAS:
        raise HTTPException(400, "unknown persona")
    p = (await _run(f"SELECT seller_id, role, markets FROM {fq('ref_sellers')} WHERE seller_id = :id", {"id": body.persona}))[0]
    role, seller_id = ("seller", p["seller_id"]) if p["role"] == "seller" else ("manager", None)
    markets = [] if role == "seller" else list(p["markets"] or [])
    who = _user(request)
    minutes = max(5, min(body.minutes, 240))
    async with _switch_lock:
        await _write_access(f"""UPDATE {SECURITY}.mg_user_access SET view_as_role = :r, view_as_seller_id = :s,
        view_as_markets = from_json(:m, 'ARRAY<STRING>'), view_as_until = current_timestamp() + make_interval(0, 0, 0, 0, 0, CAST(:min AS INT)),
        updated_by = :by, updated_at = current_timestamp() WHERE principal = :p""",
                            {"r": role, "s": seller_id, "m": json.dumps(markets), "min": minutes, "by": who, "p": sc.principal})
    await _audit(who, sc.principal, "view_as", {"persona": body.persona, "role": role, "minutes": minutes})
    forget_scope(request)
    _insights_cache.pop(sc.principal, None)
    return {"ok": True, "minutes": minutes}


@router.delete("/api/admin/view-as")
async def admin_view_as_end(request: Request):
    sc = scope_of(request)
    if sc.granted_role != "admin":
        raise HTTPException(403, "admins only")
    async with _switch_lock:
        await _write_access(f"UPDATE {SECURITY}.mg_user_access SET view_as_until = NULL, updated_at = current_timestamp() "
                            f"WHERE principal = :p", {"p": sc.principal})
    await _audit(_user(request), sc.principal, "view_as_end", {})
    forget_scope(request)
    _insights_cache.pop(sc.principal, None)
    return {"ok": True}


@router.get("/api/admin/audit")
async def admin_audit(request: Request):
    _admin(request)
    return await _run(f"SELECT * FROM {SECURITY}.mg_access_audit ORDER BY changed_at DESC LIMIT 100")


@router.get("/api/admin/rules-config")
async def admin_rules_config(request: Request):
    _admin(request)
    return await _run(f"SELECT rule_key, num_value, str_value, rule_type, description, question_for_ad_ops "
                      f"FROM {fq('mg_rules_config')} ORDER BY rule_type, rule_key")


async def _audit(who: str, principal: str, action: str, details: dict) -> None:
    await _run(f"INSERT INTO {SECURITY}.mg_access_audit (changed_at, changed_by, principal, action, details) "
               f"VALUES (current_timestamp(), :by, :p, :a, :d)",
               {"by": who, "p": principal, "a": action, "d": json.dumps(details, default=str)})
