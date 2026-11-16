"""Validate the Genie Space through the Conversation API (same client the app uses)."""
import asyncio, sys, time
from dotenv import load_dotenv
load_dotenv(".env", override=True)
from agent_server.genie import ask_genie, to_markdown

QS = sys.argv[1:] or ["Which markets have the most make-good revenue at risk right now?",
                      "What are the most urgent make-goods in Nashville?",
                      "How did GPT assist compare with ad-ops experts on acceptance and refunds?"]

async def main():
    for q in QS:
        t = time.time()
        g = await ask_genie(q)
        print(f"\n### {q}  [{g['status']}, {time.time() - t:.0f}s, rows={g['row_count']}]")
        print(to_markdown(g)[:1400])

asyncio.run(main())
