# Deterministic opportunity scoring: freshness, fit, season, eligibility,
# urgency. Every score is explainable — reasons travel with the numbers.

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from .eligibility import analyze as analyze_eligibility
from .filters import normalize
from .profile import (
    CORE_SKILLS,
    DEFAULT_SEASON_PRIORITY,
    FIT_TITLE_WEIGHTS,
    PENALTY_PATTERNS,
    SEASON_PRIORITY,
    SKILL_KEYWORDS,
)

SOURCE_OF_TRUTH_TYPES = {"greenhouse", "lever", "ashby", "custom"}


def _present(keyword: str, text: str) -> bool:
    """Whole-token match so 'ai'/'ml'/'go' don't match inside other words."""
    return re.search(
        r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])", text
    ) is not None


# Precompile title-weight matchers in descending weight order.
_TITLE_MATCHERS = sorted(FIT_TITLE_WEIGHTS.items(), key=lambda kv: -kv[1])


def _hours_since(iso_ts: Optional[str]) -> float:
    if not iso_ts:
        return 9999.0
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 3600)
    except ValueError:
        return 9999.0


def freshness_score(first_seen_at: Optional[str]) -> float:
    """Heavily favor roles found within the last 1 / 6 / 24 hours."""
    hours = _hours_since(first_seen_at)
    if hours <= 1:
        return 100
    if hours <= 6:
        return 90
    if hours <= 24:
        return 75
    if hours <= 72:
        return 50
    if hours <= 168:
        return 30
    return 10


def season_priority(season: Optional[str]) -> float:
    """0-100 priority for a detected cycle; neutral default when unknown."""
    if not season:
        return DEFAULT_SEASON_PRIORITY
    return SEASON_PRIORITY.get(season.strip().lower(), DEFAULT_SEASON_PRIORITY)


def fit_score(title: str, description: str = "") -> tuple[float, list[str]]:
    t = normalize(title)
    d = normalize(description)
    score = 30.0  # passed the internship+role filters to get here
    reasons: list[str] = []

    title_hits = [(kw, w) for kw, w in _TITLE_MATCHERS if _present(kw, t)]
    if title_hits:
        # Strongest match counts fully; extras add with diminishing returns.
        boost = title_hits[0][1] + sum(0.3 * w for _, w in title_hits[1:3])
        score += min(45.0, boost)
        reasons.append(f"title matches: {', '.join(kw for kw, _ in title_hits[:3])}")

    skill_hits = [kw for kw in SKILL_KEYWORDS if _present(kw, d)]
    if skill_hits:
        pts = sum(4 if kw in CORE_SKILLS else 2 for kw in skill_hits)
        score += min(25.0, pts)
        reasons.append(f"stack overlap: {', '.join(skill_hits[:6])}")

    for pattern, penalty, label in PENALTY_PATTERNS:
        if re.search(pattern, t) or re.search(pattern, d[:1500]):
            # An explicit intern title outweighs ambiguous penalty language.
            if "intern" in t and label in {"new-grad-only role", "PhD required"}:
                continue
            score -= penalty
            reasons.append(f"penalty: {label}")

    return max(0.0, min(100.0, score)), reasons


def urgency_score(
    freshness: float,
    fit: float,
    source_type: str,
    season: Optional[str],
    eligibility_label: str = "eligible",
) -> tuple[float, list[str]]:
    """Blend freshness, fit, cycle priority, source credibility, eligibility."""
    reasons: list[str] = []
    score = 0.45 * freshness + 0.40 * fit

    if source_type in SOURCE_OF_TRUTH_TYPES:
        score += 10
        reasons.append("direct source-of-truth posting")

    sp = season_priority(season)
    season_bonus = round((sp / 100) * 10, 1)
    score += season_bonus
    if season:
        reasons.append(f"cycle priority {round(sp)} ({season})")

    if eligibility_label == "likely_blocked":
        score -= 35
        reasons.append("eligibility: likely blocked")
    elif eligibility_label == "location_mismatch":
        score -= 25
        reasons.append("eligibility: out of your region")
    elif eligibility_label == "eligible_edge":
        score += 5
        reasons.append("eligibility: you uniquely qualify")

    # Fit/Match Score v2 (§5.17): user-accepted calibration deltas, scaled by how
    # strongly each component actually scored here. Imported lazily — calibration
    # reads SOURCE_OF_TRUTH_TYPES from this module, so a module-level import
    # would close the loop.
    from . import calibration
    adj_points, adj_reasons = calibration.apply_adjustments({
        "fit": fit,
        "freshness": freshness,
        "source_of_truth": 100.0 if source_type in SOURCE_OF_TRUTH_TYPES else 0.0,
        "season": sp,
        "eligibility_edge": 100.0 if eligibility_label == "eligible_edge" else 0.0,
    })
    score += adj_points
    reasons.extend(adj_reasons)

    return max(0.0, min(100.0, score)), reasons


def score_opportunity(opp: dict[str, Any]) -> dict[str, Any]:
    """Compute all scores + eligibility for a normalized opportunity dict."""
    fresh = freshness_score(opp.get("first_seen_at"))
    fit, fit_reasons = fit_score(opp.get("title") or "", opp.get("description") or "")
    elig = analyze_eligibility(opp)
    urgency, urgency_reasons = urgency_score(
        fresh, fit, opp.get("source_type") or "", opp.get("season"),
        elig["eligibility_label"],
    )
    return {
        "freshness_score": round(fresh, 1),
        "fit_score": round(fit, 1),
        "season_priority": round(season_priority(opp.get("season")), 1),
        "urgency_score": round(urgency, 1),
        "eligibility_label": elig["eligibility_label"],
        "eligibility_reasons": elig["eligibility_reasons"],
        "location_kind": elig["location_kind"],
        "score_reasons": fit_reasons + urgency_reasons,
    }
