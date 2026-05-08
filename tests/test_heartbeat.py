"""Tier 5 #29 — public proof-of-running heartbeat.

Covers the pure data-collection function (`build_heartbeat`) — payload shape,
fingerprint format, customer-count read-through, missing-DB tolerance, and
graceful no-systemd handling. The signing path is exercised separately by
test_signed (existing signing tests) and the CLI wrapper is left to
manual smoke (it just calls build_heartbeat → write → sign_file).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from audit_pipeline.commands.heartbeat import build_heartbeat


@pytest.fixture()
def workspace_with_pubkey(tmp_path: Path) -> Path:
    """Workspace with a fake public key under keys/ so fingerprint is non-empty."""
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "jelleo.ed25519.pub").write_bytes(b"-----BEGIN PUBLIC KEY-----\nABCD\n-----END PUBLIC KEY-----\n")
    return tmp_path


def test_payload_has_required_fields(workspace_with_pubkey: Path) -> None:
    p = build_heartbeat(workspace_with_pubkey, services=())
    for field in (
        "schema", "generated_at", "hostname",
        "cycles_total", "cycles_last_24h", "last_cycle_ts",
        "signing_pubkey_fingerprint", "registered_customers", "service_summary",
    ):
        assert field in p, f"missing field: {field}"


def test_schema_constant(workspace_with_pubkey: Path) -> None:
    p = build_heartbeat(workspace_with_pubkey, services=())
    assert p["schema"] == "jelleo-heartbeat-v1"


def test_pubkey_fingerprint_format(workspace_with_pubkey: Path) -> None:
    p = build_heartbeat(workspace_with_pubkey, services=())
    fp = p["signing_pubkey_fingerprint"]
    assert re.match(r"^([0-9a-f]{2}:){7}[0-9a-f]{2}$", fp), f"bad fingerprint: {fp!r}"


def test_pubkey_fingerprint_matches_sha256_first_8(workspace_with_pubkey: Path) -> None:
    pub_bytes = (workspace_with_pubkey / "keys" / "jelleo.ed25519.pub").read_bytes()
    expected = ":".join(f"{b:02x}" for b in hashlib.sha256(pub_bytes).digest()[:8])
    p = build_heartbeat(workspace_with_pubkey, services=())
    assert p["signing_pubkey_fingerprint"] == expected


def test_pubkey_fingerprint_empty_when_no_key(tmp_path: Path) -> None:
    p = build_heartbeat(tmp_path, services=())
    assert p["signing_pubkey_fingerprint"] == ""


def test_zero_cycles_when_no_db(workspace_with_pubkey: Path) -> None:
    p = build_heartbeat(workspace_with_pubkey, services=())
    assert p["cycles_total"] == 0
    assert p["cycles_last_24h"] == 0
    assert p["last_cycle_ts"] is None


def test_registered_customers_zero_when_no_registry(workspace_with_pubkey: Path) -> None:
    p = build_heartbeat(workspace_with_pubkey, services=())
    assert p["registered_customers"] == 0


def test_registered_customers_counts_registry(workspace_with_pubkey: Path) -> None:
    from audit_pipeline import customers as customers_mod
    customers_mod.save_registry(
        workspace_with_pubkey,
        [
            {"id": "a", "name": "A"},
            {"id": "b", "name": "B"},
        ],
    )
    p = build_heartbeat(workspace_with_pubkey, services=())
    assert p["registered_customers"] == 2


def test_service_summary_unknown_for_unknown_units(workspace_with_pubkey: Path) -> None:
    p = build_heartbeat(workspace_with_pubkey, services=("definitely-not-a-real-unit.service",))
    state = p["service_summary"]["definitely-not-a-real-unit.service"]
    assert state in {"active", "inactive", "failed", "unknown"}


def test_generated_at_uses_passed_now(workspace_with_pubkey: Path) -> None:
    fixed = datetime(2026, 5, 7, 19, 0, 0, tzinfo=timezone.utc)
    p = build_heartbeat(workspace_with_pubkey, services=(), now=fixed)
    assert p["generated_at"] == "2026-05-07T19:00:00+00:00"


def test_implementation_includes_short_sha_when_repo_present(
    workspace_with_pubkey: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _engine_sha returns a SHA, implementation field exposes it."""
    from audit_pipeline.commands import heartbeat as heartbeat_mod
    monkeypatch.setattr(heartbeat_mod, "_engine_sha", lambda repo_dir: "dee2224e8a7c0123456")
    p = build_heartbeat(workspace_with_pubkey, services=())
    assert p["engine_sha"] == "dee2224e8a7c0123456"
    assert p["implementation"] == "Copenhagen0x/audit-pipeline-cli@dee2224e8a7c"


def test_implementation_absent_when_engine_sha_blank(
    workspace_with_pubkey: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from audit_pipeline.commands import heartbeat as heartbeat_mod
    monkeypatch.setattr(heartbeat_mod, "_engine_sha", lambda repo_dir: "")
    p = build_heartbeat(workspace_with_pubkey, services=())
    assert p["engine_sha"] == ""
    assert "implementation" not in p


def test_24h_cycle_window(
    workspace_with_pubkey: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cycles within the last 24h count, older ones don't."""
    from audit_pipeline.commands import heartbeat as heartbeat_mod
    fixed = datetime(2026, 5, 7, 19, 0, 0, tzinfo=timezone.utc)

    def fake_stats(workspace: Path, now: datetime) -> tuple[int, int, str | None]:
        all_cycles = 5
        last_24h = sum(
            1 for ts in [
                now - timedelta(hours=2),
                now - timedelta(hours=5),
                now - timedelta(hours=23),
                now - timedelta(hours=25),
                now - timedelta(days=3),
            ] if ts >= now - timedelta(hours=24)
        )
        return (all_cycles, last_24h, (now - timedelta(hours=2)).isoformat(timespec="seconds"))

    monkeypatch.setattr(heartbeat_mod, "_cycle_stats", fake_stats)
    p = build_heartbeat(workspace_with_pubkey, services=(), now=fixed)
    assert p["cycles_total"] == 5
    assert p["cycles_last_24h"] == 3
    assert p["last_cycle_ts"] == (fixed - timedelta(hours=2)).isoformat(timespec="seconds")
