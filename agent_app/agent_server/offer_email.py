"""The make-good offer email: facts from the rules engine, prose from the AI, numbers checked before sending.

Every date, program, impression count and dollar figure comes from the packages the rule functions built and is
laid out by this template. The AI writes only the greeting, a short intro and the closing, and that prose may not
contain digits; check_grounding then confirms every number in the final email is one of the facts.
"""
import html
import json
import re
from datetime import date
from typing import Optional

REASON_WORDS = {"Sports Overrun": "a live sports overrun", "Political Priority": "political advertising that had priority",
                "Breaking News": "breaking news coverage", "Technical Failure": "a technical problem at the station",
                "Network Override": "a network schedule change", "Traffic Error": "a scheduling error on our side"}
_NUM = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _d(v) -> str:
    try:
        return date.fromisoformat(str(v)[:10]).strftime("%a %b %-d")
    except ValueError:
        return str(v)


def _usd(v) -> str:
    return f"${float(v):,.0f}"


def facts(spot: dict, packages: list[dict], sender_name: Optional[str] = None) -> dict:
    """What the email states, as data (also stored with the offer and shown to the manager)."""
    return {
        "advertiser": spot.get("advertiser_name"), "campaign": spot.get("campaign_name"),
        "contact_name": spot.get("contact_name"), "seller_name": sender_name or spot.get("seller_name"),
        "station": spot.get("call_letters"), "market": spot.get("market"),
        "missed": {"date": str(spot.get("broadcast_date")), "daypart": spot.get("daypart"),
                   "program": spot.get("program_name"), "reason": REASON_WORDS.get(spot.get("preemption_reason"), "a schedule change"),
                   "value_usd": spot.get("unit_rate_usd"), "owed_impressions": spot.get("owed_impressions")},
        "options": [{"label": p["package_label"], "lines": [
                        {"avail_id": ln["avail_id"], "date": str(ln["air_date"]), "daypart": ln["daypart"],
                         "program": ln["program_name"], "impressions": ln["est_impressions"]} for ln in p["lines"]],
                     "impressions": p["est_impressions"], "coverage_pct": p["coverage_pct"],
                     "value_usd": p["proposed_value_usd"], "dollar_label": p["dollar_label"]} for p in packages],
    }


def sender_from_email(email: Optional[str]) -> Optional[str]:
    """ "jane.doe@example.com" -> "Jane Doe" (when the access map has no display name)."""
    if not email or "@" not in email:
        return None
    return " ".join(w.capitalize() for w in email.split("@")[0].replace("_", ".").split(".") if w)


def default_prose(f: dict) -> dict:
    first = (f.get("contact_name") or "").split(" ")[0] or "there"
    several = len(f.get("options") or []) > 1
    return {"greeting": f"Hi {first},",
            "intro": f"I'm sorry your spot didn't air as planned; it was bumped by {f['missed']['reason']}. "
                     f"Here is what we'd like to run instead so you still get the audience you bought.",
            "closing": ("Just reply with the one you'd like and I'll get it booked." if several else
                        "If that works for you, just reply yes and I'll get it booked.") +
                       " If another day or program would suit you better, tell me and I'll find one."}


def prose_request(spot: dict, f: dict, note: Optional[str] = None) -> dict:
    """What the AI gets to write the greeting, intro and closing: names and the reason only, no facts."""
    return {"contact_first_name": (spot.get("contact_name") or "").split(" ")[0], "advertiser": spot["advertiser_name"],
            "reason": f["missed"]["reason"], "options_offered": "several" if len(f["options"]) > 1 else "one",
            "manager_note": note}


def clean_prose(raw: str, f: dict) -> tuple[dict, list[str]]:
    """The AI's prose if it follows the rules (no digits, no ids, short); otherwise the default wording."""
    fallback, problems = default_prose(f), []
    seller = (f.get("seller_name") or "").split(" ")[0]
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    out = {}
    for k in ("greeting", "intro", "closing"):
        v = str(data.get(k) or "").strip()
        if not v:
            problems.append(f"{k} missing")
        elif re.search(r"\d|AVL-|SPT-|\$", v):
            problems.append(f"{k} contained a number or id")
        elif len(v) > 400:
            problems.append(f"{k} too long")
        elif re.search(r"\btable\b", v, re.I) or (k != "greeting" and seller and re.search(rf"\b{re.escape(seller)}\b", v)):
            problems.append(f"{k} mentioned a table or the seller")
        else:
            out[k] = v
            continue
        out[k] = fallback[k]
    return out, problems


