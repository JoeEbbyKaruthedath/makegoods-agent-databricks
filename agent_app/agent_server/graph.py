"""The seller-side make-good agent as an explicit LangGraph state machine.

    parse_request ─┬─ open_question ─> ask_genie (Genie Conversation API, governed metric views) ─> END
                   ├─ add_rule ─> propose_rule -> confirm_rule ⏸ -> save_rule ─(client's make-good open)─> search_inventory
                   ├─ refine ─────────────────────────────┐
                   └─ make_good ─> load_spot ─┬─> map_audience ───────┬─> search_inventory ─(none)─> refund_path
                                       │              └─> recall_preferences ─┘   (client rules applied) │
                                       └─(not eligible)─> refund_path                                    v
             score_candidates ─> build_packages ─(no option is 10/10, passes left)─> close_gap ─> search_inventory
                  │                    └─> explain_recommendation (packages A/B/C, dollars, rule checklist)
                  └─(nothing possible)─> refund_path                         v
                         human_review ⏸ (seller; state saved in Lakebase)
                           ├─ offer A/B/C ─> draft_offer ─> manager_review ⏸ ─> confirm_send ⏸ ─> send_offer
                           │                   ─> await_client ⏸ (days) ─> parse_client_reply ─> seller_confirm ⏸
                           │                         ├─ book ─> finalize   ├─ counter ─> search_inventory   └─ refund ─> finalize
                           ├─ check my selection ─> review_selection ─> human_review
                           ├─ book / accept / modify / refund ─> finalize ─> END
                           ├─ reject ─> search_inventory (rejected avail excluded)
                           └─ refine / add a rule (free text) ─> parse_request

The LLM routes requests, explains recommendations, writes the offer email's prose and reads the client's reply;
open questions are answered by a Genie space. Every business rule, package and dollar figure comes from a Unity
Catalog function (see uc_tools.call_uc). Each node is a span in the MLflow trace and appends a plain-English step
to `trail` for the review UI. The offer lifecycle nodes are in nodes_offer.py, client rules in nodes_rules.py.
"""
import asyncio
import json
import logging
import re
from typing import Annotated, Any, Literal, Optional, TypedDict

import mlflow
from databricks_langchain import ChatDatabricks
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from mlflow.entities import AssessmentSource, AssessmentSourceType, SpanType
from pydantic import BaseModel, Field

from agent_server import genie, prompts
from agent_server import rules as R
from agent_server.config import LLM_ENDPOINT, PROMPT_EXPLAIN, PROMPT_PARSE, fq, llm_params
from agent_server.memory import Memory
from agent_server.nodes_offer import LABEL_ORDER, compact_package, make_offer_nodes, rules_config
from agent_server.nodes_rules import make_rule_nodes
from agent_server.steps import (ADV_RE, AVAIL_RE, DAYPARTS, SPOT_RE, TAGS, _accumulate, _fmt_rules, _last_human_text,
                                 _live, _step, _text, _usage, traced_node)
from agent_server.uc_tools import call_uc, current_user, query, write_as_app

logger = logging.getLogger(__name__)
_DOLLARS = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


class RuleDraft(BaseModel):
    advertiser: Optional[str] = None
    type: Optional[str] = None
    values: list[str] = Field(default_factory=list)
    strength: Optional[str] = None


class ParsedRequest(BaseModel):
    """What the user wants from the make-good agent."""
    intent: Literal["make_good", "refine", "add_rule", "open_question"]
    spot_id: Optional[str] = Field(None, description="Spot id like SPT-0058222, only if present in the message")
    exclude_tags: list[str] = Field(default_factory=list, description="Adjacency tags to avoid")
    only_dayparts: list[str] = Field(default_factory=list, description="Dayparts to restrict to")
    exclude_avail_ids: list[str] = Field(default_factory=list, description="Avail ids the user rejects")
    rule: Optional[RuleDraft] = None


class MakeGoodState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    custom_inputs: dict[str, Any]
    intent: str
    spot_id: str
    as_of: Optional[str]
    constraints: dict[str, list[str]]
    spot: dict
    audience: dict
    prefs: dict[str, float]
    base_prefs: dict[str, float]
    rules: list[dict]
    rule_params: dict
    rule_summary: dict[str, int]
    candidates: list[dict]
    ranked: list[dict]
    search: dict
    gap_iterations: int
    gap: dict
    packages: list[dict]
    suggestions: list[dict]
    shortfall: dict
    table_md: str
    explanation: str
    outcome: str
    rec_trace_id: Optional[str]
    review: Optional[dict]
    selection: Optional[dict]
    offer: Optional[dict]
    manager_decision: Optional[dict]
    send_decision: Optional[dict]
    outbound: Optional[dict]
    client_reply: Optional[dict]
    withdrawn: bool
    confirm_decision: Optional[dict]
    stage: str
    pending_rule: Optional[dict]
    rule_decision: Optional[dict]
    genie_conversation_id: Optional[str]
    genie: Optional[dict]
    trail: Annotated[list[dict], _accumulate]
    llm_usage: Annotated[list[dict], _accumulate]


def _parse_json(output: str, text: str) -> ParsedRequest:
    """Validate the router LLM's JSON; fall back to a regex read of the message if it is malformed."""
    m = re.search(r"\{.*\}", output, re.S)
    if m:
        try:
            return ParsedRequest.model_validate_json(m.group(0))
        except Exception:
            pass
    spot = SPOT_RE.search(text)
    return ParsedRequest(intent="make_good" if spot else "open_question", spot_id=spot.group(0) if spot else None)


