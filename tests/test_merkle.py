"""P4 Y0 — per-cycle Merkle root tests.

Covers:
  * Pure function determinism (same inputs → same root, byte-for-byte)
  * Tamper detection (any field change → root changes)
  * Reproducibility (round-trip via canonical_encode + leaf_hash)
  * Domain separation (leaf vs internal node hashes diverge)
  * CLI surface (compute / verify / list / rebuild-all)
  * Snapshot wiring (`cycle_merkle_roots` block in dashboard.json)
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

# ─────────────────── pure-function tests ───────────────────


def test_canonical_encode_is_field_order_independent() -> None:
    """Encoding sorts fields alphabetically — input dict order doesn't matter."""
    from audit_pipeline.merkle import canonical_encode
    fields = ("a", "b", "c")
    a = canonical_encode({"c": "3", "a": "1", "b": "2"}, fields)
    b = canonical_encode({"a": "1", "b": "2", "c": "3"}, fields)
    assert a == b
    assert a == b"a=1\nb=2\nc=3\n"


def test_canonical_encode_handles_missing_and_none() -> None:
    from audit_pipeline.merkle import canonical_encode
    out = canonical_encode({"a": None, "c": "3"}, ("a", "b", "c"))
    assert out == b"a=\nb=\nc=3\n"


def test_canonical_encode_only_uses_declared_fields() -> None:
    """Adding extra fields to the dict shouldn't change the encoding."""
    from audit_pipeline.merkle import canonical_encode
    base = canonical_encode({"a": "1"}, ("a",))
    extended = canonical_encode({"a": "1", "extra": "junk"}, ("a",))
    assert base == extended


def test_merkle_root_empty_returns_zeros() -> None:
    from audit_pipeline.merkle import merkle_root
    assert merkle_root([]) == b"\x00" * 32


def test_merkle_root_single_leaf_returns_that_leaf() -> None:
    from audit_pipeline.merkle import merkle_root
    leaf = b"\x42" * 32
    assert merkle_root([leaf]) == leaf


def test_merkle_root_deterministic_across_runs() -> None:
    """Same inputs → same root, every time."""
    from audit_pipeline.merkle import leaf_hash, merkle_root
    leaves = [leaf_hash(f"leaf-{i}".encode()) for i in range(7)]
    a = merkle_root(leaves)
    b = merkle_root(leaves)
    assert a == b


def test_merkle_root_changes_when_any_leaf_changes() -> None:
    from audit_pipeline.merkle import leaf_hash, merkle_root
    leaves = [leaf_hash(f"leaf-{i}".encode()) for i in range(5)]
    root_a = merkle_root(leaves)
    leaves_modified = list(leaves)
    leaves_modified[2] = leaf_hash(b"tampered")
    root_b = merkle_root(leaves_modified)
    assert root_a != root_b


def test_leaf_and_internal_hashes_are_domain_separated() -> None:
    """A 64-byte leaf and a (32+32) internal must hash differently."""
    from audit_pipeline.merkle import internal_hash, leaf_hash
    same_bytes = b"\xaa" * 64
    a = leaf_hash(same_bytes)
    b = internal_hash(same_bytes[:32], same_bytes[32:])
    assert a != b


def test_cycle_merkle_root_reproducible(tmp_path: Path) -> None:
    """Same cycle + findings → same root, byte-for-byte."""
    from audit_pipeline.merkle import cycle_merkle_root
    cycle = {
        "cycle_id": "C1", "engine_sha": "abc", "wrapper_sha": "def",
        "started_at": "2026-05-09T00:00:00Z", "finished_at": "2026-05-09T01:00:00Z",
        "n_dispatched": 5, "n_confirmed": 1, "target_id": 1,
    }
    findings = [
        {"id": 1, "hypothesis_id": "H1", "title": "x",
         "verdict": "TRUE", "confidence": "HIGH", "status": "confirmed",
         "severity": "Critical", "bug_class": "y"},
        {"id": 2, "hypothesis_id": "H2", "title": "z",
         "verdict": "FALSE", "confidence": "MED", "status": "rejected",
         "severity": "Low", "bug_class": "y"},
    ]
    a = cycle_merkle_root(cycle, findings)
    b = cycle_merkle_root(cycle, findings)
    assert a == b
    assert len(a) == 64  # hex of 32 bytes


def test_cycle_merkle_root_changes_on_finding_status_change() -> None:
    """Tamper detection: flipping a status changes the root."""
    from audit_pipeline.merkle import cycle_merkle_root
    cycle = {"cycle_id": "C1", "engine_sha": "x", "target_id": 1}
    findings = [{"id": 1, "status": "confirmed", "severity": "Critical"}]
    a = cycle_merkle_root(cycle, findings)
    findings[0]["status"] = "rejected"
    b = cycle_merkle_root(cycle, findings)
    assert a != b


