"""Patch #10 — L4 false-pass + subprocess.TimeoutExpired guard tests.

Closes audit HIGH findings:
  * c94ef463 — anchor_builder.py uncaught TimeoutExpired
  * ae465999 — anchor_kani_runner.py uncaught TimeoutExpired in run_kani_proof
  * 9e72ad1c — case-sensitive _KANI_SUCCESS_RE / _KANI_FAILED_RE missed
    legacy `Verification:- SUCCESSFUL` mixed-case output

Plus R1 expansions caught by reviewer audit:
  * IGNORECASE consistency across parse_kani_outcome, hunt.py compile-
    iterate loop, kani_log._parse_one_block, report.py L3 status
  * b"" fallback removed (text=True guarantees str|None)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ─────────────── anchor_builder.py TimeoutExpired ───────────────


def _make_target_repo(tmp_path: Path, program_name: str = "fake_program") -> Path:
    """Build a minimal `target_repo/programs/<name>/` skeleton that
    build_anchor_program will copy into the cycle's build dir."""
    target = tmp_path / "target_repo"
    target.mkdir()
    progs = target / "programs"
    progs.mkdir()
    prog = progs / program_name
    prog.mkdir()
    (prog / "Cargo.toml").write_text(
        f'[package]\nname = "{program_name}"\nversion = "0.1.0"\n'
        '[lib]\ncrate-type = ["cdylib", "lib"]\n'
    )
    (prog / "src").mkdir()
    (prog / "src" / "lib.rs").write_text("// stub\n")
    return target


def test_build_anchor_program_handles_timeout(tmp_path) -> None:
    """P10 R0 (HIGH c94ef463): subprocess.TimeoutExpired must be caught
    + return a structured AnchorBuildResult with returncode=124, not
    propagate uncaught and kill the cycle.

    Reviewer R0 (both): no test for the timeout path exists. R1 adds
    this one — mock subprocess.run to raise TimeoutExpired and assert
    the result shape."""
    from audit_pipeline.anchor_builder import build_anchor_program

    target_repo = _make_target_repo(tmp_path)
    cycle_dir = tmp_path / "cycle"
    cycle_dir.mkdir()

    timeout_exc = subprocess.TimeoutExpired(
        cmd=["cargo", "build-sbf"],
        timeout=900,
        output="partial stdout",  # text=True → str
        stderr="partial stderr",
    )

    with patch("subprocess.run", side_effect=timeout_exc):
        result = build_anchor_program(
            cycle_dir=cycle_dir,
            target_repo=target_repo,
            program_name="fake_program",
            timeout_s=900,
        )

    assert result.returncode == 124, (
        f"timeout should return 124 (timeout convention), got {result.returncode}"
    )
    assert result.so_path is None
    assert result.error is not None
    assert "timed out" in result.error
    # Partial log must be persisted for debugging
    assert result.build_log_path.is_file()
    log_text = result.build_log_path.read_text(encoding="utf-8")
    assert "partial stdout" in log_text
    assert "partial stderr" in log_text
    assert "TIMEOUT after 900s" in log_text


def test_build_anchor_program_handles_timeout_with_none_output(tmp_path) -> None:
    """Edge case: TimeoutExpired with stdout=None/stderr=None (no
    output captured before timeout). Must not crash."""
    from audit_pipeline.anchor_builder import build_anchor_program

    target_repo = _make_target_repo(tmp_path)
    cycle_dir = tmp_path / "cycle"
    cycle_dir.mkdir()

    timeout_exc = subprocess.TimeoutExpired(
        cmd=["cargo", "build-sbf"], timeout=60, output=None, stderr=None,
    )

    with patch("subprocess.run", side_effect=timeout_exc):
        result = build_anchor_program(
            cycle_dir=cycle_dir,
            target_repo=target_repo,
            program_name="fake_program",
            timeout_s=60,
        )
    assert result.returncode == 124
    assert result.so_path is None


