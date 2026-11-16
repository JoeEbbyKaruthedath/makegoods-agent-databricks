"""Run the make-goods graph for a Responses API request (wrapped as an MLflow ResponsesAgent in makegoods_model.py).

custom_inputs:
  thread_id   conversation id (one make-good thread, from the request to the booking); generated when absent
  spot_id + mode="find"   UI "Find make-good" button: routed without an LLM call
  review      the answer to the step the thread is paused at (custom_outputs.pending lists the allowed actions):
                make_good_review  accept | modify | reject | refine | refund | book | offer | review_selection | add_rule
                manager_review    approve | request_changes | reject        confirm_send   send | cancel
                await_client      client_reply | manual_reply | withdraw    seller_confirm book | counter | refund | withdraw
                confirm_rule      save | cancel
              e.g. {"action": "offer", "labels": ["A", "C"]}; the reviewer is the signed-in person
  as_of       decision date for point-in-time replay (evaluation); defaults to the simulation clock
  eval        True = no Lakebase writes, no pause for review (offline evaluation)
  memory      "baseline" = ignore reviewer-learned preferences (deterministic evaluation)
  rules       client rules to apply in evaluation (live runs read them from Lakebase)
eval, no_review, memory, rules and llm_endpoint are evaluation-only: the app (MG_REQUIRE_USER) ignores them.
"""
import logging
from typing import Any, AsyncGenerator, Optional
from uuid import uuid4

import mlflow
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command
from mlflow.entities import SpanType
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
    create_text_output_item,
)

from agent_server.config import LLM_ENDPOINT
from agent_server.graph import build_graph
from agent_server.memory import Memory
from agent_server.nodes_offer import compact_package
from agent_server.uc_tools import current_user, require_user

logger = logging.getLogger(__name__)
# Graph nodes are traced explicitly (graph.traced_node) so the trace reads like the flowchart;
# OpenAI autolog captures every LLM call (ChatDatabricks uses the OpenAI client) with prompts and token usage.
mlflow.openai.autolog()
logging.getLogger("mlflow.utils.autologging_utils").setLevel(logging.ERROR)

_graphs: dict[str, Any] = {}
_memory: Optional[Memory] = None
EVAL_ONLY = ("eval", "no_review", "memory", "rules", "llm_endpoint")
WAITING = {"make_good_review": "the seller's decision on the options", "manager_review": "the sales manager's sign-off",
           "confirm_send": "the seller to send the offer", "await_client": "the client's reply",
           "seller_confirm": "the seller to confirm the client's answer", "confirm_rule": "the seller to confirm the client rule"}


def set_memory(memory: Optional[Memory]) -> None:
    global _memory
    _memory = memory
    _graphs.clear()


def get_graph(eval_mode: bool, llm_endpoint: str = LLM_ENDPOINT):
    key = f"{'eval' if eval_mode or _memory is None else 'live'}:{llm_endpoint}"
    if key not in _graphs:
        _graphs[key] = build_graph(None if key.startswith("eval") else _memory, llm_endpoint)
    return _graphs[key]


def _user_text(request: ResponsesAgentRequest) -> str:
    for item in reversed(request.input):
        d = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        if d.get("role") == "user":
            c = d.get("content")
            return c if isinstance(c, str) else " ".join(p.get("text", "") for p in (c or []) if isinstance(p, dict))
    return ""


def _pending(snapshot) -> Optional[dict]:
    return snapshot.interrupts[0].value if snapshot and snapshot.interrupts else None


