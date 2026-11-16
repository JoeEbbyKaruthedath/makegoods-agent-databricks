"""Run the make-good evaluation as a named MLflow run, so variants can be compared side by side.

  python eval/run_eval.py --variant v1-sonnet                 # the agent, full dataset
  python eval/run_eval.py --variant v1-haiku                  # same graph, cheaper model
  python eval/run_eval.py --variant old-way --limit 40        # LLM applies the rules itself (the earlier GPT approach)
  python eval/run_eval.py --variant v1-sonnet --cases history --limit 20   # quick subset

The release gate (scorers.GATE) is checked for agent variants; exit code 1 when it fails.
"""
import argparse
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
os.environ.setdefault("MLFLOW_GENAI_EVAL_MAX_WORKERS", "8")


def pin_oauth_token() -> None:
    """Mint one OAuth token up front for all eval workers. With a CLI profile, every SDK client otherwise
    shells out to `databricks auth token`; under heavy parallelism the OS keyring starts failing (401s)."""
    from databricks.sdk.core import Config

    profile = os.environ.pop("DATABRICKS_CONFIG_PROFILE", None)
    if not profile:
        return
    cfg = Config(profile=profile)
    os.environ["DATABRICKS_HOST"] = cfg.host
    os.environ["DATABRICKS_TOKEN"] = cfg.authenticate()["Authorization"].split(" ", 1)[1]


pin_oauth_token()

import mlflow  # noqa: E402

from agent_server import prompts  # noqa: E402
from agent_server.config import CATALOG, PROMPT_EXPLAIN, PROMPT_PARSE, SCHEMA  # noqa: E402
from eval.predict import agent_predict, old_way_predict  # noqa: E402
from eval.scorers import GATE, SCORERS, gpt_baseline_tokens  # noqa: E402
from agent_server.uc_tools import _execute  # noqa: E402

VARIANTS = {
    "v1-sonnet": ("agent", "databricks-claude-sonnet-5-5"),
    "v1-sonnet-calibrated": ("agent", "databricks-claude-sonnet-5-5"),  # same code, weights fitted by calibrate_weights.py
    "v1-haiku": ("agent", "databricks-claude-haiku-5-5"),
    "old-way": ("old_way", "databricks-claude-sonnet-5-5"),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=VARIANTS, default="v1-sonnet")
    ap.add_argument("--limit", type=int, default=0, help="stratified subset size (0 = all)")
    ap.add_argument("--cases", default="", help="comma-separated case types to include")
    args = ap.parse_args()
    kind, endpoint = VARIANTS[args.variant]

    mlflow.set_experiment(experiment_id=os.environ["MLFLOW_EXPERIMENT_ID"])
    dataset = mlflow.genai.datasets.get_dataset(name=f"{CATALOG}.{SCHEMA}.mg_eval_set")
    df = dataset.to_df()
    df["case_type"] = df["inputs"].map(lambda i: i.get("case_type"))
    if args.cases:
        df = df[df["case_type"].isin(args.cases.split(","))]
    if args.limit:
        # deterministic, proportional to case type, so every variant sees the same subset
        df = df.assign(_k=df["inputs"].map(lambda i: hashlib.md5(i["request"].encode()).hexdigest())).sort_values("_k")
        share = args.limit / len(df)
        df = df.groupby("case_type", group_keys=False).apply(lambda g: g.head(max(1, round(len(g) * share))))
    data = df[["inputs", "expectations"]] if (args.limit or args.cases) else dataset
    n = len(df)

    predict_fn = agent_predict(endpoint) if kind == "agent" else old_way_predict(endpoint)
    print(f"variant={args.variant} endpoint={endpoint} cases={n} gpt_baseline_tokens={gpt_baseline_tokens():,.0f}")
    with mlflow.start_run(run_name=args.variant) as run:
        weights = {r["rule_key"]: r["num_value"] for r in _execute(
            f"SELECT rule_key, num_value FROM {CATALOG}.{SCHEMA}.mg_rules_config WHERE rule_type = 'soft_match'")}
        mlflow.log_params({f"rule.{k}": v for k, v in weights.items()})
        mlflow.log_params({"variant": args.variant, "agent": kind, "llm_endpoint": endpoint, "n_cases": n,
                           "framework": "langgraph" if kind == "agent" else "react-prompt-only",
                           "prompt_parse": prompts.load(PROMPT_PARSE)[1], "prompt_explain": prompts.load(PROMPT_EXPLAIN)[1]})
        mlflow.set_tags({"eval.dataset": f"{CATALOG}.{SCHEMA}.mg_eval_set", "eval.variant": args.variant})
        result = mlflow.genai.evaluate(data=data, predict_fn=predict_fn, scorers=SCORERS)

    m = result.metrics
    print(f"\nrun_id={run.info.run_id}")
    for k in sorted(m):
        if k.endswith("/mean"):
            print(f"  {k:<42} {m[k]:.3f}")
    if kind == "agent":
        failed = {k: (m.get(k), t) for k, t in GATE.items() if m.get(k) is None or m[k] < t}
        print("\nRELEASE GATE:", "PASSED" if not failed else f"FAILED {failed}")
        sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
