"""Client rules: standing preferences a seller states in conversation ("no Daytime Court for Ironwood",
"they only take Tuesdays and Thursdays"). Most of them are in no contract; they build up over a client relationship.

A rule is saved per advertiser (Lakebase store, mirrored to Unity Catalog) and applied on every search as arguments
to the rule functions, so the rules engine, not the LLM, enforces it and the trace shows which rules were applied:

    exclude_program / exclude_genre / exclude_tag / only_days / avoid_days   hard: avails that break it are not offered
    prefer_days / prefer_tag                                                  soft: raise the match score
"""
from datetime import datetime, timezone
from typing import Any, Optional

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAY_ALIASES = {d.lower(): d for d in DAYS} | {
    "monday": "Mon", "tuesday": "Tue", "tues": "Tue", "wednesday": "Wed", "thursday": "Thu", "thurs": "Thu",
    "friday": "Fri", "saturday": "Sat", "sunday": "Sun", "weekend": "Sat,Sun", "weekends": "Sat,Sun",
    "weekday": "Mon,Tue,Wed,Thu,Fri", "weekdays": "Mon,Tue,Wed,Thu,Fri"}
TYPES = {"exclude_program", "exclude_genre", "exclude_tag", "only_days", "avoid_days", "prefer_days", "prefer_tag"}
HARD = {"exclude_program", "exclude_genre", "exclude_tag", "only_days", "avoid_days"}


def days(values: list[str]) -> list[str]:
    """Day names as Mon..Sun ("Tuesdays", "thurs", "weekends" -> Tue, Thu, Sat, Sun)."""
    out: list[str] = []
    for v in values or []:
        key = str(v).strip().lower()
        for d in (_DAY_ALIASES.get(key) or _DAY_ALIASES.get(key.rstrip("s")) or "").split(","):
            if d and d not in out:
                out.append(d)
    return sorted(out, key=DAYS.index)


def label(rule: dict) -> str:
    """How the rule reads on the checklist and in the client panel."""
    v = ", ".join(rule.get("values") or [])
    return {
        "exclude_program": f"never {v}",
        "exclude_genre": f"no {v} programs",
        "exclude_tag": f"no {v} adjacency",
        "only_days": f"only {v}",
        "avoid_days": f"never on {v}",
        "prefer_days": f"prefers {v}",
        "prefer_tag": f"prefers {v} adjacency",
    }.get(rule.get("type"), v)


def normalize(draft: dict, *, tags: list[str], genres: list[str], programs: Optional[list[str]] = None) -> tuple[Optional[dict], str]:
    """Validate a rule the router extracted. Returns (rule, "") or (None, why it can't be saved)."""
    t = (draft or {}).get("type")
    if t not in TYPES:
        return None, "I couldn't tell what kind of rule that is (a program, genre, adjacency or days of the week)."
    values = [str(v).strip() for v in (draft.get("values") or []) if str(v).strip()]
    if t in ("only_days", "avoid_days", "prefer_days"):
        values = days(values)
    elif t in ("exclude_tag", "prefer_tag"):
        values = [v.lower().replace(" ", "_") for v in values if v.lower().replace(" ", "_") in tags]
    elif t == "exclude_genre":
        by_lower = {g.lower(): g for g in genres}
        values = [by_lower[v.lower()] for v in values if v.lower() in by_lower]
    elif t == "exclude_program":
        values = list(programs or [])
    if not values:
        return None, "I couldn't match that to programs, genres, adjacency tags or days we schedule."
    rule = {"type": t, "values": values, "strength": "hard" if t in HARD else "soft"}
    rule["label"] = label(rule)
    return rule, ""


def stamp(rule: dict, *, source_text: str, created_by: Optional[str], advertiser_id: str, advertiser_name: str) -> dict:
    return {**rule, "source_text": source_text[:500], "created_by": created_by or "seller", "advertiser_id": advertiser_id,
            "advertiser_name": advertiser_name, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "active": True}


def to_params(rules: list[dict], prefs: Optional[dict[str, float]] = None) -> dict[str, Any]:
    """Rule-function arguments for a client's active rules."""
    active = [r for r in rules or [] if r.get("active", True)]
    pick = lambda t: sorted({v for r in active if r["type"] == t for v in r["values"]})  # noqa: E731
    p: dict[str, Any] = {
        "exclude_programs": pick("exclude_program"), "exclude_genres": pick("exclude_genre"),
        "exclude_tags": pick("exclude_tag"), "only_dows": pick("only_days"), "avoid_dows": pick("avoid_days"),
        "prefer_dows": pick("prefer_days"), "labels": [r["label"] for r in active],
    }
    prefs = dict(prefs or {})
    for tag in pick("prefer_tag"):
        prefs[tag] = round(min(1.0, prefs.get(tag, 0.0) + 0.5), 3)
    p["prefs"] = prefs
    return p


def suggest(evidence: dict[str, dict[str, int]], existing: list[dict], min_rejects: int = 3) -> list[dict]:
    """Rules worth proposing from reviewer decisions: an adjacency rejected at least min_rejects times, never accepted."""
    have = {v for r in existing or [] if r["type"] == "exclude_tag" for v in r["values"]}
    out = []
    for tag, ev in sorted((evidence or {}).items()):
        if ev.get("rejects", 0) >= min_rejects and ev.get("accepts", 0) == 0 and tag not in have:
            rule = {"type": "exclude_tag", "values": [tag], "strength": "hard"}
            out.append({**rule, "label": label(rule), "evidence": f"rejected {ev['rejects']} times, never accepted"})
    return out
