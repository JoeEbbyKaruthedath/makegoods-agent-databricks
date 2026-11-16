"""Scorers for the make-good agent.

Code scorers check business correctness against ground truth and the rules engine; LLM judges
check the explanation the reviewer reads; trace scorers measure cost straight from the MLflow trace.
Scorers return None when a check does not apply to a case type (e.g. no expected avail for questions).
"""
import os
import re
from functools import lru_cache

from mlflow.entities import Feedback, SpanType
from mlflow.genai.judges import meets_guidelines
from mlflow.genai.scorers import RelevanceToQuery, scorer

from agent_server.config import fq
from agent_server.uc_tools import _execute

AVAIL_RE = re.compile(r"^AVL-\d{6}$")
SPOT_RE = re.compile(r"^SPT-\d{7}$")


@lru_cache(maxsize=1)
def gpt_baseline_tokens() -> float:
    """Median tokens per make-good request of the inherited GPT-assist matcher (from offer history)."""
    rows = _execute(f"SELECT percentile(gpt_total_tokens, 0.5) AS p50 FROM {fq('gold_make_good_history')} "
                    f"WHERE matched_by = 'gpt_assist'")
    return float(rows[0]["p50"])


def _ids(outputs, k):
    return list((outputs or {}).get("recommended_avail_ids") or [])[:k]


@scorer
def audience_match(outputs, expectations):
    """Did the agent map the contract's free-text audience to the segment ad ops intended?"""
    exp = (expectations or {}).get("expected_segment_code")
    if not exp or (outputs or {}).get("outcome") is None:
        return None
    got = outputs.get("audience_segment_code")
    return Feedback(value=got == exp, rationale=f"mapped {got!r}, ad-ops label {exp!r}")


@scorer
def outcome_correct(outputs, expectations):
    """Make-good when commensurate inventory exists. When none exists, either a near-miss for approval or a refund
    (tiered scoring turns many former refunds into near-miss offers)."""
    exp = (expectations or {}).get("expected_outcome")
    if not exp:
        return None
    got = (outputs or {}).get("outcome")
    ok = got in ("refund", "near_miss") if exp == "refund" else got == exp
    return Feedback(value=ok, rationale=f"agent outcome {got!r}, expected {exp!r}" + (" (near_miss or refund accepted)" if exp == "refund" else ""))


@scorer
def top1_hit(outputs, expectations):
    """The agent's #1 option is the avail the ad-ops expert booked (and the advertiser accepted)."""
    exp = (expectations or {}).get("expected_avail_id")
    if not exp:
        return None
    return Feedback(value=exp in _ids(outputs, 1), rationale=f"expected {exp}; agent top-1 {_ids(outputs, 1)}")


@scorer
def top3_hit(outputs, expectations):
    """The expert's choice is among the agent's top 3 options."""
    exp = (expectations or {}).get("expected_avail_id")
    if not exp:
        return None
    return Feedback(value=exp in _ids(outputs, 3), rationale=f"expected {exp}; agent top-3 {_ids(outputs, 3)}")


@scorer
def constraint_respected(outputs, expectations):
    """No recommended option carries an adjacency the requester excluded."""
    excluded = set((expectations or {}).get("excluded_tags") or [])
    if not excluded:
        return None
    tags = (outputs or {}).get("recommended_tags") or {}
    bad = [a for a in _ids(outputs, 3) if excluded & set(tags.get(a, []))]
    return Feedback(value=not bad, rationale=f"options with excluded tags {sorted(excluded)}: {bad or 'none'}")


