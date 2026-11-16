"""Calibrate the soft-match scoring weights from expert decisions (doc §3.2 feedback loop), no code change.

Train split: expert (manual) make-goods accepted by the advertiser BEFORE pref_cutoff_date. The eval
dataset only uses decisions on/after the cutoff, so the re-run evaluation is a held-out test.

For every training decision, the UC scorer returns each commensurate candidate's per-dimension
contribution; dividing by the current weight recovers the raw feature. A random search then picks the
weights (and impression cap) that put the expert's choice highest, and writes them to mg_rules_config.
The advertiser-preference weight is held fixed (preferences are learned from the same history).

  python eval/calibrate_weights.py            # report only
  python eval/calibrate_weights.py --apply    # also update mg_rules_config
"""
import argparse
import concurrent.futures as cf
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from agent_server.config import fq  # noqa: E402
from agent_server.uc_tools import _execute  # noqa: E402

TUNED = ["w_impressions", "w_genre", "w_yield", "w_timeliness", "w_competitive"]


def training_cases() -> list[dict]:
    return _execute(f"""
        WITH cfg AS (SELECT CAST(str_value AS DATE) AS cutoff FROM {fq('mg_rules_config')} WHERE rule_key = 'pref_cutoff_date'),
        h AS (
          SELECT h.spot_id, h.offered_avail_id, to_date(h.offered_ts) AS decision_date, c.advertiser_id
          FROM {fq('gold_make_good_history')} h
          JOIN {fq('silver_contract_obligations')} c ON c.order_id = h.order_id CROSS JOIN cfg
          WHERE h.matched_by = 'manual' AND h.offer_status = 'Accepted' AND NOT h.is_downgrade
            AND c.makegood_eligible AND to_date(h.offered_ts) < cfg.cutoff
        )
        SELECT h.*, e.is_commensurate AS ok FROM h, LATERAL {fq('evaluate_inventory')}(h.spot_id, h.decision_date) e
        WHERE e.avail_id = h.offered_avail_id AND e.is_commensurate""")


def candidate_features(case: dict, weights: dict) -> tuple[np.ndarray, np.ndarray, int] | None:
    rows = _execute(f"""
        WITH c AS (SELECT collect_list(avail_id) ids FROM {fq('evaluate_inventory')}('{case['spot_id']}', DATE'{case['decision_date']}')
                   WHERE is_commensurate),
        p AS (SELECT map_from_entries(collect_list(struct(adjacency_tag, CAST(weight AS DOUBLE)))) m
              FROM {fq('gold_advertiser_preferences')} WHERE advertiser_id = '{case['advertiser_id']}' AND abs(weight) >= 0.05)
        SELECT avail_id, air_date, impression_equivalence, score_breakdown
        FROM {fq('commensurate_scorer')}('{case['spot_id']}', (SELECT ids FROM c), (SELECT m FROM p), DATE'{case['decision_date']}')""")
    if len(rows) < 2:
        return None
    rows.sort(key=lambda r: (r["air_date"], r["avail_id"]))  # scorer's tie-break order
    b = [r["score_breakdown"] for r in rows]
    feats = np.array([[float(r["impression_equivalence"]),
                       float(x["genre"]) / weights["w_genre"],
                       float(x["yield"]) / weights["w_yield"],
                       float(x["timeliness"]) / weights["w_timeliness"],
                       -float(x["competitive"]) / weights["w_competitive"],
                       float(x["preference"])] for r, x in zip(rows, b)])  # preference kept as weighted contribution
    target = next(i for i, r in enumerate(rows) if r["avail_id"] == case["offered_avail_id"])
    return feats, np.arange(len(rows)), target


def rank_hits(data, w, cap):
    top1 = top3 = 0
    for feats, _, target in data:
        score = (w[0] * np.minimum(feats[:, 0], cap) / cap + w[1] * feats[:, 1] + w[2] * feats[:, 2]
                 + w[3] * feats[:, 3] - w[4] * feats[:, 4] + feats[:, 5])
        order = np.argsort(-score, kind="stable")  # stable keeps air-date order on ties
        pos = int(np.where(order == target)[0][0])
        top1 += pos == 0
        top3 += pos < 3
    return top1 / len(data), top3 / len(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--samples", type=int, default=4000)
    args = ap.parse_args()

    cfg = {r["rule_key"]: r["num_value"] for r in _execute(f"SELECT rule_key, num_value FROM {fq('mg_rules_config')}")}
    cases = training_cases()
    with cf.ThreadPoolExecutor(12) as ex:
        data = [d for d in ex.map(lambda c: candidate_features(c, cfg), cases) if d is not None]
    print(f"training decisions (before cutoff, >=2 candidates): {len(data)} of {len(cases)}")

    current = np.array([cfg[k] for k in TUNED])
    base = rank_hits(data, current, cfg["ie_cap"])
    print(f"current weights {dict(zip(TUNED, current))} cap={cfg['ie_cap']}: train top1={base[0]:.3f} top3={base[1]:.3f}")

    rng = np.random.default_rng(11)
    best = (base[1], base[0], current, cfg["ie_cap"])
    for _ in range(args.samples):
        w = rng.uniform(0, 0.6, len(TUNED))
        cap = float(rng.choice([1.1, 1.2, 1.3, 1.5]))
        t1, t3 = rank_hits(data, w, cap)
        if (t3, t1) > best[:2]:
            best = (t3, t1, w, cap)
    t3, t1, w, cap = best
    w = np.round(w, 3)
    print(f"calibrated weights {dict(zip(TUNED, w))} cap={cap}: train top1={t1:.3f} top3={t3:.3f}")

    if args.apply:
        for k, v in list(zip(TUNED, w)) + [("ie_cap", cap)]:
            _execute(f"UPDATE {fq('mg_rules_config')} SET num_value = {float(v)}, "
                     f"description = regexp_replace(description, ' \\\\[calibrated.*\\\\]$', '') || ' [calibrated from {len(data)} expert decisions before the preference cutoff]' "
                     f"WHERE rule_key = '{k}'")
        print("mg_rules_config updated")


if __name__ == "__main__":
    main()
