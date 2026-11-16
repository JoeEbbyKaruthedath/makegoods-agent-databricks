"""Who may receive make-good mail, how often, and the reference token that ties a reply to its offer.

MG_MAIL_ALLOWED      comma-separated addresses or @domains that may receive mail (e.g. "you@example.org,@yourcompany.com")
MG_MAIL_REDIRECT_TO  demo inbox for the synthetic contacts: an address on *.example.com is delivered here instead,
                     plus-addressed per advertiser (you+ironwoodmotors@...), so one inbox can play every client
MG_MAIL_MAX_PER_HOUR send limit for this app (default 20)
MG_MAIL_REF_SECRET   key for reference tokens (or ref_secret in the mail secret scope, MG_MAIL_SECRET_SCOPE)

Anything not allowed is refused: the app never emails an address nobody approved.
"""
import base64
import hashlib
import hmac
import logging
import os
import re
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

_DEMO_DOMAIN = re.compile(r"@([a-z0-9-]+\.)*example\.com$", re.I)
_sent: deque[float] = deque()
_lock = threading.Lock()


class MailRefused(PermissionError):
    pass


def _setting(name: str) -> str:
    """A mail setting; "none" (the bundle default) means not set."""
    v = os.getenv(name, "").strip()
    return "" if v.lower() in ("none", "-") else v


def _allowed() -> list[str]:
    return [a.strip().lower() for a in _setting("MG_MAIL_ALLOWED").split(",") if a.strip()]


def is_allowed(addr: str) -> bool:
    a = addr.strip().lower()
    return any(a == x or (x.startswith("@") and a.endswith(x)) for x in _allowed())


def deliver_to(addr: str, tag: str = "") -> str:
    """The address a message is actually sent to: demo contacts go to the redirect inbox; anything else must be allowed."""
    redirect = _setting("MG_MAIL_REDIRECT_TO")
    if _DEMO_DOMAIN.search(addr):
        if not redirect:
            if os.getenv("MG_MAIL_MODE", "sandbox").lower() == "sandbox":
                return addr  # demo mail stays inside the app (sandbox adapter): nothing leaves it
            raise MailRefused(f"{addr} is a demo address and no demo inbox is configured")
        local, _, domain = redirect.partition("@")
        tag = re.sub(r"[^a-z0-9]", "", tag.lower())[:30]
        return f"{local}+{tag}@{domain}" if tag else redirect
    if not is_allowed(addr):
        raise MailRefused(f"{addr} is not on the allowed list")
    return addr


def same_sender(from_addr: str, sent_to: str) -> bool:
    """A reply counts as the client's when it comes from the address the offer went to (plus-tags ignored)."""
    def norm(a: str) -> str:
        m = re.search(r"<([^>]+)>", a)
        a = (m.group(1) if m else a).strip().lower()
        local, _, domain = a.partition("@")
        return local.split("+", 1)[0] + "@" + domain
    return norm(from_addr) == norm(sent_to)


def check_rate() -> None:
    limit = int(os.getenv("MG_MAIL_MAX_PER_HOUR", "20"))
    now = time.time()
    with _lock:
        while _sent and now - _sent[0] > 3600:
            _sent.popleft()
        if len(_sent) >= limit:
            raise MailRefused(f"send limit reached ({limit} per hour)")
        _sent.append(now)


def secret_value(key: str) -> str:
    """A mail credential: env var MG_<KEY>, else the key in the mail secret scope (read as the app itself)."""
    v = os.getenv(f"MG_{key.upper()}")
    scope = os.getenv("MG_MAIL_SECRET_SCOPE")
    if v or not scope:
        return v or ""
    try:
        from agent_server.uc_tools import sp_ws
        return base64.b64decode(sp_ws().secrets.get_secret(scope, key).value).decode()
    except Exception:  # noqa: BLE001 - missing secret: callers decide whether that is fatal
        logger.warning("mail secret %s/%s not readable", scope, key)
        return ""


_ref_key: list[bytes] = []


def _secret() -> bytes:
    if not _ref_key:
        s = secret_value("mail_ref_secret") or secret_value("ref_secret")
        if not s:
            logger.warning("MG_MAIL_REF_SECRET not set; reference tokens are derived without a key")
            s = "makegoods-reference"
        _ref_key.append(s.encode())
    return _ref_key[0]


def ref_token(thread_id: str) -> str:
    """Short, unguessable token for an offer thread (HMAC of the thread id)."""
    digest = hmac.new(_secret(), thread_id.encode(), hashlib.sha256).digest()
    return base64.b32encode(digest).decode()[:8]


def verify_token(token: str, thread_id: str) -> bool:
    return hmac.compare_digest(token.upper(), ref_token(thread_id))