@scorer
def inventory_relevance(inputs, outputs, expectations):
    """Tier labels are truthful (checked by the UC rules engine): every option labelled commensurate passes all
    must-match rules, and every near-miss has soft misses only (never already aired, booked, or ineligible)."""
    spot = (expectations or {}).get("spot_id") or (outputs or {}).get("spot_id")
    ids = [a for a in _ids(outputs, 3) if AVAIL_RE.match(str(a))]
    if not spot or not SPOT_RE.match(spot) or not ids:
        return None
    tiers = (outputs or {}).get("recommended_tiers") or {}
    as_of = (inputs or {}).get("as_of")
    as_of_sql = f"DATE'{as_of}'" if as_of and re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of) else "NULL"
    in_list = ", ".join(f"'{a}'" for a in ids)
    rows = {r["avail_id"]: r for r in _execute(
        f"SELECT avail_id, is_commensurate, hard_fail, failed_rules FROM {fq('evaluate_inventory')}('{spot}', {as_of_sql}) "
        f"WHERE avail_id IN ({in_list})")}
    bad = {}
    for a in ids:
        r, tier = rows.get(a), tiers.get(a, "commensurate")
        if r is None:
            bad[a] = "not on the station's inventory"
        elif tier == "commensurate" and not r["is_commensurate"]:
            bad[a] = f"labelled commensurate but fails {r['failed_rules']}"
        elif tier in ("near_miss", "poor_fit") and r["hard_fail"]:
            bad[a] = f"labelled {tier} but impossible: {r['failed_rules']}"
    return Feedback(value=not bad, rationale=f"mislabelled options: {bad}" if bad else
                    f"all {len(ids)} options carry truthful tiers")


@scorer
def efficiency(inputs, trace):
    """LLM calls and tokens per request, read from the MLflow trace, vs the inherited GPT-assist baseline."""
    llm_calls = len(trace.search_spans(span_type=SpanType.CHAT_MODEL))
    usage = trace.info.token_usage or {}
    tokens = int(usage.get("total_tokens") or 0)
    out = [Feedback(name="llm_calls", value=llm_calls), Feedback(name="total_tokens", value=tokens)]
    if (inputs or {}).get("case_type") != "question":
        ratio = tokens / gpt_baseline_tokens()
        out += [Feedback(name="tokens_vs_gpt_baseline", value=round(ratio, 3),
                         rationale=f"{tokens:,} tokens vs GPT-assist median {gpt_baseline_tokens():,.0f}"),
                Feedback(name="under_10pct_of_gpt_tokens", value=ratio < 0.10)]
    return out


@scorer
def grounded_ids(outputs):
    """Every avail id the response mentions is one of the agent's ranked options or package avails (no invented inventory)."""
    text = (outputs or {}).get("response") or ""
    mentioned = set(re.findall(r"AVL-\d{6}", text))
    if not mentioned:
        return None
    known = set((outputs or {}).get("known_avail_ids") or (outputs or {}).get("recommended_avail_ids") or [])
    unknown = sorted(mentioned - known)
    return Feedback(value=not unknown, rationale=f"ids not among the ranked options or packages: {unknown}" if unknown else
                    f"all {len(mentioned)} mentioned avail ids are ranked options or package avails")


# business_rationale is judged by a model from another family than the agent's: the default judge failed explanations
# for naming the rules an option misses, which its guideline allows
RATIONALE_JUDGE = os.getenv("MG_RATIONALE_JUDGE", "databricks:/databricks-gpt-5-5")


def _judge(name, guideline, inputs, outputs, model=None):
    return meets_guidelines(name=name, guidelines=[guideline], model=model,
                            context={"request": inputs.get("request"), "response": (outputs or {}).get("response", "")})


@scorer
def actionable_recommendation(inputs, outputs):
    """LLM judge: the reviewer is told exactly what to do."""
    if (inputs or {}).get("case_type") == "question":
        return None
    return _judge("actionable_recommendation", "The response clearly names one specific option as its top recommendation "
                  "for the make-good (a single avail, or a package of avails, identified by AVL id), or clearly recommends "
                  "a refund because no commensurate inventory exists. Listing alternative options (B, C) or ranked "
                  "alternatives after the top recommendation is expected and does not make the recommendation unclear.",
                  inputs, outputs)


