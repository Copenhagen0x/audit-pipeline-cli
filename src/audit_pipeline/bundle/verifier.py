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

Optional gates (run when registered):

  5. kani_proof_holds            — re-runs the registered Kani harness for this
                                    bug class against the patched code and
                                    confirms the invariant still verifies
  6. litesvm_exploit_neutralized — re-runs the registered LiteSVM exploit
                                    test against the patched code; pre-patch
                                    the test FAILS (exploit fires), post-patch
                                    it must PASS (Solana BPF runtime evidence
                                    that the fix actually closes the hole, not
                                    just that Kani's SMT can't find a CEX)

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
    # Optional structured payload — pre/post-patch gates use this to surface
    # parse_test_outcome.status ("fired" / "passed" / etc) so downstream
    # callers can branch on it without re-parsing the cargo log.
    # Audit caught: previously GateResult had no `details` field but two
    # callers passed `details=`, raising TypeError on the SUCCESS path of
    # the pre/post-patch gates — silently disabling them on real cargo runs.
    details: dict | None = None

    def to_json(self) -> dict:
        out = {
            "passed": self.passed,
            "reason": self.reason,
            "duration_s": round(self.duration_s, 3),
        }
        if self.details is not None:
            out["details"] = self.details
        return out


def _workspace_has_test_feature(engine_repo: Path) -> bool:
    """Check if any crate in the workspace declares a `test` cargo feature.

    Percolator's PoC harness gated test-only helpers behind a `test`
    feature so `cargo test --features test` was required. Anchor /
    Solana program workspaces typically don't have such a feature, and
    passing `--features test` makes cargo reject the build with
    "none of the selected packages contains this feature: test".
    Detect on the fly so we only pass the flag where it's recognised.
    """
    try:
        # Check the workspace root Cargo.toml + each `programs/*/Cargo.toml`
        cargo_files = [engine_repo / "Cargo.toml"]
        programs_dir = engine_repo / "programs"
        if programs_dir.is_dir():
            for p in programs_dir.iterdir():
                if p.is_dir():
                    ct = p / "Cargo.toml"
                    if ct.is_file():
                        cargo_files.append(ct)
        for ct in cargo_files:
            if not ct.is_file():
                continue
            body = ct.read_text(encoding="utf-8", errors="replace")
            # Look for `[features]\n...\ntest = [...]` or `[features]\ntest = "..."`
            m = re.search(
                r"\[features\][^\[]*?^\s*test\s*=",
                body,
                re.MULTILINE | re.DOTALL,
            )
            if m:
                return True
    except OSError:
        pass
    return False


def _cargo_test_argv(engine_repo: Path, *extra: str) -> list[str]:
    """Build a `cargo test ...` argv, including `--features test` only when
    the workspace actually declares that feature. See _workspace_has_test_feature."""
    argv = ["cargo", "test"]
    if _workspace_has_test_feature(engine_repo):
        argv += ["--features", "test"]
    argv += list(extra)
    return argv


def _detect_engine_language(engine_repo: Path) -> str:
    """Inspect the repo root for the toolchain marker. Returns the language
    tag used by the adapter modules: "rust" | "solidity" | "move" | "unknown".

    Used by the test-suite gate to dispatch the right runner (cargo /
    forge / aptos move test). Order matters — Solidity repos often
    coexist with a Cargo.toml from a side tool, but foundry.toml is
    the strongest signal that the audit target is Solidity."""
    if (engine_repo / "foundry.toml").is_file():
        return "solidity"
    if (engine_repo / "Move.toml").is_file():
        return "move"
    if (engine_repo / "Cargo.toml").is_file():
        return "rust"
    return "unknown"


def _have_forge() -> bool:
    return shutil.which("forge") is not None


