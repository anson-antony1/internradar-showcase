# Duplicate Opportunity Cleanup + Canonicalization.
#
# InternRadar pulls the same role from many places: a company's own ATS
# (Greenhouse/Lever/Ashby), several GitHub "list" repos, and aggregators. The
# same Palantir or GenBio internship can therefore show up 3-13 times, drowning
# the Today plan and the Action Queue in repeats.
#
# This module finds those duplicates *deterministically* (no LLM), picks the
# single best "canonical" posting for each group, and records the grouping so
# the user-facing lists can show the canonical once and collapse the rest behind
# a "+N duplicates hidden" note. It NEVER deletes a row — every raw posting stays
# in `opportunities`; duplicates are only marked (canonical_opportunity_id +
# duplicate_reason/confidence) and mirrored into `opportunity_duplicates`.
#
# Detection signals (any one is enough to link two postings at the same company):
#   1. identical ATS job id parsed from the apply URL   (strongest)
#   2. identical normalized apply URL
#   3. same normalized title + compatible location
#   4. high title-token similarity + a corroborating signal (location or host)
# Hard blockers that prevent a false merge:
#   - different explicit season/cycle (Summer 2026 vs Summer 2027)
#   - conflicting level/track tokens (Undergrad vs Masters, II vs III, ...)
#
# Fully testable: the detection + canonical-selection logic is pure (operates on
# plain dicts); only run_dedupe / report touch the database.

from __future__ import annotations

import re
from typing import Any, Optional

from .db import get_conn, init_db, rows_to_dicts
from .filters import normalize
from .scoring import SOURCE_OF_TRUTH_TYPES

# Visible-row predicate shared by every user-facing list: show a posting if it
# is canonical/unique (no canonical pointer) OR the user already tracked it (so
# a duplicate the user is actively working on is never silently hidden).
# `{opp}` is the opportunities-table reference for the query (alias or name).
def visible_predicate(opp: str = "opportunities") -> str:
    """SQL predicate for "show this row to the user".

    A row is visible when it is not collapsed behind a canonical, OR when the
    user has already tracked it — a duplicate you applied to must never vanish
    from your own board just because dedupe later picked a different canonical.

    THE INVARIANT, since it is enforced by convention rather than by the schema:
    every user-facing opportunity list composes this in. router.py builds
    _VIEW_FILTERS as "<base filter> AND _CANON" (router.py:37) so all views
    inherit it, and today.py / the action queue additionally consult
    duplicates_hidden_map(). The deliberate exceptions are the /stats aggregates
    and job_sources.opportunity_count, which count every active row including
    collapsed duplicates: they answer "how much did this source yield", not
    "what should I look at". A new list endpoint that forgets this predicate will
    silently surface collapsed rows — including the handful that keep a
    pre-migration fingerprint after losing collision resolution in
    `radar_cli.py strip-markers`, which are hidden by exactly this mechanism and
    nothing else.
    """
    return (
        f"({opp}.canonical_opportunity_id IS NULL "
        f"OR EXISTS (SELECT 1 FROM applications ap "
        f"WHERE ap.opportunity_id = {opp}.id))"
    )


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

# Corporate suffixes / decorations stripped when blocking by company so
# "Apex" == "Apex Technology, Inc." and "🔥 Palantir" == "Palantir".
_COMPANY_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "gmbh", "plc", "sa", "ag", "labs", "lab", "technologies",
    "technology", "tech", "holdings", "group", "ai", "io", "hq",
}
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")


def normalize_company(name: Optional[str]) -> str:
    """Loose company key for *blocking* (grouping candidates), e.g.
    '🔥 Apex Technology, Inc.' -> 'apex'. Deliberately aggressive: the pairwise
    duplicate test below is the real gate against false merges."""
    base = normalize(name)
    base = _NON_ALNUM.sub(" ", base)          # drop emoji / punctuation
    tokens = [t for t in base.split() if t]
    while tokens and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens) or base.strip()