# ─────────────── anchor_kani_runner.py TimeoutExpired ───────────────


def test_run_kani_proof_handles_timeout(tmp_path) -> None:
    """P10 R0 (HIGH ae465999): run_kani_proof must catch TimeoutExpired
    and return (124, combined_log_with_timeout_note)."""
    from audit_pipeline.anchor_kani_runner import run_kani_proof

    sidecar = tmp_path / "sidecar"
    sidecar.mkdir()
    (sidecar / "Cargo.toml").write_text(
        '[package]\nname = "k"\nversion = "0.1.0"\n'
    )

    timeout_exc = subprocess.TimeoutExpired(
        cmd=["cargo", "kani"],
        timeout=1800,
        output="kani partial output",
        stderr="kani partial err",
    )

    with patch("subprocess.run", side_effect=timeout_exc):
        rc, log = run_kani_proof(
            sidecar_dir=sidecar,
            harness_name="proof_anchor_X",
            timeout_s=1800,
        )

    assert rc == 124
    assert "kani partial output" in log
    assert "kani partial err" in log
    assert "TIMEOUT after 1800s" in log


# ─────────────── parse_kani_outcome case sensitivity ───────────────


def test_parse_kani_outcome_legacy_mixed_case_success() -> None:
    """P10 R0 (HIGH 9e72ad1c): legacy cargo-kani emits
    `Verification:- SUCCESSFUL` (mixed case). The pre-patch case-
    sensitive regex misclassified this as INCONCLUSIVE. With
    re.IGNORECASE the success regex matches."""
    from audit_pipeline.anchor_kani_runner import parse_kani_outcome

    log = (
        "Compiling foo v0.1.0\n"
        "Running 1 check\n"
        "Verification:- SUCCESSFUL\n"  # MIXED CASE — the legacy form
        "Verification Time: 1.23s\n"
    )
    proved, cex, reason = parse_kani_outcome(log)
    assert proved is True
    assert cex is False


def test_parse_kani_outcome_legacy_mixed_case_failed() -> None:
    """Same — legacy mixed-case `Verification:- FAILED` must classify."""
    from audit_pipeline.anchor_kani_runner import parse_kani_outcome

    log = (
        "Compiling foo v0.1.0\n"
        "Verification:- FAILED\n"
        "Failed Checks: assertion may fail at line 42\n"
    )
    proved, cex, reason = parse_kani_outcome(log)
    assert proved is False
    assert cex is True
    assert "line 42" in reason


def test_parse_kani_outcome_modern_uppercase_success() -> None:
    """Sanity: modern uppercase format still matches (the existing case
    that the original P10 fix was meant to preserve)."""
    from audit_pipeline.anchor_kani_runner import parse_kani_outcome

    log = "VERIFICATION:- SUCCESSFUL\n"
    proved, cex, _ = parse_kani_outcome(log)
    assert proved is True
    assert cex is False


def test_parse_kani_outcome_timeout_marker() -> None:
    """The timeout-decorated log must classify as INCONCLUSIVE (not
    success, not failure). The `--- TIMEOUT after Xs ---` string does
    NOT contain `VERIFICATION:` so the regexes don't match it."""
    from audit_pipeline.anchor_kani_runner import parse_kani_outcome

    log = (
        "Compiling foo v0.1.0\n"
        "--- STDERR ---\n"
        "--- TIMEOUT after 1800s ---\n"
    )
    proved, cex, reason = parse_kani_outcome(log)
    assert proved is False
    assert cex is False
    assert "no terminal verdict" in reason


