# Databricks notebook source
# MAGIC %md
# MAGIC # Register the make-goods agent in Unity Catalog
# MAGIC
# MAGIC The LangGraph agent (`agent_server/graph.py`) is packaged as an **MLflow ResponsesAgent** with
# MAGIC *models-from-code* (`agent_server/makegoods_model.py`), registered to Unity Catalog as
# MAGIC **`<catalog>.<schema>.makegoods_agent`** (job parameters, set per bundle target), smoke-tested, and given the
# MAGIC **`champion`** alias. The target's Databricks App loads `makegoods_agent@champion` at startup,
# MAGIC so promoting a new version is: register → evaluate → move the alias → restart the app.
# MAGIC
# MAGIC | Step | What happens |
# MAGIC |---|---|
# MAGIC | 1 | Install the agent's dependencies |
# MAGIC | 2 | Declare the resources the agent uses (LLM endpoint, SQL warehouse, UC functions, tables, metric views, Genie Space) — UC records them as lineage, and Model Serving uses them for credentials |
# MAGIC | 3 | Log the agent code + requirements as an MLflow model and register a new UC version |
# MAGIC | 4 | Load the registered version back and run it on a make-good request and an open question |
# MAGIC | 5 | **Evaluate the registered version** on the UC evaluation dataset: business-correctness, rules-compliance, LLM-judge and cost scores, plus the release gate — linked to the model version |
# MAGIC | 6 | Point the alias (default `champion`) at the new version (optionally only if the gate passes) |

# COMMAND ----------

# MAGIC %pip install -q "mlflow==3.17.0" "langgraph==1.2.15" "databricks-langchain[memory]==0.21.0" "databricks-ai-bridge==0.22.0" "databricks-sdk==0.151.0" "databricks-agents>=1.12"
# MAGIC %restart_python

# COMMAND ----------

dbutils.widgets.text("alias", "champion", "Alias to point at the new version")
dbutils.widgets.text("experiment_id", "YOUR_EXPERIMENT_ID", "MLflow experiment")
dbutils.widgets.text("catalog", "YOUR_CATALOG", "Catalog")
dbutils.widgets.text("schema", "makegoods", "Schema")
dbutils.widgets.text("warehouse_id", "YOUR_WAREHOUSE_ID", "SQL warehouse")
dbutils.widgets.text("genie_space_id", "YOUR_GENIE_SPACE_ID", "Genie space")
dbutils.widgets.text("eval_cases", "40", "Evaluation cases (0 = all 150)")
dbutils.widgets.dropdown("require_gate", "false", ["true", "false"], "Promote only if the release gate passes")
ALIAS = dbutils.widgets.get("alias").strip()
EXPERIMENT_ID = dbutils.widgets.get("experiment_id").strip()
EVAL_CASES = int(dbutils.widgets.get("eval_cases") or 0)
REQUIRE_GATE = dbutils.widgets.get("require_gate") == "true"

import os
import sys
from importlib.metadata import version

import mlflow

CATALOG, SCHEMA = dbutils.widgets.get("catalog").strip(), dbutils.widgets.get("schema").strip()
# agent_server.config reads these at import time
os.environ["MG_CATALOG"], os.environ["MG_SCHEMA"] = CATALOG, SCHEMA
os.environ["MG_GENIE_SPACE_ID"] = dbutils.widgets.get("genie_space_id").strip()
MODEL_NAME = f"{CATALOG}.{SCHEMA}.makegoods_agent"
AGENT_DIR = os.path.abspath("..")  # this notebook lives in agent_app/notebooks
sys.path.insert(0, AGENT_DIR)

# The experiment stores traces in Unity Catalog tables; reading them back (evaluate, trace-based scorers) runs on this warehouse.
os.environ["MG_WAREHOUSE_ID"] = dbutils.widgets.get("warehouse_id").strip()
os.environ.setdefault("MLFLOW_TRACING_SQL_WAREHOUSE_ID", os.environ["MG_WAREHOUSE_ID"])

# Evaluation writes every score back to its UC trace from parallel worker threads (~700 writes for 40 cases). By default
# MLflow builds a new SDK client, with network round-trips, for each write, which can stall a serverless notebook for
# an hour. Resolve credentials once here (the evaluation cell has MLflow use them directly) and start the warehouse
# once instead of checking it on every write.
from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.core import Config  # noqa: E402

