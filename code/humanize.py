# Removing machine-writing tells from anything a person other than the user reads.
#
# WHY THIS IS SHARED. This logic lived inside services/scholar/drafts.py and was
# called from exactly ONE place: the LLM essay path. Everything else that produces
# outbound text went out unscrubbed — recommender request emails a professor reads,
# interview thank-you notes a recruiter reads, referral and outreach messages, and
# even drafts.py's own deterministic templates, several of which contained em
# dashes themselves. Top level (beside llm.py and export_tools.py) because the
# callers span both scholar and radar, and neither package should import the other.
#
# SCOPE, deliberately narrow: surface punctuation and phrasing only. It never
# changes a fact, a number, a name, or a [TODO] the user still has to fill in. The
# user's OWN imported writing is not run through this either — their story bank
# keeps their words, em dashes and all.

from __future__ import annotations

import re


# Strip the most common machine-writing tells from composed output: em/en dashes,
# mechanical sentence openers, and a few stock phrases. Conservative — it never
# changes facts, only surface punctuation/phrasing.
# A dash BETWEEN NUMBERS is a range, not a machine-writing tell. Handled first and
# separately, because the general rule below turns a dash into ", " and would
# silently rewrite "in 1–2 sentences" as "in 1, 2 sentences" and "500–750 words"
# as "500, 750 words" — corrupting the user's own scaffolds and word targets.
_NUM_RANGE = re.compile(r"(?<=\d)\s*[—–]\s*(?=\d)")
_AI_DASH = re.compile(r"\s*[—–]\s*")
_AI_OPENERS = re.compile(
    r"(?:^|(?<=[.!?]\s))(?:In conclusion|In summary|To conclude|Overall|Firstly|Secondly|Thirdly|"
    r"Lastly|Moreover|Furthermore|Additionally|Notably|Importantly|Ultimately|In essence),?\s+",
    re.M)
_AI_PHRASES = re.compile(r"\b(?:in today'?s world|ever[- ]evolving|navigate the complexities of)\b,?\s*", re.I)
# Lexical swaps for tells the model keeps producing despite instructions. Order
# matters (longer/specific first); each preserves meaning, just removes the tell.
_AI_SWAPS = [
    (re.compile(r"\ba testament to\b", re.I), "a reflection of"),
    (re.compile(r"\btestament to\b", re.I), "reflection of"),
    (re.compile(r"\btestaments?\b", re.I), "reflection"),
    (re.compile(r"\bdelving into\b", re.I), "digging into"),
    (re.compile(r"\bdelved into\b", re.I), "dug into"),
    (re.compile(r"\bdelved\b", re.I), "dug in"),
    (re.compile(r"\bdelve into\b", re.I), "dig into"),
    (re.compile(r"\bdelve\b", re.I), "dig in"),
    (re.compile(r"\bstepping stones\b", re.I), "steps"),
    (re.compile(r"\bstepping stone\b", re.I), "step"),
    (re.compile(r"\bas I sit down to write[^,.]*,?\s*", re.I), ""),
    (re.compile(r"\bfrom a young age,?\s*", re.I), ""),
    (re.compile(r"\bever since I was[^,.]*,?\s*", re.I), ""),
    (re.compile(r"\bwell[- ]rounded\b", re.I), "capable"),
    (re.compile(r"\bmultifaceted\b", re.I), "varied"),
    (re.compile(r"\b(?:rich|vibrant|intricate)\s+tapestry\s+of\b", re.I), "mix of"),
    (re.compile(r"\btapestry\s+of\b", re.I), "mix of"),
    (re.compile(r"\btapestry\b", re.I), "mix"),
    (re.compile(r"\bbeacon\s+of\b", re.I), "source of"),
    (re.compile(r"\bbeacons?\b", re.I), "source"),
]


def _strip_ai_tells(text: str) -> str:
    if not text:
        return text
    text = _NUM_RANGE.sub("-", text)
    text = _AI_DASH.sub(", ", text)
    text = _AI_OPENERS.sub("", text)
    text = _AI_PHRASES.sub("", text)
    for rx, rep in _AI_SWAPS:
        text = rx.sub(rep, text)
    text = re.sub(r",\s*,", ", ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" ,", ",", text)
    # Re-capitalize sentences that now start lowercase (after an opener was removed).
    text = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
    return text.strip()


# Public name. `_strip_ai_tells` stays as an alias so drafts.py's existing call
# site keeps working unchanged.
def strip_ai_tells(text: str) -> str:
    """Remove machine-writing tells from text a third party will read."""
    return _strip_ai_tells(text)


def strip_dashes(text: str) -> str:
    """Dash normalization ONLY: no capitalization, no phrase swaps.

    For short fragments where the full scrubber would do damage. A resume skill
    list contains things like "iOS" and "eBPF", and strip_ai_tells' sentence-casing
    would render those "IOS" and "EBPF" on a finished document. Numeric ranges are
    preserved as hyphens, same as the full pass.
    """
    if not text:
        return text
    text = _NUM_RANGE.sub("-", text)
    return _AI_DASH.sub(", ", text)
