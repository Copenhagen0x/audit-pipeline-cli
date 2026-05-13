"""Gate 3 — L2.cargo_check.

Verifies that a PoC test actually compiles against the real engine / wrapper
source. Built in response to cycle 20260511-183154 where eight PoCs cited
behavior contradicted by the real code (i128::MIN guarded, init checks
present, postcondition asserts running, etc.) — they "fired" anyway because
the pipeline either ran them against a stubbed harness, mocked types, or a
stale snapshot, never against the actual repo as a real test.

Companion to ``symbol_grep`` (Gate 2):

  * ``symbol_grep`` is fast static heuristic — catches obvious hallucinations
  * ``cargo_check`` is the ground-truth check — invokes ``cargo check --tests``
    on the engine repo with the PoC file staged into ``tests/``

The gate stages the PoC into ``<repo>/tests/<test_name>.rs``, runs
``cargo check --tests --message-format=short``, captures the result, then
unconditionally removes the staged file (or restores any prior version).
A non-zero ``cargo check`` exit with errors mentioning the staged path is
FAIL; clean compile is PASS; missing ``cargo`` binary is SKIP.

Used by: ``commands/poc_llm.py`` (after Gate 2 passes, before save).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

from audit_pipeline.gates import GateResult

# Phase B self-audit Defect 06: validate test_name so a malicious value
# like ``../poc_evil`` can't cause a write/unlink outside ``tests/``.
_TEST_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")


def _have_cargo() -> bool:
    return shutil.which("cargo") is not None


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def check_compiles(
    *,
    poc_source: str,
    repo_dir: Path,
    test_name: str,
    timeout: int = 180,
    allow_broken_tree: bool = False,
) -> GateResult:
    """Stage ``poc_source`` into ``repo_dir/tests/<test_name>.rs`` and run
    ``cargo check --tests``. Restore prior state on exit, regardless of result.

    Args:
        poc_source:  Rust source of the PoC test file (full file contents)
        repo_dir:    path to the engine OR wrapper repo (must contain
                     ``Cargo.toml`` and a ``tests/`` directory or allow one
                     to be created)
        test_name:   filename stem (we'll write ``tests/<test_name>.rs``).
                     Must not collide with an existing PoC.
        timeout:     seconds to wait for ``cargo check``

    Returns:
        ``GateResult(True, …)``  — cargo check exited 0; PoC compiles
        ``GateResult(False, …)`` — cargo check failed AND stderr references
            the staged test file. ``details["stderr_tail"]`` carries the
            relevant excerpt.
        ``GateResult(None, …)``  — cargo missing, repo_dir invalid, or
            staging collision (don't block, but the operator should fix).
    """
    t0 = time.time()
    if not _have_cargo():
        return GateResult(
            passed=None,
            reason="cargo binary not on PATH; cannot verify compile",
            duration_s=time.time() - t0,
        )
    if not (repo_dir / "Cargo.toml").is_file():
        return GateResult(
            passed=None,
            reason=f"no Cargo.toml at {repo_dir} — not a Rust crate",
            duration_s=time.time() - t0,
        )

    # Defect 06: validate test_name. Reject path-traversal / unsafe chars
    # BEFORE we touch the filesystem.
    if not _TEST_NAME_RE.fullmatch(test_name):
        return GateResult(
            passed=False,
            reason=(
                f"test_name {test_name!r} fails validation "
                f"(allowed: [A-Za-z][A-Za-z0-9_]{{0,127}}). Path-traversal "
                "or unsafe characters would let the gate stage / unlink "
                "outside tests/."
            ),
            duration_s=time.time() - t0,
        )

    tests_dir = repo_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    staged = tests_dir / f"{test_name}.rs"
    # Belt + suspenders: confirm the resolved path is still inside tests_dir.
    try:
        resolved = staged.resolve()
        tests_resolved = tests_dir.resolve()
        if tests_resolved not in resolved.parents and resolved.parent != tests_resolved:
            return GateResult(
                passed=False,
                reason=(
                    f"resolved staging path {resolved} is outside {tests_resolved}"
                ),
                duration_s=time.time() - t0,
            )
    except OSError:
        pass

    if staged.exists():
        return GateResult(
            passed=None,
            reason=(
                f"refusing to overwrite existing test {staged} "
                "— rename your PoC test or remove the colliding file first."
            ),
            duration_s=time.time() - t0,
        )

    try:
        staged.write_text(poc_source, encoding="utf-8")
        proc = subprocess.run(
            [
                "cargo", "check", "--tests",
                "--message-format=short",
                "--color=never",
            ],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return GateResult(
            passed=False,
            reason=f"cargo check timed out after {timeout}s",
            duration_s=time.time() - t0,
        )
    except OSError as e:
        return GateResult(
            passed=None,
            reason=f"cargo check failed to launch: {e}",
            duration_s=time.time() - t0,
        )
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass

    stdout = _strip_ansi(proc.stdout or "")
    stderr = _strip_ansi(proc.stderr or "")
    combined = stdout + "\n" + stderr

    if proc.returncode == 0:
        return GateResult(
            passed=True,
            reason=f"cargo check passed (exit 0) for tests/{test_name}.rs",
            duration_s=time.time() - t0,
            details={"stderr_tail": stderr[-1000:]},
        )

    # Non-zero — check if our staged file is mentioned in the error output.
    poc_path_token = f"tests/{test_name}.rs"
    poc_path_token_alt = f"tests\\{test_name}.rs"
    poc_referenced = (
        poc_path_token in combined or poc_path_token_alt in combined
    )

    if poc_referenced:
        # Trim to the part of stderr that names our file
        ref_idx = combined.find(poc_path_token)
        if ref_idx < 0:
            ref_idx = combined.find(poc_path_token_alt)
        # Capture a window around the reference for the report
        window = combined[max(0, ref_idx - 240):ref_idx + 600]
        return GateResult(
            passed=False,
            reason=(
                f"cargo check failed (exit {proc.returncode}) with errors "
                f"referencing tests/{test_name}.rs — PoC does not compile "
                "against the real source. See details for excerpt."
            ),
            duration_s=time.time() - t0,
            details={
                "exit_code": proc.returncode,
                "ref_excerpt": window,
                "stderr_tail": stderr[-1500:],
            },
        )

    # Non-zero but errors don't mention our file — pre-existing repo
    # breakage. Defect 07: previously this returned SKIP, which let an
    # attacker land any breaking change in upstream main and silently wave
    # every PoC compile-check through. Now default to FAIL (the repo MUST
    # compile before we believe any PoC verdict), with an explicit operator
    # opt-in via ``allow_broken_tree=True`` for legitimate dirty-tree work.
    if allow_broken_tree:
        return GateResult(
            passed=None,
            reason=(
                f"cargo check failed (exit {proc.returncode}) but errors do NOT "
                f"reference tests/{test_name}.rs. Pre-existing repo breakage "
                "tolerated (allow_broken_tree=True)."
            ),
            duration_s=time.time() - t0,
            details={
                "exit_code": proc.returncode,
                "stderr_tail": stderr[-1500:],
            },
        )
    return GateResult(
        passed=False,
        reason=(
            f"cargo check failed (exit {proc.returncode}). Errors don't "
            f"reference tests/{test_name}.rs — the repo's main tree does "
            "not compile. Fail-closed by default so a broken upstream "
            "doesn't mask hallucinated PoCs. Fix the repo OR pass "
            "allow_broken_tree=True if intentional."
        ),
        duration_s=time.time() - t0,
        details={
            "exit_code": proc.returncode,
            "stderr_tail": stderr[-1500:],
        },
    )


__all__ = ["check_compiles"]
