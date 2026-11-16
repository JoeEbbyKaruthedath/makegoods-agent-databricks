"""Build the "Make-Goods Analytics" Genie Space definition (serialized_space v2).

Sources are the 4 governed metric views + the rules config table. Writes genie/space.json;
create or update the space with the CLI commands printed at the end.

  python genie/build_space.py [--catalog <catalog>] [--schema <schema>]
"""
import argparse
import hashlib
import json
from pathlib import Path

_ap = argparse.ArgumentParser()
_ap.add_argument("--catalog", default="YOUR_CATALOG")
_ap.add_argument("--schema", default="YOUR_SCHEMA")
_args = _ap.parse_args()
CAT, SCH = _args.catalog, _args.schema
fq = lambda n: f"{CAT}.{SCH}.{n}"  # noqa: E731


def gid(key: str) -> str:
    """Stable 32-hex id so re-running produces the same ids (clean diffs on update)."""
    return hashlib.md5(key.encode()).hexdigest()


def match(*cols: str) -> list[dict]:
    return [{"column_name": c, "enable_format_assistance": True, "enable_entity_matching": True} for c in cols]


metric_views = [
    {"identifier": fq("makegoods_contract_delivery_metrics"),
     "column_configs": match("Advertiser", "Audience Segment", "Daypart", "Market", "Pacing Status")},
    {"identifier": fq("makegoods_offer_outcome_metrics"),
     "column_configs": match("Advertiser", "Advertiser Category", "Market", "Matching Method", "Offer Status",
                             "Offered Daypart", "Preempted Daypart", "Preemption Reason", "Station")},
    {"identifier": fq("makegoods_open_queue_metrics"),
     "column_configs": match("Advertiser", "Advertiser Category", "Audience Segment", "Daypart", "Deadline Bucket",
                             "Market", "Network", "Preemption Reason", "Queue Status", "Station")},
    {"identifier": fq("makegoods_preemption_metrics"),
     "column_configs": match("Daypart", "Market", "Network", "Preemption Reason", "Program Genre", "Spot Status", "Station")},
]
for mv in metric_views:
    mv["column_configs"].sort(key=lambda c: c["column_name"])

tables = [{
    "identifier": fq("mg_rules_config"),
    "column_configs": sorted([
        {"column_name": "description", "description": ["What the rule, threshold or weight means."]},
        {"column_name": "num_value", "description": ["Current numeric value of the threshold, weight or penalty."]},
        {"column_name": "question_for_ad_ops", "description": ["Open policy question for ad ops about this rule."]},
        {"column_name": "rule_key", "description": ["Rule or parameter name, e.g. rating_floor_pct, w_timeliness, pen_daypart_step."],
         "synonyms": ["rule", "parameter", "threshold"]},
        {"column_name": "rule_type", "description": ["must_match (hard eligibility), soft_match (ranking weight), near_miss (tiered-score penalty), system."],
         "enable_format_assistance": True, "enable_entity_matching": True},
        {"column_name": "str_value", "description": ["Current text value (e.g. the as-of date)."]},
    ], key=lambda c: c["column_name"]),
}]