_cfg = Config()
os.environ["DATABRICKS_HOST"] = _cfg.host
os.environ["DATABRICKS_TOKEN"] = _cfg.authenticate()["Authorization"].split(" ", 1)[1]
os.environ["MLFLOW_SQL_WAREHOUSE_AUTO_START"] = "false"
WorkspaceClient().warehouses.start_and_wait(os.environ["MG_WAREHOUSE_ID"])

mlflow.set_registry_uri("databricks-uc")
mlflow.set_experiment(experiment_id=EXPERIMENT_ID)
print(f"model: {MODEL_NAME}  alias: {ALIAS}  agent code: {AGENT_DIR}/agent_server")

# COMMAND ----------

# MAGIC %md ## 2. Resources the agent depends on

# COMMAND ----------

from mlflow.models.resources import (
    DatabricksFunction,
    DatabricksGenieSpace,
    DatabricksServingEndpoint,
    DatabricksSQLWarehouse,
    DatabricksTable,
)

from agent_server.config import LLM_ENDPOINT, WAREHOUSE_ID
from agent_server.genie import GENIE_SPACE_ID

UC_FUNCTIONS = ["spot_context", "audience_mapper", "inventory_search", "inventory_rule_summary", "evaluate_inventory",
                "commensurate_scorer", "shortfall_calculator", "recommendation_formatter", "is_commensurate",
                "value_reconciliation", "package_builder", "gap_fillers", "program_lookup", "advertiser_lookup",
                "read_client_email"]
UC_TABLES = ["gold_advertiser_preferences", "mg_rules_config", "gold_make_good_candidates", "gold_available_inventory",
             "gold_spot_access", "gold_make_good_outcomes", "ref_sellers", "ref_advertiser_contacts",
             "mg_decision_events", "mg_advertiser_rules", "mg_client_emails", "mg_client_email_reads",
             "makegoods_open_queue_metrics", "makegoods_offer_outcome_metrics", "makegoods_preemption_metrics",
             "makegoods_contract_delivery_metrics", "makegoods_resolution_metrics"]

resources = [
    DatabricksServingEndpoint(endpoint_name=LLM_ENDPOINT),
    DatabricksSQLWarehouse(warehouse_id=WAREHOUSE_ID),
    DatabricksGenieSpace(genie_space_id=GENIE_SPACE_ID),
    *[DatabricksFunction(function_name=f"{CATALOG}.{SCHEMA}.{f}") for f in UC_FUNCTIONS],
    *[DatabricksTable(table_name=f"{CATALOG}.{SCHEMA}.{t}") for t in UC_TABLES],
]
pip_requirements = [
    f"mlflow=={version('mlflow')}",
    f"langgraph=={version('langgraph')}",
    f"databricks-langchain[memory]=={version('databricks-langchain')}",
    f"databricks-ai-bridge=={version('databricks-ai-bridge')}",
    f"databricks-sdk=={version('databricks-sdk')}",
]
print(len(resources), "resources;", pip_requirements)

# COMMAND ----------

# MAGIC %md ## 3. Log and register
# MAGIC The input example routes straight to the make-good flow (no parse LLM call) in evaluation mode, so
# MAGIC logging validates the model end to end against the real UC functions and LLM endpoint.

# COMMAND ----------

input_example = {
    "input": [{"role": "user", "content": "Find a commensurate make-good for spot SPT-0058222"}],
    "custom_inputs": {"spot_id": "SPT-0058222", "mode": "find", "eval": True},
}

with mlflow.start_run(run_name="register_makegoods_agent") as run:
    mlflow.log_params({"llm_endpoint": LLM_ENDPOINT, "genie_space_id": GENIE_SPACE_ID, "framework": "langgraph"})
    info = mlflow.pyfunc.log_model(
        name="makegoods_agent",
        python_model=os.path.join(AGENT_DIR, "agent_server", "makegoods_model.py"),
        code_paths=[os.path.join(AGENT_DIR, "agent_server")],
        resources=resources,
        pip_requirements=pip_requirements,
        input_example=input_example,
        registered_model_name=MODEL_NAME,
    )

NEW_VERSION = info.registered_model_version
print(f"registered {MODEL_NAME} version {NEW_VERSION}  (model_id {info.model_id}, run {run.info.run_id})")

# COMMAND ----------

# MAGIC %md ## 4. Smoke-test the registered version

# COMMAND ----------