# Query parameters that name the *referrer*, not the posting. Aggregators append
# them when they link out, so one Greenhouse page arrives as three distinct URLs
# (?utm_source=Simplify, ?utm_source=github-vansh-ouckah, bare) and rule 2 —
# "identical normalized apply URL", the second-strongest signal we have — never
# fires on exactly the cross-source repeats it exists to catch.
# Matched by exact name plus the utm_* family. Everything else in the query is
# KEPT, because on several ATS/HRIS hosts the query *is* the job identity
# (…/careers?gh_jid=6123456, Workday's …?jobId=R244387): dropping the whole
# query string, as this used to, collapses every posting on such a board into
# one URL and would merge unrelated requisitions.
_TRACKING_PARAMS = {
    "ref", "referrer", "referer", "source", "src", "gh_src", "gclid", "fbclid",
    "msclkid", "trk", "trackingid", "mc_cid", "mc_eid", "_ga", "_hsenc",
}

# The same Greenhouse board is served under two hostnames — legacy
# boards.greenhouse.io and current job-boards.greenhouse.io — and rows scraped
# at different times carry different ones for the byte-identical page. Fold them
# so rule 2 and rule 4's host comparison see one board rather than two. The
# optional middle group keeps the regional boards paired the same way
# (job-boards.eu.greenhouse.io <-> boards.eu.greenhouse.io, as IMC uses).
_GH_HOST_ALIAS = re.compile(r"^job-boards(\.[a-z]{2})?\.greenhouse\.io")


def _strip_tracking(query: str) -> str:
    """Keep only the query params that can be part of a posting's identity."""
    kept = []
    for part in query.split("&"):
        if not part:
            continue
        name = part.split("=", 1)[0]
        if name.startswith("utm_") or name in _TRACKING_PARAMS:
            continue
        kept.append(part)
    # Param order is not identity: '?a=1&b=2' and '?b=2&a=1' are one page, and
    # different scrapers emit different orders.
    return "&".join(sorted(kept))


def normalize_url(url: Optional[str]) -> str:
    """Canonical apply URL for equality: drop scheme, 'www.', fragment, tracking
    params, trailing slash, and ATS-form noise like '/apply' or '/embed'. An
    identity-bearing query string survives (see _TRACKING_PARAMS)."""
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("#", 1)[0]
    u = _GH_HOST_ALIAS.sub(lambda m: "boards%s.greenhouse.io" % (m.group(1) or ""), u)
    path, _, query = u.partition("?")
    path = re.sub(r"/(apply|embed)(/)?$", "", path).rstrip("/")
    query = _strip_tracking(query)
    return path + "?" + query if query else path


_GH_PATH_ID = re.compile(r"greenhouse\.io/.*?(?:/jobs/|for_)(\d+)")
# gh_jid is deliberately host-agnostic: a company that embeds its Greenhouse
# board on its own careers page links to 'acme.com/careers?gh_jid=6123456', with
# no greenhouse.io anywhere in the URL. Requiring the greenhouse.io host (as the
# single combined pattern did) hid the strongest signal we have — the requisition
# id — for precisely the postings most likely to be scraped from two places.
_GH_QUERY_ID = re.compile(r"[?&]gh_jid=(\d+)")
# Greenhouse's embeddable application form: '…/embed/job_app?token=<job id>'. The
# host IS required here, unlike gh_jid — 'token' is a generic parameter name
# elsewhere and would happily match a session token on an unrelated site.
# Live example this recovers: Jump Trading posts the same requisition as
# 'boards.greenhouse.io/embed/job_app?token=8002989' on one list and
# 'jumptrading.com/hr/job?gh_jid=8002989' on another.
_GH_EMBED_ID = re.compile(r"greenhouse\.io/.*[?&]token=(\d+)")
_LEVER_ID = re.compile(r"lever\.co/[^/]+/([0-9a-f]{8}-[0-9a-f-]{27,})")
_ASHBY_ID = re.compile(r"ashbyhq\.com/[^/]+/([0-9a-f]{8}-[0-9a-f-]{27,})")


