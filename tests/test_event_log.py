"""Tests for the live event-emission helper.

The Bridge dashboard's motion (Stage line, hypothesis grid, tool-call
stream) is fed by JSON-line events appended to the active cycle's
hunt.log.jsonl. The SSE service tails the file and broadcasts each
line. This test pins:
  - emit_event appends a single JSON line per call
  - JELLEO_CYCLE_LOG_PATH takes priority over JELLEO_WORKSPACE
  - JELLEO_WORKSPACE fallback resolves the latest cycle dir by mtime
  - emit_event NEVER raises, even when no path resolves
  - event payload always includes "event" and "ts"
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from audit_pipeline.utils.event_log import emit_event


def test_emit_event_writes_jsonl_line(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "hunt.log.jsonl"
    monkeypatch.setenv("JELLEO_CYCLE_LOG_PATH", str(log))
    emit_event("recon_hyp_start", hyp_id="H1", use_tools=True)
    assert log.is_file()
    line = log.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["event"] == "recon_hyp_start"
    assert rec["hyp_id"] == "H1"
    assert rec["use_tools"] is True
    assert "ts" in rec
    assert isinstance(rec["ts"], (int, float))


def test_emit_event_appends_multiple_lines(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "hunt.log.jsonl"
    monkeypatch.setenv("JELLEO_CYCLE_LOG_PATH", str(log))
    for i in range(5):
        emit_event("tool_call", hyp_id=f"H{i}", tool="read_file")
    lines = log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 5
    for i, line in enumerate(lines):
        rec = json.loads(line)
        assert rec["event"] == "tool_call"
        assert rec["hyp_id"] == f"H{i}"


def test_emit_event_no_path_is_silent(tmp_path: Path, monkeypatch) -> None:
    """No env vars set → emit_event silently no-ops, doesn't raise."""
    monkeypatch.delenv("JELLEO_CYCLE_LOG_PATH", raising=False)
    monkeypatch.delenv("JELLEO_WORKSPACE", raising=False)
    # Must not raise
    emit_event("recon_hyp_start", hyp_id="X")


def test_emit_event_falls_back_to_workspace_latest_cycle(
    tmp_path: Path, monkeypatch,
) -> None:
    """When JELLEO_CYCLE_LOG_PATH is unset, fall back to the latest
    cycle dir under JELLEO_WORKSPACE/hunts/."""
    import os
    hunts = tmp_path / "hunts"
    older = hunts / "20260510-100000"
    older.mkdir(parents=True)
    (older / "hunt.log.jsonl").touch()

    newer = hunts / "20260512-200000"
    newer.mkdir(parents=True)

    # Force a clean mtime gap regardless of OS resolution
    now = time.time()
    os.utime(older, (now - 7200, now - 7200))
    os.utime(newer, (now, now))

    monkeypatch.delenv("JELLEO_CYCLE_LOG_PATH", raising=False)
    monkeypatch.setenv("JELLEO_WORKSPACE", str(tmp_path))
    emit_event("cycle_active", cycle="20260512-200000")

    newer_log = newer / "hunt.log.jsonl"
    assert newer_log.is_file(), "should have created the file in latest cycle dir"
    older_log_content = (older / "hunt.log.jsonl").read_text(encoding="utf-8")
    assert older_log_content == "", "older cycle log should be untouched"


def test_emit_event_oserror_on_write_is_silent(
    tmp_path: Path, monkeypatch,
) -> None:
    """If the path resolves but the write fails (read-only fs, etc),
    emit_event must NOT raise and must NOT crash the cycle."""
    # Point at a parent that exists but make the file a directory so write fails
    bad = tmp_path / "weird"
    bad.mkdir()
    (bad / "hunt.log.jsonl").mkdir()  # now the "file" is a directory — open will fail
    monkeypatch.setenv("JELLEO_CYCLE_LOG_PATH", str(bad / "hunt.log.jsonl"))
    emit_event("recon_hyp_start", hyp_id="X")  # must not raise


def test_emit_event_priority_cycle_log_over_workspace(
    tmp_path: Path, monkeypatch,
) -> None:
    """JELLEO_CYCLE_LOG_PATH takes precedence over JELLEO_WORKSPACE."""
    explicit = tmp_path / "explicit.jsonl"
    hunts = tmp_path / "hunts" / "20260512"
    hunts.mkdir(parents=True)

    monkeypatch.setenv("JELLEO_CYCLE_LOG_PATH", str(explicit))
    monkeypatch.setenv("JELLEO_WORKSPACE", str(tmp_path))

    emit_event("test_event", payload="x")
    assert explicit.is_file()
    # workspace fallback path must NOT have been touched
    assert not (hunts / "hunt.log.jsonl").exists()


def test_emit_event_serializes_non_jsonable_fields(
    tmp_path: Path, monkeypatch,
) -> None:
    """Path objects and similar non-JSON-native types must serialize via
    str() rather than crash the emit."""
    log = tmp_path / "hunt.log.jsonl"
    monkeypatch.setenv("JELLEO_CYCLE_LOG_PATH", str(log))
    emit_event("paths_event", path=Path("/some/path"))
    rec = json.loads(log.read_text(encoding="utf-8").strip())
    assert "path" in rec
    assert "some" in rec["path"] and "path" in rec["path"]
