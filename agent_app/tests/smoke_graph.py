"""Smoke test: run the graph in eval mode (no Lakebase) on the hero spot and print the decision trail."""
import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv(".env", override=True)

import mlflow  # noqa: E402
from mlflow.types.responses import ResponsesAgentRequest  # noqa: E402

from agent_server.agent import run_agent  # noqa: E402

mlflow.set_experiment(experiment_id=os.environ["MLFLOW_EXPERIMENT_ID"])
MESSAGE = sys.argv[1] if len(sys.argv) > 1 else "Spot SPT-0058222 was preempted by the college football overrun. Find a commensurate make-good."


@mlflow.trace(name="smoke_test")
async def main():
    req = ResponsesAgentRequest(input=[{"role": "user", "content": MESSAGE}], custom_inputs={"eval": True})
    async for kind, value in run_agent(req):
        if kind == "step":
            for s in value:
                print(f"- [{s['node']}] {s['title']}: {s['detail']}")
        elif kind == "text":
            print("\n=== AGENT ===\n" + value)
        else:
            print(json.dumps({k: value[k] for k in ("status", "outcome", "recommended_avail_id", "llm_calls", "llm_tokens", "trace_id")}, default=str))


asyncio.run(main())
mlflow.flush_trace_async_logging()