def ats_job_key(url: Optional[str]) -> str:
    """Stable ATS job identifier parsed from an apply URL, e.g. 'gh:6123456' or
    'ashby:<uuid>'. Two postings sharing this are the *same* job even if one was
    scraped from a GitHub list and the other pulled directly from the ATS;
    two postings carrying *different* ids on the same ATS are two different
    requisitions and must never merge (see duplicate_pair)."""
    if not url:
        return ""
    u = url.lower()
    for ats, rx in (("gh", _GH_PATH_ID), ("gh", _GH_QUERY_ID), ("gh", _GH_EMBED_ID),
                    ("lever", _LEVER_ID), ("ashby", _ASHBY_ID)):
        m = rx.search(u)
        if m:
            return f"{ats}:{m.group(1)}"
    return ""


def _apply_host(url: Optional[str]) -> str:
    """Bare host of an apply URL. Cut at '?' as well as '/': normalize_url now
    keeps a surviving query string, so a URL with a query and NO path —
    'https://stripe.com?gh_jid=1' — would otherwise yield a "host" of
    'stripe.com?gh_jid=1'. Not a cosmetic wart; both callers misread it:
      * rule 4 compares that against a plain 'stripe.com' and finds no
        corroborating host match, so two links to one board stop corroborating;
      * has_direct_apply substring-matches _AGGREGATOR_HOSTS, and query text is
        now inside the string it searches — 'acme.com?from=simplify.jobs' reads
        as an aggregator, demoting a company's own page in canonical selection."""
    return normalize_url(url).split("/", 1)[0].split("?", 1)[0] if url else ""


# Aggregator / list hosts that are not a company's own application page.
_AGGREGATOR_HOSTS = (
    "workatastartup.com", "github.com", "githubusercontent.com",
    "simplify.jobs", "linkedin.com", "indeed.com", "glassdoor.com",
    "google.com", "tracker", "wellfound.com",
    "intern-list.com", "jobright.ai",   # list/aggregator detail pages
)


def has_direct_apply(opp: dict[str, Any]) -> bool:
    """True if the apply URL looks like a real, directly-applyable posting page
    rather than an aggregator/list link."""
    host = _apply_host(opp.get("apply_url"))
    if not host:
        return False
    return not any(agg in host for agg in _AGGREGATOR_HOSTS)


# Tokens that don't help tell two roles apart (generic internship boilerplate).
_TITLE_STOP = {
    "intern", "interns", "internship", "internships", "co", "op", "coop",
    "student", "students", "program", "programme", "summer", "fall", "autumn",
    "spring", "winter", "the", "a", "an", "of", "for", "and", "to", "in", "at",
    "early", "career", "careers", "university", "college", "graduate",
    "undergraduate",  # level handled separately as a disambiguator
    "2024", "2025", "2026", "2027", "2028",
}

# Level / track tokens: if two titles carry *different* ones, they are different
# roles and must never be merged (e.g. Apple "Undergrad" vs "Masters").
_DISAMBIGUATORS = {
    "undergrad", "undergraduate", "bachelors", "bachelor", "bs",
    "masters", "master", "ms", "msc", "grad",
    "phd", "doctoral", "doctorate",
    "mba", "i", "ii", "iii", "iv", "v",
    "senior", "sr", "junior", "jr", "lead", "staff", "principal",
}

_WORD = re.compile(r"[a-z0-9+#]+")

# Collapse common split compounds so "Full Stack" == "Fullstack",
# "Front End" == "Frontend", etc. before tokenizing.
_COMPOUNDS = [
    (re.compile(r"\bfull[\s-]?stack\b"), "fullstack"),
    (re.compile(r"\bfront[\s-]?end\b"), "frontend"),
    (re.compile(r"\bback[\s-]?end\b"), "backend"),
]


def _stem(tok: str) -> str:
    """Crude stem so engineering/engineer/engineers and systems/system unify."""
    if len(tok) > 5 and tok.endswith("ing"):
        tok = tok[:-3]
    if len(tok) > 3 and tok.endswith("s"):
        tok = tok[:-1]
    return tok


def title_tokens(title: Optional[str]) -> set[str]:
    text = normalize(title)
    for rx, repl in _COMPOUNDS:
        text = rx.sub(repl, text)
    raw = _WORD.findall(text)
    return {_stem(t) for t in raw if t not in _TITLE_STOP and not t.isdigit()}


