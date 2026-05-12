"""Tests for ``audit_pipeline.gates.cargo_check`` (Gate 3).

All ``cargo`` invocations are mocked — these tests must NOT shell out to
a real Rust toolchain. The integration smoke test against a real crate
lives elsewhere.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from audit_pipeline.gates.cargo_check import check_compiles


def _mock_cargo(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


@pytest.fixture
def fake_crate(tmp_path: Path) -> Path:
    """Tiny fake crate with Cargo.toml and tests/ dir."""
    (tmp_path / "Cargo.toml").write_text("[package]\nname=\"fake\"\nversion=\"0.0.0\"\n")
    (tmp_path / "tests").mkdir()
    return tmp_path


class TestCheckCompiles:
    def test_no_cargo_returns_skip(self, fake_crate):
        with patch("audit_pipeline.gates.cargo_check._have_cargo", return_value=False):
            r = check_compiles(
                poc_source="fn x() {}", repo_dir=fake_crate, test_name="t",
            )
        assert r.passed is None
        assert "cargo" in r.reason.lower()

    def test_no_cargo_toml_returns_skip(self, tmp_path):
        with patch("audit_pipeline.gates.cargo_check._have_cargo", return_value=True):
            r = check_compiles(
                poc_source="fn x() {}", repo_dir=tmp_path, test_name="t",
            )
        assert r.passed is None
        assert "Cargo.toml" in r.reason or "Rust crate" in r.reason

    def test_clean_compile_passes(self, fake_crate):
        with patch("audit_pipeline.gates.cargo_check._have_cargo", return_value=True), \
             patch("subprocess.run", return_value=_mock_cargo(0, stdout="ok")):
            r = check_compiles(
                poc_source="fn test_x() {}", repo_dir=fake_crate, test_name="t_alpha",
            )
        assert r.passed is True
        assert "passed" in r.reason

    def test_staging_collision_returns_skip(self, fake_crate):
        # Pre-create a file with the same name
        (fake_crate / "tests" / "t_collide.rs").write_text("// existing")
        with patch("audit_pipeline.gates.cargo_check._have_cargo", return_value=True):
            r = check_compiles(
                poc_source="fn x() {}", repo_dir=fake_crate, test_name="t_collide",
            )
        assert r.passed is None
        assert "overwrite" in r.reason

    def test_compile_error_referencing_poc_fails(self, fake_crate):
        stderr = (
            "error[E0425]: cannot find function `settle_after_close` in this scope\n"
            "  --> tests/t_hallucinate.rs:5:13\n"
            "   |\n"
            "5  |     engine.settle_after_close(0);\n"
        )
        with patch("audit_pipeline.gates.cargo_check._have_cargo", return_value=True), \
             patch("subprocess.run", return_value=_mock_cargo(101, stderr=stderr)):
            r = check_compiles(
                poc_source="fn test_h() { engine.settle_after_close(0); }",
                repo_dir=fake_crate, test_name="t_hallucinate",
            )
        assert r.passed is False
        assert "does not compile" in r.reason
        assert "settle_after_close" in r.details["ref_excerpt"]

    def test_compile_error_NOT_referencing_poc_skips(self, fake_crate):
        # Pre-existing repo breakage — failures in src/lib.rs, not our test
        stderr = (
            "error[E0277]: type mismatch\n"
            "  --> src/lib.rs:42:1\n"
        )
        with patch("audit_pipeline.gates.cargo_check._have_cargo", return_value=True), \
             patch("subprocess.run", return_value=_mock_cargo(101, stderr=stderr)):
            r = check_compiles(
                poc_source="fn test_x() {}",
                repo_dir=fake_crate, test_name="t_innocent",
            )
        assert r.passed is None    # skip-not-fail
        assert "pre-existing" in r.reason

    def test_timeout_fails(self, fake_crate):
        import subprocess as _sp
        with patch("audit_pipeline.gates.cargo_check._have_cargo", return_value=True), \
             patch("subprocess.run", side_effect=_sp.TimeoutExpired(cmd="cargo", timeout=180)):
            r = check_compiles(
                poc_source="fn test_x() {}", repo_dir=fake_crate, test_name="t",
            )
        assert r.passed is False
        assert "timed out" in r.reason

    def test_staged_file_cleaned_up_on_success(self, fake_crate):
        staged = fake_crate / "tests" / "t_cleanup.rs"
        assert not staged.exists()
        with patch("audit_pipeline.gates.cargo_check._have_cargo", return_value=True), \
             patch("subprocess.run", return_value=_mock_cargo(0)):
            check_compiles(
                poc_source="fn test_x() {}",
                repo_dir=fake_crate, test_name="t_cleanup",
            )
        assert not staged.exists(), "staged file should be removed after successful check"

    def test_staged_file_cleaned_up_on_failure(self, fake_crate):
        staged = fake_crate / "tests" / "t_cleanup_fail.rs"
        with patch("audit_pipeline.gates.cargo_check._have_cargo", return_value=True), \
             patch("subprocess.run", return_value=_mock_cargo(101, stderr="tests/t_cleanup_fail.rs: error")):
            check_compiles(
                poc_source="fn test_x() {}",
                repo_dir=fake_crate, test_name="t_cleanup_fail",
            )
        assert not staged.exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
