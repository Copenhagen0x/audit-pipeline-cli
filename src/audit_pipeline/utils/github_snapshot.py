"""Ephemeral source-snapshot loader.

The autonomous hunt loop reads source files from upstream protocol repos
(Anatoly's `aeyakovenko/percolator-prog`, etc.). Historically the pipeline
maintained a persistent local clone for that purpose, which created a class
of bugs around state drift, accidental local edits (e.g., a rebrand sweep
overwriting `spec.md` in the clone), and `git pull` failures that silently
killed the loop for days.

This module replaces the persistent clone with an ephemeral snapshot:

  * Downloaded fresh per hunt cycle from a pinned commit SHA
  * Lives in a temp dir for the lifetime of a context-manager
  * Cleaned up on exit
  * Works as a drop-in `workspace: Path` for the existing tool layer
    (`tool_read_file`, `tool_grep`, `tool_find_function`)
  * Compiles cleanly — the extracted tarball is a full source tree, so
    `cargo`, `kani`, and LiteSVM can all run against it directly

Pinning to a SHA also gives reproducibility: every parallel agent in a
hunt cycle sees the exact same bytes, even if upstream pushes new commits
mid-cycle.
"""

from __future__ import annotations

import io
import shutil
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path

import requests

from audit_pipeline.utils.github import _headers, parse_github_repo

# How long the tarball download is given before we abort (large repos can
# take a few seconds; 60s is generous and prevents indefinite hangs).
_DOWNLOAD_TIMEOUT_SEC = 60

# Max tarball size we'll accept (200 MB). Percolator is far smaller; this
# is a safety bound against a misconfigured target.
_MAX_TARBALL_BYTES = 200 * 1024 * 1024


class SnapshotDownloadError(RuntimeError):
    """Raised when the upstream tarball cannot be fetched or extracted."""