def test_parse_kani_outcome_rejects_verification_rust_path_false_match() -> None:
    """P10 R2 (goober MEDIUM): the IGNORECASE pattern must NOT match a
    Rust module path like `verification::check` in rustc error output —
    otherwise the rustc-error early-return guard skips and the loop
    continues past a compile failure. Lock in the `:-` requirement that
    excludes the `::` Rust path form."""
    from audit_pipeline.anchor_kani_runner import parse_kani_outcome

    log = (
        "warning: unused variable\n"
        "error[E0432]: unresolved import verification::check\n"
        "error: could not compile `foo` due to 1 previous error\n"
    )
    proved, cex, reason = parse_kani_outcome(log)
    assert proved is False
    assert cex is False
    # Should classify as compile-fail, NOT as "rustc error in harness
    # before verification" (which would mean the verdict guard
    # false-matched the `verification::` text and skipped the rustc-
    # error guard).
    assert "failed to compile" in reason


def test_parse_kani_outcome_dashless_verdict_falls_through_as_inconclusive() -> None:
    """P10 R2 (goober MEDIUM): we deliberately require the `:-` literal
    in `_KANI_VERDICT_LINE_RE` and the two SUCCESS/FAILED regexes,
    which means a hypothetical dashless `VERIFICATION SUCCESSFUL`
    line (CBMC-style, not observed in cargo-kani) falls through to
    INCONCLUSIVE. Lock in that classification so a future cargo-kani
    version that switches to dashless output surfaces as INCONCLUSIVE
    (visible signal for the operator to widen the regex), not silently
    as PASS or FAIL."""
    from audit_pipeline.anchor_kani_runner import parse_kani_outcome

    log = "VERIFICATION SUCCESSFUL\n"  # dashless — hypothetical
    proved, cex, reason = parse_kani_outcome(log)
    assert proved is False
    assert cex is False
    assert "no terminal verdict" in reason


def test_parse_kani_outcome_rustc_error_with_legacy_verdict() -> None:
    """P10 R1 (code-reviewer LOW + goober HIGH): the rustc-error early
    return guard `"VERIFICATION:" not in log` was case-sensitive. If a
    log had rustc warnings AND a legacy `Verification:- SUCCESSFUL`
    verdict, the guard would short-circuit and misclassify the proof.
    R1 uses an IGNORECASE regex search for the guard."""
    from audit_pipeline.anchor_kani_runner import parse_kani_outcome

    log = (
        "warning: unused variable\n"
        "error[E0277]: trait bound not satisfied\n"  # rustc-style error
        "Verification:- SUCCESSFUL\n"  # legacy mixed case
    )
    proved, cex, reason = parse_kani_outcome(log)
    # The successful verdict must win — the guard must NOT misclassify
    # this as a rustc compile error.
    assert proved is True, (
        f"legacy mixed-case verdict with rustc warnings should classify "
        f"as proved; got proved={proved} reason={reason!r}"
    )


# ─────────────── kani_log.py case sensitivity ───────────────


def test_kani_log_parse_one_block_legacy_mixed_case() -> None:
    """P10 R1 (goober HIGH): kani_log._parse_one_block had a hard-coded
    case-sensitive `"VERIFICATION:- SUCCESSFUL"` substring check that
    missed the legacy `Verification:- SUCCESSFUL` form — disclosure
    report showed "0 harnesses proven" for old/mixed-version cycles."""
    from audit_pipeline.utils.kani_log import parse_kani_log

    # `parse_kani_log` splits on `^Checking harness ` boundaries.
    log = (
        "Some preamble\n"
        "Checking harness harness_check_X...\n"
        "Verification:- SUCCESSFUL\n"  # MIXED case
        "Verification Time: 0.5s\n\n"
        "Checking harness harness_check_Y...\n"
        "Verification:- FAILED\n"
        "Verification Time: 1.0s\n\n"
    )
    results = parse_kani_log(log)
    verdicts = {r.name: r.verdict for r in results}
    # The PASS verdict for harness_X must be detected via the IGNORECASE
    # normalisation R1 added.
    assert verdicts.get("harness_check_X") == "PASS", (
        f"legacy mixed-case Verification:- SUCCESSFUL should classify "
        f"as PASS; got {verdicts}"
    )
    assert verdicts.get("harness_check_Y") == "FAIL"
