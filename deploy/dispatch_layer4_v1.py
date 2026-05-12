#!/usr/bin/env python3
"""Layer 4 (LiteSVM) dispatcher for Kani-confirmed bugs.

For each bug confirmed by Kani in Layer 3, author a LiteSVM test that
reproduces the bug through on-chain instruction handlers (the wrapper's
public BPF entrypoints), not direct engine calls. Run cargo test from the
wrapper crate.

Pattern: mirrors wrapper/tests/test_a1_siphon_regression.rs structure
(uses `mod common; use common::*;` from wrapper/tests/common/mod.rs).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CYCLE = Path("/root/audit_runs/percolator-live/hunts/20260511-183154")
HYP_DIR = Path("/root/audit-pipeline-cli/src/audit_pipeline/templates/hypotheses")
ENGINE_ROOT = Path("/root/audit_runs/percolator-live/target/engine")
WRAPPER_ROOT = Path("/root/audit_runs/percolator-live/target/wrapper")
WRAPPER_TESTS = WRAPPER_ROOT / "tests"
WRAPPER_COMMON = WRAPPER_TESTS / "common" / "mod.rs"
WRAPPER_REFERENCE_TEST = WRAPPER_TESTS / "test_a1_siphon_regression.rs"
POC_DIR = CYCLE / "poc"
KANI_DIR = CYCLE / "kani"
LITESVM_OUT = CYCLE / "litesvm"
LOG = CYCLE / "hunt.log.jsonl"

CONCURRENCY = 2
CARGO_TIMEOUT = 1800
MAX_FIX_ROUNDS = 3

# Kani-confirmed bugs from Layer 3 (21 confirmed + 2 proved-safe + 1 OOM = 24).
# Run L4 on the 21 confirmed bugs since those are the ones we want to prove
# reachable through on-chain instructions.
KANI_CONFIRMED = [
    "H1-residual-conservation",
    "H5-permissionless-trigger-surface",
    "PD4-residual-conservation",
    "PD9-haircut-direction-monotonic",
    "S3-settle-after-close",
    "V1-residual-conservation-strict",
    "V1-vault-residual-conservation",
    "V2-vault-balance-equation",
    "V5-haircut-direction",
    "V7-insurance-counter-vault-coupling",
    "SH6-resolve-flat-negative-gate",
    "AR7-saturating-arithmetic-correctness",
    "CI10-resolution-final",
    "T1-hyperp-mark-cpi-bundled-trade",
    "U20-resolvedcrank-early-return-skips-recurring-fees",
    "U21-permissionless-resolve-bypass-engine-init-check",
    "U29-hyperp-mark-push-zero-after-stale-allowed",
    "U30-deposit-fee-credits-zero-debt-after-sync-still-succeeds",
    "P2-oracle-account-binding",
    "V26-compute-trade-pnl-no-i128-min",
    # K12 + SH9 = Kani proved safe; X29 = Kani OOM. Skip from L4 dispatch.
]


def slug(h: str) -> str:
    return h.lower().replace("-", "_")


def short_litesvm_name(hyp_id: str) -> str:
    """Short name for the litesvm test file. Kani filename limit is ~150 chars
    once Kani's compiler mangles it; full slug + suffix can exceed that.
    Keep short."""
    s = slug(hyp_id)
    if len(s) > 50:
        # Truncate to first ~40 chars while still distinctive.
        s = s[:40].rstrip("_")
    return f"litesvm_{s}"


def append_log(event: dict) -> None:
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def load_hyp_meta(hyp_id: str) -> dict | None:
    import yaml
    for f in HYP_DIR.glob("*.yaml"):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        for _k, v in data.items():
            if not isinstance(v, list):
                continue
            for h in v:
                if isinstance(h, dict) and h.get("id") == hyp_id:
                    return h
    return None


def build_prompt(
    hyp_id: str, claim: str, engine_function: str,
    poc_text: str, kani_text: str, reference_test: str,
    common_excerpt: str, test_name: str,
) -> str:
    return f"""You are authoring a Layer-4 LiteSVM regression test for the
