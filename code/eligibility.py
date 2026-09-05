# Eligibility layer — the VisaLens analysis, resolved for a specific candidate.
#
# Deterministically detects work-authorization and location signals in a
# posting, then resolves them against the CANDIDATE profile so the radar can
# say not just "is this a good role" but "can / should I actually apply".
#
# Fully explainable: every verdict comes with the reasons that produced it.
# No LLM involved. The same code yields different verdicts for a different
# profile (a US citizen clears a citizenship requirement; an F-1 student does
# not), because it reads the profile's AUTHORIZED_FOR capability set.

from __future__ import annotations

import re
from typing import Any

from .filters import normalize
from .profile import AUTHORIZED_FOR, CANDIDATE

# Display order = severity order (worst-to-act-on last for "edge" advantage).
ELIGIBILITY_LABELS = [
    "eligible_edge",      # you qualify for a restricted role many can't
    "eligible",           # clean apply
    "needs_review",       # ambiguous authorization language
    "location_mismatch",  # outside your target locations
    "likely_blocked",     # hard requirement you don't meet
]

ELIGIBILITY_TITLES = {
    "eligible_edge": "Eligible · edge",
    "eligible": "Eligible",
    "needs_review": "Review eligibility",
    "location_mismatch": "Out of region",
    "likely_blocked": "Likely blocked",
}

# ── Location classification ──────────────────────────────────────────────
_US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
_US_MARKERS = {"united states", "usa", "u.s.", "u.s.a", "u.s.a.", "us"}
_US_STATE_NAMES = {
    "california", "new york", "texas", "washington", "massachusetts",
    "illinois", "georgia", "colorado", "florida", "virginia", "oregon",
    "pennsylvania", "north carolina", "new jersey", "arizona", "michigan",
}
_CANADA_MARKERS = {
    "canada", "ontario", "quebec", "british columbia", "alberta", "toronto",
    "vancouver", "montreal", "ottawa", "waterloo", "ON", "QC", "BC",
}
_NON_US_MARKERS = {
    "united kingdom", "london", "ireland", "dublin", "germany", "berlin",
    "munich", "france", "paris", "netherlands", "amsterdam", "spain",
    "madrid", "barcelona", "poland", "warsaw", "sweden", "stockholm",
    "switzerland", "zurich", "israel", "tel aviv", "india", "bangalore",
    "bengaluru", "hyderabad", "pune", "gurgaon", "noida", "singapore",
    "australia", "sydney", "melbourne", "japan", "tokyo", "china", "beijing",
    "shanghai", "shenzhen", "hong kong", "taiwan", "korea", "seoul", "brazil",
    "sao paulo", "mexico", "mexico city", "argentina", "emea", "apac",
    "latam", "uk", "eu",
}


def _tokens(location: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,/|()]+", location or "") if t.strip()]


def classify_location(location: str, remote: int = 0) -> str:
    """Return one of: us, us_remote, remote, canada, non_us, unknown."""
    loc = normalize(location)
    toks = _tokens(location)
    is_remote = bool(remote) or "remote" in loc

    has_canada = (
        any(m in loc for m in ("canada", "ontario", "quebec", "british columbia",
                               "toronto", "vancouver", "montreal", "ottawa", "waterloo"))
        or any(t in {"ON", "QC", "BC"} for t in toks)
    )
    has_non_us = any(m in loc for m in _NON_US_MARKERS)
    has_us = (
        any(m in loc for m in _US_MARKERS)
        or any(name in loc for name in _US_STATE_NAMES)
        or any(t.upper() in _US_STATE_ABBR for t in toks)
    )

    if has_us:
        return "us_remote" if is_remote else "us"
    if has_canada:
        return "canada"
    if has_non_us:
        return "non_us"
    if is_remote:
        return "remote"        # unspecified-region remote; usually US on these boards
    return "unknown"


def _location_in_region(kind: str) -> tuple[bool, str]:
    """Resolve a location kind against the candidate's location policy."""
    mode = CANDIDATE["location_mode"]
    if kind in ("us", "us_remote"):
        return True, "in your US target region"
    if kind == "remote":
        return (CANDIDATE.get("remote_ok", True),
                "remote (region unspecified)")
    if kind == "canada":
        ok = mode in ("us_and_canada", "anywhere")
        return ok, "Canada"
    if kind == "non_us":
        return mode == "anywhere", "outside the US"
    return True, "location unconfirmed"  # unknown → don't penalize


