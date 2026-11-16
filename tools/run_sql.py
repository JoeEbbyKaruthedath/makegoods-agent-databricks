"""Run a multi-statement SQL file on a SQL warehouse, in order.

Statements are split on a semicolon at end of line. `USE CATALOG` / `USE SCHEMA` set the
context for the statements that follow (the Statement Execution API is stateless).

The SQL files name the catalog YOUR_CATALOG and the schema YOUR_SCHEMA (plus
YOUR_SCHEMA_security for access control). --catalog / --schema replace those names when
the file is run, so the same files deploy to any target schema (e.g. makegoods_v2)
without being edited.

  python tools/run_sql.py uc_functions/00_config_and_core.sql --catalog <catalog> [--schema makegoods_v2] [--show]
  python tools/run_sql.py uc_functions/91_grants_v2.sql --catalog <catalog> --schema makegoods_v2 --param admin_user=you@company.com ...
"""
import argparse
import os
import re
import sys

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "YOUR_WAREHOUSE_ID")


def statements(sql: str):
    sql = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    return [s.strip() for s in re.split(r";[ \t]*\n", sql + "\n") if s.strip()]


def substitute(sql: str, catalog: str, schema: str, params: dict | None = None) -> str:
    """Point the file's catalog and schema names at the target ones, and fill {{name}} values from --param."""
    for k, v in (params or {}).items():
        sql = sql.replace("{{" + k + "}}", v)
    missing = sorted(set(re.findall(r"\{\{(\w+)\}\}", sql)))
    if missing:
        sys.exit(f"missing --param for: {', '.join(missing)}")
    sql = re.sub(r"\bYOUR_CATALOG\b", catalog, sql)
    sql = re.sub(r"\bYOUR_SCHEMA_security\b", f"{schema}_security", sql)
    return re.sub(r"\bYOUR_SCHEMA\b", schema, sql)


def run(w: WorkspaceClient, sql: str, catalog=None, schema=None, show=False, params=None):
    for stmt in statements(substitute(sql, catalog, schema, params)):
        m = re.match(r"USE\s+(CATALOG|SCHEMA)\s+(\S+)$", stmt, re.I)
        if m:
            if m.group(1).upper() == "CATALOG":
                catalog = m.group(2)
            else:
                schema = m.group(2)
            continue
        head = " ".join(stmt.split())[:90]
        r = w.statement_execution.execute_statement(
            statement=stmt, warehouse_id=WAREHOUSE_ID, catalog=catalog, schema=schema, wait_timeout="50s")
        while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
            r = w.statement_execution.get_statement(r.statement_id)
        if r.status.state != StatementState.SUCCEEDED:
            msg = r.status.error.message if r.status.error else str(r.status.state)
            print(f"FAILED: {head}\n  " + "\n  ".join(msg.strip().splitlines()[:4]))
            sys.exit(1)
        print(f"ok  {head}")
        if show and r.result and r.result.data_array:
            cols = [c.name for c in r.manifest.schema.columns]
            print("    " + " | ".join(cols))
            for row in r.result.data_array[:50]:
                print("    " + " | ".join("" if v is None else str(v) for v in row))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--show", action="store_true", help="print result rows")
    ap.add_argument("--catalog", default=os.environ.get("MG_CATALOG", "YOUR_CATALOG"))
    ap.add_argument("--schema", default=os.environ.get("MG_SCHEMA", "makegoods"))
    ap.add_argument("--param", action="append", default=[], help="name=value for {{name}} in the file (repeatable)")
    a = ap.parse_args()
    params = dict(kv.split("=", 1) for kv in a.param)
    client = WorkspaceClient(profile=os.environ.get("DATABRICKS_CONFIG_PROFILE", "DEFAULT"))
    run(client, open(a.file).read(), a.catalog, a.schema, a.show, params)
