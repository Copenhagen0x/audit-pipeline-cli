"""Tests for `audit-pipeline cycle ...` lifecycle commands.

Born out of the cycle 20260511-183154 retraction: that cycle was killed
mid-run and stayed in_progress in the DB indefinitely, making the
dashboard misleading. These commands let the operator close out cycles
after the fact.

Coverage:
  - mark_cycle_finished updates finished_at without disturbing other fields
  - get_cycle round-trips a row
  - `cycle finish` CLI marks an unfinished cycle as finished
  - `cycle finish` on an already-finished cycle is a no-op (no double-stamp)
  - `cycle finish` on a non-existent cycle exits non-zero
  - `cycle retract` finishes the row + writes retraction.json sidecar
  - `cycle list` shows in_progress vs finished status
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from click.testing import CliRunner


def _seed_db(workspace: Path) -> tuple[int, str, str]:
    from audit_pipeline.db import FindingsDB
    db = FindingsDB(workspace / "findings.db")
    target_id = db.upsert_target(name="testproto")
    db.insert_cycle(target_id=target_id, cycle_id="C-LIVE",
                    engine_sha="aa" * 16)
    db.insert_cycle(target_id=target_id, cycle_id="C-DONE",
                    engine_sha="bb" * 16)
    # Manually mark one finished so the list view has both states
    with db._conn() as c:
        c.execute(
            "UPDATE cycles SET finished_at = ? WHERE cycle_id = ?",
            ("2026-05-12T03:00:00+00:00", "C-DONE"),
        )
    return target_id, "C-LIVE", "C-DONE"


def test_mark_cycle_finished_sets_timestamp(tmp_path: Path) -> None:
    from audit_pipeline.db import FindingsDB
    _seed_db(tmp_path)
    db = FindingsDB(tmp_path / "findings.db")
    assert db.get_cycle("C-LIVE")["finished_at"] is None
    ok = db.mark_cycle_finished("C-LIVE")
    assert ok is True
    after = db.get_cycle("C-LIVE")
    assert after["finished_at"] is not None
    # Other columns untouched
    assert after["engine_sha"] == "aa" * 16


def test_mark_cycle_finished_is_idempotent(tmp_path: Path) -> None:
    """A second call on an already-finished cycle must NOT overwrite the
    original finished_at (the column has IS NULL guard)."""
    from audit_pipeline.db import FindingsDB
    _seed_db(tmp_path)
    db = FindingsDB(tmp_path / "findings.db")
    original = db.get_cycle("C-DONE")["finished_at"]
    ok = db.mark_cycle_finished("C-DONE")
    assert ok is False  # no rows updated
    after = db.get_cycle("C-DONE")
    assert after["finished_at"] == original


def test_mark_cycle_finished_unknown_cycle_returns_false(tmp_path: Path) -> None:
    from audit_pipeline.db import FindingsDB
    _seed_db(tmp_path)
    db = FindingsDB(tmp_path / "findings.db")
    assert db.mark_cycle_finished("does-not-exist") is False


def test_get_cycle_round_trips(tmp_path: Path) -> None:
    from audit_pipeline.db import FindingsDB
    _seed_db(tmp_path)
    db = FindingsDB(tmp_path / "findings.db")
    c = db.get_cycle("C-LIVE")
    assert c is not None
    assert c["cycle_id"] == "C-LIVE"
    assert c["engine_sha"] == "aa" * 16
    assert db.get_cycle("does-not-exist") is None


# ───── CLI tests ─────


def _invoke(workspace: Path, *args: str):
    from audit_pipeline.commands.cycle import cycle_cmd
    runner = CliRunner()
    return runner.invoke(cycle_cmd, list(args), obj={"workspace": str(workspace)})


def test_cli_list_shows_in_progress_and_finished(tmp_path: Path) -> None:
    _seed_db(tmp_path)
    r = _invoke(tmp_path, "list")
    assert r.exit_code == 0, r.output
    assert "C-LIVE" in r.output
    assert "C-DONE" in r.output
    assert "in_progress" in r.output
    assert "finished" in r.output


def test_cli_finish_marks_unfinished_cycle(tmp_path: Path) -> None:
    _seed_db(tmp_path)
    r = _invoke(tmp_path, "finish", "C-LIVE")
    assert r.exit_code == 0, r.output
    assert "marked finished" in r.output
    from audit_pipeline.db import FindingsDB
    db = FindingsDB(tmp_path / "findings.db")
    assert db.get_cycle("C-LIVE")["finished_at"] is not None


def test_cli_finish_with_explicit_timestamp(tmp_path: Path) -> None:
    _seed_db(tmp_path)
    ts = "2026-05-12T08:58:00+00:00"
    r = _invoke(tmp_path, "finish", "C-LIVE", "--finished-at", ts)
    assert r.exit_code == 0, r.output
    from audit_pipeline.db import FindingsDB
    db = FindingsDB(tmp_path / "findings.db")
    assert db.get_cycle("C-LIVE")["finished_at"] == ts


def test_cli_finish_already_done_is_noop(tmp_path: Path) -> None:
    """Calling finish on a cycle already finished must NOT overwrite the
    timestamp and must NOT exit non-zero."""
    _seed_db(tmp_path)
    from audit_pipeline.db import FindingsDB
    db = FindingsDB(tmp_path / "findings.db")
    original = db.get_cycle("C-DONE")["finished_at"]
    r = _invoke(tmp_path, "finish", "C-DONE")
    assert r.exit_code == 0, r.output
    assert "already finished" in r.output.lower()
    assert db.get_cycle("C-DONE")["finished_at"] == original


def test_cli_finish_unknown_cycle_exits_nonzero(tmp_path: Path) -> None:
    _seed_db(tmp_path)
    r = _invoke(tmp_path, "finish", "no-such-cycle")
    assert r.exit_code != 0


def test_cli_retract_writes_sidecar(tmp_path: Path) -> None:
    """The retract subcommand must finish the DB row AND write a
    retraction.json sidecar that the cycle archive page can read."""
    _seed_db(tmp_path)
    cycle_dir = tmp_path / "hunts" / "C-LIVE"
    cycle_dir.mkdir(parents=True)
    r = _invoke(
        tmp_path, "retract", "C-LIVE",
        "--reason", "False positives debunked publicly",
        "--ref-url", "https://github.com/x/y/issues/92",
    )
    assert r.exit_code == 0, r.output
    sidecar = cycle_dir / "retraction.json"
    assert sidecar.is_file()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["cycle_id"] == "C-LIVE"
    assert payload["reason"].startswith("False positives")
    assert payload["ref_url"].endswith("/issues/92")
    assert payload["schema"] == "jelleo-retraction-v1"
    # DB row should now be finished
    from audit_pipeline.db import FindingsDB
    db = FindingsDB(tmp_path / "findings.db")
    assert db.get_cycle("C-LIVE")["finished_at"] is not None


def test_cli_retract_missing_cycle_dir_is_soft_warn(tmp_path: Path) -> None:
    """If the cycle dir doesn't exist, retract should still mark the DB
    row finished (don't error). Useful when the cycle dir was rotated
    away but the DB row remained."""
    _seed_db(tmp_path)
    r = _invoke(
        tmp_path, "retract", "C-LIVE",
        "--reason", "test",
    )
    # No cycle dir under hunts/, but DB row should still be finished
    assert r.exit_code == 0, r.output
    from audit_pipeline.db import FindingsDB
    db = FindingsDB(tmp_path / "findings.db")
    assert db.get_cycle("C-LIVE")["finished_at"] is not None


def test_cli_cycle_command_registered() -> None:
    from audit_pipeline.cli import main
    assert "cycle" in main.commands
