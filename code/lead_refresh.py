# Bounded, scheduled re-verification of stale leads.
#
# WHY. lead_freshness made staleness visible and measured the gap: 18 of 28
# `verified` leads had not been re-checked in 44-45 days, and NOTHING re-verified
# them. _discovery_pass already re-verifies `extracted` and `discovered` leads
# (10 each per cycle) — the statuses that carry no legitimacy claim — while the
# statuses that DO claim "this is a real award" were never revisited. This closes
# exactly that gap and nothing wider.
#
# WHAT IT IS NOT. Not a generic job framework, not a queue table, and not a second
# definition of staleness: eligibility comes from lead_freshness, which in turn
# reuses the scholarship verification cadence. One definition, three consumers.
#
# SCOPE OF STATUS, decided from measured data rather than by listing every status:
#   verified        REFRESHED — the only status asserting legitimacy, and the only
#                   one whose staleness misleads a user. 28 rows, 18 stale.
#   extracted /     already re-verified every cycle by _discovery_pass; adding them
#   discovered      here would double the request volume for nothing.
#   recurring_watch has its own dedicated pass (lead_verifier.reactivate_recurring),
#                   which reopens a template only on its own fresh evidence.
#   needs_review    left MANUAL. The status means a human should look, and it is
#                   where blocked/CAPTCHA pages land — re-fetching them on a
#                   schedule is how you hammer a site that already said no.
#   stale           left manual for the same reason: it is a conclusion, not a gap.
#   added / rejected / duplicate  terminal. verify() already refuses them; excluded
#                   here too so a refresh can never resurrect a decision.

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any, Optional

from services.scholar.filters import days_until, host_of
from . import lead_freshness, lead_store, lead_verifier

# Statuses this scheduler will touch. Deliberately one entry — see the header.
REFRESHABLE_STATUSES = ("verified",)

# Per-cycle bound. The operator wakes hourly, so 3/cycle is 72/day against a
# measured backlog of 18 and a steady state of ~18/week: generous headroom while
# still clearing the backlog in about six cycles.
DEFAULT_LIMIT = 3
# At most one request per issuer host per cycle. This is the guard that stops ten
# Florida factsheets — all on floridastudentfinancialaidsg.org — from becoming ten
# rapid requests to one host the moment their 7-day window elapses together.
DEFAULT_PER_HOST = 1

# Backoff after consecutive unproductive attempts, derived from lead_verifications
# rather than stored: attempt N waits 2^N days, capped. Without this a permanently
# unreachable lead is retried every cycle forever.
BACKOFF_BASE_DAYS = 1
BACKOFF_CAP_DAYS = 14
# Outcomes that mean "we did not learn anything about this award". Everything else
# (ok / changed / document / dead / stale) is a real reading of the source.
UNPRODUCTIVE_OUTCOMES = ("blocked", "unfetched", "error")


def _attempt_history(lead_id: int) -> tuple[int, Optional[date]]:
    """(consecutive unproductive attempts, date of the most recent attempt).

    Read from lead_verifications, which already records one row per attempt with
    an outcome and a timestamp — so no next_attempt_at column is needed. Proving
    that first was the point: a new column would have to be migrated, backfilled
    and kept in sync with a history table that already holds the same facts.
    """
    rows = lead_store.list_lead_verifications(lead_id, limit=20)
    if not rows:
        return 0, None
    # list_lead_verifications returns newest-first; count the leading run of
    # unproductive outcomes and stop at the first real reading.
    streak = 0
    for r in rows:
        if (r.get("outcome") or "") in UNPRODUCTIVE_OUTCOMES:
            streak += 1
        else:
            break
    last = None
    try:
        last = datetime.fromisoformat(
            str(rows[0].get("created_at") or "").split(".")[0][:19]).date()
    except ValueError:
        last = None
    return streak, last


def _backoff_days(streak: int) -> int:
    if streak <= 0:
        return 0
    return min(BACKOFF_CAP_DAYS, BACKOFF_BASE_DAYS * (2 ** (streak - 1)))


def _priority(lead: dict[str, Any], fresh: dict[str, Any], today: date) -> tuple:
    """Deterministic sort key; lower sorts first.

    Band 1 — stale AND a deadline inside 14 days: the only case where being out of
             date can cost the user the award.
    Band 2 — stale with terms unconfirmed: we cannot even say whether it is open.
    Band 3 — stale but terms known and not urgent.
    Within a band: oldest evidence first, then id, so the order is total and does
    not depend on dict iteration or SQL row order.
    """
    du = days_until(lead.get("extracted_deadline"), today)
    if du is not None and 0 <= du <= lead_freshness.NEAR_DEADLINE_WINDOW_DAYS:
        band = 1
    elif not fresh["terms_confirmed"]:
        band = 2
    else:
        band = 3
    age = fresh["verification_age_days"]
    # Never-verified sorts before any dated row inside its band.
    return (band, 0 if age is None else 1, -(age or 0), lead["id"])


