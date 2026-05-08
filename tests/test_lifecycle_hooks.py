"""Tier 2 #8 + #9 — lifecycle hook tests.

The transition_finding() method schedules sibling-derivation +
auto-propagation in a daemon thread on Status.CONFIRMED. Both hooks are
fire-and-forget; they must:
  - never block the DB transition
  - never raise on missing API key / missing corpus / missing finding

These tests use run_hooks=False to avoid network calls during unit tests,
and a structural inspection of _fire_confirmed_hooks() to confirm the
daemon-thread + silenced-exceptions wiring.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from audit_pipeline.db import FindingsDB
from audit_pipeline.lifecycle import InvalidTransition, Status
from audit_pipeline.severity import Severity


@pytest.fixture()
def db_with_finding(tmp_path: Path) -> tuple[FindingsDB, int]:
    """Fresh DB + a single 'new' finding, ready to transition."""
    db = FindingsDB(tmp_path / "findings.db")
    target_id = db.upsert_target(name="percolator", github_url="https://github.com/x/x")
    fid = db.upsert_finding(
        target_id=target_id,
        cycle_id="c-1",
        hypothesis_id="H99-test",
        verdict="TRUE",
        confidence=None,
        severity=Severity.HIGH,
        status=Status.NEW,
        engine_sha="abc1234",
        wrapper_sha=None,
        debate_promoted=False,
        poc_fired=False,
        title="lifecycle hook test finding",
        poc_path=None,
    )
    return db, fid


def test_legal_transition_succeeds(db_with_finding: tuple[FindingsDB, int]) -> None:
    db, fid = db_with_finding
    db.transition_finding(fid, Status.TRIAGED, "test", actor="test", run_hooks=False)
    db.transition_finding(fid, Status.CONFIRMED, "test", actor="test", run_hooks=False)
    f = db.get_finding(fid)
    assert f["status"] == "confirmed"


def test_illegal_transition_raises(db_with_finding: tuple[FindingsDB, int]) -> None:
    db, fid = db_with_finding
    # NEW -> FIXED is illegal (skips triaged/confirmed/disclosed)
    with pytest.raises(InvalidTransition):
        db.transition_finding(fid, Status.FIXED, "test", run_hooks=False)


def test_transition_writes_audit_row(db_with_finding: tuple[FindingsDB, int]) -> None:
    """Every transition appends to the transitions table."""
    db, fid = db_with_finding
    db.transition_finding(fid, Status.TRIAGED, "manual triage", actor="me",
                          run_hooks=False)
    db.transition_finding(fid, Status.CONFIRMED, "PoC fired", actor="me",
                          run_hooks=False)
    history = db.transitions_for(fid)
    assert len(history) >= 2
    last = history[-1]
    assert last["from_status"] == "triaged"
    assert last["to_status"] == "confirmed"


def test_run_hooks_false_does_not_schedule_thread(
    db_with_finding: tuple[FindingsDB, int]
) -> None:
    """With run_hooks=False the daemon-thread schedule must not run.

    Asserted indirectly: no derived/ directory is ever created (which is
    what derive_siblings_async writes). If the hook ran (even silently),
    we'd see filesystem side-effects.
    """
    db, fid = db_with_finding
    db.transition_finding(fid, Status.TRIAGED, "test", run_hooks=False)
    db.transition_finding(fid, Status.CONFIRMED, "test", run_hooks=False)
    # workspace dir is the DB's parent
    derived_dir = db.path.parent / "derived"
    assert not derived_dir.exists()


def test_fire_confirmed_hooks_signature_is_correct() -> None:
    """The post-confirmed hook must call BOTH async helpers via _run_one_hook
    and run in a daemon thread.

    The hooks are now dispatched through `_run_one_hook` (F21 — adds
    structured logging per invocation under <workspace>/hooks/). Targets
    are module-level wrappers `_derive_target` and `_propagate_target`
    that lazily import the underlying async helpers.
    """
    import audit_pipeline.db as db_module

    src = inspect.getsource(FindingsDB._fire_confirmed_hooks)
    assert "_derive_target" in src, "must dispatch sibling-derivation hook"
    assert "_propagate_target" in src, "must dispatch propagation hook"
    assert "threading" in src, "must spawn a thread"
    assert "daemon=True" in src, "thread must be daemon (don't block exit)"
    assert "_run_one_hook" in src, "must wrap each hook with logging dispatcher"

    # Module-level targets must reference the actual async helpers
    derive_src = inspect.getsource(db_module._derive_target)
    assert "derive_siblings_async" in derive_src
    propagate_src = inspect.getsource(db_module._propagate_target)
    assert "propagate_from_finding_async" in propagate_src


def test_transition_finding_signature_includes_run_hooks_flag() -> None:
    """run_hooks=True is the default; tests opt out via False."""
    sig = inspect.signature(FindingsDB.transition_finding)
    assert "run_hooks" in sig.parameters
    assert sig.parameters["run_hooks"].default is True
