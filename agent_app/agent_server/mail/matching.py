"""Match a reply to its offer and keep only what the client wrote.

The reply is untrusted text from outside the company: it is reduced to the client's own words (quoted history,
signatures after "--", and HTML removed), capped in length, and only ever read by the reply parser, which has
no tools and can only pick among the options that were offered.
"""
import html as html_lib
import re
from typing import Optional

MAX_CHARS = 4000
REF_RE = re.compile(r"\[MG-([A-Z2-7]{8})\]")
_QUOTE_START = [
    re.compile(r"^On .{0,200}wrote:\s*$", re.I | re.M),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.I | re.M),
    re.compile(r"^From:\s.+$", re.M),
    re.compile(r"^_{5,}", re.M),
]


def ref_from_subject(subject: str) -> Optional[str]:
    m = REF_RE.search(subject or "")
    return m.group(1) if m else None


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return html_lib.unescape(text)


def own_text(body: str, is_html: bool = False) -> str:
    """The client's new text: no quoted history, no quoted lines, no signature block, at most MAX_CHARS."""
    text = html_to_text(body) if is_html else body
    text = text.replace("\r\n", "\n")
    cut = len(text)
    for pat in _QUOTE_START:
        m = pat.search(text)
        if m:
            cut = min(cut, m.start())
    text = text[:cut]
    lines = [ln for ln in text.split("\n") if not ln.lstrip().startswith(">")]
    text = "\n".join(lines)
    text = re.split(r"\n--\s*\n", text, maxsplit=1)[0]
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_CHARS]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def labels_mentioned(text: str, offered: list[str]) -> list[str]:
    """Option letters the client names explicitly ("option B", "B works", "go with A and C"): a cross-check
    for the parser, never the decision itself."""
    found = []
    for label in offered:
        if re.search(rf"\b(option|package|choice)\s*{label}\b", text, re.I) or \
           re.search(rf"(^|[\s(\"'])({label})(\s+(works|is fine|please|sounds good|looks good)|[.!,)]|\s*$)", text, re.M):
            found.append(label)
    return found
