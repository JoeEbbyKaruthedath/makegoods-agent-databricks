"""Build the make-good evaluation dataset and register it in Unity Catalog (MLflow-managed dataset).

Case types (inputs -> expectations):
  history     expert (manual) make-goods accepted by the advertiser on/after the preference cutoff,
              replayed as of the decision date -> expected_avail_id, expected_segment_code, outcome make_good
  refund      queue spots with no commensurate inventory left -> outcome refund
  constraint  "avoid news adjacency" requests -> excluded_tags respected (+ expected outcome)
  question    open questions about the queue -> per-row guidelines
  rule        a client rule excludes the program of the spot's best avail -> no option breaks the rule
Preferences used by the agent are learned only from decisions BEFORE the cutoff, so history cases
are a fair out-of-sample test.

  python eval/build_eval_dataset.py [--history 120]
"""
import argparse
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

import mlflow  # noqa: E402

from agent_server.config import CATALOG, SCHEMA, fq  # noqa: E402
from agent_server.uc_tools import _execute  # noqa: E402

DATASET = f"{CATALOG}.{SCHEMA}.mg_eval_set"
LABELS = f"/Volumes/{CATALOG}/{SCHEMA}/raw_landing/eval_labels/contract_audience_labels.csv"
TEMPLATES = [
    "Spot {spot_id} on {call_letters} was preempted ({reason}) on {date}. Find a commensurate make-good.",
    "{advertiser}'s {daypart} spot {spot_id} got bumped by {reason_lc}. What's the best replacement inventory?",
    "Need a make-good for {spot_id} — it didn't air as scheduled on {date} ({reason_lc}).",
    "Can you find replacement inventory for {spot_id} ({advertiser}, {call_letters} {daypart})?",
]


def h(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16)


def request_text(r: dict, salt: str = "") -> str:
    t = TEMPLATES[h(r["spot_id"] + salt) % len(TEMPLATES)]
    return t.format(spot_id=r["spot_id"], call_letters=r["call_letters"], reason=r["preemption_reason"],
                    reason_lc=r["preemption_reason"].lower(), date=r["spot_date"], advertiser=r["advertiser_name"],
                    daypart=r["daypart"])


def history_cases(n: int) -> list[dict]:
    rows = _execute(f"""
        WITH lab AS (SELECT order_id, labeled_segment_code
                     FROM read_files('{LABELS}', format => 'csv', header => true)),
        h AS (
          SELECT h.offer_id, h.spot_id, h.order_id, h.advertiser_name, h.call_letters, h.original_daypart AS daypart,
                 h.preemption_reason, CAST(h.spot_date AS STRING) AS spot_date, h.offered_avail_id,
                 to_date(h.offered_ts) AS decision_date, c.audience_match_type, lab.labeled_segment_code
          FROM {fq('gold_make_good_history')} h
          JOIN {fq('silver_contract_obligations')} c ON c.order_id = h.order_id
          JOIN lab ON lab.order_id = h.order_id
          WHERE h.matched_by = 'manual' AND h.offer_status = 'Accepted' AND NOT h.is_downgrade
            AND to_date(h.offered_ts) >= (SELECT CAST(str_value AS DATE) FROM {fq('mg_rules_config')} WHERE rule_key = 'pref_cutoff_date')
            AND c.makegood_eligible
        )
        SELECT h.*, e.is_commensurate AS offered_is_commensurate
        FROM h, LATERAL {fq('evaluate_inventory')}(h.spot_id, h.decision_date) e
        WHERE e.avail_id = h.offered_avail_id""")
    rows = [r for r in rows if r["offered_is_commensurate"]]
    rows.sort(key=lambda r: h(r["offer_id"]))
    # stratify: round-robin across dayparts so Prime / Late Fringe are well represented
    by_dp: dict[str, list] = {}
    for r in rows:
        by_dp.setdefault(r["daypart"], []).append(r)
    picked = []
    while len(picked) < n and any(by_dp.values()):
        for dp in sorted(by_dp):
            if by_dp[dp] and len(picked) < n:
                picked.append(by_dp[dp].pop())
    return [{
        "inputs": {"request": request_text(r), "as_of": str(r["decision_date"]), "case_type": "history"},
        "expectations": {"expected_avail_id": r["offered_avail_id"], "expected_segment_code": r["labeled_segment_code"],
                         "expected_outcome": "make_good", "spot_id": r["spot_id"],
                         "audience_match_type": r["audience_match_type"]},
    } for r in picked]