examples = [
    ("Which markets have the most make-good revenue at risk right now?",
     f"SELECT `Market`, MEASURE(`Open Spots`) AS open_spots, MEASURE(`Revenue at Risk`) AS revenue_at_risk, "
     f"MEASURE(`Spots Due Within 3 Days`) AS due_within_3_days FROM {fq('makegoods_open_queue_metrics')} "
     f"GROUP BY ALL ORDER BY revenue_at_risk DESC LIMIT 10",
     "Ranking markets by a governed measure from the open work queue."),
    ("What are the most urgent make-goods in Nashville?",
     f"SELECT `Spot`, `Advertiser`, `Station`, `Daypart`, `Preemption Reason`, `Make-good Deadline`, `Days to Deadline`, "
     f"MEASURE(`Revenue at Risk`) AS revenue_at_risk FROM {fq('makegoods_open_queue_metrics')} WHERE `Market` = 'Nashville' "
     f"GROUP BY ALL ORDER BY `Days to Deadline`, revenue_at_risk DESC LIMIT 10",
     "Listing individual open spots for a market, most urgent first."),
    ("How do refunds and acceptance compare across matching methods?",
     f"SELECT `Matching Method`, MEASURE(`Offers`) AS offers, MEASURE(`Acceptance Rate`) AS acceptance_rate, "
     f"MEASURE(`Refunded Amount`) AS refunded, MEASURE(`Downgrade Rate`) AS downgrade_rate, "
     f"MEASURE(`Avg GPT Tokens per Offer`) AS avg_gpt_tokens FROM {fq('makegoods_offer_outcome_metrics')} "
     f"GROUP BY ALL ORDER BY acceptance_rate DESC",
     "Comparing offer outcomes; rates come from composed measures, never recomputed."),
    ("How much have we refunded by daypart?",
     f"SELECT `Preempted Daypart`, MEASURE(`Refunded Amount`) AS refunded, MEASURE(`Refunds Issued`) AS refunds_issued, "
     f"MEASURE(`Downgrade Rate`) AS downgrade_rate FROM {fq('makegoods_offer_outcome_metrics')} "
     f"GROUP BY ALL ORDER BY refunded DESC",
     "'Daypart' for refunds and offers means the preempted daypart; use Offered Daypart only when asked about the replacement."),
    ("Which markets have the highest preemption rate?",
     f"SELECT `Market`, MEASURE(`Scheduled Spots`) AS scheduled_spots, MEASURE(`Preempted or Missed Spots`) AS preempted_or_missed, "
     f"MEASURE(`Preemption Rate`) AS preemption_rate FROM {fq('makegoods_preemption_metrics')} "
     f"GROUP BY ALL ORDER BY preemption_rate DESC LIMIT 10",
     "A rate needs all scheduled spots in the denominator: do not filter Spot Status or Preemption Reason when computing Preemption Rate."),
    ("How many spots were preempted each week, by reason?",
     f"SELECT `Broadcast Week`, `Preemption Reason`, MEASURE(`Preempted or Missed Spots`) AS preempted_or_missed "
     f"FROM {fq('makegoods_preemption_metrics')} WHERE `Spot Status` <> 'Aired' GROUP BY ALL "
     f"ORDER BY `Broadcast Week`, preempted_or_missed DESC",
     "Weekly trend of preemption counts by reason."),
    ("Which contracts are under-delivering the most?",
     f"SELECT `Order`, `Advertiser`, `Market`, `Flight End`, MEASURE(`Pacing Index`) AS pacing_index, "
     f"MEASURE(`Impression Shortfall to Date`) AS shortfall FROM {fq('makegoods_contract_delivery_metrics')} "
     f"WHERE `Pacing Status` = 'Under-delivering' GROUP BY ALL ORDER BY shortfall DESC LIMIT 10",
     "Contract-level delivery against the pro-rated guarantee."),
    ("What are the make-good rules and their current thresholds?",
     f"SELECT rule_key, rule_type, num_value, str_value, description FROM {fq('mg_rules_config')} "
     f"WHERE rule_type IN ('must_match', 'soft_match', 'near_miss') ORDER BY rule_type, rule_key",
     "Explaining the commensurate rules from the governed config table."),
]

instructions = """## PURPOSE
- Analytics for ad ops on make-goods: the open work queue, refunds and offer outcomes, preemptions, and contract delivery.
- The data is a simulation as of 2026-10-30.

## DISAMBIGUATION
- "Today", "now", "this week" and "right now" mean the as-of date 2026-10-30; never use current_date().
- Revenue at risk and open make-goods come from makegoods_open_queue_metrics; refunds and acceptance from makegoods_offer_outcome_metrics; preemptions from makegoods_preemption_metrics; pacing and under-delivery from makegoods_contract_delivery_metrics; rules and thresholds from mg_rules_config.
- "GPT" or "the old system" means Matching Method 'GPT assist' or 'Rules engine'.

## GLOSSARY
- Commensurate make-good: a replacement spot that matches the missed one on station, daypart (same or better), audience rating at or above the floor, spot length, and timing (inside the make-good window and before the flight ends). The thresholds are the must_match rules in mg_rules_config.
- Near-miss: an avail that misses only soft rules; it can be offered as an exception with approval.
- Refund: the client is credited the missed spot's value when no commensurate make-good exists.
- When asked what a term means, start the answer with its glossary definition in full (for commensurate: station, daypart, rating floor, spot length, timing), then show the current thresholds from mg_rules_config.

## CONSTRAINTS
- Do not recommend or book a specific replacement avail; tell the user to click Find make-good on the spot in the app.

## Instructions you must follow when providing summaries
- Show dollars as whole numbers and rates as percentages.
- Say that figures are as of 2026-10-30."""

sample_questions = [
    "Which markets have the most make-good revenue at risk right now?",
    "What are the most urgent make-goods in Nashville?",
    "How much have we refunded by daypart?",
    "How did GPT assist compare with ad-ops experts on acceptance and refunds?",
    "Which contracts are under-delivering the most?",
]

space = {
    "version": 2,
    "config": {"sample_questions": sorted([{"id": gid("sq:" + q), "question": [q]} for q in sample_questions], key=lambda x: x["id"])},
    "data_sources": {
        "metric_views": sorted(metric_views, key=lambda m: m["identifier"]),
        "tables": tables,
    },
    "instructions": {
        "text_instructions": [{"id": gid("ti:main"), "content": [instructions]}],
        "example_question_sqls": sorted([{"id": gid("ex:" + q), "question": [q], "sql": [sql], "usage_guidance": [g]}
                                         for q, sql, g in examples], key=lambda x: x["id"]),
    },
}

out = Path(__file__).resolve().parent / "space.json"
out.write_text(json.dumps(space, indent=2))
print(f"wrote {out} ({len(json.dumps(space))} bytes); instructions {len(instructions)} chars")
