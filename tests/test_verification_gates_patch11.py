"""Patch #11 — verification-gates security hardening tests.

Closes audit findings:
  * CRITICAL 0efd25c3 — freshness_gate SHA prefix-match spoofable
  * HIGH e971bf01 — post_cycle.py poc_path traversal

Plus R1 expansions caught by reviewer audit:
  * Missing-config bypass in check_freshness (workspace.json with
    empty component → gate returned passed=True silently)
  * Non-hex / short pin silently degraded to time-based check
  * cycle_dir depth-1 collapsed workspace_root to / (no protection)
  * Resolved-vs-unresolved poc_path inconsistency (TOCTOU window)
  * Sibling display command (freshness.py) used the old spoofable check
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


# ─────────────── freshness_gate.py SHA exact-match ───────────────


def _stub_get_latest_commit(sha: str, date: str = "2026-05-24T00:00:00Z"):
    """Return a `get_latest_commit` mock that returns the given SHA."""
    return lambda owner, repo, ref=None: {
        "sha": sha,
        "commit": {
            "author": {"date": date},
            "message": "stub commit",
        },
    }


def _make_workspace_json(
    tmp_path: Path,
    engine_sha: str = "a" * 40,
    wrapper_sha: str = "b" * 40,
    engine_repo: str = "https://github.com/x/engine",
    wrapper_repo: str = "https://github.com/x/wrapper",
) -> Path:
    cfg = {
        "engine":  {"repo": engine_repo,  "sha": engine_sha,  "local": "engine"},
        "wrapper": {"repo": wrapper_repo, "sha": wrapper_sha, "local": "wrapper"},
    }
    p = tmp_path / "workspace.json"
    p.write_text(json.dumps(cfg))
    return p


def test_freshness_exact_40char_match_passes(tmp_path, monkeypatch) -> None:
    """P11 R0 (CRITICAL 0efd25c3): the 40-char exact-match fast path
    must FIRE for a real full SHA. Locks the actual security
    invariant the patch claims to enforce — the R0 tests all used
    8-char SHAs that never reached this branch."""
    _make_workspace_json(tmp_path, engine_sha="a" * 40, wrapper_sha="b" * 40)

    def stub(owner, repo, ref=None):
        # Match the SHAs we wrote for each repo
        return {
            "sha": "a" * 40 if "engine" in repo else "b" * 40,
            "commit": {"author": {"date": "2026-05-24T00:00:00Z"},
                       "message": "stub"},
        }

    monkeypatch.setattr(
        "audit_pipeline.utils.github.get_latest_commit", stub,
    )
    from audit_pipeline.gates.freshness_gate import check_freshness
    r = check_freshness(workspace=tmp_path)
    assert r.passed is True, f"expected fresh, got {r.reason}"
    for comp in r.details["components"]:
        assert comp["status"] == "fresh"


def test_freshness_rejects_7char_prefix_spoof(tmp_path, monkeypatch) -> None:
    """P11 R0 (CRITICAL 0efd25c3): a 7-char pin must NOT be accepted
    even if its prefix matches the 40-char HEAD. The original
    prefix-match would have returned `fresh`; R0 fix makes it raise
    FreshnessConfigError via the hex-format guard."""
    # 7-char pin — would match `abcdef0aaa...` prefix in old code
    _make_workspace_json(
        tmp_path, engine_sha="abcdef0", wrapper_sha="abcdef0",
    )
    monkeypatch.setattr(
        "audit_pipeline.utils.github.get_latest_commit",
        _stub_get_latest_commit("abcdef0" + "1" * 33),
    )
    from audit_pipeline.gates.freshness_gate import check_freshness
    r = check_freshness(workspace=tmp_path)
    assert r.passed is False
    assert "40 lowercase hex" in r.reason or "must be exactly 40" in r.reason


def test_freshness_rejects_non_hex_pin(tmp_path, monkeypatch) -> None:
    """P11 R1 (threat-modeler MEDIUM): a tag name or branch name as
    `pinned` (e.g. `v1.2.3`, `main`) silently fell through to the
    time-based check, computing freshness against the tag's date
    rather than SHA identity. R1 hardens with a hex format guard
    that raises FreshnessConfigError."""
    _make_workspace_json(tmp_path, engine_sha="main", wrapper_sha="v1.0.0")
    monkeypatch.setattr(
        "audit_pipeline.utils.github.get_latest_commit",
        _stub_get_latest_commit("a" * 40),
    )
    from audit_pipeline.gates.freshness_gate import check_freshness
    r = check_freshness(workspace=tmp_path)
    assert r.passed is False
    assert "40 lowercase hex" in r.reason


def test_freshness_rejects_missing_component(tmp_path, monkeypatch) -> None:
    """P11 R1 (threat-modeler HIGH): a workspace.json with an empty
    `engine: {}` section previously returned status='missing-config'
    which check_freshness silently ignored — gate passed without
    checking any SHA. R1 routes missing-config through
    FreshnessConfigError so the gate fails closed."""
    cfg = {
        "engine":  {},  # empty section
        "wrapper": {"repo": "https://github.com/x/w", "sha": "b" * 40,
                    "local": "w"},
    }
    (tmp_path / "workspace.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(
        "audit_pipeline.utils.github.get_latest_commit",
        _stub_get_latest_commit("b" * 40),
    )
    from audit_pipeline.gates.freshness_gate import check_freshness
    r = check_freshness(workspace=tmp_path)
    assert r.passed is False
    assert "missing" in r.reason.lower() or "empty section" in r.reason


# ─────────────── post_cycle.py poc_path traversal ───────────────


def test_post_cycle_rejects_absolute_path_escape(tmp_path) -> None:
    """P11 R0 (HIGH e971bf01): an absolute db_poc_path like /etc/passwd
    must NOT be read. Old code trusted any absolute path; R0 confines
    to workspace_root via relative_to.

    R2 (goober HIGH): assertion strengthened — verify the chosen
    poc_path is the CANONICAL one (under cycle_dir/poc/), not the
    outside escape path. The previous content-only assertion was a
    false negative since the content was never stored in the row
    even if a bypass had read it."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hunts").mkdir()
    cycle_dir = workspace / "hunts" / "CYC123"
    cycle_dir.mkdir()
    poc_dir = cycle_dir / "poc"
    poc_dir.mkdir()

    from audit_pipeline.gates.post_cycle import check_post_cycle

    # Use a real existing path outside workspace as the malicious poc
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("ATTACKER CONTENT — should not be read")

    confirmed = [{
        "id":             1,
        "hypothesis_id":  "H1-fake",
        "poc_path":       str(outside.resolve()),
    }]
    report = check_post_cycle(
        cycle_dir=cycle_dir,
        confirmed_findings=confirmed,
        engine_src_dir=workspace / "engine_src",
        wrapper_src_dir=workspace / "wrapper_src",
    )
    # Confirm the chosen poc_path is the CANONICAL fallback under
    # cycle_dir/poc/, NOT the outside escape path. This is the
    # load-bearing assertion R2 added — without it, a future
    # regression that silently read the outside file would still
    # pass the content-absent check.
    row = report.rows[0]
    chosen = str(row.get("poc_path", ""))
    assert str(outside.resolve()) not in chosen, (
        f"escape allowed — outside path was chosen: {chosen}"
    )
    assert str(cycle_dir) in chosen, (
        f"expected canonical fallback under cycle_dir, got: {chosen}"
    )
    # Belt-and-suspenders: content also must not appear
    for v in row.values():
        assert "ATTACKER CONTENT" not in str(v), (
            f"secret content leaked into row: {row}"
        )