def _disambiguators(title: Optional[str]) -> set[str]:
    return {t for t in _WORD.findall(normalize(title)) if t in _DISAMBIGUATORS}


def title_similarity(a: Optional[str], b: Optional[str]) -> float:
    """Jaccard overlap of meaningful title tokens, 0..1."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


_LOC_NOISE = {
    "in", "the", "usa", "us", "united", "states", "of", "america", "remote",
    "hybrid", "onsite", "on", "site", "multiple", "locations", "various",
}


def _season_key(opp: dict[str, Any]) -> str:
    return normalize(opp.get("season"))


def _locations_compatible(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Lenient: a missing location never blocks; otherwise require equality,
    containment, both-remote, or a shared city token."""
    la, lb = normalize(a.get("location")), normalize(b.get("location"))
    if not la or not lb:
        return True
    if la == lb or la in lb or lb in la:
        return True
    if "remote" in la and "remote" in lb:
        return True
    ta = set(_WORD.findall(la)) - _LOC_NOISE
    tb = set(_WORD.findall(lb)) - _LOC_NOISE
    return bool(ta & tb)


# ---------------------------------------------------------------------------
# Pairwise duplicate decision (pure)
# ---------------------------------------------------------------------------

def duplicate_pair(a: dict[str, Any], b: dict[str, Any]) -> Optional[tuple[float, str]]:
    """Return (confidence, reason) if a and b are the same posting, else None.

    Assumes a and b are already in the same company block. Hard blockers run
    first so a strong-but-wrong signal can't override a real distinction."""
    # Hard blocker: explicitly different recruiting cycles.
    sa, sb = _season_key(a), _season_key(b)
    if sa and sb and sa != sb:
        return None
    # Hard blocker: conflicting level/track (Undergrad vs Masters, II vs III).
    da, db = _disambiguators(a.get("title")), _disambiguators(b.get("title"))
    if da and db and da != db:
        return None
    # Hard blocker: two different requisition ids on the SAME ATS. A company
    # opens one Lever/Greenhouse/Ashby row per requisition, so different ids mean
    # different jobs no matter how alike the titles read — and rules 3 and 4 read
    # only title + location + host, all of which Palantir's dozen
    # near-identically-titled Lever reqs share. This is a blocker rather than a
    # tiebreak because a false merge is the expensive direction: the loser is
    # collapsed out of Today and the Action Queue, so a live posting the user
    # could have applied to disappears.
    # Only same-ATS ids conflict. Two ids at *different* ATSes usually means one
    # side is a stale link left behind by a board migration, which is a genuine
    # duplicate; the rules below still have to earn that merge on title/location.
    ka, kb = ats_job_key(a.get("apply_url")), ats_job_key(b.get("apply_url"))
    if ka and kb and ka != kb and ka.split(":", 1)[0] == kb.split(":", 1)[0]:
        return None

    # 1) Same ATS job id parsed from the apply URL — strongest signal.
    if ka and ka == kb:
        return (0.98, f"same ATS job ({ka}) from different sources")

    # 2) Identical normalized apply URL.
    ua, ub = normalize_url(a.get("apply_url")), normalize_url(b.get("apply_url"))
    if ua and ua == ub:
        return (0.95, "identical apply URL")

    # 3) Same normalized title + compatible location.
    ta, tb = normalize(a.get("title")), normalize(b.get("title"))
    if ta and ta == tb and _locations_compatible(a, b):
        return (0.90, "same company, same title, compatible location")

    # 4) Near-identical title + a corroborating signal.
    sim = title_similarity(a.get("title"), b.get("title"))
    if sim >= 0.8 and (_locations_compatible(a, b)
                       or _apply_host(a.get("apply_url")) == _apply_host(b.get("apply_url")) != ""):
        return (round(0.70 + 0.25 * sim, 2),
                f"same company, near-identical title (token similarity {sim:.0%})")

    return None


# ---------------------------------------------------------------------------
# Grouping (pure)
# ---------------------------------------------------------------------------

