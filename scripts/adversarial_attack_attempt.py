"""Adversarial attack-mode agent — explicitly try to drain insurance.

Unlike the recon agents (which verify invariants), this agent's goal is
to FIND a sequence of public engine calls that decreases
insurance_fund.balance below its starting value, in a way that does not
correspond to a legitimate protocol-loss event.

Output: a Rust integration test that compiles + runs against the engine
under feature="test", demonstrating either:
  - SUCCESS: insurance decreases, panic with "VIOLATION: ..."
  - FAILURE: assertion holds (insurance preserved), test passes cleanly

This is run against the engine at the bounty 3 pin (5059332).

Usage:
  python3 adversarial_attack_attempt.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Add audit-pipeline-cli to sys.path
sys.path.insert(0, "/root/audit-pipeline-cli/src")

from audit_pipeline.utils.github_snapshot import GitHubSnapshot
from audit_pipeline.utils.llm_tools import run_tool_using_agent

SYSTEM_PROMPT = """\
You are a Solana DeFi security researcher attempting to win an active
bug bounty against the Percolator perpetuals engine. The bounty target is
the live mainnet program at:

    Program:   2LfCFmDKwcnHunqdsCW9uV7KNgBgnFGASs8uM7MwHgHm
    Engine:    aeyakovenko/percolator @ 5059332f8a (this snapshot)
    Wrapper:   aeyakovenko/percolator-prog @ 04b854e

WIN CONDITION (bounty rules, max_risk.md §8):
  Cause engine.insurance_fund.balance to decrease below its starting
  value via any sequence of public-callable engine functions.

OUT OF SCOPE (do not pursue these):
  - Pyth oracle manipulation
  - Solana validator attacks
  - Bugs in the wrapper that don't reach the engine

IN SCOPE (attack vectors to consider):
  - Admission bypass (e.g. open a position that should be rejected)
  - K-coefficient overflow / drift
  - ADL math edge cases
  - Conservation violation (vault/c_tot/insurance accounting drift)
  - Fee credits sign flip
  - Cascade bypass (multi-instruction sequences that skip cascades)
  - Force-close edge cases
  - Resolved-mode reconciliation drains
  - Multi-step sequences where state evolves wrong

PRIOR DISCLOSED BUGS (already patched):
  - F7: use_insurance_buffer debits insurance without crediting vault
    (helper-level violation still empirically reproducible at this SHA;
    the question is whether public APIs gate it correctly)
  - #57: Sweep-Gap K-Drift (untouched accounts accumulate drift)
  - #61, #65, #69: cascade bypass via Trade*/LiquidateAtOracle
  - #63, #67: KeeperCrank reward edge cases
  - #75, #76: catchup_accrue rollback gaps

YOUR TASK:
  Construct a Rust integration test (against feature="test") that
  demonstrates a NEW attack — one that survives Anatoly's patches at
  pin 5059332. The test should:

  1. Open with `#![cfg(feature = "test")]`
  2. Use `RiskEngine::new(params)` with valid RiskParams
  3. Drive the engine through a sequence of PUBLIC or test_visible
     functions (the test_visible! macro on engine helpers makes them
     accessible under cfg(test))
  4. Snapshot insurance_fund.balance before and after the sequence
  5. Assert insurance_after >= insurance_before with a panic message
     "VIOLATION CONFIRMED: insurance drained from {pre} to {post}
      (delta={delta}); attack chain: ..."
  6. Compile with `cargo test --features test` against the engine

If you can construct such a test that PANICS, that's a real bounty win.
If after deep investigation you cannot find such a sequence, output a
test that PASSES (insurance preserved) — that's a meaningful safety
attestation.

INVESTIGATION APPROACH:
  - Use grep to find all callers of `use_insurance_buffer`,
    `absorb_protocol_loss`, `record_uninsured_protocol_loss`
  - Use read_file on percolator.rs to read the relevant code regions
  - Check what gates each call (market_mode, bankruptcy_hmax_lock,
    admission checks)
  - Trace public-callable wrappers (the engine's pub fn / test_visible
    macros)
  - Look for state combinations that could let absorb run with a `loss`
    value that doesn't match a real economic loss

OUTPUT FORMAT:
  Pure Rust source code only. No prose preamble. Start with
  `#![cfg(feature = "test")]`. End with the closing brace of the test
  function. The test name must be `test_attack_attempt_2026_05_06`.

  If you cannot construct a working attack, write a passing test that
  documents why the engine is robust against the family of attacks you
  considered.
"""

INITIAL_USER_MESSAGE = """\
Please read the engine source at src/percolator.rs (it's a large file —
~10K lines, focus on the regions you need). Investigate the gating
around use_insurance_buffer / absorb_protocol_loss. Find out:

1. Every public or test_visible function that can transitively reach
   absorb_protocol_loss
2. The preconditions each path enforces (market_mode, account state,
   bankruptcy lock, admission checks)
3. Whether any path can reach absorb_protocol_loss with a `loss`
   argument that is NOT backed by a real per-account loss event

Then construct the attack-attempt test. Output ONLY the Rust source.
"""


def main() -> int:
    workspace_dir = Path(os.environ.get("WORKSPACE_DIR", "/root/audit_runs/percolator-live"))

    print("Opening GitHubSnapshot of aeyakovenko/percolator @ 5059332...", flush=True)
    with GitHubSnapshot("aeyakovenko/percolator", "5059332") as snap:
        print(f"  workspace: {snap.workspace}", flush=True)
        print(f"  resolved sha: {snap.sha}", flush=True)
        print(flush=True)
        print("Dispatching adversarial agent (max 30 turns)...", flush=True)
        result = run_tool_using_agent(
            workspace=snap.workspace,
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

    out_dir = Path("/root/adversarial_attempts")
    out_dir.mkdir(parents=True, exist_ok=True)
    test_path = out_dir / "test_attack_attempt_2026_05_06.rs"
    log_path = out_dir / "tool_calls.jsonl"

    test_path.write_text(result.text, encoding="utf-8")
    print(f"Wrote: {test_path}", flush=True)
    with log_path.open("w", encoding="utf-8") as f:
        for tc in result.tool_calls:
            f.write(json.dumps(tc) + "\n")
    print(f"Wrote: {log_path}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
