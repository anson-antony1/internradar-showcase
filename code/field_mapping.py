# Application field mapping + safety classification.
#
# Defines which fields are SAFE to auto-fill (with a confidence + source), which
# are SENSITIVE and must be left for the human, and how to recognize a field on
# a real page from its label/name. Nothing here ever decides to *submit*.

from __future__ import annotations

import re
from typing import Any, Optional

CONFIDENCE_FILL = 0.75   # at/above -> fill; below -> needs_review

# key -> (label, profile source, base confidence, page-match substrings)
SAFE_FIELDS = [
    ("first_name", "First name", "profile.first_name", 0.97,
     ["first name", "firstname", "first_name", "given name", "fname"]),
    ("last_name", "Last name", "profile.last_name", 0.92,
     ["last name", "lastname", "last_name", "family name", "surname", "lname"]),
    ("full_name", "Full name", "profile.full_name", 0.9,
     ["full name", "your name", "name"]),
    ("email", "Email", "profile.email", 0.97,
     ["email", "e-mail"]),
    ("phone", "Phone", "profile.phone", 0.9,
     ["phone", "mobile", "telephone", "cell"]),
    ("school", "School", "profile.school", 0.95,
     ["school", "university", "college", "institution"]),
    ("major", "Major", "profile.major", 0.9,
     ["major", "field of study", "discipline", "concentration"]),
    ("degree", "Degree", "profile.degree", 0.85,
     ["degree", "level of education", "education level"]),
    ("graduation", "Graduation date", "profile.graduation", 0.9,
     ["graduation", "grad date", "expected graduation", "completion date",
      "end date"]),
    ("work_authorization", "Work authorization", "profile.work_authorization", 0.9,
     ["authorized to work", "work authorization", "legally authorized",
      "eligible to work"]),
    ("sponsorship", "Require sponsorship", "profile.needs_sponsorship", 0.92,
     ["sponsorship", "require sponsorship", "need sponsorship", "visa support"]),
    ("location_preference", "Location preference", "profile.location_preference", 0.7,
     ["location preference", "preferred location", "willing to relocate",
      "work location"]),
    ("linkedin", "LinkedIn", "profile.linkedin", 0.95,
     ["linkedin"]),
    ("github", "GitHub", "profile.github", 0.95,
     ["github", "git hub"]),
    ("portfolio", "Portfolio / Website", "profile.portfolio", 0.82,
     ["portfolio", "website", "personal site", "url"]),
    ("location", "Location (city)", "profile.location", 0.8,
     ["current location", "location (city)", "current city", "city/town",
      "city, state"]),
    ("gpa", "GPA", "profile.gpa", 0.85,
     ["gpa", "grade point"]),
]

# Categories that must be left to the human (default: do NOT fill).
SENSITIVE_PATTERNS = [
    ("demographic", r"(\b(gender|sex|race|ethnic|ethnicity|hispanic|latino|"
                    r"national origin|sexual orientation|pronoun)\b|eeoc?|"
                    r"_systemfield_eeoc)"),
    ("veteran", r"\b(veteran|military|armed forces|protected veteran)\b"),
    ("disability", r"\b(disabilit\w*|disabled|accommodation)\b"),
    ("legal_ack", r"\b(certify|acknowledge|agree|consent|i confirm|terms|"
                  r"truthful|accurate to the best|e-signature|signature|"
                  r"electronic signature)\b"),
    ("salary", r"\b(salary|compensation|desired pay|expected pay|pay expectation|"
               r"hourly rate)\b"),
    ("criminal", r"\b(criminal|felony|conviction|background check)\b"),
    # Immigration status is a legal attestation, and the value the agent had for it
    # was never entered by the user: store._defaults() ships
    # work_authorization="U.S. citizen / permanent resident" and
    # needs_sponsorship=False as BASELINE defaults, which store.save_profile cannot
    # even clear (it skips empty values on merge). Those were classified safe at
    # 0.90/0.92 against a 0.75 fill threshold, so "Are you legally authorized to
    # work in the United States?" was auto-answered on a real application from a
    # default. Tellingly, "Are you a U.S. citizen?" already deferred to review, so
    # the old behaviour was inconsistent as well as wrong.
    ("citizenship", r"\b(citizen|citizenship|work authorization|authorized to work|"
                    r"sponsorship|require sponsorship|need sponsorship|visa|"
                    r"work permit|right to work|immigration|h-?1b|opt|cpt|ead|"
                    r"green card|permanent resident)\b"),
    ("relocation_unclear", r"\b(relocat)\b"),
    ("long_form", r"\b(cover letter|why do you|tell us|describe|in your own words|"
                  r"essay|additional information)\b"),
]
_SENSITIVE = [(cat, re.compile(p, re.I)) for cat, p in SENSITIVE_PATTERNS]

