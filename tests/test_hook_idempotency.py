"""Tests for hook_runs idempotency (cross-cutting audit Defect 14).

Before this fix, post-confirmed hooks were fire-and-forget daemons.
Re-transitioning the same finding to CONFIRMED (after a crash, manual
retry, audit replay) would re-run derive_siblings + propagate, doubling
spend and producing duplicate siblings.

The fix: hook_runs table with UNIQUE(finding_id, hook_name). _run_one_hook
claims the pair before executing; subsequent claims with outcome='ok'
short-circuit. Prior 'error' outcomes are allowed to retry.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def test_hook_runs_table_exists_after_init(tmp_path: Path) -> None:
    from audit_pipeline.db import FindingsDB
    FindingsDB(tmp_path / "findings.db")
    conn = sqlite3.connect(str(tmp_path / "findings.db"))
    rows = list(conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='hook_runs'"
    ))
    conn.close()
    assert len(rows) == 1


def test_first_hook_run_records_claim(tmp_path: Path) -> None:
    """A successful hook run should leave a hook_runs row with outcome='ok'."""
    from audit_pipeline.db import FindingsDB
    db = FindingsDB(tmp_path / "findings.db")
    calls = {"n": 0}

    def fake_target(ws, fid):
        calls["n"] += 1

    # Invoke directly — bypass the threaded firing path so the test is
    # deterministic.
    db._run_one_hook(tmp_path, 42, "derive_siblings", fake_target)
    assert calls["n"] == 1

    conn = sqlite3.connect(str(tmp_path / "findings.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT outcome FROM hook_runs WHERE finding_id = 42 AND hook_name = 'derive_siblings'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["outcome"] == "ok"


def test_second_run_of_same_hook_is_skipped(tmp_path: Path) -> None:
    """After a successful run, re-firing for the same (finding_id, hook)
    must NOT call the target again."""
    from audit_pipeline.db import FindingsDB
    db = FindingsDB(tmp_path / "findings.db")
    calls = {"n": 0}

    def fake_target(ws, fid):
        calls["n"] += 1

    db._run_one_hook(tmp_path, 99, "propagate", fake_target)
    db._run_one_hook(tmp_path, 99, "propagate", fake_target)
    db._run_one_hook(tmp_path, 99, "propagate", fake_target)
    assert calls["n"] == 1


def test_different_hooks_for_same_finding_both_run(tmp_path: Path) -> None:
    """The (finding_id, hook_name) uniqueness — same finding + different
    hook should both be allowed to run."""
    from audit_pipeline.db import FindingsDB
    db = FindingsDB(tmp_path / "findings.db")
    calls = {"derive": 0, "propagate": 0}

    def fake_derive(ws, fid):
        calls["derive"] += 1

    def fake_propagate(ws, fid):
        calls["propagate"] += 1

    db._run_one_hook(tmp_path, 7, "derive_siblings", fake_derive)
    db._run_one_hook(tmp_path, 7, "propagate", fake_propagate)
    assert calls["derive"] == 1
    assert calls["propagate"] == 1


def test_failed_hook_allowed_to_retry(tmp_path: Path) -> None:
    """A prior 'error' outcome should NOT block a retry — transient
    failures (network, rate limits) shouldn't permanently stick."""
    from audit_pipeline.db import FindingsDB
    db = FindingsDB(tmp_path / "findings.db")
    calls = {"n": 0}

    def fake_target(ws, fid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient failure")

    db._run_one_hook(tmp_path, 13, "propagate", fake_target)
    db._run_one_hook(tmp_path, 13, "propagate", fake_target)
    assert calls["n"] == 2

    conn = sqlite3.connect(str(tmp_path / "findings.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT outcome FROM hook_runs WHERE finding_id = 13 AND hook_name = 'propagate'"
    ).fetchone()
    conn.close()
    # After the retry succeeded, the final outcome must be 'ok'.
    assert row is not None
    assert row["outcome"] == "ok"
