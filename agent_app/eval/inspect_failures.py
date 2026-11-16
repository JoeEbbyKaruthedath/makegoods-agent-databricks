"""Summarize failing rows of an evaluation run from its traces and assessments.

  python eval/inspect_failures.py <run_id> <scorer_name> [--n 8]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
import mlflow  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("run_id")
ap.add_argument("scorer")
ap.add_argument("--n", type=int, default=8)
a = ap.parse_args()

df = mlflow.search_traces(locations=[mlflow.get_run(a.run_id).info.experiment_id], run_id=a.run_id, max_results=500)
fails = []
for _, row in df.iterrows():
    for asm in row["assessments"] or []:
        fb = asm.get("feedback") or {}
        if asm.get("assessment_name") == a.scorer and fb.get("value") in (False, "no"):
            req = json.loads(row["request"]) if isinstance(row["request"], str) else (row["request"] or {})
            fails.append((req.get("message") or req.get("request") or "", asm.get("rationale"), row["trace_id"]))
print(f"{a.scorer}: {len(fails)} failing rows")
for msg, rationale, tid in fails[: a.n]:
    print("\n- request:", msg[:120])
    print("  trace:", tid)
    print("  rationale:", (rationale or "")[:700].replace("\n", " "))
