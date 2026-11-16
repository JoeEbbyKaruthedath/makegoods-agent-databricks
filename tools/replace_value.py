"""Replace a workspace-specific value (catalog, warehouse id, experiment id, ...) in every tracked file.

  python3 tools/replace_value.py <old> <new>

Plain-text replacement (no regex). Uses `git ls-files` when run inside a git checkout, otherwise walks
the tree and skips data/, virtualenvs and build output. README.md is left alone so its instructions keep
showing the original values.
"""
import os
import subprocess
import sys

SKIP_DIRS = {".git", ".venv", "data", ".databricks", "__pycache__", "node_modules"}
SKIP_FILES = {"README.md"}


def files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout.decode()
        return [p for p in out.split("\0") if p]
    except (subprocess.CalledProcessError, FileNotFoundError):
        found = []
        for root, dirs, names in os.walk("."):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            found += [os.path.join(root, n) for n in names]
        return found


def main() -> None:
    if len(sys.argv) != 3 or not sys.argv[1]:
        sys.exit(__doc__)
    old, new = sys.argv[1], sys.argv[2]
    changed = 0
    for path in files():
        if os.path.basename(path) in SKIP_FILES:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue  # binary or missing file
        if old in text:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text.replace(old, new))
            print(f"  {path}: {text.count(old)}")
            changed += 1
    print(f"{old} -> {new}: {changed} file(s) changed")


if __name__ == "__main__":
    main()