def queue_rows() -> list[dict]:
    return _execute(f"""
        WITH lab AS (SELECT order_id, labeled_segment_code FROM read_files('{LABELS}', format => 'csv', header => true))
        SELECT q.spot_id, q.advertiser_name, q.call_letters, q.daypart, q.preemption_reason,
               CAST(q.broadcast_date AS STRING) AS spot_date, q.days_to_deadline, lab.labeled_segment_code,
               (SELECT count(*) FROM {fq('inventory_search')}(q.spot_id)) AS n_commensurate,
               (SELECT count(*) FROM {fq('inventory_search')}(q.spot_id, 25, NULL, array('news'))) AS n_without_news,
               (SELECT count(*) FROM {fq('inventory_search')}(q.spot_id) s WHERE array_contains(s.adjacency_tags, 'news')) AS n_news,
               (SELECT first(program_name) FROM {fq('inventory_search')}(q.spot_id)) AS top_program
        FROM {fq('gold_make_good_candidates')} q
        JOIN {fq('silver_contract_obligations')} c ON c.order_id = q.order_id
        JOIN lab ON lab.order_id = q.order_id
        WHERE q.advertiser_id <> 'ADV-0151'""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", type=int, default=120)
    args = ap.parse_args()

    records = history_cases(args.history)
    q = sorted(queue_rows(), key=lambda r: h(r["spot_id"]))
    refunds = [r for r in q if r["n_commensurate"] == 0][:15]
    records += [{"inputs": {"request": request_text(r, "refund"), "case_type": "refund"},
                 "expectations": {"expected_outcome": "refund", "expected_segment_code": r["labeled_segment_code"],
                                  "spot_id": r["spot_id"]}} for r in refunds]
    constrained = [r for r in q if r["n_news"] > 0][:10]
    records += [{"inputs": {"request": f"Find a make-good for {r['spot_id']} but avoid news adjacency — the advertiser doesn't want news.",
                            "case_type": "constraint"},
                 "expectations": {"excluded_tags": ["news"], "expected_segment_code": r["labeled_segment_code"],
                                  "expected_outcome": "make_good" if r["n_without_news"] > 0 else "refund",
                                  "spot_id": r["spot_id"]}} for r in constrained]
    ruled = [r for r in q if r["n_commensurate"] > 1 and r["top_program"]][:15]
    records += [{"inputs": {"request": f"Find a make-good for {r['spot_id']}.", "case_type": "rule",
                            "rules": [{"type": "exclude_program", "values": [r["top_program"]], "strength": "hard",
                                       "label": f"never {r['top_program']}", "active": True}]},
                 "expectations": {"spot_id": r["spot_id"], "expected_segment_code": r["labeled_segment_code"]}} for r in ruled]
    questions = [
        ("Which markets have the most make-good revenue at risk right now?",
         ["The response must name specific markets with dollar amounts of revenue at risk.",
          "The response must not recommend a specific make-good avail."]),
        ("What are the most urgent make-goods in Nashville?",
         ["The response must list specific spot ids in Nashville with their deadlines."]),
        ("How many open make-goods are due within 3 days, and in which markets?",
         ["The response must give counts of spots due within 3 days and name markets."]),
        ("Which advertisers in Milwaukee are waiting on make-goods?",
         ["The response must name advertisers in Milwaukee from the work queue."]),
        ("What does 'commensurate' mean for a make-good here?",
         ["The response must describe matching on station, daypart, audience rating, spot length and timing.",
          "The response must not invent spot ids or dollar amounts."]),
    ]
    records += [{"inputs": {"request": text, "case_type": "question"}, "expectations": {"guidelines": g}}
                for text, g in questions]

    counts = {}
    for r in records:
        counts[r["inputs"]["case_type"]] = counts.get(r["inputs"]["case_type"], 0) + 1
    print("cases:", counts)

    exp_id = mlflow.set_experiment(experiment_id=os.environ["MLFLOW_EXPERIMENT_ID"]).experiment_id
    try:
        ds = mlflow.genai.datasets.get_dataset(name=DATASET)
        print("updating existing dataset", DATASET)
    except Exception:
        ds = mlflow.genai.datasets.create_dataset(name=DATASET, experiment_id=exp_id)
        print("created dataset", DATASET)
    ds.merge_records(records)
    print("records in dataset:", len(ds.to_df()))


if __name__ == "__main__":
    main()