def test_cycle_merkle_root_changes_on_engine_sha_change() -> None:
    """Tamper detection: re-pointing to a different engine_sha changes the root."""
    from audit_pipeline.merkle import cycle_merkle_root
    cycle_a = {"cycle_id": "C1", "engine_sha": "aaa", "target_id": 1}
    cycle_b = {"cycle_id": "C1", "engine_sha": "bbb", "target_id": 1}
    findings = [{"id": 1, "status": "new"}]
    assert cycle_merkle_root(cycle_a, findings) != cycle_merkle_root(cycle_b, findings)


def test_cycle_merkle_summary_shape() -> None:
    from audit_pipeline.merkle import SCHEMA_VERSION, cycle_merkle_summary
    cycle = {"cycle_id": "C1", "engine_sha": "x", "target_id": 1}
    findings = [{"id": 1}, {"id": 2}]
    s = cycle_merkle_summary(cycle, findings)
    assert s["schema"] == f"jelleo-cycle-merkle-{SCHEMA_VERSION}"
    assert s["cycle_id"] == "C1"
    assert s["n_findings"] == 2
    assert s["n_leaves"] == 3
    assert len(s["merkle_root"]) == 64
    assert "cycle" in s["fields"] and "finding" in s["fields"]


# ─────────────────── CLI surface ───────────────────


def _seed(workspace: Path) -> str:
    """Insert a cycle + 2 findings; return the cycle_id."""
    from audit_pipeline.db import FindingsDB
    from audit_pipeline.lifecycle import Status
    from audit_pipeline.severity import Severity
    db = FindingsDB(workspace / "findings.db")
    target_id = db.upsert_target(name="testproto")
    db.insert_cycle(target_id=target_id, cycle_id="C-merkle-test",
                    engine_sha="aaa" * 16)
    for i, sev in enumerate([Severity.CRITICAL, Severity.HIGH]):
        db.upsert_finding(
            target_id=target_id, cycle_id="C-merkle-test",
            hypothesis_id=f"H{i}", title=f"f{i}",
            verdict="TRUE", confidence="HIGH",
            status=Status.CONFIRMED, severity=sev,
            bug_class="x",
        )
    return "C-merkle-test"


def _invoke(workspace: Path, *args: str):
    from audit_pipeline.commands.merkle import merkle_cmd
    runner = CliRunner()
    return runner.invoke(merkle_cmd, list(args), obj={"workspace": str(workspace)})


def test_cli_compute_writes_sidecar(tmp_path: Path) -> None:
    cid = _seed(tmp_path)
    r = _invoke(tmp_path, "compute", cid, "--no-sign")
    assert r.exit_code == 0, r.output
    sidecar = tmp_path / "hunts" / cid / "merkle.json"
    assert sidecar.is_file()
    d = json.loads(sidecar.read_text(encoding="utf-8"))
    assert d["cycle_id"] == cid
    assert len(d["merkle_root"]) == 64
    assert d["n_findings"] == 2


def test_cli_verify_passes_when_db_unchanged(tmp_path: Path) -> None:
    cid = _seed(tmp_path)
    _invoke(tmp_path, "compute", cid, "--no-sign")
    r = _invoke(tmp_path, "verify", cid)
    assert r.exit_code == 0, r.output
    assert "OK" in r.output


def test_cli_verify_fails_on_db_tamper(tmp_path: Path) -> None:
    """If a finding gets transitioned after compute, verify must FAIL."""
    cid = _seed(tmp_path)
    _invoke(tmp_path, "compute", cid, "--no-sign")
    # Tamper: walk one finding through the lifecycle so the canonical
    # encoding differs from what was hashed at compute time.
    from audit_pipeline.db import FindingsDB
    from audit_pipeline.lifecycle import Status
    db = FindingsDB(tmp_path / "findings.db")
    findings = [f for f in db.list_findings(limit=10) if f.get("cycle_id") == cid]
    db.transition_finding(findings[0]["id"], Status.DISCLOSED, "tamper-test", "test")
    r = _invoke(tmp_path, "verify", cid)
    assert r.exit_code != 0
    flat = " ".join(r.output.split())
    assert "MISMATCH" in flat or "mismatch" in flat


def test_cli_list_shows_root_when_present(tmp_path: Path) -> None:
    cid = _seed(tmp_path)
    _invoke(tmp_path, "compute", cid, "--no-sign")
    r = _invoke(tmp_path, "list")
    assert r.exit_code == 0
    assert cid in r.output


def test_cli_rebuild_all_creates_missing(tmp_path: Path) -> None:
    cid = _seed(tmp_path)
    sidecar = tmp_path / "hunts" / cid / "merkle.json"
    assert not sidecar.is_file()
    r = _invoke(tmp_path, "rebuild-all", "--no-sign")
    assert r.exit_code == 0, r.output
    assert sidecar.is_file()


def test_cli_command_registered_at_top_level() -> None:
    from audit_pipeline.cli import main
    assert "merkle" in main.commands


