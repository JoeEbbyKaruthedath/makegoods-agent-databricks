"""How the agent is doing in production: the monitoring scorers' results and people's decisions on app traffic.

  python eval/production_report.py [--days 7] [--by-day]

Reads the app's traces (evaluation traces are left out) with their assessments: the scorers started by
notebooks/04_production_monitoring.py, and the feedback the app logs from people (manager_decision, client_decision).
Prints, per scorer, how many traces it scored and the pass rate.
"""
import argparse
import collections
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
import mlflow  # noqa: E402

PASS = {True, "yes", "true", "True"}
FAIL = {False, "no", "false", "False"}


def _json(v):
    for _ in range(3):
        if not isinstance(v, str):
            break
        try:
            v = json.loads(v)
        except ValueError:
            break
    return v if isinstance(v, dict) else {}


def is_app_traffic(t) -> bool:
    """App requests: the agent's root span, no evaluation flag, not an evaluation run's trace."""
    if "mlflow.eval.requestId" in (t.info.tags or {}) or (t.info.request_metadata or {}).get("mlflow.sourceRun"):
        return False
    root = next((s for s in t.data.spans if s.name == "makegoods_agent"), None)
    return root is not None and not (_json(root.inputs).get("custom_inputs") or {}).get("eval")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--by-day", action="store_true")
    ap.add_argument("--max", type=int, default=5000)
    a = ap.parse_args()
    since = int((time.time() - a.days * 86400) * 1000)
    traces = mlflow.search_traces(locations=[os.environ["MLFLOW_EXPERIMENT_ID"]], max_results=a.max, return_type="list",
                                  filter_string=f"trace.timestamp_ms > {since}", order_by=["timestamp_ms DESC"])
    app = [t for t in traces if is_app_traffic(t)]
    steps = collections.Counter(_json(next(s for s in t.data.spans if s.name == "makegoods_agent").inputs).get("resumed_from")
                                or "new request" for t in app)
    print(f"{len(app)} app requests in the last {a.days} days: " + ", ".join(f"{k} {v}" for k, v in steps.most_common()))
    scores: dict[tuple, list] = collections.defaultdict(list)
    people: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for t in app:
        day = time.strftime("%Y-%m-%d", time.localtime(t.info.timestamp_ms / 1000)) if a.by_day else "all"
        for asm in t.info.assessments or []:
            value = getattr(asm, "value", None)
            if getattr(getattr(asm, "source", None), "source_type", "") == "HUMAN":
                people[asm.name][str(value)] += 1
            elif value in PASS or value in FAIL:
                scores[(asm.name, day)].append(value in PASS)
    if not scores and not people:
        print("No assessments yet: start the scorers with the production_monitoring job, then use the app.")
        return 0
    print(f"\n{'scorer':<28} {'day':<11} {'scored':>6} {'pass':>6}")
    for (name, day), vals in sorted(scores.items()):
        print(f"{name:<28} {day:<11} {len(vals):>6} {sum(vals) / len(vals):>6.0%}")
    if people:
        print("\nDecisions logged by people on the recommendations:")
        for name, c in sorted(people.items()):
            print(f"  {name:<20} " + ", ".join(f"{k} {v}" for k, v in c.most_common()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
