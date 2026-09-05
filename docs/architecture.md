# Architecture

The longer version of the [README](../README.md). Everything here was measured
against the running system, not recalled.

## Shape

```
┌─ sources ──────────────────────────────────────────────────────────┐
│  Greenhouse · Lever · Ashby REST APIs      55 active boards         │
│  public GitHub internship lists            138 auto-discovered      │
└────────────────────────────┬───────────────────────────────────────┘
                             │  scan (8h base interval, per-source backoff)
                             ▼
                   raw_postings  ──▶  opportunities
                             │
       ┌─────────────────────┼─────────────────────┐
       ▼                     ▼                     ▼
    filter               dedupe                  score
  word-boundary      4 signals, 3 hard      0.45·fresh + 0.40·fit
  keyword match      blockers, marks        + source + season
                     rather than deletes    + eligibility
                             │
                             ▼
                       eligibility
       eligible_edge / eligible / needs_review /
       location_mismatch / likely_blocked
                             │
                             ▼
                       action queue  ──▶  tracker
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
   FastAPI /api/radar   Playwright apply    CLI: export,
   → Next.js UI         agent (never        backup, restore,
                        submits)            purge
```

Two apps share this plumbing: InternRadar (internships, `radar.db`, 27 tables)
and a sibling scholarship finder (`scholar.db`, 48 tables). Scoring, dedup,
verification, and draft generation are common; the domain models are not.

## Why company boards, not aggregators

A posting appears on the company's own Greenhouse/Lever/Ashby board first and
propagates outward. Reading the board directly is both earlier and cleaner —
these are documented JSON APIs, so there's no HTML parsing, no rate-limit
roulette, and no terms-of-service question. Only public, unauthenticated,
structured endpoints are used.

Public GitHub internship lists are the second source. They're wide but noisy and
lag the boards, so they serve mainly as a **discovery feed**: a role in a list
that points at an ATS board InternRadar doesn't yet monitor becomes a candidate
source.

## Self-healing sources

This is the part that makes the source list grow without maintenance.

1. **Discover.** While scanning, any posting whose apply URL points at an
   unmonitored ATS board is recorded in `source_discoveries` as a candidate, with
   the company, ATS type, identifier, and how it was found.
2. **Verify.** `verify_discoveries()` hits the candidate's ATS API directly. A
   board that answers with real postings is **promoted** to a monitored source;
   one that doesn't is **rejected**. Nothing is trusted because it was linked —
   it's trusted because it answered.
3. **Retire, carefully.** Each source carries a failure streak and a reliability
   score. Retirement thresholds differ by *kind* of failure, which matters more
   than it sounds:

   - **Permanent** (404/410, taken from the HTTP status via
     `adapters.is_permanent_failure`, never from message text) retires after
     **two** consecutive confirmations. Two rather than one, so a single 404
     mis-served during an ATS incident can't retire a live board.
   - **Transient** failures need **five**.
   - Reliability below 20 also retires.

4. **The provisional exemption.** A source that has *never once* succeeded is
   treated as provisional rather than broken. A GitHub list for a future season
   (`SimplifyJobs/Summer2027-Internships`) 404s for weeks before the repo exists.
   Retiring it would be effectively permanent, because the scheduler selects only
   `active = 1` and the seed upsert deliberately doesn't touch `active` — so a
   reseed couldn't revive it. It stays on a weekly probe instead.

5. **Un-retire on success.** Retirement is a health signal, not a tombstone.
   Outages end, repos get renamed back, ATS migrations finish. A targeted
   re-check reaches a retired row and flips it live again on its first success.

The reliability, freshness, and priority bars in the Sources screenshot are this
system's state made visible.

## Dedup, in order

Postings are blocked by normalized company first, so comparison is never
quadratic across the whole table. Within a company block, **hard blockers run
before any similarity signal**:

| Blocker | Why |
|---|---|
| Different explicit season | Summer 2026 ≠ Summer 2027. Merging hides a live opening. |
| Conflicting level/track | Undergrad vs Masters; II vs III. |
| Different req ids, same ATS | One req per location produces near-identical titles; the id is the only distinction. |

Then, strongest signal first:

1. Identical ATS requisition id parsed from the apply URL
2. Identical normalized apply URL (tracking parameters stripped)
3. Same normalized title + compatible location
4. High title-token similarity + a corroborating signal (location or apply host)

Matches are written to `opportunity_duplicates` with a canonical pointer. The
rows survive; a `visible_predicate` hides non-canonical ones from browsing views.
A wrong merge is therefore inspectable and reversible, which is the whole reason
for marking instead of deleting.

## Scoring