def find_duplicate_groups(
    opps: list[dict[str, Any]],
) -> tuple[list[list[dict[str, Any]]], dict[int, tuple[float, str]]]:
    """Group opportunities into duplicate sets using union-find over the pairwise
    decision, blocking by normalized company so we never compare across firms.

    Returns (groups, best_edge) where each group is a list of >=2 opps and
    best_edge maps an opportunity id to the (confidence, reason) of its strongest
    link into the group (used for the per-row duplicate_reason)."""
    by_company: dict[str, list[dict[str, Any]]] = {}
    for opp in opps:
        by_company.setdefault(normalize_company(opp.get("company_name")), []).append(opp)

    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    best_edge: dict[int, tuple[float, str]] = {}

    def note(opp_id: int, conf: float, reason: str) -> None:
        if opp_id not in best_edge or conf > best_edge[opp_id][0]:
            best_edge[opp_id] = (conf, reason)

    for members in by_company.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                res = duplicate_pair(a, b)
                if res is None:
                    continue
                conf, reason = res
                union(a["id"], b["id"])
                note(a["id"], conf, reason)
                note(b["id"], conf, reason)

    clusters: dict[int, list[dict[str, Any]]] = {}
    id_to_opp = {o["id"]: o for o in opps}
    for opp in opps:
        if opp["id"] in parent:
            clusters.setdefault(find(opp["id"]), []).append(opp)

    groups = [members for members in clusters.values() if len(members) >= 2]
    # Deterministic order: largest groups first, then by lowest id.
    groups.sort(key=lambda g: (-len(g), min(o["id"] for o in g)))
    for g in groups:
        g.sort(key=lambda o: o["id"])
    return groups, best_edge


# ---------------------------------------------------------------------------
# Canonical selection (pure)
# ---------------------------------------------------------------------------

_ELIG_RANK = {
    "eligible_edge": 4, "eligible": 3, "needs_review": 2,
    "location_mismatch": 1, "likely_blocked": 0,
}


def _canonical_key(opp: dict[str, Any]) -> tuple:
    """Higher tuple == better canonical. Mirrors the documented priority order."""
    sot = 1 if (opp.get("source_type") or "") in SOURCE_OF_TRUTH_TYPES \
        or opp.get("is_source_of_truth") else 0
    direct = 1 if has_direct_apply(opp) else 0
    fresh = opp.get("first_seen_at") or ""           # ISO strings sort by recency
    elig = _ELIG_RANK.get(opp.get("eligibility_label") or "eligible", 2)
    urgency = float(opp.get("urgency_score") or 0)
    richness = len(opp.get("description") or "") + (10 if normalize(opp.get("location")) else 0)
    return (sot, direct, fresh, elig, urgency, richness, -int(opp["id"]))


# (criterion index in the key tuple, human reason). Index 0 == strongest.
_CRITERION_REASON = {
    0: "source-of-truth company ATS preferred over list/aggregator sources",
    1: "has a direct, applyable URL",
    2: "more recently discovered",
    3: "stronger eligibility status",
    4: "higher overall score",
    5: "richer description / location metadata",
}


