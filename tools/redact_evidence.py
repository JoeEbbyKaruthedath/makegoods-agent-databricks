"""Copy run logs into evidence/ with workspace-specific values replaced by placeholders.

  python tools/redact_evidence.py <source file> <evidence/name> [--target v2] [--profile <profile>]

Replaced (values read from the git-ignored .databricks/bundle/<target>/variable-overrides.json, the CLI profile and
.databricks/blocked-terms.txt, so none of them is written into the repo):
  catalog, schema, app name, Lakebase project, app title, deploy folder, warehouse / experiment / Genie space ids
  -> <catalog>, <schema>, <app>, <lakebase-project>, Make-Goods, <bundle-folder>, <warehouse-id>, ...
  the workspace host, app URLs, the workspace id, the signed-in user's email and workspace user/principal ids
  blocked terms, tokens, and terminal color codes and progress-bar redraws
Prints how many values it replaced.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = {"catalog": "<catalog>", "schema": "<schema>", "app_name": "<app>", "lakebase_project": "<lakebase-project>",
               "app_title": "Make-Goods", "deploy_folder": "<bundle-folder>", "warehouse_id": "<warehouse-id>",
               "experiment_id": "<experiment-id>", "genie_space_id": "<genie-space-id>"}


def rules(target: str, profile: str | None) -> list[tuple[re.Pattern, str]]:
    out: list[tuple[str, str]] = []
    f = ROOT / ".databricks" / "bundle" / target / "variable-overrides.json"
    for k, v in (json.loads(f.read_text()) if f.exists() else {}).items():
        if k in PLACEHOLDER and isinstance(v, str) and v and v.lower() != "none":
            out.append((re.escape(v), PLACEHOLDER[k]))
    if profile:
        from databricks.sdk import WorkspaceClient

        w = WorkspaceClient(profile=profile)
        host = w.config.host.replace("https://", "").rstrip("/")
        out.append((re.escape(host), "<workspace-host>"))
        me = w.current_user.me()
        out.append((re.escape(me.user_name), "<user>"))
        if me.id:
            out.append((re.escape(str(me.id)), "<user-id>"))
        wid = w.get_workspace_id()
        out.append((re.escape(str(wid)), "<workspace-id>"))
    out += [(r"[a-z0-9-]+\.(?:aws\.|azure\.|gcp\.)?databricksapps\.com", "<app-url>"),
            (r"[a-z0-9-]+\.cloud\.databricks\.com", "<workspace-host>"),
            (r"/Users/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "/Users/<principal>"),
            (r"\bdapi[0-9a-f]{32}\b", "<token>")]
    terms = ROOT / ".databricks" / "blocked-terms.txt"
    for t in (terms.read_text().splitlines() if terms.exists() else []):
        t = t.strip()
        if t and not t.startswith("#"):
            out.append((rf"{re.escape(t)}[ _-]?", ""))
    # longest values first, so a schema name is replaced before a shorter folder name inside it
    out.sort(key=lambda r: -len(r[0]))
    return [(re.compile(p, re.I), r) for p, r in out]


def clean(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)  # terminal colors
    lines = []
    for line in text.split("\n"):
        if "\r" in line:  # progress bars redraw with carriage returns: keep the last state
            line = [p for p in line.split("\r") if p.strip()][-1] if line.strip() else ""
        lines.append(line.rstrip())
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("dest")
    ap.add_argument("--target", default="v2")
    ap.add_argument("--profile")
    a = ap.parse_args()
    text = clean(Path(a.source).read_text(errors="replace"))
    n = 0
    for pat, rep in rules(a.target, a.profile):
        text, k = pat.subn(rep, text)
        n += k
    dest = ROOT / a.dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    print(f"{a.dest}: {len(text.splitlines())} lines, {n} values replaced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
