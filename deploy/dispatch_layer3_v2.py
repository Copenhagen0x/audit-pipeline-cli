#!/usr/bin/env python3
"""Layer 3 (Kani) dispatcher v2 — POC-LIFTING APPROACH.

Lifts each STRONG fire's existing L2 PoC test (which already has the
correct assertion that fires the bug) into a Kani symbolic harness.
The LLM only needs to do mechanical symbolic-ification — it does NOT
re-derive the spec invariant from natural language. This avoids the
v1 failure mode where the LLM authored harnesses asserting the engine's
buggy behavior instead of the spec.

For each STRONG hyp:
  1. Read poc/test_<slug>.rs (the L2 PoC that fires the bug)
  2. Read engine source (for struct fields)
  3. LLM authors harness: same assertion, concrete inputs → kani::any() + bounded kani::assume
  4. Verify the authored harness's assertion matches the PoC's panic message
  5. cargo kani --default-unwind 128 --harness <name>
  6. Parse verdict from output + iteration log

Concurrency = 2 (kani is RAM-heavy).
NO retry loop on cargo check — uses cargo kani --only-codegen for kani-aware
compile validation, with up to 3 fix rounds via direct LLM follow-up.
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

import yaml

CYCLE = Path("/root/audit_runs/percolator-live/hunts/20260511-183154")
HYP_DIR = Path("/root/audit-pipeline-cli/src/audit_pipeline/templates/hypotheses")
ENGINE_ROOT = Path("/root/audit_runs/percolator-live/target/engine")
ENGINE_SRC = ENGINE_ROOT / "src" / "percolator.rs"
ENGINE_TESTS = ENGINE_ROOT / "tests"
POC_DIR = CYCLE / "poc"
KANI_OUT = CYCLE / "kani"
LOG = CYCLE / "hunt.log.jsonl"

CONCURRENCY = 2
KANI_TIMEOUT = 86400  # 24h — user explicitly said "do not kill, give Kani as much time as it needs"
MAX_FIX_ROUNDS = 3

STRONG_HYPS = [
    # F7 family (11)
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
    # Net-new (13)
    "AR7-saturating-arithmetic-correctness",
    "CI10-resolution-final",
    # L3-keeper-crank-cursor-budget — TEMPORARILY EXCLUDED: orphaned cbmc
    # process from previous dispatcher run is still verifying it. Re-include
    # after orphan finishes naturally OR run manually.
    "T1-hyperp-mark-cpi-bundled-trade",
    "U20-resolvedcrank-early-return-skips-recurring-fees",
    "U21-permissionless-resolve-bypass-engine-init-check",
    "U29-hyperp-mark-push-zero-after-stale-allowed",
    "U30-deposit-fee-credits-zero-debt-after-sync-still-succeeds",
    "K12-cu-tier-test-loophole-tier-promotion-dos",
    "P2-oracle-account-binding",
    "SH9-stuck-target-accrual-rejection",
    "X29-keepercrank-rr-cursor-double-scan",
    "V26-compute-trade-pnl-no-i128-min",
]


def slug(h: str) -> str:
    return h.lower().replace("-", "_")


def append_log(event: dict) -> None:
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def load_hyp_meta(hyp_id: str) -> dict | None:
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


def build_lifting_prompt(
    hyp_id: str, claim: str, engine_function: str,
    poc_text: str, engine_src_text: str, harness_name: str,
) -> str:
    return f"""You are lifting a Layer-2 concrete PoC test into a Layer-3 Kani
symbolic harness for the Jelleo audit pipeline.

# THE EXACT PATTERN YOU MUST FOLLOW

This is a working Kani harness for the F7 residual-conservation bug. It uses
small-model RiskParams (max_accounts: 4), tight symbolic bounds (vault <= 1M),
direct field assignment (no panic-prone setup helpers), unwind(8), and the
shared `common` module. **Your harness MUST follow this exact structure** —
any deviation (large bounds, .unwrap(), going through top_up_insurance_fund
when direct field set works, etc.) causes Kani to either fail to reach the
spec assertion OR panic in setup code instead of the spec.

