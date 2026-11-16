"""Business wording for the review UI.

The agent's decision trail is written for engineers: it names the tools, model and stores each step used, and the
same detail is kept in the MLflow trace. The review UI shows ad-ops language instead, with no platform or model names.
"""
import ast
import re

# Step titles shown to reviewers; None keeps the agent's own title (it already varies with the outcome).
TITLES = {
    "parse_request": "Understood the request",
    "build_packages": "Built the make-good options with a rule checklist",
    "close_gap": "Widened the search to close the gap",
    "review_selection": "Checked your selection",
    "draft_offer": "Drafted the client offer",
    "manager_review": None,
    "confirm_send": None,
    "send_offer": None,
    "await_client": None,
    "parse_client_reply": "Read the client's reply",
    "seller_confirm": None,
    "propose_rule": None,
    "confirm_rule": None,
    "save_rule": "Saved the client rule",
    "load_spot": None,
    "map_audience": "Matched the contracted audience",
    "recall_preferences": "Recalled advertiser preferences",
    "search_inventory": "Checked inventory against the make-good rules",
    "score_candidates": "Scored the options out of 10",
    "check_shortfall": "Checked the impression math",
    "format_recommendation": "Prepared the options table",
    "explain_recommendation": "Wrote the recommendation",
    "refund_path": "Recommended a refund",
    "human_review": "Reviewer decision",
    "finalize": None,
    "ask_genie": "Looked up make-goods analytics",
}

# Last-resort scrub for anything platform-specific left in a detail line.
_SCRUB = [
    (re.compile(r"\s*\((?:deterministic, )?no LLM call\)"), ""),
    (re.compile(r"\s+in Lakebase\b"), ""),
    (re.compile(r"\bUC functions?\b"), "rules engine"),
    (re.compile(r"\bthe Genie space\b|\bGenie\b"), "make-goods analytics"),
    (re.compile(r"\bdatabricks-[a-z0-9-]+"), ""),
    (re.compile(r"\bLLM\b"), "AI"),
    (re.compile(r"\s*\(prompt [^)]*\)"), ""),
    (re.compile(r"\bvia (gmail|sandbox)\b"), ""),
    (re.compile(r"\bLakebase\b"), "the make-goods workspace"),
]


def _scrub(text: str) -> str:
    for pattern, repl in _SCRUB:
        text = pattern.sub(repl, text)
    return re.sub(r"\s{2,}", " ", text).strip()


AUDIENCE_MATCH = {"exact_alias": "exact match", "normalized": "matched after normalizing the wording",
                  "advertiser_default": "advertiser's default audience", "unmapped": "not mapped"}
REVIEW_ACTION = {"accept": "Accepted the recommendation", "modify": "Chose a different option", "reject": "Rejected the recommendation",
                 "refine": "Asked for a refined search", "refund": "Chose the refund"}


def _constraints(c: dict) -> str:
    parts = []
    if c.get("exclude_tags"):
        parts.append("avoid " + ", ".join(c["exclude_tags"]))
    if c.get("exclude_avail_ids"):
        parts.append("exclude " + ", ".join(c["exclude_avail_ids"]))
    if c.get("only_dayparts"):
        parts.append("only " + ", ".join(c["only_dayparts"]))
    return "; ".join(parts) or "no extra constraints"


def _detail(node: str, detail: str) -> str:
    if node == "parse_request":
        detail = re.sub(r"\.?\s*Via .*$", "", detail)
        if detail.startswith("Open question"):
            return "A question about make-goods performance"
        m = re.match(r"(Refine the make-good for \S+): (\{.*\})$", detail)
        if m:
            try:
                return f"{m.group(1)}: {_constraints(ast.literal_eval(m.group(2)))}"
            except (ValueError, SyntaxError):
                pass
    elif node == "map_audience":
        return re.sub(r"\(([a-z_]+),", lambda m: f"({AUDIENCE_MATCH.get(m.group(1), m.group(1).replace('_', ' '))},", detail)
    elif node == "human_review":  # "<action> by <reviewer>"
        action, _, who = detail.partition(" by ")
        label = REVIEW_ACTION.get(action, action.capitalize())
        return label + (f" ({who})" if who and who not in ("None", "reviewer") else "")
    elif node == "format_recommendation":
        return "Options ranked by the make-good rules"
    elif node in ("draft_offer", "parse_client_reply"):
        return _scrub(re.sub(r";?\s*(wording by )?AI\b[^;]*", "", _scrub(detail)))
    elif node == "explain_recommendation":
        m = re.search(r"guardrail (passed|flagged (.+))$", detail)
        if m and m.group(1) == "passed":
            return "Checked that every avail it cites is one of the ranked options"
        if m:
            return f"Flagged avails that aren't among the options: {m.group(2)}"
        return ""
    elif node == "ask_genie":  # "<space> · <status> · <n> rows[ · <error>]"
        parts = detail.split(" · ")
        if len(parts) > 3:
            return "Couldn't answer from the analytics: " + _scrub(" · ".join(parts[3:]))
        rows = re.search(r"(\d+) rows", detail)
        return f"{rows.group(1)} rows from the make-goods analytics" if rows else ""
    return _scrub(detail)


def step(item: dict) -> dict:
    """One decision-trail step in business wording (node and structured fields are kept for the UI's logic)."""
    node = item.get("node", "")
    title = TITLES.get(node) or _scrub(item.get("title", ""))
    return {**item, "title": title, "detail": _detail(node, item.get("detail") or "")}


def text(markdown: str) -> str:
    """Agent messages: the recommendation text is already business language; only the analytics fallbacks name a product."""
    return (markdown.replace("Genie couldn't answer that:", "I couldn't answer that from the make-goods analytics:")
            .replace("Genie returned no answer.", "No answer came back for that question."))


def error(e: Exception) -> str:
    """A failure as the person sees it."""
    if isinstance(e, PermissionError):
        return "You don't have access to that client's make-goods."
    return "Something went wrong while working on this make-good. Try again, or start a new conversation."
