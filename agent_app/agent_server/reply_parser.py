"""Validate what was read from a client's reply before anyone acts on it.

The app reads replies with AI Functions in SQL (read_client_email: ai_classify + ai_extract); from_ai_read turns that
reading into a decision. validate() checks an LLM reading the same way (the fallback when the SQL read fails).

The reply is untrusted outside text. The AI only describes it (decision, options picked, counter request); this
module then keeps the description inside what is possible:
  - options must be letters that were actually offered (anything else is dropped and lowers confidence)
  - the evidence quote must appear in the email, word for word, or confidence is capped at 0.5
  - option letters the client names explicitly are cross-checked against the AI's reading
  - a reply from an address other than the one the offer went to is marked unverified
  - counter requests pass only as days, dayparts and program names (no free-form instructions)
  - wording that tries to instruct the system ("ignore your instructions", "book all", "issue a refund") is flagged
Nothing is booked from this: the seller confirms every reply.
"""
import json
import re
from typing import Optional

from agent_server.mail.matching import labels_mentioned, normalize
from agent_server.mail.policy import same_sender
from agent_server.rules import days as norm_days

DECISIONS = {"accept", "decline", "counter", "question", "unclear"}
_INSTRUCTIONS = re.compile(r"ignore (all |any |your |the |previous |prior )*(instructions|rules)|system prompt|you are now|"
                           r"disregard (the|your|all)|act as|book (all|every)|issue (a )?refund|override", re.I)
CONFIRM_THRESHOLD = 0.8


def validate(raw: str, email_text: str, offered: list[str], *, from_addr: str = "", sent_to: str = "",
             dayparts: Optional[list[str]] = None) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        d = {}
    notes: list[str] = []
    decision = d.get("decision") if d.get("decision") in DECISIONS else "unclear"
    picked = [str(x).strip().upper() for x in (d.get("options") or [])]
    options = [x for x in picked if x in offered]
    if len(options) != len(picked):
        notes.append(f"ignored options that were not offered: {sorted(set(picked) - set(offered))}")
    try:
        conf = max(0.0, min(1.0, float(d.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    quote = str(d.get("evidence_quote") or "").strip()
    if not quote or normalize(quote) not in normalize(email_text):
        conf = min(conf, 0.5)
        notes.append("the quoted evidence is not in the email")
    named = labels_mentioned(email_text, offered)
    if decision == "accept" and named and set(named) != set(options):
        conf = min(conf, 0.6)
        notes.append(f"the email names option(s) {named}, the reading says {options}")
    if decision == "accept" and not options:
        decision, conf = "unclear", min(conf, 0.4)
        notes.append("accepted without naming an offered option")
    if picked and len(options) != len(picked):
        conf = min(conf, 0.6)
    c = d.get("counter") or {}
    dp = set(dayparts or [])
    counter = {"only_days": norm_days(c.get("only_days") or []), "avoid_days": norm_days(c.get("avoid_days") or []),
               "exclude_programs": [str(p)[:60] for p in (c.get("exclude_programs") or []) if str(p).strip()][:5],
               "only_dayparts": [x for x in (c.get("only_dayparts") or []) if x in dp]}
    if _INSTRUCTIONS.search(email_text or ""):
        conf = min(conf, 0.3)
        notes.append("the email contains wording that tries to instruct the system; read it yourself before acting")
    verified = same_sender(from_addr, sent_to) if from_addr and sent_to else True
    if not verified:
        conf = min(conf, 0.5)
        notes.append("the reply came from a different address than the offer was sent to")
    return {"decision": decision, "options": options, "counter": counter,
            "question": str(d.get("question") or "")[:500], "evidence_quote": quote[:300], "confidence": round(conf, 2),
            "sender_verified": verified, "needs_seller": True, "auto_confident": conf >= CONFIRM_THRESHOLD and verified,
            "notes": notes}


def _counter_from_text(change: str, dayparts: list[str]) -> dict:
    """Days and dayparts the client asks for, from the requested change ("Tuesdays or Thursdays", "not weekends")."""
    words = re.findall(r"[A-Za-z]+", change or "")
    days = norm_days(words)
    negative = bool(re.search(r"\b(no|not|never|can'?t|cannot|avoid|except|without)\b", change or "", re.I))
    dp = [d for d in dayparts if re.search(rf"\b{re.escape(d)}\b", change or "", re.I)]
    return {"only_days": [] if negative else days, "avoid_days": days if negative else [], "exclude_programs": [],
            "only_dayparts": [] if negative else dp[:1]}


def from_ai_read(read: dict, email_text: str, offered: list[str], *, from_addr: str = "", sent_to: str = "",
                 dayparts: Optional[list[str]] = None) -> dict:
    """Decision from the AI Functions reading (intent yes | no | needs more information, option letter, change, question)."""
    notes: list[str] = []
    intent = (read or {}).get("intent") or ""
    label = ((read or {}).get("option_label") or "").strip().upper() or None
    change, question = (read or {}).get("requested_change") or "", (read or {}).get("question") or ""
    options: list[str] = []
    conf = 0.9
    if intent == "yes":
        if label and label in offered:
            options = [label]
        elif label:
            notes.append(f"the client names option {label}, which was not offered")
            conf = 0.4
        elif len(labels_mentioned(email_text, offered)) == 1:
            options = labels_mentioned(email_text, offered)  # "C please."
        elif len(offered) == 1:
            options = list(offered)  # one option was offered: a plain yes accepts it
        else:
            notes.append("the client agrees without saying which option")
            conf = 0.5
        decision = "accept" if options else "unclear"
    elif intent == "no":
        decision = "decline"
    elif intent:
        decision = "question"  # a counter when the client names days or dayparts (below)
    else:
        decision, conf = "unclear", 0.3
    named = labels_mentioned(email_text, offered)
    if options and named and set(named) != set(options):
        conf = min(conf, 0.6)
        notes.append(f"the email names option(s) {named}, the reading says {options}")
    if _INSTRUCTIONS.search(email_text or ""):
        conf = min(conf, 0.3)
        notes.append("the email contains wording that tries to instruct the system; read it yourself before acting")
    verified = same_sender(from_addr, sent_to) if from_addr and sent_to else True
    if not verified:
        conf = min(conf, 0.5)
        notes.append("the reply came from a different address than the offer was sent to")
    quote = next((sent.strip() for sent in re.split(r"(?<=[.!?])\s+", email_text or "") if sent.strip()), "")[:300]
    counter = _counter_from_text(change, dayparts or [])
    if decision == "question" and not any(counter.values()):  # the change names no days: look in the whole email
        counter = _counter_from_text(email_text, dayparts or [])
    if decision == "question" and any(counter.values()):
        decision = "counter"
    return {"decision": decision, "options": options, "counter": counter,
            "question": (question or "")[:500], "requested_change": change[:300], "evidence_quote": quote,
            "confidence": round(conf, 2), "sender_verified": verified, "needs_seller": True,
            "auto_confident": conf >= CONFIRM_THRESHOLD and verified, "notes": notes, "read_by": "ai_functions"}
