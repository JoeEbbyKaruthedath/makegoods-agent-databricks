"""Call Unity Catalog functions from the agent.

Every business rule lives in a UC function (deterministic SQL, governed by UC grants and
lineage). This module executes them on a SQL warehouse and records each call as a TOOL span
in the MLflow trace: function, arguments, row count, latency.

Identity: in the app, every request runs as the signed-in person. The app sets their token with
`acting_as(token, email)`; ws() then returns a client for that person, so Unity Catalog row
filters decide what the rule functions, queries and analytics can see. With MG_REQUIRE_USER=true
(the app) a call without a person's token fails instead of falling back to the app's own identity.
Writes the app makes on its own behalf (decision events, rule mirror) use sp_ws() explicitly.
Notebooks, jobs and evaluation run as their own identity (no person token).
"""
import asyncio
import contextvars
import hashlib
import json
import os
import re
import time
from datetime import date
from typing import Any, Optional

import mlflow
from databricks.sdk import WorkspaceClient
from databricks.sdk.config import Config
from databricks.sdk.service.sql import StatementParameterListItem, StatementState
from mlflow.entities import SpanType

from agent_server.config import WAREHOUSE_ID, fq

_ws: WorkspaceClient | None = None
_user_clients: dict[str, WorkspaceClient] = {}
_SAFE_TOKEN = re.compile(r"^[^\x00-\x1f;]{0,200}$")  # no control chars / statement separators

# the person a request runs as (set by the app per request; empty outside the app)
_user_token: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("mg_user_token", default=None)
_user_email: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("mg_user_email", default=None)


def require_user() -> bool:
    return os.getenv("MG_REQUIRE_USER", "false").lower() == "true"


def acting_as(token: Optional[str], email: Optional[str]) -> None:
    """Run the rest of this request (and the tasks it starts) as this person."""
    _user_token.set(token)
    _user_email.set(email)


def current_user() -> Optional[str]:
    return _user_email.get()


def sp_ws() -> WorkspaceClient:
    """The app's (or job's) own identity."""
    global _ws
    if _ws is None:
        _ws = WorkspaceClient()
    return _ws


def ws() -> WorkspaceClient:
    """Client for the person this request runs as; the process identity outside the app."""
    token = _user_token.get()
    if not token:
        if require_user():
            raise PermissionError("no signed-in user for this request")
        return sp_ws()
    key = hashlib.sha256(token.encode()).hexdigest()
    client = _user_clients.get(key)
    if client is None:
        if len(_user_clients) > 256:
            _user_clients.clear()
        client = WorkspaceClient(config=Config(host=sp_ws().config.host, token=token, auth_type="pat"))
        _user_clients[key] = client
    return client


def _str(v: str) -> str:
    if not _SAFE_TOKEN.match(v):
        raise ValueError(f"unsafe argument value: {v!r}")
    return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"


