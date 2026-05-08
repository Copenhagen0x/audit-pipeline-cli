"""Wave 8b — propagation chain visualization tests."""

from __future__ import annotations

from pathlib import Path

from audit_pipeline.commands.propagate_chain import _render_chain_html


def _fake_finding(fid: int = 123) -> dict:
    return {
        "id":            fid,
        "hypothesis_id": "V7-insurance-counter-vault-coupling",
        "title":         "Insurance counter decoupled from vault on residual settlement",
        "severity":      "Critical",
        "bug_class":     "insurance-counter-vault-divergence",
        "status":        "confirmed",
    }


def _fake_siblings() -> list[dict]:
    return [
        {
            "id":         "SIB-V7-1-liquidation-insurance-vault-skew",
            "severity":   "Critical",
            "class":      "state_transition",
            "bug_class":  "insurance-counter-vault-divergence-on-liquidation",
            "applies_to": ["perp_dex", "drift_protocol"],
            "claim":      "During a liquidation event, when the insurance fund absorbs a shortfall...",
        },
        {
            "id":         "SIB-V7-2-settle-pnl-insurance-vault-mismatch",
            "severity":   "High",
            "class":      "invariant_property",
            "bug_class":  "insurance-counter-vault-divergence-on-settle",
            "applies_to": ["perp_dex"],
            "claim":      "On settle-PnL, the insurance counter must match the vault delta.",
        },
    ]


def test_render_returns_self_contained_html(tmp_path: Path) -> None:
    """The output must be a complete HTML document (doctype + head + body)."""
    html = _render_chain_html(
        finding=_fake_finding(),
        siblings=_fake_siblings(),
        sibling_path=tmp_path / "derived" / "X-siblings.yaml",
        report_path=tmp_path / "recon" / "report.md",
        queue_items=[],
        fired=False,
        workspace=tmp_path,
    )
    assert "<!doctype html>" in html
    assert "<head>" in html and "</head>" in html
    assert "<body>" in html and "</body>" in html
    # Must include inlined CSS so the page is self-contained
    assert "<style>" in html


def test_render_includes_parent_finding(tmp_path: Path) -> None:
    html = _render_chain_html(
        finding=_fake_finding(),
        siblings=[], sibling_path=None, report_path=None,
        queue_items=[], fired=False, workspace=tmp_path,
    )
    assert "V7-insurance-counter-vault-coupling" in html
    assert "insurance-counter-vault-divergence" in html
    assert "Critical" in html


def test_render_lists_all_siblings(tmp_path: Path) -> None:
    siblings = _fake_siblings()
    html = _render_chain_html(
        finding=_fake_finding(),
        siblings=siblings,
        sibling_path=None, report_path=None, queue_items=[],
        fired=False, workspace=tmp_path,
    )
    for sib in siblings:
        assert sib["id"] in html
        assert sib["bug_class"] in html


def test_render_handles_empty_chain(tmp_path: Path) -> None:
    """A finding with no siblings / no propagation report shouldn't crash."""
    html = _render_chain_html(
        finding=_fake_finding(),
        siblings=[], sibling_path=None, report_path=None,
        queue_items=[], fired=False, workspace=tmp_path,
    )
    assert "No siblings derived" in html
    assert "No propagation report" in html
    assert "No Layer-1 dispatch items queued" in html


def test_render_shows_fired_badge(tmp_path: Path) -> None:
    html = _render_chain_html(
        finding=_fake_finding(),
        siblings=[], sibling_path=None, report_path=None,
        queue_items=[], fired=True, workspace=tmp_path,
    )
    assert "FIRED" in html


def test_render_renders_queue_items(tmp_path: Path) -> None:
    queue = [
        {
            "candidate_repo": "drift-protocol-v2",
            "candidate_file": "programs/drift/src/state/perp_market.rs",
            "candidate_line": 412,
            "candidate_score": 4,
            "status": "pending",
            "suggested_hunt": {"bug_class_filter": "insurance-counter-vault-divergence"},
        }
    ]
    html = _render_chain_html(
        finding=_fake_finding(),
        siblings=[], sibling_path=None, report_path=None,
        queue_items=queue, fired=False, workspace=tmp_path,
    )
    assert "drift-protocol-v2" in html
    assert "412" in html
    assert "pending" in html


def test_render_escapes_html_entities(tmp_path: Path) -> None:
    """Untrusted finding content (titles, claims) must be HTML-escaped."""
    f = _fake_finding()
    f["title"] = "<script>alert('xss')</script>"
    f["bug_class"] = "ev<il>"
    html = _render_chain_html(
        finding=f, siblings=[], sibling_path=None, report_path=None,
        queue_items=[], fired=False, workspace=tmp_path,
    )
    assert "<script>alert" not in html  # raw script tag must NOT survive
    assert "&lt;script&gt;" in html or "alert(&#x27;xss&#x27;)" in html


def test_chain_cli_registered() -> None:
    """The chain CLI must be reachable from the propagate command group."""
    from audit_pipeline.commands.propagate import propagate_cmd
    sub_names = list(propagate_cmd.commands.keys())
    assert "chain" in sub_names, f"chain subcommand missing; have: {sub_names}"