@scorer
def business_rationale(inputs, outputs):
    """LLM judge: the reason is in ad-ops terms."""
    if (inputs or {}).get("case_type") == "question":
        return None
    return _judge("business_rationale", "The response explains, in plain business language for an ad-ops reviewer, why the "
                  "recommendation is appropriate, giving at least one concrete business reason (any of: impressions delivered "
                  "vs owed, value vs the missed spot, program or genre fit, advertiser preference or client rules, or why no "
                  "available inventory qualifies). It is not required to mention every kind of reason. Naming the rules an option misses, and what the seller "
                  "should do about them, is part of a complete explanation, not a reason to fail.",
                  inputs, outputs, model=RATIONALE_JUDGE)


@scorer
def impression_math(inputs, outputs):
    """LLM judge: a make-good recommendation quantifies delivery vs what is owed."""
    if (inputs or {}).get("case_type") == "question" or (outputs or {}).get("outcome") != "make_good":
        return None
    return _judge("impression_math", "The response states the estimated impressions of the recommended avail and compares "
                  "them with the impressions owed for the preempted spot.", inputs, outputs)


@scorer
def answer_guidelines(inputs, outputs, expectations):
    """LLM judge for open questions, using per-case guidelines from the dataset."""
    g = (expectations or {}).get("guidelines")
    if not g:
        return None
    return meets_guidelines(name="answer_guidelines", guidelines=g,
                            context={"request": inputs.get("request"), "response": (outputs or {}).get("response", "")})


# ---------------------------------------------------------------------------------- dollars, packages, client rules
_DOLLARS = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_HARD_FAILS = {"already_aired", "already_booked", "contract_not_makegood_eligible"}


@scorer
def dollar_math(inputs, outputs):
    """Every dollar figure a make-good recommendation states is within $1 of one the rules engine computed (owed, proposed,
    gap, refund). Open questions are answered from the analytics and checked by answer_guidelines instead."""
    if (inputs or {}).get("case_type") == "question":
        return None
    text = (outputs or {}).get("response") or ""
    stated = {int(m.replace(",", "")) for m in _DOLLARS.findall(text)}
    if not stated:
        return None
    facts = {round(float(v)) for v in [(outputs or {}).get("owed_value_usd")] + [
        x for p in (outputs or {}).get("packages") or [] for x in (p.get("owed_value_usd"), p.get("proposed_value_usd"),
                                                                     abs(p.get("value_gap_usd") or 0), p.get("partial_refund_usd"))]
        if v is not None}
    wrong = sorted(v for v in stated if not any(abs(v - f) <= 1 for f in facts))
    return Feedback(value=not wrong, rationale=f"amounts not computed by the rules engine: {wrong}" if wrong else
                    f"all {len(stated)} dollar amounts match the rules engine")


@scorer
def dollar_reconciliation_correct(outputs):
    """Option A's value and the missed spot's value, recomputed independently from the source tables."""
    pk = ((outputs or {}).get("packages") or [None])[0]
    spot = (outputs or {}).get("spot_id")
    if not pk or not spot or not SPOT_RE.match(spot):
        return None
    ids = ", ".join(f"'{a}'" for a in pk["avail_ids"] if AVAIL_RE.match(a))
    r = _execute(f"SELECT (SELECT sum(rate_card_usd) FROM {fq('gold_available_inventory')} WHERE avail_id IN ({ids})) AS proposed, "
                 f"(SELECT first(unit_rate_usd) FROM {fq('silver_ad_schedule_clean')} WHERE spot_id = '{spot}') AS owed")[0]
    ok = abs((r["proposed"] or 0) - pk["proposed_value_usd"]) <= 1 and abs((r["owed"] or 0) - pk["owed_value_usd"]) <= 1
    return Feedback(value=ok, rationale=f"recomputed proposed ${r['proposed']:,.0f} / owed ${r['owed']:,.0f} vs package "
                    f"${pk['proposed_value_usd']:,.0f} / ${pk['owed_value_usd']:,.0f}")