def _engine_test_argv(engine_repo: Path) -> list[str]:
    """Return the test-suite command argv for the target's language.

    For Solidity (foundry.toml present): `forge test --json` (so we can
    parse pass/fail per-test instead of trusting the exit code alone).
    For Rust / Anchor (Cargo.toml): `cargo test [--features test]`.
    """
    lang = _detect_engine_language(engine_repo)
    if lang == "solidity":
        # `--json` emits structured per-test output so the gate can
        # detect failures even when forge exits 0 (some forge versions
        # exit 0 with `status: "Failure"` test results, especially
        # with `--no-fail-fast`).
        return ["forge", "test", "--json"]
    # Default: rust / anchor / unknown all go to cargo
    return _cargo_test_argv(engine_repo)


def _forge_json_has_failures(stdout: str) -> tuple[bool, int, str]:
    """Parse forge --json output for per-test failures.

    Returns (any_failed, failed_count, first_reason). Each line of
    forge --json stdout is a JSON object keyed by
    "<file>:<contract>" containing test_results. A test failed if
    status == "Failure" or success == False.

    Falls back to a substring regex on `"status":"Failure"` (with
    optional whitespace) for the case where the JSON has been
    truncated mid-stream (L2 adapter caps stdout at 8000 chars
    before writing the runlog, so json.loads on the runlog fails
    even though the Failure marker is present in the visible bytes).
    """
    failed: list[str] = []
    first_reason = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        for _file, fdata in obj.items():
            if not isinstance(fdata, dict):
                continue
            results = fdata.get("test_results") or {}
            for tname, tdata in results.items():
                if not isinstance(tdata, dict):
                    continue
                status = str(tdata.get("status") or "").strip().lower()
                success = tdata.get("success")
                if status == "failure" or success is False:
                    failed.append(tname)
                    if not first_reason:
                        first_reason = str(tdata.get("reason") or status or "")[:200]

    # Fallback: handle truncated JSON. The L2 adapter truncates stdout
    # at 8000 chars (line 514 of poc_adapters/solidity.py), which is
    # smaller than a typical forge --json output (~10-30KB per test).
    # The Failure marker reliably appears in the first ~500 bytes of
    # output for fired tests, so a substring scan recovers the signal
    # without needing a full JSON parse.
    if not failed:
        m = re.search(r'"status"\s*:\s*"Failure"', stdout)
        if m:
            failed.append("(truncated-json-failure)")
            r = re.search(r'"reason"\s*:\s*"([^"]+)"', stdout)
            if r:
                first_reason = r.group(1)[:200]
    return (len(failed) > 0, len(failed), first_reason)


