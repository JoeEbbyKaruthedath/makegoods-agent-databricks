"""predict_fns for evaluation: the LangGraph agent (any model) and the "old way" baseline.

mlflow.genai.evaluate calls predict_fn from worker threads and, during evaluation, exports every
span to the Unity Catalog trace tables synchronously (one network call per span end). Each call
therefore runs on its own event loop with its own graph/LLM client, so one row's span exports never
stall another row. asyncio.run starts the task in the worker thread's context, keeping the agent's
spans inside the evaluation row's trace.
"""
import asyncio
import json
import re
from pathlib import Path
from uuid import uuid4

import mlflow
from databricks_langchain import ChatDatabricks
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from mlflow.types.responses import ResponsesAgentRequest

from agent_server.agent import run_agent
from agent_server.config import CATALOG, SCHEMA
from agent_server.graph import build_graph
from agent_server.uc_tools import call_uc

# The agent traces itself (explicit node spans + OpenAI autolog for LLM calls). Stop mlflow.genai.evaluate
# from also switching on LangChain autolog: its per-runnable callbacks would duplicate every span.
mlflow.langchain.autolog(disable=True)


def _outputs(texts: list[str], final: dict) -> dict:
    ranked = final.get("ranked") or []
    return {
        "response": "\n\n".join(texts),
        "outcome": final.get("outcome") or None,
        "spot_id": final.get("spot_id"),
        "audience_segment_code": (final.get("audience") or {}).get("segment_code"),
        "recommended_avail_ids": [r["avail_id"] for r in ranked[:5]],
        "recommended_tags": {r["avail_id"]: r.get("adjacency_tags") or [] for r in ranked[:5]},
        "recommended_tiers": {r["avail_id"]: r.get("tier") for r in ranked[:5]},
        "recommended_lines": [{k: r.get(k) for k in ("avail_id", "air_date", "program_name", "program_genre", "adjacency_tags")}
                              for r in ranked[:5]],
        "top_match_score": ranked[0].get("match_score") if ranked else None,
        "packages": final.get("packages") or [],
        "known_avail_ids": sorted({r["avail_id"] for r in ranked} |
                                  {a for p in final.get("packages") or [] for a in p.get("avail_ids") or []}),
        "owed_value_usd": final.get("owed_value_usd"),
        "client_rules": final.get("client_rules") or [],
        "search_passes": final.get("search_passes"),
    }


def agent_predict(llm_endpoint: str):
    def predict_fn(request: str, as_of: str | None = None, case_type: str | None = None, rules: list | None = None):
        async def go():
            graph = build_graph(None, llm_endpoint)
            ci = {"eval": True, "memory": "baseline", "as_of": as_of, "llm_endpoint": llm_endpoint, "thread_id": str(uuid4()),
                  **({"rules": rules} if rules else {})}
            req = ResponsesAgentRequest(input=[{"role": "user", "content": request}], custom_inputs=ci)
            texts, final = [], {}
            async for kind, value in run_agent(req, graph=graph):
                if kind == "text":
                    texts.append(value)
                elif kind == "final":
                    final = value
            return texts, final

        texts, final = asyncio.run(go())
        return _outputs(texts, final)

    return predict_fn


# ---------------------------------------------------------------------------------- old way baseline
def _policy_text() -> str:
    """The written make-good policy: local generator output, else the copy in the UC landing volume."""
    for path in (Path(__file__).resolve().parents[2] / "data/raw/reference/make_good_policy.md",
                 Path(f"/Volumes/{CATALOG}/{SCHEMA}/raw_landing/reference/make_good_policy.md")):
        if path.exists():
            return path.read_text()
    return ""


OLD_SYSTEM_TEMPLATE = """You are a make-good assistant for a broadcast TV ad operations team.
A preempted or missed spot must get a commensurate make-good or the advertiser is refunded.
Use the tools to look up the spot and its contract, the station's inventory, and the audience rating indices.
Apply every rule in the policy below yourself, including computing each avail's rating in the contract's audience
segment (household rating x the segment index for the avail's daypart and genre) and comparing it to the floor.
Rank the commensurate avails, best first, and explain your recommendation for the reviewer.

{policy}

End your answer with a JSON object on its own line:
{{"segment_code": "<canonical code such as A25-54>", "outcome": "make_good" or "refund", "ranked_avail_ids": ["AVL-...", "..."], "ranked_tags": {{"AVL-...": ["tag", "..."]}}}}"""


@tool
async def get_spot_and_contract(spot_id: str) -> str:
    """The preempted spot and its contract exactly as stored (free-text audience target, guaranteed rating, window, flight)."""
    return json.dumps(await call_uc("raw_spot_and_contract", p_spot_id=spot_id.upper()), default=str)


@tool
async def get_station_inventory(station_id: str, from_date: str, to_date: str) -> str:
    """All avails on a station between two dates (YYYY-MM-DD), raw: household rating, status, tags, competitive blocks."""
    return json.dumps(await call_uc("raw_station_inventory", p_station_id=station_id, p_from=from_date, p_to=to_date), default=str)


@tool
async def get_audience_indices() -> str:
    """Audience taxonomy: segment codes, their aliases, and rating index by daypart and genre."""
    return json.dumps(await call_uc("raw_audience_indices"), default=str)


def old_way_predict(llm_endpoint: str):
    def predict_fn(request: str, as_of: str | None = None, case_type: str | None = None, rules: list | None = None):
        async def go():
            agent = create_agent(model=ChatDatabricks(endpoint=llm_endpoint),
                                 tools=[get_spot_and_contract, get_station_inventory, get_audience_indices],
                                 system_prompt=OLD_SYSTEM_TEMPLATE.format(policy=_policy_text()))
            today = as_of or "2026-10-30"
            result = await agent.ainvoke({"messages": [HumanMessage(f"Today is {today}; only avails airing after today are usable.\n\n{request}")]},
                                         {"recursion_limit": 25})
            return result["messages"]

        messages = asyncio.run(go())
        text = next((m.content for m in reversed(messages) if isinstance(m, AIMessage) and m.content), "")
        text = text if isinstance(text, str) else " ".join(c.get("text", "") for c in text if isinstance(c, dict))
        parsed = {}
        for m in reversed(list(re.finditer(r"\{.*\}", text, re.S))):
            try:
                parsed = json.loads(m.group(0))
                break
            except Exception:
                continue
        spot = re.search(r"SPT-\d{7}", request)
        return {
            "response": text,
            "outcome": parsed.get("outcome") or ("make_good" if spot else None),
            "spot_id": spot.group(0) if spot else None,
            "audience_segment_code": parsed.get("segment_code"),
            "recommended_avail_ids": list(parsed.get("ranked_avail_ids") or [])[:5],
            "recommended_tags": parsed.get("ranked_tags") or {},
        }

    return predict_fn


# ---------------------------------------------------------------------------------- registered model
def registered_predict(pyfunc_model):
    """predict_fn for a model loaded from the registry (mlflow.pyfunc.load_model): same outputs as agent_predict,
    so the same scorers evaluate a registered version exactly like local code."""
    def predict_fn(request: str, as_of: str | None = None, case_type: str | None = None, rules: list | None = None):
        out = pyfunc_model.predict({"input": [{"role": "user", "content": request}],
                                    "custom_inputs": {"eval": True, "memory": "baseline", "as_of": as_of, "thread_id": str(uuid4()),
                                                      **({"rules": rules} if rules else {})}})
        texts = [c["text"] for item in out.get("output", []) for c in item.get("content", []) if c.get("type") == "output_text"]
        return _outputs(texts, out.get("custom_outputs") or {})

    return predict_fn
