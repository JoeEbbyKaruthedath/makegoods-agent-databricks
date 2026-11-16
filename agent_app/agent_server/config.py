"""Runtime configuration (env vars set by app.yaml in the Databricks App, or .env locally)."""
import os

CATALOG = os.getenv("MG_CATALOG", "YOUR_CATALOG")
SCHEMA = os.getenv("MG_SCHEMA", "makegoods")
WAREHOUSE_ID = os.getenv("MG_WAREHOUSE_ID", "YOUR_WAREHOUSE_ID")
LLM_ENDPOINT = os.getenv("MG_LLM_ENDPOINT", "databricks-claude-sonnet-5-5")
# The LLM steps are short and fully specified (route, explain, write prose, read a reply): no hidden reasoning needed.
# Claude models think before answering unless told otherwise; that can triple a step's output tokens.
LLM_THINKING = os.getenv("MG_LLM_THINKING", "between_tools")  # "default" = leave the model's setting


def llm_params(endpoint: str) -> dict:
    """Extra request parameters for an LLM endpoint."""
    if "claude" in endpoint and LLM_THINKING != "default":
        return {"extra_body": {"thinking": {"type": LLM_THINKING}}}
    return {}

LAKEBASE_ENDPOINT = os.getenv("LAKEBASE_AUTOSCALING_ENDPOINT") or None
LAKEBASE_PROJECT = os.getenv("LAKEBASE_AUTOSCALING_PROJECT") or None
LAKEBASE_BRANCH = os.getenv("LAKEBASE_AUTOSCALING_BRANCH") or None
MEMORY_SCHEMA = os.getenv("LAKEBASE_AGENT_MEMORY_SCHEMA", "makegoods_agent_memory")  # LangGraph checkpoints + store
APP_SCHEMA = os.getenv("LAKEBASE_APP_SCHEMA", "makegoods")  # review queue + decision log

PROMPT_EXPLAIN = f"{CATALOG}.{SCHEMA}.mg_explain_recommendation"
PROMPT_PARSE = f"{CATALOG}.{SCHEMA}.mg_parse_request"
PROMPT_OFFER = f"{CATALOG}.{SCHEMA}.mg_offer_email"
PROMPT_REPLY = f"{CATALOG}.{SCHEMA}.mg_client_reply"


def fq(name: str) -> str:
    return f"{CATALOG}.{SCHEMA}.{name}"


def lakebase_kwargs() -> dict:
    if LAKEBASE_ENDPOINT:
        return {"autoscaling_endpoint": LAKEBASE_ENDPOINT}
    return {"project": LAKEBASE_PROJECT, "branch": LAKEBASE_BRANCH}