def _line(ln: dict) -> str:
    return f"{_d(ln['date'])}, {ln['daypart']}: {ln['program']}"


def render(f: dict, prose: dict, ref: str) -> tuple[str, str, str]:
    """(subject, text, html) of the offer. One option reads as a single proposal; several are lettered."""
    m = f["missed"]
    subject = f"Make-good for your {m['daypart']} spot on {_d(m['date'])} ({f['campaign'] or f['advertiser']}) [MG-{ref}]"
    owed = f"{m['owed_impressions']:,}" if m.get("owed_impressions") else None
    missed = (f"{m['daypart']} on {_d(m['date'])}, {m['program']} on {f['station']} {f['market']}"
              + (f" ({owed} impressions, {_usd(m['value_usd'])})" if owed else f" ({_usd(m['value_usd'])})"))
    single = len(f["options"]) == 1
    blocks = []
    for o in f["options"]:
        head = "" if single else f"Option {o['label']}: "
        when = "; ".join(_line(ln) for ln in o["lines"])
        detail = (f"{o['impressions']:,} estimated impressions ({round(o['coverage_pct'] * 100)}% of what was owed), "
                  f"valued at {_usd(o['value_usd'])}")
        blocks.append((head, when, detail))
    text = "\n".join([
        prose["greeting"], "", prose["intro"], "",
        f"The spot that didn't run: {missed}.", "",
        "What we'd like to run instead:" if single else "What we can run instead:",
        *[f"  {h}{w}\n  {d}" for h, w, d in blocks], "",
        prose["closing"], "", "Thanks,", f["seller_name"] or "", f"{f['station']} {f['market']}", "",
        f"Reference MG-{ref}"])
    esc = html.escape
    body_blocks = "".join(f"<div style='margin:8px 0;padding:10px 12px;border-left:3px solid #1a7f37;background:#f6fbf7'>"
                          f"<b>{esc(h)}{esc(w)}</b><br><span style='color:#5f6b7a'>{esc(d)}</span></div>" for h, w, d in blocks)
    html_body = f"""<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#1b1f24;max-width:560px">
<p>{esc(prose['greeting'])}</p><p>{esc(prose['intro'])}</p>
<p style="color:#5f6b7a">The spot that didn't run: {esc(missed)}.</p>
<p style="margin-bottom:4px"><b>{'What we&#39;d like to run instead' if single else 'What we can run instead'}</b></p>{body_blocks}
<p>{esc(prose['closing'])}</p>
<p>Thanks,<br><b>{esc(f['seller_name'] or '')}</b><br>{esc(str(f['station']))} {esc(str(f['market']))}</p>
<p style="color:#898781;font-size:12px">Reference MG-{ref}</p></div>"""
    return subject, text, html_body


def check_grounding(text: str, f: dict, ref: str) -> list[str]:
    """Numbers in the email that are not facts (should always be empty: only the template writes numbers)."""
    m = f["missed"]
    allowed = {ref}
    for v in [m.get("value_usd"), m.get("owed_impressions")] + [x for o in f["options"] for x in (o["impressions"], o["value_usd"])]:
        if v is not None:
            allowed |= {f"{float(v):,.0f}", f"{float(v):.0f}"}
    for o in f["options"]:
        allowed.add(str(round(o["coverage_pct"] * 100)))
    for d in [m["date"]] + [ln["date"] for o in f["options"] for ln in o["lines"]]:
        allowed.add(_d(d).split(" ")[-1])
    for name in [m.get("program")] + [ln["program"] for o in f["options"] for ln in o["lines"]] + [f.get("market"), f.get("station")]:
        allowed |= set(_NUM.findall(str(name or "")))
    return sorted({n for n in _NUM.findall(text.replace(f"MG-{ref}", "")) if n not in allowed})
