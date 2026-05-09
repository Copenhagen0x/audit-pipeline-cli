"""Bundle verification gates.

Four machine gates run BEFORE the operator can authorize a bundle.
All four must pass for `bundle review` to even offer the authorization
prompt. The result is persisted to `<bundle-dir>/verification.json` so
the authorization marker can checksum it.

Gates (in order, fail-fast):

  1. patch_well_formed   — patch.diff parses as a unified diff and
                            touches at most ONE file
  2. poc_fails_pre_patch — running the PoC against the unpatched repo
                            triggers the bug (regression has the right
                            shape — proves the PoC is real)
  3. poc_passes_post_patch — applying patch.diff and re-running the PoC
                            does NOT trigger the bug (patch actually fixes)
  4. tests_pass_post_patch — running the full existing test suite against
                            the patched repo passes (no new regressions)

Optional gate (runs when registered):

  5. kani_proof_holds    — re-runs the registered Kani harness for this
                            bug class against the patched code and
                            confirms the invariant still verifies

Each gate has a `passed: bool` + `reason: str` + `duration_s: float`.
The verification.json shape:

  {
    "finding_id": <id>,
    "engine_sha": <40-hex>,
    "patch_sha":  <sha256>,
    "ran_at":     <ISO-8601 UTC>,
    "gates": {
      "patch_well_formed":     {"passed": True, "reason": "...", "duration_s": 0.01},
      "poc_fails_pre_patch":   {"passed": True, "reason": "...", "duration_s": 4.2},
      "poc_passes_post_patch": {"passed": True, "reason": "...", "duration_s": 4.0},
      "tests_pass_post_patch": {"passed": True, "reason": "...", "duration_s": 38.0},
      "kani_proof_holds":      {"passed": null, "reason": "skipped — no harness", "duration_s": 0}
    }
  }

The actual cargo / Kani invocation is delegated to the existing layer
modules (confirm.py for cargo test, kani.py for Kani). This module
focuses on the orchestration + verdict aggregation. When neither
cargo nor Kani is reachable (CI / no-toolchain machines), gates 2-5
are recorded as `passed: null, reason: "skipped — toolchain absent"`,
which is treated as a FAIL by `auth.write_authorization()`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from audit_pipeline.bundle.auth import file_sha256
from audit_pipeline.bundle.patcher import files_touched, is_unified_diff
from audit_pipeline.bundle.paths import (
    bundle_dir,
    patch_path,
    verification_path,
)


@dataclass
class GateResult:
    passed: bool | None       # True / False / None (skipped)
    reason: str
    duration_s: float

    def to_json(self) -> dict:
        return {
            "passed": self.passed,
            "reason": self.reason,
            "duration_s": round(self.duration_s, 3),
        }


def _gate_patch_well_formed(workspace: Path, finding_id: int) -> GateResult:
    t0 = time.time()
    p = patch_path(workspace, finding_id)
    if not p.is_file():
        return GateResult(False, f"missing patch.diff at {p}", time.time() - t0)
    text = p.read_text(encoding="utf-8", errors="replace")
    if not is_unified_diff(text):
        return GateResult(False, "patch.diff is not a valid unified diff", time.time() - t0)
    touched = files_touched(text)
    if not touched:
        return GateResult(False, "patch touches no files (no +++ b/ headers)", time.time() - t0)
    if len(touched) > 1:
        return GateResult(
            False,
            f"patch touches {len(touched)} files: {touched}. "
            f"Bundle policy is one-file-per-patch.",
            time.time() - t0,
        )
    return GateResult(True, f"valid unified diff modifying {touched[0]}", time.time() - t0)


def _have_cargo() -> bool:
    return shutil.which("cargo") is not None


def _have_kani() -> bool:
    return shutil.which("cargo-kani") is not None or shutil.which("kani") is not None


def _gate_poc_fails_pre_patch(
    workspace: Path,
    finding_id: int,
    engine_repo: Path | None,
    poc_test_name: str | None,
) -> GateResult:
    t0 = time.time()
    if not _have_cargo():
        return GateResult(None, "skipped — cargo not in PATH", time.time() - t0)
    if engine_repo is None or not engine_repo.is_dir():
        return GateResult(None, "skipped — no engine_repo provided", time.time() - t0)
    if not poc_test_name:
        return GateResult(None, "skipped — no poc_test_name provided", time.time() - t0)
    try:
        # cargo test exits 0 if all tests pass. We want the PoC to FAIL on the
        # unpatched repo (i.e. exit non-zero) to prove the bug is reproducible.
        proc = subprocess.run(
            ["cargo", "test", "--features", "test", "--test", poc_test_name],
            cwd=str(engine_repo),
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return GateResult(False, "cargo test timed out (>600s)", time.time() - t0)
    if proc.returncode == 0:
        return GateResult(
            False,
            "PoC PASSED on unpatched repo — bug is not reproducible. "
            "Either the PoC is wrong or the repo already has the fix.",
            time.time() - t0,
        )
    return GateResult(True, f"PoC failed on unpatched repo (exit {proc.returncode}) — bug reproduces",
                      time.time() - t0)


def _apply_patch(engine_repo: Path, patch_text: str) -> tuple[bool, str]:
    """Apply a unified diff via `git apply`. Returns (ok, stderr)."""
    if not (engine_repo / ".git").is_dir():
        return (False, f"{engine_repo} is not a git repo (need .git/)")
    try:
        proc = subprocess.run(
            ["git", "apply", "--check", "-"],
            cwd=str(engine_repo),
            input=patch_text, capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return (False, f"git apply --check failed: {proc.stderr.strip()}")
        proc = subprocess.run(
            ["git", "apply", "-"],
            cwd=str(engine_repo),
            input=patch_text, capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return (False, f"git apply failed: {proc.stderr.strip()}")
    except subprocess.TimeoutExpired:
        return (False, "git apply timed out")
    return (True, "")


def _gate_poc_passes_post_patch(
    workspace: Path,
    finding_id: int,
    engine_repo: Path | None,
    poc_test_name: str | None,
) -> GateResult:
    t0 = time.time()
    if not _have_cargo():
        return GateResult(None, "skipped — cargo not in PATH", time.time() - t0)
    if engine_repo is None or not engine_repo.is_dir():
        return GateResult(None, "skipped — no engine_repo provided", time.time() - t0)
    if not poc_test_name:
        return GateResult(None, "skipped — no poc_test_name provided", time.time() - t0)

    p = patch_path(workspace, finding_id)
    if not p.is_file():
        return GateResult(False, "no patch.diff to apply", time.time() - t0)

    # Apply the patch in a stash to keep the engine_repo clean for re-runs
    # We use git apply + git stash to ensure we can roll back regardless of
    # whether cargo test passes or fails.
    patch_text = p.read_text(encoding="utf-8", errors="replace")
    ok, err = _apply_patch(engine_repo, patch_text)
    if not ok:
        return GateResult(False, f"could not apply patch: {err}", time.time() - t0)

    try:
        proc = subprocess.run(
            ["cargo", "test", "--features", "test", "--test", poc_test_name],
            cwd=str(engine_repo),
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        # roll back patch
        subprocess.run(["git", "checkout", "."], cwd=str(engine_repo))
        return GateResult(False, "cargo test timed out post-patch (>600s)", time.time() - t0)
    finally:
        # Always roll back to keep the working tree clean
        subprocess.run(["git", "checkout", "."], cwd=str(engine_repo),
                       capture_output=True)

    if proc.returncode != 0:
        return GateResult(
            False,
            f"PoC still failed post-patch (exit {proc.returncode}). "
            f"Patch did not fix the bug.",
            time.time() - t0,
        )
    return GateResult(True, "PoC passed post-patch — bug fixed", time.time() - t0)


def _gate_tests_pass_post_patch(
    workspace: Path,
    finding_id: int,
    engine_repo: Path | None,
) -> GateResult:
    t0 = time.time()
    if not _have_cargo():
        return GateResult(None, "skipped — cargo not in PATH", time.time() - t0)
    if engine_repo is None or not engine_repo.is_dir():
        return GateResult(None, "skipped — no engine_repo provided", time.time() - t0)

    p = patch_path(workspace, finding_id)
    if not p.is_file():
        return GateResult(False, "no patch.diff to apply", time.time() - t0)

    patch_text = p.read_text(encoding="utf-8", errors="replace")
    ok, err = _apply_patch(engine_repo, patch_text)
    if not ok:
        return GateResult(False, f"could not apply patch: {err}", time.time() - t0)

    try:
        proc = subprocess.run(
            ["cargo", "test", "--features", "test"],
            cwd=str(engine_repo),
            capture_output=True, text=True, timeout=1800,
        )
    except subprocess.TimeoutExpired:
        subprocess.run(["git", "checkout", "."], cwd=str(engine_repo),
                       capture_output=True)
        return GateResult(False, "full cargo test timed out (>1800s)", time.time() - t0)
    finally:
        subprocess.run(["git", "checkout", "."], cwd=str(engine_repo),
                       capture_output=True)

    if proc.returncode != 0:
        # Truncate stderr so it fits in verification.json
        tail = (proc.stderr or "")[-400:]
        return GateResult(
            False,
            f"existing test suite regressed post-patch (exit {proc.returncode}). "
            f"tail: {tail}",
            time.time() - t0,
        )
    return GateResult(True, "full test suite passed post-patch", time.time() - t0)


def _gate_kani_proof_holds(
    workspace: Path,
    finding_id: int,
    engine_repo: Path | None,
    kani_harness: str | None,
) -> GateResult:
    t0 = time.time()
    if not _have_kani():
        return GateResult(None, "skipped — cargo-kani not in PATH", time.time() - t0)
    if engine_repo is None or not engine_repo.is_dir():
        return GateResult(None, "skipped — no engine_repo provided", time.time() - t0)
    if not kani_harness:
        return GateResult(None, "skipped — no kani_harness registered for this bug class",
                          time.time() - t0)

    p = patch_path(workspace, finding_id)
    if not p.is_file():
        return GateResult(False, "no patch.diff to apply", time.time() - t0)

    patch_text = p.read_text(encoding="utf-8", errors="replace")
    ok, err = _apply_patch(engine_repo, patch_text)
    if not ok:
        return GateResult(False, f"could not apply patch: {err}", time.time() - t0)

    try:
        proc = subprocess.run(
            ["cargo", "kani", "--harness", kani_harness],
            cwd=str(engine_repo),
            capture_output=True, text=True, timeout=1800,
        )
    except subprocess.TimeoutExpired:
        subprocess.run(["git", "checkout", "."], cwd=str(engine_repo),
                       capture_output=True)
        return GateResult(False, "kani timed out (>1800s)", time.time() - t0)
    finally:
        subprocess.run(["git", "checkout", "."], cwd=str(engine_repo),
                       capture_output=True)

    if proc.returncode != 0:
        return GateResult(False, f"kani harness {kani_harness} did not verify post-patch",
                          time.time() - t0)
    return GateResult(True, f"kani harness {kani_harness} verified post-patch",
                      time.time() - t0)


def run_all_gates(
    workspace: Path,
    finding_id: int,
    *,
    engine_sha: str = "",
    engine_repo: Path | None = None,
    poc_test_name: str | None = None,
    kani_harness: str | None = None,
) -> dict:
    """Run all gates and persist the result to verification.json."""
    bdir = bundle_dir(workspace, finding_id)
    bdir.mkdir(parents=True, exist_ok=True)

    gates: dict[str, GateResult] = {}
    gates["patch_well_formed"] = _gate_patch_well_formed(workspace, finding_id)

    if gates["patch_well_formed"].passed:
        gates["poc_fails_pre_patch"] = _gate_poc_fails_pre_patch(
            workspace, finding_id, engine_repo, poc_test_name)
        gates["poc_passes_post_patch"] = _gate_poc_passes_post_patch(
            workspace, finding_id, engine_repo, poc_test_name)
        gates["tests_pass_post_patch"] = _gate_tests_pass_post_patch(
            workspace, finding_id, engine_repo)
        gates["kani_proof_holds"] = _gate_kani_proof_holds(
            workspace, finding_id, engine_repo, kani_harness)
    else:
        # Skip downstream gates if the patch is malformed
        for gate_name in ("poc_fails_pre_patch", "poc_passes_post_patch",
                           "tests_pass_post_patch", "kani_proof_holds"):
            gates[gate_name] = GateResult(
                None, "skipped — patch_well_formed failed", 0.0)

    out = {
        "finding_id": finding_id,
        "engine_sha": engine_sha,
        "patch_sha":  file_sha256(patch_path(workspace, finding_id)),
        "ran_at":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gates":      {k: v.to_json() for k, v in gates.items()},
    }

    verification_path(workspace, finding_id).write_text(
        json.dumps(out, indent=2, sort_keys=True), encoding="utf-8",
    )
    return out


def all_passed(verification: dict) -> bool:
    """True iff every gate has passed=True (None / False both fail)."""
    return all(
        g.get("passed") is True
        for g in (verification.get("gates") or {}).values()
    )
