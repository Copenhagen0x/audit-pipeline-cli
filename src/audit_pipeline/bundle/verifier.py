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
import re
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
    # FIX B-#10: reject LLM-authored patches that touch paths outside the
    # engine repo (path traversal via `+++ b/../../etc/passwd` or absolute
    # paths). git apply IS mostly safe, but defense-in-depth: reject any
    # `..` segments, absolute paths, or paths containing dangerous tokens.
    target = touched[0]
    if (
        target.startswith("/")
        or target.startswith("\\")
        or ".." in target.replace("\\", "/").split("/")
        or "\x00" in target
    ):
        return GateResult(
            False,
            f"patch target {target!r} contains path-traversal or absolute "
            f"path segments — refusing to apply",
            time.time() - t0,
        )
    # Reject diffs that include binary patch markers, symlink-mode changes,
    # rename ops, or new-file modes (LLM should only modify existing files).
    forbidden_markers = [
        "GIT binary patch",
        "Binary files",
        "new file mode 120000",  # symlink
        "rename from",
        "rename to",
        "deleted file mode",
        "new file mode",
    ]
    for marker in forbidden_markers:
        if marker in text:
            return GateResult(
                False,
                f"patch contains forbidden marker {marker!r} — bundle policy "
                f"allows text edits to existing files only",
                time.time() - t0,
            )
    return GateResult(True, f"valid unified diff modifying {target}", time.time() - t0)


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
    # FIX B-#12: validate poc_test_name has the shape `test_<alnum_>`.
    # Without this, an empty / whitespace / wildcard value would expand
    # `cargo test --test <X>` into running ALL tests, any failure of which
    # would falsely confirm the PoC.
    if not re.match(r"^test_[A-Za-z0-9_]+$", poc_test_name):
        return GateResult(
            None,
            f"skipped — poc_test_name {poc_test_name!r} doesn't match "
            f"^test_[A-Za-z0-9_]+$ (could match unrelated tests)",
            time.time() - t0,
        )
    try:
        # cargo test exits 0 if all tests pass. We want the PoC to FAIL on the
        # unpatched repo (i.e. test assertion fires) to prove the bug is
        # reproducible. FIX B-#14: distinguish compile failure / "no such
        # test" / panic from genuine test assertion failure. Any non-zero exit
        # was previously treated as "bug reproduces" which is wrong — compile
        # errors and missing test binaries would falsely confirm fake bugs.
        proc = subprocess.run(
            ["cargo", "test", "--features", "test", "--test", poc_test_name],
            cwd=str(engine_repo),
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return GateResult(False, "cargo test timed out (>600s)", time.time() - t0)

    # Phase B 12-audit P3 Defect 01: this gate used to substring-match the
    # combined log for `"test result: FAILED" / "panicked at" / "assertion
    # failed"`. A PoC using `assert!()` with custom messages, `assert_eq!`,
    # `unimplemented!()`, `todo!()`, `process::abort()`, or a `#[ignore]`d
    # test would match NONE of those, fall through to SKIP, and the bundle
    # would be "verified" anyway because nothing forced a hard FAIL. Now
    # we parse the libtest per-test result line for THIS specific test.
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    from audit_pipeline.utils.cargo_text import parse_test_outcome
    outcome = parse_test_outcome(combined, poc_test_name)

    if outcome.status == "compile_failed":
        return GateResult(
            None,
            f"skipped — cargo failed to compile / no test target named "
            f"{poc_test_name} (this is NOT a bug-reproduction signal). "
            f"excerpt: {outcome.compile_excerpt[:300]}",
            time.time() - t0,
        )
    if outcome.status == "ignored":
        # A `#[ignore]`d test is NOT a fired PoC — the harness skipped it.
        # Old gate would have seen `test result: ok` and silently accepted.
        return GateResult(
            False,
            f"PoC test {poc_test_name} is `#[ignore]`d — refusing to count "
            f"as bug reproduction. Re-author without `#[ignore]`.",
            time.time() - t0,
        )
    if outcome.status == "fired":
        return GateResult(
            True,
            f"PoC test {poc_test_name} ran and FAILED on unpatched repo "
            f"(binary: {outcome.binary_passed} passed, "
            f"{outcome.binary_failed} failed) — bug reproduces",
            time.time() - t0,
            details={"outcome": outcome.status},
        )
    if outcome.status == "passed":
        return GateResult(
            False,
            f"PoC test {poc_test_name} PASSED on unpatched repo — bug not "
            "reproducible. Either the PoC is wrong or the repo already has "
            "the fix.",
            time.time() - t0,
        )
    if outcome.status == "not_run":
        return GateResult(
            False,
            f"PoC test {poc_test_name} was NOT executed by `cargo test "
            f"--test {poc_test_name}`. The test binary either doesn't "
            "contain a function with this name (hallucinated PoC), or it "
            "wasn't compiled in. Re-author the PoC.",
            time.time() - t0,
        )
    # indeterminate — log truncated / unparseable
    return GateResult(
        None,
        f"skipped — could not determine outcome of {poc_test_name} from "
        f"cargo output (exit {proc.returncode}). Log may be truncated.",
        time.time() - t0,
    )


def _apply_patch(engine_repo: Path, patch_text: str) -> tuple[bool, str]:
    """Apply a unified diff via `git apply`. Returns (ok, stderr).

    Uses `--recount` so LLM-authored hunk headers with wrong line numbers
    can still apply when the surrounding context (the lines before/after
    each `@@` block) matches uniquely. Falls back to plain `git apply` if
    --recount also fails.

    Resets the target file to HEAD before applying so previous gate runs
    that left the working tree dirty (or that didn't reverse cleanly)
    don't poison subsequent attempts. The `_unapply_patch` finally clause
    can leave the tree in inconsistent state when --recount has rewritten
    the hunk to apply but the original patch can't be reversed by `-R`.
    """
    if not (engine_repo / ".git").is_dir():
        return (False, f"{engine_repo} is not a git repo (need .git/)")
    # Reset target file to HEAD so prior dirty state can't poison this apply.
    try:
        subprocess.run(
            ["git", "checkout", "--", "src/percolator.rs"],
            cwd=str(engine_repo), capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
    # Pass 1: --recount + --whitespace=fix lets git derive correct line
    # numbers from context. Tolerates LLM hallucinated line numbers.
    for flags in (
        ["--check", "--recount", "--whitespace=fix"],
        ["--check"],
    ):
        try:
            proc = subprocess.run(
                ["git", "apply", *flags, "-"],
                cwd=str(engine_repo),
                input=patch_text, capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            return (False, "git apply --check timed out")
        if proc.returncode == 0:
            apply_flags = [f for f in flags if f != "--check"]
            try:
                proc = subprocess.run(
                    ["git", "apply", *apply_flags, "-"],
                    cwd=str(engine_repo),
                    input=patch_text, capture_output=True, text=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                return (False, "git apply timed out")
            if proc.returncode == 0:
                return (True, "")
    return (False,
            f"git apply --check failed (tried --recount): {proc.stderr.strip()}")


def _unapply_patch(engine_repo: Path, patch_text: str) -> bool:
    """Reverse a unified diff via `git apply -R`. Returns True on success.

    FIX #2 / B-#11: Replaces the previous `git checkout .` rollback which
    would NUKE every uncommitted change in the working tree (including
    hunt's LLM-authored PoC files that live at tests/test_<finding>.rs but
    are not git-tracked). Reverse-apply touches only the patch's own
    files.

    Returns False if the reverse-apply failed AND the working tree still
    has unstaged changes touching the patch's target file — that's a
    partial-state leak risk: subsequent gates run against a contaminated
    tree. Caller should treat False as "skip the rest of this verify
    run" rather than silently continuing.
    """
    try:
        proc = subprocess.run(
            ["git", "apply", "-R", "-"],
            cwd=str(engine_repo),
            input=patch_text, capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0:
            return True
        # Reverse-apply failed. Probe `git status --porcelain` to see if
        # patched files are still dirty — if so, we have residual state.
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(engine_repo),
            capture_output=True, text=True, timeout=10,
        )
        if status.stdout.strip():
            # Anything dirty after a failed reverse = partial state
            return False
        return True  # reverse failed but tree is clean → safe (patch never applied)
    except (subprocess.TimeoutExpired, OSError):
        return False


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
        _unapply_patch(engine_repo, patch_text)
        return GateResult(False, "cargo test timed out post-patch (>600s)", time.time() - t0)
    finally:
        # Always roll back to keep the working tree clean
        _unapply_patch(engine_repo, patch_text)

    # Phase B 12-audit P3 Defect 01 (post-patch half): this gate used to
    # accept ANY rc=0 as PASS with no failure detection. If the patch broke
    # compilation OR removed the test entirely, cargo could exit 0 (test
    # not in this binary) and the gate would PASS the bundle. Mirror the
    # pre-patch structured parse for symmetry.
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    from audit_pipeline.utils.cargo_text import parse_test_outcome
    outcome = parse_test_outcome(combined, poc_test_name)

    if outcome.status == "passed":
        return GateResult(
            True,
            f"PoC test {poc_test_name} PASSED post-patch — bug fixed.",
            time.time() - t0,
            details={"outcome": outcome.status},
        )
    if outcome.status == "fired":
        return GateResult(
            False,
            f"PoC test {poc_test_name} still FAILED post-patch — patch did "
            "not fix the bug.",
            time.time() - t0,
        )
    if outcome.status == "ignored":
        return GateResult(
            False,
            f"PoC test {poc_test_name} is `#[ignore]`d post-patch — patch "
            "may have side-effected the test annotation. Re-author.",
            time.time() - t0,
        )
    if outcome.status == "compile_failed":
        return GateResult(
            False,
            f"Patch broke the build — `cargo test` failed to compile. "
            f"Excerpt: {outcome.compile_excerpt[:300]}",
            time.time() - t0,
        )
    if outcome.status == "not_run":
        return GateResult(
            False,
            f"Post-patch, test {poc_test_name} is NOT in the test binary "
            "(patch may have deleted the function, or the harness no "
            "longer compiles it in). This is NOT 'bug fixed'.",
            time.time() - t0,
        )
    # indeterminate
    return GateResult(
        None,
        f"could not determine post-patch outcome of {poc_test_name}.",
        time.time() - t0,
    )


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
        _unapply_patch(engine_repo, patch_text)
        return GateResult(False, "full cargo test timed out (>1800s)", time.time() - t0)
    finally:
        _unapply_patch(engine_repo, patch_text)

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
        _unapply_patch(engine_repo, patch_text)
        return GateResult(False, "kani timed out (>1800s)", time.time() - t0)
    finally:
        _unapply_patch(engine_repo, patch_text)

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

    # FIX B-#18: derive engine_sha from `git rev-parse HEAD` inside the engine
    # repo as the AUTHORITATIVE provenance value, not from a free-form arg
    # that could be doctored via meta.json. Falls back to the caller-supplied
    # engine_sha only when git isn't available.
    actual_engine_sha = engine_sha
    if engine_repo is not None and (engine_repo / ".git").is_dir():
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(engine_repo),
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                actual_engine_sha = proc.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            pass  # fall back to caller-supplied

    out = {
        "finding_id": finding_id,
        "engine_sha": actual_engine_sha,
        "engine_sha_claimed": engine_sha if engine_sha != actual_engine_sha else None,
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
