"""Make-good packages and the offer lifecycle, as graph nodes.

    score_candidates -> build_packages ─(no option is 10/10, passes left)─> close_gap -> search_inventory (wider)
                                      └─> explain_recommendation -> human_review (seller picks options to offer)
    draft_offer (facts from the rules engine, prose from the AI, numbers checked)
      -> manager_review  ⏸ approve | request changes (-> draft_offer) | reject (-> human_review)
      -> confirm_send    ⏸ the seller presses Send
      -> send_offer      (mail adapter; offered avails held for the client)
      -> await_client    ⏸ waits, possibly for days, until the client's reply arrives (or the seller withdraws)
      -> parse_client_reply (AI reads the reply; reply_parser keeps it to what was offered)
      -> seller_confirm  ⏸ book | counter (-> search again with the client's ask) | refund | withdraw

Each ⏸ is an interrupt: the thread is saved in the Lakebase checkpointer and resumes when a person (or the reply
watcher) answers. Nodes before an interrupt are re-run on resume, so everything they write is idempotent.
"""
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import mlflow
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt
from mlflow.entities import SpanType

from agent_server import offer_email, prompts, reply_parser
from agent_server.config import PROMPT_OFFER, PROMPT_REPLY, fq
from agent_server.mail import OutboundMessage, get_adapter
from agent_server.mail.policy import MailRefused, check_rate, deliver_to, ref_token
from agent_server.memory import Memory
from agent_server.steps import DAYPARTS, _live, _step, _text, _usage, log_human_feedback
from agent_server.mail.matching import own_text
from agent_server.uc_tools import call_uc, current_user, query, query_as_app, write_as_app

logger = logging.getLogger(__name__)
LABEL_ORDER = "ABCS"
_cfg_cache: dict[str, tuple[float, dict]] = {}
CFG_DEFAULTS = {"search_horizon_days": 30, "gap_loop_max_iterations": 2, "gap_widen_horizon_days": 14,
                "gap_widen_max_results": 60, "hold_hours": 72, "approval_required_over_usd": 0}


async def rules_config() -> dict:
    """mg_rules_config values the lifecycle uses (cached 5 minutes; ad ops change them without a redeploy)."""
    hit = _cfg_cache.get("v")
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    rows = await query(f"SELECT rule_key, num_value, str_value FROM {fq('mg_rules_config')}", "rules_config")
    v = dict(CFG_DEFAULTS) | {r["rule_key"]: r["num_value"] if r["num_value"] is not None else r["str_value"] for r in rows}
    _cfg_cache["v"] = (time.time(), v)
    return v


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def package_table(packages: list[dict]) -> str:
    """The options table shown under the recommendation (built from the rules engine's output, not by the AI)."""
    if not packages:
        return ""
    head = ("| Option | Avails | Airs | Est. impressions | % of owed | Value vs missed spot | Rules |\n"
            "|---|---|---|---|---|---|---|")
    rows = []
    for p in packages:
        fails = [c["label"] for c in p["checklist"] if not c["passed"]]
        rows.append("| {l} | {a} | {d} | {i:,} | {c:.0%} | {v} | {r} |".format(
            l=p["package_label"], a=", ".join(p["avail_ids"]),
            d="; ".join(f"{ln['air_date']} {ln['daypart']} {ln['program_name']}" for ln in p["lines"]),
            i=p["est_impressions"], c=p["coverage_pct"], v=f"${p['proposed_value_usd']:,.0f}: {p['dollar_label']}",
            r=f"{p['rules_passed']}/{p['rules_total']}" + (f" (misses: {'; '.join(fails)})" if fails else " ✓")))
    return "\n".join([head, *rows])


def compact_package(p: dict) -> dict:
    keep = ("package_label", "strategy", "avail_ids", "n_lines", "est_impressions", "owed_impressions", "coverage_pct",
            "owed_value_usd", "proposed_value_usd", "value_gap_usd", "value_ratio", "dollar_match", "partial_refund_usd",
            "dollar_label", "rules_passed", "rules_total", "package_score", "is_full")
    return {**{k: p.get(k) for k in keep},
            "lines": [{k: ln.get(k) for k in ("avail_id", "rank", "air_date", "daypart", "program_name", "program_genre",
                                              "adjacency_tags", "rate_card_usd", "est_impressions", "match_score", "tier",
                                              "misses")} for ln in p.get("lines") or []],
            "checklist": p.get("checklist") or []}


