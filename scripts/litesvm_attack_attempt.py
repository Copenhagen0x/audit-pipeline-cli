"""Autonomous Layer 4 LiteSVM attack-mode agent.

Unlike the existing `audit-pipeline litesvm author` command (which is a
template-based scaffold that requires manual filling), this agent
autonomously authors a complete LiteSVM test from scratch.

Pipeline:
  1. Use a local clone of aeyakovenko/percolator-prog as workspace
     (BPF binary must already be built via cargo-build-sbf --features small)
  2. Spawn a tool-using Claude agent with full read access to the wrapper
     source AND the existing TestEnv harness (tests/common/mod.rs)
  3. Agent's task: write a Rust integration test that drains
     insurance_fund.balance via a sequence of public instructions
     (Trade*, KeeperCrank, Deposit/Withdraw, etc.)
  4. Strip code fences + prose preface, write to tests/, compile, run
  5. Classify outcome (fired / safety_attestation / compile_error / unknown)

Usage:
  python3 litesvm_attack_attempt.py [WRAPPER_DIR]
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Add audit-pipeline-cli to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from audit_pipeline.utils.llm_tools import run_tool_using_agent


SYSTEM_PROMPT = """\
You are a Solana DeFi security researcher attempting to win an active
bug bounty against the Percolator perpetuals protocol. You are now
operating at LAYER 4 — multi-instruction LiteSVM test authoring against
the deployed BPF program (not just the engine library).

Bounty target:
  Program:    2LfCFmDKwcnHunqdsCW9uV7KNgBgnFGASs8uM7MwHgHm (mainnet)
  Wrapper:    aeyakovenko/percolator-prog @ 04b854e
  Engine:     aeyakovenko/percolator @ 5059332f8a (pinned by Cargo.toml)

WIN CONDITION (max_risk.md §8):
  Cause `engine.insurance_fund.balance` to decrease below its starting
  value via any sequence of public-callable BPF instructions.

OUT OF SCOPE:
  - Pyth oracle manipulation
  - Solana validator attacks
  - Bugs in the test harness (we want a real engine bug)

TEST HARNESS:
  The wrapper repo has `tests/common/mod.rs` with a `TestEnv` struct that
  wraps LiteSVM and provides high-level helpers:
    - TestEnv::new() — fresh LiteSVM environment with the BPF program loaded
    - env.init_market_with_invert(invert) — initialize the market
    - env.init_lp(&keypair) — register an LP, returns lp_idx
    - env.init_user(&keypair) — register a user, returns user_idx
    - env.deposit(&keypair, idx, amount) — deposit collateral
    - env.withdraw(&keypair, idx, amount) — withdraw collateral
    - env.trade(&user, &lp, lp_idx, user_idx, size) — execute a trade
    - env.crank() — run KeeperCrank
    - env.top_up_insurance(&admin, amount) — add to insurance
    - env.read_engine_vault() / read_account_pnl(idx) / read_pnl_pos_tot()
    - env.read_insurance_balance() — read insurance_fund.balance

  Read the actual mod.rs to find the EXACT method signatures, plus look
  at existing tests (test_basic.rs, test_conservation.rs, test_a1_siphon_regression.rs,
  test_economic_attack_vectors.rs) for working examples.

INVESTIGATION APPROACH:
  1. Read tests/common/mod.rs to learn TestEnv's exact API
  2. Read tests/test_a1_siphon_regression.rs and test_economic_attack_vectors.rs
     for adversarial test patterns that already exist
  3. Identify a multi-instruction sequence that you believe could decrease
     insurance — common candidates:
       - Self-trade with same owner causing K-drift
       - Crank with empty candidates that advances clock without touching
       - Withdraw racing a partial-liquidation cascade
       - Trade with admit_h_max causing admission of an under-margin position
  4. Write a single complete test function that:
     - Opens with `mod common; use common::*;`
     - Sets up a `TestEnv`, market, two accounts
     - Records `insurance_pre = env.read_insurance_balance()`
     - Drives the attack sequence
     - Records `insurance_post`
     - Asserts `insurance_post >= insurance_pre` with a panic message
       "VIOLATION: insurance drained from {pre} to {post}"

OUTPUT FORMAT:
  Pure Rust source code only — no prose preamble, no markdown fences,
  no analysis text. Start with `mod common;`. End with the closing
  brace of the test function. The test name must be
  `test_litesvm_attack_attempt_2026_05_06`.

  If after deep investigation you cannot find an attack sequence, write
  a passing test that documents which attacks were tried + why each
  failed (as comments inside the test).
"""

INITIAL_USER_MESSAGE = """\
Investigate the wrapper repo. Steps:

1. Read tests/common/mod.rs (lines 1-200, then targeted reads for the methods
   you need) to learn the EXACT TestEnv API.
2. Read at least one of: tests/test_a1_siphon_regression.rs,
   tests/test_economic_attack_vectors.rs, tests/test_conservation.rs to
   understand how existing tests structure attacks against the wrapper.
3. Read src/percolator.rs to find a sequence of public instructions that
   could plausibly reach `use_insurance_buffer` without a real per-account
   loss event backing it.
