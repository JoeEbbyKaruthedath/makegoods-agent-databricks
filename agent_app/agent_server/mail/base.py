"""Mail adapter interface: send one offer, fetch replies on the threads the app started."""
from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass
class OutboundMessage:
    to: str
    subject: str
    text: str
    html: str
    ref: str  # reference token, also in the subject as [MG-<ref>]
    cc: list[str] = field(default_factory=list)
    reply_to: Optional[str] = None


@dataclass
class InboundMessage:
    message_id: str
    thread_id: str  # the mail system's thread (conversation) id
    from_addr: str
    subject: str
    text: str
    received_at: str
    in_reply_to: Optional[str] = None


@dataclass
class SentMessage:
    message_id: str
    thread_id: str
    sent_to: str


class MailAdapter(Protocol):
    name: str

    async def send(self, msg: OutboundMessage) -> SentMessage:
        ...

    async def fetch_replies(self, thread_ids: list[str], known_ids: set[str]) -> list[InboundMessage]:
        """New messages from someone other than the app on these threads, excluding message ids already seen."""
        ...
