"""End-to-end human-in-the-loop test against Lakebase (local dev branch).

1. Hero spot 1 -> recommendation, graph pauses for review
2. Reviewer picks the sports-adjacent option instead (modify) -> decision logged, preferences learned
3. Hero spot 2 (same advertiser) -> sports adjacency now ranks first
4. Refinement while paused ("avoid news") and an open question
"""
import asyncio
import json
import os
import sys
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv(".env", override=True)

import mlflow  # noqa: E402
from mlflow.types.responses import ResponsesAgentRequest  # noqa: E402

from agent_server.agent import run_agent, set_memory  # noqa: E402
from agent_server.memory import Memory  # noqa: E402

mlflow.set_experiment(experiment_id=os.environ["MLFLOW_EXPERIMENT_ID"])


async def ask(text, thread_id, **ci):
    req = ResponsesAgentRequest(input=[{"role": "user", "content": text}], custom_inputs={"thread_id": thread_id, **ci})
    out = {}
    with mlflow.start_span(name="hitl_test_request"):
        async for kind, value in run_agent(req):
            if kind == "text":
                print(value.split("\n|")[0][:600])
            elif kind == "final":
                out = value
    print(f"  -> status={out['status']} outcome={out['outcome']} top={out['recommended_avail_id']} "
          f"llm_calls={out['llm_calls']} tokens={out['llm_tokens']}")
    for r in out.get("ranked") or []:
        print(f"     #{r['rank']} {r['avail_id']} {r['program_name']:<28} {r['air_date']} score={r['score']:.3f} {r['adjacency_tags']}")
    return out


async def main():
    async with Memory() as memory:
        set_memory(memory)
        t1 = f"test-{uuid4().hex[:8]}"
        print("\n## 1. Hero spot 1 (UI button)")
        o = await ask("Find a commensurate make-good for SPT-0058222", t1, spot_id="SPT-0058222", mode="find")
        sports = next(r["avail_id"] for r in o["ranked"] if "sports" in r["adjacency_tags"])
        print(f"\n## 2. Reviewer chooses the sports option {sports} instead (modify)")
        await ask("", t1, review={"action": "modify", "avail_id": sports, "reviewer": "adops-reviewer",
                                  "reason": "Ironwood always wants sports adjacency"})
        print("   preference evidence now:", json.dumps(await memory.pref_evidence("ADV-0151")))

        print("\n## 3. Hero spot 2, same advertiser, new conversation")
        t2 = f"test-{uuid4().hex[:8]}"
        await ask("SPT-0058223 got bumped by the NFL overrun on KPBT. What's the best make-good?", t2)

        print("\n## 4. Refinement while paused for review")
        await ask("avoid news adjacency and keep it in Prime", t2)

        print("\n## 5. Open question")
        await ask("Which markets have the most make-good revenue at risk right now?", f"test-{uuid4().hex[:8]}")

        print("\n## decision log (latest):")
        for d in (await memory.list_decisions(3)):
            print("  ", {k: d[k] for k in ("spot_id", "action", "outcome", "final_avail_id", "reviewer", "pref_updates")})
        print("## pending reviews:", [r["spot_id"] for r in await memory.list_reviews()])
    mlflow.flush_trace_async_logging()


asyncio.run(main())
