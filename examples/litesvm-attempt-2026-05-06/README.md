# Autonomous Layer 4 LiteSVM attack attempt — 2026-05-06

**First end-to-end autonomous Layer 4 dispatch.** A tool-using agent
autonomously authored a complete LiteSVM test, compiled it against the
actual BPF binary, and executed it in a Solana VM simulator. The agent
attempted four distinct attack vectors against the live bounty 3 target.
**All four attacks were defeated by the engine.**

This is structurally different from previous methodology runs:
- **Layer 1 (recon)** reads source, makes structural verdicts — no execution
- **Layer 2 (engine confirm)** writes Rust unit tests against the engine
  *library* — execution but not at the BPF level
- **Layer 4 (litesvm)** writes Rust integration tests against the *deployed
  BPF binary* in a Solana VM — full multi-instruction attack sequences

The pipeline already had Layer 4 *scaffolding* (template-based
`audit-pipeline litesvm author` command), but no agent that authored
the test logic autonomously. The `scripts/litesvm_attack_attempt.py`
script we just built fills that gap.

## Setup

- **Target:** mainnet bounty 3 program `2LfCFmDKwcnHunqdsCW9uV7KNgBgnFGASs8uM7MwHgHm`
- **Wrapper repo:** `aeyakovenko/percolator-prog @ 04b854e` (cloned locally)
- **BPF binary:** built locally via `cargo-build-sbf --features small`
  (256 accounts tier, identical layout to the deployed binary modulo
  size tier)
- **Win condition:** decrease `engine.insurance_fund.balance` below
  starting value via any sequence of public BPF instructions
- **Out of scope:** Pyth manipulation, Solana validator attacks
- **Agent:** Claude Sonnet 4-6, max_turns=30, full tool access against
  the wrapper source

## Cost

```
Turns:           31
Tool calls:      44 (read_file × 32, grep × 8, find_function × 4)
Input tokens:    2,313,995
Output tokens:   8,756
Total cost:      ~$7.00
Wall time:       agent ~6 min + cargo test 7.48s
```

## Attack vectors the agent constructed

The agent authored a single test exercising four distinct attacks:

### Attack A — A1 self-trade + adversarial oracle walk

Open a matched LONG/SHORT pair (attacker controls both), then walk the
oracle ~25 % down over many slots while cranking. Pre-v12.19 this drained
insurance. Defeated by:

1. `max_price_move_bps_per_slot = 4` rejects single-slot 25 % gaps
2. §1.4 solvency envelope (`4×100 + 100·funding + 50 = 460 ≤ 500
   mm_bps`) ensures the LP's own capital absorbs the loss before
   insurance is touched
3. Admission-threshold gate at `percolator.rs:6519` blocks fresh ADL
   enqueues when the price-move budget is exhausted

### Attack B — crank-reward maintenance-fee sweep drain

Submit `KeeperCrank` as a named caller hoping to extract the 50 %
crank-reward share. Defeated by: the `sweep_delta` is the
*increase* in insurance from the fee pass; reward is bounded by
`min(sweep_delta × 50%, ins_now)`. Net change per crank is always
`+sweep_delta − reward ≥ 0`. Insurance can only grow through this path.

### Attack C — rapid deposit + withdraw extraction

Deposit, open a position, move price favorably, convert released PnL,
withdraw — hoping to extract more than deposited. Defeated by the
engine's `vault = c_tot + insurance + residual` accounting invariant.
Pre-existing regression test
`test_attack_yfi_style_profit_recycling_no_net_extraction`
(`tests/test_economic_attack_vectors.rs:264`) confirms this.

### Attack D — LP profit extraction via reverse price walk

Walk the oracle back toward baseline to give the LP profit, then
withdraw the profit. Defeated by the same conservation invariant as
Attack C.

## Test result

```
$ cargo test --features small --test test_litesvm_attack_attempt_2026_05_06 --release
running 1 test
test test_litesvm_attack_attempt_2026_05_06 ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured;
0 filtered out; finished in 7.48s
```

**Outcome:** `safety_attestation` — insurance balance preserved across
all four attack vectors, asserted programmatically by the test.

## What this proves

- The pipeline now has **autonomous Layer 4** end-to-end dispatch
  (script-only, no human authoring)
- Against the live bounty 3 wrapper at `04b854e`, four reasonable
  attack vectors all fail to drain insurance
- The methodology can re-run against any future SHA in <10 minutes
  for ~$7 — a continuous regression for the bounty target

## What this does NOT prove

- Other untried attack vectors might succeed (e.g. sweep-gap K-drift
  with hundreds of accounts; the agent didn't construct that)
- The agent's analysis is bounded by what it explored in 31 turns
- This is the small-tier (256-account) test harness; the deployed
  binary is the 4096-account tier (the layout is structurally
  identical but exact CU bounds differ)

## Files

```
test_litesvm_attack_attempt_2026_05_06.rs        — agent's full attack test
test_litesvm_attack_attempt_2026_05_06.cargo.log — cargo test output (PASS)
test_litesvm_attack_attempt_2026_05_06.summary.json — pipeline-style summary
README.md                                        — this file
```

## Re-running

```bash
# Local laptop with Solana toolchain installed:
git clone --depth=1 https://github.com/aeyakovenko/percolator-prog.git
cd percolator-prog
cargo-build-sbf --features small        # ~50s, builds BPF binary

# Then from audit-pipeline-cli:
python scripts/litesvm_attack_attempt.py /path/to/percolator-prog
```

The script is at
[`scripts/litesvm_attack_attempt.py`](../../scripts/litesvm_attack_attempt.py)
in this repo.
