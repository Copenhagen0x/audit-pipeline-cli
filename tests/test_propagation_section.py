"""P2 H29/H30 — propagation section in cycle/weekly reports."""

from __future__ import annotations

from pathlib import Path

import yaml


def _make_finding(fid: int, hyp: str = "V7-test", title: str = "test") -> dict:
    return {
        "id": fid,
        "hypothesis_id": hyp,
        "title": title,
        "severity": "Critical",
        "status": "confirmed",
        "bug_class": "insurance-counter-vault-divergence",
    }


def _seed_propagation_artifacts(workspace: Path, finding_id: int, hyp: str) -> None:
    """Create derived siblings YAML + propagation report + chain HTML for a finding."""
    derived = workspace / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    (derived / f"{hyp}-siblings.yaml").write_text(
        yaml.safe_dump({"hypotheses": [
            {"id": "SIB-1", "severity": "Critical", "claim": "x", "bug_class": "y"},
            {"id": "SIB-2", "severity": "High", "claim": "y", "bug_class": "z"},
        ]}),
        encoding="utf-8",
    )

    autofire = workspace / "recon" / "propagate" / "auto-fire"
    autofire.mkdir(parents=True, exist_ok=True)
    (autofire / f"propagation_finding_{finding_id}_test.md").write_text("# report", encoding="utf-8")

    chains = workspace / "recon" / "propagate" / "chains"
    chains.mkdir(parents=True, exist_ok=True)
    (chains / f"{finding_id}.html").write_text("<html></html>", encoding="utf-8")


def test_section_empty_when_no_artifacts(tmp_path: Path) -> None:
    from audit_pipeline.commands.report import _propagation_section
    findings = [_make_finding(1)]
    out = _propagation_section(tmp_path, findings, public=True)
    assert out == ""


def test_section_public_shows_only_counters(tmp_path: Path) -> None:
    """Public mode: counters only — no finding ID/title leaks."""
    from audit_pipeline.commands.report import _propagation_section
    _seed_propagation_artifacts(tmp_path, finding_id=42, hyp="V7-secret-finding")
    findings = [_make_finding(42, hyp="V7-secret-finding", title="Critical undisclosed bug")]
    html = _propagation_section(tmp_path, findings, public=True)
    assert "Propagation activity" in html
    assert "Siblings derived" in html
    assert "Chain pages" in html
    # Public: must NOT leak the finding ID, hypothesis, or title
    assert "V7-secret-finding" not in html
    assert "Critical undisclosed bug" not in html
    assert "<code>42</code>" not in html


def test_section_full_shows_per_finding_table(tmp_path: Path) -> None:
    """Full mode: per-finding table with IDs and hypotheses (customer-private)."""
    from audit_pipeline.commands.report import _propagation_section
    _seed_propagation_artifacts(tmp_path, finding_id=42, hyp="V7-x")
    findings = [_make_finding(42, hyp="V7-x", title="bug detail")]
    html = _propagation_section(tmp_path, findings, public=False)
    # Per-finding row appears
    assert "V7-x" in html
    assert "bug detail" in html
    # Counter section also present
    assert "Siblings derived" in html


def test_section_handles_multiple_findings(tmp_path: Path) -> None:
    """Counter aggregation across multiple findings."""
    from audit_pipeline.commands.report import _propagation_section
    _seed_propagation_artifacts(tmp_path, finding_id=1, hyp="A")
    _seed_propagation_artifacts(tmp_path, finding_id=2, hyp="B")
    findings = [_make_finding(1, hyp="A"), _make_finding(2, hyp="B")]
    html = _propagation_section(tmp_path, findings, public=True)
    # 2 findings × 2 siblings each = 4 total
    assert ">4<" in html  # total siblings
    # Both findings have chain pages
    assert ">2<" in html  # n findings with propagation