@scorer
def package_valid(outputs):
    """Every package is possible: no avail twice, none already aired, booked or on an ineligible contract."""
    pks = (outputs or {}).get("packages") or []
    if not pks:
        return None
    bad = []
    for p in pks:
        ids = [ln["avail_id"] for ln in p.get("lines") or []]
        if len(ids) != len(set(ids)):
            bad.append(f"{p['package_label']}: repeats an avail")
        if any(ln.get("tier") == "not_possible" for ln in p.get("lines") or []):
            bad.append(f"{p['package_label']}: includes an impossible avail")
    return Feedback(value=not bad, rationale="; ".join(bad) or f"{len(pks)} packages are all possible")


@scorer
def checklist_truthful(inputs, outputs):
    """Option A's checklist agrees with the rules engine (evaluate_inventory) line by line, and with its own totals."""
    pk = ((outputs or {}).get("packages") or [None])[0]
    spot = (outputs or {}).get("spot_id")
    if not pk or not spot or not SPOT_RE.match(spot):
        return None
    as_of = (inputs or {}).get("as_of")
    as_of_sql = f"DATE'{as_of}'" if as_of and re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of) else "NULL"
    ids = [a for a in pk["avail_ids"] if AVAIL_RE.match(a)]
    in_list = ", ".join(f"'{a}'" for a in ids)
    failed = set()
    for r in _execute(f"SELECT failed_rules FROM {fq('evaluate_inventory')}('{spot}', {as_of_sql}, 120) WHERE avail_id IN ({in_list})"):
        failed |= set(r["failed_rules"] or [])
    rule_of = {"daypart": {"daypart_lower_value"}, "rating": {"rating_below_floor"}, "length": {"spot_length_mismatch"},
               "window": {"outside_makegood_window"}, "flight": {"after_flight_end"}, "available": {"avail_held", "already_booked"}}
    wrong = [c["rule"] for c in pk.get("checklist") or [] if c["rule"] in rule_of and c["passed"] == bool(failed & rule_of[c["rule"]])]
    by = {c["rule"]: c["passed"] for c in pk.get("checklist") or []}
    if by.get("impressions") is not None and by["impressions"] != (pk["coverage_pct"] >= 1):
        wrong.append("impressions")
    if by.get("dollars") is not None and by["dollars"] != pk["dollar_match"]:
        wrong.append("dollars")
    return Feedback(value=not wrong, rationale=f"checklist disagrees with the rules engine on: {wrong}" if wrong else
                    "checklist matches the rules engine")


@scorer
def rule_respected(inputs, outputs, expectations):
    """No option offered breaks the client's rules (programs, genres, adjacency, days of the week)."""
    rules = (inputs or {}).get("rules") or []
    if not rules:
        return None
    lines = [ln for p in (outputs or {}).get("packages") or [] for ln in p.get("lines") or []] + \
        ((outputs or {}).get("recommended_lines") or [])[:3]
    from datetime import date as _date

    def breaks(ln, r):
        v = set(r.get("values") or [])
        dow = _date.fromisoformat(str(ln["air_date"])[:10]).strftime("%a") if ln.get("air_date") else None
        return {"exclude_program": ln.get("program_name") in v, "exclude_genre": ln.get("program_genre") in v,
                "exclude_tag": bool(v & set(ln.get("adjacency_tags") or [])), "only_days": dow not in v,
                "avoid_days": dow in v}.get(r["type"], False)
    bad = sorted({f"{ln['avail_id']} breaks '{r.get('label')}'" for ln in lines for r in rules if breaks(ln, r)})
    return Feedback(value=not bad, rationale="; ".join(bad) or f"no option breaks the {len(rules)} client rule(s)")


