"""GitHub API helpers for repo freshness + watch.

Uses the public GitHub REST API. No auth needed for public repos, but the
caller can pass a token via the GITHUB_TOKEN env var to lift the
60-req/hr unauthenticated rate limit to 5000/hr.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

import requests

GITHUB_API = "https://api.github.com"

# A commit SHA safe to splice into a compare-API URL segment and to trust as a
# diff baseline. EXACT 40 hex chars — same as freshness_gate's `_RE_SHA1_40`,
# and for the same reason (its CRITICAL 0efd25c3 fix): an abbreviated SHA used
# as a compare baseline is brute-forceable into an ambiguous ref by an attacker
# who controls the audited repo. Only a full 40-char SHA is trusted here.
_RE_COMPARE_SHA = re.compile(r"[0-9a-f]{40}")
# Conservative GitHub owner/repo identifier (defence-in-depth so a future/
# careless caller can't splice a path-traversal segment into the URL). Must
# START with an alphanumeric — rejects dot-only segments like "." / ".." that
# the raw charset would otherwise pass.
_RE_GH_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def parse_github_repo(url: str) -> tuple[str, str]:
    """Return (owner, repo) from a github URL like https://github.com/foo/bar(.git)."""
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc.lower() not in ("github.com", "www.github.com"):
        raise ValueError(f"Not a github URL: {url}")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Cannot parse owner/repo from {url}")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def _headers() -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def get_latest_commit(owner: str, repo: str, ref: str = "HEAD", timeout: int = 30) -> dict:
    """Return the latest commit dict for owner/repo at ref.

    Result shape:
        {
            "sha": "...",
            "commit": {"message": "...", "author": {"date": "...", "name": "..."}},
            "html_url": "...",
        }
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/commits/{ref}"
    resp = requests.get(url, headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def list_commits_since(
    owner: str,
    repo: str,
    base_sha: str,
    ref: str = "HEAD",
    timeout: int = 30,
    max_commits: int = 50,
) -> list[dict]:
    """Return commits between base_sha and ref (most-recent-first).

    Uses GitHub's compare API. Empty list if base_sha == ref.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/compare/{base_sha}...{ref}"
    resp = requests.get(url, headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    commits = body.get("commits", [])
    # GitHub returns oldest-first; reverse for newest-first
    commits = list(reversed(commits))
    return commits[:max_commits]


# Compare API caps `files` at 300; a larger diff is truncated.
_COMPARE_FILES_CAP = 300


def changed_files_via_compare(
    owner: str,
    repo: str,
    base_sha: str,
    head_ref: str = "HEAD",
    timeout: int = 30,
) -> set[str]:
    """Changed file paths between base_sha and head_ref via the GitHub
    compare API — the source-mode equivalent of
    ``scoping.changed_files_between`` (which needs a local ``.git``).

    Returns posix-style repo-relative paths. **Fail-closed by contract:**
    returns an EMPTY set on ANY doubt, so the caller's "empty => run the
    full library, never silently narrow the scan" invariant holds. Empty is
    returned when:

      - ``base_sha`` isn't a hex SHA (never splice unvalidated input into
        the URL / trust it as a scoping baseline);
      - the request errors, or returns a non-object JSON body;
      - **the base is NOT a direct ancestor of the head** (status != "ahead"
        / merge-base != base). The compare endpoint diffs against the
        MERGE-BASE, not a raw two-commit diff, so on a rebased / force-pushed
        / diverged ref its ``files`` list is INCOMPLETE — trusting it would
        silently drop a genuinely-changed file. We only trust the list on a
        clean fast-forward, where it equals ``git diff base..head``;
      - the diff exceeds the 300-file compare cap (truncated → untrustworthy).

    Renamed/copied entries contribute BOTH the new ``filename`` and the old
    ``previous_filename``, so a hypothesis pinned to the pre-rename path still
    counts the file as changed.
    """
    if not (isinstance(base_sha, str) and _RE_COMPARE_SHA.fullmatch(base_sha)):
        return set()
    # Validate every OTHER segment spliced into the URL too: head_ref is a full
    # SHA or the literal "HEAD"; owner/repo a conservative identifier charset.
    # Any doubt => fail closed (run the full library).
    if not (head_ref == "HEAD"
            or (isinstance(head_ref, str) and _RE_COMPARE_SHA.fullmatch(head_ref))):
        return set()
    if not (isinstance(owner, str) and _RE_GH_IDENT.fullmatch(owner)
            and isinstance(repo, str) and _RE_GH_IDENT.fullmatch(repo)):
        return set()
    url = f"{GITHUB_API}/repos/{owner}/{repo}/compare/{base_sha}...{head_ref}"
    try:
        resp = requests.get(url, headers=_headers(), timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError):
        return set()
    if not isinstance(body, dict):  # 200 with a non-object body → fail closed
        return set()
    # Trust the file list ONLY when base is a direct ancestor of head (clean
    # fast-forward). Any divergence => the merge-base diff is incomplete =>
    # fall back to the full library.
    if body.get("status") != "ahead":
        return set()
    merge_base = ((body.get("merge_base_commit") or {}).get("sha")) or ""
    base_commit = ((body.get("base_commit") or {}).get("sha")) or ""
    # A missing/empty merge-base OR base-commit is itself doubt — fail closed.
    # (Without this, `x and y and not(...)` short-circuited to False when either
    # field was absent, silently TRUSTING the file list — goober round-2.)
    if not merge_base or not base_commit:
        return set()
    # Both are full 40-char SHAs from the API JSON — exact equality only (a
    # strict-prefix relationship between two equal-length strings is impossible).
    if merge_base != base_commit:
        return set()
    files = body.get("files") or []
    if not isinstance(files, list) or len(files) >= _COMPARE_FILES_CAP:
        return set()
    changed: set[str] = set()
    for f in files:
        if not isinstance(f, dict):
            continue
        if f.get("filename"):
            changed.add(f["filename"])
        if f.get("previous_filename"):  # rename/copy: keep the OLD path too
            changed.add(f["previous_filename"])
    return changed