def sql_literal(v: Any) -> str:
    """Render a Python value as a Spark SQL literal (function args can't be bound as typed arrays/maps)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return f"CAST({v!r} AS DOUBLE)"
    if isinstance(v, date):
        return f"DATE'{v.isoformat()}'"
    if isinstance(v, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            return f"DATE'{v}'"
        return _str(v)
    if isinstance(v, (list, tuple)):
        return "CAST(array(" + ", ".join(_str(str(x)) for x in v) + ") AS ARRAY<STRING>)"
    if isinstance(v, dict):
        if not v:
            return "CAST(map() AS MAP<STRING, DOUBLE>)"
        return "map(" + ", ".join(f"{_str(str(k))}, CAST({float(x)!r} AS DOUBLE)" for k, x in v.items()) + ")"
    raise TypeError(f"unsupported argument type: {type(v)}")


def _split_top(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        depth += ch == "<"
        depth -= ch == ">"
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    return out + ([cur.strip()] if cur.strip() else [])


def _coerce(v: Any, type_text: str) -> Any:
    """Nested values come back as JSON strings ("true", "65268"); give them their declared type."""
    if v is None:
        return None
    t = type_text.strip()
    u = t.upper()
    if u.startswith("ARRAY<"):
        inner = t[6:-1]
        return [_coerce(x, inner) for x in v] if isinstance(v, list) else v
    if u.startswith("STRUCT<"):
        fields = {}
        for f in _split_top(t[7:-1]):
            name, _, ftype = f.partition(":")
            fields[name.strip().strip("`")] = ftype.strip().removesuffix(" NOT NULL")
        return {k: _coerce(x, fields.get(k, "STRING")) for k, x in v.items()} if isinstance(v, dict) else v
    if u.startswith("MAP<"):
        _, vt = _split_top(t[4:-1])
        return {k: _coerce(x, vt) for k, x in v.items()} if isinstance(v, dict) else v
    if isinstance(v, str):
        if u in ("INT", "BIGINT", "SMALLINT", "TINYINT", "LONG", "SHORT", "BYTE"):
            return int(v)
        if u in ("DOUBLE", "FLOAT") or u.startswith("DECIMAL"):
            return float(v)
        if u == "BOOLEAN":
            return v.lower() == "true"
    return v


def _convert(value: str | None, type_name: str, type_text: str = ""):
    if value is None:
        return None
    t = type_name.upper()
    if t in ("INT", "BIGINT", "SHORT", "LONG", "BYTE"):
        return int(value)
    if t in ("DOUBLE", "FLOAT", "DECIMAL"):
        return float(value)
    if t == "BOOLEAN":
        return value.lower() == "true"
    if t in ("ARRAY", "STRUCT", "MAP"):
        return _coerce(json.loads(value), type_text) if type_text else json.loads(value)
    return value


def _execute(sql: str, params: Optional[dict[str, Any]] = None, client: Optional[WorkspaceClient] = None) -> list[dict]:
    """Run one statement as the current person (or `client`). `params` are bound as :name markers."""
    w = client or ws()
    bound = [StatementParameterListItem(name=k, value=None if v is None else str(v)) for k, v in (params or {}).items()]
    r = w.statement_execution.execute_statement(statement=sql, warehouse_id=WAREHOUSE_ID, wait_timeout="50s",
                                                parameters=bound or None)
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(0.3)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(r.status.error.message if r.status.error else str(r.status.state))
    cols = [(c.name, c.type_name.value if c.type_name else "STRING", c.type_text or "") for c in r.manifest.schema.columns]
    rows = (r.result.data_array or []) if r.result else []
    return [{n: _convert(v, t, tt) for (n, t, tt), v in zip(cols, row)} for row in rows]


async def call_uc(function: str, **args: Any) -> list[dict]:
    """Run `SELECT * FROM <catalog>.<schema>.<function>(arg => value, ...)` and return rows as dicts."""
    named = ", ".join(f"{k} => {sql_literal(v)}" for k, v in args.items() if v is not None)
    sql = f"SELECT * FROM {fq(function)}({named})"
    with mlflow.start_span(name=f"uc.{function}", span_type=SpanType.TOOL) as span:
        span.set_inputs({"function": fq(function), **{k: v for k, v in args.items() if v is not None}})
        t0 = time.perf_counter()
        rows = await asyncio.to_thread(_execute, sql)
        span.set_attributes({"uc.function": fq(function), "uc.rows": len(rows),
                             "uc.latency_ms": round((time.perf_counter() - t0) * 1000)})
        span.set_outputs(rows if len(rows) <= 25 else {"rows": len(rows), "first_25": rows[:25]})
    return rows


async def query(sql: str, name: str, params: Optional[dict[str, Any]] = None) -> list[dict]:
    """Read-only SQL for app plumbing (baseline preferences, queue view), traced as a RETRIEVER span."""
    with mlflow.start_span(name=name, span_type=SpanType.RETRIEVER) as span:
        span.set_inputs({"sql": sql, **({"params": params} if params else {})})
        rows = await asyncio.to_thread(_execute, sql, params)
        span.set_outputs({"rows": len(rows)})
    return rows


async def query_as_app(sql: str, name: str, params: Optional[dict[str, Any]] = None) -> list[dict]:
    """A read the app makes as itself (client emails it stored), traced as a TOOL span."""
    with mlflow.start_span(name=name, span_type=SpanType.TOOL) as span:
        span.set_inputs({"sql": sql, **({"params": params} if params else {})})
        rows = await asyncio.to_thread(_execute, sql, params, sp_ws())
        span.set_outputs(rows if len(rows) <= 25 else {"rows": len(rows)})
    return rows


async def write_as_app(sql: str, params: Optional[dict[str, Any]] = None) -> None:
    """A write the app makes on its own behalf (decision events, client-rule mirror), never as the person."""
    await asyncio.to_thread(_execute, sql, params, sp_ws())
