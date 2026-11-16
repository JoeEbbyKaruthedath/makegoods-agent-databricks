"""Client email for make-good offers: send an offer, then read the client's reply.

    base.py       the adapter interface (OutboundMessage, InboundMessage, MailAdapter)
    gmail_api.py  Gmail over HTTPS with an OAuth refresh token (MG_MAIL_MODE=gmail)
    sandbox.py    no real mail: messages stay in Lakebase and the app's client inbox page (MG_MAIL_MODE=sandbox)
    policy.py     who may receive mail (allowlist + redirect of demo addresses), send rate, reference tokens
    matching.py   which offer a reply belongs to, and the reply's own text (quoted history removed)

A Microsoft Graph adapter (Outlook / Teams) implements the same interface.
"""
import os

from agent_server.mail.base import InboundMessage, MailAdapter, OutboundMessage


def mail_mode() -> str:
    return os.getenv("MG_MAIL_MODE", "sandbox").lower()


def get_adapter(memory=None) -> MailAdapter:
    """The adapter for MG_MAIL_MODE (gmail | sandbox)."""
    mode = mail_mode()
    if mode == "gmail":
        from agent_server.mail.gmail_api import GmailAdapter
        return GmailAdapter.from_env()
    from agent_server.mail.sandbox import SandboxAdapter
    return SandboxAdapter(memory)


__all__ = ["InboundMessage", "MailAdapter", "OutboundMessage", "get_adapter", "mail_mode"]
