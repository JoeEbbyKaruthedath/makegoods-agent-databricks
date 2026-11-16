"""Who is signed in, what they may see, and what they may do.

The Databricks App runs with user authorization: every request carries the signed-in person's token
(x-forwarded-access-token) and email. The middleware makes the whole request run as that person (uc_tools.acting_as),
so Unity Catalog row filters decide what the queue, the agent's rule functions and the analytics return. Their role
and scope come from Unity Catalog too (mg_my_access, read as them). With MG_REQUIRE_USER=true a request without a
person's token is refused; local development (no proxy) runs as the CLI profile.
"""
import json
import time
from typing import Optional

from agent_server.config import CATALOG, SCHEMA
from agent_server.memory import Scope
from agent_server.uc_tools import _execute, acting_as, require_user
from starlette.responses import JSONResponse

SECURITY = f"{CATALOG}.{SCHEMA}_security"
_scopes: dict[str, tuple[float, Scope]] = {}
# which roles may answer each step a make-good thread can be paused at
ACT = {"make_good_review": {"seller", "manager", "admin"}, "manager_review": {"manager", "admin"},
       "confirm_send": {"seller", "manager", "admin"}, "await_client": {"seller", "manager", "admin"},
       "seller_confirm": {"seller", "manager", "admin"}, "confirm_rule": {"seller", "manager", "admin"}}
HUMAN_ACTIONS_BLOCKED = {"client_reply"}  # only the reply watcher answers for the client


class UserMiddleware:
    """Pure ASGI middleware (the request, its streamed response and the tasks it starts all see the person)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        token = headers.get("x-forwarded-access-token")
        email = headers.get("x-forwarded-email") or headers.get("x-forwarded-preferred-username")
        if not token and require_user() and scope.get("path") not in ("/health", "/favicon.ico"):
            return await JSONResponse({"detail": "sign in required"}, status_code=401)(scope, receive, send)
        acting_as(token, email or ("local-developer" if not require_user() else None))
        scope.setdefault("state", {})["user"] = {"token": token, "email": email}
        return await self.app(scope, receive, send)


def user_of(request) -> dict:
    return request.scope.get("state", {}).get("user") or {}


def scope_of(request, fresh: bool = False) -> Scope:
    """The signed-in person's role, seller id and markets (cached a minute; 'view as' changes refresh it)."""
    u = user_of(request)
    key = u.get("token") or "local"
    hit = _scopes.get(key)
    if hit and not fresh and time.time() - hit[0] < 60:
        return hit[1]
    try:
        rows = _execute(f"SELECT * FROM {SECURITY}.mg_my_access")
    except Exception:
        if require_user():
            raise
        rows = None  # local development against a schema without access control: see everything
    if rows is None:
        sc = Scope(role="admin", granted_role="admin", display_name="local developer")
    elif rows:
        r = rows[0]
        markets = r.get("markets") or []
        sc = Scope(role=r.get("role"), seller_id=r.get("seller_id"),
                   markets=json.loads(markets) if isinstance(markets, str) else list(markets or []),
                   principal=r.get("principal"), granted_role=r.get("granted_role"),
                   view_as_until=r.get("view_as_until"), display_name=r.get("display_name"))
    else:
        sc = Scope()
    _scopes[key] = (time.time(), sc)
    return sc


def forget_scope(request) -> None:
    _scopes.pop(user_of(request).get("token") or "local", None)


def can_act(sc: Scope, pending_type: Optional[str], action: Optional[str], row: Optional[dict]) -> Optional[str]:
    """Why this person may not take this action on this thread (None = allowed)."""
    if action in HUMAN_ACTIONS_BLOCKED:
        return "client replies come from the client's email"
    if sc.role is None:
        return "you don't have access to make-goods yet; ask an admin"
    if row is not None and not sc.allows(row.get("market"), row.get("seller_id")):
        return "this make-good belongs to a client you can't see"
    if pending_type and sc.role not in ACT.get(pending_type, set()):
        return "this step needs a sales manager" if pending_type == "manager_review" else "your role can't take this step"
    return None
