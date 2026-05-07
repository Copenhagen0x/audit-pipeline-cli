# Adversarial attack attempt — 2026-05-06

A 31-turn / 49-tool-call adversarial agent dispatched against the live
Percolator bounty 3 target with the explicit goal of constructing a
working insurance-drain exploit. **The agent concluded the engine is
robust against the bounty win condition at SHA `5059332`.**

This is a different methodology mode from the recon agents: the recon
loops verify invariants, the adversarial loop explicitly attempts to
construct attack sequences. Both are now in the pipeline.

## Setup

- **Target:** mainnet bounty 3 program `2LfCFmDKwcnHunqdsCW9uV7KNgBgnFGASs8uM7MwHgHm`
- **Engine pin:** `aeyakovenko/percolator @ 5059332f8a`
- **Wrapper pin:** `aeyakovenko/percolator-prog @ 04b854e`
- **Win condition:** decrease `engine.insurance_fund.balance` below its
  starting value via any sequence of public engine calls
- **Out of scope:** Pyth manipulation, Solana validator attacks
- **Agent:** Claude Sonnet 4-6, max_turns=30, full tool access
  (`read_file`, `grep`, `find_function`)

## Cost

```
Turns:           31
Tool calls:      49 (read_file × 36, grep × 8, find_function × 5)
Input tokens:    1,254,069
Output tokens:   9,711
Total cost:      ~$3.80
Wall time:       ~7 minutes
```

## What the agent did

The agent systematically traced every path to `absorb_protocol_loss` /
`use_insurance_buffer` / `record_uninsured_protocol_loss` (the three
helpers that decrease `insurance_fund.balance`) and asked: can any
public-API call sequence reach them with a `loss` argument that isn't
backed by a real per-account economic loss?

Its trace:

```
absorb_protocol_loss (line 4845) reachable from:
  ├─ resolve_flat_negative (line 7124, 7145)
  │    ← touch_account_live_local (line 7214)
  │       ← settle_account_not_atomic, execute_trade_not_atomic,
  │         liquidate_at_oracle_not_atomic, close_account_not_atomic,
  │         keeper_crank Phase 2 sweep
  ├─ settle_flat_negative_pnl_not_atomic (line 10423)
  └─ reconcile_resolved_not_atomic (line 9930)

use_insurance_buffer (line 4811, called by absorb) also reachable from:
  └─ enqueue_adl bankruptcy path (line 4983)
       ← gated by trigger_bankruptcy_hmax_lock (line 4981)
```

For each path, the preconditions were enumerated:

1. `resolve_flat_negative` requires `position_basis_q == 0 AND pnl < 0`
   AND the loss argument equals `pnl.unsigned_abs()`. After the call,
   `set_pnl_with_reserve(idx, 0, NoPositiveIncreaseAllowed, None)`
   zeroes pnl — second call on same account is a no-op (no double-drain).

2. Negative PnL on a Live market arises ONLY from real economic events:
   `settle_side_effects_live` (ADL/funding loss application) or
   `accrue_market_to + position mismatch` (real price movement). Both
   require real vault collateral to have entered via `deposit_not_atomic`.

3. `set_pnl` on a Live market only permits POSITIVE PnL increase via
   `UseAdmissionPair` (line 2403-2412) — a caller cannot forge negative
   PnL.

4. `deposit_not_atomic` explicitly skips `resolve_flat_negative`
   (line 7386-7389, comment: "deposit MUST NOT invoke
   resolve_flat_negative") — so deposits cannot be used to drain.

5. The conservation check `vault >= c_tot + insurance_fund.balance`
   (line 5977) is enforced at every public API boundary via
   `assert_public_postconditions`. No sequence can drain insurance
   below the vault slack.

## Verdict (from the agent)

> **CONCLUSION: No public-API sequence drains insurance_fund.balance
> below its starting value without corresponding real economic loss
> from vault. The engine is robust against insurance-drain attacks
> via public API.**

## Caveats

- The agent's analysis is structural; it does not run code. The test
  it constructed has setup bugs (over-strict params validation; same
  pattern that bit our other confirm runs tonight). Reading the
  reasoning is the artifact, not running the test.
- This does not validate the wrapper layer in isolation (wrapper code
  was out of the snapshot — separate adversarial pass would target
  `aeyakovenko/percolator-prog`).
- Pyth manipulation paths are explicitly out of scope per bounty
  rules; the agent did not analyze those.
- F7 at the helper level remains empirically violated
  (test_confirm_v1_vault_residual_conservation panics with
  delta=+10000 at this same SHA when called directly via the
  test_visible! macro). The adversarial verdict is specifically about
  reachability from PUBLIC API.

## Files

```
test_attack_attempt_2026_05_06.rs   — agent's full analysis + Rust test
                                       (test has setup bugs; the prose
                                       analysis at the top is the value)
tool_calls.jsonl                    — every read_file / grep /
                                       find_function the agent issued
                                       during its 31-turn investigation
README.md                           — this file
```

## How to re-run

```bash
python3 scripts/adversarial_attack_attempt.py
```

The script lives at `scripts/adversarial_attack_attempt.py` in this
repo and opens a fresh GitHubSnapshot every run, so it always probes
the latest pinned SHA.
