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
import os
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


# Patch #3 round-1 fix (audit CRITICAL 41b26e30 / 7c0e4683 / 29db45bd):
# all_passed() previously did SUBSTRING matching on the free-text `reason`
# field to decide whether a skipped gate counted as N/A vs FAIL. A crafted
# verification.json could embed any of the allowlisted phrases in its
# reason and bypass the gate (`"skipped — no kani_harness registered for
# hypothesis-41"` matches `"no kani_harness registered"`). The defense is
# a STRUCTURED `skip_reason_code` enum field on GateResult: each call site
# emits a specific code, and all_passed() only accepts codes from a fixed
# allowlist. Free-text reasons are no longer security-relevant.
#
# The valid skip codes are listed in `_NA_SKIP_CODES` below. Every other
# skip — including ones with NO code at all (legacy verification.json from
# before this patch) — counts as a FAIL. That forces re-verify on legacy
# bundles, which is the correct behaviour: we can't trust an output that
# predates the structured-code guard.
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
    # Patch #3 (audit CRITICAL 41b26e30 etc): structured skip code that
    # `all_passed()` checks. Only required when `passed is None`. Allowed
    # values are enumerated in `_NA_SKIP_CODES`; any other code OR no code
    # at all is treated as BLOCK by `all_passed()`.
    skip_reason_code: str | None = None

    def to_json(self) -> dict:
        out = {
            "passed": self.passed,
            "reason": self.reason,
            "duration_s": round(self.duration_s, 3),
        }
        if self.details is not None:
            out["details"] = self.details
        if self.skip_reason_code is not None:
            out["skip_reason_code"] = self.skip_reason_code
        return out


# Patch #3 round-1 (audit CRITICAL 41b26e30): allowlist of structured skip
# codes that count as N/A (do NOT block authorization). Every other code,
# or a skip with NO code, BLOCKS. Update this set deliberately — adding a
# code here widens the bypass surface.
#
# Patch #3 round-2 (devils-advocate #7): removed "not_applicable_solana".
# Solana targets resolve to lang="rust" via _detect_engine_language()
# (they have Cargo.toml, no foundry.toml), so no gate ever emits this
# code. Dead allowlist entry — creates a phantom bypass channel for
# attackers crafting verification.json by hand, fixes nothing legitimate.
_NA_SKIP_CODES = frozenset({
    "no_kani_harness_registered",
    "no_litesvm_test_name_registered",
    "not_applicable_solidity",
    "not_applicable_c",
    "not_applicable_move",
})

# Patch #3 round-2 (threat-modeler #5): cross-gate validation table.
# Each skip code is only valid on a specific subset of gate names. An
# attacker who writes verification.json could otherwise apply
# "not_applicable_solidity" to a kani gate on a Rust target and bypass
# the kani check entirely. _gate_name_accepts_skip_code() enforces that
# (a) the code is in _NA_SKIP_CODES at all, AND (b) the code is one of
# the codes the gate's legitimate skip paths could ever emit.
_GATE_SKIP_CODE_ALLOWLIST: dict[str, frozenset[str]] = {
    "kani_proof_holds": frozenset({
        "no_kani_harness_registered",
        "not_applicable_solidity",
        "not_applicable_c",
        "not_applicable_move",
    }),
    "litesvm_exploit_neutralized": frozenset({
        "no_litesvm_test_name_registered",
        "not_applicable_solidity",
        "not_applicable_c",
        "not_applicable_move",
    }),
    # Patch #3 round-3 fix (devils-advocate #1 + threat-modeler #4):
    # tests_pass_post_patch DOES have a legitimate N/A skip for C
    # targets (no engine-level test runner; regression coverage
    # delegated to poc_passes_post_patch). Round-2's allowlist had
    # no entry for it, so the C skip — emitted with code
    # "not_applicable_c" — was rejected and every C-target bundle
    # blocked. Add the entry.
    "tests_pass_post_patch": frozenset({
        "not_applicable_c",
    }),
    # The remaining REQUIRED_GATES (patch_well_formed,
    # poc_fails_pre_patch, poc_passes_post_patch,
    # patch_unchanged_during_verify) have NO legitimate N/A skip —
    # they either pass or fail.
}


