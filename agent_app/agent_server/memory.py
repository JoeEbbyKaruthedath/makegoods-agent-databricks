"""Agent memory on Lakebase (managed Postgres).

- Session memory: LangGraph checkpointer. Every step of every conversation is saved, which is
  what lets the graph pause for human review and resume later (even hours later).
- Preference memory: LangGraph store, namespace ("advertiser_prefs", advertiser_id). Holds the
  accept/reject evidence learned from reviewer decisions, layered over the baseline learned
  from history (gold_advertiser_preferences).
- Client rules: LangGraph store, namespace ("advertiser_rules", advertiser_id), one item per rule
  ("no Daytime Court", "prefers Tue/Thu"), applied as arguments to the rule functions on every search.
- Review queue + decision log: plain Postgres tables the review UI reads and writes.
- Offer lifecycle: offers (draft -> manager approval -> sent -> client reply -> booked), the mail sent and
  received for each, holds on offered avails, and an event log of every step.

Rows carry the spot's market and owning seller, so every list can be scoped to what the signed-in person may
see (Scope: admin = all, manager = their markets, seller = their own clients). The scope itself comes from
Unity Catalog (mg_my_access) as that person.
"""
import json
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Optional

from databricks_ai_bridge.lakebase import AsyncLakebasePool
from databricks_langchain import AsyncCheckpointSaver, AsyncDatabricksStore

from agent_server.config import APP_SCHEMA, MEMORY_SCHEMA, lakebase_kwargs

logger = logging.getLogger(__name__)

