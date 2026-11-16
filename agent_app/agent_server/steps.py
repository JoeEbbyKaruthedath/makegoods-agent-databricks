"""Shared pieces of the make-good graph: vocabularies, the decision-trail step format, and the traced node wrapper."""
import inspect
import json
import re
import time
from typing import Any, Optional

import mlflow
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt
from mlflow.entities import SpanType

from agent_server.memory import Memory

TAGS = ["news", "local", "weather", "sports", "live", "entertainment", "drama", "comedy", "family",
        "late_night", "lifestyle", "court", "talk", "game_show", "celebrity"]
DAYPARTS = ["Early Morning", "Daytime", "Early Fringe", "Prime Access", "Prime", "Late Fringe"]
SPOT_RE = re.compile(r"SPT-\d{7}", re.I)
AVAIL_RE = re.compile(r"AVL-\d{6}")
ADV_RE = re.compile(r"ADV-\d{4}")
RULE_LABELS = {
    "daypart_lower_value": "lower-value daypart", "rating_below_floor": "rating below 90% floor",
    "spot_length_mismatch": "different spot length", "outside_makegood_window": "outside make-good window",
    "after_flight_end": "after flight end", "already_aired": "date already passed", "avail_held": "avail on hold",
    "already_booked": "already booked", "contract_not_makegood_eligible": "contract not make-good eligible",
}


def _accumulate(old: Optional[list], new: Any) -> list:
    """Reducer: append new items (parallel branches merge cleanly); {"reset": [...]} starts a new request."""
    if isinstance(new, dict) and "reset" in new:
        return list(new["reset"])
    return list(old or []) + list(new or [])


def _step(node: str, title: str, detail: str = "", reset: bool = False, **data):
    item = {"node": node, "title": title, "detail": detail, "ts": round(time.time(), 3), **data}
    return {"reset": [item]} if reset else [item]


def _usage(node: str, msg) -> dict:
    u = getattr(msg, "usage_metadata", None) or {}
    return {"node": node, "input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0)}


def _last_human_text(state: dict) -> str:
    for m in reversed(state.get("messages") or []):
        if isinstance(m, HumanMessage):
            return m.content if isinstance(m.content, str) else " ".join(
                c.get("text", "") for c in m.content if isinstance(c, dict))
    return ""


def _text(content) -> str:
    """User-facing text of an LLM message. Reasoning models return content blocks ([reasoning, text]), sometimes
    serialized as a JSON string; keep only the text blocks."""
    if isinstance(content, str):
        stripped = content.lstrip()
        if not (stripped.startswith("[{") and '"type"' in stripped):
            return content
        try:
            content = json.loads(stripped)
        except ValueError:
            return content
    return "".join(c if isinstance(c, str) else c.get("text", "") for c in content or []
                   if isinstance(c, str) or (isinstance(c, dict) and c.get("type") in ("text", "output_text")))


def _live(state: dict, memory: Optional[Memory]) -> bool:
    return memory is not None and not (state.get("custom_inputs") or {}).get("eval")


def _fmt_rules(summary: dict[str, int]) -> str:
    fails = sorted(((k, v) for k, v in summary.items() if k not in ("evaluated", "commensurate")), key=lambda kv: -kv[1])
    return ", ".join(f"{RULE_LABELS.get(k, k)} {v}" for k, v in fails[:4])


_SPAN_INPUT_KEYS = ("intent", "spot_id", "as_of", "constraints", "prefs", "review")


def traced_node(fn):
    """Run a graph node inside its own CHAIN span. A human-review pause is recorded as such, not as an error."""
    takes_config = "config" in inspect.signature(fn).parameters

    async def node(state: dict, config: RunnableConfig):
        paused = None
        with mlflow.start_span(name=fn.__name__, span_type=SpanType.CHAIN) as span:
            span.set_inputs({k: state.get(k) for k in _SPAN_INPUT_KEYS if state.get(k)})
            try:
                out = await (fn(state, config) if takes_config else fn(state))
                span.set_outputs({k: v for k, v in (out or {}).items() if k != "messages"}
                                 | ({"message": _text(out["messages"][-1].content)} if (out or {}).get("messages") else {}))
            except GraphInterrupt as gi:
                span.set_attribute("graph.paused_for", fn.__name__)
                span.set_outputs({"status": f"paused at {fn.__name__}: waiting for a person (or the client's reply)"})
                paused = gi
        if paused:
            raise paused
        return out

    node.__name__ = fn.__name__
    return node


def log_human_feedback(trace_id: Optional[str], name: str, value: Any, source_id: Optional[str],
                       rationale: Optional[str] = None) -> None:
    """Attach a person's decision (manager, client, seller) to the recommendation's trace: production feedback that
    later becomes evaluation labels."""
    if not trace_id:
        return
    from mlflow.entities import AssessmentSource, AssessmentSourceType

    try:
        mlflow.log_feedback(trace_id=trace_id, name=name, value=value, rationale=rationale,
                            source=AssessmentSource(source_type=AssessmentSourceType.HUMAN, source_id=source_id or "unknown"))
    except Exception:  # noqa: BLE001 - feedback is best effort; never fail the workflow on it
        import logging
        logging.getLogger(__name__).warning("could not log %s feedback to trace %s", name, trace_id, exc_info=True)
