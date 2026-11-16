"""Gmail over HTTPS (REST API) with an OAuth refresh token.

Credentials come from the mail secret scope (MG_MAIL_SECRET_SCOPE, read as the app itself) or env vars:
  gmail_client_id, gmail_client_secret, gmail_refresh_token   OAuth client + refresh token (send + read scopes)
  gmail_sender                                                address the offers are sent from
  gmail_quota_project (optional)                              sent as x-goog-user-project

Reading is scoped to threads the app started: fetch_replies calls threads.get for those thread ids only. The
mailbox is never listed or searched.
"""
import asyncio
import base64
import logging
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parsedate_to_datetime

import requests

from agent_server.mail.base import InboundMessage, OutboundMessage, SentMessage
from agent_server.mail.matching import own_text
from agent_server.mail.policy import secret_value

logger = logging.getLogger(__name__)
API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class GmailAdapter:
    name = "gmail"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, sender: str, quota_project: str = ""):
        self._cid, self._secret, self._refresh = client_id, client_secret, refresh_token
        self.sender, self._quota = sender, quota_project
        self._token, self._expires = "", 0.0

    @classmethod
    def from_env(cls) -> "GmailAdapter":
        keys = ("gmail_client_id", "gmail_client_secret", "gmail_refresh_token", "gmail_sender")
        v = {k: secret_value(k) for k in keys + ("gmail_quota_project",)}
        missing = [k for k in keys if not v[k]]
        if missing:
            raise RuntimeError(f"Gmail mail mode needs {', '.join(missing)} in the mail secret scope")
        return cls(v["gmail_client_id"], v["gmail_client_secret"], v["gmail_refresh_token"], v["gmail_sender"],
                   v["gmail_quota_project"])

    def _headers(self) -> dict:
        if time.time() > self._expires - 60:
            r = requests.post(TOKEN_URL, data={"client_id": self._cid, "client_secret": self._secret,
                                               "refresh_token": self._refresh, "grant_type": "refresh_token"}, timeout=20)
            r.raise_for_status()
            body = r.json()
            self._token, self._expires = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        h = {"Authorization": f"Bearer {self._token}"}
        if self._quota:
            h["x-goog-user-project"] = self._quota
        return h

    async def send(self, msg: OutboundMessage) -> SentMessage:
        return await asyncio.to_thread(self._send, msg)

    async def fetch_replies(self, thread_ids: list[str], known_ids: set[str]) -> list[InboundMessage]:
        return await asyncio.to_thread(self._fetch, thread_ids, known_ids)

    def _send(self, msg: OutboundMessage) -> SentMessage:
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = self.sender, msg.to, msg.subject
        if msg.cc:
            m["Cc"] = ", ".join(msg.cc)
        if msg.reply_to:
            m["Reply-To"] = msg.reply_to
        m["Date"], m["Message-ID"] = formatdate(localtime=True), make_msgid(domain=self.sender.split("@")[-1])
        m["X-MakeGood-Ref"] = msg.ref
        m.set_content(msg.text)
        m.add_alternative(msg.html, subtype="html")
        raw = base64.urlsafe_b64encode(m.as_bytes()).decode()
        r = requests.post(f"{API}/messages/send", headers=self._headers(), json={"raw": raw}, timeout=30)
        r.raise_for_status()
        body = r.json()
        return SentMessage(message_id=body["id"], thread_id=body["threadId"], sent_to=msg.to)

    def _fetch(self, thread_ids: list[str], known_ids: set[str]) -> list[InboundMessage]:
        out = []
        for tid in thread_ids:
            r = requests.get(f"{API}/threads/{tid}", headers=self._headers(), params={"format": "full"}, timeout=30)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            for m in r.json().get("messages", []):
                if m["id"] in known_ids or "SENT" in m.get("labelIds", []):
                    continue
                headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
                if self.sender.lower() in headers.get("from", "").lower():
                    continue
                text, is_html = _body(m.get("payload", {}))
                try:
                    received = parsedate_to_datetime(headers.get("date", "")).isoformat()
                except (TypeError, ValueError):
                    received = time.strftime("%Y-%m-%dT%H:%M:%S")
                out.append(InboundMessage(message_id=m["id"], thread_id=tid, from_addr=headers.get("from", ""),
                                          subject=headers.get("subject", ""), text=own_text(text, is_html),
                                          received_at=received, in_reply_to=headers.get("in-reply-to")))
        return out


def _body(payload: dict) -> tuple[str, bool]:
    """Plain-text part if there is one, else the HTML part."""
    parts, plain, html = [payload], None, None
    while parts:
        p = parts.pop()
        parts.extend(p.get("parts", []) or [])
        data = (p.get("body") or {}).get("data")
        if not data:
            continue
        text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
        if p.get("mimeType") == "text/plain" and plain is None:
            plain = text
        elif p.get("mimeType") == "text/html" and html is None:
            html = text
    return (plain, False) if plain is not None else (html or "", True)