registered = mlflow.pyfunc.load_model(f"models:/{MODEL_NAME}/{NEW_VERSION}")
for request in [
    input_example,
    {"input": [{"role": "user", "content": "Which markets have the most make-good revenue at risk right now?"}],
     "custom_inputs": {"eval": True}},
]:
    out = registered.predict(request)
    text = "\n\n".join(c["text"] for item in out["output"] for c in item.get("content", []) if c.get("type") == "output_text")
    co = out.get("custom_outputs") or {}
    print(f"=== {request['input'][0]['content']}\noutcome={co.get('outcome')} intent={co.get('intent')} "
          f"llm_calls={co.get('llm_calls')} tokens={co.get('llm_tokens')}\n{text[:900]}\n")

# COMMAND ----------

# MAGIC %md ## 5. Evaluate the registered version
# MAGIC Same scorers as `eval/run_eval.py` (definitions in `eval/scorers.py`), run against the **registered** model:
# MAGIC
# MAGIC | Scorer | Checks |
# MAGIC |---|---|
# MAGIC | `audience_match` | Free-text contract audience mapped to the segment ad ops intended |
# MAGIC | `outcome_correct` | Make-good when commensurate inventory exists; near-miss or refund when it does not |
# MAGIC | `top1_hit`, `top3_hit` | The expert's historical choice is the agent's #1 / in its top 3 |
# MAGIC | `inventory_relevance` | Tier labels are truthful (verified by the UC rules engine) |
# MAGIC | `constraint_respected` | Reviewer constraints such as "avoid news" are honored |
# MAGIC | `grounded_ids` | No invented avail ids in the explanation |
# MAGIC | `actionable_recommendation`, `business_rationale`, `impression_math`, `answer_guidelines` | LLM judges on what the reviewer reads |
# MAGIC | `llm_calls`, `total_tokens`, `tokens_vs_gpt_baseline` | Cost, read from each MLflow trace, vs the GPT-assist baseline |
# MAGIC | `dollar_math`, `dollar_reconciliation_correct` | Every dollar figure comes from the rules engine; option A's value recomputed from the source tables |
# MAGIC | `package_valid`, `checklist_truthful` | Options are possible, and their rule checklist agrees with the rules engine |
# MAGIC | `rule_respected` | No option breaks the client's rules |
# MAGIC | `reply_parse_correct`, `injection_resisted` | Client replies read correctly; replies that try to instruct the system are contained |
# MAGIC | `rule_parse_correct`, `offer_grounded`, `offer_email_quality` | Client rules from conversation; offer emails grounded and well written |

# COMMAND ----------

import hashlib

os.environ.setdefault("MLFLOW_GENAI_EVAL_MAX_WORKERS", "8")
from eval.predict import registered_predict  # noqa: E402
from eval.scorers import GATE, SCORERS  # noqa: E402

df = mlflow.genai.datasets.get_dataset(name=f"{CATALOG}.{SCHEMA}.mg_eval_set").to_df()
df["case_type"] = df["inputs"].map(lambda i: i.get("case_type"))
if EVAL_CASES and EVAL_CASES < len(df):  # deterministic stratified sample, same cases for every version
    df = df.assign(_k=df["inputs"].map(lambda i: hashlib.md5(i["request"].encode()).hexdigest())).sort_values("_k")
    share = EVAL_CASES / len(df)
    df = df.groupby("case_type", group_keys=False).apply(lambda g: g.head(max(1, round(len(g) * share))))
print("evaluation cases by type:", df["case_type"].value_counts().to_dict())

os.environ["MLFLOW_ENABLE_DB_SDK"] = "false"  # score writes use the credentials pinned in the config cell (no client per write)
try:
    with mlflow.start_run(run_name=f"eval-makegoods_agent-v{NEW_VERSION}") as eval_run:
        mlflow.set_tags({"eval.model_name": MODEL_NAME, "eval.model_version": str(NEW_VERSION), "eval.variant": f"registered-v{NEW_VERSION}",
                         "eval.dataset": f"{CATALOG}.{SCHEMA}.mg_eval_set"})
        results = mlflow.genai.evaluate(data=df[["inputs", "expectations"]], predict_fn=registered_predict(registered),
                                        scorers=SCORERS, model_id=info.model_id)
finally:
    os.environ.pop("MLFLOW_ENABLE_DB_SDK", None)

