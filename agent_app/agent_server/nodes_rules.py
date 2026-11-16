"""Client rules from conversation, as graph nodes.

    parse_request (intent add_rule) -> propose_rule -> confirm_rule ⏸ save | cancel -> save_rule
        -> search_inventory again when a make-good for that client is open (the new rule applies at once), else END

The router extracts a draft; propose_rule resolves the advertiser among the clients the person may see
(advertiser_lookup) and the program names against real inventory (program_lookup), so a rule is only ever saved
against things the rules engine can match. The seller confirms before anything is saved.
"""
import json
from typing import Any, Optional

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from agent_server import rules as R
from agent_server.config import fq
from agent_server.memory import Memory
from agent_server.steps import ADV_RE, TAGS, _live, _step
from agent_server.uc_tools import call_uc, current_user, query, write_as_app

_genres: list[str] = []


async def genres() -> list[str]:
    if not _genres:
        _genres.extend(r["program_genre"] for r in await query(
            f"SELECT DISTINCT program_genre FROM {fq('gold_available_inventory')} ORDER BY 1", "rules.genres"))
    return _genres


def make_rule_nodes(memory: Optional[Memory]) -> dict:
    async def propose_rule(state: dict) -> dict:
        draft = state.get("pending_rule") or {}
        spot = state.get("spot") or {}
        adv_text = (draft.get("advertiser") or "").strip()
        if adv_text and not (spot and adv_text.lower() in (spot.get("advertiser_name") or "").lower()):
            hits = await call_uc("advertiser_lookup", p_text=adv_text)
            if len(hits) != 1:
                why = (f"I found no client matching \"{adv_text}\" among your accounts." if not hits else
                       f"\"{adv_text}\" matches {len(hits)} clients ({', '.join(h['advertiser_name'] for h in hits[:4])}); which one?")
                return {"pending_rule": None, "messages": [AIMessage(why)], "trail": _step("propose_rule", "Couldn't pick the client", why)}
            adv = {"advertiser_id": hits[0]["advertiser_id"], "advertiser_name": hits[0]["advertiser_name"]}
        elif spot:
            adv = {"advertiser_id": spot["advertiser_id"], "advertiser_name": spot["advertiser_name"]}
        else:
            why = "Which client is this rule for? Name the advertiser, or open one of their make-goods first."
            return {"pending_rule": None, "messages": [AIMessage(why)], "trail": _step("propose_rule", "Couldn't pick the client", why)}
        programs = None
        if draft.get("type") == "exclude_program":
            programs = []
            for v in draft.get("values") or []:
                found = await call_uc("program_lookup", p_text=str(v), p_station_id=spot.get("station_id"))
                found = sorted(found, key=lambda r: ("exact", "contains", "similar").index(r["match_type"]))
                programs += [r["program_name"] for r in found[:2] if r["program_name"] not in programs]
        rule, why = R.normalize(draft, tags=TAGS, genres=await genres(), programs=programs)
        if not rule:
            return {"pending_rule": None, "messages": [AIMessage(why)], "trail": _step("propose_rule", "Couldn't read the rule", why)}
        pending = {**rule, **adv, "source_text": draft.get("source_text") or ""}
        return {"pending_rule": pending,
                "trail": _step("propose_rule", "Understood a client rule", f"{adv['advertiser_name']}: {rule['label']} ({rule['strength']})")}

    async def confirm_rule(state: dict) -> dict:
        p = state["pending_rule"]
        if not _live(state, memory):
            return {"rule_decision": {"action": "save"}}
        d = interrupt({"type": "confirm_rule", "advertiser_id": p["advertiser_id"], "advertiser_name": p["advertiser_name"],
                       "rule": {k: p[k] for k in ("type", "values", "strength", "label")}, "actions": ["save", "cancel"]})
        d = d if isinstance(d, dict) else {"action": str(d)}
        return {"rule_decision": d, "trail": _step("confirm_rule", "Seller decision on the rule",
                                                   "Saved" if d.get("action") == "save" else "Not saved")}

    async def save_rule(state: dict, config: RunnableConfig) -> dict:
        p = state["pending_rule"]
        assert ADV_RE.fullmatch(p["advertiser_id"])
        rule = R.stamp({k: p[k] for k in ("type", "values", "strength", "label")}, source_text=p.get("source_text") or "",
                       created_by=current_user(), advertiser_id=p["advertiser_id"], advertiser_name=p["advertiser_name"])
        if _live(state, memory):
            rule = await memory.save_rule(p["advertiser_id"], rule)
            await memory.log_event(config["configurable"]["thread_id"], "rule_saved", rule["created_by"], spot=state.get("spot"),
                                   details=rule)
            await write_as_app(
                f"INSERT INTO {fq('mg_advertiser_rules')} (rule_id, advertiser_id, advertiser_name, rule_type, rule_values, "
                f"strength, label, source_text, created_by, created_at, active, market, seller_id) VALUES (:rule_id, "
                f":advertiser_id, :advertiser_name, :rule_type, from_json(:rule_values, 'ARRAY<STRING>'), :strength, :label, "
                f":source_text, :created_by, current_timestamp(), true, :market, :seller_id)",
                {"rule_id": rule["rule_id"], "advertiser_id": rule["advertiser_id"], "advertiser_name": rule["advertiser_name"],
                 "rule_type": rule["type"], "rule_values": json.dumps(rule["values"]), "strength": rule["strength"],
                 "label": rule["label"], "source_text": rule["source_text"], "created_by": rule["created_by"],
                 "market": (state.get("spot") or {}).get("market"), "seller_id": (state.get("spot") or {}).get("seller_id")})
        same_client = bool(state.get("ranked") or state.get("candidates")) and \
            (state.get("spot") or {}).get("advertiser_id") == p["advertiser_id"]
        rules = (list(state.get("rules") or []) + [rule]) if same_client else state.get("rules")
        update: dict[str, Any] = {"pending_rule": None, "rules": rules, "rule_decision": {"action": "save", "rerun": same_client},
                                  "messages": [AIMessage(f"Saved for {p['advertiser_name']}: {rule['label']}. The rules engine "
                                                         f"applies it to every search for this client"
                                                         + (", starting with this make-good." if same_client else "."))],
                                  "trail": _step("save_rule", "Saved the client rule", f"{p['advertiser_name']}: {rule['label']}")}
        if same_client:
            update["rule_params"] = R.to_params(rules, state.get("base_prefs") or state.get("prefs"))
            update["prefs"] = update["rule_params"]["prefs"]
            update["gap_iterations"], update["search"] = 0, {}
        return update

    return {"propose_rule": propose_rule, "confirm_rule": confirm_rule, "save_rule": save_rule}
