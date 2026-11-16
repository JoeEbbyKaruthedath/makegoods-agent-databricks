"""Save a finished Databricks job run as a Jupyter notebook with every cell's output, as plain text.

  python tools/export_run_notebook.py <job run id> <out dir> --profile <profile>

Writes one <task_key>.ipynb per notebook task of the run (markdown cells as markdown, code cells with their text,
table and error outputs), plus <task_key>.txt with the same content for reading without a notebook viewer.
Pass the files through tools/redact_evidence.py before committing them.
"""
import argparse
import base64
import json
import re
import urllib.parse
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import ViewsToExport

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def text_of(s: str) -> str:
    s = ANSI.sub("", s or "")
    return "\n".join([p for p in line.split("\r") if p.strip()][-1] if "\r" in line and line.strip() else line
                     for line in s.split("\n"))


def table_text(data: list, schema: list) -> str:
    cols = [c.get("name", "") for c in schema or []]
    rows = [" | ".join(cols)] if cols else []
    rows += [" | ".join("" if v is None else str(v) for v in r) for r in (data or [])[:50]]
    return "\n".join(rows)


def outputs(results: dict | None) -> list[str]:
    if not results:
        return []
    items = results.get("data") if results.get("type") == "listResults" else [results]
    out = []
    for it in items or []:
        kind, data = it.get("type"), it.get("data")
        if kind in ("ansi", "text", "raw"):
            out.append(text_of(data if isinstance(data, str) else json.dumps(data)))
        elif kind == "mimeBundle" and isinstance(data, dict):
            out.append(text_of(data.get("text/plain") or ""))
        elif kind == "table":
            out.append(table_text(data, it.get("schema")))
        elif kind == "error":
            out.append(text_of(str(data)))
    if results.get("cause"):
        out.append(text_of(results["cause"]))
    return [o for o in out if o.strip()]


def notebook(model: dict) -> tuple[dict, str]:
    cells, txt = [], []
    for c in sorted(model["commands"], key=lambda c: c.get("position", 0)):
        src = c.get("command") or ""
        if src.lstrip().startswith("%md"):
            md = re.sub(r"^\s*%md\s?", "", src, count=1)
            cells.append({"cell_type": "markdown", "metadata": {}, "source": md.splitlines(keepends=True)})
            txt.append(md)
            continue
        outs = outputs(c.get("results"))
        cells.append({"cell_type": "code", "metadata": {}, "execution_count": None, "source": src.splitlines(keepends=True),
                      "outputs": [{"output_type": "stream", "name": "stdout", "text": o.splitlines(keepends=True)} for o in outs]})
        txt.append("```python\n" + src + "\n```\n" + ("\n".join("Output:\n" + o for o in outs) if outs else "(no output)"))
    nb = {"nbformat": 4, "nbformat_minor": 5, "cells": cells,
          "metadata": {"language_info": {"name": "python"}, "databricks": {"notebook_name": model.get("name")}}}
    return nb, "\n\n".join(txt) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id", type=int)
    ap.add_argument("out_dir")
    ap.add_argument("--profile")
    a = ap.parse_args()
    w = WorkspaceClient(profile=a.profile)
    run = w.jobs.get_run(a.run_id)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for t in run.tasks or [run]:
        if not getattr(t, "notebook_task", None):
            continue
        view = w.jobs.export_run(t.run_id, views_to_export=ViewsToExport.CODE).views[0]
        m = re.search(r"__DATABRICKS_NOTEBOOK_MODEL = '([^']+)'", view.content)
        model = json.loads(urllib.parse.unquote(base64.b64decode(m.group(1)).decode()))
        nb, txt = notebook(model)
        key = getattr(t, "task_key", None) or "notebook"
        state = t.state.result_state.value if t.state and t.state.result_state else "?"
        header = f"# Job run {a.run_id}, task {key}: {state}\n\n"
        (out / f"{key}.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False))
        (out / f"{key}.txt").write_text(header + txt)
        print(f"{key}: {state}, {len(nb['cells'])} cells -> {out}/{key}.ipynb")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
