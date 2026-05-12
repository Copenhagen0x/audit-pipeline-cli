"""Tests for ``audit_pipeline.gates.symbol_grep`` (Gate 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from audit_pipeline.gates.symbol_grep import (
    check_symbols,
    extract_project_symbols,
)


# ---------- extract_project_symbols -------------------------------------

class TestExtractProjectSymbols:
    def test_finds_snake_case_function(self):
        src = "fn test_x() { compute_trade_pnl(1, 2); }"
        out = extract_project_symbols(src)
        assert "compute_trade_pnl" in out["snake_case"]

    def test_finds_camel_case_struct(self):
        src = "let cfg = MarketConfig { x: 1 };"
        out = extract_project_symbols(src)
        assert "MarketConfig" in out["camel_case"]

    def test_strips_comments(self):
        src = """
        // compute_trade_pnl is not called here
        let x = 1;
        /* MarketConfig only mentioned in block comment */
        fn foo() { real_function(); }
        """
        out = extract_project_symbols(src)
        assert "compute_trade_pnl" not in out["snake_case"]
        assert "MarketConfig" not in out["camel_case"]
        assert "real_function" in out["snake_case"]

    def test_strips_string_literals(self):
        src = 'panic!("ghost_function should not be flagged");'
        out = extract_project_symbols(src)
        assert "ghost_function" not in out["snake_case"]

    def test_whitelists_rust_stdlib(self):
        src = "let v = Vec::new(); v.checked_add(1); String::from(\"x\");"
        out = extract_project_symbols(src)
        assert out["snake_case"] == []
        assert "Vec" not in out["camel_case"]
        assert "String" not in out["camel_case"]

    def test_real_world_hallucinated_symbol(self):
        # mimics the F11 + F13 cases from the retracted cycle
        src = """
        let cfg = MarketConfig::default();
        let feed = cfg.oracle_leg_feed_id;
        engine.settle_after_close(idx);
        """
        out = extract_project_symbols(src)
        assert "oracle_leg_feed_id" in out["snake_case"]
        assert "settle_after_close" in out["snake_case"]
        # MarketConfig should also be flagged (it IS real, but the gate
        # will grep to confirm; the extractor's job is just to pull it out)
        assert "MarketConfig" in out["camel_case"]


# ---------- check_symbols against real .rs files ------------------------

@pytest.fixture
def fake_engine_repo(tmp_path: Path) -> Path:
    """Tiny fake engine src/ tree with a few real Rust symbols."""
    src = tmp_path / "engine" / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text(
        """
        pub fn compute_trade_pnl(size: i128, diff: i128) -> i128 { 0 }
        pub struct MarketConfig {
            pub index_feed_id: [u8; 32],
        }
        pub fn require_initialized(data: &[u8]) -> Result<(), ()> { Ok(()) }
        """
    )
    return src


class TestCheckSymbols:
    def test_all_symbols_present_passes(self, fake_engine_repo, tmp_path):
        poc = """
        fn test_compute_trade_pnl_overflow() {
            let cfg = MarketConfig { index_feed_id: [0; 32] };
            let p = compute_trade_pnl(1, 2);
            require_initialized(&[]).unwrap();
        }
        """
        r = check_symbols(poc_source=poc, search_dirs=[fake_engine_repo])
        assert r.passed is True
        assert r.details["missing"] == []

    def test_hallucinated_symbol_fails(self, fake_engine_repo):
        poc = """
        fn test_settle() {
            let cfg = MarketConfig::default();
            engine.settle_after_close(0);  // hallucinated
        }
        """
        r = check_symbols(poc_source=poc, search_dirs=[fake_engine_repo])
        assert r.passed is False
        assert "settle_after_close" in r.details["missing"]
        assert "Hallucinated" in r.reason

    def test_multiple_hallucinations_listed(self, fake_engine_repo):
        poc = """
        fn test_ghost() {
            ghost_a();
            ghost_b();
            ghost_c();
        }
        """
        r = check_symbols(poc_source=poc, search_dirs=[fake_engine_repo])
        assert r.passed is False
        assert set(r.details["missing"]) >= {"ghost_a", "ghost_b", "ghost_c"}

    def test_search_dirs_missing_skips(self, tmp_path):
        nonexistent = tmp_path / "does_not_exist"
        r = check_symbols(poc_source="fn x() { foo_bar(); }", search_dirs=[nonexistent])
        assert r.passed is None
        assert "no valid search dirs" in r.reason

    def test_whitelist_only_poc_passes(self, fake_engine_repo):
        poc = """
        fn test_whitelist_only() {
            let v: Vec<u32> = Vec::new();
            let s = String::from("hello");
            let r: Result<u32, ()> = Ok(1);
            let _ = r.unwrap_or(0);
        }
        """
        r = check_symbols(poc_source=poc, search_dirs=[fake_engine_repo])
        assert r.passed is True
        # All symbols whitelisted → checked count should be 0
        assert r.details["checked"] == 0

    def test_tolerance_param(self, fake_engine_repo):
        poc = "fn t() { ghost_a(); compute_trade_pnl(1, 2); }"
        # With strict (0 tolerance) → fail
        r_strict = check_symbols(poc_source=poc, search_dirs=[fake_engine_repo])
        assert r_strict.passed is False
        # With 1 tolerance → pass (one hallucination allowed)
        r_loose = check_symbols(
            poc_source=poc, search_dirs=[fake_engine_repo], max_hallucinations=1,
        )
        assert r_loose.passed is True

    def test_test_prefix_hallucination_no_longer_whitelisted(self, fake_engine_repo):
        """Phase B self-audit Defect 02: previously ANY symbol starting with
        ``test_`` was silently whitelisted. A hallucinated helper named
        ``test_settle_after_close`` slipped past the gate. Now it must fail
        unless the operator explicitly names that test as the PoC's own."""
        poc = """
        fn test_outer() {
            // hallucinated helper that doesn't exist anywhere
            test_settle_after_close(0);
        }
        """
        r = check_symbols(
            poc_source=poc, search_dirs=[fake_engine_repo],
            allowed_test_names=frozenset({"test_outer"}),  # only our wrapper
        )
        assert r.passed is False
        assert "test_settle_after_close" in r.details["missing"]

    def test_allowed_test_name_whitelisted(self, fake_engine_repo):
        """The PoC's own ``test_<finding_name>`` is correctly whitelisted."""
        poc = """
        fn test_h1_residual_conservation_fires() {
            compute_trade_pnl(1, 2);
        }
        """
        r = check_symbols(
            poc_source=poc, search_dirs=[fake_engine_repo],
            allowed_test_names=frozenset({"test_h1_residual_conservation_fires"}),
        )
        assert r.passed is True

    def test_searches_multiple_dirs(self, tmp_path):
        engine_src = tmp_path / "engine" / "src"
        engine_src.mkdir(parents=True)
        (engine_src / "lib.rs").write_text("pub fn engine_only() {}")
        wrapper_src = tmp_path / "wrapper" / "src"
        wrapper_src.mkdir(parents=True)
        (wrapper_src / "lib.rs").write_text("pub fn wrapper_only() {}")

        poc = "fn t() { engine_only(); wrapper_only(); }"
        r = check_symbols(
            poc_source=poc, search_dirs=[engine_src, wrapper_src],
        )
        assert r.passed is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