def make_offer_nodes(llm, llm_endpoint: str, memory: Optional[Memory]) -> dict:
    def live(state) -> bool:
        return _live(state, memory)

    async def event(state, config, name: str, actor: Optional[str] = None, details: Optional[dict] = None):
        if live(state):
            await memory.log_event(config["configurable"]["thread_id"], name, actor or current_user(),
                                   offer_id=(state.get("offer") or {}).get("offer_id"), spot=state.get("spot"),
                                   details=details, trace_id=mlflow.get_active_trace_id())

    # ------------------------------------------------------------------ packages + gap loop
    async def build_packages(state: dict) -> dict:
        ranked, rp, search = state["ranked"], state.get("rule_params") or {}, state.get("search") or {}
        rows = await call_uc("package_builder", p_spot_id=state["spot_id"], p_avail_ids=[r["avail_id"] for r in ranked],
                             p_prefs=state.get("prefs") or None, p_as_of=state.get("as_of"),
                             p_prefer_dows=rp.get("prefer_dows") or None, p_horizon_days=search.get("horizon_days"),
                             p_client_rules=rp.get("labels") or None)
        packages = sorted(rows, key=lambda p: LABEL_ORDER.index(p["package_label"]))
        a = packages[0] if packages else None
        sf = {}
        if a:
            sfr = await call_uc("shortfall_calculator", p_spot_id=state["spot_id"], p_avail_ids=a["avail_ids"])
            sf = sfr[0] if sfr else {}
        cfg = await rules_config()
        full = any(p["is_full"] for p in packages)
        can_widen = not full and state.get("gap_iterations", 0) < int(cfg["gap_loop_max_iterations"])
        detail = "; ".join(f"Option {p['package_label']} {', '.join(p['avail_ids'])}: {p['rules_passed']}/{p['rules_total']} rules, "
                           f"{p['coverage_pct']:.0%} of impressions, value {p['dollar_label']}" for p in packages) or "no package possible"
        if span := mlflow.get_current_active_span():
            span.set_attributes({"packages.count": len(packages), "packages.full": full,
                                 "packages.best_rules_passed": a["rules_passed"] if a else 0})
        return {"packages": packages, "shortfall": sf, "table_md": package_table(packages),
                "gap": {"full": full, "can_widen": can_widen},
                "trail": _step("build_packages", "Built make-good options with a rule checklist", detail)}

    async def close_gap(state: dict) -> dict:
        cfg = await rules_config()
        it = state.get("gap_iterations", 0) + 1
        horizon = int(cfg["search_horizon_days"]) + int(cfg["gap_widen_horizon_days"]) * it
        search = {"horizon_days": horizon, "max_results": int(cfg["gap_widen_max_results"])}
        a = (state.get("packages") or [{}])[0]
        missing = [c["label"] for c in a.get("checklist") or [] if not c["passed"]]
        detail = (f"No option meets every rule with the impressions and value owed (best is Option A, "
                  f"{a.get('rules_passed', 0)}/{a.get('rules_total', 11)}: misses {', '.join(missing) or 'coverage'}). "
                  f"Searching {horizon} days ahead, up to {search['max_results']} avails (pass {it + 1}).")
        return {"gap_iterations": it, "search": search, "trail": _step("close_gap", "Widened the search to close the gap", detail)}

    # ------------------------------------------------------------------ offer draft + approvals
    async def draft_offer(state: dict, config: RunnableConfig) -> dict:
        s, sel = state["spot"], state["selection"]
        thread_id = config["configurable"]["thread_id"]
        review = state.get("review") or {}
        sender = review.get("reviewer_name") or offer_email.sender_from_email(review.get("reviewer") or current_user())
        f = offer_email.facts(s, sel["packages"], sender_name=sender)
        note = (state.get("manager_decision") or {}).get("note")
        template, version = prompts.load(PROMPT_OFFER)
        resp = await llm.ainvoke([SystemMessage(template), HumanMessage(json.dumps(offer_email.prose_request(s, f, note)))])
        prose, problems = offer_email.clean_prose(_text(resp.content), f)
        ref = ref_token(thread_id)
        subject, text, html = offer_email.render(f, prose, ref)
        with mlflow.start_span(name="guardrail.offer_facts", span_type=SpanType.GUARDRAIL) as g:
            ungrounded = offer_email.check_grounding(text, f, ref)
            g.set_inputs({"email": text, "prose_problems": problems})
            g.set_outputs({"numbers_not_in_facts": ungrounded, "passed": not ungrounded})
        if ungrounded:  # only the template writes numbers, so this means the template and facts disagree
            prose = offer_email.default_prose(f)
            subject, text, html = offer_email.render(f, prose, ref)
        offer_id = (state.get("offer") or {}).get("offer_id") or f"OFR-{uuid.uuid4().hex[:10].upper()}"
        cfg = await rules_config()
        value = sum(p["proposed_value_usd"] for p in sel["packages"])
        needs_approval = value >= float(cfg["approval_required_over_usd"] or 0)
        status = "awaiting_manager" if needs_approval else "approved"
        offer = {"offer_id": offer_id, "thread_id": thread_id, "spot_id": s["spot_id"], "advertiser_id": s["advertiser_id"],
                 "advertiser_name": s["advertiser_name"], "market": s.get("market"), "seller_id": s.get("seller_id"),
                 "seller_name": s.get("seller_name"), "manager_id": s.get("manager_id"), "contact_name": s.get("contact_name"),
                 "contact_email": s.get("contact_email"), "subject": subject, "body_text": text, "body_html": html,
                 "packages": [compact_package(p) for p in sel["packages"]], "ref_token": ref, "status": status,
                 "manager_note": note, "trace_id": state.get("rec_trace_id")}
        if live(state):
            await memory.upsert_offer(offer)
            await memory.set_stage(thread_id, status, offer_id)
        await event(state | {"offer": offer}, config, "drafted", details={"labels": sel["labels"], "prose_problems": problems,
                                                                       "needs_approval": needs_approval})
        mlflow.update_current_trace(tags={"stage": status, "offer_id": offer_id})
        who = "your sales manager" if needs_approval else "you"
        return {"offer": {k: offer[k] for k in ("offer_id", "subject", "status", "ref_token", "contact_name", "contact_email")},
                "stage": status, "manager_decision": None, "llm_usage": [_usage("draft_offer", resp)],
                "messages": [AIMessage(f"Drafted the offer for {s.get('contact_name') or 'the client'} "
                                       f"(Options {', '.join(sel['labels'])}); it goes to {who} before it is sent.\n\n"
                                       f"Subject: {subject}\n\n{text}")],
                "trail": _step("draft_offer", "Drafted the client offer",
                               f"Facts from the rules engine, wording by LLM {llm_endpoint} (prompt {version}); numbers "
                               f"checked: {'all match the options' if not ungrounded else 'template used'}"
                               + (f"; wording replaced ({'; '.join(problems)})" if problems else ""))}

    async def manager_review(state: dict, config: RunnableConfig) -> dict:
        offer = state["offer"]
        if offer["status"] == "approved":
            return {"manager_decision": {"action": "approve", "reviewer": "not required"},
                    "trail": _step("manager_review", "No manager sign-off needed", "Offer value is under the approval limit")}
        decision = interrupt({"type": "manager_review", "offer_id": offer["offer_id"], "spot_id": state["spot_id"],
                              "subject": offer["subject"], "labels": state["selection"]["labels"],
                              "actions": ["approve", "request_changes", "reject"]})
        decision = decision if isinstance(decision, dict) else {"action": str(decision)}
        action, who = decision.get("action"), decision.get("reviewer") or current_user()
        status = {"approve": "approved", "request_changes": "returned", "reject": "returned"}.get(action, "awaiting_manager")
        if live(state):
            await memory.update_offer(offer["offer_id"], status=status, manager_note=decision.get("note"),
                                      **({"approved_by": who, "approved_at": _now()} if action == "approve" else {}))
            await memory.set_stage(config["configurable"]["thread_id"], status)
        await event(state, config, f"manager_{action}", who, {"note": decision.get("note")})
        log_human_feedback(state.get("rec_trace_id"), "manager_decision", action, who, decision.get("note"))
        return {"manager_decision": {**decision, "reviewer": who}, "stage": status, "offer": {**offer, "status": status},
                "trail": _step("manager_review", "Sales manager decision",
                               f"{ {'approve': 'Approved', 'request_changes': 'Asked for changes', 'reject': 'Rejected'}.get(action, action)} by {who}"
                               + (f": {decision['note']}" if decision.get("note") else ""))}

    async def confirm_send(state: dict, config: RunnableConfig) -> dict:
        offer = state["offer"]
        decision = interrupt({"type": "confirm_send", "offer_id": offer["offer_id"], "subject": offer["subject"],
                              "to": offer.get("contact_email"), "actions": ["send", "cancel"]})
        decision = decision if isinstance(decision, dict) else {"action": str(decision)}
        if decision.get("action") != "send" and live(state):
            await memory.update_offer(offer["offer_id"], status="withdrawn")
            await memory.set_stage(config["configurable"]["thread_id"], "recommended")
        return {"send_decision": decision, "trail": _step("confirm_send", "Seller decision on sending",
                                                           "Send" if decision.get("action") == "send" else "Cancelled the offer")}

    async def send_offer(state: dict, config: RunnableConfig) -> dict:
        offer, s = state["offer"], state["spot"]
        thread_id = config["configurable"]["thread_id"]
        row = await memory.get_offer(offer["offer_id"]) if live(state) else None
        if row and row.get("message_id"):  # already sent (this node re-runs after a restart)
            return {"outbound": {"message_id": row["message_id"], "mail_thread_id": row["mail_thread_id"], "sent_to": row["sent_to"]},
                    "stage": "sent", "trail": _step("send_offer", "Offer already sent", row["sent_to"])}
        adapter = get_adapter(memory)
        try:
            to = deliver_to(offer.get("contact_email") or "", tag=s["advertiser_name"])
            check_rate()
            sent = await adapter.send(OutboundMessage(to=to, subject=row["subject"] if row else offer["subject"],
                                                      text=row["body_text"] if row else "", html=row["body_html"] if row else "",
                                                      ref=offer["ref_token"]))
        except (MailRefused, Exception) as e:  # noqa: BLE001 - surface any delivery failure to the seller
            logger.warning("offer %s not sent: %s", offer["offer_id"], e)
            return {"outbound": {"error": str(e)}, "messages": [AIMessage(f"The offer was not sent: {e}")],
                    "trail": _step("send_offer", "Offer not sent", str(e), warning=True)}
        cfg = await rules_config()
        hours = float(cfg["hold_hours"])
        avails = sorted({a for p in state["selection"]["packages"] for a in p["avail_ids"]})
        await memory.update_offer(offer["offer_id"], status="sent", sent_to=sent.sent_to, message_id=sent.message_id,
                                  mail_thread_id=sent.thread_id, sent_by=current_user(), sent_at=_now())
        await memory.log_mail({"message_id": sent.message_id, "offer_id": offer["offer_id"], "thread_id": thread_id,
                               "mail_thread_id": sent.thread_id, "direction": "out", "to_addr": sent.sent_to,
                               "subject": row["subject"], "body_text": row["body_text"], "processed": True})
        await memory.hold_avails(thread_id, s["spot_id"], offer["offer_id"], avails, hours)
        await memory.set_stage(thread_id, "sent")
        await event(state, config, "sent", details={"to": sent.sent_to, "via": adapter.name, "held": avails})
        mlflow.update_current_trace(tags={"stage": "sent"})
        return {"outbound": {"message_id": sent.message_id, "mail_thread_id": sent.thread_id, "sent_to": sent.sent_to},
                "stage": "sent", "offer": {**offer, "status": "sent"},
                "messages": [AIMessage(f"Sent to {sent.sent_to}. {', '.join(avails)} are held for this client for "
                                       f"{hours:.0f} hours; the client's reply will show up here.")],
                "trail": _step("send_offer", "Sent the offer to the client",
                               f"{sent.sent_to} via {adapter.name}; held {', '.join(avails)} for {hours:.0f} h")}

    # ------------------------------------------------------------------ the client's answer
    async def await_client(state: dict, config: RunnableConfig) -> dict:
        offer = state["offer"]
        reply = interrupt({"type": "await_client", "offer_id": offer["offer_id"], "labels": state["selection"]["labels"],
                           "sent_to": (state.get("outbound") or {}).get("sent_to"),
                           "actions": ["client_reply", "manual_reply", "withdraw"]})
        reply = reply if isinstance(reply, dict) else {"action": "manual_reply", "text": str(reply)}
        if reply.get("action") == "withdraw":
            if live(state):
                await memory.release_holds(config["configurable"]["thread_id"])
                await memory.update_offer(offer["offer_id"], status="withdrawn")
                await memory.set_stage(config["configurable"]["thread_id"], "recommended")
            await event(state, config, "withdrawn")
            return {"client_reply": None, "withdrawn": True, "llm_usage": {"reset": []},
                    "trail": _step("await_client", "Offer withdrawn", "", reset=True)}
        source = "the client's email" if reply.get("action") == "client_reply" else f"{reply.get('reviewer') or current_user() or 'the seller'} (entered by hand)"
        return {"client_reply": {**reply, "source": source}, "withdrawn": False, "llm_usage": {"reset": []},
                "trail": _step("await_client", "Client replied", f"From {source}", reset=True)}

    async def parse_client_reply(state: dict, config: RunnableConfig) -> dict:
        reply, offer, sel = state["client_reply"], state["offer"], state["selection"]
        offered = sel["labels"]
        described = "; ".join(f"{p['package_label']} = " + ", ".join(f"{ln['air_date']} {ln['program_name']}" for ln in p["lines"])
                              for p in sel["packages"])
        text = own_text(reply.get("text") or "")
        from_addr = reply.get("from") if reply.get("action") == "client_reply" else ""
        sent_to = (state.get("outbound") or {}).get("sent_to") or ""
        usage, read_by = [], "AI Functions (read_client_email)"
        try:  # store the reply in Unity Catalog and read it there with AI Functions
            read = await read_with_ai_functions(state, config, reply, text, offered)
            with mlflow.start_span(name="guardrail.client_reply", span_type=SpanType.GUARDRAIL) as g:
                parsed = reply_parser.from_ai_read(read, text, offered, from_addr=from_addr, sent_to=sent_to, dayparts=DAYPARTS)
                g.set_inputs({"email": text, "offered": offered, "ai_read": read})
                g.set_outputs(parsed)
        except Exception:  # noqa: BLE001 - the SQL read failed: read it with the LLM instead
            logger.warning("AI Functions read failed; reading the reply with the LLM", exc_info=True)
            template, version = prompts.load(PROMPT_REPLY)
            system = prompts.render(template, options=described, dayparts=", ".join(DAYPARTS))
            resp = await llm.ainvoke([SystemMessage(system), HumanMessage(f"<client_email>\n{text}\n</client_email>")])
            usage, read_by = [_usage("parse_client_reply", resp)], f"LLM {llm_endpoint} (prompt {version})"
            with mlflow.start_span(name="guardrail.client_reply", span_type=SpanType.GUARDRAIL) as g:
                parsed = reply_parser.validate(_text(resp.content), text, offered, from_addr=from_addr, sent_to=sent_to,
                                               dayparts=DAYPARTS)
                g.set_inputs({"email": text, "offered": offered})
                g.set_outputs(parsed)
        if live(state):
            await memory.update_offer(offer["offer_id"], status="replied", reply={**parsed, "text": text[:2000]})
            await memory.set_stage(config["configurable"]["thread_id"], "client_replied")
        await event(state, config, "client_replied", "client", {k: parsed[k] for k in ("decision", "options", "confidence", "notes")})
        log_human_feedback(state.get("rec_trace_id"), "client_decision", parsed["decision"], "client",
                           f"options {parsed['options']}; quote: {parsed['evidence_quote']}")
        what = {"accept": f"picks Option {' and '.join(parsed['options'])}", "decline": "declines the offer",
                "counter": "asks for something different", "question": "has a question", "unclear": "reply is unclear"}[parsed["decision"]]
        msg = (f"Client {what} (confidence {parsed['confidence']:.0%}"
               + (f", \"{parsed['evidence_quote']}\"" if parsed["evidence_quote"] else "") + ")."
               + (f" Question: {parsed['question']}" if parsed["question"] else "")
               + (" Note: " + "; ".join(parsed["notes"]) if parsed["notes"] else "") + " Confirm to continue.")
        mlflow.update_current_trace(tags={"stage": "client_replied", "client_decision": parsed["decision"]})
        return {"client_reply": {**reply, "parsed": parsed}, "stage": "client_replied", "messages": [AIMessage(msg)],
                "llm_usage": usage,
                "trail": _step("parse_client_reply", "Read the client's reply",
                               f"{what}; confidence {parsed['confidence']:.0%}; read by {read_by}"
                               + (f"; {'; '.join(parsed['notes'])}" if parsed["notes"] else ""), warning=bool(parsed["notes"]))}

    async def read_with_ai_functions(state: dict, config: RunnableConfig, reply: dict, text: str, offered: list[str]) -> dict:
        """Write the reply to mg_client_emails, read it with read_client_email (ai_classify + ai_extract), return the reading."""
        s_, offer = state["spot"], state["offer"]
        email_id = reply.get("message_id") or f"manual-{uuid.uuid4().hex[:16]}"
        await write_as_app(
            f"INSERT INTO {fq('mg_client_emails')} (email_id, received_at, offer_id, thread_id, spot_id, advertiser_id, "
            f"advertiser_name, market, seller_id, from_addr, subject, body, offered_options, source) VALUES (:email_id, "
            f"current_timestamp(), :offer_id, :thread_id, :spot_id, :advertiser_id, :advertiser_name, :market, :seller_id, "
            f":from_addr, :subject, :body, from_json(:offered, 'ARRAY<STRING>'), :source)",
            {"email_id": email_id, "offer_id": offer["offer_id"], "thread_id": config["configurable"]["thread_id"],
             "spot_id": s_["spot_id"], "advertiser_id": s_["advertiser_id"], "advertiser_name": s_["advertiser_name"],
             "market": s_.get("market"), "seller_id": s_.get("seller_id"), "from_addr": reply.get("from"),
             "subject": reply.get("subject"), "body": text, "offered": json.dumps(offered),
             "source": "mailbox" if reply.get("action") == "client_reply" else "entered by the seller"})
        await write_as_app(
            f"INSERT INTO {fq('mg_client_email_reads')} SELECT email_id, current_timestamp(), r.intent, r.option_label, "
            f"r.requested_change, r.question FROM (SELECT email_id, {fq('read_client_email')}(body) AS r "
            f"FROM {fq('mg_client_emails')} WHERE email_id = :email_id)", {"email_id": email_id})
        rows = await query_as_app(f"SELECT intent, option_label, requested_change, question FROM {fq('mg_client_email_reads')} "
                                  f"WHERE email_id = :email_id ORDER BY read_at DESC LIMIT 1", "ai_functions.read_client_email",
                                  {"email_id": email_id})
        if not rows:
            raise RuntimeError(f"no reading stored for {email_id}")
        return rows[0]

    async def seller_confirm(state: dict, config: RunnableConfig) -> dict:
        parsed = (state.get("client_reply") or {}).get("parsed") or {}
        sel = state["selection"]
        decision = interrupt({"type": "seller_confirm", "offer_id": state["offer"]["offer_id"], "parsed": parsed,
                              "labels": sel["labels"], "actions": ["book", "counter", "refund", "withdraw"]})
        decision = decision if isinstance(decision, dict) else {"action": str(decision)}
        action, who = decision.get("action"), decision.get("reviewer") or current_user()
        # how the reply was read rides along, so monitoring can check the seller's action against it
        update: dict[str, Any] = {"confirm_decision": {**decision, "reviewer": who, "client_read": parsed.get("decision")}}
        if action == "book":
            labels = [x for x in (decision.get("labels") or parsed.get("options") or []) if x in sel["labels"]]
            chosen = [p for p in sel["packages"] if p["package_label"] in labels]
            ids = [a for p in chosen for a in p["avail_ids"]]
            update["review"] = {"action": "book", "avail_ids": ids, "labels": labels, "reviewer": who,
                                "refund_gap": bool(decision.get("refund_gap")),
                                "reason": decision.get("reason") or f"client chose Option {' and '.join(labels)}"}
        elif action == "counter":
            c = {**(parsed.get("counter") or {}), **(decision.get("constraints") or {})}
            cons = dict(state.get("constraints") or {})
            for k in ("only_days", "avoid_days", "exclude_programs", "only_dayparts"):
                if c.get(k):
                    cons[k] = sorted(set(cons.get(k) or []) | set(c[k]))
            update.update(constraints=cons, gap_iterations=0, search={})
            if live(state):
                await memory.release_holds(config["configurable"]["thread_id"])
                await memory.update_offer(state["offer"]["offer_id"], status="countered")
                await memory.set_stage(config["configurable"]["thread_id"], "recommended")
        elif action == "refund":
            update["review"] = {"action": "refund", "reviewer": who, "reason": decision.get("reason") or "client declined the offer"}
        elif live(state):
            await memory.release_holds(config["configurable"]["thread_id"])
            await memory.update_offer(state["offer"]["offer_id"], status="withdrawn")
        await event(state, config, f"seller_{action}", who, {"labels": decision.get("labels")})
        return update | {"trail": _step("seller_confirm", "Seller decision on the client's reply", f"{action} by {who}")}

    return {"build_packages": build_packages, "close_gap": close_gap, "draft_offer": draft_offer,
            "manager_review": manager_review, "confirm_send": confirm_send, "send_offer": send_offer,
            "await_client": await_client, "parse_client_reply": parse_client_reply, "seller_confirm": seller_confirm}
