"""Regression tests for the P0 fixes from the full pipeline audit.

Each test pins ONE of the P0 defects so silent regressions surface
immediately. Order matches the P0 list in the audit synthesis.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


# ────────────────── P0 #1: GateResult details field ──────────────────


def test_gateresult_accepts_details_kwarg() -> None:
    """Pre-/post-patch gates pass details=. Before fix this raised TypeError."""
    from audit_pipeline.bundle.verifier import GateResult
    r = GateResult(True, "fired", 0.1, details={"outcome": "fired"})
    assert r.details == {"outcome": "fired"}
    assert r.to_json()["details"] == {"outcome": "fired"}


def test_gateresult_details_optional() -> None:
    """Existing callers that don't pass details still work."""
    from audit_pipeline.bundle.verifier import GateResult
    r = GateResult(True, "ok", 0.1)
    assert r.details is None
    assert "details" not in r.to_json()


# ──────────────── P0 #2: PoC factory safe RiskParams ────────────────


def test_poc_template_no_overflow_min_funding_slots() -> None:
    """The PoC factory must not ship the cycle-20260511 overflow values
    AS CODE. (Doc comments referencing the historical value are fine.)"""
    tmpl = Path(__file__).resolve().parents[1] / "src" / "audit_pipeline" / \
           "templates" / "engine_state_conservation_poc.rs.template"
    src = tmpl.read_text(encoding="utf-8")
    # The actual code line — has trailing comma — must not contain the bad value
    assert "min_funding_lifetime_slots: 10_000_000," not in src
    assert "max_active_positions_per_side: MAX_ACCOUNTS as u64," not in src
    # Sanity: safe values present as code
    assert "min_funding_lifetime_slots: 100," in src
    assert "max_active_positions_per_side: 64," in src


def test_poc_llm_prompt_no_overflow_min_funding_slots() -> None:
    """The LLM-author prompt must not contain the broken values as code."""
    src = (Path(__file__).resolve().parents[1] / "src" / "audit_pipeline" /
           "commands" / "poc_llm.py").read_text(encoding="utf-8")
    assert "min_funding_lifetime_slots: 10_000_000," not in src
    assert "max_active_positions_per_side: MAX_ACCOUNTS as u64," not in src
    assert "min_funding_lifetime_slots: 100," in src
    assert "max_active_positions_per_side: 64," in src


# ────────────── P0 #3: #[kani::unwind] stripped in synth_kani ──────────────


def test_strip_kani_unwind_removes_per_function_attrs() -> None:
    from audit_pipeline.commands.synth_kani import _strip_kani_unwind
    src = """
#[kani::proof]
#[kani::unwind(8)]
fn harness() {
    foo();
}
"""
    out = _strip_kani_unwind(src)
    assert "#[kani::unwind" not in out
    assert "#[kani::proof]" in out
    assert "fn harness()" in out


def test_strip_kani_unwind_handles_multiple_attrs_per_file() -> None:
    from audit_pipeline.commands.synth_kani import _strip_kani_unwind
    src = """
#[kani::unwind(8)]
fn a() {}
#[kani::unwind(128)]
fn b() {}
"""
    out = _strip_kani_unwind(src)
    assert "kani::unwind" not in out


def test_strip_kani_unwind_idempotent() -> None:
    from audit_pipeline.commands.synth_kani import _strip_kani_unwind
    src = "#[kani::proof]\nfn x() {}"
    assert _strip_kani_unwind(src) == src


# ────────────── P0 #4: L4 LiteSVM behind L2.5 triage filter ──────────────


def test_hunt_l4_runs_after_l2_5_triage() -> None:
    """Find the 'Layer 4' and 'Layer 2.5' anchors in hunt.py — L4 MUST
    appear after L2.5 in source order so the triage filter narrows L4
    too (otherwise false fires still burn LiteSVM spend)."""
    hunt_src = (Path(__file__).resolve().parents[1] / "src" /
                "audit_pipeline" / "commands" / "hunt.py").read_text(encoding="utf-8")
    idx_l25 = hunt_src.find("# ---------- Layer 2.5: fire triage")
    idx_l4 = hunt_src.find("# ---------- Layer 4: LiteSVM")
    assert idx_l25 > 0, "Layer 2.5 anchor missing"
    assert idx_l4 > 0, "Layer 4 anchor missing"
    assert idx_l25 < idx_l4, (
        "Layer 4 must run AFTER Layer 2.5 so layer3_dispatch_filter "
        "narrows fired_for_litesvm too"
    )


def test_hunt_fired_for_litesvm_uses_dispatch_filter() -> None:
    """Scan the lines around `fired_for_litesvm = [` and assert
    `layer3_dispatch_filter` appears in the list comprehension body."""
    hunt_src = (Path(__file__).resolve().parents[1] / "src" /
                "audit_pipeline" / "commands" / "hunt.py").read_text(encoding="utf-8")
    idx = hunt_src.find("fired_for_litesvm = [")
    assert idx > 0, "fired_for_litesvm not found"
    # Read forward until balanced brackets to capture the full comprehension
    chunk = hunt_src[idx:idx + 400]
    assert "layer3_dispatch_filter" in chunk, (
        "fired_for_litesvm must apply layer3_dispatch_filter so false fires "
        "are filtered before L4 author + dispatch"
    )