`scoring.py` is 167 lines and has no model in it.

```python
score  = 0.45 * freshness + 0.40 * fit
score += 10 if source_type in {greenhouse, lever, ashby, custom} else 0
score += (season_priority / 100) * 10

# eligibility
score += {"likely_blocked": -35, "location_mismatch": -25, "eligible_edge": +5}
```

Freshness is banded (1h / 6h / 24h), not continuous — a role posted 61 minutes
ago and one posted 59 minutes ago don't deserve different ranks, and banding
makes the score stable enough to be worth showing.

Every function returns a `reasons` list alongside its number, and those
accumulate into `score_reasons` on the opportunity. The UI shows them. This is
the difference between "this is ranked 4th" and "this is ranked 4th because it's
from a source-of-truth board, posted today, in your target cycle, and you
uniquely qualify."

An optional calibration layer applies user-accepted deltas scaled by how strongly
each component actually scored — imported lazily, because calibration reads
`SOURCE_OF_TRUTH_TYPES` back out of `scoring` and a module-level import would
close the cycle.

## Eligibility

Five verdicts, resolved from the posting text against the profile:

| Verdict | Meaning |
|---|---|
| `eligible_edge` | You qualify for a restricted role many applicants can't. |
| `eligible` | Clean apply. |
| `needs_review` | Ambiguous authorization language. |
| `location_mismatch` | Outside your target locations. |
| `likely_blocked` | A hard requirement you don't meet. |

The same posting produces different verdicts for different people, entirely from
the profile file. A citizenship-required role is `likely_blocked` for one person
and `eligible_edge` for another.

`location_mismatch` and `likely_blocked` are excluded from every browsing view,
not merely down-ranked. A role you can't take is not a lower-priority role; it's
noise, and scrolling past it repeatedly is the cost.

## The apply agent

```
form_extract  →  field_mapping  →  playwright_runner
   read DOM       classify each      fill / skip / flag
                  field              ── stops here ──
```

`field_mapping.decide()` returns one of `fill`, `upload`, or `needs_review` per
field, with a confidence. Fills happen above 0.75. Categories that never fill
regardless of confidence:

- **Citizenship / work authorization** — sponsorship, visa, OPT/CPT, EAD, green
  card, right to work.
- **Demographics** — race, gender, disability, veteran status.
- **Signatures, certifications, agreements, fees.**

`upload` requires the field to name itself resume or CV. Anything else with
`type=file` is flagged for review rather than guessed at.

The runner contains no `.click()`, `.press()`, or `.check()`. `select_option()`
appears once, in the fill branch, for `<select>` dropdowns. The plan object
carries `auto_submit: False` and `stop_before_submit: True`. There is no code
path to submission — the boundary is the absence of the capability, not a flag
that could be flipped.

An offline self-test exercises the ATS adapters against bundled dummy forms with
no browser and no network.

## Data

SQLite, local. `radar.db` is 27 tables; the notable ones:

| Table | Holds |
|---|---|
| `opportunities` | The scored, deduped roles. |
| `raw_postings` | What each source actually returned, unmodified. |
| `opportunity_duplicates` | Canonical pointers, so merges are inspectable. |
| `job_sources` | Boards, tiers, reliability, failure streaks, next check. |
| `source_discoveries` | Candidate boards, and whether verification promoted them. |
| `applications` / `application_steps` | Tracker state and per-role progress. |
| `apply_profile` | Field values for the apply agent (gitignored). |

Export, backup, restore, and purge are CLI-only and deliberately not exposed over
HTTP — they're the operations that move the entire dataset, and a local web UI
is not the right place for a one-click full export.

## Testing

44 test modules, 1,812 assertions, 49 pytest items, plus frontend typecheck,
lint, and build. All in GitHub Actions on push and PR.

The suites are standalone scripts built on a `check(name, cond, detail)` helper,
collected under pytest by a parametrized wrapper. Trade-offs are in the README.

Several tests are closer to **fitness functions** than unit tests — they assert a
safety property still holds in the source, so a later refactor can't quietly
remove it:

- The repo-wide privacy sweep over every tracked file.
- Assertions that sensitive field categories never classify as fillable.
- Assertions that PII is scrubbed before any draft reaches a cloud model.
- Assertions that a failed re-verification never withdraws a lead's legitimacy.

Time-dependence was a real bug here. The re-verification tests originally used a
hard-coded `TODAY` literal while SQLite stamped rows with `datetime('now')`; they
passed while the wall clock sat near the literal and started failing once it
moved on. Every date in those suites is now derived from `date.today()`, so the
fixtures stay internally consistent whenever the suite runs.