async def run_agent(request: ResponsesAgentRequest, graph=None) -> AsyncGenerator[tuple[str, Any], None]:
    """Yields ("step", [trail items]) as nodes finish, ("text", str) per user-facing message, then ("final", custom_outputs)."""
    ci = dict(request.custom_inputs or {})
    if require_user():
        for k in EVAL_ONLY:
            ci.pop(k, None)
    thread_id = str(ci.get("thread_id") or getattr(request.context, "conversation_id", None) or uuid4())
    graph = graph or get_graph(bool(ci.get("eval")), ci.get("llm_endpoint") or LLM_ENDPOINT)
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 80}
    user = current_user() or getattr(request.context, "user_id", None) or (ci.get("review") or {}).get("reviewer")
    if ci.get("review") and current_user():
        ci["review"] = {**ci["review"], "reviewer": current_user()}  # who decided is the signed-in person, not the request

    pending = _pending(await graph.aget_state(config))
    text = _user_text(request)
    if pending and ci.get("review"):
        if ci["review"].get("action") not in pending.get("actions", []):
            yield "text", (f"That isn't possible right now: this make-good is waiting for "
                           f"{WAITING.get(pending.get('type'), 'another step')}.")
            yield "final", await _final(graph, config, thread_id, None)
            return
        inp: Any = Command(resume=ci["review"])
    elif pending and text and not ci.get("mode"):
        if pending.get("type") != "make_good_review":
            yield "text", (f"This make-good is waiting for {WAITING.get(pending.get('type'), 'another step')}; "
                           f"use the buttons on it, or start a new conversation.")
            yield "final", await _final(graph, config, thread_id, None)
            return
        inp = Command(resume={"action": "refine", "text": text, "reviewer": user})
    else:
        inp = {"messages": [HumanMessage(text)], "custom_inputs": ci}

    with mlflow.start_span(name="makegoods_agent", span_type=SpanType.AGENT) as root:
        root.set_inputs({"message": text, "custom_inputs": ci, "resumed_from": (pending or {}).get("type")})
        mlflow.update_current_trace(metadata={"mlflow.trace.session": thread_id, **({"mlflow.trace.user": user} if user else {})})
        said: list[str] = []
        async for chunk in graph.astream(inp, config, stream_mode="updates"):
            for node, update in chunk.items():
                if node.startswith("__") or not isinstance(update, dict):
                    continue
                if update.get("trail"):
                    yield "step", update["trail"] if isinstance(update["trail"], list) else update["trail"]["reset"]
                for m in update.get("messages") or []:
                    if isinstance(m, AIMessage) and m.content and not m.tool_calls:
                        said.append(m.content if isinstance(m.content, str) else str(m.content))
                        yield "text", said[-1]
        snap = await graph.aget_state(config)
        root.set_outputs({"response": "\n\n".join(said), "next": list(snap.next), "outcome": snap.values.get("outcome"),
                          "stage": snap.values.get("stage"), "pending": (_pending(snap) or {}).get("type")})
        trace_id = root.trace_id

    yield "final", await _final(graph, config, thread_id, trace_id)


async def thread_state(thread_id: str) -> dict:
    """Where a thread is now (same shape as the final event), e.g. to open it from the offers board."""
    graph = get_graph(False)
    return await _final(graph, {"configurable": {"thread_id": thread_id}}, thread_id, None)


async def _final(graph, config: dict, thread_id: str, trace_id: Optional[str]) -> dict:
    final = await graph.aget_state(config)
    state = final.values
    usage = state.get("llm_usage") or []
    pending = _pending(final)
    offer = state.get("offer") or {}
    return {
        "thread_id": thread_id,
        "status": "awaiting_review" if pending else "complete",
        "pending": pending,  # the paused step: type, allowed actions and what it shows (built by the graph)
        "stage": state.get("stage"),
        "intent": state.get("intent"),
        "spot_id": state.get("spot_id"),
        "outcome": state.get("outcome"),
        "recommended_avail_id": (state.get("ranked") or [{}])[0].get("avail_id") if state.get("ranked") else None,
        "ranked": [{k: r.get(k) for k in ("rank", "avail_id", "air_date", "daypart", "program_name", "adjacency_tags",
                                         "est_impressions", "impression_equivalence", "score", "score_breakdown", "reasons",
                                         "match_score", "tier", "misses", "rate_card_usd")}
                   for r in (state.get("ranked") or [])[:10]],
        "packages": [compact_package(p) for p in state.get("packages") or []],
        "suggestions": state.get("suggestions") or [],
        "selection": (state.get("selection") or {}).get("labels"),
        "offer": {k: offer.get(k) for k in ("offer_id", "subject", "status", "contact_name")} if offer else None,
        "client_reply": ((state.get("client_reply") or {}).get("parsed")),
        "client_rules": [r.get("label") for r in state.get("rules") or []],
        "owed_value_usd": (state.get("spot") or {}).get("unit_rate_usd"),
        "audience": state.get("audience"),
        "prefs": state.get("prefs"),
        "rule_summary": state.get("rule_summary"),
        "shortfall": state.get("shortfall"),
        "search_passes": (state.get("gap_iterations") or 0) + 1,
        "trail": state.get("trail"),
        "llm_calls": len(usage),
        "llm_tokens": sum(u["input_tokens"] + u["output_tokens"] for u in usage),
        "trace_id": trace_id,
        "genie": state.get("genie") if state.get("intent") == "open_question" else None,
        "recommendation_trace_id": state.get("rec_trace_id"),
    }


async def ainvoke(request: ResponsesAgentRequest, graph=None) -> ResponsesAgentResponse:
    outputs, custom_outputs = [], {}
    async for kind, value in run_agent(request, graph):
        if kind == "text":
            outputs.append(create_text_output_item(text=value, id=str(uuid4())))
        elif kind == "final":
            custom_outputs = value
    return ResponsesAgentResponse(output=outputs, custom_outputs=custom_outputs)


async def astream(request: ResponsesAgentRequest, graph=None) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    async for kind, value in run_agent(request, graph):
        if kind == "text":
            yield ResponsesAgentStreamEvent(type="response.output_item.done",
                                            item=create_text_output_item(text=value, id=str(uuid4())))
