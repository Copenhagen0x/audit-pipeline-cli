"""P2 D13 — sibling review CLI tests."""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner


def _seed(workspace: Path) -> int:
    """Create a finding + a derived siblings YAML for it. Returns finding_id."""
    from audit_pipeline.db import FindingsDB
    from audit_pipeline.lifecycle import Status
    from audit_pipeline.severity import Severity

    db = FindingsDB(workspace / "findings.db")
    target_id = db.upsert_target(name="test_protocol")
    db.insert_cycle(target_id=target_id, cycle_id="C1")
    fid = db.upsert_finding(
        target_id=target_id, cycle_id="C1",
        hypothesis_id="V7-test-class", title="test",
        verdict="TRUE", confidence="HIGH",
        status=Status.CONFIRMED, severity=Severity.CRITICAL,
        bug_class="insurance-counter-vault-divergence",
    )

    derived = workspace / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    (derived / "V7-test-class-siblings.yaml").write_text(
        yaml.safe_dump({
            "hypotheses": [
                {"id": "SIB-V7-1", "severity": "Critical", "claim": "x", "bug_class": "y"},
                {"id": "SIB-V7-2", "severity": "High",     "claim": "y", "bug_class": "z"},
            ],
        }),
        encoding="utf-8",
    )
    return fid


def _invoke(workspace: Path, *args: str):
    """Invoke a triage-siblings subcommand with workspace-bound context."""
    from audit_pipeline.commands.triage_siblings import triage_siblings_cmd
    runner = CliRunner()
    return runner.invoke(
        triage_siblings_cmd,
        list(args),
        obj={"workspace": str(workspace)},
    )


def test_list_shows_pending(tmp_path: Path) -> None:
    _seed(tmp_path)
    r = _invoke(tmp_path, "list")
    assert r.exit_code == 0, r.output
    assert "PENDING" in r.output
    assert "V7-test-class" in r.output


def test_show_prints_yaml(tmp_path: Path) -> None:
    fid = _seed(tmp_path)
    r = _invoke(tmp_path, "show", str(fid))
    assert r.exit_code == 0, r.output
    assert "SIB-V7-1" in r.output
    assert "SIB-V7-2" in r.output


def test_approve_moves_file(tmp_path: Path) -> None:
    fid = _seed(tmp_path)
    r = _invoke(tmp_path, "approve", str(fid))
    assert r.exit_code == 0, r.output
    assert (tmp_path / "derived" / "approved" / "V7-test-class-siblings.yaml").is_file()
    assert not (tmp_path / "derived" / "V7-test-class-siblings.yaml").is_file()


def test_reject_moves_file_and_writes_reason(tmp_path: Path) -> None:
    fid = _seed(tmp_path)
    r = _invoke(tmp_path, "reject", str(fid), "--reason", "noise")
    assert r.exit_code == 0, r.output
    assert (tmp_path / "derived" / "rejected" / "V7-test-class-siblings.yaml").is_file()
    assert (tmp_path / "derived" / "rejected" / "V7-test-class-siblings.reason.txt").is_file()


def test_merge_appends_into_target_library(tmp_path: Path) -> None:
    fid = _seed(tmp_path)
    # First approve
    _invoke(tmp_path, "approve", str(fid))

    # Create target class library YAML
    target = tmp_path / "perp_dex_class.yaml"
    target.write_text(
        yaml.safe_dump({"hypotheses": [{"id": "EXISTING-1", "severity": "Low",
                                         "claim": "z", "bug_class": "w"}]}),
        encoding="utf-8",
    )

    r = _invoke(tmp_path, "merge", str(fid), "--target", str(target))
    assert r.exit_code == 0, r.output

    # Target file should now have EXISTING-1 + 2 SIBs = 3 entries
    doc = yaml.safe_load(target.read_text(encoding="utf-8"))
    ids = [h["id"] for h in doc["hypotheses"]]
    assert ids == ["EXISTING-1", "SIB-V7-1", "SIB-V7-2"]
    # Source file moved to derived/merged/
    assert (tmp_path / "derived" / "merged" / "V7-test-class-siblings.yaml").is_file()


def test_merge_skips_duplicate_ids(tmp_path: Path) -> None:
    fid = _seed(tmp_path)
    _invoke(tmp_path, "approve", str(fid))

    # Target already has SIB-V7-1
    target = tmp_path / "x.yaml"
    target.write_text(
        yaml.safe_dump({"hypotheses": [
            {"id": "SIB-V7-1", "severity": "Critical", "claim": "old", "bug_class": "old"},
        ]}),
        encoding="utf-8",
    )

    r = _invoke(tmp_path, "merge", str(fid), "--target", str(target))
    assert r.exit_code == 0, r.output
    assert "skipped 1 dup" in r.output
    doc = yaml.safe_load(target.read_text(encoding="utf-8"))
    ids = [h["id"] for h in doc["hypotheses"]]
    # Old one preserved (not overwritten), only SIB-V7-2 added
    assert ids == ["SIB-V7-1", "SIB-V7-2"]
    # Old SIB-V7-1's claim must be preserved
    sib1 = next(h for h in doc["hypotheses"] if h["id"] == "SIB-V7-1")
    assert sib1["claim"] == "old"


def test_show_errors_when_finding_missing(tmp_path: Path) -> None:
    _seed(tmp_path)
    r = _invoke(tmp_path, "show", "99999")
    assert r.exit_code != 0
    assert "no sibling YAML" in r.output


def test_merge_requires_approved_status(tmp_path: Path) -> None:
    """merge from pending state must error pointing operator at approve first."""
    fid = _seed(tmp_path)
    target = tmp_path / "x.yaml"
    target.write_text(yaml.safe_dump({"hypotheses": []}), encoding="utf-8")
    r = _invoke(tmp_path, "merge", str(fid), "--target", str(target))
    assert r.exit_code != 0
    assert "approve" in r.output.lower()


def test_cli_command_registered() -> None:
    """triage-siblings must be exposed at the top-level CLI."""
    from audit_pipeline.cli import main
    sub_names = list(main.commands.keys())
    assert "triage-siblings" in sub_names, f"missing; have: {sub_names}"