```rust
//! Layer-3 Kani harness for <HYP_ID>.

#![cfg(kani)]

mod common;
use common::*;

fn h1_params() -> RiskParams {{
    RiskParams {{
        maintenance_margin_bps: 500,
        initial_margin_bps: 1000,
        max_trading_fee_bps: 0,
        max_accounts: 4,             // SMALL MODEL — required for tractability
        liquidation_fee_bps: 0,
        liquidation_fee_cap: U128::ZERO,
        min_liquidation_abs: U128::ZERO,
        min_nonzero_mm_req: 5,
        min_nonzero_im_req: 6,
        h_min: 1,
        h_max: 10,                   // small (10 not 100)
        resolve_price_deviation_bps: 1000,
        max_accrual_dt_slots: 100,
        max_abs_funding_e9_per_slot: 10_000,
        min_funding_lifetime_slots: 10_000_000,
        max_active_positions_per_side: 4,  // matches max_accounts
        max_price_move_bps_per_slot: 4,
    }}
}}

#[kani::proof]
#[kani::unwind(8)]                    // small (8 not 128)
#[kani::solver(cadical)]
fn h1_residual_conservation_invariant() {{
    let mut engine = RiskEngine::new(h1_params());

    // SMALL symbolic bounds. <= 1_000_000 NOT MAX_VAULT_TVL.
    let vault_init: u128 = kani::any();
    kani::assume(vault_init <= 1_000_000);
    let ins_init: u128 = kani::any();
    kani::assume(ins_init <= vault_init);

    // DIRECT FIELD ASSIGNMENT — bypasses panic-prone helpers like
    // top_up_insurance_fund. The engine's existing proofs_*.rs harnesses
    // do this same pattern.
    engine.vault = U128::new(vault_init);
    engine.insurance_fund.balance = U128::new(ins_init);

    let loss: u128 = kani::any();
    kani::assume(loss > 0);
    kani::assume(loss <= ins_init);  // bug only fires when ins can absorb

    let residual_before: i128 =
        engine.vault.get() as i128
        - engine.c_tot.get() as i128
        - engine.insurance_fund.balance.get() as i128;

    kani::cover!(true, "reached_spec_assertion");

    engine.absorb_protocol_loss(loss);

    let residual_after: i128 =
        engine.vault.get() as i128
        - engine.c_tot.get() as i128
        - engine.insurance_fund.balance.get() as i128;

    // SPEC INVARIANT (lifted from L2 PoC). Failure = bug confirmed.
    assert_eq!(residual_before, residual_after, "residual conservation");
}}
```

# YOUR JOB

Adapt the pattern above for the new hypothesis. Specifically:
1. Rename `h1_params` → `<HYP_SLUG>_params`
2. Change function name to EXACTLY `{harness_name}` (no `proof_` prefix)
3. Replace the `engine.absorb_protocol_loss(loss)` call with the call
   under test from the PoC's `#[test] fn ..._fires()` body.