def test_post_cycle_accepts_legitimate_workspace_path(tmp_path) -> None:
    """Sanity: a real Aptos-style poc_path under workspace/tests/aptos/
    must STILL be readable — the confinement must not break legitimate
    multi-language layouts."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hunts").mkdir()
    cycle_dir = workspace / "hunts" / "CYC123"
    cycle_dir.mkdir()
    (workspace / "tests" / "aptos").mkdir(parents=True)
    legit_poc = workspace / "tests" / "aptos" / "test_h1.move"
    legit_poc.write_text("// legitimate aptos PoC\nfun test_h1() { ... }\n")

    from audit_pipeline.gates.post_cycle import check_post_cycle

    confirmed = [{
        "id":            1,
        "hypothesis_id": "h1",
        "poc_path":      str(legit_poc.resolve()),
    }]
    report = check_post_cycle(
        cycle_dir=cycle_dir,
        confirmed_findings=confirmed,
        engine_src_dir=workspace / "engine_src",
        wrapper_src_dir=workspace / "wrapper_src",
    )
    # The poc_path was inside workspace, so it should be read normally.
    # Whether the test passes the symbol-grep is irrelevant — what
    # matters is that the path was accepted (used in poc_path field).
    row = report.rows[0]
    assert str(legit_poc.resolve()) in str(row.get("poc_path", "")), (
        f"legitimate workspace path was not accepted: row={row}"
    )


def test_post_cycle_shallow_cycle_dir_falls_back_safely(tmp_path, monkeypatch) -> None:
    """P11 R1 (code-reviewer LOW + goober MEDIUM): a shallow `cycle_dir`
    (e.g. `/CYC123` depth-1) would make `.parent.parent` collapse to
    `/`, defeating the protection. R1 adds a depth-check fallback to
    canonical layout.

    R2 (goober HIGH #2 false-negative): tmp_path is naturally 5-8 parts
    deep on real OSes — the depth guard never fires. R2 mocks `Path.resolve`
    on the cycle_dir to return a 3-part shallow path so the guard
    actually engages, AND asserts the chosen poc_path is the canonical
    fallback (not the outside escape path)."""
    cycle_dir = tmp_path / "shallow_cycle"
    cycle_dir.mkdir()

    # Mock cycle_dir.resolve() to return a shallow 3-part path so the
    # depth-guard actually fires. The original cycle_dir on disk is
    # still tmp_path/shallow_cycle (deep) so file operations work.
    real_resolve = Path.resolve

    def patched_resolve(self, *args, **kwargs):
        if self == cycle_dir:
            # P11 R3+R4 (code-reviewer LOW + goober): use a truly
            # shallow path with FEWER than 4 parts on EITHER platform.
            # `/a/b/c` has 4 parts on BOTH POSIX (`('/', 'a', 'b', 'c')`)
            # and Windows (`('\\', 'a', 'b', 'c')`) so the guard never
            # fired. `/a/b` has 3 parts on both platforms → guard fires.
            return Path("/a/b")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", patched_resolve)

    from audit_pipeline.gates.post_cycle import check_post_cycle

    outside = tmp_path / "secret.txt"
    outside.write_text("LEAK ME")

    confirmed = [{
        "id":            1,
        "hypothesis_id": "h1",
        "poc_path":      str(outside),  # absolute path
    }]
    report = check_post_cycle(
        cycle_dir=cycle_dir,
        confirmed_findings=confirmed,
        engine_src_dir=None,
        wrapper_src_dir=None,
    )
    # The depth guard must have fired → poc_path is the canonical
    # fallback under cycle_dir/poc/, NOT the outside escape path.
    row = report.rows[0]
    chosen = str(row.get("poc_path", ""))
    assert str(outside) not in chosen, (
        f"depth-guard didn't fire — outside path was chosen: {chosen}"
    )
    assert "poc" in chosen and "test_" in chosen, (
        f"expected canonical fallback path, got: {chosen}"
    )


def test_freshness_rejects_blank_sha(tmp_path, monkeypatch) -> None:
    """P11 R2 (goober CRITICAL + code-reviewer MEDIUM): R1's hex guard
    `if _pin_norm and not fullmatch(...)` had a bypass — empty `sha: ""`
    in the config short-circuited (falsy `_pin_norm`) and fell through
    to the time-based check. R2 fix: unconditional guard."""
    cfg = {
        "engine":  {"sha": "", "repo": "https://github.com/x/engine",
                    "local": "e"},
        "wrapper": {"sha": "b" * 40, "repo": "https://github.com/x/wrapper",
                    "local": "w"},
    }
    (tmp_path / "workspace.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(
        "audit_pipeline.utils.github.get_latest_commit",
        _stub_get_latest_commit("b" * 40),
    )
    from audit_pipeline.gates.freshness_gate import check_freshness
    r = check_freshness(workspace=tmp_path)
    assert r.passed is False
    assert "40 lowercase hex" in r.reason or "must be exactly 40" in r.reason


def test_freshness_rejects_whitespace_only_sha(tmp_path, monkeypatch) -> None:
    """Companion to blank-sha test: whitespace-only must also fail."""
    cfg = {
        "engine":  {"sha": "   ", "repo": "https://github.com/x/e", "local": "e"},
        "wrapper": {"sha": "b" * 40, "repo": "https://github.com/x/w", "local": "w"},
    }
    (tmp_path / "workspace.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(
        "audit_pipeline.utils.github.get_latest_commit",
        _stub_get_latest_commit("b" * 40),
    )
    from audit_pipeline.gates.freshness_gate import check_freshness
    r = check_freshness(workspace=tmp_path)
    assert r.passed is False
    assert "40 lowercase hex" in r.reason


def test_freshness_rejects_unresolvable_foreign_sha(tmp_path, monkeypatch) -> None:
    """P11 R2+R3 (threat-modeler MEDIUM #4): a valid 40-char hex pin
    that doesn't exist in the upstream repo (foreign-SHA / force-pushed)
    previously silently degraded to date-proximity. R2 raised
    FreshnessConfigError on the unresolvable case. R3 narrows the
    raise to HTTP 404 specifically — uses a real `requests.HTTPError`
    with a mock response so the test would catch a future regression
    that tightens the exception handler."""
    import requests
    _make_workspace_json(tmp_path, engine_sha="a" * 40, wrapper_sha="b" * 40)

    class _MockResp:
        status_code = 404

    def stub(owner, repo, ref=None):
        if ref and ref != "HEAD":
            # Real-shape HTTPError with status_code=404 on the response
            err = requests.HTTPError("404: commit not found")
            err.response = _MockResp()
            raise err
        return {
            "sha": "f" * 40,  # head differs from pinned "a"*40
            "commit": {"author": {"date": "2026-05-24T00:00:00Z"},
                       "message": "head commit"},
        }

    monkeypatch.setattr(
        "audit_pipeline.utils.github.get_latest_commit", stub,
    )
    from audit_pipeline.gates.freshness_gate import check_freshness
    r = check_freshness(workspace=tmp_path, max_stale_hours=6.0)
    assert r.passed is False
    assert "does not resolve" in r.reason


def test_freshness_rejects_401_bad_token(tmp_path, monkeypatch) -> None:
    """P11 R4 (threat-modeler LOW): an expired or missing GITHUB_TOKEN
    returns HTTP 401. R4 classifies this as permanent (operator must
    rotate token, not retry). The error message must mention token
    rotation explicitly so the operator doesn't waste time editing
    workspace.json."""
    import requests
    _make_workspace_json(tmp_path, engine_sha="a" * 40, wrapper_sha="b" * 40)

    class _MockResp:
        status_code = 401
        headers: dict[str, str] = {}

    def stub(owner, repo, ref=None):
        if ref and ref != "HEAD":
            err = requests.HTTPError("401 unauthorized")
            err.response = _MockResp()
            raise err
        return {
            "sha": "f" * 40,
            "commit": {"author": {"date": "2026-05-24T00:00:00Z"},
                       "message": "head commit"},
        }

    monkeypatch.setattr(
        "audit_pipeline.utils.github.get_latest_commit", stub,
    )
    from audit_pipeline.gates.freshness_gate import check_freshness
    r = check_freshness(workspace=tmp_path, max_stale_hours=6.0)
    assert r.passed is False
    assert "401" in r.reason
    assert "rotate the token" in r.reason


def test_freshness_treats_403_without_retry_after_as_permanent(
    tmp_path, monkeypatch
) -> None:
    """P11 R5 (goober + threat-modeler MEDIUM): 403 without
    Retry-After indicates access-denied (private/deleted repo or token
    lacks scope) — permanent. With Retry-After it indicates GitHub
    secondary rate limit — transient."""
    import requests
    _make_workspace_json(tmp_path, engine_sha="a" * 40, wrapper_sha="b" * 40)

    class _MockResp:
        status_code = 403
        headers: dict[str, str] = {}  # no Retry-After → permanent

    def stub(owner, repo, ref=None):
        if ref and ref != "HEAD":
            err = requests.HTTPError("403 forbidden")
            err.response = _MockResp()
            raise err
        return {
            "sha": "f" * 40,
            "commit": {"author": {"date": "2026-05-24T00:00:00Z"},
                       "message": "head commit"},
        }

    monkeypatch.setattr(
        "audit_pipeline.utils.github.get_latest_commit", stub,
    )
    from audit_pipeline.gates.freshness_gate import check_freshness
    r = check_freshness(workspace=tmp_path, max_stale_hours=6.0)
    assert r.passed is False
    assert "403" in r.reason


def test_freshness_treats_403_with_retry_after_as_transient(
    tmp_path, monkeypatch
) -> None:
    """P11 R5 (goober MEDIUM + threat-modeler #2): 403 with
    Retry-After indicates GitHub secondary rate limit — transient.
    Must NOT hard-fail. Re-raises to outer handler → passed=None."""
    import requests
    _make_workspace_json(tmp_path, engine_sha="a" * 40, wrapper_sha="b" * 40)

    class _MockResp:
        status_code = 403
        headers = {"Retry-After": "60"}  # secondary rate-limit signal

    def stub(owner, repo, ref=None):
        if ref and ref != "HEAD":
            err = requests.HTTPError("403 rate limited")
            err.response = _MockResp()
            raise err
        return {
            "sha": "f" * 40,
            "commit": {"author": {"date": "2026-05-24T00:00:00Z"},
                       "message": "head commit"},
        }

    monkeypatch.setattr(
        "audit_pipeline.utils.github.get_latest_commit", stub,
    )
    from audit_pipeline.gates.freshness_gate import check_freshness
    r = check_freshness(workspace=tmp_path, max_stale_hours=6.0)
    # Transient → outer handler → status=unreachable → passed=None
    assert r.passed is None


def test_freshness_treats_403_with_ratelimit_remaining_zero_as_transient(
    tmp_path, monkeypatch
) -> None:
    """P11 R5 (threat-modeler LOW): the 403 classifier inspects TWO
    transient signals — `Retry-After` AND `x-ratelimit-remaining: 0`.
    The other R5 tests cover `Retry-After`; this one locks the
    rate-limit-remaining branch so a future regression on either
    signal is caught."""
    import requests
    _make_workspace_json(tmp_path, engine_sha="a" * 40, wrapper_sha="b" * 40)

    class _MockResp:
        status_code = 403
        # No Retry-After — but x-ratelimit-remaining: 0 still signals
        # GitHub primary rate limit
        headers = {"x-ratelimit-remaining": "0"}

    def stub(owner, repo, ref=None):
        if ref and ref != "HEAD":
            err = requests.HTTPError("403 rate limited")
            err.response = _MockResp()
            raise err
        return {
            "sha": "f" * 40,
            "commit": {"author": {"date": "2026-05-24T00:00:00Z"},
                       "message": "head commit"},
        }

    monkeypatch.setattr(
        "audit_pipeline.utils.github.get_latest_commit", stub,
    )
    from audit_pipeline.gates.freshness_gate import check_freshness
    r = check_freshness(workspace=tmp_path, max_stale_hours=6.0)
    # Transient → passed=None
    assert r.passed is None


def test_freshness_transient_pin_fetch_skips(tmp_path, monkeypatch) -> None:
    """P11 R3 (all 3 reviewers): a TRANSIENT failure on the pinned-SHA
    fetch (timeout, 5xx, 429 rate-limit) must NOT be misclassified as
    a foreign-SHA hard-fail. R3 distinguishes HTTP 404 (real foreign-
    SHA → FreshnessConfigError) from other exceptions (transient →
    re-raise to outer handler which records `status: unreachable`
    → gate returns `passed=None` for caller retry)."""
    _make_workspace_json(tmp_path, engine_sha="a" * 40, wrapper_sha="b" * 40)

    def stub(owner, repo, ref=None):
        if ref and ref != "HEAD":
            # NOT a 404 — a transient timeout-like failure
            raise TimeoutError("connection timed out talking to GitHub")
        return {
            "sha": "f" * 40,  # HEAD differs from pinned to force the
                              # date-comparison branch
            "commit": {"author": {"date": "2026-05-24T00:00:00Z"},
                       "message": "head commit"},
        }

    monkeypatch.setattr(
        "audit_pipeline.utils.github.get_latest_commit", stub,
    )
    from audit_pipeline.gates.freshness_gate import check_freshness
    r = check_freshness(workspace=tmp_path, max_stale_hours=6.0)
    # Transient pin-fetch failure → both components fall into the
    # outer `except Exception` → status=unreachable. With no fresh
    # component, the gate returns passed=None (caller may retry).
    assert r.passed is None
    assert "could not reach" in r.reason.lower()


def test_freshness_rejects_empty_components_tuple(tmp_path) -> None:
    """P11 R2 (threat-modeler LOW): `check_freshness(components=())`
    previously returned passed=True with no checks. R2 adds a guard
    at the API boundary."""
    _make_workspace_json(tmp_path, engine_sha="a" * 40, wrapper_sha="b" * 40)
    from audit_pipeline.gates.freshness_gate import check_freshness
    r = check_freshness(workspace=tmp_path, components=())
    assert r.passed is False
    assert "empty components" in r.reason


# ─────────────── freshness.py display command parity ───────────────


def test_freshness_display_uses_exact_match_too(tmp_path, monkeypatch) -> None:
    """P11 R1 (code-reviewer + goober HIGH): the sibling display
    command `freshness.py` had its OWN copy of the spoofable prefix
    match. R1 mirrors the gate's exact-match logic so display and
    gate cannot diverge — operator can't see 'up-to-date' on a
    spoofed 7-char pin while the gate blocks correctly."""
    import inspect
    from audit_pipeline.commands import freshness as freshness_cmd
    src = inspect.getsource(freshness_cmd)
    # Must NOT contain the old spoofable check
    assert "pinned.startswith(latest_sha[" not in src, (
        "old prefix-match still present in freshness display command"
    )
    # MUST use the shared SHA-1 hex regex (R2 hoisted to module scope)
    assert "_RE_SHA1_40" in src, (
        "freshness display command not using the shared "
        "_RE_SHA1_40 pattern from freshness_gate"
    )
