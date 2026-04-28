"""GitHub API helpers for repo freshness + watch.

Uses the public GitHub REST API. No auth needed for public repos, but the
caller can pass a token via the GITHUB_TOKEN env var to lift the
60-req/hr unauthenticated rate limit to 5000/hr.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import requests

GITHUB_API = "https://api.github.com"


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
