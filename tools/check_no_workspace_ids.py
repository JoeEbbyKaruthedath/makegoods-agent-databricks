"""Pre-commit check: block staged changes that contain workspace-specific values.

The repo ships with YOUR_... placeholders. Real values (catalog, warehouse, experiment, Genie space, workspace
host) live only in the git-ignored .databricks/bundle/<target>/variable-overrides.json files. This check reads
those values and fails the commit when any added line contains one of them, a workspace host name or a
personal access token. It also blocks the terms listed, one per line, in the git-ignored
.databricks/blocked-terms.txt (case-insensitive), e.g. names that must not appear in the repo.

Install once per clone:
  printf '#!/bin/sh\nexec python3 tools/check_no_workspace_ids.py\n' > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = [
    re.compile(r"\b[a-z0-9-]+\.cloud\.databricks\.com\b"),
    re.compile(r"\b[a-z0-9-]+\.azuredatabricks\.net\b"),
    re.compile(r"\bdapi[0-9a-f]{32}\b"),
]


# overrides that are plain settings, not workspace identifiers
SETTINGS = {"schema", "app_name", "lakebase_branch", "mail_mode", "mail_secret_scope"}


def override_values() -> set[str]:
    values: set[str] = set()
    for f in ROOT.glob(".databricks/bundle/*/variable-overrides.json"):
        for k, v in json.loads(f.read_text()).items():
            if k not in SETTINGS and isinstance(v, str) and len(v) >= 6 and not v.startswith("YOUR_") and v.lower() != "none":
                values.add(v)
    return values


def blocked_terms() -> list[str]:
    f = ROOT / ".databricks" / "blocked-terms.txt"
    if not f.exists():
        return []
    return [t.strip() for t in f.read_text().splitlines() if t.strip() and not t.startswith("#")]


def main() -> int:
    diff = subprocess.run(["git", "diff", "--cached", "-U0", "--no-color"], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout
    values = override_values()
    terms = blocked_terms()
    hits, path = [], None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        found = [v for v in values if v in added] + [m.group(0) for p in PATTERNS for m in p.finditer(added)] \
            + [t for t in terms if t.lower() in added.lower()]
        if found:
            hits.append((path, found))
    if not hits:
        return 0
    print("Commit blocked: staged changes contain workspace-specific values or blocked terms (keep them in the "
          "git-ignored overrides):")
    for path, found in hits:
        print(f"  {path}: {', '.join(sorted(set(found)))}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