4. Replace the spec assertion's expression with the LOGICAL EQUIVALENT of
   the PoC's final `assert!`/`assert_eq!`. Keep the assertion message
   SHORT and STATIC (no format args — Kani can't render runtime formats).
5. Keep the small-model RiskParams + small bounds + direct field assignment.
6. NO `.unwrap()` or `.expect()` — use `kani::assume(result.is_ok())`.

# Hard rules

- Function name MUST be EXACTLY `{harness_name}`
- No `.unwrap()` / `.expect()` anywhere
- max_accounts MUST be 4 (small-model required for Kani tractability)
- All symbolic u128 bounds MUST be `<= 1_000_000` (no MAX_VAULT_TVL)
- Direct field assignment for state injection (NOT helper methods)
- Use `mod common; use common::*;` at the top
- Assertion message MUST be a static string literal (no `format!` args)
- Add `kani::cover!(true, "reached_spec_assertion");` before the final assert
  to confirm Kani reaches it (the cover check shows up in Kani output)
- ONLY use PUBLIC engine APIs

# Hypothesis

- ID: `{hyp_id}`
- Engine function under test: `{engine_function}`
- Natural-language claim:

```
{claim}
```

# The L2 PoC (this assertion fires on the engine — that's our ground truth)

```rust
{poc_text}
```

# Engine source (authoritative struct fields + helper signatures)

```rust
{engine_src_text}
```

# Required harness shape

```rust
#![cfg(kani)]

use percolator::*;

#[kani::proof]
#[kani::unwind(128)]
#[kani::solver(cadical)]
fn {harness_name}() {{
    // Symbolic versions of every concrete input the PoC used.
    // Use kani::any() for each, with kani::assume bounds matching the PoC's
    // realistic ranges (typically <= MAX_VAULT_TVL / 4 to avoid overflow).

    // Build engine identically to PoC (use the SAME params_for_*() factory or
    // inline the same RiskParams struct).

    // Inject pre-state identically to PoC (same top_up_insurance_fund / deposit
    // calls, but with symbolic amounts).

    // Snapshot pre-state.

    // Call the function under test (same line as PoC).

    // Snapshot post-state.

    // **Copy the PoC's assertion VERBATIM** (this is the spec invariant).
}}
```

# Output format

Output ONLY the Rust harness file content. Wrap in a single ```rust fence.
No explanatory prose before or after the fence.
"""


def author_harness(hyp_id: str, harness_name: str) -> tuple[str | None, dict]:
    """Returns (harness_text or None, metadata)."""
    meta_yaml = load_hyp_meta(hyp_id)
    if not meta_yaml:
        return None, {"err": "hyp not in any yaml"}
    claim = (meta_yaml.get("claim") or hyp_id).strip()
    engine_function = meta_yaml.get("engine_function") or "absorb_protocol_loss"

    poc_path = POC_DIR / f"test_{slug(hyp_id)}.rs"
    if not poc_path.is_file():
        return None, {"err": f"no L2 PoC at {poc_path.name}"}
    poc_text = poc_path.read_text(encoding="utf-8", errors="replace")

    if not ENGINE_SRC.is_file():
        return None, {"err": f"no engine source at {ENGINE_SRC}"}
    engine_src_text = ENGINE_SRC.read_text(encoding="utf-8", errors="replace")

    prompt = build_lifting_prompt(
        hyp_id, claim, engine_function, poc_text, engine_src_text, harness_name,
    )

    # Direct LLM call via audit_pipeline.utils.llm
    sys.path.insert(0, "/root/audit-pipeline-cli/src")
    from audit_pipeline.utils.llm import complete

    os.environ["JELLEO_SPEND_CALLER"] = f"layer3_v2/{hyp_id}/author"
    response = complete(prompt)

    # Extract Rust code block
    m = re.search(r"```rust\n(.*?)\n```", response.text, re.DOTALL)
    if not m:
        return None, {"err": "no rust code block in LLM response", "raw_tail": response.text[-200:]}
    return m.group(1), {
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cost_usd": response.cost_usd,
    }


def fix_harness(hyp_id: str, harness_name: str, prev_harness: str, errors: str) -> tuple[str | None, dict]:
    """Ask LLM to fix compile errors. Returns (new_harness_text or None, metadata)."""
    meta_yaml = load_hyp_meta(hyp_id)
    if not meta_yaml:
        return None, {"err": "hyp meta missing"}
    claim = (meta_yaml.get("claim") or hyp_id).strip()
    engine_function = meta_yaml.get("engine_function") or "absorb_protocol_loss"

    poc_path = POC_DIR / f"test_{slug(hyp_id)}.rs"
    poc_text = poc_path.read_text(encoding="utf-8", errors="replace") if poc_path.is_file() else ""
    engine_src_text = ENGINE_SRC.read_text(encoding="utf-8", errors="replace")

    prompt = f"""Your previous Kani harness for `{hyp_id}` did NOT compile.
Fix the errors and reproduce the FULL harness.

# Compile errors (from cargo kani --only-codegen)

```
{errors[:3000]}
```

# Your previous harness (FAILING)

```rust
{prev_harness}
```

# REMINDER: assertion semantics

The L2 PoC's assertion below fires on the engine, proving the bug exists.
Keep that assertion's LOGIC (same comparison) — only fix compile issues.

# L2 PoC

```rust
{poc_text}
```

# Engine source (use it for correct field names and helper signatures)

```rust
{engine_src_text}
```

# Constraints (unchanged from initial prompt)

- Function name MUST be EXACTLY `{harness_name}`
- Only use PUBLIC engine APIs (no `assert_public_postconditions` etc.)
- `#[kani::proof]` + `#[kani::unwind(128)]` + `#[kani::solver(cadical)]`
- `#![cfg(kani)]` at top of file
- Output ONLY the harness, wrapped in a single ```rust fence
"""
    sys.path.insert(0, "/root/audit-pipeline-cli/src")
    from audit_pipeline.utils.llm import complete

    os.environ["JELLEO_SPEND_CALLER"] = f"layer3_v2/{hyp_id}/fix"
    response = complete(prompt)
    m = re.search(r"```rust\n(.*?)\n```", response.text, re.DOTALL)
    if not m:
        return None, {"err": "no rust code in fix response", "raw_tail": response.text[-200:]}
    return m.group(1), {
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cost_usd": response.cost_usd,
    }


def cargo_kani_codegen_check(harness_name: str) -> tuple[bool, str]:
    """Returns (ok, output_tail). Uses cargo kani --only-codegen which is
    kani-aware (activates --cfg kani + injects kani crate)."""
    try:
        proc = subprocess.run(
            ["cargo", "kani", "--only-codegen", "--tests", "--features", "test",
             "--harness", harness_name, "--default-unwind", "128"],
            cwd=str(ENGINE_ROOT),
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return False, "cargo kani --only-codegen timed out (600s)"
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return proc.returncode == 0, out[-3000:]


def cargo_kani_run(harness_name: str) -> tuple[str, str]:
    """Run actual verification. Returns (verdict, full_output).
    verdict ∈ {SUCCESSFUL, FAILED_SPEC, FAILED_SETUP, TIMEOUT, ERROR}.

    FAILED_SPEC vs FAILED_SETUP:
      - FAILED_SPEC: the failed check's location is in tests/<harness>.rs
        (i.e., our spec assertion fired — real bug confirmation)
      - FAILED_SETUP: the failed check is in src/percolator.rs or std
        (i.e., engine internal panic or harness setup unwrap — false positive)
    """
    try:
        proc = subprocess.run(
            ["cargo", "kani", "--tests", "--features", "test",
             "--harness", harness_name, "--default-unwind", "128"],
            cwd=str(ENGINE_ROOT),
            capture_output=True, text=True, timeout=KANI_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT", "cargo kani exceeded timeout"
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if "VERIFICATION:- SUCCESSFUL" in out or "Verification:- SUCCESSFUL" in out:
        return "SUCCESSFUL", out
    if not ("VERIFICATION:- FAILED" in out or "Verification:- FAILED" in out):
        return "ERROR", out

    # Parse the failed-check section to determine if it's spec or setup.
    # Format Kani emits per-check:
    #   Check N: <name>
    #     - Status: FAILURE
    #     - Description: "..."
    #     - Location: <path>:<line>:<col>
    failed_locations = []
    blocks = re.split(r"^Check \d+: ", out, flags=re.MULTILINE)
    for block in blocks[1:]:
        if "Status: FAILURE" not in block:
            continue
        loc_m = re.search(r"Location:\s*(\S+?\.rs):", block)
        failed_locations.append(loc_m.group(1) if loc_m else "(unknown)")

    if not failed_locations:
        return "FAILED_SETUP", out  # no parseable location = treat as setup
    # If ANY failed check is in our harness file, that's the spec violation.
    harness_in_loc = any(harness_name in loc for loc in failed_locations)
    if harness_in_loc:
        return "FAILED_SPEC", out
    # All failures are in engine src or std lib — internal panic, not spec
    return "FAILED_SETUP", out


def assertion_intent_check(harness_text: str, poc_text: str) -> str:
    """Cheap heuristic: does the harness's last `assert*!` reference any of
    the same fields/identifiers the PoC's panic-causing assert references?
    Catches the wrong-direction-spec bug from v1.
    Returns 'ok' or a warning string."""
    poc_asserts = re.findall(r"assert(?:_eq|_ne)?!\s*\([^;]+\)", poc_text)
    harness_asserts = re.findall(r"assert(?:_eq|_ne)?!\s*\([^;]+\)", harness_text)
    if not harness_asserts:
        return "WARN: harness has no assert!() at all"
    if not poc_asserts:
        return "ok (poc has no assert; nothing to compare against)"
    poc_idents = set(re.findall(r"\b(post_\w+|pre_\w+|residual\w*|insurance\w*|vault\w*)\b",
                                " ".join(poc_asserts)))
    harness_idents = set(re.findall(r"\b(post_\w+|pre_\w+|residual\w*|insurance\w*|vault\w*)\b",
                                    " ".join(harness_asserts)))
    if poc_idents and not (poc_idents & harness_idents):
        return f"WARN: harness asserts on different identifiers than PoC. PoC uses {poc_idents}, harness uses {harness_idents}"
    return "ok"


def dispatch_one(hyp_id: str) -> dict:
    harness_name = f"{slug(hyp_id)}_invariant"
    harness_path = ENGINE_TESTS / f"{harness_name}.rs"

    # RESUME logic: if cargo_kani_<harness>.log exists with a real verdict
    # (SUCCESSFUL or FAILED), skip and reload the result. This lets us
    # restart the dispatcher without redoing the 17 hyps already done.
    log_path = KANI_OUT / f"cargo_kani_{harness_name}.log"
    if log_path.is_file() and log_path.stat().st_size > 1000:
        try:
            existing = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
        if "VERIFICATION:- SUCCESSFUL" in existing or "Verification:- SUCCESSFUL" in existing:
            outcome = "kani_invariant_proven_safe"
            verdict = "SUCCESSFUL"
        elif "VERIFICATION:- FAILED" in existing or "Verification:- FAILED" in existing:
            # Re-classify spec vs setup using the existing parser logic
            failed_locations = []
            blocks = re.split(r"^Check \d+: ", existing, flags=re.MULTILINE)
            for block in blocks[1:]:
                if "Status: FAILURE" not in block:
                    continue
                loc_m = re.search(r"Location:\s*(\S+?\.rs):", block)
                failed_locations.append(loc_m.group(1) if loc_m else "(unknown)")
            harness_in_loc = any(harness_name in loc for loc in failed_locations)
            verdict = "FAILED_SPEC" if harness_in_loc else "FAILED_SETUP"
            outcome = "kani_bug_confirmed" if harness_in_loc else "kani_setup_panic"
        else:
            verdict = None
            outcome = None
        if outcome:
            append_log({
                "event": "kani_one", "hypothesis_id": hyp_id,
                "outcome": outcome, "kani_verdict": verdict,
                "harness_name": harness_name, "dispatcher": "layer3_v2",
                "resumed": True,
            })
            return {"hyp_id": hyp_id, "outcome": outcome, "verdict": verdict,
                    "fix_rounds": 0, "intent": "(resumed)"}

    # Round 1: author
    append_log({
        "event": "kani_authored", "hypothesis_id": hyp_id,
        "harness_name": harness_name, "dispatcher": "layer3_v2",
        "phase": "author_round_1",
    })
    harness_text, meta = author_harness(hyp_id, harness_name)
    if harness_text is None:
        append_log({"event": "kani_one", "hypothesis_id": hyp_id,
                    "outcome": "author_failed", "detail": meta.get("err"),
                    "dispatcher": "layer3_v2"})
        return {"hyp_id": hyp_id, "outcome": "author_failed", "detail": meta.get("err")}

    intent = assertion_intent_check(harness_text, (POC_DIR / f"test_{slug(hyp_id)}.rs").read_text() if (POC_DIR / f"test_{slug(hyp_id)}.rs").is_file() else "")

    # L3+L4 audit Defect 04 (HIGH): per-function `#[kani::unwind(N)]`
    # attributes OVERRIDE `--default-unwind`. The template ships
    # `#[kani::unwind(8)]` which forced Kani to return SUCCESSFUL
    # trivially under bounds too tight to actually find the bug. Strip
    # ALL `#[kani::unwind(...)]` attributes before writing so the
    # dispatcher's --default-unwind value actually wins.
    harness_text = re.sub(r"^\s*#\[kani::unwind\([^)]*\)\]\s*\n?", "", harness_text, flags=re.MULTILINE)

    harness_path.write_text(harness_text, encoding="utf-8")

    # Compile-fix loop
    fix_round = 0
    while True:
        ok, out = cargo_kani_codegen_check(harness_name)
        if ok:
            break
        fix_round += 1
        if fix_round > MAX_FIX_ROUNDS:
            append_log({"event": "kani_one", "hypothesis_id": hyp_id,
                        "outcome": "compile_fail_after_fixes",
                        "fix_rounds": fix_round, "stderr_tail": out[-500:],
                        "intent_check": intent, "dispatcher": "layer3_v2"})
            return {"hyp_id": hyp_id, "outcome": "compile_fail",
                    "fix_rounds": fix_round, "intent": intent}
        append_log({"event": "kani_authored", "hypothesis_id": hyp_id,
                    "harness_name": harness_name, "dispatcher": "layer3_v2",
                    "phase": f"fix_round_{fix_round}"})
        new_text, meta = fix_harness(hyp_id, harness_name, harness_text, out)
        if new_text is None:
            append_log({"event": "kani_one", "hypothesis_id": hyp_id,
                        "outcome": "fix_author_failed",
                        "fix_rounds": fix_round, "detail": meta.get("err"),
                        "dispatcher": "layer3_v2"})
            return {"hyp_id": hyp_id, "outcome": "fix_author_failed"}
        harness_text = new_text
        # Same unwind-strip on fixed harnesses
        harness_text = re.sub(r"^\s*#\[kani::unwind\([^)]*\)\]\s*\n?", "", harness_text, flags=re.MULTILINE)
        harness_path.write_text(harness_text, encoding="utf-8")

    # Run actual verification
    verdict, full_out = cargo_kani_run(harness_name)
    if verdict == "FAILED_SPEC":
        outcome = "kani_bug_confirmed"  # the harness's spec assertion fired
    elif verdict == "FAILED_SETUP":
        outcome = "kani_setup_panic"  # internal panic; spec not reached
    elif verdict == "SUCCESSFUL":
        outcome = "kani_invariant_proven_safe"
    else:
        outcome = f"kani_{verdict.lower()}"

    log_path = KANI_OUT / f"cargo_kani_{harness_name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(full_out, encoding="utf-8")

    append_log({
        "event": "kani_one", "hypothesis_id": hyp_id,
        "outcome": outcome, "kani_verdict": verdict,
        "fix_rounds": fix_round, "intent_check": intent,
        "harness_name": harness_name, "dispatcher": "layer3_v2",
    })
    return {
        "hyp_id": hyp_id, "outcome": outcome, "verdict": verdict,
        "fix_rounds": fix_round, "intent": intent,
    }


def main() -> int:
    KANI_OUT.mkdir(parents=True, exist_ok=True)
    print(f"Layer 3 v2 — POC-LIFTING dispatcher")
    print(f"Hyps: {len(STRONG_HYPS)} STRONG fires")
    print(f"Concurrency: {CONCURRENCY}")
    print(f"Max fix rounds: {MAX_FIX_ROUNDS}")
    print(f"Per-hyp kani timeout: {KANI_TIMEOUT}s")
    print()

    confirmed: list[str] = []
    safe: list[str] = []
    failed: list[tuple[str, str]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(dispatch_one, h): h for h in STRONG_HYPS}
        for fut in as_completed(futs):
            done += 1
            hid = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                failed.append((hid, f"exception: {e}"))
                print(f"[{done}/{len(STRONG_HYPS)}] {hid} EXCEPTION: {e}", flush=True)
                continue
            outcome = res.get("outcome", "?")
            print(f"[{done}/{len(STRONG_HYPS)}] {hid} {outcome} (intent={res.get('intent','-')[:60]})", flush=True)
            if outcome == "kani_bug_confirmed":
                confirmed.append(hid)
            elif outcome == "kani_invariant_proven_safe":
                safe.append(hid)
            else:
                failed.append((hid, outcome))

    print()
    print(f"DONE. confirmed={len(confirmed)} safe={len(safe)} failed={len(failed)}")
    print()
    if confirmed:
        print("KANI CONFIRMED BUGS (counterexample found):")
        for h in confirmed:
            print(f"  {h}")
    if safe:
        print("KANI PROVED SAFE (no counterexample exists in bounded state):")
        for h in safe:
            print(f"  {h}")
    if failed:
        print("FAILED / NEEDS REVIEW:")
        for h, why in failed:
            print(f"  {h}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
