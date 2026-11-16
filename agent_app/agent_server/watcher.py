"""Reply watcher: picks up clients' email replies and resumes the waiting make-good threads.

Runs inside the app (started from its lifespan) every MG_MAIL_POLL_SECONDS (default 60), and on demand from the
"Check replies" button. For each offer that was sent and is waiting for the client it:
  1. fetches new messages on the offer's mail thread only (never lists or searches the mailbox),
  2. records each message once (mg_mail_log, keyed by message id),
  3. takes a lock on the make-good thread, checks the thread is still waiting for the client,
  4. resumes it with the reply: the graph reads the reply (parse_client_reply) and pauses for the seller to confirm.
It runs as the app itself and reaches only steps that read no client data from Unity Catalog.
"""
import asyncio
import logging
import os
from typing import Optional

from mlflow.types.responses import ResponsesAgentRequest

from agent_server.mail import get_adapter, mail_mode
from agent_server.mail.matching import ref_from_subject
from agent_server.mail.policy import verify_token
from agent_server.memory import Memory

logger = logging.getLogger(__name__)


async def check_replies(memory: Memory, run_agent, offer_ids: Optional[set[str]] = None) -> list[dict]:
    """One pass. Returns what was picked up: [{offer_id, thread_id, message_id, resumed, why}]."""
    offers = [o for o in await memory.offers_awaiting_client() if not offer_ids or o["offer_id"] in offer_ids]
    if not offers:
        return []
    by_mail_thread = {o["mail_thread_id"]: o for o in offers}
    adapter = get_adapter(memory)
    known = await memory.processed_message_ids(list(by_mail_thread))
    out = []
    for msg in await adapter.fetch_replies(list(by_mail_thread), known):
        offer = by_mail_thread.get(msg.thread_id)
        if not offer:
            continue
        token = ref_from_subject(msg.subject)
        matched_by = "mail thread" + (" + reference" if token and verify_token(token, offer["thread_id"]) else "")
        await memory.log_mail({"message_id": msg.message_id, "offer_id": offer["offer_id"], "thread_id": offer["thread_id"],
                               "mail_thread_id": msg.thread_id, "direction": "in", "from_addr": msg.from_addr,
                               "to_addr": offer.get("sent_to"), "subject": msg.subject, "body_text": msg.text,
                               "matched_by": matched_by})
        item = {"offer_id": offer["offer_id"], "thread_id": offer["thread_id"], "message_id": msg.message_id, "resumed": False}
        async with memory.thread_lock(offer["thread_id"]) as got:
            if not got:
                item["why"] = "another worker is handling this thread"
                out.append(item)
                continue
            req = ResponsesAgentRequest(input=[{"role": "user", "content": ""}], custom_inputs={
                "thread_id": offer["thread_id"],
                "review": {"action": "client_reply", "text": msg.text, "from": msg.from_addr, "subject": msg.subject,
                           "message_id": msg.message_id, "received_at": msg.received_at, "reviewer": "client"}})
            try:
                async for kind, value in run_agent(req):
                    if kind == "final":
                        item["resumed"] = (value.get("pending") or {}).get("type") == "seller_confirm"
                        item["why"] = None if item["resumed"] else f"thread is at {value.get('stage')}"
            except Exception as e:  # noqa: BLE001 - one bad reply must not stop the others
                logger.exception("could not resume %s with reply %s", offer["thread_id"], msg.message_id)
                item["why"] = str(e)
        await memory.mark_mail_processed(msg.message_id)
        out.append(item)
    return out


async def run_forever(memory: Memory, run_agent) -> None:
    """Background loop for the app's lifespan (off when MG_MAIL_MODE=off)."""
    if mail_mode() == "off":
        return
    every = int(os.getenv("MG_MAIL_POLL_SECONDS", "60"))
    logger.info("reply watcher on (%s mail, every %ss)", mail_mode(), every)
    while True:
        try:
            picked = await check_replies(memory, run_agent)
            if picked:
                logger.info("reply watcher: %s", picked)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep watching
            logger.exception("reply watcher pass failed")
        await asyncio.sleep(every)
