"""Behavioral tests for `audit-pipeline export-scabench`.

The export adapter is the EXIT POINT of any cycle that ships to a
ScaBench-style evaluator (OtterSec pilot, future Nethermind-grade
clients). A regression here means we ship malformed JSON to a vendor —
worse than shipping zero findings.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from audit_pipeline.cli import main as cli_main
from audit_pipeline.db import FindingsDB
from audit_pipeline.lifecycle import Status
from audit_pipeline.severity import Severity


def _seed_db(workspace: Path) -> tuple[int, str]:
    """Build a small DB with one real, one new, one rejected finding."""
    db = FindingsDB(workspace / "findings.db")
    tid = db.upsert_target("osec-solana-small", engine_repo="x")
    cid = "20260513-test"
    db.insert_cycle(cid, tid)
    db.upsert_finding(
        tid, cid, "vault_unbounded_withdraw",
        verdict="TRUE", confidence="HIGH",
        severity=Severity.HIGH, status=Status.CONFIRMED,
        title="Withdraw permits draining without amount<=total_deposits",
        details={
            "target_file": "programs/vault/src/lib.rs",
            "target_function": "withdraw",
            "target_lines": "42-65",
            "claim": "amount parameter unchecked against total_deposits",
            "impact": "admin can drain vault",
        },
    )
    db.upsert_finding(
        tid, cid, "untriaged_hyp",
        verdict="TRUE", confidence="LOW",
        severity=Severity.LOW, status=Status.NEW, title="untriaged",
    )
    db.upsert_finding(
        tid, cid, "rejected_hyp",
        verdict="FALSE", confidence="HIGH",
        severity=Severity.HIGH, status=Status.REJECTED, title="rejected",
    )
    return tid, cid


def test_export_basic_shape(tmp_path: Path) -> None:
    """Output JSON has the four required keys and the findings array shape."""
    _seed_db(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, [
        "-w", str(tmp_path), "export-scabench",
        "--target-name", "osec-solana-small",
        "--project-id", "solana-small",
    ])
    assert result.exit_code == 0, result.output
    out = tmp_path / "submissions" / "solana-small_results.json"
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    # Top-level shape
    assert payload["project"] == "solana-small"
    assert payload["total_findings"] == 1
    assert isinstance(payload["files_analyzed"], int)
    assert isinstance(payload["findings"], list)
    # Per-finding shape: must include every key the scorer expects
    f0 = payload["findings"][0]
    for key in ("title", "description", "severity", "location", "file"):
        assert key in f0, f"missing required ScaBench key: {key}"


def test_export_excludes_new_and_rejected(tmp_path: Path) -> None:
    """Findings still in NEW or REJECTED never make it into the submission.

    REGRESSION: any path that lets pre-triage noise or known-false
    findings escape to a vendor evaluator is a SHIP-BLOCKER. Pin it
    with a behavioral test.
    """
    _seed_db(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, [
        "-w", str(tmp_path), "export-scabench",
        "--target-name", "osec-solana-small",
    ])
    assert result.exit_code == 0
    out_path = tmp_path / "submissions" / "osec-solana-small_results.json"
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    ids = {f["title"] for f in payload["findings"]}
    assert "untriaged" not in ids, "NEW-status finding leaked into submission"
    assert "rejected" not in ids, "REJECTED finding leaked into submission"


def test_severity_maps_to_lowercase(tmp_path: Path) -> None:
    """ScaBench expects lowercase severity. Critical collapses to 'high'."""
    db = FindingsDB(tmp_path / "findings.db")
    tid = db.upsert_target("t", engine_repo="x")
    db.insert_cycle("c1", tid)
    for sev_internal, sev_expected in [
        (Severity.CRITICAL, "high"),
        (Severity.HIGH,     "high"),
        (Severity.MEDIUM,   "medium"),
        (Severity.LOW,      "low"),
    ]:
        db.upsert_finding(
            tid, "c1", f"hyp_{sev_internal.value}",
            verdict="TRUE", confidence="HIGH",
            severity=sev_internal, status=Status.CONFIRMED,
            title=f"finding {sev_internal.value}",
        )
    runner = CliRunner()
    result = runner.invoke(cli_main, [
        "-w", str(tmp_path), "export-scabench",
        "--target-name", "t", "--project-id", "p",
    ])
    assert result.exit_code == 0
    payload = json.loads(
        (tmp_path / "submissions" / "p_results.json").read_text(encoding="utf-8")
    )
    found = {f["title"]: f["severity"] for f in payload["findings"]}
    assert found["finding Critical"] == "high"
    assert found["finding High"] == "high"
    assert found["finding Medium"] == "medium"
    assert found["finding Low"] == "low"


def test_info_excluded_by_default_included_with_flag(tmp_path: Path) -> None:
    """Info severity drops by default (don't pollute the FP count). The
    --include-info flag lets you submit them anyway if you really want."""
    db = FindingsDB(tmp_path / "findings.db")
    tid = db.upsert_target("t", engine_repo="x")
    db.insert_cycle("c1", tid)
    db.upsert_finding(
        tid, "c1", "info_only",
        verdict="TRUE", confidence="HIGH",
        severity=Severity.INFO, status=Status.CONFIRMED,
        title="info-only finding",
    )
    runner = CliRunner()
    # Default: info excluded
    r1 = runner.invoke(cli_main, [
        "-w", str(tmp_path), "export-scabench",
        "--target-name", "t", "--project-id", "p1",
    ])
    assert r1.exit_code == 0
    p1 = json.loads((tmp_path / "submissions" / "p1_results.json").read_text())
    assert p1["total_findings"] == 0
    # With flag: info included
    r2 = runner.invoke(cli_main, [
        "-w", str(tmp_path), "export-scabench",
        "--target-name", "t", "--project-id", "p2",
        "--include-info",
    ])
    assert r2.exit_code == 0
    p2 = json.loads((tmp_path / "submissions" / "p2_results.json").read_text())
    assert p2["total_findings"] == 1
    assert p2["findings"][0]["severity"] == "info"


def test_cycle_filter(tmp_path: Path) -> None:
    """--cycle-id scopes output to that cycle only."""
    db = FindingsDB(tmp_path / "findings.db")
    tid = db.upsert_target("t", engine_repo="x")
    db.insert_cycle("c1", tid)
    db.insert_cycle("c2", tid)
    db.upsert_finding(
        tid, "c1", "in_c1",
        verdict="TRUE", confidence="HIGH",
        severity=Severity.HIGH, status=Status.CONFIRMED, title="in_c1",
    )
    db.upsert_finding(
        tid, "c2", "in_c2",
        verdict="TRUE", confidence="HIGH",
        severity=Severity.HIGH, status=Status.CONFIRMED, title="in_c2",
    )
    runner = CliRunner()
    result = runner.invoke(cli_main, [
        "-w", str(tmp_path), "export-scabench",
        "--cycle-id", "c1", "--project-id", "p",
    ])
    assert result.exit_code == 0
    payload = json.loads(
        (tmp_path / "submissions" / "p_results.json").read_text(encoding="utf-8")
    )
    titles = {f["title"] for f in payload["findings"]}
    assert "in_c1" in titles
    assert "in_c2" not in titles


def test_location_falls_back_gracefully(tmp_path: Path) -> None:
    """If details_json has no target_file/function/lines, we still emit
    a non-empty location (hypothesis_id as last resort)."""
    db = FindingsDB(tmp_path / "findings.db")
    tid = db.upsert_target("t", engine_repo="x")
    db.insert_cycle("c1", tid)
    db.upsert_finding(
        tid, "c1", "bare_hyp",
        verdict="TRUE", confidence="HIGH",
        severity=Severity.HIGH, status=Status.CONFIRMED,
        title="finding with no details",
    )
    runner = CliRunner()
    result = runner.invoke(cli_main, [
        "-w", str(tmp_path), "export-scabench",
        "--target-name", "t", "--project-id", "p",
    ])
    assert result.exit_code == 0
    payload = json.loads(
        (tmp_path / "submissions" / "p_results.json").read_text(encoding="utf-8")
    )
    f0 = payload["findings"][0]
    assert f0["location"] == "bare_hyp"  # falls back to hypothesis_id
    assert f0["file"] == "(file unknown)"


# ---------------------------------------------------------------------------
# REGRESSION TESTS — added 2026-05-13 after 18-agent self-audit found
# three CRITICAL gaps in the ScaBench export. Each test pins one of those
# gaps so it can't silently re-open.
# ---------------------------------------------------------------------------


def test_description_includes_location_and_file_inline(tmp_path: Path) -> None:
    """REGRESSION: Nethermind's auditagent-scoring-algo matcher LLM only
    reads `title` + `description` when deciding whether two findings are
    the same. The top-level `location` and `file` JSON fields are NOT
    consulted during matching — they only show in the human-readable
    output. Without inlining them into description, every per-location
    finding looks like the same generic claim to the matcher.

    Pin that location + file appear inside the description body so
    the scoring LLM actually sees them.
    """
    db = FindingsDB(tmp_path / "findings.db")
    tid = db.upsert_target("t", engine_repo="x")
    db.insert_cycle("c1", tid)
    db.upsert_finding(
        tid, "c1", "rich_hyp",
        verdict="TRUE", confidence="HIGH",
        severity=Severity.HIGH, status=Status.CONFIRMED,
        title="Withdraw drains vault",
        details={
            "target_file": "programs/vault/src/lib.rs",
            "target_function": "withdraw",
            "target_lines": "42-65",
            "claim": "no amount cap",
        },
    )
    runner = CliRunner()
    runner.invoke(cli_main, [
        "-w", str(tmp_path), "export-scabench",
        "--target-name", "t", "--project-id", "p",
    ])
    payload = json.loads((tmp_path / "submissions" / "p_results.json").read_text())
    desc = payload["findings"][0]["description"]
    assert "programs/vault/src/lib.rs" in desc, (
        "target_file must appear in description for the scorer's matcher"
    )
    assert "withdraw" in desc, (
        "target_function must appear in description for the scorer's matcher"
    )


def test_closed_not_planned_excluded_from_submission(tmp_path: Path) -> None:
    """REGRESSION: closed_not_planned is an internal lifecycle label for
    "found but not actionable / by-design". It should NEVER reach a
    vendor evaluation submission — to the scorer it looks like a real
    finding the auditor is claiming, and inflates the FP count.

    The audit found this label was being shipped to evaluators. Pin that
    it's now blocked.
    """
    db = FindingsDB(tmp_path / "findings.db")
    tid = db.upsert_target("t", engine_repo="x")
    db.insert_cycle("c1", tid)
    db.upsert_finding(
        tid, "c1", "closed_hyp",
        verdict="TRUE", confidence="HIGH",
        severity=Severity.HIGH, status=Status.CLOSED_NOT_PLANNED,
        title="we-found-it-but-wont-fix",
    )
    db.upsert_finding(
        tid, "c1", "confirmed_hyp",
        verdict="TRUE", confidence="HIGH",
        severity=Severity.HIGH, status=Status.CONFIRMED,
        title="real-confirmed-bug",
    )
    runner = CliRunner()
    runner.invoke(cli_main, [
        "-w", str(tmp_path), "export-scabench",
        "--target-name", "t", "--project-id", "p",
    ])
    payload = json.loads((tmp_path / "submissions" / "p_results.json").read_text())
    titles = {f["title"] for f in payload["findings"]}
    assert "we-found-it-but-wont-fix" not in titles, (
        "closed_not_planned must not appear in vendor submission"
    )
    assert "real-confirmed-bug" in titles


def test_vulnerability_type_emitted_for_known_bug_class(tmp_path: Path) -> None:
    """REGRESSION: ScaBench's per-category F1 rollup groups findings by
    `vulnerability_type`. Without this field every finding categorizes
    to 'Other' and the cross-category recall metrics collapse to a flat
    list, hurting how OSec's internal eval reads our coverage.
    """
    db = FindingsDB(tmp_path / "findings.db")
    tid = db.upsert_target("t", engine_repo="x")
    db.insert_cycle("c1", tid)
    db.upsert_finding(
        tid, "c1", "reent_hyp",
        verdict="TRUE", confidence="HIGH",
        severity=Severity.HIGH, status=Status.CONFIRMED,
        title="reentrancy in withdraw",
        details={"bug_class": "reentrancy"},
    )
    db.upsert_finding(
        tid, "c1", "arith_hyp",
        verdict="TRUE", confidence="HIGH",
        severity=Severity.HIGH, status=Status.CONFIRMED,
        title="overflow in mint",
        details={"bug_class": "arithmetic_overflow"},
    )
    runner = CliRunner()
    runner.invoke(cli_main, [
        "-w", str(tmp_path), "export-scabench",
        "--target-name", "t", "--project-id", "p",
    ])
    payload = json.loads((tmp_path / "submissions" / "p_results.json").read_text())
    by_title = {f["title"]: f for f in payload["findings"]}
    assert by_title["reentrancy in withdraw"]["vulnerability_type"] == "Reentrancy"
    assert by_title["overflow in mint"]["vulnerability_type"] == "Arithmetic"