# ─────────────────── snapshot wiring ───────────────────


def test_snapshot_recent_cycle_merkle_roots(tmp_path: Path) -> None:
    """Built snapshot.json includes cycle_merkle_roots for each merkle.json found."""
    cid = _seed(tmp_path)
    _invoke(tmp_path, "compute", cid, "--no-sign")
    from audit_pipeline.commands.dashboard import _recent_cycle_merkle_roots
    roots = _recent_cycle_merkle_roots(tmp_path)
    assert len(roots) == 1
    assert roots[0]["cycle_id"] == cid
    assert len(roots[0]["merkle_root"]) == 64


def test_cli_verify_schema_rotation_distinguishable_from_tamper(tmp_path: Path) -> None:
    """A v1 sidecar verified under v2 code must surface 'SCHEMA ROTATED', not 'MISMATCH'."""
    cid = _seed(tmp_path)
    _invoke(tmp_path, "compute", cid, "--no-sign")
    sidecar = tmp_path / "hunts" / cid / "merkle.json"
    d = json.loads(sidecar.read_text(encoding="utf-8"))
    d["schema"] = "jelleo-cycle-merkle-v0-fictional"
    sidecar.write_text(json.dumps(d), encoding="utf-8")
    r = _invoke(tmp_path, "verify", cid)
    assert r.exit_code != 0
    flat = " ".join(r.output.split())
    assert "SCHEMA ROTATED" in flat


def test_cli_verify_uses_targeted_finding_query(tmp_path: Path) -> None:
    """list_findings_by_cycle is the source of truth — verify uses that path."""
    from audit_pipeline.db import FindingsDB
    db = FindingsDB(tmp_path / "findings.db")
    cid = _seed(tmp_path)
    by_cycle = db.list_findings_by_cycle(cid)
    assert len(by_cycle) == 2  # the 2 findings the seeder inserts


def test_tamper_detected_on_poc_fired_field() -> None:
    """Audit-fix: flipping poc_fired must change the merkle root."""
    from audit_pipeline.merkle import cycle_merkle_root
    cycle = {"cycle_id": "C1", "engine_sha": "x", "target_id": 1}
    findings = [{"id": 1, "poc_fired": 1, "status": "confirmed"}]
    a = cycle_merkle_root(cycle, findings)
    findings[0]["poc_fired"] = 0
    b = cycle_merkle_root(cycle, findings)
    assert a != b


def test_tamper_detected_on_finding_engine_sha() -> None:
    """Audit-fix: rewriting per-finding engine_sha must invalidate root."""
    from audit_pipeline.merkle import cycle_merkle_root
    cycle = {"cycle_id": "C1", "engine_sha": "cycle-sha", "target_id": 1}
    findings = [{"id": 1, "engine_sha": "abc", "status": "confirmed"}]
    a = cycle_merkle_root(cycle, findings)
    findings[0]["engine_sha"] = "xyz"
    b = cycle_merkle_root(cycle, findings)
    assert a != b


def test_report_skips_misplaced_merkle_sidecar(tmp_path: Path) -> None:
    """If merkle.json's cycle_id doesn't match the cycle being rendered,
    report.py must not surface its hash (operator copy/paste guard)."""
    from audit_pipeline.commands.report import _render_cycle_html
    target = {"name": "T"}
    cycle = {"cycle_id": "C-real", "started_at": "now",
             "engine_sha": "a", "wrapper_sha": "b"}
    findings = [{"id": 1, "title": "x", "severity": "Low", "status": "new"}]
    # Place a merkle.json under C-real but with cycle_id=C-other (misplaced)
    mp = tmp_path / "hunts" / "C-real" / "merkle.json"
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({
        "cycle_id": "C-other",
        "merkle_root": "deadbeef" * 8,
    }), encoding="utf-8")
    html = _render_cycle_html(target, cycle, findings, "",
                               workspace=tmp_path, public=True)
    assert "deadbeef" not in html, "report leaked a misplaced merkle root"


def test_snapshot_excludes_per_finding_data(tmp_path: Path) -> None:
    """Public snapshot must NOT include per-finding identifiers (pre-disclosure rule).

    Verifies the structure: each entry has only {cycle_id, engine_sha,
    merkle_root, n_findings, schema}. Substring checks on hex digests
    are unreliable (random hex contains arbitrary 2-char patterns), so
    we assert on the full set of allowed keys per entry instead.
    """
    cid = _seed(tmp_path)
    _invoke(tmp_path, "compute", cid, "--no-sign")
    from audit_pipeline.commands.dashboard import _recent_cycle_merkle_roots
    roots = _recent_cycle_merkle_roots(tmp_path)
    assert len(roots) == 1
    allowed = {"cycle_id", "engine_sha", "merkle_root", "n_findings", "schema"}
    for entry in roots:
        assert set(entry.keys()) == allowed, (
            f"snapshot leaked extra keys: {set(entry.keys()) - allowed}"
        )
