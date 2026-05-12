"""Tests for ``audit_pipeline.gates.post_cycle`` — the post-cycle QA
gate that re-runs cheap pre-disclosure checks before auto-publish.

Replays the failure modes that auto-published yesterday's retracted
cycle without operator review.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from audit_pipeline.gates.post_cycle import (
    PUBLISH_BLOCKED_SENTINEL,
    check_post_cycle,
    write_block_sentinel,
)


@pytest.fixture
def cycle_with_engine(tmp_path: Path) -> tuple[Path, Path]:
    """Construct a fake cycle dir with a poc/ subdir plus a tiny engine src."""
    cycle = tmp_path / "hunts" / "20260512-cycle1"
    poc = cycle / "poc"
    poc.mkdir(parents=True)
    engine_src = tmp_path / "engine" / "src"
    engine_src.mkdir(parents=True)
    (engine_src / "lib.rs").write_text(
        "pub fn compute_trade_pnl(a: i128, b: i128) -> i128 { 0 }\n"
        "pub fn require_initialized(d: &[u8]) {}\n"
    )
    return cycle, engine_src


class TestCheckPostCycle:
    def test_clean_poc_passes(self, cycle_with_engine):
        cycle, engine_src = cycle_with_engine
        (cycle / "poc" / "test_h1.rs").write_text(
            "#[test]\nfn test_h1() { compute_trade_pnl(1, 2); }\n"
        )
        findings = [{"id": 1, "hypothesis_id": "H1", "poc_fired": True}]
        report = check_post_cycle(
            cycle_dir=cycle,
            confirmed_findings=findings,
            engine_src_dir=engine_src,
        )
        assert report.passed is True
        assert report.n_failed == 0

    def test_hallucinated_symbol_blocks(self, cycle_with_engine):
        """Yesterday's F11/F13 scenario: PoC cites a function that doesn't
        exist anywhere in engine source. Post-cycle QA must catch it
        before auto-publish, EVEN IF the cycle-start symbol_grep gate
        was bypassed for some reason."""
        cycle, engine_src = cycle_with_engine
        (cycle / "poc" / "test_s3.rs").write_text(
            "#[test]\nfn test_s3() {\n"
            "    engine.settle_after_close(0);  // hallucinated\n"
            "}\n"
        )
        findings = [{"id": 13, "hypothesis_id": "S3", "poc_fired": True}]
        report = check_post_cycle(
            cycle_dir=cycle,
            confirmed_findings=findings,
            engine_src_dir=engine_src,
        )
        assert report.passed is False
        assert report.n_failed == 1
        assert "settle_after_close" in report.rows[0]["reason"]

    def test_ignored_poc_blocks(self, cycle_with_engine):
        """A `#[ignore]`d PoC cannot have legitimately fired."""
        cycle, engine_src = cycle_with_engine
        (cycle / "poc" / "test_x.rs").write_text(
            "#[test]\n#[ignore]\nfn test_x() { compute_trade_pnl(1,2); }\n"
        )
        findings = [{"id": 99, "hypothesis_id": "X", "poc_fired": True}]
        report = check_post_cycle(
            cycle_dir=cycle,
            confirmed_findings=findings,
            engine_src_dir=engine_src,
        )
        assert report.passed is False
        assert "#[ignore]" in report.rows[0]["reason"]

    def test_unimplemented_poc_blocks(self, cycle_with_engine):
        cycle, engine_src = cycle_with_engine
        (cycle / "poc" / "test_y.rs").write_text(
            "#[test]\nfn test_y() {\n    unimplemented!()\n}\n"
        )
        findings = [{"id": 100, "hypothesis_id": "Y", "poc_fired": True}]
        report = check_post_cycle(
            cycle_dir=cycle,
            confirmed_findings=findings,
            engine_src_dir=engine_src,
        )
        assert report.passed is False
        assert "unimplemented" in report.rows[0]["reason"]

    def test_missing_poc_file_blocks(self, cycle_with_engine):
        cycle, engine_src = cycle_with_engine
        findings = [{"id": 1, "hypothesis_id": "GHOST", "poc_fired": True}]
        report = check_post_cycle(
            cycle_dir=cycle,
            confirmed_findings=findings,
            engine_src_dir=engine_src,
        )
        assert report.passed is False
        assert "missing on disk" in report.rows[0]["reason"]

    def test_mixed_some_pass_some_fail(self, cycle_with_engine):
        cycle, engine_src = cycle_with_engine
        (cycle / "poc" / "test_good.rs").write_text(
            "#[test]\nfn test_good() { compute_trade_pnl(1,2); }\n"
        )
        (cycle / "poc" / "test_bad.rs").write_text(
            "#[test]\nfn test_bad() { settle_after_close(0); }\n"
        )
        findings = [
            {"id": 1, "hypothesis_id": "good", "poc_fired": True},
            {"id": 2, "hypothesis_id": "bad", "poc_fired": True},
        ]
        report = check_post_cycle(
            cycle_dir=cycle,
            confirmed_findings=findings,
            engine_src_dir=engine_src,
        )
        # Aggregate fails on ANY single failure
        assert report.passed is False
        assert report.n_failed == 1
        assert report.n_findings == 2

    def test_no_findings_trivially_passes(self, cycle_with_engine):
        cycle, engine_src = cycle_with_engine
        report = check_post_cycle(
            cycle_dir=cycle,
            confirmed_findings=[],
            engine_src_dir=engine_src,
        )
        assert report.passed is True
        assert report.n_findings == 0


class TestWriteBlockSentinel:
    def test_sentinel_path_and_content(self, tmp_path):
        cycle = tmp_path / "c1"
        cycle.mkdir()
        from audit_pipeline.gates.post_cycle import PostCycleReport
        report = PostCycleReport(
            passed=False, n_findings=20, n_failed=20,
            rows=[{"id": 1, "passed": False, "reason": "fake"}],
        )
        sentinel = write_block_sentinel(cycle, report)
        assert sentinel == cycle / PUBLISH_BLOCKED_SENTINEL
        assert sentinel.is_file()
        loaded = json.loads(sentinel.read_text())
        assert loaded["passed"] is False
        assert loaded["n_failed"] == 20


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