def _search_args(state: dict) -> dict:
    """Arguments for inventory_search: reviewer constraints for this make-good + the client's standing rules."""
    c, rp, s = state.get("constraints") or {}, state.get("rule_params") or {}, state.get("search") or {}
    merged = lambda a, b: sorted(set(a or []) | set(b or [])) or None  # noqa: E731
    return {"p_exclude_tags": merged(c.get("exclude_tags"), rp.get("exclude_tags")),
            "p_only_dayparts": c.get("only_dayparts") or None,
            "p_exclude_programs": merged(c.get("exclude_programs"), rp.get("exclude_programs")),
            "p_exclude_genres": rp.get("exclude_genres") or None,
            "p_only_dows": merged(c.get("only_days"), rp.get("only_dows")),
            "p_avoid_dows": merged(c.get("avoid_days"), rp.get("avoid_dows")),
            "p_horizon_days": s.get("horizon_days"), "p_max_results": s.get("max_results") or 25}


def build_graph(memory: Optional[Memory] = None, llm_endpoint: str = LLM_ENDPOINT):
    llm = ChatDatabricks(endpoint=llm_endpoint, extra_params=llm_params(llm_endpoint))
    offer_nodes = make_offer_nodes(llm, llm_endpoint, memory)
    rule_nodes = make_rule_nodes(memory)

    # ------------------------------------------------------------------ nodes
    async def parse_request(state: MakeGoodState) -> dict:
        ci = state.get("custom_inputs") or {}
        text = _last_human_text(state)
        current = state.get("spot_id")
        usage: list[dict] = []
        if ci.get("mode") == "find" and ci.get("spot_id"):
            p, via = ParsedRequest(intent="make_good", spot_id=ci["spot_id"]), "UI action (routed without an LLM call)"
        else:
            template, version = prompts.load(PROMPT_PARSE)
            system = prompts.render(template, tags=", ".join(TAGS), dayparts=", ".join(DAYPARTS),
                                    current_spot=(f"{current} ({(state.get('spot') or {}).get('advertiser_name')})"
                                                  if current else "none"))
            raw = await llm.ainvoke([SystemMessage(system), HumanMessage(text)])
            p = _parse_json(_text(raw.content), text)
            usage = [_usage("parse_request", raw)]
            via = f"LLM {llm_endpoint} (prompt {version})"
            if span := mlflow.get_current_active_span():
                span.set_attribute("prompt.parse_version", version)
        # guardrails: never trust an id the user did not actually provide
        spot = p.spot_id.upper() if p.spot_id and (p.spot_id.upper() in text.upper() or ci.get("spot_id", "").upper() == p.spot_id.upper()) else None
        tags = [t for t in p.exclude_tags if t in TAGS]
        dayparts = [d for d in p.only_dayparts if d in DAYPARTS]
        avails = [a for a in p.exclude_avail_ids if AVAIL_RE.fullmatch(a)]
        intent = p.intent
        if intent == "add_rule" and not (p.rule and p.rule.type):
            intent = "refine" if current else "open_question"
        if intent == "make_good" and not spot:
            intent = "refine" if current else "open_question"
        if intent == "make_good" and current and spot == current and (tags or dayparts or avails):
            intent = "refine"
        if intent == "refine" and not current:
            intent = "open_question"

        update: dict[str, Any] = {"intent": intent, "llm_usage": {"reset": usage}, "as_of": ci.get("as_of")}
        if intent == "make_good":
            update.update(spot_id=spot, constraints={"exclude_tags": tags, "only_dayparts": dayparts, "exclude_avail_ids": avails},
                          spot={}, audience={}, prefs={}, base_prefs={}, rules=[], rule_params={}, candidates=[], ranked=[],
                          search={}, gap_iterations=0, gap={}, packages=[], suggestions=[], shortfall={}, table_md="",
                          explanation="", outcome="", rec_trace_id=None, review=None, selection=None, offer=None,
                          manager_decision=None, outbound=None, client_reply=None, stage="")
            detail = f"Find a commensurate make-good for {spot}" + (f"; constraints {update['constraints']}" if tags or dayparts or avails else "")
        elif intent == "refine":
            c = dict(state.get("constraints") or {})
            c["exclude_tags"] = sorted(set(c.get("exclude_tags", [])) | set(tags))
            c["exclude_avail_ids"] = sorted(set(c.get("exclude_avail_ids", [])) | set(avails))
            c["only_dayparts"] = dayparts or c.get("only_dayparts", [])
            update.update(constraints=c, gap_iterations=0, search={})
            detail = f"Refine the make-good for {current}: {c}"
        elif intent == "add_rule":
            update["pending_rule"] = {**p.rule.model_dump(), "source_text": text}
            detail = f"A client rule: {p.rule.type} {', '.join(p.rule.values)}" + (f" for {p.rule.advertiser}" if p.rule.advertiser else "")
        else:
            detail = "Open question, routed to the Genie space"
        mlflow.update_current_trace(tags={"intent": intent, **({"spot_id": spot or current} if (spot or current) else {})})
        update["trail"] = _step("parse_request", "Understood the request", f"{detail}. Via {via}.", reset=True)
        return update

    async def load_spot(state: MakeGoodState) -> dict:
        rows = await call_uc("spot_context", p_spot_id=state["spot_id"])
        if not rows:  # also when the spot belongs to a client this person may not see
            return {"outcome": "not_found", "messages": [AIMessage(f"I couldn't find spot {state['spot_id']} among your accounts.")],
                    "trail": _step("load_spot", "Spot not found")}
        s = rows[0]
        mlflow.update_current_trace(tags={"advertiser_id": s["advertiser_id"], "station": s["call_letters"],
                                          **({"market": s["market"]} if s.get("market") else {})})
        if s["status"] == "Aired":
            return {"spot": s, "outcome": "not_preempted",
                    "messages": [AIMessage(f"{s['spot_id']} aired as scheduled on {s['broadcast_date']}; no make-good is owed.")],
                    "trail": _step("load_spot", "Spot aired normally", "No make-good needed")}
        detail = (f"{s['advertiser_name']} · {s['call_letters']} ({s['market']}) · {s['daypart']} {s['broadcast_date']} · "
                  f"{s['preemption_reason']} · ${s['unit_rate_usd']:,.0f} at risk · contract audience \"{s['audience_segment_text']}\", "
                  f"guaranteed rating {s['guaranteed_rating']}, deadline {s['makegood_deadline']}"
                  + (f" · seller {s['seller_name']}" if s.get("seller_name") else ""))
        return {"spot": s, "trail": _step("load_spot", "Loaded spot and contract", detail)}

    async def map_audience(state: MakeGoodState) -> dict:
        s = state["spot"]
        rows = await call_uc("audience_mapper", p_advertiser_id=s["advertiser_id"], p_audience_text=s["audience_segment_text"])
        a = rows[0] if rows else {"match_type": "unmapped", "confidence": 0.0}
        detail = (f"\"{s['audience_segment_text']}\" → {a.get('audience_segment_id')} {a.get('segment_code')} "
                  f"({a['match_type']}, confidence {a['confidence']:.1f})")
        return {"audience": a, "trail": _step("map_audience", "Mapped the contracted audience", detail,
                                              warning=a["confidence"] < 0.9)}

    async def recall_preferences(state: MakeGoodState) -> dict:
        adv = state["spot"]["advertiser_id"]
        assert ADV_RE.fullmatch(adv)
        ci = state.get("custom_inputs") or {}
        base = await query(f"SELECT adjacency_tag, accepts, rejects FROM {fq('gold_advertiser_preferences')} "
                           f"WHERE advertiser_id = :adv", "memory.baseline_preferences", {"adv": adv})
        counts = {r["adjacency_tag"]: [int(r["accepts"]), int(r["rejects"])] for r in base}
        live: dict = {}
        rules: list[dict] = list(ci.get("rules") or []) if ci.get("eval") else []
        with mlflow.start_span(name="memory.lakebase_preferences", span_type=SpanType.MEMORY) as span:
            span.set_inputs({"namespaces": [["advertiser_prefs", adv], ["advertiser_rules", adv]]})
            if _live(state, memory) and ci.get("memory") != "baseline":
                live = await memory.pref_evidence(adv)
            if _live(state, memory):
                rules = await memory.rules(adv)
            span.set_outputs({"preference_evidence": live, "client_rules": [r.get("label") for r in rules]})
        for tag, ev in live.items():
            c = counts.setdefault(tag, [0, 0])
            c[0] += ev.get("accepts", 0)
            c[1] += ev.get("rejects", 0)
        prefs = {t: round((a - r) / (a + r + 1), 3) for t, (a, r) in counts.items() if abs((a - r) / (a + r + 1)) >= 0.05}
        rp = R.to_params(rules, prefs)
        name = state["spot"]["advertiser_name"]
        if prefs:
            detail = f"{name}: " + ", ".join(f"{t} {w:+.2f}" for t, w in sorted(prefs.items(), key=lambda kv: -abs(kv[1])))
            detail += f" ({len(base)} tags learned from history, {len(live)} updated by reviewers in Lakebase)"
        else:
            detail = f"{name}: no adjacency preferences learned yet"
        if rules:
            detail += f"; client rules: {'; '.join(rp['labels'])}"
        return {"prefs": rp["prefs"], "base_prefs": prefs, "rules": rules, "rule_params": rp,
                "trail": _step("recall_preferences", "Recalled advertiser preferences and client rules", detail)}

    async def search_inventory(state: MakeGoodState, config: RunnableConfig) -> dict:
        c = state.get("constraints") or {}
        exclude = list(c.get("exclude_avail_ids", []))
        if _live(state, memory):
            exclude += await memory.booked_avails(exclude_thread=config["configurable"]["thread_id"])
        args = _search_args(state)
        cands, summary_rows = await asyncio.gather(
            call_uc("inventory_search", p_spot_id=state["spot_id"], p_exclude_avail_ids=exclude or None,
                    p_as_of=state.get("as_of"), p_include_near_misses=True, **args),
            call_uc("inventory_rule_summary", p_spot_id=state["spot_id"], p_as_of=state.get("as_of")))
        summary = {r["rule"]: int(r["avails"]) for r in summary_rows}
        n_comm = sum(1 for x in cands if x["is_commensurate"])
        rp = state.get("rule_params") or {}
        detail = (f"Evaluated {summary.get('evaluated', 0)} avails on {state['spot']['call_letters']}: "
                  f"{summary.get('commensurate', 0)} pass every must-match rule"
                  + (f", {n_comm} still available after excluding avails already booked, held or rejected, reviewer constraints"
                     f" and client rules" if n_comm != summary.get("commensurate", 0) else "")
                  + f"; {len(cands) - n_comm} other possible options (soft rule misses only) kept for scoring."
                  + f" Most common failures: {_fmt_rules(summary)}."
                  + (f" Search widened to {args['p_horizon_days']} days." if args.get("p_horizon_days") else "")
                  + (f" Client rules applied: {'; '.join(rp['labels'])}." if rp.get("labels") else ""))
        span = mlflow.get_current_active_span()
        if span:
            span.set_attributes({"inventory.evaluated": summary.get("evaluated", 0),
                                 "inventory.commensurate": summary.get("commensurate", 0),
                                 "inventory.near_misses": len(cands) - n_comm,
                                 "inventory.rule_failures": json.dumps(summary),
                                 "inventory.client_rules": json.dumps(rp.get("labels") or [])})
        return {"candidates": cands, "rule_summary": summary,
                "trail": _step("search_inventory", "Searched inventory with the commensurate rules", detail)}

    async def score_candidates(state: MakeGoodState) -> dict:
        ids = [c["avail_id"] for c in state["candidates"]]
        rp = state.get("rule_params") or {}
        rows = await call_uc("commensurate_scorer", p_spot_id=state["spot_id"], p_avail_ids=ids,
                             p_prefs=state.get("prefs") or None, p_as_of=state.get("as_of"),
                             p_prefer_dows=rp.get("prefer_dows") or None,
                             p_horizon_days=(state.get("search") or {}).get("horizon_days"))
        ranked = [r for r in sorted(rows, key=lambda r: r["rank"]) if r["tier"] in ("commensurate", "near_miss", "poor_fit")]
        label = {"commensurate": "commensurate", "near_miss": "near-miss", "poor_fit": "low match"}
        detail = "; ".join(f"#{r['rank']} {r['avail_id']} {r['program_name']} {r['air_date']} {r['match_score']}/10 "
                           f"{label[r['tier']]}" + (f" ({'; '.join(r['misses'])})" if r["misses"] else "")
                           for r in ranked[:3]) or "no possible option"
        return {"ranked": ranked, "trail": _step("score_candidates", "Scored the options out of 10", detail)}

    async def explain_recommendation(state: MakeGoodState) -> dict:
        s, a = state["spot"], state.get("audience") or {}
        keep = ("rank", "match_score", "tier", "misses", "avail_id", "air_date", "program_name", "est_impressions", "reasons")
        # commensurate -> make-good; near-miss (>= near_miss_min_score) -> exception for approval; below it -> refund,
        # with the best options still shown so the reviewer can overrule
        outcome = {"commensurate": "make_good", "near_miss": "near_miss"}.get(state["ranked"][0]["tier"], "refund")
        packages = state.get("packages") or []
        payload = {
            "spot": {k: s[k] for k in ("spot_id", "advertiser_name", "daypart", "broadcast_date", "program_name",
                                       "preemption_reason", "unit_rate_usd")},
            "outcome": outcome,
            "audience": a.get("segment_code"),
            "owed_value_usd": s["unit_rate_usd"],
            "advertiser_preferences_applied": dict(sorted((state.get("base_prefs") or state.get("prefs") or {}).items(),
                                                          key=lambda kv: -abs(kv[1]))[:3]),
            "client_rules_applied": (state.get("rule_params") or {}).get("labels") or [],
            **({"search_passes": state["gap_iterations"] + 1} if state.get("gap_iterations") else {}),
            "options": [{**{k: r[k] for k in keep}, "reasons": [x for x in r["reasons"] if not x.startswith(("NEAR MISS", "LOW MATCH"))][:3]}
                        for r in state["ranked"][:2]],
            "packages": [{"label": p["package_label"], "avail_ids": p["avail_ids"], "est_impressions": p["est_impressions"],
                          "coverage_pct": p["coverage_pct"], "proposed_value_usd": p["proposed_value_usd"],
                          "dollar_label": p["dollar_label"],
                          **({"partial_refund_usd": p["partial_refund_usd"]} if p["partial_refund_usd"] else {}),
                          "failed_rules": [c["label"] for c in p["checklist"] if not c["passed"]],
                          # what Option A airs: the spots may sit outside the top-ranked options listed above
                          **({"lines": [{k: ln.get(k) for k in ("avail_id", "air_date", "daypart", "program_name", "program_genre",
                                                                 "match_score")} for ln in p.get("lines") or []]} if i == 0 else {})}
                         for i, p in enumerate(packages)],
            "owed_impressions": (state.get("shortfall") or {}).get("owed_impressions"),
        }
        template, version = prompts.load(PROMPT_EXPLAIN)
        resp = await llm.ainvoke([SystemMessage(template), HumanMessage(json.dumps(payload, default=str, separators=(",", ":")))])
        explanation = _text(resp.content).strip()
        summary = state.get("rule_summary") or {}
        others = [f"Option {p['package_label']} ({', '.join(p['avail_ids'])}: {p['rules_passed']}/{p['rules_total']} rules, {p['dollar_label']})"
                  for p in packages[1:]]
        explanation += ((f"\n\nAlso available: {'; '.join(others)}." if others else "") +
                        f"\n\nChecked {summary.get('evaluated', 0)} avails: {summary.get('commensurate', 0)} pass every must-match rule, "
                        f"{sum(1 for r in state['ranked'] if r['tier'] == 'near_miss')} near-misses considered. "
                        f"Most common misses: {_fmt_rules(summary)}.")
        with mlflow.start_span(name="guardrail.avail_ids", span_type=SpanType.GUARDRAIL) as g:
            known = {r["avail_id"] for r in state["ranked"]}
            invented = sorted(set(AVAIL_RE.findall(explanation)) - known)
            g.set_inputs({"explanation": explanation})
            g.set_outputs({"invented_avail_ids": invented, "passed": not invented})
        with mlflow.start_span(name="guardrail.dollar_figures", span_type=SpanType.GUARDRAIL) as g:
            facts = {round(float(v)) for v in [s["unit_rate_usd"]] + [x for p in packages for x in
                     (p["owed_value_usd"], p["proposed_value_usd"], abs(p["value_gap_usd"] or 0), p["partial_refund_usd"])]
                     if v is not None}
            stated = {int(m.replace(",", "")) for m in _DOLLARS.findall(explanation)}
            wrong = sorted(v for v in stated if not any(abs(v - f) <= 1 for f in facts))
            g.set_inputs({"stated_usd": sorted(stated), "facts_usd": sorted(facts)})
            g.set_outputs({"not_in_facts": wrong, "passed": not wrong})
        if invented:
            explanation += f"\n\n⚠️ Guardrail: ignore {', '.join(invented)}; only the avails in the table are valid."
        if wrong:
            explanation += "\n\n⚠️ Guardrail: use the dollar figures in the options table; some amounts above don't match it."
        if a.get("confidence", 1) < 0.9:
            explanation += (f"\n\n⚠️ The contract audience \"{s['audience_segment_text']}\" was inferred as {a.get('segment_code')} "
                            f"({a.get('match_type')}); please confirm.")
        span = mlflow.get_current_active_span()
        if span:
            span.set_attribute("prompt.explain_version", version)
        mlflow.update_current_trace(tags={"outcome": outcome, "best_avail_id": state["ranked"][0]["avail_id"],
                                          "best_match_score": str(state["ranked"][0]["match_score"]),
                                          **({"best_package_rules": f"{packages[0]['rules_passed']}/{packages[0]['rules_total']}",
                                              "dollar_match": str(packages[0]["dollar_match"]).lower()} if packages else {})})
        guard = "passed" if not invented and not wrong else "flagged " + ", ".join(invented + [f"${v:,}" for v in wrong])
        return {"explanation": explanation, "outcome": outcome, "rec_trace_id": mlflow.get_active_trace_id(), "stage": "recommended",
                "messages": [AIMessage(f"{explanation}\n\n{state['table_md']}")],
                "llm_usage": [_usage("explain_recommendation", resp)],
                "trail": _step("explain_recommendation", "Wrote the recommendation",
                               f"LLM {llm_endpoint} (prompt {version}); guardrail {guard}")}

    async def refund_path(state: MakeGoodState) -> dict:
        s, summary = state["spot"], state.get("rule_summary") or {}
        if not s.get("makegood_eligible", True):
            why = "the contract is not make-good eligible"
        else:
            why = (f"none of the {summary.get('evaluated', 0)} avails on {s['call_letters']} pass every must-match rule "
                   f"({_fmt_rules(summary)}) and no near-miss scores high enough to offer")
            if (state.get("constraints") or {}).get("exclude_tags") or (state.get("constraints") or {}).get("exclude_avail_ids") \
                    or (state.get("rule_params") or {}).get("labels"):
                why += " after applying your constraints and the client's rules"
        text = (f"No commensurate or near-miss make-good exists for {s['spot_id']} ({s['advertiser_name']}, {s['call_letters']} "
                f"{s['daypart']} {s['broadcast_date']}): {why}. Recommendation: refund ${s['unit_rate_usd']:,.0f} to "
                f"{s['advertiser_name']}.")
        mlflow.update_current_trace(tags={"outcome": "refund"})
        return {"outcome": "refund", "ranked": [], "packages": [], "rec_trace_id": mlflow.get_active_trace_id(),
                "explanation": text, "stage": "recommended", "messages": [AIMessage(text)],
                "trail": _step("refund_path", "Recommended a refund", why + " (deterministic, no LLM call)")}

    async def human_review(state: MakeGoodState, config: RunnableConfig) -> dict:
        if not _live(state, memory) or (state.get("custom_inputs") or {}).get("no_review"):
            return {"review": None}
        s, ranked, packages = state["spot"], state.get("ranked") or [], state.get("packages") or []
        thread_id = config["configurable"]["thread_id"]
        await memory.upsert_review({
            "thread_id": thread_id, "spot_id": s["spot_id"], "advertiser_id": s["advertiser_id"],
            "advertiser_name": s["advertiser_name"], "station": s["call_letters"], "daypart": s["daypart"],
            "revenue_at_risk_usd": s["unit_rate_usd"], "outcome": state["outcome"],
            "recommended_avail_id": ranked[0]["avail_id"] if ranked else None, "ranked": ranked[:5],
            "table_md": state.get("table_md"), "explanation": state.get("explanation"), "trail": state.get("trail"),
            "trace_id": state.get("rec_trace_id"), "market": s.get("market"), "seller_id": s.get("seller_id"),
            "stage": "recommended", "packages": [compact_package(p) for p in packages]})
        actions = (["accept", "modify", "reject"] if state["outcome"] == "refund" else
                   ["accept", "modify", "reject", "refine"] + (["refund"] if state["outcome"] == "near_miss" else [])) \
            if ranked else ["accept", "reject"]
        if ranked:
            actions += ["book", "offer", "review_selection", "add_rule"]
        decision = interrupt({"type": "make_good_review", "spot_id": s["spot_id"], "outcome": state["outcome"],
                              "options": [r["avail_id"] for r in ranked[:5]],
                              "packages": [p["package_label"] for p in packages], "actions": actions})
        decision = decision if isinstance(decision, dict) else {"action": str(decision)}
        action = decision.get("action")
        decision["reviewer"] = decision.get("reviewer") or current_user()
        update: dict[str, Any] = {"review": decision, "llm_usage": {"reset": []}, "selection": None,
                                  "trail": _step("human_review", "Reviewer decision", f"{action} by "
                                                 f"{decision.get('reviewer') or 'reviewer'}", reset=True)}
        if action in ("refine", "add_rule"):
            update["messages"] = [HumanMessage(decision.get("text", ""))]
            # the text goes to the router: drop the "Find make-good" button input that started this thread
            update["custom_inputs"] = {k: v for k, v in (state.get("custom_inputs") or {}).items() if k not in ("mode", "spot_id")}
        elif action == "reject" and ranked:
            top = ranked[0]
            prefs = await memory.add_pref_evidence(s["advertiser_id"], [], top["adjacency_tags"])
            await memory.log_decision({"thread_id": thread_id, "spot_id": s["spot_id"], "advertiser_id": s["advertiser_id"],
                                       "action": "reject", "outcome": "option_rejected", "recommended_avail_id": top["avail_id"],
                                       "reviewer": decision.get("reviewer"), "reason": decision.get("reason"),
                                       "recommendation_trace_id": state.get("rec_trace_id"), "pref_updates": prefs,
                                       "market": s.get("market"), "seller_id": s.get("seller_id")})
            _log_review_feedback(state.get("rec_trace_id"), decision)
            c = dict(state.get("constraints") or {})
            c["exclude_avail_ids"] = sorted(set(c.get("exclude_avail_ids", [])) | {top["avail_id"]})
            update.update(constraints=c, gap_iterations=0, search={})
        elif action == "offer":
            labels = [x for x in decision.get("labels") or [] if x in {p["package_label"] for p in packages}]
            chosen = [p for p in packages if p["package_label"] in labels]
            if not chosen:  # stays at human_review (after_review: offer without a selection)
                update["messages"] = [AIMessage("Pick at least one of the options (A, B, C) to offer the client.")]
            else:
                update["selection"] = {"labels": [p["package_label"] for p in chosen], "packages": chosen}
                await memory.log_event(thread_id, "options_selected", decision["reviewer"], spot=s, details={"labels": labels},
                                       trace_id=state.get("rec_trace_id"))
                log = {"labels": labels, "first": packages[0]["package_label"] if packages else None}
                _log_review_feedback(state.get("rec_trace_id"), {**decision, "reason": decision.get("reason") or json.dumps(log)})
        return update

    async def review_selection(state: MakeGoodState) -> dict:
        """'Check my selection': how the seller's own picks fall short, and the avails that would close the gap."""
        ids = [a for a in (state.get("review") or {}).get("avail_ids") or [] if AVAIL_RE.fullmatch(a)]
        known = {r["avail_id"] for r in state.get("ranked") or []}
        ids = [a for a in ids if a in known]
        if not ids:
            return {"review": None, "messages": [AIMessage("Select one or more of the listed avails first.")],
                    "trail": _step("review_selection", "Nothing selected")}
        rp = state.get("rule_params") or {}
        pk, fillers = await asyncio.gather(
            call_uc("package_builder", p_spot_id=state["spot_id"], p_avail_ids=[r["avail_id"] for r in state["ranked"]],
                    p_prefs=state.get("prefs") or None, p_as_of=state.get("as_of"), p_prefer_dows=rp.get("prefer_dows") or None,
                    p_horizon_days=(state.get("search") or {}).get("horizon_days"), p_fixed_avail_ids=ids,
                    p_client_rules=rp.get("labels") or None),
            call_uc("gap_fillers", p_spot_id=state["spot_id"], p_selected_ids=ids,
                    p_candidate_ids=[r["avail_id"] for r in state["ranked"]], p_prefs=state.get("prefs") or None,
                    p_as_of=state.get("as_of")))
        packages = [p for p in state.get("packages") or [] if p["package_label"] != "S"] + pk
        s_pk = pk[0] if pk else None
        if s_pk and s_pk["is_full"]:
            text = (f"Your selection ({', '.join(ids)}) passes all {s_pk['rules_total']} rules: {s_pk['coverage_pct']:.0%} of the "
                    f"impressions owed, value {s_pk['dollar_label']}. It is listed as Option S; you can offer it as is.")
        elif s_pk:
            fails = [c["label"] for c in s_pk["checklist"] if not c["passed"]]
            text = (f"Your selection ({', '.join(ids)}) covers {s_pk['coverage_pct']:.0%} of the impressions owed, value "
                    f"{s_pk['dollar_label']}; it misses: {'; '.join(fails)}.")
            if fillers:
                text += " To close the gap, add one of: " + "; ".join(
                    f"{f['avail_id']} ({f['program_name']} {f['air_date']}, {f['match_score']}/10): {f['reason']}" for f in fillers) + "."
        else:
            text = "Those avails can't be combined into a make-good for this spot."
        return {"packages": packages, "suggestions": fillers, "review": None, "messages": [AIMessage(text)],
                "trail": _step("review_selection", "Checked your selection", text)}

    async def finalize(state: MakeGoodState, config: RunnableConfig) -> dict:
        r, s, ranked = state["review"], state["spot"], state.get("ranked") or []
        action, reviewer = r.get("action"), r.get("reviewer") or current_user() or "ad-ops reviewer"
        thread_id = config["configurable"]["thread_id"]
        top = ranked[0] if ranked else None
        offer = state.get("offer") or {}
        accepted, rejected, finals, refund, booked_value = [], [], [], None, None
        lines = {ln["avail_id"]: ln for p in state.get("packages") or [] for ln in p["lines"]} | {x["avail_id"]: x for x in ranked}
        if action == "reject":  # reviewer declined a refund recommendation
            outcome = "escalated"
            text = f"Refund for {s['spot_id']} not approved by {reviewer}; escalated to sales for manual handling."
        elif action == "refund" or not top or (action == "accept" and state.get("outcome") == "refund"):
            outcome, refund = "refund", s["unit_rate_usd"]
            text = f"Refund of ${refund:,.0f} to {s['advertiser_name']} for {s['spot_id']} approved by {reviewer} and logged."
        else:
            if action == "book":
                ids = list(r.get("avail_ids") or [])
                if r.get("label") and not ids:
                    ids = next((p["avail_ids"] for p in state.get("packages") or [] if p["package_label"] == r["label"]), [])
            else:
                ids = [r.get("avail_id") if action == "modify" else top["avail_id"]]
            finals = [lines.get(a) for a in ids]
            if not ids or any(f is None for f in finals):
                return {"messages": [AIMessage(f"{', '.join(a for a in ids if a not in lines) or 'That choice'} is not one of the "
                                               f"options ({', '.join(x['avail_id'] for x in ranked[:5])}). Pick one of those.")],
                        "review": None, "trail": _step("finalize", "Rejected an invalid choice", ", ".join(ids))}
            outcome = "make_good" if all(f["tier"] == "commensurate" for f in finals) else "make_good_exception"
            accepted = sorted({t for f in finals for t in f.get("adjacency_tags") or []})
            if top["avail_id"] not in ids:
                rejected = [t for t in top["adjacency_tags"] if t not in accepted]
            sf = await call_uc("shortfall_calculator", p_spot_id=s["spot_id"], p_avail_ids=ids)
            sf = sf[0] if sf else {}
            booked_value = sf.get("proposed_value_usd")
            if r.get("refund_gap") and (sf.get("partial_refund_usd") or 0) > 0:
                refund = sf["partial_refund_usd"]
            text = (f"Booked {', '.join(f['avail_id'] for f in finals)} ("
                    + "; ".join(f"{f['program_name']}, {f['daypart']} {f['air_date']}" for f in finals)
                    + f") as the make-good for {s['spot_id']}: {sf.get('proposed_impressions', 0):,} est. impressions of "
                    f"{sf.get('owed_impressions', 0):,} owed, value {sf.get('dollar_label', '')}. Approved by {reviewer}.")
            if refund:
                text += f" Partial refund of ${refund:,.0f} for the value gap."
            misses = [m for f in finals for m in f.get("misses") or []]
            if outcome == "make_good_exception" and misses:
                text += f" Approved as an exception to the make-good rules: {'; '.join(misses)}."
        prefs = await memory.add_pref_evidence(s["advertiser_id"], accepted, rejected) if accepted or rejected else {}
        await memory.log_decision({
            "thread_id": thread_id, "spot_id": s["spot_id"], "advertiser_id": s["advertiser_id"], "action": action,
            "outcome": outcome, "recommended_avail_id": top["avail_id"] if top else None,
            "final_avail_id": finals[0]["avail_id"] if finals else None, "final_avail_ids": [f["avail_id"] for f in finals] or None,
            "refund_usd": refund, "reviewer": reviewer, "reason": r.get("reason"),
            "recommendation_trace_id": state.get("rec_trace_id"), "pref_updates": prefs, "market": s.get("market"),
            "seller_id": s.get("seller_id"), "offer_id": offer.get("offer_id"), "booked_value_usd": booked_value,
            "owed_value_usd": s["unit_rate_usd"]})
        await memory.resolve_review(thread_id, s["spot_id"], "decided")
        await memory.release_holds(thread_id)
        if offer.get("offer_id"):
            await memory.update_offer(offer["offer_id"], status="booked" if finals else "refunded")
        await memory.log_event(thread_id, outcome, reviewer, offer_id=offer.get("offer_id"), spot=s,
                               details={"avail_ids": [f["avail_id"] for f in finals], "refund_usd": refund},
                               trace_id=state.get("rec_trace_id"))
        await _decision_event(state, thread_id, outcome, reviewer, [f["avail_id"] for f in finals], refund, booked_value)
        _log_review_feedback(state.get("rec_trace_id"), {**r, "action": "accept" if action == "book" and finals and
                                                         top and finals[0]["avail_id"] == top["avail_id"] else
                                                         ("modify" if action == "book" else action)},
                             finals[0]["avail_id"] if finals else None)
        if prefs:
            text += (f" Preference memory for {s['advertiser_name']} updated: "
                     + ", ".join([f"+{t}" for t in accepted] + [f"−{t}" for t in rejected]) + ".")
        mlflow.update_current_trace(tags={"review_action": action, "outcome": outcome, "stage": "closed"})
        return {"outcome": outcome, "stage": "closed", "messages": [AIMessage(text)],
                "trail": _step("finalize", "Logged the decision", text)}

    async def _decision_event(state, thread_id, outcome, actor, avail_ids, refund, booked_value) -> None:
        """Mirror the decision to Unity Catalog (row-filtered) for business results and analytics."""
        s = state["spot"]
        try:
            await write_as_app(
                f"INSERT INTO {fq('mg_decision_events')} (event_at, thread_id, offer_id, spot_id, advertiser_id, market, "
                f"seller_id, outcome, final_avail_ids, refund_usd, owed_value_usd, booked_value_usd, actor, trace_id, "
                f"preempted_on, days_to_resolve) VALUES (current_timestamp(), :thread_id, :offer_id, :spot_id, :advertiser_id, "
                f":market, :seller_id, :outcome, from_json(:avail_ids, 'ARRAY<STRING>'), CAST(:refund AS DOUBLE), "
                f"CAST(:owed AS DOUBLE), CAST(:booked AS DOUBLE), :actor, :trace_id, CAST(:preempted_on AS DATE), "
                f"datediff(current_date(), CAST(:preempted_on AS DATE)))",
                {"thread_id": thread_id, "offer_id": (state.get("offer") or {}).get("offer_id"), "spot_id": s["spot_id"],
                 "advertiser_id": s["advertiser_id"], "market": s.get("market"), "seller_id": s.get("seller_id"),
                 "outcome": outcome, "avail_ids": json.dumps(avail_ids), "refund": refund, "owed": s["unit_rate_usd"],
                 "booked": booked_value, "actor": actor, "trace_id": state.get("rec_trace_id"),
                 "preempted_on": str(s.get("broadcast_date"))})
        except Exception:  # noqa: BLE001 - the Lakebase decision log is the system of record; the UC copy is for analytics
            logger.warning("could not write the decision event for %s to Unity Catalog", s["spot_id"], exc_info=True)

    async def ask_genie(state: MakeGoodState) -> dict:
        g = await genie.ask_genie(_last_human_text(state), state.get("genie_conversation_id"))
        detail = (f"Genie space \"Make-Goods Analytics\" (governed metric views) · {g['status']} · "
                  f"{g['row_count']} rows" + (f" · {g['error']}" if g.get("error") else ""))
        return {"messages": [AIMessage(genie.to_markdown(g))], "genie_conversation_id": g["conversation_id"],
                "genie": {k: g[k] for k in ("space_id", "conversation_id", "message_id", "status", "sql", "row_count")},
                "llm_usage": [], "trail": _step("ask_genie", "Answered with Genie", detail)}

    # ------------------------------------------------------------------ routing
    def after_parse(state: MakeGoodState) -> str:
        return {"make_good": "load_spot", "refine": "search_inventory", "add_rule": "propose_rule"}.get(state["intent"], "ask_genie")

    def after_load(state: MakeGoodState):
        if state.get("outcome") in ("not_found", "not_preempted"):
            return END
        return ["map_audience", "recall_preferences"] if state["spot"].get("makegood_eligible", True) else "refund_path"

    def after_search(state: MakeGoodState) -> str:
        return "score_candidates" if state.get("candidates") else "refund_path"

    def after_score(state: MakeGoodState) -> str:
        return "build_packages" if state.get("ranked") else "refund_path"

    def after_packages(state: MakeGoodState) -> str:
        return "close_gap" if (state.get("gap") or {}).get("can_widen") else "explain_recommendation"

    def after_review(state: MakeGoodState) -> str:
        r = state.get("review")
        if not r:
            return END
        return {"accept": "finalize", "modify": "finalize", "refund": "finalize", "book": "finalize",
                "reject": "search_inventory" if state.get("ranked") and state.get("outcome") != "refund" else "finalize",
                "refine": "parse_request", "add_rule": "parse_request",
                "offer": "draft_offer" if state.get("selection") else "human_review",
                "review_selection": "review_selection"}.get(r.get("action"), END)

    def after_finalize(state: MakeGoodState) -> str:
        return END if state.get("review") else "human_review"

    def after_manager(state: MakeGoodState) -> str:
        return {"approve": "confirm_send", "request_changes": "draft_offer"}.get(
            (state.get("manager_decision") or {}).get("action"), "human_review")

    def after_confirm_send(state: MakeGoodState) -> str:
        return "send_offer" if (state.get("send_decision") or {}).get("action") == "send" else "human_review"

    def after_send(state: MakeGoodState) -> str:
        return "confirm_send" if (state.get("outbound") or {}).get("error") else "await_client"

    def after_await(state: MakeGoodState) -> str:
        return "human_review" if state.get("withdrawn") else "parse_client_reply"

    def after_seller_confirm(state: MakeGoodState) -> str:
        a = (state.get("confirm_decision") or {}).get("action")
        return {"book": "finalize", "refund": "finalize", "counter": "search_inventory"}.get(a, "human_review")

    def after_propose_rule(state: MakeGoodState) -> str:
        if state.get("pending_rule"):
            return "confirm_rule"
        return "human_review" if state.get("ranked") and _live(state, memory) else END

    def after_confirm_rule(state: MakeGoodState) -> str:
        if (state.get("rule_decision") or {}).get("action") == "save":
            return "save_rule"
        return "human_review" if state.get("ranked") and _live(state, memory) else END

    def after_save_rule(state: MakeGoodState) -> str:
        if (state.get("rule_decision") or {}).get("rerun"):
            return "search_inventory"  # the rule is for the client of the open make-good: apply it now
        return "human_review" if state.get("ranked") and _live(state, memory) else END

    g = StateGraph(MakeGoodState)
    for fn in (parse_request, load_spot, map_audience, recall_preferences, search_inventory, score_candidates,
               explain_recommendation, refund_path, human_review, review_selection, finalize, ask_genie,
               *offer_nodes.values(), *rule_nodes.values()):
        g.add_node(fn.__name__, traced_node(fn))
    g.add_edge(START, "parse_request")
    g.add_conditional_edges("parse_request", after_parse, ["load_spot", "search_inventory", "propose_rule", "ask_genie"])
    g.add_conditional_edges("load_spot", after_load, ["map_audience", "recall_preferences", "refund_path", END])
    g.add_edge(["map_audience", "recall_preferences"], "search_inventory")
    g.add_conditional_edges("search_inventory", after_search, ["score_candidates", "refund_path"])
    g.add_conditional_edges("score_candidates", after_score, ["build_packages", "refund_path"])
    g.add_conditional_edges("build_packages", after_packages, ["close_gap", "explain_recommendation"])
    g.add_edge("close_gap", "search_inventory")
    g.add_edge("explain_recommendation", "human_review")
    g.add_edge("refund_path", "human_review")
    g.add_conditional_edges("human_review", after_review, ["finalize", "search_inventory", "parse_request", "draft_offer",
                                                           "review_selection", "human_review", END])
    g.add_edge("review_selection", "human_review")
    g.add_conditional_edges("finalize", after_finalize, ["human_review", END])
    g.add_edge("draft_offer", "manager_review")
    g.add_conditional_edges("manager_review", after_manager, ["confirm_send", "draft_offer", "human_review"])
    g.add_conditional_edges("confirm_send", after_confirm_send, ["send_offer", "human_review"])
    g.add_conditional_edges("send_offer", after_send, ["confirm_send", "await_client"])
    g.add_conditional_edges("await_client", after_await, ["human_review", "parse_client_reply"])
    g.add_edge("parse_client_reply", "seller_confirm")
    g.add_conditional_edges("seller_confirm", after_seller_confirm, ["finalize", "search_inventory", "human_review"])
    g.add_conditional_edges("propose_rule", after_propose_rule, ["confirm_rule", "human_review", END])
    g.add_conditional_edges("confirm_rule", after_confirm_rule, ["save_rule", "human_review", END])
    g.add_conditional_edges("save_rule", after_save_rule, ["search_inventory", "human_review", END])
    g.add_edge("ask_genie", END)
    return g.compile(checkpointer=memory.checkpointer if memory else InMemorySaver())


def _log_review_feedback(trace_id: Optional[str], decision: dict, final_avail_id: Optional[str] = None) -> None:
    """Attach the reviewer's decision to the recommendation's trace (human feedback -> future eval labels)."""
    if not trace_id:
        return
    source = AssessmentSource(source_type=AssessmentSourceType.HUMAN, source_id=decision.get("reviewer") or "ad-ops reviewer")
    try:
        mlflow.log_feedback(trace_id=trace_id, name="reviewer_decision", value=decision.get("action"), source=source,
                            rationale=decision.get("reason"))
        if decision.get("action") == "modify" and final_avail_id:
            mlflow.log_expectation(trace_id=trace_id, name="expected_avail_id", value=final_avail_id, source=source)
    except Exception:
        logger.warning("could not log reviewer feedback to trace %s", trace_id, exc_info=True)