scores = {k.removesuffix("/mean"): round(float(v), 3) for k, v in results.metrics.items() if k.endswith("/mean")}
gate = {k.removesuffix("/mean"): (scores.get(k.removesuffix("/mean")), t) for k, t in GATE.items()}
GATE_PASSED = all(v is not None and v >= t for v, t in gate.values())
mlflow.log_metric("release_gate_passed", float(GATE_PASSED), run_id=eval_run.info.run_id)

import pandas as pd  # noqa: E402

# Component checks: reading client replies (incl. injection attempts), client rules from conversation, offer emails
from databricks_langchain import ChatDatabricks  # noqa: E402

from eval.component_cases import records as component_records  # noqa: E402
from eval.run_component_eval import offer_predict, reply_predict, rule_predict  # noqa: E402
from eval.scorers import COMPONENT_GATE, COMPONENT_SCORERS  # noqa: E402

from agent_server.config import llm_params  # noqa: E402

_llm = ChatDatabricks(endpoint=LLM_ENDPOINT, extra_params=llm_params(LLM_ENDPOINT))
os.environ["MLFLOW_ENABLE_DB_SDK"] = "false"
try:
    for kind, fn in (("reply", reply_predict), ("rule", rule_predict), ("offer", offer_predict)):
        with mlflow.start_run(run_name=f"eval-components-{kind}-v{NEW_VERSION}"):
            mlflow.set_tags({"eval.model_name": MODEL_NAME, "eval.model_version": str(NEW_VERSION), "eval.component": kind})
            r = mlflow.genai.evaluate(data=component_records()[kind], predict_fn=fn(_llm), scorers=COMPONENT_SCORERS[kind])
        scores.update({k.removesuffix("/mean"): round(float(v), 3) for k, v in r.metrics.items() if k.endswith("/mean")})
finally:
    os.environ.pop("MLFLOW_ENABLE_DB_SDK", None)
GATE = {**GATE, **COMPONENT_GATE}
gate = {k.removesuffix("/mean"): (scores.get(k.removesuffix("/mean")), t) for k, t in GATE.items()}
GATE_PASSED = all(v is not None and v >= t for v, t in gate.values())
mlflow.log_metric("release_gate_passed", float(GATE_PASSED), run_id=eval_run.info.run_id)

display(pd.DataFrame([{"scorer": k, "score": v, "gate": GATE.get(f"{k}/mean"),
                       "status": ("PASS" if v >= GATE[f"{k}/mean"] else "FAIL") if f"{k}/mean" in GATE else ""}
                      for k, v in sorted(scores.items())]))
print(f"RELEASE GATE: {'PASSED' if GATE_PASSED else 'FAILED'}  |  eval run {eval_run.info.run_id} (MLflow experiment -> Evaluation runs)")

# COMMAND ----------

# MAGIC %md ## 6. Promote: point the alias at the new version
# MAGIC The scores are saved as `score_<scorer>` tags on the model version, so the app (and Catalog Explorer) show how each version performed.
# MAGIC With `require_gate = true` the alias only moves when every release-gate threshold passes.
# MAGIC After this cell, restart the app (`databricks bundle run makegoods_agent`) to serve the new version.

# COMMAND ----------

from mlflow import MlflowClient

client = MlflowClient(registry_uri="databricks-uc")
client.set_model_version_tag(MODEL_NAME, str(NEW_VERSION), "release_gate", "passed" if GATE_PASSED else "failed")
client.set_model_version_tag(MODEL_NAME, str(NEW_VERSION), "eval_run_id", eval_run.info.run_id)
for k, v in scores.items():  # UC tag keys can't contain "." or "="
    client.set_model_version_tag(MODEL_NAME, str(NEW_VERSION), f"score_{k}", str(v))
if ALIAS and (GATE_PASSED or not REQUIRE_GATE):
    client.set_registered_model_alias(MODEL_NAME, ALIAS, NEW_VERSION)
    print(f"{MODEL_NAME}@{ALIAS} -> version {NEW_VERSION}" + ("" if GATE_PASSED else "  (gate failed; promoted because require_gate=false)"))
else:
    print(f"alias {ALIAS} NOT moved: release gate failed")
for v in sorted(int(mv.version) for mv in client.search_model_versions(f"name='{MODEL_NAME}'")):
    mv = client.get_model_version(MODEL_NAME, str(v))
    print(f"  v{v}  aliases={mv.aliases}  release_gate={(mv.tags or {}).get('release_gate')}")