4. Write the LiteSVM test.

Output ONLY valid Rust code starting with `mod common;`.
"""


def _strip_prose_and_fences(text: str) -> str:
    """Strip leading prose, code fences, and trailing prose."""
    # Strip code fences if present
    m = re.search(r"```(?:rust|rs)?\n(.+?)```", text, re.DOTALL)
    if m:
        text = m.group(1)

    # Strip leading prose — find first Rust marker
    lines = text.splitlines()
    rust_anchors = (
        "mod ", "use ", "extern crate", "#![", "#[", "pub ", "fn ",
        "const ", "static ", "type ", "struct ", "enum ", "trait ",
        "impl ", "// ",
    )
    first_rust = -1
    for i, ln in enumerate(lines):
        stripped = ln.lstrip()
        if any(stripped.startswith(a) for a in rust_anchors):
            first_rust = i
            break
    if first_rust > 0:
        lines = lines[first_rust:]

    # Strip trailing prose after last `}`
    last_close = -1
    for i, ln in enumerate(lines):
        if ln.strip() == "}":
            last_close = i
    if last_close >= 0:
        lines = lines[: last_close + 1]

    return "\n".join(lines) + "\n"


def main() -> int:
    if len(sys.argv) > 1:
        wrapper_dir = Path(sys.argv[1])
    else:
        wrapper_dir = Path("C:/Users/btrco/OneDrive/Desktop/percolator-prog-litesvm")

    if not wrapper_dir.exists():
        print(f"ERROR: wrapper dir not found at {wrapper_dir}", flush=True)
        return 2

    # Sanity: BPF binary built?
    bpf_path = wrapper_dir / "target" / "deploy" / "percolator_prog.so"
    if not bpf_path.exists():
        print(f"ERROR: BPF binary not built. Run cargo-build-sbf --features small first.", flush=True)
        return 2

    print(f"Wrapper dir: {wrapper_dir}", flush=True)
    print(f"BPF binary:  {bpf_path}", flush=True)
    print(flush=True)
    print("Dispatching tool-using agent (max 30 turns)...", flush=True)

    result = run_tool_using_agent(
        workspace=wrapper_dir,
        system_prompt=SYSTEM_PROMPT,
        initial_user_message=INITIAL_USER_MESSAGE,
        model="claude-sonnet-4-6",
        max_turns=30,
        max_tokens_per_turn=8192,
    )

    print(flush=True)
    print(f"=== Agent finished ({result.n_turns} turns, "
          f"{len(result.tool_calls)} tool calls) ===", flush=True)
    print(f"Tokens: {result.input_tokens:,}in / {result.output_tokens:,}out", flush=True)
    print(f"Stop reason: {result.stop_reason}", flush=True)
    print(flush=True)

    test_code = _strip_prose_and_fences(result.text)
    test_name = "test_litesvm_attack_attempt_2026_05_06"
    test_path = wrapper_dir / "tests" / f"{test_name}.rs"
    raw_path = wrapper_dir / "tests" / f"{test_name}.raw.txt"

    raw_path.write_text(result.text, encoding="utf-8")
    test_path.write_text(test_code, encoding="utf-8")
    print(f"Raw output:  {raw_path}", flush=True)
    print(f"Test file:   {test_path} ({len(test_code)} bytes)", flush=True)

    # Compile + run
    print(flush=True)
    print(f"Compiling: cargo test --features small --test {test_name} --release ...",
          flush=True)
    proc = subprocess.run(
        ["cargo", "test", "--features", "small", "--test", test_name, "--release"],
        cwd=wrapper_dir,
        capture_output=True,
        text=True,
        timeout=600,
    )
    log_path = wrapper_dir / "tests" / f"{test_name}.cargo.log"
    log_path.write_text(
        f"=== stdout ===\n{proc.stdout}\n=== stderr ===\n{proc.stderr}\n",
        encoding="utf-8",
    )
    print(f"Cargo log:   {log_path}", flush=True)
    print(f"Exit code:   {proc.returncode}", flush=True)

    # Classify outcome
    combined = proc.stdout + proc.stderr
    if proc.returncode == 0 and "test result: ok" in combined:
        outcome = "safety_attestation"
    elif "VIOLATION" in combined or "panicked at" in combined:
        if "test result: FAILED" in combined:
            outcome = "fired"
        else:
            outcome = "panicked"
    elif "could not compile" in combined or "error[" in combined:
        outcome = "compile_error"
    else:
        outcome = "unknown"

    print(flush=True)
    print(f"=== OUTCOME: {outcome} ===", flush=True)

    summary_path = wrapper_dir / "tests" / f"{test_name}.summary.json"
    summary_path.write_text(json.dumps({
        "test_name": test_name,
        "test_path": str(test_path),
        "cargo_rc": proc.returncode,
        "outcome": outcome,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "n_turns": result.n_turns,
        "n_tool_calls": len(result.tool_calls),
    }, indent=2), encoding="utf-8")
    print(f"Summary:     {summary_path}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
