#!/usr/bin/env python3
"""Layer 4 redo for the 5 LLM-failed tests with stronger prompt.

Uses the WORKING H1 LiteSVM test as a template + explicit error code decoder.
The original L4 dispatcher's prompt didn't include a working LiteSVM test as
template — only the L2 PoC + L3 harness — so the LLM didn't learn the trade
setup pattern that succeeds in BPF.
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
WRAPPER_ROOT = Path("/root/audit_runs/percolator-live/target/wrapper")
WRAPPER_TESTS = WRAPPER_ROOT / "tests"
WRAPPER_COMMON = WRAPPER_TESTS / "common" / "mod.rs"
H1_WORKING_TEST = WRAPPER_TESTS / "litesvm_h1_residual_conservation.rs"
POC_DIR = CYCLE / "poc"
LITESVM_OUT = CYCLE / "litesvm"
LOG = CYCLE / "hunt.log.jsonl"

CONCURRENCY = 2
CARGO_TIMEOUT = 1800
MAX_FIX_ROUNDS = 3

REDO_HYPS = [
    "H5-permissionless-trigger-surface",
    "V1-residual-conservation-strict",
    "T1-hyperp-mark-cpi-bundled-trade",
    "U30-deposit-fee-credits-zero-debt-after-sync-still-succeeds",
    "V26-compute-trade-pnl-no-i128-min",
    "V7-insurance-counter-vault-coupling",
]

ERROR_DECODER = """
# Wrapper PercolatorError code decoder (ALL of L4 v1's failures came from these)

  Custom(13)  EngineInsufficientBalance
  Custom(14)  EngineUndercollateralized   ← fix: reduce position_size to fit margin
  Custom(15)  EngineUnauthorized
  Custom(18)  EngineOverflow
  Custom(21)  EnginePositionSizeMismatch  ← fix: use exactly the right size
  Custom(27)  HyperpTradeNoCpiDisabled    ← fix: use init_market_with_cap NOT init_market_hyperp,
                                                 OR use encode_trade_cpi instead of try_trade
  InvalidInstructionData                  ← fix: instruction tag wrong; for engine-internal
                                                 helpers like settle_flat_negative_pnl, drive
                                                 them via KeeperCrank instead of trying to call
                                                 the helper directly.

# Position-size formula for non-Hyperp markets

When using `init_market_with_cap(0, 80)`:
  - Default oracle: $138 (138_000_000 in e-6)
  - Initial margin BPS: 1000 (10%)
  - Max position_size = user_capital / (oracle_price * margin_bps / 1e7)

For user_deposit=100_000:
  position_size <= 100_000 * 10_000 / (138 * 1000) ≈ 7246
  Use position_size = 7000 (the H1 working pattern).

For larger deposits, scale proportionally.
"""


def slug(h: str) -> str:
    return h.lower().replace("-", "_")


def short_litesvm_name(hyp_id: str) -> str:
    s = slug(hyp_id)
    if len(s) > 50:
        s = s[:40].rstrip("_")
    return f"litesvm_{s}"


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


def build_prompt(hyp_id: str, claim: str, engine_function: str,
                 poc_text: str, common_text: str, h1_template: str,
                 test_name: str) -> str:
    return f"""You are re-authoring a LiteSVM L4 test that the FIRST author attempt failed
on (instruction-encoding error or undercollateralized trade). Use the WORKING
H1 test below as your STRUCTURAL TEMPLATE — copy its setup pattern verbatim,
only change the spec assertion + any hyp-specific path needed.

{ERROR_DECODER}

# Hypothesis

- ID: `{hyp_id}`
- Engine function: `{engine_function}`
- Natural-language claim:

```
{claim}
```

# Layer 2 PoC (engine-direct concrete witness — gives you the spec assertion)

```rust
{poc_text}
```

# THE WORKING TEMPLATE — litesvm_h1_residual_conservation.rs

This test SUCCESSFULLY drives the engine into the absorb_protocol_loss path
through public BPF instructions and asserts the F7 invariant. **Copy its
structure** — `init_market_with_cap`, `top_up_insurance`, `init_lp/init_user`
with `lp_deposit=2_000_000` + `user_deposit=100_000`, `position_size=7000`,
`set_slot_and_price` to crash, KeeperCrank loop, then state assertion.

