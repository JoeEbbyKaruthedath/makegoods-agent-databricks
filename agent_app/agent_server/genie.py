"""Open questions go to the "Make-Goods Analytics" Genie Space via the Genie Conversation API.

Genie answers from governed metric views (makegoods_*_metrics) and the rules config. Each call is a
TOOL span in the MLflow trace with the generated SQL; follow-ups reuse the Genie conversation.
"""
import asyncio
import os
from datetime import timedelta
from typing import Optional

import mlflow
from mlflow.entities import SpanType

from agent_server.uc_tools import ws

GENIE_SPACE_ID = os.getenv("MG_GENIE_SPACE_ID", "YOUR_GENIE_SPACE_ID")
MAX_ROWS = 25


def _ask(question: str, conversation_id: Optional[str]) -> dict:
    w = ws()
    if conversation_id:
        msg = w.genie.create_message_and_wait(GENIE_SPACE_ID, conversation_id, question, timeout=timedelta(minutes=4))
    else:
        msg = w.genie.start_conversation_and_wait(GENIE_SPACE_ID, question, timeout=timedelta(minutes=4))
    out = {"space_id": GENIE_SPACE_ID, "conversation_id": msg.conversation_id, "message_id": msg.message_id or msg.id,
           "status": msg.status.value if msg.status else None, "text": None, "sql": None, "description": None,
           "columns": [], "rows": [], "row_count": 0, "suggested_questions": [], "error": None}
    attachment_id = None
    for att in msg.attachments or []:
        if att.text and att.text.content:
            out["text"] = att.text.content
        if att.query and att.query.query:
            out["sql"], out["description"], attachment_id = att.query.query, att.query.description, att.attachment_id
        if att.suggested_questions and att.suggested_questions.questions:
            out["suggested_questions"] = list(att.suggested_questions.questions)
    if msg.error:
        out["error"] = msg.error.error
    if attachment_id:
        res = w.genie.get_message_attachment_query_result(GENIE_SPACE_ID, msg.conversation_id, out["message_id"], attachment_id)
        sr = res.statement_response
        if sr and sr.manifest and sr.manifest.schema:
            out["columns"] = [c.name for c in sr.manifest.schema.columns]
            rows = (sr.result.data_array if sr.result else None) or []
            out["row_count"] = sr.manifest.total_row_count or len(rows)
            out["rows"] = rows[:MAX_ROWS]
    return out


async def ask_genie(question: str, conversation_id: Optional[str] = None) -> dict:
    with mlflow.start_span(name="genie.ask", span_type=SpanType.TOOL) as span:
        span.set_inputs({"space_id": GENIE_SPACE_ID, "question": question, "conversation_id": conversation_id})
        out = await asyncio.to_thread(_ask, question, conversation_id)
        span.set_attributes({"genie.space_id": GENIE_SPACE_ID, "genie.conversation_id": out["conversation_id"] or "",
                             "genie.status": out["status"] or "", "genie.rows": out["row_count"]})
        span.set_outputs({k: out[k] for k in ("status", "text", "description", "sql", "columns", "row_count", "error")})
    return out


def _fmt(v: Optional[str]) -> str:
    if v is None:
        return ""
    try:
        f = float(v)
    except ValueError:
        return v.replace("T00:00:00.000Z", "").replace("|", "/")
    if f.is_integer() and abs(f) >= 1:
        return f"{int(f):,}"
    return f"{f:,.3f}".rstrip("0").rstrip(".") if abs(f) < 1 else f"{f:,.2f}"


def to_markdown(g: dict) -> str:
    """Genie's answer as chat markdown: summary text, result table, and the generated SQL (so users can trust it)."""
    parts = []
    if g.get("error") and not g.get("sql"):
        return f"Genie couldn't answer that: {g['error']}"
    if g.get("text"):
        parts.append(g["text"])
    elif g.get("description"):
        parts.append(g["description"])
    if g.get("columns"):
        head = "| " + " | ".join(g["columns"]) + " |"
        sep = "|" + "---|" * len(g["columns"])
        body = ["| " + " | ".join(_fmt(v) for v in row) + " |" for row in g["rows"]]
        parts.append("\n".join([head, sep] + body))
        if g["row_count"] > len(g["rows"]):
            parts.append(f"_Showing {len(g['rows'])} of {g['row_count']} rows._")
    if g.get("sql"):
        parts.append("```sql\n" + g["sql"].strip() + "\n```")
    return "\n\n".join(parts) or "Genie returned no answer."