Jelleo audit pipeline. The L2 PoC test (engine-level direct call) and the
L3 Kani harness (symbolic) both confirm the bug exists in the engine. Your
job is to reproduce the bug through the WRAPPER's on-chain BPF instruction
handlers, using LiteSVM (in-process Solana VM).

# Critical instructions

- This test runs from `wrapper/tests/<test_name>.rs`. Use `mod common; use common::*;`
  to access the wrapper's TestEnv + helper utilities.
- DO NOT call engine functions directly (e.g., `engine.absorb_protocol_loss(...)`).
  You must use the on-chain INSTRUCTION HANDLERS (`TradeCpi`, `KeeperCrank`,
  `SettleFlatNegativePnl`, etc.) that the BPF program exposes.
- Use real Solana SDK keypairs, accounts, transactions.
- Sequence the instructions to drive the engine into the buggy state, then
  assert state changes (vault, insurance balance, etc.) prove the bug fires.
- The function name MUST be EXACTLY `{test_name}` (no `proof_` prefix).
- Use `#[test]` attribute (NOT `#[kani::proof]`). LiteSVM tests are concrete.
- NO `.unwrap()` / `.expect()` in setup is fine here — these are concrete tests,
  not symbolic. Use `.expect("setup")` style for test helpers.

# Hypothesis

- ID: `{hyp_id}`
- Engine function under test: `{engine_function}`
- Natural-language claim:

```
{claim}
```

# Layer 2 PoC (concrete witness — calls engine directly)

```rust
{poc_text}
```

# Layer 3 Kani harness (symbolic — same spec assertion)

```rust
{kani_text}
```

# REFERENCE PATTERN — wrapper/tests/test_a1_siphon_regression.rs

This is the canonical example of a LiteSVM regression test in the wrapper.
Mimic its structure: TestEnv + on-chain instructions + state assertions.

```rust
{reference_test}
```

# wrapper/tests/common/mod.rs (excerpt — TestEnv + key helpers)

```rust
{common_excerpt}
```

# Required test shape

```rust
mod common;
#[allow(unused_imports)]
use common::*;

use solana_sdk::signature::{{Keypair, Signer}};

#[test]
fn {test_name}() {{
    // 1. Build TestEnv (LiteSVM + program loaded + admin account).
    // 2. Initialize a market with realistic params.
    // 3. Construct the sequence of on-chain instructions that triggers the
    //    bug per the L2 PoC's behavior.
    // 4. Send transactions via env.svm.send_transaction(...).
    // 5. Read post-state from env.svm.get_account(...).
    // 6. Assert the bug fires (state change matches the PoC's panic condition).
}}
```

# Output format