def select_due(limit: int = DEFAULT_LIMIT, per_host: int = DEFAULT_PER_HOST,
               today: Optional[date] = None,
               statuses: tuple[str, ...] = REFRESHABLE_STATUSES) -> list[dict[str, Any]]:
    """The leads this cycle should re-verify, in priority order.

    Status filtering happens in SQL so the hourly hot path never walks the 2,000+
    duplicate rows; freshness, backoff and host spreading are applied in Python
    because each needs derived values SQL does not hold.
    """
    today = today or date.today()
    pool: list[dict[str, Any]] = []
    for st in statuses:
        pool.extend(lead_store.list_leads(status=st, limit=500))

    scored: list[tuple[tuple, dict[str, Any]]] = []
    for lead in pool:
        fresh = lead_freshness.verification_freshness(lead, today)
        if fresh["verification_fresh"]:
            continue
        if fresh["legitimacy"] in ("rejected", "duplicate"):
            continue  # belt and braces; SQL already excluded these statuses
        streak, last_attempt = _attempt_history(lead["id"])
        wait = _backoff_days(streak)
        if wait and last_attempt is not None:
            # lead_verifications.created_at is stamped by SQLite with the real
            # wall clock, while `today` is injectable — so the difference can come
            # out NEGATIVE (an injected past date, or clock skew). Treating that as
            # "inside the window" would back the lead off forever and silently stop
            # refreshing it, so only a genuinely elapsed-but-insufficient gap
            # defers. Fail open: refreshing one lead early is cheap, never
            # refreshing it again is not.
            elapsed = (today - last_attempt).days
            if 0 <= elapsed < wait:
                continue  # still inside its backoff window
        lead["_refresh_backoff_days"] = wait
        lead["_refresh_failure_streak"] = streak
        scored.append((_priority(lead, fresh, today), lead))

    scored.sort(key=lambda pair: pair[0])
    chosen: list[dict[str, Any]] = []
    host_used: dict[str, int] = {}
    for _key, lead in scored:
        if len(chosen) >= limit:
            break
        host = host_of(lead.get("official_url") or lead.get("candidate_url")
                       or lead.get("source_url")) or ""
        if host_used.get(host, 0) >= per_host:
            continue  # another issuer gets this slot; this one waits a cycle
        host_used[host] = host_used.get(host, 0) + 1
        chosen.append(lead)
    return chosen


def refresh_stale_leads(limit: int = DEFAULT_LIMIT, *, dry_run: bool = False,
                        lead_id: Optional[int] = None,
                        per_host: int = DEFAULT_PER_HOST,
                        today: Optional[date] = None) -> dict[str, Any]:
    """Re-verify up to `limit` stale leads. Never raises for one lead's failure.

    Legitimacy is never withdrawn here. verify() decides the status from its own
    evidence, and a transient failure leaves the lead exactly as it was — the only
    thing this function adds is WHEN that check happens.
    """
    started = time.time()
    today = today or date.today()
    summary = {"eligible": 0, "attempted": 0, "refreshed": 0, "unchanged": 0,
               "needs_review": 0, "transient_failures": 0, "permanent_failures": 0,
               "skipped": 0, "identity_conflicts": 0, "dry_run": dry_run,
               "leads": [], "duration_ms": 0}

    if lead_id is not None:
        one = lead_store.get_lead(lead_id)
        if not one:
            raise LookupError("Lead not found")
        due = [one]
        summary["eligible"] = 1
    else:
        due = select_due(limit=limit, per_host=per_host, today=today)
        # `eligible` counts everything that WOULD be due ignoring the cap, so a
        # caller can see the backlog rather than only what one cycle took.
        summary["eligible"] = len(select_due(limit=10_000, per_host=10_000,
                                            today=today))

    for lead in due:
        before_status = lead["status"]
        entry = {"id": lead["id"], "title": (lead.get("title") or "")[:60],
                 "before": before_status, "after": before_status, "outcome": None}
        if dry_run:
            entry["outcome"] = "dry-run (not fetched)"
            summary["skipped"] += 1
            summary["leads"].append(entry)
            continue
        try:
            # No write transaction is held across the network call: verify() opens
            # and closes its own connections, so a slow fetch cannot block a
            # concurrent reader or the next cycle.
            after = lead_verifier.verify(lead["id"], today=today,
                                         preserve_legitimacy=True)
            summary["attempted"] += 1
            entry["after"] = after["status"]
            hist = lead_store.list_lead_verifications(lead["id"], limit=1)
            outcome = (hist[0].get("outcome") if hist else None) or "unknown"
            entry["outcome"] = outcome
            if outcome in UNPRODUCTIVE_OUTCOMES:
                summary["transient_failures"] += 1
            elif outcome == "dead":
                summary["permanent_failures"] += 1
            elif after["status"] == before_status:
                summary["unchanged"] += 1
            else:
                summary["refreshed"] += 1
            if after["status"] == "needs_review" and before_status != "needs_review":
                # A legitimate award that stopped verifying is a REVIEW item, never
                # an automatic rejection.
                summary["needs_review"] += 1
        except Exception as exc:
            # One lead must not abort the batch, and a thrown fetch is transient
            # by default: nothing about the award changed, only our ability to see
            # it. Recorded so the attempt is auditable.
            summary["attempted"] += 1
            summary["transient_failures"] += 1
            entry["outcome"] = "exception: %s" % type(exc).__name__
            lead_store.log_event(lead["id"], "error",
                                 "refresh attempt failed: %s" % str(exc)[:160])
        summary["leads"].append(entry)

    summary["duration_ms"] = int((time.time() - started) * 1000)
    return summary