def select_canonical(group: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    """Pick the best posting in a duplicate group and explain why it won."""
    ranked = sorted(group, key=_canonical_key, reverse=True)
    canonical, runner_up = ranked[0], ranked[1]
    ck, rk = _canonical_key(canonical), _canonical_key(runner_up)
    # Default when every ranked criterion ties (decided by the stable id
    # tiebreak): still explain the canonical's standing.
    if canonical.get("source_type") in SOURCE_OF_TRUTH_TYPES \
            or canonical.get("is_source_of_truth"):
        reason = "equally-ranked source-of-truth posting (earliest id)"
    else:
        reason = "best available posting in the group (earliest id)"
    for idx in range(len(_CRITERION_REASON)):
        if ck[idx] != rk[idx]:
            reason = _CRITERION_REASON[idx]
            sot_type = canonical.get("source_type")
            if idx == 0 and sot_type:
                reason = (f"source-of-truth ATS ({sot_type}) preferred over "
                          "list/aggregator sources")
            break
    return canonical, reason


# ---------------------------------------------------------------------------
# Database run + report
# ---------------------------------------------------------------------------

_LOAD_SQL = """
    SELECT id, company_name, title, location, season, apply_url, source_type,
           is_source_of_truth, first_seen_at, eligibility_label, urgency_score,
           fit_score, description
    FROM opportunities
    WHERE active = 1 AND status != 'hidden'
"""


def _summarize(opp: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": opp["id"],
        "company_name": opp.get("company_name"),
        "title": opp.get("title"),
        "location": opp.get("location"),
        "season": opp.get("season"),
        "source_type": opp.get("source_type"),
        "is_source_of_truth": opp.get("is_source_of_truth"),
        "apply_url": opp.get("apply_url"),
        "urgency_score": opp.get("urgency_score"),
        "eligibility_label": opp.get("eligibility_label"),
    }


def run_dedupe(dry_run: bool = False) -> dict[str, Any]:
    """Detect duplicates over the live DB, mark canonicals + duplicates, and
    rebuild the opportunity_duplicates audit table. Idempotent — re-running
    recomputes from the current opportunity set. Never deletes opportunities."""
    init_db()
    conn = get_conn()
    try:
        opps = rows_to_dicts(conn.execute(_LOAD_SQL).fetchall())
        groups, best_edge = find_duplicate_groups(opps)

        report_groups: list[dict[str, Any]] = []
        duplicates_hidden = 0
        for group in groups:
            canonical, why = select_canonical(group)
            group_id = f"dg-{canonical['id']}"
            dups = [o for o in group if o["id"] != canonical["id"]]
            duplicates_hidden += len(dups)
            dup_records = []
            for dup in dups:
                conf, reason = best_edge.get(
                    dup["id"], (0.75, "grouped with a near-identical posting")
                )
                dup_records.append({
                    **_summarize(dup), "confidence": conf, "reason": reason,
                })
            report_groups.append({
                "group_id": group_id,
                "size": len(group),
                "canonical": _summarize(canonical),
                "why_canonical": why,
                "duplicates": dup_records,
            })

        if not dry_run:
            _persist(conn, report_groups)
            conn.commit()

        return {
            "dry_run": dry_run,
            "num_groups": len(report_groups),
            "num_duplicates_hidden": duplicates_hidden,
            "num_opportunities": len(opps),
            "groups": report_groups,
        }
    finally:
        conn.close()


def _persist(conn, report_groups: list[dict[str, Any]]) -> None:
    # Reset prior assignments, then write the fresh grouping. No deletes of
    # opportunity rows — only the dedupe annotation columns are cleared.
    conn.execute(
        "UPDATE opportunities SET canonical_opportunity_id = NULL, "
        "duplicate_confidence = NULL, duplicate_reason = NULL "
        "WHERE canonical_opportunity_id IS NOT NULL"
    )
    conn.execute("DELETE FROM opportunity_duplicates")
    for g in report_groups:
        canonical_id = g["canonical"]["id"]
        for dup in g["duplicates"]:
            conn.execute(
                "UPDATE opportunities SET canonical_opportunity_id = ?, "
                "duplicate_confidence = ?, duplicate_reason = ? WHERE id = ?",
                (canonical_id, dup["confidence"], dup["reason"], dup["id"]),
            )
            conn.execute(
                "INSERT INTO opportunity_duplicates "
                "(group_id, canonical_id, duplicate_id, reason, confidence, "
                " preferred_source_reason) VALUES (?, ?, ?, ?, ?, ?)",
                (g["group_id"], canonical_id, dup["id"], dup["reason"],
                 dup["confidence"], g["why_canonical"]),
            )


def duplicates_hidden_map() -> dict[int, int]:
    """canonical_id -> number of active duplicates collapsed under it. Used to
    annotate the Today plan / API with a '+N duplicates hidden' count."""
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT canonical_opportunity_id AS cid, COUNT(*) AS n "
            "FROM opportunities WHERE canonical_opportunity_id IS NOT NULL "
            "AND active = 1 GROUP BY canonical_opportunity_id"
        ).fetchall()
        return {int(r["cid"]): int(r["n"]) for r in rows}
    finally:
        conn.close()