class GitHubSnapshot(AbstractContextManager["GitHubSnapshot"]):
    """An ephemeral, read-only snapshot of an upstream GitHub repo.

    Usage:
        with GitHubSnapshot("aeyakovenko/percolator-prog", sha="abc1234") as snap:
            # snap.workspace is a Path to the extracted source root
            tool_read_file(snap.workspace, "spec.md")
            # ... or pass to run_tool_using_agent(workspace=snap.workspace, ...)
        # On exit, the temp dir is removed automatically.

    The snapshot is downloaded from GitHub's tarball endpoint:
        https://api.github.com/repos/{owner}/{repo}/tarball/{ref}
    which serves a `.tar.gz` of the tree at the requested commit.

    Authenticated via `GITHUB_TOKEN` env var if set (lifts unauthenticated
    60-req/hr rate limit to 5000/hr; required for private repos).
    """

    def __init__(
        self,
        repo: str,
        sha: str | None = None,
        *,
        cache_dir: Path | None = None,
        keep_on_exit: bool = False,
    ) -> None:
        """
        Args:
            repo: Either "owner/repo" or a full GitHub URL.
            sha: Specific commit SHA to pin to. If None, uses the default
                branch HEAD at download time. Always prefer pinning when
                you want reproducibility within a single cycle.
            cache_dir: Optional directory for the temp extract. Defaults
                to system temp. Useful in tests or when you want the
                snapshot to live next to other workspace artifacts.
            keep_on_exit: If True, do NOT delete the temp dir on exit.
                Default False. Useful for debugging only.
        """
        if "/" in repo and not repo.startswith("http"):
            owner, repo_name = repo.split("/", 1)
        else:
            owner, repo_name = parse_github_repo(repo)

        self.owner = owner
        self.repo_name = repo_name
        self.sha = sha
        self._cache_dir = cache_dir
        self._keep_on_exit = keep_on_exit
        self._tmp_root: Path | None = None
        self._workspace: Path | None = None
        self._actual_sha: str | None = None

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def workspace(self) -> Path:
        """Path to the extracted source root.

        Inside the tarball, GitHub puts everything under a top-level dir
        named like `{owner}-{repo}-{sha7}/`. We resolve to that inner
        directory so callers can pass it as `workspace` directly to
        `tool_read_file`, etc.
        """
        if self._workspace is None:
            raise RuntimeError("snapshot not entered (use as a context manager)")
        return self._workspace

    @property
    def resolved_sha(self) -> str:
        """The actual commit SHA that was downloaded.

        If `sha` was given at construction, this matches. Otherwise, this
        is the default-branch HEAD at the moment of download.
        """
        if self._actual_sha is None:
            raise RuntimeError("snapshot not entered (use as a context manager)")
        return self._actual_sha

    @property
    def repo_slug(self) -> str:
        return f"{self.owner}/{self.repo_name}"

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> GitHubSnapshot:
        ref = self.sha or "HEAD"
        url = (
            f"https://api.github.com/repos/{self.owner}/{self.repo_name}"
            f"/tarball/{ref}"
        )

        # Fetch with auth if available, follow redirects to the codeload server.
        try:
            resp = requests.get(
                url,
                headers=_headers(),
                timeout=_DOWNLOAD_TIMEOUT_SEC,
                stream=True,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            raise SnapshotDownloadError(
                f"network error fetching {self.repo_slug}@{ref}: {e}"
            ) from e

        if resp.status_code == 404:
            raise SnapshotDownloadError(
                f"upstream repo or ref not found: {self.repo_slug}@{ref} "
                f"(check that the SHA exists and the repo is accessible)"
            )
        if resp.status_code == 403:
            raise SnapshotDownloadError(
                f"GitHub rate-limited or denied access to {self.repo_slug}@{ref}; "
                f"set GITHUB_TOKEN env var to lift the unauthenticated 60/hr limit"
            )
        resp.raise_for_status()

        # Capture the actual SHA from the URL we were redirected to (if any),
        # or fall back to whatever the user requested. This is best-effort
        # since GitHub doesn't return the SHA in headers for tarball.
        self._actual_sha = self.sha or self._extract_sha_from_url(resp.url) or ref

        # Read tarball into memory with a hard size cap.
        buf = io.BytesIO()
        total = 0
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > _MAX_TARBALL_BYTES:
                raise SnapshotDownloadError(
                    f"tarball for {self.repo_slug}@{ref} exceeds "
                    f"{_MAX_TARBALL_BYTES // (1024 * 1024)} MB safety cap"
                )
            buf.write(chunk)
        buf.seek(0)

        # Create temp dir for extraction.
        if self._cache_dir is not None:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._tmp_root = Path(
                tempfile.mkdtemp(
                    prefix=f"jelleo-snap-{self.owner}-{self.repo_name}-",
                    dir=str(self._cache_dir),
                )
            )
        else:
            self._tmp_root = Path(
                tempfile.mkdtemp(
                    prefix=f"jelleo-snap-{self.owner}-{self.repo_name}-"
                )
            )

        # Extract tarball.
        try:
            with tarfile.open(fileobj=buf, mode="r:gz") as tf:
                # Safety: refuse path-escape entries (CVE-2007-4559 style).
                for member in tf.getmembers():
                    name = member.name
                    if name.startswith("/") or ".." in Path(name).parts:
                        raise SnapshotDownloadError(
                            f"tarball contains unsafe path: {name}"
                        )
                tf.extractall(path=self._tmp_root)
        except (tarfile.TarError, SnapshotDownloadError) as e:
            shutil.rmtree(self._tmp_root, ignore_errors=True)
            self._tmp_root = None
            raise SnapshotDownloadError(
                f"failed to extract tarball for {self.repo_slug}@{ref}: {e}"
            ) from e

        # The tarball wraps everything in a single top-level dir like
        # `aeyakovenko-percolator-prog-3c9c849/`. Find it.
        children = [c for c in self._tmp_root.iterdir() if c.is_dir()]
        if len(children) == 1:
            self._workspace = children[0]
            # Extract the actual SHA from the tarball's inner dir name.
            # GitHub names it `{owner}-{repo}-{sha7}/` reliably.
            dir_name = children[0].name
            sha_from_dir = self._extract_sha_from_dir_name(
                dir_name, self.owner, self.repo_name
            )
            if sha_from_dir:
                self._actual_sha = sha_from_dir
        elif len(children) == 0:
            shutil.rmtree(self._tmp_root, ignore_errors=True)
            self._tmp_root = None
            raise SnapshotDownloadError(
                f"empty tarball for {self.repo_slug}@{ref}"
            )
        else:
            # Unusual: tarball has multiple top-level dirs. Use the temp
            # root as workspace and let the caller deal with it.
            self._workspace = self._tmp_root

        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._tmp_root is None:
            return
        if self._keep_on_exit:
            return
        # Best-effort cleanup. Never let cleanup errors mask the original
        # exception (if any).
        try:
            shutil.rmtree(self._tmp_root, ignore_errors=True)
        finally:
            self._tmp_root = None
            self._workspace = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_sha_from_dir_name(
        dir_name: str, owner: str, repo_name: str
    ) -> str | None:
        """GitHub tarballs are wrapped in `{owner}-{repo}-{sha7}/`.

        Pulls the trailing hex segment as the resolved SHA. Returns None
        if the dir name doesn't match the expected pattern.
        """
        prefix = f"{owner}-{repo_name}-"
        if not dir_name.startswith(prefix):
            return None
        candidate = dir_name[len(prefix):]
        if (
            7 <= len(candidate) <= 40
            and all(c in "0123456789abcdefABCDEF" for c in candidate)
        ):
            return candidate.lower()
        return None

    @staticmethod
    def _extract_sha_from_url(url: str) -> str | None:
        """GitHub's codeload redirect URL contains the resolved SHA.

        Example after redirect:
          https://codeload.github.com/aeyakovenko/percolator-prog/legacy.tar.gz/refs/heads/main
          https://codeload.github.com/aeyakovenko/percolator-prog/legacy.tar.gz/3c9c849...

        We try to pull the trailing path segment as a SHA candidate.
        Returns None if no SHA-shaped trailing segment found.
        """
        if not url:
            return None
        parts = [p for p in url.split("/") if p]
        if not parts:
            return None
        candidate = parts[-1]
        # Full SHAs are 40 hex chars; abbreviated 7-char SHAs also acceptable.
        if (
            len(candidate) >= 7
            and len(candidate) <= 40
            and all(c in "0123456789abcdefABCDEF" for c in candidate)
        ):
            return candidate
        return None


def open_snapshot(
    repo: str,
    sha: str | None = None,
    *,
    cache_dir: Path | None = None,
) -> GitHubSnapshot:
    """Convenience wrapper. Equivalent to `GitHubSnapshot(repo, sha)`.

    Use as a context manager:

        with open_snapshot("aeyakovenko/percolator-prog", "3c9c849") as snap:
            run_tool_using_agent(workspace=snap.workspace, ...)
    """
    return GitHubSnapshot(repo, sha, cache_dir=cache_dir)


def iter_snapshots(
    repos: list[tuple[str, str | None]],
    *,
    cache_dir: Path | None = None,
) -> Iterator[GitHubSnapshot]:
    """Yield active snapshots for each (repo, sha) pair, cleaning up
    each one before yielding the next. Useful when running per-target
    agents serially.

    NOT a context manager itself — each yielded snapshot is the
    context. The intended pattern is:

        for snap in iter_snapshots([("owner1/repo1", sha1), ...]):
            with snap:
                ... # do work
    """
    for repo, sha in repos:
        yield GitHubSnapshot(repo, sha, cache_dir=cache_dir)


def is_snapshot_workspace(workspace: Path) -> bool:
    """Heuristic: is this Path the result of a snapshot extraction?

    Used by code that wants to log / report whether reads went through
    a live snapshot vs. a persistent local workspace.
    """
    s = str(workspace)
    return "jelleo-snap-" in s or s.startswith(tempfile.gettempdir())