# Fields that look fillable but must NOT be guessed (avoid e.g. mapping
# "Company name" -> your full name). Always left for manual review.
_AMBIGUOUS = re.compile(
    r"\b(company|employer|organization|organisation|reference|emergency|"
    r"how did you hear|where did you hear|referr?al source|other name|"
    r"preferred name|pronoun)\b", re.I,
)


def _profile_value(profile: dict[str, Any], key: str) -> Optional[str]:
    if key == "full_name":
        full = f"{profile.get('first_name','')} {profile.get('last_name','')}".strip()
        return full or None
    if key == "sponsorship":
        # Truthful, profile-driven: do you REQUIRE sponsorship?
        return "No" if not profile.get("needs_sponsorship") else "Yes"
    if key == "work_authorization":
        return profile.get("work_authorization") or None
    val = profile.get(key)
    if isinstance(val, bool):
        return "Yes" if val else "No"
    return str(val).strip() if val not in (None, "") else None


def _is_placeholder(value: Optional[str]) -> bool:
    return bool(value and value.strip().startswith("[") and value.strip().endswith("]"))


def resolve_fields(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Compute the planned fill for every safe field, with confidence + source
    + status (filled | needs_review | manual_only)."""
    out: list[dict[str, Any]] = []
    for key, label, source, conf, _match in SAFE_FIELDS:
        value = _profile_value(profile, key)
        if value is None or value == "":
            out.append({
                "field_key": key, "field_label": label, "value": "",
                "confidence": 0.0, "source": source, "status": "manual_only",
                "note": "not set in your profile — fill manually or add it to your profile",
            })
            continue
        if _is_placeholder(value):
            out.append({
                "field_key": key, "field_label": label, "value": value,
                "confidence": 0.4, "source": source, "status": "needs_review",
                "note": "placeholder — set the real value in your profile",
            })
            continue
        status = "filled" if conf >= CONFIDENCE_FILL else "needs_review"
        out.append({
            "field_key": key, "field_label": label, "value": value,
            "confidence": conf, "source": source, "status": status,
        })
    return out


def match_field_key(label: str) -> Optional[str]:
    """Map a page field's label/name to a safe field key, or None."""
    low = (label or "").lower()
    # Most specific first: longer match substrings win.
    best: tuple[int, Optional[str]] = (0, None)
    for key, _label, _src, _conf, matches in SAFE_FIELDS:
        for m in matches:
            if m in low and len(m) > best[0]:
                best = (len(m), key)
    return best[1]


def classify_label(label: str) -> dict[str, str]:
    """Classify a page field: safe (mapped key), sensitive (category), or unknown."""
    for cat, rx in _SENSITIVE:
        if rx.search(label or ""):
            return {"kind": "sensitive", "category": cat}
    if _AMBIGUOUS.search(label or ""):
        return {"kind": "unknown"}
    key = match_field_key(label)
    if key:
        return {"kind": "safe", "key": key}
    return {"kind": "unknown"}


# key -> base confidence + source, for adapters that map by name/id.
SAFE_BY_KEY = {key: {"label": label, "source": source, "confidence": conf}
               for key, label, source, conf, _m in SAFE_FIELDS}