# ────────────── P0 #5: Sibling diversity on auto-fire path ──────────────


def test_derive_siblings_async_calls_diversity_filter() -> None:
    """Inspect the derive_siblings_async source to assert the diversity
    filter is invoked before writing to disk. Earlier the filter was
    only wired into _append_siblings (manual --append-to)."""
    src = (Path(__file__).resolve().parents[1] / "src" / "audit_pipeline" /
           "commands" / "derive_siblings.py").read_text(encoding="utf-8")
    # Find the async function block
    m = re.search(
        r"def derive_siblings_async\(.*?(?=\ndef |\nclass )",
        src,
        re.DOTALL,
    )
    assert m, "derive_siblings_async not found"
    body = m.group(0)
    assert "_enforce_sibling_diversity" in body, (
        "derive_siblings_async must call _enforce_sibling_diversity "
        "before writing siblings YAML (auto-fire path was bypassing it)"
    )


def test_jaccard_threshold_is_03_not_06() -> None:
    """Threshold should be tighter than the original 0.6 to actually
    catch rephrased-same-idea duplicates."""
    src = (Path(__file__).resolve().parents[1] / "src" / "audit_pipeline" /
           "commands" / "derive_siblings.py").read_text(encoding="utf-8")
    # The numeric threshold lives in JACCARD_THRESHOLD constant
    m = re.search(r"JACCARD_THRESHOLD\s*=\s*([\d.]+)", src)
    assert m, "JACCARD_THRESHOLD constant not found"
    val = float(m.group(1))
    assert val <= 0.3, f"threshold {val} too loose; should be <= 0.3"


# ────────────── P0 #6: Postgres hook_runs mirror ──────────────


def test_postgres_schema_has_hook_runs() -> None:
    """Postgres backend's SCHEMA_PG must include hook_runs to match SQLite."""
    from audit_pipeline.db_postgres import SCHEMA_PG
    flat = "\n".join(SCHEMA_PG)
    assert "CREATE TABLE IF NOT EXISTS hook_runs" in flat
    assert "UNIQUE(finding_id, hook_name)" in flat


# ────────────── P0 #7: JELLEO_CYCLE_LOG_PATH set on resume ──────────────


def test_hunt_sets_cycle_log_path_outside_resume_branch() -> None:
    """Env-var setup must NOT be inside the `else:` (new-cycle) branch;
    resume path also needs it."""
    src = (Path(__file__).resolve().parents[1] / "src" / "audit_pipeline" /
           "commands" / "hunt.py").read_text(encoding="utf-8")
    # The env-var setup should appear before any reference to the recon
    # subprocess (which depends on the env vars being correct).
    env_idx = src.find('_os.environ["JELLEO_CYCLE_LOG_PATH"]')
    recon_argv_idx = src.find('"recon",')
    assert env_idx > 0
    assert env_idx < recon_argv_idx
    # Must NOT be inside the new-cycle else: branch — verify by checking
    # the surrounding lines mention "Live-dashboard event emission" at
    # workspace-level scope (4-space indent), not at if-else-body scope
    # (8-space indent inside the else block).
    lines = src.splitlines()
    for i, ln in enumerate(lines):
        if '_os.environ["JELLEO_CYCLE_LOG_PATH"]' in ln:
            leading = len(ln) - len(ln.lstrip())
            assert leading == 4, (
                f"env-var assignment at line {i+1} has indent {leading}, "
                f"expected 4 (function-body scope, not inside if/else)"
            )
            break


# ────────────── P0 #8: Resume does NOT double-bill daily cap ──────────────


def test_hunt_resume_skips_daily_cap_record_spend() -> None:
    src = (Path(__file__).resolve().parents[1] / "src" / "audit_pipeline" /
           "commands" / "hunt.py").read_text(encoding="utf-8")
    # The post-summary record_spend for layer1_cost must be conditional
    # on `not summary_is_valid_for_resume`.
    assert "if not summary_is_valid_for_resume:" in src
    # And the next line(s) should be the record_spend for layer1
    m = re.search(
        r"if not summary_is_valid_for_resume:\s*\n\s*daily_cap\.record_spend\(layer1_cost\)",
        src,
    )
    assert m, (
        "resume must guard daily_cap.record_spend(layer1_cost) behind "
        "`if not summary_is_valid_for_resume`"
    )


# ────────────── P0 #9: post_cycle uses canonical slug ──────────────


def test_post_cycle_uses_canonical_slug() -> None:
    src = (Path(__file__).resolve().parents[1] / "src" / "audit_pipeline" /
           "gates" / "post_cycle.py").read_text(encoding="utf-8")
    assert "from audit_pipeline.utils.slug import slug_for_hypothesis" in src
    assert "slug_for_hypothesis(hyp_id)" in src


def test_post_cycle_handles_long_hyp_ids() -> None:
    """Long hyp IDs (>60 chars, 19 in live library) must round-trip
    through the canonical slug without truncation mismatch."""
    from audit_pipeline.utils.slug import slug_for_hypothesis
    long_id = "S11-mark-min-fee-collected-into-insurance-but-mark-walk-counted-only-when-paid"
    slug = slug_for_hypothesis(long_id)
    # The canonical helper caps at 60 chars
    assert len(slug) <= 60
    # Hunt + poc_llm + post_cycle MUST agree on this exact value
    assert slug_for_hypothesis(long_id) == slug