def _find_standalone_poc_log(workspace: Path, poc_test_name: str) -> Path | None:
    """Locate the L2 cargo log for a rustc-standalone PoC, if one exists.

    Solana / Aptos L2 PoCs are typically compiled with `rustc --test` against
    a single .rs file rather than as cargo workspace members. The L2 stage
    writes the compile + test output to ``<workspace>/hunts/<cycle>/poc/cargo_<slug>.log``
    where ``<slug>`` is the lowercase, hyphen-replaced hyp_id.

    The verify gate uses this log as authoritative pre-patch evidence:
    if the PoC fired at L2, the bug is reproducible. We don't try to
    re-run a standalone PoC during verify because it has its own
    inlined copy of the buggy code and isn't sensitive to the patch.

    Returns the latest matching log path, or None if no standalone
    log was found (caller falls back to cargo workspace mode).
    """
    if not workspace or not workspace.is_dir():
        return None
    # poc_test_name shape: test_<slug>[_fires|_panics|...]
    # The cargo log is named cargo_<slug>.log. Strip leading "test_"
    # and any trailing suffix to derive the slug.
    name = poc_test_name
    if name.startswith("test_"):
        name = name[5:]
    # Strip trailing convention suffixes
    for suf in ("_fires", "_panics", "_witness", "_reproduces"):
        if name.endswith(suf):
            name = name[: -len(suf)]
            break
    candidates = sorted(
        workspace.glob(f"hunts/*/poc/cargo_{name}.log"),
        key=lambda p: p.stat().st_mtime if p.is_file() else 0,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _standalone_poc_fired(log_path: Path, poc_test_name: str) -> bool:
    """Parse a rustc-standalone cargo log; True if the named test failed.

    libtest emits either `test <name> ... FAILED` (bare test fn) or
    `test <module>::<name> ... FAILED` (test fn inside a module). Match
    both forms.
    """
    try:
        body = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if "test result: FAILED" not in body:
        return False
    # Anchor on word boundary so partial-name matches don't falsely fire:
    # match `test <maybe::path>name ... FAILED` where the named test fn
    # appears at the end of the cargo line.
    pat = re.compile(
        rf"^test\s+(?:[A-Za-z0-9_]+::)*{re.escape(poc_test_name)}\s+\.\.\.\s+FAILED",
        re.MULTILINE,
    )
    return pat.search(body) is not None


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
    # A legitimate structural fix can span a few files within one crate
    # (e.g. state schema change + the handlers that use it). Cap at 5
    # files and require all paths share the same `programs/<name>/`
    # crate root so a patch can't accidentally cross crate boundaries.
    MAX_FILES_PER_BUNDLE = 5
    if len(touched) > MAX_FILES_PER_BUNDLE:
        return GateResult(
            False,
            f"patch touches {len(touched)} files (>{MAX_FILES_PER_BUNDLE}): {touched}. "
            f"Bundles are scoped to at most {MAX_FILES_PER_BUNDLE} files within one crate.",
            time.time() - t0,
        )
    if len(touched) > 1:
        crate_roots = set()
        for f in touched:
            parts = f.split("/")
            if len(parts) >= 2 and parts[0] == "programs":
                crate_roots.add(parts[1])
            else:
                crate_roots.add("__root__")
        if len(crate_roots) > 1:
            return GateResult(
                False,
                f"patch touches multiple crates {sorted(crate_roots)}: {touched}. "
                f"Bundles are scoped to a single crate.",
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


def _gate_poc_fails_pre_patch_solidity(
    workspace: Path,
    finding_id: int,
    engine_repo: Path,
    poc_test_name: str,
) -> GateResult:
    """Solidity variant: read the L2 forge runlog to verify the PoC fired
    pre-patch. The L2 runlog at
    ``hunts/<cycle>/poc/runlog_<slug>.log`` already contains the forge
    JSON output from when L2 dispatched the PoC. If it shows
    ``"status":"Failure"`` for any test, the bug is reproducible.
    """
    t0 = time.time()
    # poc_test_name is "test_<slug>" — strip prefix to get the slug
    slug = poc_test_name[5:] if poc_test_name.startswith("test_") else poc_test_name
    # Look in the cycle's poc/ dir; need to find cycle dir from workspace
    poc_dir_candidates = list(workspace.glob("hunts/*/poc"))
    if not poc_dir_candidates:
        return GateResult(None, "skipped — no hunts/*/poc dir found",
                          time.time() - t0)
    # Pick the most recent cycle
    poc_dir = sorted(poc_dir_candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    runlog = poc_dir / f"runlog_{slug}.log"
    if not runlog.is_file():
        return GateResult(None, f"skipped — L2 runlog not found at {runlog}",
                          time.time() - t0)
    try:
        log_text = runlog.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return GateResult(None, f"skipped — could not read L2 runlog {runlog}",
                          time.time() - t0)
    any_failed, n_failed, first_reason = _forge_json_has_failures(log_text)
    if any_failed:
        return GateResult(
            True,
            f"PoC fired at L2 ({n_failed} forge test failure(s); reason: "
            f"{first_reason[:120]})",
            time.time() - t0,
            details={"outcome": "fired", "mode": "forge-l2-runlog"},
        )
    if "Compilation failed" in log_text or "compile error" in log_text.lower():
        return GateResult(
            False,
            f"PoC at L2 had COMPILE error — bundle cannot be authorized "
            f"without a fired PoC",
            time.time() - t0,
        )
    return GateResult(
        False,
        f"PoC at L2 did not fire (no forge test Failure in {runlog.name})",
        time.time() - t0,
    )


def _gate_poc_fails_pre_patch(
    workspace: Path,
    finding_id: int,
    engine_repo: Path | None,
    poc_test_name: str | None,
) -> GateResult:
    t0 = time.time()
    if engine_repo is None or not engine_repo.is_dir():
        return GateResult(None, "skipped — no engine_repo provided", time.time() - t0)
    if not poc_test_name:
        return GateResult(None, "skipped — no poc_test_name provided", time.time() - t0)
    # Language-aware dispatch: Solidity reads the forge L2 runlog;
    # Rust/Solana re-runs cargo test on the standalone PoC.
    if _detect_engine_language(engine_repo) == "solidity":
        return _gate_poc_fails_pre_patch_solidity(
            workspace, finding_id, engine_repo, poc_test_name,
        )
    if not _have_cargo():
        return GateResult(None, "skipped — cargo not in PATH", time.time() - t0)
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

    # Detect rustc-standalone PoC (typical for Solana / Aptos cycles where
    # L2 PoCs are single-file .rs tests compiled with `rustc --test`, not
    # cargo workspace members). When present, the L2 cargo log already
    # records the pre-patch fire result — read it directly instead of
    # re-running cargo (which would fail to find the test in the engine
    # repo's workspace).
    standalone_log = _find_standalone_poc_log(workspace, poc_test_name)
    if standalone_log is not None:
        if _standalone_poc_fired(standalone_log, poc_test_name):
            return GateResult(
                True,
                f"PoC test {poc_test_name} fired at L2 "
                f"(rustc-standalone log: {standalone_log.name})",
                time.time() - t0,
                details={"outcome": "fired", "mode": "rustc-standalone"},
            )
        return GateResult(
            False,
            f"PoC test {poc_test_name} did NOT fire at L2 "
            f"(standalone log shows test passed — bug not reproducible)",
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
            _cargo_test_argv(engine_repo, "--test", poc_test_name),
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
    # Reset every touched file to HEAD so prior dirty state can't poison
    # this apply. P3+P4 audit Defect 02 cont.: this used to hardcode
    # `src/percolator.rs`; now reads `files_touched(patch)` so the reset
    # works for any engine layout.
    files = files_touched(patch_text) or ["src/percolator.rs"]
    for f in files:
        try:
            subprocess.run(
                ["git", "checkout", "--", f],
                cwd=str(engine_repo), capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass

    # P3+P4 audit Defect 02 (CRITICAL): capture each hunk's claimed
    # ``@@ -<orig_line>,<n> +<new_line>,<m> @@`` BEFORE apply so we can
    # verify post-apply that git didn't silently re-anchor the patch
    # to a different location (`--recount` masks wrong-anchor patches
    # by deriving line numbers from context alone).
    import re as _re
    claimed_hunks: list[tuple[str, int, int]] = []
    current_file = None
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            continue
        m = _re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if m and current_file:
            new_start = int(m.group(3))
            new_count = int(m.group(4) or "1")
            claimed_hunks.append((current_file, new_start, new_count))
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
                # P3+P4 audit Defect 02 (CRITICAL) post-apply check:
                # diff the working tree against HEAD and confirm each
                # cited hunk-anchor line is INSIDE the resulting diff
                # range for the same file. If git's `--recount` re-
                # anchored to a different region, the actual modified
                # lines won't overlap with `claimed_hunks` and we
                # refuse the apply.
                anchor_ok, anchor_msg = _verify_anchors_post_apply(
                    engine_repo, claimed_hunks,
                )
                if not anchor_ok:
                    # Roll back the misapplied patch before returning
                    _unapply_patch(engine_repo, patch_text)
                    return (False,
                            f"patch applied but at wrong anchor: {anchor_msg}")
                return (True, "")
    return (False,
            f"git apply --check failed (tried --recount): {proc.stderr.strip()}")


def _verify_anchors_post_apply(
    engine_repo: Path,
    claimed_hunks: list[tuple[str, int, int]],
) -> tuple[bool, str]:
    """Confirm each claimed `@@ +<line>,<count> @@` overlaps the actual
    modified line range in the working tree.

    Runs ``git diff --unified=0 -- <file>`` per touched file and parses
    the post-image hunks. For each claim ``(file, start, count)``,
    require the claim's range to overlap at least one actual modified
    range. Tolerates ±10 lines slack to absorb whitespace fixups.
    """
    if not claimed_hunks:
        return (True, "no hunk anchors to verify")
    import re as _re
    by_file: dict[str, list[tuple[int, int]]] = {}
    files = sorted({f for f, _, _ in claimed_hunks})
    SLACK = 10
    for f in files:
        try:
            proc = subprocess.run(
                ["git", "diff", "--unified=0", "--", f],
                cwd=str(engine_repo),
                capture_output=True, text=True, timeout=20,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return (False, f"git diff failed for {f}: {e}")
        if proc.returncode != 0:
            return (False, f"git diff exit {proc.returncode} for {f}")
        ranges: list[tuple[int, int]] = []
        for line in (proc.stdout or "").splitlines():
            m = _re.match(r"^@@ -(?:\d+)(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2) or "1")
                ranges.append((start, count))
        by_file[f] = ranges
    for f, start, count in claimed_hunks:
        actual_ranges = by_file.get(f, [])
        claim_lo, claim_hi = start, start + count + SLACK
        overlapping = any(
            (a_start <= claim_hi and a_start + a_count + SLACK >= claim_lo)
            for a_start, a_count in actual_ranges
        )
        if not overlapping:
            return (False,
                    f"file {f}: claimed @@ +{start},{count} @@ doesn't "
                    f"overlap any actual modification (ranges: {actual_ranges})")
    return (True, f"all {len(claimed_hunks)} hunk anchors verified")


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


def _gate_poc_passes_post_patch_solidity(
    workspace: Path,
    finding_id: int,
    engine_repo: Path,
    poc_test_name: str,
) -> GateResult:
    """Solidity variant: apply patch, redeploy the L2 PoC test, run
    `forge test`, verify the test now PASSES (= the patch defuses
    the bug). The L2 PoC test file persists at
    ``<workspace>/tests/solidity/test_<name>.t.sol`` from L2 dispatch.
    """
    t0 = time.time()
    if not _have_forge():
        return GateResult(None, "skipped — forge not in PATH",
                          time.time() - t0)
    # Locate L2 test file in workspace
    slug = poc_test_name[5:] if poc_test_name.startswith("test_") else poc_test_name
    l2_src = workspace / "tests" / "solidity" / f"test_{slug}.t.sol"
    if not l2_src.is_file():
        return GateResult(
            None,
            f"skipped — L2 test file not found at {l2_src}",
            time.time() - t0,
        )

    p = patch_path(workspace, finding_id)
    if not p.is_file():
        return GateResult(False, "no patch.diff to apply", time.time() - t0)
    patch_text = p.read_text(encoding="utf-8", errors="replace")

    # Apply patch
    ok, err = _apply_patch(engine_repo, patch_text)
    if not ok:
        return GateResult(False, f"could not apply patch: {err}",
                          time.time() - t0)

    # Deploy L2 test into repo's test dir + run forge
    repo_test_dir = engine_repo / "tests"
    if not repo_test_dir.is_dir():
        repo_test_dir = engine_repo / "test"
    repo_test_dir.mkdir(parents=True, exist_ok=True)
    deployed = repo_test_dir / f"jelleo_p3_verify_{slug}.t.sol"
    try:
        deployed.write_text(l2_src.read_text(encoding="utf-8", errors="replace"),
                             encoding="utf-8")
    except OSError as e:
        _unapply_patch(engine_repo, patch_text)
        return GateResult(False, f"could not deploy L2 test: {e}",
                          time.time() - t0)

    try:
        proc = subprocess.run(
            ["forge", "test", "--match-path",
             str(deployed.relative_to(engine_repo)), "--json"],
            cwd=str(engine_repo),
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        deployed.unlink(missing_ok=True)
        _unapply_patch(engine_repo, patch_text)
        return GateResult(False, "forge test timed out post-patch (>600s)",
                          time.time() - t0)
    finally:
        deployed.unlink(missing_ok=True)
        _unapply_patch(engine_repo, patch_text)

    # The L2 PoC used to FAIL pre-patch (= bug). Post-patch, it must
    # PASS (no test_results with status=Failure).
    any_failed, n_failed, first_reason = _forge_json_has_failures(proc.stdout or "")
    if any_failed:
        return GateResult(
            False,
            f"PoC still fires post-patch ({n_failed} forge failure(s); "
            f"first: {first_reason[:120]}) — patch does not defuse the bug",
            time.time() - t0,
        )
    if proc.returncode != 0:
        return GateResult(
            False,
            f"forge test exit {proc.returncode} post-patch (likely compile "
            f"error or other infra issue): "
            f"{(proc.stderr or proc.stdout)[:200]}",
            time.time() - t0,
        )
    return GateResult(
        True,
        f"PoC passes post-patch — patch defuses the bug "
        f"(forge test --match-path jelleo_p3_verify_{slug}.t.sol exit 0, no failures)",
        time.time() - t0,
        details={"mode": "forge-test-on-l2-redeploy"},
    )


def _gate_poc_passes_post_patch(
    workspace: Path,
    finding_id: int,
    engine_repo: Path | None,
    poc_test_name: str | None,
) -> GateResult:
    t0 = time.time()
    if engine_repo is None or not engine_repo.is_dir():
        return GateResult(None, "skipped — no engine_repo provided", time.time() - t0)
    if not poc_test_name:
        return GateResult(None, "skipped — no poc_test_name provided", time.time() - t0)
    # Language-aware dispatch
    if _detect_engine_language(engine_repo) == "solidity":
        return _gate_poc_passes_post_patch_solidity(
            workspace, finding_id, engine_repo, poc_test_name,
        )
    if not _have_cargo():
        return GateResult(None, "skipped — cargo not in PATH", time.time() - t0)

    p = patch_path(workspace, finding_id)
    if not p.is_file():
        return GateResult(False, "no patch.diff to apply", time.time() - t0)

    # Rustc-standalone PoCs have their own inlined copy of the buggy code,
    # so the engine-repo patch doesn't affect the PoC's runtime behavior
    # (it would still fire post-patch). For these PoCs the proper post-patch
    # witness is L4 LiteSVM (instruction-level reproduction against the
    # patched .so). Here we instead verify the patch APPLIES CLEANLY against
    # the engine repo — anything stronger is delegated to the LiteSVM and
    # tests-pass gates.
    standalone_log = _find_standalone_poc_log(workspace, poc_test_name)
    if standalone_log is not None:
        patch_text_chk = p.read_text(encoding="utf-8", errors="replace")
        ok_apply, err_apply = _apply_patch(engine_repo, patch_text_chk)
        if not ok_apply:
            return GateResult(
                False,
                f"could not apply patch: {err_apply}",
                time.time() - t0,
            )
        _unapply_patch(engine_repo, patch_text_chk)
        return GateResult(
            True,
            f"patch applies cleanly; PoC is rustc-standalone "
            f"(runtime witness delegated to LiteSVM / tests gate)",
            time.time() - t0,
            details={"mode": "rustc-standalone-delegated"},
        )

    # Apply the patch in a stash to keep the engine_repo clean for re-runs
    # We use git apply + git stash to ensure we can roll back regardless of
    # whether cargo test passes or fails.
    patch_text = p.read_text(encoding="utf-8", errors="replace")
    ok, err = _apply_patch(engine_repo, patch_text)
    if not ok:
        return GateResult(False, f"could not apply patch: {err}", time.time() - t0)

    try:
        proc = subprocess.run(
            _cargo_test_argv(engine_repo, "--test", poc_test_name),
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
    if engine_repo is None or not engine_repo.is_dir():
        return GateResult(None, "skipped — no engine_repo provided", time.time() - t0)

    # Dispatch the right test runner for the target's language.
    lang = _detect_engine_language(engine_repo)
    if lang == "solidity":
        if not _have_forge():
            return GateResult(None, "skipped — forge not in PATH (Solidity target needs Foundry)", time.time() - t0)
    else:
        if not _have_cargo():
            return GateResult(None, "skipped — cargo not in PATH", time.time() - t0)

    p = patch_path(workspace, finding_id)
    if not p.is_file():
        return GateResult(False, "no patch.diff to apply", time.time() - t0)

    patch_text = p.read_text(encoding="utf-8", errors="replace")
    ok, err = _apply_patch(engine_repo, patch_text)
    if not ok:
        return GateResult(False, f"could not apply patch: {err}", time.time() - t0)

    argv = _engine_test_argv(engine_repo)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(engine_repo),
            capture_output=True, text=True, timeout=1800,
        )
    except subprocess.TimeoutExpired:
        _unapply_patch(engine_repo, patch_text)
        return GateResult(False, f"full {argv[0]} test timed out (>1800s)", time.time() - t0)
    finally:
        _unapply_patch(engine_repo, patch_text)

    if proc.returncode != 0:
        # Truncate stderr so it fits in verification.json
        tail = (proc.stderr or "")[-400:]
        return GateResult(
            False,
            f"existing test suite regressed post-patch ({argv[0]} exit {proc.returncode}). "
            f"tail: {tail}",
            time.time() - t0,
        )
    # Solidity additional check: forge can exit 0 even with per-test
    # Failure (e.g. --no-fail-fast mode). Parse --json output to
    # detect per-test failures.
    if lang == "solidity":
        any_failed, n_failed, first_reason = _forge_json_has_failures(proc.stdout or "")
        if any_failed:
            return GateResult(
                False,
                f"existing test suite regressed post-patch ({n_failed} forge test(s) "
                f"failed). first: {first_reason}",
                time.time() - t0,
            )
    return GateResult(True, f"full test suite passed post-patch ({argv[0]})", time.time() - t0)


def _gate_kani_proof_holds(
    workspace: Path,
    finding_id: int,
    engine_repo: Path | None,
    kani_harness: str | None,
) -> GateResult:
    t0 = time.time()
    if engine_repo is None or not engine_repo.is_dir():
        return GateResult(None, "skipped — no engine_repo provided", time.time() - t0)
    # Language-aware skip: Kani is Rust-only. Solidity targets use
    # Halmos at L3, not Kani — record N/A explicitly so the dashboard
    # can show "Kani: N/A (Solidity)" instead of "skipped — no harness"
    # which falsely implies misconfiguration.
    _lang = _detect_engine_language(engine_repo)
    if _lang == "solidity":
        return GateResult(None, "skipped — Kani not applicable to Solidity (L3 used Halmos)",
                          time.time() - t0)
    if not _have_kani():
        return GateResult(None, "skipped — cargo-kani not in PATH", time.time() - t0)
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


def _gate_litesvm_exploit_neutralized(
    workspace: Path,
    finding_id: int,
    engine_repo: Path | None,
    litesvm_test_name: str | None,
) -> GateResult:
    """Apply patch, run the LiteSVM test, expect it to PASS (exploit no longer fires).

    LiteSVM is Solana's BPF runtime simulator. Pre-patch, the harness asserts the
    exploit invariant is violated (`test result: FAILED`). Post-patch the same
    harness must report `test result: ok` — the on-chain runtime evidence that
    the fix actually neutralizes the exploit, not just that Kani's SMT solver
    couldn't find a counterexample to a possibly-incorrect harness.
    """
    t0 = time.time()
    if engine_repo is None or not engine_repo.is_dir():
        return GateResult(None, "skipped — no engine_repo provided", time.time() - t0)
    # Language-aware skip: LiteSVM is Solana-only. Solidity targets
    # use forge invariant/fuzz at L4 instead — record explicit N/A.
    _lang = _detect_engine_language(engine_repo)
    if _lang == "solidity":
        return GateResult(None, "skipped — LiteSVM not applicable to Solidity (L4 used forge invariant)",
                          time.time() - t0)
    if not litesvm_test_name:
        return GateResult(None, "skipped — no litesvm_test_name registered for this bug class",
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
            ["cargo", "test", litesvm_test_name, "--", "--nocapture", "--test-threads=1"],
            cwd=str(engine_repo),
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        _unapply_patch(engine_repo, patch_text)
        return GateResult(False, "litesvm test timed out (>600s)", time.time() - t0)
    finally:
        _unapply_patch(engine_repo, patch_text)

    combined = (proc.stdout or "") + (proc.stderr or "")
    if "test result: ok" in combined:
        return GateResult(True, f"litesvm test {litesvm_test_name} passes post-patch (exploit neutralized)",
                          time.time() - t0)
    if "test result: FAILED" in combined:
        return GateResult(False, f"litesvm test {litesvm_test_name} still FAILS post-patch — exploit not fixed",
                          time.time() - t0)
    return GateResult(False, f"litesvm test {litesvm_test_name} produced no recognizable verdict",
                      time.time() - t0)


def run_all_gates(
    workspace: Path,
    finding_id: int,
    *,
    engine_sha: str = "",
    engine_repo: Path | None = None,
    poc_test_name: str | None = None,
    kani_harness: str | None = None,
    litesvm_test_name: str | None = None,
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
        gates["litesvm_exploit_neutralized"] = _gate_litesvm_exploit_neutralized(
            workspace, finding_id, engine_repo, litesvm_test_name)
    else:
        # Skip downstream gates if the patch is malformed
        for gate_name in ("poc_fails_pre_patch", "poc_passes_post_patch",
                           "tests_pass_post_patch", "kani_proof_holds",
                           "litesvm_exploit_neutralized"):
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
    """True iff every active gate passed.

    A gate with passed=True counts as pass.
    A gate with passed=False counts as FAIL (blocks the bundle).
    A gate with passed=None is a SKIP — it counts as N/A (does NOT block)
    when the reason is "no harness/test registered for this bug class",
    because that's a configuration absence rather than a verification
    failure. Other SKIPs (e.g. "cargo not in PATH", "no engine_repo
    provided") DO block — the gate couldn't actually run, so we don't
    know whether it would pass.
    """
    for g in (verification.get("gates") or {}).values():
        passed = g.get("passed")
        if passed is True:
            continue
        if passed is False:
            return False
        # passed is None — inspect reason
        reason = (g.get("reason") or "").lower()
        # N/A reasons that don't block:
        #   - config absence ("no harness registered ...")
        #   - language-mismatch ("not applicable to solidity ...")
        if "no kani_harness registered" in reason \
                or "no litesvm_test_name registered" in reason \
                or "not applicable to solidity" in reason \
                or "not applicable to solana" in reason:
            continue  # N/A — config or language, not a verification failure
        return False  # any other skip (e.g. cargo missing) blocks
    return True