Output ONLY the Rust test file content. Wrap in a single ```rust fence.
No explanatory prose before or after the fence.
"""


def author_test(hyp_id: str, test_name: str) -> tuple[str | None, dict]:
    meta = load_hyp_meta(hyp_id)
    if not meta:
        return None, {"err": "hyp not in any yaml"}
    claim = (meta.get("claim") or hyp_id).strip()
    engine_function = meta.get("engine_function") or "absorb_protocol_loss"

    poc_path = POC_DIR / f"test_{slug(hyp_id)}.rs"
    if not poc_path.is_file():
        return None, {"err": f"no L2 PoC at {poc_path.name}"}
    poc_text = poc_path.read_text(encoding="utf-8", errors="replace")

    kani_name = f"{slug(hyp_id)}_invariant"
    kani_path = ENGINE_ROOT / "tests" / f"{kani_name}.rs"
    kani_text = kani_path.read_text(encoding="utf-8", errors="replace") if kani_path.is_file() else "(L3 harness file missing — use the L2 PoC)"

    reference_test = WRAPPER_REFERENCE_TEST.read_text(encoding="utf-8", errors="replace")
    # FULL common/mod.rs — gives LLM visibility into ALL TestEnv helpers + instruction
    # encoders. Without this, LLM tries trades that engine rejects with Custom() codes
    # because it doesn't know about prerequisite setup helpers.
    common_excerpt = WRAPPER_COMMON.read_text(encoding="utf-8", errors="replace")

    prompt = build_prompt(
        hyp_id, claim, engine_function,
        poc_text, kani_text, reference_test, common_excerpt, test_name,
    )

    sys.path.insert(0, "/root/audit-pipeline-cli/src")
    from audit_pipeline.utils.llm import complete

    os.environ["JELLEO_SPEND_CALLER"] = f"layer4_v1/{hyp_id}/author"
    response = complete(prompt)

    m = re.search(r"```rust\n(.*?)\n```", response.text, re.DOTALL)
    if not m:
        return None, {"err": "no rust code in LLM response", "raw_tail": response.text[-200:]}
    return m.group(1), {
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cost_usd": response.cost_usd,
    }


def fix_test(hyp_id: str, test_name: str, prev_test: str, errors: str) -> tuple[str | None, dict]:
    meta = load_hyp_meta(hyp_id)
    claim = (meta.get("claim") or hyp_id).strip() if meta else hyp_id
    poc_path = POC_DIR / f"test_{slug(hyp_id)}.rs"
    poc_text = poc_path.read_text(encoding="utf-8", errors="replace") if poc_path.is_file() else ""
    # Full common/mod.rs in fix prompt too (same reasoning as initial prompt).
    common_excerpt = WRAPPER_COMMON.read_text(encoding="utf-8", errors="replace")

    prompt = f"""Your previous LiteSVM test for `{hyp_id}` did NOT compile.
Fix the errors and reproduce the FULL test.

# Compile errors

```
{errors[:3000]}
```

# Your previous test (FAILING)

```rust
{prev_test}
```

# Hypothesis claim (for context)

{claim}

# L2 PoC (engine-level reference for the bug behavior)

```rust
{poc_text}
```

# wrapper/tests/common/mod.rs (excerpt — use these types/helpers)

```rust
{common_excerpt}
```

# Hard rules (unchanged)

- Function name MUST be EXACTLY `{test_name}`
- `mod common; use common::*;` at top
- `#[test]` attribute (concrete, not Kani)
- Output ONLY the test file, wrapped in a single ```rust fence
"""
    sys.path.insert(0, "/root/audit-pipeline-cli/src")
    from audit_pipeline.utils.llm import complete

    os.environ["JELLEO_SPEND_CALLER"] = f"layer4_v1/{hyp_id}/fix"
    response = complete(prompt)
    m = re.search(r"```rust\n(.*?)\n```", response.text, re.DOTALL)
    if not m:
        return None, {"err": "no rust code in fix response", "raw_tail": response.text[-200:]}
    return m.group(1), {
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cost_usd": response.cost_usd,
    }


def cargo_test_run(test_name: str) -> tuple[bool, bool, str, str]:
    """Returns (compiles, fired, full_output, classifier_note).
       compiles = build succeeded
       fired = the spec assertion fired (bug reproduced through BPF)
       classifier_note = why we classified it that way (for logging)

    "Fired" detection must distinguish:
      - REAL FIRE: spec assertion in our test panicked (e.g., "BUG CONFIRMED:
        residual grew from 0 to X" message in output)
      - INFRASTRUCTURE FAILURE: setup-side panic (e.g., "BPF not found",
        "Run: cargo build-sbf", panic in tests/common/mod.rs)
    """
    try:
        proc = subprocess.run(
            ["cargo", "test", "--features", "test-sbf", "--test", test_name,
             "--", "--nocapture"],
            cwd=str(WRAPPER_ROOT),
            capture_output=True, text=True, timeout=CARGO_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, False, "cargo test timed out", "timeout"
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")

    looks_compile_failed = (
        "could not compile" in out
        or "error[E" in out.split("test result:")[0]
        or "did not match any tests" in out
    )
    if looks_compile_failed:
        return False, False, out, "compile_failed"

    # Filter out infrastructure failures BEFORE checking for "fired" markers.
    INFRA_FAIL_MARKERS = [
        "BPF not found",
        "Run: cargo build-sbf",
        "tests/common/mod.rs",   # setup helper panic
        "ProgramFailedToLoad",
        "could not load program",
    ]
    is_infra_fail = any(m in out for m in INFRA_FAIL_MARKERS)
    if is_infra_fail and "BUG CONFIRMED" not in out:
        # Setup blew up; spec assertion never reached.
        return True, False, out, "infra_fail (setup panicked, spec not reached)"

    # Now check for REAL fire signals — explicit BUG markers OR a panic
    # inside the test file (not common/mod.rs).
    if "BUG CONFIRMED" in out:
        return True, True, out, "BUG CONFIRMED text in output"
    # Look for panic location pointing at our test file specifically.
    test_file_panic = re.search(
        rf"panicked at tests/{re.escape(test_name)}\.rs", out
    )
    if test_file_panic:
        return True, True, out, f"panic at tests/{test_name}.rs (spec assertion)"
    # If test exit code != 0 but no infra-fail markers, treat as fire.
    if proc.returncode != 0 and not is_infra_fail:
        return True, True, out, "non-zero exit, no infra-fail markers"
    return True, False, out, "test passed (no spec violation)"


def dispatch_one(hyp_id: str) -> dict:
    test_name = short_litesvm_name(hyp_id)
    test_path = WRAPPER_TESTS / f"{test_name}.rs"

    LITESVM_OUT.mkdir(parents=True, exist_ok=True)
    cargo_log_path = LITESVM_OUT / f"cargo_{test_name}.log"

    # Resume: if cargo log already exists with a real result, skip
    if cargo_log_path.is_file() and cargo_log_path.stat().st_size > 1000:
        existing = cargo_log_path.read_text(encoding="utf-8", errors="replace")
        if "test result:" in existing or "could not compile" in existing:
            fired = ("test result: FAILED" in existing or "panicked at" in existing
                     or "BUG CONFIRMED" in existing)
            outcome = "litesvm_bug_reproduced" if fired else "litesvm_passed"
            append_log({"event": "litesvm_one", "hypothesis_id": hyp_id,
                        "outcome": outcome, "test_name": test_name,
                        "dispatcher": "layer4_v1", "resumed": True})
            return {"hyp_id": hyp_id, "outcome": outcome, "resumed": True}

    # Author
    append_log({"event": "litesvm_authored", "hypothesis_id": hyp_id,
                "test_name": test_name, "dispatcher": "layer4_v1",
                "phase": "author_round_1"})
    test_text, meta = author_test(hyp_id, test_name)
    if test_text is None:
        append_log({"event": "litesvm_one", "hypothesis_id": hyp_id,
                    "outcome": "author_failed", "detail": meta.get("err"),
                    "dispatcher": "layer4_v1"})
        return {"hyp_id": hyp_id, "outcome": "author_failed", "detail": meta.get("err")}

    test_path.write_text(test_text, encoding="utf-8")

    # Compile-fix loop via cargo test
    fix_round = 0
    classifier_note = ""
    while True:
        compiles, fired, full_out, classifier_note = cargo_test_run(test_name)
        if compiles:
            break
        fix_round += 1
        if fix_round > MAX_FIX_ROUNDS:
            cargo_log_path.write_text(full_out, encoding="utf-8")
            append_log({"event": "litesvm_one", "hypothesis_id": hyp_id,
                        "outcome": "compile_fail_after_fixes",
                        "fix_rounds": fix_round, "dispatcher": "layer4_v1"})
            return {"hyp_id": hyp_id, "outcome": "compile_fail",
                    "fix_rounds": fix_round}
        append_log({"event": "litesvm_authored", "hypothesis_id": hyp_id,
                    "test_name": test_name, "dispatcher": "layer4_v1",
                    "phase": f"fix_round_{fix_round}"})
        new_text, meta = fix_test(hyp_id, test_name, test_text, full_out)
        if new_text is None:
            append_log({"event": "litesvm_one", "hypothesis_id": hyp_id,
                        "outcome": "fix_author_failed",
                        "fix_rounds": fix_round, "detail": meta.get("err"),
                        "dispatcher": "layer4_v1"})
            return {"hyp_id": hyp_id, "outcome": "fix_author_failed"}
        test_text = new_text
        test_path.write_text(test_text, encoding="utf-8")

    # Compiled. If infra_fail (runtime panic in setup, spec not reached), retry
    # with LLM seeing the runtime error. Up to MAX_FIX_ROUNDS more rounds.
    runtime_round = 0
    while "infra_fail" in classifier_note:
        runtime_round += 1
        if runtime_round > MAX_FIX_ROUNDS:
            cargo_log_path.write_text(full_out, encoding="utf-8")
            append_log({"event": "litesvm_one", "hypothesis_id": hyp_id,
                        "outcome": "litesvm_infra_fail",
                        "fix_rounds": fix_round,
                        "runtime_rounds": runtime_round - 1,
                        "test_name": test_name,
                        "classifier_note": classifier_note,
                        "dispatcher": "layer4_v1"})
            return {"hyp_id": hyp_id, "outcome": "litesvm_infra_fail",
                    "classifier_note": classifier_note,
                    "runtime_rounds": runtime_round - 1}
        append_log({"event": "litesvm_authored", "hypothesis_id": hyp_id,
                    "test_name": test_name, "dispatcher": "layer4_v1",
                    "phase": f"runtime_fix_round_{runtime_round}"})
        new_text, meta = fix_test(hyp_id, test_name, test_text, full_out)
        if new_text is None:
            append_log({"event": "litesvm_one", "hypothesis_id": hyp_id,
                        "outcome": "fix_author_failed",
                        "fix_rounds": fix_round,
                        "runtime_rounds": runtime_round,
                        "detail": meta.get("err"), "dispatcher": "layer4_v1"})
            return {"hyp_id": hyp_id, "outcome": "fix_author_failed"}
        test_text = new_text
        test_path.write_text(test_text, encoding="utf-8")
        compiles, fired, full_out, classifier_note = cargo_test_run(test_name)
        if not compiles:
            # Compile broke during runtime-fix retry — bail
            cargo_log_path.write_text(full_out, encoding="utf-8")
            append_log({"event": "litesvm_one", "hypothesis_id": hyp_id,
                        "outcome": "compile_broke_during_runtime_fix",
                        "fix_rounds": fix_round,
                        "runtime_rounds": runtime_round,
                        "test_name": test_name, "dispatcher": "layer4_v1"})
            return {"hyp_id": hyp_id, "outcome": "compile_broke_during_runtime_fix"}

    cargo_log_path.write_text(full_out, encoding="utf-8")
    if fired:
        outcome = "litesvm_bug_reproduced"
    else:
        outcome = "litesvm_passed"
    append_log({"event": "litesvm_one", "hypothesis_id": hyp_id,
                "outcome": outcome, "fired": fired, "fix_rounds": fix_round,
                "runtime_rounds": runtime_round,
                "test_name": test_name, "classifier_note": classifier_note,
                "dispatcher": "layer4_v1"})
    return {"hyp_id": hyp_id, "outcome": outcome, "fired": fired,
            "fix_rounds": fix_round, "runtime_rounds": runtime_round,
            "classifier_note": classifier_note}


def _prepare_bpf_environment() -> None:
    """L3+L4 audit Defect 03 (HIGH) + Defect 10 (MED): two preflights.

    1. Rebuild the BPF .so so we're not testing yesterday's binary. The
       cycle's pinned engine_sha is meaningless if `target/deploy/*.so`
       is weeks old from a different feature set. Running `cargo
       build-sbf` once at dispatcher start guarantees the on-chain
       artefact matches the source we're claiming to test.
    2. Sweep stale L4 PoCs out of `wrapper/tests/litesvm_*.rs`. Yesterday
       437 stale tests accumulated; cargo's discovery step parses them
       all for every `--test <name>` invocation, exploding compile time
       and risking E0428 conflicts that mask the target's compile
       outcome.
    """
    import datetime as _dt
    print("[preflight] rebuilding BPF binary with cargo build-sbf ...", flush=True)
    try:
        rc = subprocess.run(
            ["cargo", "build-sbf"],
            cwd=str(WRAPPER_ROOT), capture_output=True, text=True, timeout=900,
        )
        if rc.returncode != 0:
            print(f"[preflight] cargo build-sbf FAILED (rc={rc.returncode}). "
                  "Continuing with whatever .so is on disk; expect infra "
                  f"failures. stderr tail: {(rc.stderr or '')[-400:]}",
                  flush=True)
        else:
            print(f"[preflight] cargo build-sbf OK", flush=True)
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError) as e:
        print(f"[preflight] cargo build-sbf unavailable: {e}", flush=True)

    # Sweep stale PoCs into an archive dir so they don't pollute cargo
    # discovery, but keep them retrievable if forensics needs them.
    archive_dir = WRAPPER_ROOT / "tests.archive" / _dt.datetime.utcnow().strftime("preflight-%Y%m%dT%H%M%SZ")
    archive_dir.mkdir(parents=True, exist_ok=True)
    n_archived = 0
    for stale in (WRAPPER_ROOT / "tests").glob("litesvm_*.rs"):
        # Keep the current cycle's PoCs in place; archive only the rest.
        if any(stale.stem.endswith(slug(h)[:40]) for h in KANI_CONFIRMED):
            continue
        try:
            stale.rename(archive_dir / stale.name)
            n_archived += 1
        except OSError:
            pass
    if n_archived:
        print(f"[preflight] archived {n_archived} stale litesvm_*.rs → {archive_dir}",
              flush=True)


def main() -> int:
    LITESVM_OUT.mkdir(parents=True, exist_ok=True)
    print(f"Layer 4 v1 LiteSVM dispatcher")
    print(f"Hyps: {len(KANI_CONFIRMED)} Kani-confirmed bugs")
    print(f"Concurrency: {CONCURRENCY}")
    print(f"Max fix rounds: {MAX_FIX_ROUNDS}")
    print(f"Per-hyp cargo timeout: {CARGO_TIMEOUT}s")
    print()
    _prepare_bpf_environment()
    print()

    reproduced: list[str] = []
    not_reproduced: list[str] = []
    failed: list[tuple[str, str]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(dispatch_one, h): h for h in KANI_CONFIRMED}
        for fut in as_completed(futs):
            done += 1
            hid = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                failed.append((hid, f"exception: {e}"))
                print(f"[{done}/{len(KANI_CONFIRMED)}] {hid} EXCEPTION: {e}", flush=True)
                continue
            outcome = res.get("outcome", "?")
            print(f"[{done}/{len(KANI_CONFIRMED)}] {hid} {outcome}", flush=True)
            if outcome == "litesvm_bug_reproduced":
                reproduced.append(hid)
            elif outcome == "litesvm_passed":
                not_reproduced.append(hid)
            else:
                failed.append((hid, outcome))

    print()
    print(f"DONE. reproduced_in_BPF={len(reproduced)} not_reproduced={len(not_reproduced)} failed={len(failed)}")
    print()
    if reproduced:
        print("BUG REPRODUCED IN BPF (via on-chain instructions):")
        for h in reproduced:
            print(f"  {h}")
    if not_reproduced:
        print("BUG NOT REPRODUCED IN BPF (wrapper-side defenses caught it OR test PASSES):")
        for h in not_reproduced:
            print(f"  {h}")
    if failed:
        print("FAILED / NEEDS REVIEW:")
        for h, why in failed:
            print(f"  {h}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