# ── Work-authorization signal detection ──────────────────────────────────
# Each pattern maps to the capability a candidate must hold to clear it.
_AUTH_PATTERNS = [
    ("us_citizenship",
     re.compile(r"\b(u\.?s\.?\s*citizen(ship)?|must be a citizen|"
                r"citizenship (is )?required|require[sd]?[^.]{0,30}citizenship)\b", re.I),
     "U.S. citizenship required"),
    ("security_clearance",
     re.compile(r"\b(security clearance|active clearance|secret clearance|"
                r"ts/sci|top secret|polygraph)\b", re.I),
     "security clearance required"),
    ("us_work",
     re.compile(r"\b(no(t)?[^.]{0,20}sponsor\w*|without sponsorship|"
                r"unable to sponsor|do(es)? not[^.]{0,20}sponsor\w*|"
                r"sponsorship (is )?not (available|offered|provided))\b", re.I),
     "no visa sponsorship offered"),
    ("us_work",
     re.compile(r"\b(authorized to work|work authorization|"
                r"legally authorized to work|must be authorized)\b", re.I),
     "must be authorized to work in the U.S."),
]
_SPONSOR_OK = re.compile(
    r"\b(visa sponsorship (available|provided|offered)|will sponsor|"
    r"open to sponsor\w*|sponsorship available)\b", re.I,
)

_CAP_DESCRIPTIONS = {
    "us_citizenship": "U.S. citizenship",
    "security_clearance": "a security clearance",
    "us_work": "U.S. work authorization",
}

# ── GitHub-list marker flags ─────────────────────────────────────────────
# A posting parsed from a public list README has NO description — there is
# nothing for _AUTH_PATTERNS to match — so the list's own marker emoji are the
# only work-authorization evidence those rows ever carry. Only the two markers
# whose meaning the lists state are mapped: 🛂 (no sponsorship offered) and 🇺🇸
# (US citizenship required). Every other flag, including 🔥 (recently added),
# is deliberately absent and therefore cannot move a verdict — see
# filters.LIST_MARKERS for why unverified markers are not recorded at all.
_MARKER_CAPS = {
    "no_sponsorship": ("us_work", "no visa sponsorship offered (🛂 on the public list)"),
    "us_citizenship": ("us_citizenship", "U.S. citizenship required (🇺🇸 on the public list)"),
}


def _marker_flags(opp: dict[str, Any]) -> list[str]:
    """Parse opportunities.list_markers ('new,no_sponsorship') into flags."""
    return [f.strip() for f in (opp.get("list_markers") or "").split(",") if f.strip()]


def analyze(opp: dict[str, Any]) -> dict[str, Any]:
    """Return {eligibility_label, eligibility_reasons, location_kind}."""
    title = opp.get("title") or ""
    desc = (opp.get("description") or "")[:6000]
    haystack = f"{title}\n{desc}"
    authorized = AUTHORIZED_FOR.get(CANDIDATE["work_authorization"], set())
    reasons: list[str] = []

    # Location first — for a US-only candidate this is the common blocker.
    kind = classify_location(opp.get("location") or "", opp.get("remote") or 0)
    in_region, region_note = _location_in_region(kind)

    # Work-authorization requirements found in the posting.
    required_caps: list[tuple[str, str]] = []  # (capability, human label)
    seen_caps: set[str] = set()
    for cap, pattern, label in _AUTH_PATTERNS:
        if pattern.search(haystack) and cap not in seen_caps:
            seen_caps.add(cap)
            required_caps.append((cap, label))
    # Same requirements, sourced from the list markers instead of prose. Treated
    # exactly like a text match — including the eligible_edge path, since a 🇺🇸
    # role a citizen can take is the same advantage whether the requirement was
    # written out or flagged with an emoji.
    for flag in _marker_flags(opp):
        marker_cap = _MARKER_CAPS.get(flag)
        if marker_cap is None or marker_cap[0] in seen_caps:
            continue
        seen_caps.add(marker_cap[0])
        required_caps.append(marker_cap)

    blockers = [(cap, label) for cap, label in required_caps if cap not in authorized]
    advantages = [
        (cap, label) for cap, label in required_caps
        if cap in authorized and cap in {"us_citizenship", "security_clearance"}
    ]

    # Resolve to a single label, worst-binding wins.
    if not in_region:
        reasons.append(f"location {region_note} — outside your target region")
        # An out-of-region role you also can't be authorized for is still blocked.
        if blockers:
            reasons += [f"requires {_CAP_DESCRIPTIONS.get(c, c)}: {lbl}" for c, lbl in blockers]
            label = "likely_blocked"
        else:
            label = "location_mismatch"
    elif blockers:
        reasons += [
            f"requires {_CAP_DESCRIPTIONS.get(cap, cap)} you don't hold ({label})"
            for cap, label in blockers
        ]
        label = "likely_blocked"
    elif advantages:
        for cap, label_text in advantages:
            reasons.append(f"{label_text} — you qualify; far fewer eligible applicants")
        label = "eligible_edge"
    else:
        if required_caps:
            reasons.append("work-authorization language present — you clear it")
        if _SPONSOR_OK.search(haystack):
            reasons.append("employer offers visa sponsorship")
        if kind in ("us", "us_remote", "remote"):
            reasons.append(region_note)
        elif kind == "unknown":
            reasons.append("location unconfirmed — verify before applying")
        label = "eligible"

    return {
        "eligibility_label": label,
        "eligibility_reasons": reasons,
        "location_kind": kind,
    }