Only change:
1. Function name → `{test_name}` exactly
2. Final spec assertion → match the L2 PoC's invariant for this hyp
3. If the hyp targets a DIFFERENT internal helper than absorb_protocol_loss,
   you may need different actions (e.g., for U30 use deposit_fee_credits;
   for U21 use ResolvePermissionless on empty market; for V26 use trades that
   exercise compute_trade_pnl). For these, KEEP the H1 setup but swap the
   action(s) at the bug-trigger step.

```rust
{h1_template}
```

# wrapper/tests/common/mod.rs (full helper catalog)

```rust
{common_text}
```

# Output

Output ONLY the Rust test file content. Wrap in a single ```rust fence.
Function name MUST be exactly `{test_name}`.
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
    common_text = WRAPPER_COMMON.read_text(encoding="utf-8", errors="replace")
    h1_template = H1_WORKING_TEST.read_text(encoding="utf-8", errors="replace")

    prompt = build_prompt(hyp_id, claim, engine_function, poc_text,
                          common_text, h1_template, test_name)

    sys.path.insert(0, "/root/audit-pipeline-cli/src")
    from audit_pipeline.utils.llm import complete

    os.environ["JELLEO_SPEND_CALLER"] = f"layer4_redo/{hyp_id}/author"
    response = complete(prompt)
    m = re.search(r"```rust\n(.*?)\n```", response.text, re.DOTALL)
    if not m:
        return None, {"err": "no rust code in LLM response"}
    return m.group(1), {"cost_usd": response.cost_usd}


def cargo_test_run(test_name: str) -> tuple[bool, bool, str, str]:
    try:
        proc = subprocess.run(
            ["cargo", "test", "--features", "test-sbf", "--test", test_name,
             "--", "--nocapture"],
            cwd=str(WRAPPER_ROOT), capture_output=True, text=True,
            timeout=CARGO_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, False, "timeout", "timeout"
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if ("could not compile" in out
            or "error[E" in out.split("test result:")[0]
            or "did not match any tests" in out):
        return False, False, out, "compile_failed"
    INFRA_FAIL = ["BPF not found", "Run: cargo build-sbf",
                  "tests/common/mod.rs", "ProgramFailedToLoad"]
    is_infra = any(m in out for m in INFRA_FAIL)
    if is_infra and "BUG CONFIRMED" not in out:
        return True, False, out, "infra_fail"
    if "BUG CONFIRMED" in out:
        return True, True, out, "BUG CONFIRMED"
    if re.search(rf"panicked at tests/{re.escape(test_name)}\.rs", out):
        return True, True, out, f"panic at tests/{test_name}.rs"
    if proc.returncode != 0 and not is_infra:
        return True, True, out, "non-zero exit"
    return True, False, out, "test passed"


def dispatch_one(hyp_id: str) -> dict:
    test_name = short_litesvm_name(hyp_id)
    test_path = WRAPPER_TESTS / f"{test_name}.rs"
    cargo_log = LITESVM_OUT / f"cargo_{test_name}.log"

    append_log({"event": "litesvm_authored", "hypothesis_id": hyp_id,
                "test_name": test_name, "dispatcher": "layer4_redo",
                "phase": "author_round_1"})
    text, meta = author_test(hyp_id, test_name)
    if text is None:
        append_log({"event": "litesvm_one", "hypothesis_id": hyp_id,
                    "outcome": "author_failed", "dispatcher": "layer4_redo"})
        return {"hyp_id": hyp_id, "outcome": "author_failed"}
    test_path.write_text(text, encoding="utf-8")

    fix_round = 0
    while True:
        compiles, fired, out, note = cargo_test_run(test_name)
        if compiles:
            break
        fix_round += 1
        if fix_round > MAX_FIX_ROUNDS:
            cargo_log.write_text(out, encoding="utf-8")
            append_log({"event": "litesvm_one", "hypothesis_id": hyp_id,
                        "outcome": "compile_fail", "fix_rounds": fix_round,
                        "dispatcher": "layer4_redo"})
            return {"hyp_id": hyp_id, "outcome": "compile_fail"}
        # quick fix retry without LLM (just give up if not compiling first try
        # with the working template; usually means hyp needs custom approach)
        cargo_log.write_text(out, encoding="utf-8")
        append_log({"event": "litesvm_one", "hypothesis_id": hyp_id,
                    "outcome": "compile_fail", "fix_rounds": fix_round,
                    "dispatcher": "layer4_redo"})
        return {"hyp_id": hyp_id, "outcome": "compile_fail",
                "fix_rounds": fix_round}

    cargo_log.write_text(out, encoding="utf-8")
    if "infra_fail" in note:
        outcome = "litesvm_infra_fail"
    elif fired:
        outcome = "litesvm_bug_reproduced"
    else:
        outcome = "litesvm_passed"
    append_log({"event": "litesvm_one", "hypothesis_id": hyp_id,
                "outcome": outcome, "fired": fired,
                "test_name": test_name, "classifier_note": note,
                "dispatcher": "layer4_redo"})
    return {"hyp_id": hyp_id, "outcome": outcome, "note": note}


def _prepare_bpf_environment() -> None:
    """L3+L4 audit Defects 03 + 10: rebuild BPF + archive stale PoCs.

    Mirrors the preflight in dispatch_layer4_v1.py so both L4 entry
    paths converge on a fresh .so + clean tests/ dir before running.
    """
    import datetime as _dt
    print("[preflight] cargo build-sbf ...", flush=True)
    # POST-AUDIT FIX: previously silent-continued on build failure with
    # "Continuing with whatever .so is on disk" — defeated the entire
    # purpose of the preflight (stale .so was the cycle 20260511 L4 bug).
    # Now exit non-zero so the dispatcher refuses to run tests against
    # a broken / stale binary. Operator must investigate the build failure
    # before re-running. Use SKIP_BUILD_SBF_PREFLIGHT=1 to override
    # (e.g. running offline on a machine without solana-cli installed).
    import os as _os
    if _os.environ.get("SKIP_BUILD_SBF_PREFLIGHT", "").strip() == "1":
        print("[preflight] SKIPPED — SKIP_BUILD_SBF_PREFLIGHT=1 in env", flush=True)
        return
    try:
        rc = subprocess.run(
            ["cargo", "build-sbf"],
            cwd=str(WRAPPER_ROOT), capture_output=True, text=True, timeout=900,
        )
        if rc.returncode != 0:
            print(f"[preflight] cargo build-sbf FAILED rc={rc.returncode}; "
                  f"tail: {(rc.stderr or '')[-500:]}", flush=True)
            print(
                "[preflight] ABORT — refusing to dispatch L4 tests against "
                "a stale/broken .so. Fix the build and re-run, or set "
                "SKIP_BUILD_SBF_PREFLIGHT=1 to override.",
                flush=True,
            )
            sys.exit(1)
        else:
            print("[preflight] cargo build-sbf OK", flush=True)
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError) as e:
        print(
            f"[preflight] cargo build-sbf unavailable: {e}\n"
            "[preflight] ABORT — without a fresh build the .so on disk "
            "may be stale. Set SKIP_BUILD_SBF_PREFLIGHT=1 to override "
            "if you know it's current.",
            flush=True,
        )
        sys.exit(1)

    archive_dir = WRAPPER_ROOT / "tests.archive" / _dt.datetime.utcnow().strftime("redo-preflight-%Y%m%dT%H%M%SZ")
    archive_dir.mkdir(parents=True, exist_ok=True)
    n_archived = 0
    for stale in (WRAPPER_ROOT / "tests").glob("litesvm_*.rs"):
        if any(stale.stem.endswith(slug(h)[:40]) for h in REDO_HYPS):
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
    print(f"L4 REDO with H1 template + error decoder")
    print(f"Hyps: {len(REDO_HYPS)}")
    print()
    _prepare_bpf_environment()
    print()
    results = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(dispatch_one, h): h for h in REDO_HYPS}
        done = 0
        for fut in as_completed(futs):
            done += 1
            hid = futs[fut]
            try:
                r = fut.result()
            except Exception as e:
                results[hid] = {"outcome": f"exception: {e}"}
                print(f"[{done}/{len(REDO_HYPS)}] {hid} EXCEPTION", flush=True)
                continue
            results[hid] = r
            print(f"[{done}/{len(REDO_HYPS)}] {hid} {r.get('outcome')}",
                  flush=True)
    print()
    print("DONE")
    for h, r in results.items():
        print(f"  {h}: {r.get('outcome')} ({r.get('note','-')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