DDL = [
    f"CREATE SCHEMA IF NOT EXISTS {APP_SCHEMA}",
    f"""CREATE TABLE IF NOT EXISTS {APP_SCHEMA}.mg_review_queue (
        thread_id TEXT PRIMARY KEY,
        spot_id TEXT NOT NULL,
        advertiser_id TEXT,
        advertiser_name TEXT,
        station TEXT,
        daypart TEXT,
        revenue_at_risk_usd DOUBLE PRECISION,
        outcome TEXT,
        recommended_avail_id TEXT,
        ranked JSONB,
        table_md TEXT,
        explanation TEXT,
        trail JSONB,
        trace_id TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    f"""CREATE TABLE IF NOT EXISTS {APP_SCHEMA}.mg_decision_log (
        decision_id BIGSERIAL PRIMARY KEY,
        thread_id TEXT,
        spot_id TEXT NOT NULL,
        advertiser_id TEXT,
        action TEXT NOT NULL,
        outcome TEXT NOT NULL,
        recommended_avail_id TEXT,
        final_avail_id TEXT,
        refund_usd DOUBLE PRECISION,
        reviewer TEXT,
        reason TEXT,
        recommendation_trace_id TEXT,
        pref_updates JSONB,
        decided_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    f"""ALTER TABLE {APP_SCHEMA}.mg_review_queue ADD COLUMN IF NOT EXISTS market TEXT,
        ADD COLUMN IF NOT EXISTS seller_id TEXT, ADD COLUMN IF NOT EXISTS stage TEXT,
        ADD COLUMN IF NOT EXISTS packages JSONB, ADD COLUMN IF NOT EXISTS offer_id TEXT""",
    f"""ALTER TABLE {APP_SCHEMA}.mg_decision_log ADD COLUMN IF NOT EXISTS final_avail_ids JSONB,
        ADD COLUMN IF NOT EXISTS market TEXT, ADD COLUMN IF NOT EXISTS seller_id TEXT,
        ADD COLUMN IF NOT EXISTS offer_id TEXT, ADD COLUMN IF NOT EXISTS booked_value_usd DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS owed_value_usd DOUBLE PRECISION""",
    f"""CREATE TABLE IF NOT EXISTS {APP_SCHEMA}.mg_offers (
        offer_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        spot_id TEXT NOT NULL,
        advertiser_id TEXT,
        advertiser_name TEXT,
        market TEXT,
        seller_id TEXT,
        seller_name TEXT,
        manager_id TEXT,
        contact_name TEXT,
        contact_email TEXT,
        sent_to TEXT,
        subject TEXT,
        body_text TEXT,
        body_html TEXT,
        packages JSONB,
        ref_token TEXT,
        status TEXT NOT NULL,
        manager_note TEXT,
        approved_by TEXT,
        approved_at TIMESTAMPTZ,
        sent_by TEXT,
        sent_at TIMESTAMPTZ,
        message_id TEXT,
        mail_thread_id TEXT,
        reply JSONB,
        trace_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    f"""CREATE TABLE IF NOT EXISTS {APP_SCHEMA}.mg_mail_log (
        message_id TEXT PRIMARY KEY,
        offer_id TEXT,
        thread_id TEXT,
        mail_thread_id TEXT,
        direction TEXT NOT NULL,
        from_addr TEXT,
        to_addr TEXT,
        subject TEXT,
        body_text TEXT,
        matched_by TEXT,
        processed BOOLEAN NOT NULL DEFAULT false,
        logged_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    f"""CREATE TABLE IF NOT EXISTS {APP_SCHEMA}.mg_avail_holds (
        avail_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        spot_id TEXT,
        offer_id TEXT,
        held_until TIMESTAMPTZ NOT NULL)""",
    f"""CREATE TABLE IF NOT EXISTS {APP_SCHEMA}.mg_offer_events (
        event_id BIGSERIAL PRIMARY KEY,
        thread_id TEXT NOT NULL,
        offer_id TEXT,
        spot_id TEXT,
        market TEXT,
        seller_id TEXT,
        event TEXT NOT NULL,
        actor TEXT,
        details JSONB,
        trace_id TEXT,
        at TIMESTAMPTZ NOT NULL DEFAULT now())""",
]


@dataclass
class Scope:
    """What the signed-in person may see (from Unity Catalog mg_my_access)."""
    role: Optional[str] = None  # seller | manager | admin; None = nothing
    seller_id: Optional[str] = None
    markets: list[str] = field(default_factory=list)
    principal: Optional[str] = None
    granted_role: Optional[str] = None  # role granted; differs from role during an admin's "view as"
    view_as_until: Optional[str] = None
    display_name: Optional[str] = None

    @classmethod
    def everything(cls) -> "Scope":
        return cls(role="admin")

    def where(self, alias: str = "") -> tuple[str, list]:
        """SQL condition limiting rows that carry market + seller_id to this scope."""
        a = f"{alias}." if alias else ""
        if self.role == "admin":
            return "TRUE", []
        if self.role == "manager" and self.markets:
            return f"{a}market = ANY(%s)", [list(self.markets)]
        if self.role == "seller" and self.seller_id:
            return f"{a}seller_id = %s", [self.seller_id]
        return "FALSE", []

    def allows(self, market: Optional[str], seller_id: Optional[str]) -> bool:
        if self.role == "admin":
            return True
        if self.role == "manager":
            return market in self.markets
        return self.role == "seller" and seller_id is not None and seller_id == self.seller_id


_OFFER_COLS = ["offer_id", "thread_id", "spot_id", "advertiser_id", "advertiser_name", "market", "seller_id", "seller_name",
               "manager_id", "contact_name", "contact_email", "sent_to", "subject", "body_text", "body_html", "packages",
               "ref_token", "status", "manager_note", "approved_by", "approved_at", "sent_by", "sent_at", "message_id",
               "mail_thread_id", "reply", "trace_id"]
_JSON_COLS = {"packages", "reply", "ranked", "trail", "pref_updates", "final_avail_ids", "details"}


def _val(col: str, v: Any) -> Any:
    return json.dumps(v, default=str) if col in _JSON_COLS and v is not None else v


class Memory:
    def __init__(self):
        kw = lakebase_kwargs()
        self.checkpointer = AsyncCheckpointSaver(**kw, schema=MEMORY_SCHEMA)
        self.store = AsyncDatabricksStore(**kw, schema=MEMORY_SCHEMA)
        self.pool = AsyncLakebasePool(**kw, min_size=1, max_size=5)

    async def __aenter__(self):
        await self.checkpointer.__aenter__()
        await self.store.__aenter__()
        await self.pool.open()
        await self.checkpointer.setup()
        await self.store.setup()
        async with self.pool.connection() as conn:
            for stmt in DDL:
                await conn.execute(stmt)
        logger.info("Lakebase memory ready (memory schema=%s, app schema=%s)", MEMORY_SCHEMA, APP_SCHEMA)
        return self

    async def __aexit__(self, *exc):
        await self.pool.close()
        await self.store.__aexit__(*exc)
        await self.checkpointer.__aexit__(*exc)

    # ---------------------------------------------------------------- preference memory
    async def pref_evidence(self, advertiser_id: str) -> dict[str, dict[str, int]]:
        item = await self.store.aget(("advertiser_prefs", advertiser_id), "adjacency")
        return dict(item.value) if item else {}

    async def add_pref_evidence(self, advertiser_id: str, accepted: list[str], rejected: list[str]) -> dict:
        ev = await self.pref_evidence(advertiser_id)
        for tag in accepted:
            ev.setdefault(tag, {"accepts": 0, "rejects": 0})["accepts"] += 1
        for tag in rejected:
            ev.setdefault(tag, {"accepts": 0, "rejects": 0})["rejects"] += 1
        await self.store.aput(("advertiser_prefs", advertiser_id), "adjacency", ev)
        return {"accepted": accepted, "rejected": rejected}

    # ---------------------------------------------------------------- review queue
    async def upsert_review(self, row: dict[str, Any]) -> None:
        cols = ["thread_id", "spot_id", "advertiser_id", "advertiser_name", "station", "daypart",
                "revenue_at_risk_usd", "outcome", "recommended_avail_id", "ranked", "table_md",
                "explanation", "trail", "trace_id", "market", "seller_id", "stage", "packages", "offer_id"]
        vals = [_val(c, row.get(c)) for c in cols]
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[1:])
        async with self.pool.connection() as conn:
            await conn.execute(
                f"INSERT INTO {APP_SCHEMA}.mg_review_queue ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                f"ON CONFLICT (thread_id) DO UPDATE SET {sets}, status = 'pending', updated_at = now()", vals)

    async def resolve_review(self, thread_id: str, spot_id: str, status: str) -> None:
        """Close this thread's review and any other pending review of the same spot."""
        async with self.pool.connection() as conn:
            await conn.execute(f"UPDATE {APP_SCHEMA}.mg_review_queue SET status = %s, updated_at = now() "
                               f"WHERE thread_id = %s OR (spot_id = %s AND status = 'pending')", (status, thread_id, spot_id))

    async def set_stage(self, thread_id: str, stage: str, offer_id: Optional[str] = None) -> None:
        """Where a thread is in the lifecycle (review list badge): recommended, drafted, awaiting_manager, ..."""
        async with self.pool.connection() as conn:
            await conn.execute(f"UPDATE {APP_SCHEMA}.mg_review_queue SET stage = %s, offer_id = coalesce(%s, offer_id), "
                               f"updated_at = now() WHERE thread_id = %s", (stage, offer_id, thread_id))

    async def get_review(self, thread_id: str) -> Optional[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {APP_SCHEMA}.mg_review_queue WHERE thread_id = %s", (thread_id,))
            return await cur.fetchone()

    async def list_reviews(self, status: str = "pending", limit: int = 50, scope: Optional[Scope] = None) -> list[dict]:
        cond, params = (scope or Scope.everything()).where()
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {APP_SCHEMA}.mg_review_queue WHERE status = %s AND {cond} "
                                     f"ORDER BY updated_at DESC LIMIT %s", (status, *params, limit))
            return await cur.fetchall()

    # ---------------------------------------------------------------- decision log
    async def log_decision(self, row: dict[str, Any]) -> None:
        cols = ["thread_id", "spot_id", "advertiser_id", "action", "outcome", "recommended_avail_id",
                "final_avail_id", "refund_usd", "reviewer", "reason", "recommendation_trace_id", "pref_updates",
                "final_avail_ids", "market", "seller_id", "offer_id", "booked_value_usd", "owed_value_usd"]
        vals = [_val(c, row.get(c)) for c in cols]
        async with self.pool.connection() as conn:
            await conn.execute(f"INSERT INTO {APP_SCHEMA}.mg_decision_log ({', '.join(cols)}) "
                               f"VALUES ({', '.join(['%s'] * len(cols))})", vals)

    async def list_decisions(self, limit: int = 50, scope: Optional[Scope] = None) -> list[dict]:
        cond, params = (scope or Scope.everything()).where()
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {APP_SCHEMA}.mg_decision_log WHERE {cond} "
                                     f"ORDER BY decided_at DESC LIMIT %s", (*params, limit))
            return await cur.fetchall()

    async def booked_avails(self, exclude_thread: Optional[str] = None) -> list[str]:
        """Avails already used by a make-good decided in the app, or held for another client's open offer
        (excluded from other spots' searches). Global on purpose: inventory is shared across sellers."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"""SELECT DISTINCT a FROM (
                      SELECT final_avail_id AS a FROM {APP_SCHEMA}.mg_decision_log
                      WHERE outcome IN ('make_good', 'make_good_exception') AND final_avail_id IS NOT NULL
                      UNION ALL
                      SELECT jsonb_array_elements_text(final_avail_ids) FROM {APP_SCHEMA}.mg_decision_log
                      WHERE outcome IN ('make_good', 'make_good_exception') AND final_avail_ids IS NOT NULL
                      UNION ALL
                      SELECT avail_id FROM {APP_SCHEMA}.mg_avail_holds
                      WHERE held_until > now() AND thread_id IS DISTINCT FROM %s) x""", (exclude_thread,))
            return [r["a"] for r in await cur.fetchall()]

    async def decided_spots(self) -> list[str]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT DISTINCT spot_id FROM {APP_SCHEMA}.mg_decision_log")
            return [r["spot_id"] for r in await cur.fetchall()]

    async def spot_stages(self) -> dict[str, dict]:
        """Latest lifecycle stage per spot with an open review (queue badges)."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT DISTINCT ON (spot_id) spot_id, thread_id, stage, offer_id FROM "
                                     f"{APP_SCHEMA}.mg_review_queue WHERE status = 'pending' ORDER BY spot_id, updated_at DESC")
            return {r["spot_id"]: r for r in await cur.fetchall()}

    # ---------------------------------------------------------------- client rules
    async def rules(self, advertiser_id: str) -> list[dict]:
        items = await self.store.asearch(("advertiser_rules", advertiser_id), limit=100)
        return sorted((dict(i.value) for i in items), key=lambda r: r.get("created_at", ""))

    async def save_rule(self, advertiser_id: str, rule: dict) -> dict:
        rule = {**rule, "rule_id": rule.get("rule_id") or f"rule-{uuid.uuid4().hex[:10]}"}
        await self.store.aput(("advertiser_rules", advertiser_id), rule["rule_id"], rule)
        return rule

    async def delete_rule(self, advertiser_id: str, rule_id: str) -> None:
        await self.store.adelete(("advertiser_rules", advertiser_id), rule_id)

    # ---------------------------------------------------------------- offers
    async def upsert_offer(self, row: dict[str, Any]) -> None:
        cols = [c for c in _OFFER_COLS if c in row]
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "offer_id")
        async with self.pool.connection() as conn:
            await conn.execute(
                f"INSERT INTO {APP_SCHEMA}.mg_offers ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                f"ON CONFLICT (offer_id) DO UPDATE SET {sets}, updated_at = now()", [_val(c, row[c]) for c in cols])

    async def update_offer(self, offer_id: str, **fields: Any) -> None:
        """Change some fields of an existing offer (status, approval, sending, the client's reply)."""
        cols = [c for c in fields if c in _OFFER_COLS and c != "offer_id"]
        if not cols:
            return
        sets = ", ".join(f"{c} = %s" for c in cols)
        async with self.pool.connection() as conn:
            await conn.execute(f"UPDATE {APP_SCHEMA}.mg_offers SET {sets}, updated_at = now() WHERE offer_id = %s",
                               [_val(c, fields[c]) for c in cols] + [offer_id])

    async def get_offer(self, offer_id: str) -> Optional[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {APP_SCHEMA}.mg_offers WHERE offer_id = %s", (offer_id,))
            return await cur.fetchone()

    async def list_offers(self, scope: Optional[Scope] = None, statuses: Optional[list[str]] = None,
                          limit: int = 200) -> list[dict]:
        cond, params = (scope or Scope.everything()).where()
        status_cond = "status = ANY(%s)" if statuses else "TRUE"
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {APP_SCHEMA}.mg_offers WHERE {cond} AND {status_cond} "
                                     f"ORDER BY updated_at DESC LIMIT %s",
                                     (*params, *([statuses] if statuses else []), limit))
            return await cur.fetchall()

    async def offers_awaiting_client(self) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {APP_SCHEMA}.mg_offers WHERE status = 'sent' AND mail_thread_id IS NOT NULL")
            return await cur.fetchall()

    # ---------------------------------------------------------------- holds
    async def hold_avails(self, thread_id: str, spot_id: str, offer_id: str, avail_ids: list[str], hours: float) -> None:
        async with self.pool.connection() as conn:
            for a in avail_ids:
                await conn.execute(
                    f"INSERT INTO {APP_SCHEMA}.mg_avail_holds (avail_id, thread_id, spot_id, offer_id, held_until) "
                    f"VALUES (%s, %s, %s, %s, now() + make_interval(hours => %s)) ON CONFLICT (avail_id) DO UPDATE SET "
                    f"thread_id = EXCLUDED.thread_id, spot_id = EXCLUDED.spot_id, offer_id = EXCLUDED.offer_id, "
                    f"held_until = EXCLUDED.held_until", (a, thread_id, spot_id, offer_id, int(round(hours))))

    async def release_holds(self, thread_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(f"DELETE FROM {APP_SCHEMA}.mg_avail_holds WHERE thread_id = %s", (thread_id,))

    # ---------------------------------------------------------------- mail
    async def log_mail(self, row: dict[str, Any]) -> bool:
        """Record a sent or received message; False when it was already recorded (each message is handled once)."""
        cols = ["message_id", "offer_id", "thread_id", "mail_thread_id", "direction", "from_addr", "to_addr", "subject",
                "body_text", "matched_by", "processed"]
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"INSERT INTO {APP_SCHEMA}.mg_mail_log ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                f"ON CONFLICT (message_id) DO NOTHING", [row.get(c, False if c == "processed" else None) for c in cols])
            return cur.rowcount == 1

    async def mark_mail_processed(self, message_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(f"UPDATE {APP_SCHEMA}.mg_mail_log SET processed = true WHERE message_id = %s", (message_id,))

    async def processed_message_ids(self, mail_thread_ids: list[str]) -> set[str]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT message_id FROM {APP_SCHEMA}.mg_mail_log WHERE mail_thread_id = ANY(%s) "
                                     f"AND (direction = 'out' OR processed)", (mail_thread_ids,))
            return {r["message_id"] for r in await cur.fetchall()}

    async def sandbox_replies(self, mail_thread_ids: list[str]) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {APP_SCHEMA}.mg_mail_log WHERE mail_thread_id = ANY(%s) "
                                     f"AND direction = 'in' AND NOT processed ORDER BY logged_at", (mail_thread_ids,))
            return await cur.fetchall()

    async def mail_for_thread(self, thread_id: str) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {APP_SCHEMA}.mg_mail_log WHERE thread_id = %s ORDER BY logged_at",
                                     (thread_id,))
            return await cur.fetchall()

    # ---------------------------------------------------------------- lifecycle events
    async def log_event(self, thread_id: str, event: str, actor: Optional[str] = None, *, offer_id: Optional[str] = None,
                        spot: Optional[dict] = None, details: Optional[dict] = None, trace_id: Optional[str] = None) -> None:
        spot = spot or {}
        async with self.pool.connection() as conn:
            await conn.execute(
                f"INSERT INTO {APP_SCHEMA}.mg_offer_events (thread_id, offer_id, spot_id, market, seller_id, event, actor, "
                f"details, trace_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (thread_id, offer_id, spot.get("spot_id"), spot.get("market"), spot.get("seller_id"), event, actor,
                 _val("details", details), trace_id))

    async def list_events(self, thread_id: Optional[str] = None, scope: Optional[Scope] = None, limit: int = 500) -> list[dict]:
        cond, params = (scope or Scope.everything()).where()
        tcond = "thread_id = %s" if thread_id else "TRUE"
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM {APP_SCHEMA}.mg_offer_events WHERE {cond} AND {tcond} "
                                     f"ORDER BY at DESC LIMIT %s", (*params, *([thread_id] if thread_id else []), limit))
            return await cur.fetchall()

    @asynccontextmanager
    async def thread_lock(self, thread_id: str):
        """Only one worker resumes a thread at a time (session advisory lock); yields False when another holds it."""
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (thread_id,))
            ok = (await cur.fetchone())["ok"]
            try:
                yield ok
            finally:
                if ok:
                    await conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (thread_id,))
