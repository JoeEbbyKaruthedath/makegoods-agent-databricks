"""No real mail: offers and replies stay in Lakebase (mg_mail_log).

For rehearsals and for workspaces without a mailbox. The app's client inbox page shows each offer as the client
would receive it and lets you reply as the client; the reply watcher picks the reply up exactly as it would from
a real mailbox.
"""
import uuid

from agent_server.mail.base import InboundMessage, OutboundMessage, SentMessage


class SandboxAdapter:
    name = "sandbox"

    def __init__(self, memory):
        self.memory = memory

    async def send(self, msg: OutboundMessage) -> SentMessage:
        mail_thread = f"sbx-{uuid.uuid4().hex[:16]}"
        return SentMessage(message_id=f"sbx-msg-{uuid.uuid4().hex[:16]}", thread_id=mail_thread, sent_to=msg.to)

    async def fetch_replies(self, thread_ids: list[str], known_ids: set[str]) -> list[InboundMessage]:
        rows = await self.memory.sandbox_replies(thread_ids)
        return [InboundMessage(message_id=r["message_id"], thread_id=r["mail_thread_id"], from_addr=r["from_addr"],
                               subject=r["subject"] or "", text=r["body_text"] or "", received_at=str(r["logged_at"]))
                for r in rows if r["message_id"] not in known_ids]
