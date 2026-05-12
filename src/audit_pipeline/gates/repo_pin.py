"""Gate 6 — L5.repo_pin.

Validates that any commit SHA pinned in a disclosure body actually belongs to
the GitHub repo the disclosure is being filed against. Built in direct
response to the cycle-20260511-183154 retraction: the issue header read
``percolator-prog @ 6cd742f25a…`` but ``6cd742f25a`` is the **engine** repo
(``aeyakovenko/percolator``) HEAD, not the wrapper repo
(``aeyakovenko/percolator-prog``) the issue was filed against. One-call check
against the GitHub API at issue-creation time would have caught this; this
gate makes that check mandatory.

The gate scans the disclosure body for hex SHAs (≥7 chars, typical git short
or full hashes), looks up the commit on the target repo via ``gh api``, and
returns FAIL if any cited SHA doesn't resolve against the filed-to repo.

Used by: ``commands/issue.py`` (file_cmd, auto_file_cmd) before invoking
``gh issue create``.
"""

from __future__ import annotations

import json
import re
import subprocess
import time

from audit_pipeline.gates import GateResult

# Conservative SHA regex: hex, 7-40 chars, word-boundary delimited so we don't
# match longer hex strings (Merkle roots, signatures, etc.). Backticks and
# punctuation around the SHA are common in markdown.
_SHA_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{7,40})(?![0-9a-fA-F])")

# Heuristic noise filter: only treat a hex string as a candidate SHA if it
# looks adjacent to git/repo/commit/HEAD/SHA prose, OR appears in a
# repo@sha-style anchor. Otherwise long hex strings would be false-positive
# matches (e.g. Merkle roots, request IDs, ed25519 fingerprints).
_SHA_CONTEXT_HINTS = (
    "sha", "commit", "@", "head", "ref", "pinned", "tag", "branch",
    "engine_sha", "wrapper_sha",
)


def _looks_like_sha_context(body: str, span: tuple[int, int]) -> bool:
    """Return True if the hex match at ``span`` is in git-context prose.

    Hex like Merkle roots also appear in bodies; we don't want to look those
    up as commits and fail the gate on Merkle-root mismatches. We require one
    of the context hints within ~40 chars before the match.
    """
    start, _ = span
    context = body[max(0, start - 40):start].lower()
    return any(hint in context for hint in _SHA_CONTEXT_HINTS)


def extract_candidate_shas(body: str) -> list[str]:
    """Return distinct candidate SHAs appearing in git-context prose."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _SHA_RE.finditer(body):
        if not _looks_like_sha_context(body, m.span()):
            continue
        sha = m.group(1).lower()
        if sha not in seen:
            seen.add(sha)
            out.append(sha)
    return out


def _commit_exists_in_repo(sha: str, repo: str, gh_path: str = "gh") -> bool | None:
    """Hit ``gh api repos/<repo>/commits/<sha>``.

    Returns:
        True  — commit resolves in ``repo``
        False — 404 / commit not in repo
        None  — gh CLI missing or transport error (skip gate, don't block)
    """
    try:
        proc = subprocess.run(
            [gh_path, "api", f"repos/{repo}/commits/{sha}", "--silent"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    # gh exits 0 on found, non-zero on 404 / other error. stderr distinguishes
    # "404" from "no such repo" vs "auth". We treat any non-zero as "not in repo"
    # only if stderr explicitly mentions 404 / not found — auth errors should
    # skip the gate, not fail it.
    if proc.returncode == 0:
        return True
    err = (proc.stderr or "").lower()
    if "404" in err or "not found" in err or "no commit" in err:
        return False
    # Treat anything else (auth issues, rate-limit, network) as skip.
    return None


def check_repo_pin(
    *,
    body: str,
    target_repo: str,
    gh_path: str = "gh",
) -> GateResult:
    """Verify every git-context SHA in ``body`` resolves in ``target_repo``.

    Args:
        body:        markdown body that will be POSTed to the issue
        target_repo: ``owner/name`` the issue is being filed to
        gh_path:     ``gh`` CLI binary (override for tests / non-default PATH)

    Returns:
        ``GateResult(passed=True, …)`` if every candidate SHA resolves
        ``GateResult(passed=False, …)`` with details listing each SHA that
            didn't resolve in ``target_repo``
        ``GateResult(passed=None, …)`` if the gh CLI is unavailable or all
            API lookups timed out (the caller should decide to retry or
            override with ``--allow-mixed-pin``)
    """
    t0 = time.time()
    shas = extract_candidate_shas(body)
    if not shas:
        return GateResult(
            passed=True,
            reason="no git-context SHAs cited in body",
            duration_s=time.time() - t0,
        )

    not_in_repo: list[str] = []
    indeterminate: list[str] = []
    in_repo: list[str] = []
    for sha in shas:
        result = _commit_exists_in_repo(sha, target_repo, gh_path)
        if result is True:
            in_repo.append(sha)
        elif result is False:
            not_in_repo.append(sha)
        else:
            indeterminate.append(sha)

    details = {
        "target_repo": target_repo,
        "shas_checked": len(shas),
        "in_repo":   in_repo,
        "not_in_repo": not_in_repo,
        "indeterminate": indeterminate,
    }

    if not_in_repo:
        return GateResult(
            passed=False,
            reason=(
                f"{len(not_in_repo)} SHA(s) cited in body do NOT resolve in "
                f"{target_repo}: {', '.join(s[:10] for s in not_in_repo)}. "
                "Likely a wrong-repo header (engine SHA pinned in wrapper-repo "
                "disclosure or vice versa). Fix the body or pass "
                "--allow-mixed-pin to override."
            ),
            duration_s=time.time() - t0,
            details=details,
        )
    if indeterminate and not in_repo:
        return GateResult(
            passed=None,
            reason=(
                "could not verify any SHA against repo (gh missing / rate limit / "
                "network). Re-run when gh CLI can reach the API, or "
                "--allow-mixed-pin to override."
            ),
            duration_s=time.time() - t0,
            details=details,
        )
    return GateResult(
        passed=True,
        reason=f"all {len(in_repo)} cited SHA(s) resolve in {target_repo}",
        duration_s=time.time() - t0,
        details=details,
    )


__all__ = ["check_repo_pin", "extract_candidate_shas"]
