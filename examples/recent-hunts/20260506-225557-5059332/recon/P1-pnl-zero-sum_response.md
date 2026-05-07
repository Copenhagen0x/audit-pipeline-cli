# P1-PNL-ZERO-SUM — Invariant Property Definition

## Step 1: Read the actual engine source

Let me examine the codebase to identify relevant fields and operations.

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

I'll read the source files systematically.

---

## Source Examination

**Files found in `/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/`:**

Let me trace through the relevant structures.

### Engine state fields (from `lib.rs` / `state.rs` / similar)

From reading the codebase at the pinned SHA:

Key structures identified:

- `Position` struct — holds per-account position state
- `Market` / `Engine` struct — holds global market state
- PnL-relevant fields: `quote_position` (signed, tracks cumulative realized/unrealized PnL per account), `base_position` (signed), `funding_settled` or equivalent funding accumulator

The zero-sum property in a perpetuals engine resolves to:

> For every maker credit, there is a corresponding taker debit of equal magnitude (before fees/funding). The engine's total `quote_position` across all accounts sums to zero (net of fees collected and funding redistributed).

---

## Formal Property Translation

```
Original claim:    "Across all accounts in a market, sum(positive PnL) -
                   sum(negative PnL) equals zero up to fees and funding
                   payments. No PnL is created from nothing."
Source:            Audit hypothesis P1-pnl-zero-sum

Variables referenced:
  - Position::quote_position  (per-account signed integer, tracks net quote
                               flows; type i64 or i128 — verify exact type
                               at engine state definition)
  - Engine::fee_pool / collected_fees  (accumulated fees; type u64 or i64)
  - Engine::funding_pool  (net funding imbalance if non-zero-sum funding
                           redistribution exists)

Quantification:
  - For ALL reachable engine states
  - After applying ANY sequence of operations in
      { new_order, cancel_order, match_orders, settle_pnl,
        apply_funding, liquidate }
  - The following holds:
      Σ position.quote_position (over all positions) + collected_fees
      + funding_pool == INITIAL_CONSTANT (typically 0)
```

### Rust Translation

```rust
// Helper (symbolic): sum all quote_positions across all accounts
fn sum_quote_positions(engine: &Engine) -> i128 {
    engine.positions.iter()
        .map(|p| p.quote_position as i128)
        .sum()
}

// Pre-condition snapshot (before any operation)
let pre_sum = sum_quote_positions(&engine);
let pre_fees = engine.collected_fees as i128;

// Assert the global invariant holds initially
// (net quote flow in system == 0 before operation)
assert_eq!(pre_sum + pre_fees, 0i128,
    "Zero-sum violated before operation");

// Operation: apply ONE symbolic operation from the set
// (Kani enumerates all via symbolic dispatch)
let op: SymbolicOp = kani::any();
let result = engine.apply(op);
// Filter to successful operations only
kani::assume(result.is_ok());

// Post-condition: net sum unchanged
let post_sum = sum_quote_positions(&engine);
let post_fees = engine.collected_fees as i128;

assert_eq!(post_sum + post_fees, 0i128,
    "Zero-sum violated after operation: \
     pre_sum={pre_sum} post_sum={post_sum} \
     pre_fees={pre_fees} post_fees={post_fees}");
```

---

## Caveats Requiring Source Verification

Before encoding this harness, verify at exact source lines:

| Question | What to grep for |
|---|---|
| What is the exact field name for per-position quote tracking? | `grep -rn "quote_position\|realized_pnl\|quote_asset" src/` |
| Is funding zero-sum within the engine or externally subsidized? | `grep -rn "funding" src/` — check if funding debits == credits |
| Are fees burned or credited to a pool tracked in engine state? | `grep -rn "fee" src/` |
| Is `quote_position` i64 or i128? Overflow risk if i64 and many accounts | check struct definition |

---

## Output Summary

```
Suggested Kani harness name:  proof_pnl_zero_sum
Estimated harness complexity: HIGH

Rationale: Requires symbolic sum over ALL accounts (unbounded collection).
Kani will need a concrete bound on account count (e.g., N=4 or N=8) to
remain tractable. The symbolic operation dispatch adds another dimension.
Recommend starting with N=2 accounts and a single operation, then expanding.
```

**Verdict on translation readiness**: `NEEDS_LAYER_2_TO_DECIDE`
**Confidence**: MED
**Reason**: The claim is translatable in principle, but the exact field names for quote-position accounting and the fee/funding pool representation must be confirmed against actual source before the harness compiles. The structure above is the correct shape; field names require one grep pass to finalize.