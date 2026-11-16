"""Publish the agent's prompts to the MLflow Prompt Registry (Unity Catalog) and point alias "production" at them.

Re-run after editing a template in agent_server/prompts.py: a changed template becomes a new version
and "production" moves to it, so the running app picks it up within 5 minutes (no redeploy) and
every trace records the version it used.

  python scripts/register_prompts.py [--alias production] [--message "why this changed"]
"""
import argparse

from dotenv import load_dotenv

load_dotenv(".env", override=True)

import mlflow  # noqa: E402

from agent_server.prompts import DEFAULTS  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--alias", default="production")
ap.add_argument("--message", default="make-goods agent prompt")
args = ap.parse_args()

for name, template in DEFAULTS.items():
    try:
        latest = mlflow.genai.load_prompt(f"prompts:/{name}@{args.alias}")
    except Exception:
        latest = None
    if latest is not None and latest.template == template:
        print(f"{name}: unchanged (v{latest.version})")
        continue
    p = mlflow.genai.register_prompt(name=name, template=template, commit_message=args.message,
                                     tags={"agent": "makegoods-agent", "framework": "langgraph"})
    mlflow.genai.set_prompt_alias(name=name, alias=args.alias, version=p.version)
    print(f"{name}: registered v{p.version} -> @{args.alias}")