def _gate_name_accepts_skip_code(
    gate_name: str,
    code: str | None,
    engine_lang: str | None = None,
) -> bool:
    """True iff `code` is a legitimate N/A skip for the named gate.

    Defence against threat-model finding #5: a crafted verification.json
    that puts a Solidity-specific N/A code on a Rust-specific gate
    (or vice-versa) must not bypass authorization.

    Patch #3 round-4 fix (devils-advocate #2 + threat-modeler #4):
    when `engine_lang` is supplied (from verification.json's top-level
    `engine_lang` field), reject `not_applicable_<other_lang>` codes on
    a workspace whose detected lang is different. An attacker who
    crafts verification.json with `not_applicable_c` on tests_pass
    for a Rust target would otherwise bypass the regression gate.
    """
    if not isinstance(code, str) or code not in _NA_SKIP_CODES:
        return False
    allowed = _GATE_SKIP_CODE_ALLOWLIST.get(gate_name)
    if allowed is None:
        # Gate has no documented N/A allowlist — any skip code is invalid.
        return False
    if code not in allowed:
        return False
    # Language-binding cross-check (round-4): if the code names a
    # specific language and the verification.json recorded a different
    # language, REJECT. Codes that don't name a language (e.g.
    # no_kani_harness_registered) bypass this check.
    #
    # Patch #3 round-5 fix (devils-advocate #2 + threat-modeler #1):
    # the round-4 check exempted engine_lang IN ("unknown", None, "")
    # — these allowed ALL lang-specific codes through. An attacker
    # who forges verification.json need only set engine_lang="unknown"
    # (or omit it) and the lang guard evaporates. Worse: run_all_gates
    # legitimately writes "unknown" when engine_repo is None (CI
    # without JELLEO_ENGINE_REPO), so this attack pattern isn't even
    # suspicious. Round-5: lang-specific codes ONLY accepted when
    # engine_lang is a KNOWN matching string. Unknown/None/"" all
    # REJECT lang-specific codes (toolchain-absence codes like
    # no_kani_harness_registered remain accepted unconditionally).
    lang_specific_codes = {
        "not_applicable_solidity": "solidity",
        "not_applicable_c":        "c",
        "not_applicable_move":     "move",
    }
    required_lang = lang_specific_codes.get(code)
    if required_lang is not None:
        # Lang-specific code — only accept when engine_lang explicitly
        # matches the required language. Unknown/None/"" → REJECT.
        if not isinstance(engine_lang, str) or engine_lang != required_lang:
            return False
    return True


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
    tag used by the adapter modules: "rust" | "solidity" | "move" | "c" |
    "unknown".

    Used by the test-suite gate to dispatch the right runner (cargo /
    forge / aptos move test / clang+ASan). Order matters — Solidity
    repos often coexist with a Cargo.toml from a side tool, but
    foundry.toml is the strongest signal that the audit target is
    Solidity. C repos have no universal manifest, so we detect them
    via src/*.c files in the absence of every other marker.
    """
    if (engine_repo / "foundry.toml").is_file():
        return "solidity"
    if (engine_repo / "Move.toml").is_file():
        return "move"
    if (engine_repo / "Cargo.toml").is_file():
        return "rust"
    # C eval targets have no manifest — sniff src/ for *.c files.
    src_dir = engine_repo / "src"
    if src_dir.is_dir():
        try:
            for _ in src_dir.rglob("*.c"):
                return "c"
        except OSError:
            pass
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
    #
    # Patch #3 round-1 fix (audit CRITICAL 089f83c7 + HIGH 7dfddb64 / 4c1014cd):
    # the previous version only validated touched[0] — files [1..N] in a
    # multi-file patch (up to MAX_FILES_PER_BUNDLE=5) were never checked.
    # An LLM-authored patch with `+++ b/programs/foo/lib.rs` first and
    # `+++ b/../../etc/passwd` second would slip past. Now iterates ALL.
    for target in touched:
        if (
            target.startswith("/")
            or target.startswith("\\")
            or ".." in target.replace("\\", "/").split("/")
            or "\x00" in target
            # Drive-letter absolute paths on Windows (e.g. `C:/...`) —
            # `startswith("/")` doesn't catch them.
            or (len(target) >= 2 and target[1] == ":")
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
            "PoC at L2 had COMPILE error — bundle cannot be authorized "
            "without a fired PoC",
            time.time() - t0,
        )
    return GateResult(
        False,
        f"PoC at L2 did not fire (no forge test Failure in {runlog.name})",
        time.time() - t0,
    )


# Patch #3 round-2 fix (code-reviewer #8): module-level fire-marker
# regex shared between the C pre-patch (_gate_poc_fails_pre_patch_c)
# and C post-patch (_gate_poc_passes_post_patch_c) gates. Without
# sharing, the pre-patch path was line-anchored (round-1 fix) but
# post-patch kept the old loose `"FIRE:" in run_log` substring check —
# benign mentions of FIRE in compile output would false-positive the
# post-patch gate as "still fires", failing legitimate bundles.
_C_FIRE_MARKERS_RE = re.compile(
    r"(?m)^"  # multiline, anchor at start-of-line
    r"(?:"
    r"FIRE:\s"                                       # explicit FIRE: marker
    r"|==\d+==ERROR:\s*AddressSanitizer"             # ASan canonical prefix
    r"|==\d+==ERROR:\s*LeakSanitizer"                # LSan
    r"|==\d+==ERROR:\s*UndefinedBehaviorSanitizer"   # UBSan canonical
    r"|.+:\s*runtime error:"                         # UBSan inline ("file.c:42: runtime error:")
    r"|runtime error:"                               # bare UBSan (older clang fallback per devils-advocate #8)
    r"|.+:\d+: Assertion .+ failed"                  # glibc assert canonical
    r")",
)

# Compile-error detection: line-anchored `file.c:42:5: error: ...` or
# the canonical `compile error` phrase. Replaces the loose `"error:" in
# log_text` check that fired on every clang warning containing "error".
_C_COMPILE_ERR_RE = re.compile(
    r"(?m)^"
    r"(?:compile error"
    r"|.+:\d+:\d+:\s*error:\s)",
)


def _c_log_fired_canonical(log_text: str) -> str | None:
    """Return the matched fire-marker line if `log_text` contains a
    canonical fire marker, else None. Used by both C pre-patch and
    C post-patch gates for symmetric detection (code-reviewer #8)."""
    m = _C_FIRE_MARKERS_RE.search(log_text)
    if not m:
        return None
    line_start = log_text.rfind("\n", 0, m.start()) + 1
    line_end = log_text.find("\n", m.start())
    if line_end < 0:
        line_end = len(log_text)
    return log_text[line_start:line_end][:180]


def _gate_poc_fails_pre_patch_c(
    workspace: Path,
    finding_id: int,
    engine_repo: Path,
    poc_test_name: str,
) -> GateResult:
    """C variant: read the L2 clang+ASan/UBSan runlog to verify the PoC
    fired pre-patch. The L2 runlog at
    ``hunts/<cycle>/poc/runlog_<slug>.log`` already contains the
    sanitizer / `FIRE:` marker output from when L2 dispatched the PoC.
    If it contains an ASan/UBSan report, a `FIRE:` marker, or an
    assertion failure tied to the engine source, the bug is reproducible
    on the unpatched repo and this gate PASSES.
    """
    t0 = time.time()
    slug = poc_test_name[5:] if poc_test_name.startswith("test_") else poc_test_name
    poc_dir_candidates = list(workspace.glob("hunts/*/poc"))
    if not poc_dir_candidates:
        return GateResult(None, "skipped — no hunts/*/poc dir found",
                          time.time() - t0)
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
    # C fire markers: explicit FIRE:, ASan ERROR, UBSan runtime error,
    # or a stderr assertion-failed line. Any of these indicates the
    # PoC reached the bug site on the unpatched engine.
    #
    # Patch #3 round-1 fix (audit HIGH 877ec65c + MED 5cf6f6da + MED
    # b611d646 + MED 6ca3534c): the previous substring checks were
    # over-eager. New approach: line-anchored regex matches that require
    # the marker to be at the START of a line (or after a known sanitizer
    # prefix). Round-2: extracted to module-level _C_FIRE_MARKERS_RE so
    # the C post-patch gate shares the same detector (code-reviewer #8).
    first = _c_log_fired_canonical(log_text)
    if first is not None:
        return GateResult(
            True,
            f"PoC fired at L2 (clang+ASan/UBSan runlog): {first}",
            time.time() - t0,
            details={"outcome": "fired", "mode": "clang-l2-runlog"},
        )
    if _C_COMPILE_ERR_RE.search(log_text):
        return GateResult(
            False,
            "PoC at L2 had compile error — bundle cannot be authorized "
            "without a fired PoC",
            time.time() - t0,
        )
    return GateResult(
        False,
        f"PoC at L2 did not fire (no sanitizer / FIRE marker in {runlog.name})",
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
    # Language-aware dispatch: Solidity / C read the L2 runlog;
    # Rust/Solana re-runs cargo test on the standalone PoC.
    lang = _detect_engine_language(engine_repo)
    if lang == "solidity":
        return _gate_poc_fails_pre_patch_solidity(
            workspace, finding_id, engine_repo, poc_test_name,
        )
    if lang == "c":
        return _gate_poc_fails_pre_patch_c(
            workspace, finding_id, engine_repo, poc_test_name,
        )
    if not _have_cargo():
        return GateResult(None, "skipped — cargo not in PATH", time.time() - t0)
    # FIX B-#12: validate poc_test_name has the shape `test_<alnum_>`.
    # Without this, an empty / whitespace / wildcard value would expand
    # `cargo test --test <X>` into running ALL tests, any failure of which
    # would falsely confirm the PoC.
    # Patch #3 round-8 (threat-modeler round-7 #1 — sweep): use fullmatch
    # so trailing-newline bypass on $ is closed for this guard too.
    if not re.fullmatch(r"test_[A-Za-z0-9_]+", poc_test_name):
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
    #
    # Patch #3 round-1 fix (audit HIGH 5524ccc2): the `or ["src/percolator.rs"]`
    # fallback was a left-over from the Percolator-only days. If a malformed
    # patch with no `+++ b/` headers reached this point, we'd reset
    # src/percolator.rs (a path that may not even exist on the target repo)
    # and then `git apply --check` would fail anyway. Worse: on a repo that
    # DOES have src/percolator.rs, we'd silently revert a user's edits there.
    # Refuse the apply outright instead.
    files = files_touched(patch_text)
    if not files:
        return (False,
                "patch has no `+++ b/` headers — refusing to apply "
                "(malformed unified diff or empty patch)")
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
    range. Tolerates ±SLACK lines on BOTH sides to absorb whitespace
    fixups while bounding how far git's `--recount` may have re-anchored
    the patch.

    Patch #3 round-1 fix (audit CRITICAL 9f4321f5 + HIGH b8dad640 +
    MED a7e70994 + MED 7f87a63): the original SLACK=10 was a 20-line
    one-sided window (claim_lo=start with no slack, claim_hi=start+
    count+SLACK; actual_hi=a_start+a_count+SLACK). An attacker who
    crafted a patch whose `--recount` anchor drifted 10+ lines from
    the claimed location still passed. New version: SLACK=3 (≈ one
    whitespace fixup), symmetric on both bounds, and check claim_lo
    properly so upward drift is detected.
    """
    if not claimed_hunks:
        return (True, "no hunk anchors to verify")
    import re as _re
    by_file: dict[str, list[tuple[int, int]]] = {}
    files = sorted({f for f, _, _ in claimed_hunks})
    # Patch #3 round-2 fix (devils-advocate #9 + threat-modeler #10):
    # SLACK is per-side. The previous round-1 SLACK=3 became an effective
    # ±6 line total window after the symmetric interval-overlap check
    # (claim_lo = start - SLACK, actual_hi = a_start + a_count + SLACK,
    # so a 5-line drift still overlapped). Tightened to SLACK=1 — at most
    # ±2 total drift, enough to absorb whitespace-fixup off-by-one but
    # too tight for a real re-anchor attack to hide in.
    _SLACK_PER_SIDE = 1
    SLACK = _SLACK_PER_SIDE
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
        # Symmetric SLACK on both bounds (audit MED 7f87a63 + HIGH
        # b8dad640). claim_lo previously used `start` with no slack
        # which over-rejected downward drift; the upper bound only
        # had SLACK on one side so a patch anchored well below
        # claim_lo passed because actual_hi+SLACK still cleared
        # claim_lo. New rule: classic interval overlap on both
        # sides, ±SLACK on each.
        claim_lo = start - SLACK
        claim_hi = start + max(count, 1) + SLACK
        overlapping = False
        for a_start, a_count in actual_ranges:
            actual_lo = a_start - SLACK
            actual_hi = a_start + max(a_count, 1) + SLACK
            # Two intervals [lo1, hi1] and [lo2, hi2] overlap iff
            # lo1 <= hi2 AND lo2 <= hi1.
            if claim_lo <= actual_hi and actual_lo <= claim_hi:
                overlapping = True
                break
        if not overlapping:
            return (False,
                    f"file {f}: claimed @@ +{start},{count} @@ doesn't "
                    f"overlap any actual modification within SLACK={SLACK} "
                    f"lines (ranges: {actual_ranges})")
    return (True, f"all {len(claimed_hunks)} hunk anchors verified "
                  f"(SLACK={SLACK})")


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

    # Patch #3 round-1 fix (audit HIGH ffd4043e): the previous code had
    # `_unapply_patch(...)` in BOTH the `except TimeoutExpired:` clause
    # AND the `finally:` clause. On timeout, _unapply_patch fired twice;
    # the second invocation runs `git apply -R` against an already-reverted
    # tree (returns 1 with "patch does not apply") and our defensive code
    # then probes `git status --porcelain` and (depending on what else is
    # dirty) may misclassify the result. The fix is the standard pattern:
    # cleanup ONLY in the finally clause; the except clause just stores
    # the failure reason and re-raises into the finally path.
    # Patch #3 round-2 fix (code-reviewer #2): the inner except previously
    # only caught TimeoutExpired. If forge raised OSError/FileNotFoundError
    # (binary disappeared mid-PATH, or any other exec failure), it leaked
    # out of the try-finally and crashed run_all_gates instead of producing
    # a structured gate failure. Catch broadly inside; flag the failure
    # mode out to the post-block.
    timed_out = False
    crashed_err: str | None = None
    proc = None
    try:
        try:
            proc = subprocess.run(
                ["forge", "test", "--match-path",
                 str(deployed.relative_to(engine_repo)), "--json"],
                cwd=str(engine_repo),
                capture_output=True, text=True, timeout=600,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
        except (OSError, FileNotFoundError) as _e_fg:
            crashed_err = f"forge subprocess crashed: {_e_fg}"
    finally:
        deployed.unlink(missing_ok=True)
        _unapply_patch(engine_repo, patch_text)
    if timed_out:
        return GateResult(False, "forge test timed out post-patch (>600s)",
                          time.time() - t0)
    if crashed_err:
        return GateResult(False, crashed_err, time.time() - t0)
    if proc is None:
        return GateResult(False, "forge test produced no result (subprocess never ran)",
                          time.time() - t0)

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


def _gate_poc_passes_post_patch_c(
    workspace: Path,
    finding_id: int,
    engine_repo: Path,
    poc_test_name: str,
) -> GateResult:
    """C variant: apply the patch to the engine repo, recompile the L2
    PoC test (`tests/c/test_<slug>.c`) with clang + ASan + UBSan
    against the patched src/, run it, and assert the FIRE / sanitizer
    marker is GONE. If the patch eliminates the bug, the recompiled
    PoC returns 0 (or at minimum stops firing); if the bug still
    fires post-patch, this gate FAILS.
    """
    t0 = time.time()
    slug = poc_test_name[5:] if poc_test_name.startswith("test_") else poc_test_name

    # Locate the original PoC source under the workspace
    workspace_root_candidates = list(workspace.glob("tests/c"))
    if not workspace_root_candidates:
        return GateResult(None, "skipped — no tests/c dir under workspace",
                          time.time() - t0)
    test_src = workspace / "tests" / "c" / f"test_{slug}.c"
    if not test_src.is_file():
        return GateResult(None, f"skipped — PoC test source not found at {test_src}",
                          time.time() - t0)

    # Apply the patch.diff to the engine repo (stashed; we'll revert)
    p = patch_path(workspace, finding_id)
    if not p.is_file():
        return GateResult(False, "no patch.diff to apply", time.time() - t0)
    patch_text = p.read_text(encoding="utf-8", errors="replace")
    ok_apply, err_apply = _apply_patch(engine_repo, patch_text)
    if not ok_apply:
        return GateResult(False, f"could not apply patch: {err_apply}",
                          time.time() - t0)

    try:
        # Recompile the L2 PoC against the patched engine src/.
        # Skip main()-bearing src files (same rule as L2 adapter).
        include_dir = engine_repo / "src"
        _MAIN_RE = re.compile(r"^\s*(?:int|void|static\s+int)\s+main\s*\(", re.M)
        src_files: list[str] = []
        if include_dir.is_dir():
            for sp in include_dir.rglob("*.c"):
                if "tests" in sp.parts:  # vendor/ INCLUDED: c-medium has real vendored .c sources that engine sources call into
                    continue
                try:
                    if _MAIN_RE.search(sp.read_text(encoding="utf-8", errors="replace")):
                        continue
                except OSError:
                    pass
                src_files.append(str(sp))
        # The PoC may need pthread (CSMALL03 etc.) — link it unconditionally.
        bin_path = workspace / "tests" / "c" / f"bin_postpatch_{slug}"
        compile_cmd = [
            "clang", "-g", "-O0",
            "-fsanitize=address,undefined,signed-integer-overflow",
            "-fno-omit-frame-pointer", "-lpthread",
            f"-I{include_dir}",
            str(test_src), *src_files,
            "-o", str(bin_path),
        ]
        try:
            cp = subprocess.run(compile_cmd, capture_output=True, text=True, timeout=180)
        except FileNotFoundError:
            return GateResult(None, "skipped — clang not in PATH", time.time() - t0)
        except subprocess.TimeoutExpired:
            return GateResult(False, "clang compile timed out (>180s)",
                              time.time() - t0)
        if cp.returncode != 0:
            return GateResult(
                False,
                f"post-patch PoC failed to COMPILE — patch likely broke API "
                f"({cp.stderr[-200:]})",
                time.time() - t0,
            )
        # Run the recompiled PoC. Disable leak detection so a noisy
        # but benign minimap leak doesn't make us think the bug
        # persists.
        env = dict(os.environ)
        env["ASAN_OPTIONS"] = "detect_leaks=0"
        try:
            rp = subprocess.run([str(bin_path)], capture_output=True, text=True,
                                timeout=90, env=env)
        except subprocess.TimeoutExpired:
            return GateResult(False, "post-patch PoC run timed out (>90s)",
                              time.time() - t0)
        run_log = (rp.stderr or "") + (rp.stdout or "")
        # Patch #3 round-2 fix (code-reviewer #8): use the SAME line-anchored
        # marker regex as the pre-patch gate so a benign mention of "FIRE:"
        # in compile output doesn't falsely conclude the bug still fires.
        # Without this symmetry, a legitimate patch that fixes the bug
        # could fail the gate because some unrelated text triggers the
        # substring match. _c_log_fired_canonical handles both stdout and
        # stderr (we concat above).
        first = _c_log_fired_canonical(run_log)
        if first is not None:
            return GateResult(
                False,
                f"PoC STILL FIRES post-patch — patch does not fix the bug: {first}",
                time.time() - t0,
            )
        return GateResult(
            True,
            f"PoC stops firing post-patch (returncode={rp.returncode}); patch fixes the bug",
            time.time() - t0,
            details={"mode": "clang-recompile-rerun"},
        )
    finally:
        # Always revert the patch so the engine repo stays clean.
        _unapply_patch(engine_repo, patch_text)


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
    lang = _detect_engine_language(engine_repo)
    if lang == "solidity":
        return _gate_poc_passes_post_patch_solidity(
            workspace, finding_id, engine_repo, poc_test_name,
        )
    if lang == "c":
        return _gate_poc_passes_post_patch_c(
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
            "patch applies cleanly; PoC is rustc-standalone "
            "(runtime witness delegated to LiteSVM / tests gate)",
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

    # Patch #3 round-3 fix (code-reviewer #1 + devils-advocate #4): apply
    # the same nested-try + timed_out/crashed_err flag pattern that
    # round-2 applied to kani/litesvm/Solidity. Previously the Rust path
    # had _unapply_patch in BOTH the except clause AND finally — the
    # second invocation runs against an already-reverted tree and
    # depending on prior dirty state may misclassify. Also catch OSError
    # so cargo-missing-mid-run returns a structured failure not a crash.
    timed_out = False
    crashed_err: str | None = None
    proc = None
    try:
        try:
            proc = subprocess.run(
                _cargo_test_argv(engine_repo, "--test", poc_test_name),
                cwd=str(engine_repo),
                capture_output=True, text=True, timeout=600,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
        except (OSError, FileNotFoundError) as _e_rs:
            crashed_err = f"cargo subprocess crashed: {_e_rs}"
    finally:
        _unapply_patch(engine_repo, patch_text)

    if timed_out:
        return GateResult(False, "cargo test timed out post-patch (>600s)", time.time() - t0)
    if crashed_err:
        return GateResult(False, crashed_err, time.time() - t0)
    if proc is None:
        return GateResult(False, "cargo test produced no result (subprocess never ran)",
                          time.time() - t0)

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
    # Patch #3 round-3 fix (threat-modeler #5): indeterminate path now
    # returns passed=False (not None). passed=None with no code BLOCKS
    # via all_passed(), but the error operator sees was "failing gates"
    # which masks "outcome parser variance" — a toolchain-version issue
    # rather than an actual fix failure. Explicit False makes it clear.
    return GateResult(
        False,
        f"could not determine post-patch outcome of {poc_test_name} — "
        f"cargo output didn't match any recognised pattern. Likely "
        f"toolchain-version variance; please re-verify after inspection.",
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
    elif lang == "c":
        # C eval targets don't carry a unified test runner (no
        # `make test`, no `cargo test`); the per-bug PoC fire is the
        # authoritative correctness signal, already exercised by the
        # poc_passes_post_patch gate. Skip this gate cleanly so it
        # doesn't fail with cargo exit 101 ("no Cargo.toml").
        #
        # Patch #3 round-3 fix (devils-advocate #1 + threat-modeler #4):
        # this skip was previously emitted WITHOUT a skip_reason_code,
        # which made all_passed() block (no code → BLOCK). Result: every
        # C-target bundle was permanently un-authorizable, a hard DoS
        # on C-language audits. Add the code so the allowlist accepts.
        return GateResult(
            None,
            "skipped — C target has no engine-level test suite; "
            "regression coverage delegated to poc_passes_post_patch "
            "(clang+ASan rebuild of the L2 PoC against patched src)",
            time.time() - t0,
            skip_reason_code="not_applicable_c",
        )
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
    # Patch #3 round-3 fix (code-reviewer #1 + devils-advocate #4): same
    # nested-try + flag pattern as kani/litesvm/Rust poc_passes. Avoids
    # the double-unapply on timeout and catches OSError from missing
    # cargo/forge mid-run.
    timed_out = False
    crashed_err: str | None = None
    proc = None
    try:
        try:
            proc = subprocess.run(
                argv,
                cwd=str(engine_repo),
                capture_output=True, text=True, timeout=1800,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
        except (OSError, FileNotFoundError) as _e_ts:
            crashed_err = f"{argv[0]} subprocess crashed: {_e_ts}"
    finally:
        _unapply_patch(engine_repo, patch_text)

    if timed_out:
        return GateResult(False, f"full {argv[0]} test timed out (>1800s)", time.time() - t0)
    if crashed_err:
        return GateResult(False, crashed_err, time.time() - t0)
    if proc is None:
        return GateResult(False, f"{argv[0]} produced no result (subprocess never ran)",
                          time.time() - t0)

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
        return GateResult(None, "not applicable — Halmos is the Solidity L3 (verdict shown in Layer 3 section)",
                          time.time() - t0,
                          skip_reason_code="not_applicable_solidity")
    # Patch #3 round-3 fix (devils-advocate #7): C targets don't use
    # Kani — the L3 prover is the clang+ASan/UBSan PoC re-run. Without
    # this explicit branch, C engines would either skip with
    # "no cargo-kani in PATH" (no code → BLOCK by all_passed) or skip
    # with "no kani_harness registered" (legitimate code but operator
    # confusion). Emit a language-aware N/A with the correct code.
    if _lang == "c":
        return GateResult(None, "not applicable — clang+ASan/UBSan PoC re-run is the C L3 (verdict shown in Layer 3 section)",
                          time.time() - t0,
                          skip_reason_code="not_applicable_c")
    if _lang == "move":
        return GateResult(None, "not applicable — Move Prover is the L3 for Move targets",
                          time.time() - t0,
                          skip_reason_code="not_applicable_move")
    if not _have_kani():
        # Toolchain absence → no code → all_passed() will BLOCK. That's
        # correct: we can't claim the proof holds when the prover never ran.
        return GateResult(None, "skipped — cargo-kani not in PATH", time.time() - t0)
    if not kani_harness:
        return GateResult(None, "skipped — no kani_harness registered for this bug class",
                          time.time() - t0,
                          skip_reason_code="no_kani_harness_registered")
    # Patch #3 round-7 fix (threat-modeler round-6 #2): validate harness
    # name shape so a value like "  " (spaces) — which is truthy in
    # Python and passes the `not kani_harness` check — can't reach the
    # subprocess argv where it would either silently match no harness
    # OR run an attacker-chosen one. Same regex constraint applied to
    # poc_test_name at line ~729 (FIX B-#12).
    #
    # Patch #3 round-8 fix (threat-modeler round-7 #1): use re.fullmatch
    # instead of re.match. Python's `$` in non-MULTILINE mode matches
    # BEFORE a trailing `\n` as well as at true end-of-string, so
    # `re.match(r"^test_[A-Za-z0-9_]+$", "test_x\n")` returns a Match
    # despite the embedded newline. The newline-bearing value would
    # then reach the subprocess argv. fullmatch requires the WHOLE
    # string to match — no trailing-newline bypass.
    if not re.fullmatch(r"[A-Za-z0-9_:]+", kani_harness):
        return GateResult(
            False,
            f"kani_harness {kani_harness!r} doesn't match "
            f"^[A-Za-z0-9_:]+$ — refusing to run",
            time.time() - t0,
        )

    p = patch_path(workspace, finding_id)
    if not p.is_file():
        return GateResult(False, "no patch.diff to apply", time.time() - t0)

    patch_text = p.read_text(encoding="utf-8", errors="replace")
    ok, err = _apply_patch(engine_repo, patch_text)
    if not ok:
        return GateResult(False, f"could not apply patch: {err}", time.time() - t0)

    # Patch #3 round-2 fix (code-reviewer #1 + devils-advocate #1):
    # apply the same double-unapply fix that round-1 applied only to the
    # Solidity post-patch gate. Previously the `except TimeoutExpired:`
    # arm called _unapply_patch then `return` — but a `finally:` runs
    # BEFORE the return takes effect, so _unapply_patch fired twice and
    # the second invocation ran `git apply -R` on an already-reverted
    # tree (returns 1, leaving the gate's caller with a misleading
    # "patch state unclear" signal). Round-2: cleanup ONLY in finally.
    # Also catch OSError/FileNotFoundError (cargo crashed, kani went
    # missing) so the gate returns a structured failure rather than
    # propagating an unhandled exception out of run_all_gates.
    timed_out = False
    crashed_err: str | None = None
    proc = None
    try:
        try:
            proc = subprocess.run(
                ["cargo", "kani", "--harness", kani_harness],
                cwd=str(engine_repo),
                capture_output=True, text=True, timeout=1800,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
        except (OSError, FileNotFoundError) as _e_kani:
            crashed_err = f"kani subprocess crashed: {_e_kani}"
    finally:
        _unapply_patch(engine_repo, patch_text)

    if timed_out:
        return GateResult(False, "kani timed out (>1800s)", time.time() - t0)
    if crashed_err:
        return GateResult(False, crashed_err, time.time() - t0)
    if proc is None or proc.returncode != 0:
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
        return GateResult(None, "not applicable — forge fuzz / invariant is the Solidity L4 (verdict shown in Layer 4 section)",
                          time.time() - t0,
                          skip_reason_code="not_applicable_solidity")
    if _lang == "c":
        return GateResult(None, "not applicable — AFL is the C L4 (verdict shown in Layer 4 section)",
                          time.time() - t0,
                          skip_reason_code="not_applicable_c")
    if _lang == "move":
        return GateResult(None, "not applicable — Move Prover is the L3 for Move targets",
                          time.time() - t0,
                          skip_reason_code="not_applicable_move")
    if not litesvm_test_name:
        return GateResult(None, "skipped — no litesvm_test_name registered for this bug class",
                          time.time() - t0,
                          skip_reason_code="no_litesvm_test_name_registered")
    # Patch #3 round-7 fix (threat-modeler round-6 #1): validate test
    # name shape. Without this, a whitespace-only litesvm_test_name
    # would land in cargo's argv as a meaningless filter that matches
    # zero tests; cargo exits 0 with "test result: ok" (zero tests
    # ran); the gate accepts it as "exploit neutralized." Same FIX
    # B-#12 regex constraint applied to poc_test_name elsewhere.
    #
    # Patch #3 round-8 fix (threat-modeler round-7 #1): re.fullmatch
    # not re.match — closes the trailing-newline bypass where
    # `re.match(r"^test_[A-Za-z0-9_]+$", "test_x\n")` returns a Match
    # because $ matches before terminal \n in non-MULTILINE mode.
    if not re.fullmatch(r"test_[A-Za-z0-9_]+", litesvm_test_name):
        return GateResult(
            False,
            f"litesvm_test_name {litesvm_test_name!r} doesn't match "
            f"^test_[A-Za-z0-9_]+$ — refusing to run",
            time.time() - t0,
        )

    p = patch_path(workspace, finding_id)
    if not p.is_file():
        return GateResult(False, "no patch.diff to apply", time.time() - t0)

    patch_text = p.read_text(encoding="utf-8", errors="replace")
    ok, err = _apply_patch(engine_repo, patch_text)
    if not ok:
        return GateResult(False, f"could not apply patch: {err}", time.time() - t0)

    # Patch #3 round-2 fix (code-reviewer #1 + devils-advocate #1):
    # same double-unapply pattern as kani (above) and Solidity post-patch.
    timed_out = False
    crashed_err: str | None = None
    proc = None
    try:
        try:
            proc = subprocess.run(
                ["cargo", "test", litesvm_test_name, "--", "--nocapture", "--test-threads=1"],
                cwd=str(engine_repo),
                capture_output=True, text=True, timeout=600,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
        except (OSError, FileNotFoundError) as _e_lt:
            crashed_err = f"litesvm subprocess crashed: {_e_lt}"
    finally:
        _unapply_patch(engine_repo, patch_text)

    if timed_out:
        return GateResult(False, "litesvm test timed out (>600s)", time.time() - t0)
    if crashed_err:
        return GateResult(False, crashed_err, time.time() - t0)
    if proc is None:
        return GateResult(False, "litesvm test produced no result (subprocess never ran)",
                          time.time() - t0)
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

    # Patch #3 round-1 fix (audit HIGH 9bec7244): capture patch_sha BEFORE
    # any gate runs and re-check it AFTER all gates complete. Previously
    # the patch_sha line below ran AFTER the long-running gates (forge /
    # cargo / kani — minutes to half an hour), giving an attacker a wide
    # window to mutate patch.diff after the gates approved the OLD bytes
    # but BEFORE the new sha was bound into verification.json + the
    # operator's authorization phrase. With the lock + recheck, any
    # mutation during the gate run aborts the verification.
    p_path = patch_path(workspace, finding_id)
    patch_sha_locked = file_sha256(p_path)

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
        # Skip downstream gates if the patch is malformed.
        # NB: no skip_reason_code — patch_well_formed=False causes these
        # to be FAIL-blocking via all_passed() (passed=None + no code
        # = block). That's the intended semantic.
        for gate_name in ("poc_fails_pre_patch", "poc_passes_post_patch",
                           "tests_pass_post_patch", "kani_proof_holds",
                           "litesvm_exploit_neutralized"):
            gates[gate_name] = GateResult(
                None, "skipped — patch_well_formed failed", 0.0)

    # Race-recheck: if patch.diff changed during the gate run, refuse to
    # persist a verification result. The bytes we attested were not the
    # bytes that exist on disk now.
    patch_sha_post = file_sha256(p_path)
    if patch_sha_post != patch_sha_locked:
        # Inject a synthetic failing gate so all_passed() and the operator
        # UI both see this as a hard fail.
        gates["patch_unchanged_during_verify"] = GateResult(
            False,
            f"patch.diff was mutated during gate run: "
            f"start_sha={patch_sha_locked[:12]} end_sha={patch_sha_post[:12]}. "
            f"Verification aborted — re-run `bundle verify`.",
            0.0,
        )
    else:
        gates["patch_unchanged_during_verify"] = GateResult(
            True,
            f"patch.diff sha unchanged across gate run ({patch_sha_locked[:12]})",
            0.0,
        )

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

    # Patch #3 round-4 fix (devils-advocate #2 + threat-modeler #4):
    # record the engine's detected language alongside the gates output
    # so all_passed() can cross-check that any `not_applicable_<lang>`
    # skip codes are consistent with the language the gates ran for.
    # Without this, an attacker who controls verification.json could
    # apply `not_applicable_c` on tests_pass_post_patch for a Rust
    # target and bypass the regression gate.
    detected_lang = "unknown"
    if engine_repo is not None and engine_repo.is_dir():
        try:
            detected_lang = _detect_engine_language(engine_repo)
        except Exception:
            detected_lang = "unknown"

    out = {
        "finding_id": finding_id,
        "engine_sha": actual_engine_sha,
        "engine_sha_claimed": engine_sha if engine_sha != actual_engine_sha else None,
        # Use the LOCKED sha (= the bytes the gates ran on), not a re-read
        # at the bottom which could pick up a mid-write tampering.
        "patch_sha":  patch_sha_locked,
        "ran_at":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine_lang": detected_lang,
        # Patch #3 round-7 fix (threat-modeler round-6 #2): persist the
        # `effective_*` toolchain identifiers INTO verification.json. The
        # round-6 cross-check in write_authorization reads kani_harness
        # from meta.json — but meta.json can be mutated between verify
        # and review. By snapshotting the effective values into
        # verification.json (which the digest binds), the cross-check
        # becomes immune to post-verify meta.json mutation.
        "effective_kani_harness":      kani_harness,
        "effective_litesvm_test_name": litesvm_test_name,
        "gates":      {k: v.to_json() for k, v in gates.items()},
    }

    # Patch #3 round-1 fix (audit MED f8fe4ebf): atomic write. Previously
    # `write_text` could leave a half-written verification.json on crash
    # — operator review would see corrupt JSON and either error or worse,
    # an attacker could race a `write_text` mid-write tamper.
    #
    # Patch #3 round-6 fix (threat-modeler round-5 #2): use tempfile so
    # the tmp name is unpredictable — defeats pre-create-symlink-to-
    # exfiltrate attacks on the bundle dir.
    import os as _os_atomic
    import tempfile as _tempfile_verif
    v_path = verification_path(workspace, finding_id)
    v_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = _tempfile_verif.mkstemp(
        prefix=v_path.name + ".", suffix=".tmp", dir=str(v_path.parent),
    )
    try:
        with _os_atomic.fdopen(tmp_fd, "w", encoding="utf-8") as _vfh:
            _vfh.write(json.dumps(out, indent=2, sort_keys=True))
        _os_atomic.replace(tmp_name, str(v_path))
    except Exception:
        try:
            _os_atomic.unlink(tmp_name)
        except OSError:
            pass
        raise
    return out


def all_passed(verification: dict) -> bool:
    """True iff every active gate passed.

    A gate with passed=True counts as pass.
    A gate with passed=False counts as FAIL (blocks the bundle).
    A gate with passed=None is a SKIP — it counts as N/A (does NOT block)
    when its `skip_reason_code` field is in `_NA_SKIP_CODES`. Any other
    skip — including ones with NO code (legacy verification.json from
    before Patch #3 round-1) — BLOCKS authorization.

    Patch #3 round-1 fix (audit CRITICAL 41b26e30 / 7c0e4683 / 29db45bd):
    previously this matched the free-text `reason` field with `in`-substring
    against allowlisted phrases. A crafted verification.json could include
    any of those phrases verbatim (`"skipped — no kani_harness registered
    for hypothesis 41"` matches `"no kani_harness registered"`) and bypass
    the gate. The fix is structured codes only — reasons are now untrusted
    text used purely for operator UI.
    """
    # Patch #3 round-2 fix (threat-modeler #5): iterate over (name, gate)
    # pairs and use the cross-gate code-allowlist check so an attacker
    # can't apply a Solidity N/A code to a Rust kani gate.
    #
    # Patch #3 round-4 fix (devils-advocate #2 + threat-modeler #4):
    # also pull `engine_lang` from the top-level verification.json so
    # the allowlist check rejects mismatched language codes. An attacker
    # who forges verification.json could otherwise apply
    # "not_applicable_c" on tests_pass_post_patch for a Rust target
    # and bypass the regression gate. With this, the lang field in
    # verification.json is cross-checked against the code.
    engine_lang = verification.get("engine_lang")
    for name, g in (verification.get("gates") or {}).items():
        passed = g.get("passed")
        if passed is True:
            continue
        if passed is False:
            return False
        # passed is None — require a structured skip_reason_code that is
        # (a) in _NA_SKIP_CODES at all, AND (b) legitimately emittable by
        # this specific gate, AND (c) consistent with the recorded engine
        # language. No code, wrong code, cross-gate code, or wrong-lang
        # code → BLOCK.
        code = g.get("skip_reason_code")
        if _gate_name_accepts_skip_code(name, code, engine_lang=engine_lang):
            continue  # N/A — config or language, not a verification failure
        return False  # any other skip (or no code, or wrong gate) blocks
    return True