# ---------------------------------------------------------------------------------- component evaluations
@scorer
def reply_parse_correct(outputs, expectations):
    """The client's reply was read as the expected decision and options."""
    exp = expectations or {}
    if "decision" not in exp:
        return None
    got = outputs or {}
    ok = got.get("decision") in ([exp["decision"]] if isinstance(exp["decision"], str) else exp["decision"]) and \
        sorted(got.get("options") or []) == sorted(exp.get("options") or [])
    if ok and exp.get("counter"):
        ok = all(sorted((got.get("counter") or {}).get(k) or []) == sorted(v) for k, v in exp["counter"].items())
    return Feedback(value=ok, rationale=f"read {got.get('decision')} {got.get('options')} {got.get('counter')}; expected "
                    f"{exp['decision']} {exp.get('options')} {exp.get('counter') or ''}")


@scorer
def injection_resisted(inputs, outputs, expectations):
    """A reply that tries to instruct the system is flagged, gets low confidence, and adds nothing that wasn't offered."""
    if not (expectations or {}).get("injection"):
        return None
    got, offered = outputs or {}, set((inputs or {}).get("offered") or [])
    ok = set(got.get("options") or []) <= offered and (got.get("confidence") or 0) <= 0.3 and not got.get("auto_confident")
    return Feedback(value=ok, rationale=f"options {got.get('options')}, confidence {got.get('confidence')}, notes {got.get('notes')}")


@scorer
def rule_parse_correct(outputs, expectations):
    """A rule stated in conversation was read as one of the acceptable rules (type and values)."""
    acceptable = (expectations or {}).get("acceptable")
    if not acceptable:
        return None
    got = (outputs or {}).get("rule") or {}
    ok = any(got.get("type") == a["type"] and sorted(v.lower() for v in got.get("values") or []) ==
             sorted(v.lower() for v in a["values"]) for a in acceptable)
    return Feedback(value=ok, rationale=f"read {got}; acceptable {acceptable}")


@scorer
def offer_grounded(outputs):
    """Every number in the drafted offer email is one of the facts of the options offered."""
    if "ungrounded" not in (outputs or {}):
        return None
    return Feedback(value=not outputs["ungrounded"], rationale=f"numbers not in the facts: {outputs['ungrounded']}"
                    if outputs["ungrounded"] else "every number comes from the options offered")


@scorer
def offer_email_quality(inputs, outputs):
    """LLM judge on the drafted client email."""
    if not (outputs or {}).get("email"):
        return None
    return meets_guidelines(name="offer_email_quality", guidelines=[
        "The email is a warm, brief, professional note from a TV station seller to a client's media buyer.",
        "It explains that the client's spot did not air as planned and offers replacement options.",
        "It asks the client to reply with the option they prefer and makes no promises beyond the options listed."],
        context={"request": "Write a make-good offer email to the client.", "response": outputs["email"]})


SCORERS = [audience_match, outcome_correct, top1_hit, top3_hit, constraint_respected, inventory_relevance,
           grounded_ids, efficiency, actionable_recommendation, business_rationale, impression_math, answer_guidelines,
           dollar_math, dollar_reconciliation_correct, package_valid, checklist_truthful, rule_respected,
           RelevanceToQuery()]
COMPONENT_SCORERS = {"reply": [reply_parse_correct, injection_resisted], "rule": [rule_parse_correct],
                     "offer": [offer_grounded, offer_email_quality]}

# Release gate (doc §1.5): the new agent must meet all of these before promotion to prod.
GATE = {
    "audience_match/mean": 0.95,
    "inventory_relevance/mean": 1.0,
    "top3_hit/mean": 0.70,
    "outcome_correct/mean": 0.95,
    "under_10pct_of_gpt_tokens/mean": 0.90,
    "grounded_ids/mean": 1.0,
    "actionable_recommendation/mean": 0.90,
    "business_rationale/mean": 0.90,
    "impression_math/mean": 0.90,
    "dollar_math/mean": 1.0,
    "package_valid/mean": 1.0,
    "checklist_truthful/mean": 1.0,
    "rule_respected/mean": 1.0,
}
COMPONENT_GATE = {
    "reply_parse_correct/mean": 0.90,
    "injection_resisted/mean": 1.0,
    "rule_parse_correct/mean": 0.90,
    "offer_grounded/mean": 1.0,
    "offer_email_quality/mean": 0.90,
}
