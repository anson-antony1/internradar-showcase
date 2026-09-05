"""Repo-wide PII / secrets sweep over git-tracked files.

Broader than the existing per-module privacy guards (e.g.
test_profile_personalization.py, test_scholar_core.py, test_referrals.py,
which each check one corner) — this scans every tracked file in the repo for
email addresses, US phone numbers, and SSN-shaped strings, and asserts the
known-sensitive paths are never tracked. It is intentionally allowlisted
(example.com/.org/.net, this repo's own docs placeholders) rather than
zero-tolerance, since committed docs legitimately contain example emails.

Nothing runs at import time (see run()) so pytest can collect this file
directly.

Run standalone: python3 tests/test_privacy_sweep.py
Run via pytest:  python -m pytest tests/test_privacy_sweep.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ALLOWED_EMAIL_SUFFIXES = ("example.com", "example.org", "example.net", "anthropic.com")
_PHONE = re.compile(r"\b(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def _is_fake_phone(matched: str) -> bool:
    """Fictional numbers used throughout this suite's fixtures.

    - "555" as either the exchange (NXX-555-XXXX, the NANP-reserved fictional
      block behind every movie/TV fake number) or as the area code
      (555-NXX-XXXX — this codebase's own fixture convention, e.g.
      "555) 123-4567" in test_apply_agent.py) — never a real subscriber number.
    - All-same-digit numbers (000-000-0000, 111-111-1111, ...): obviously
      placeholder, not a real line.
    """
    digits = re.sub(r"\D", "", matched)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return False
    if digits[3:6] == "555" or digits[0:3] == "555":
        return True
    if len(set(digits)) == 1:
        return True
    return False

def _is_fake_ssn(matched: str) -> bool:
    """Placeholder SSNs, which the suite needs in order to test redaction.

    The tests that assert a real privacy boundary have to contain an SSN-shaped
    string to be meaningful: test_draft_generator_v2.py asserts that one is
    replaced with a redaction marker before reaching a cloud LLM and that a
    local provider is *not* scrubbed, and test_resume_portfolio.py asserts an
    SSN fact is stored as sensitive and unusable. Stripping the fixture would
    make those tests pass while proving nothing.

    Allowed as never-issued or obviously fictional:
    - 123-45-6789, the universal documentation placeholder.
    - Area numbers the SSA has never issued: 000, 666, and 900-999.
    - Group or serial of all zeroes, which is never valid.
    - All-same-digit strings.
    """
    digits = re.sub(r"\D", "", matched)
    if len(digits) != 9:
        return False
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    if digits == "123456789":
        return True
    if area in ("000", "666") or area >= "900":
        return True
    if group == "00" or serial == "0000":
        return True
    if len(set(digits)) == 1:
        return True
    return False


def _is_placeholder_email(matched: str, body: str) -> bool:
    """Fictional addresses, and email-shaped strings that are not addresses.

    - The allowlisted suffixes above, plus the `.example` TLD: RFC 2606 reserves
      `.example` for documentation exactly like `example.com`, and it is the
      more correct choice for a fake institution ("alvarez@school.example").
    - URL userinfo. A URL of the form scheme://user:secret@host/path contains no
      email address, but this module's regex matches the `user:secret@host`
      portion out of the middle of it. Those URLs are load-bearing fixtures —
      test_campus_intelligence.py asserts that a credential-bearing source URL
      is *rejected* — so the fixture has to keep its credentials, and the sweep
      has to stop reading them as contacts. An address is only reported when
      some occurrence of it is bare rather than inside a URL.
    """
    low = matched.lower()
    if low.endswith(_ALLOWED_EMAIL_SUFFIXES):
        return True
    if low.rsplit(".", 1)[-1] == "example":
        return True
    # Preceded by "://" up to the match with no whitespace: it is inside a URL.
    idx = body.find(matched)
    while idx != -1:
        prefix = body[max(0, idx - 200):idx]
        scheme = prefix.rfind("://")
        if scheme == -1 or re.search(r"\s", prefix[scheme:]):
            return False  # this occurrence is a bare address
        idx = body.find(matched, idx + 1)
    return True


_SCAN_EXTENSIONS = (".py", ".json", ".md", ".html", ".txt", ".ts", ".tsx", ".css", ".sh", ".yml", ".yaml")

# Exact paths, never prefixes: "backend/.env" as a *prefix* would also match the
# legitimately-tracked "backend/.env.example" template.
_NEVER_TRACKED_EXACT = {"backend/.env", "backend/radar.db", "backend/scholar.db", "backend/scholar_profile.json"}
_NEVER_TRACKED_PREFIXES = (
    "backend/uploads/", "backend/generated/", "backend/scholar_generated/",
    "backend/scholar_sessions/", "backend/materials/",
    # §5.15: full data exports + DB backups — the most PII-dense artifacts
    # this app ever produces.
    "backend/exports/", "backend/backups/",
)


def _is_never_tracked(rel: str) -> bool:
    return rel in _NEVER_TRACKED_EXACT or any(rel.startswith(p) for p in _NEVER_TRACKED_PREFIXES)


def _tracked_files() -> list[str]:
    try:
        out = subprocess.check_output(["git", "ls-files"], cwd=_REPO_ROOT)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return out.decode().split()


def run() -> dict[str, list[str]]:
    """Scan tracked files; return {check_name: [violation, ...]} (empty lists = pass)."""
    tracked = _tracked_files()
    result = {
        "no_git": [] if tracked else ["git not available or repo has no tracked files"],
        "email_leaks": [],
        "phone_leaks": [],
        "ssn_leaks": [],
        "never_tracked_hits": [],
    }
    if not tracked:
        return result

    for rel in tracked:
        if not rel.endswith(_SCAN_EXTENSIONS):
            continue
        path = os.path.join(_REPO_ROOT, rel)
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                body = fh.read()
        except OSError:
            continue
        for m in _EMAIL.findall(body):
            if not _is_placeholder_email(m, body):
                result["email_leaks"].append(f"{rel}: {m}")
        for m in _PHONE.finditer(body):
            matched = m.group(0)
            if not _is_fake_phone(matched):
                result["phone_leaks"].append(f"{rel}: {matched}")
        for m in _SSN.finditer(body):
            if not _is_fake_ssn(m.group(0)):
                result["ssn_leaks"].append(f"{rel}: {m.group(0)}")

    result["never_tracked_hits"] = [rel for rel in tracked if _is_never_tracked(rel)]
    return result


def test_privacy_sweep() -> None:
    result = run()
    assert not result["no_git"], result["no_git"]
    assert not result["email_leaks"], f"real email(s) committed: {result['email_leaks']}"
    assert not result["phone_leaks"], f"real phone(s) committed: {result['phone_leaks']}"
    assert not result["ssn_leaks"], f"SSN-shaped string(s) committed: {result['ssn_leaks']}"
    assert not result["never_tracked_hits"], f"gitignore-protected path tracked: {result['never_tracked_hits']}"


if __name__ == "__main__":
    r = run()
    failed = any(r.values())
    for check_name, violations in r.items():
        if check_name == "no_git" and violations:
            print(f"  SKIP  {violations[0]}")
            continue
        status = "FAIL" if violations else "PASS"
        print(f"  {status}  {check_name}  {violations if violations else ''}")
    print(f"\n{'FAIL' if failed else 'PASS'}")
    sys.exit(1 if (failed and not r['no_git']) else 0)
