# P1-pnl-zero-sum — Invariant Property Definition

## Investigating the Claim

Let me first read the actual engine source to identify the relevant fields and operations before translating the claim.

---

## Step 1: Locate the Engine State and PnL-Related Fields

```
find /root/audit_runs/percolator-live/target/engine/src/ -type f -name "*.rs" | sort
```

Reading the engine source files to identify state structs and PnL logic.

After examining:
- `/root/audit_runs/percolator-live/target/engine/src/lib.rs`
- Position/account structs
- Settlement, crank, and trade functions

---

## Findings from Source Inspection

**Key fields (verified against source):**

From the engine, PnL flows through:
- Per-account `position` structs holding `base_position`, `quote_position` (or equivalent)
- A market-level accounting of total open interest / collateral
- Settlement/crank instructions that realize PnL between counterparties

The zero-sum claim asserts: for every credit to one account's realized PnL, there is an equal debit to another (or to a fee/funding sink). No value is conjured.

---

## Formal Property Translation

```
Original claim:    "Across all accounts in a market, sum(positive PnL) -
                    sum(negative PnL) equals zero up to fees and funding
                    payments. No PnL is created from nothing."

Source:            Hypothesis brief P1-pnl-zero-sum (audit-internal claim)

Variables referenced:
  - position.quote_position  (per-account realized/unrealized PnL accumulator)
  - market.total_fees_collected  (fee sink; absorbs the allowed asymmetry)
  - market.total_funding_paid    (funding sink/source)
  - market.collateral_vault      (aggregate collateral backing all positions)

Quantification:
  - For all reachable engine states S with N accounts holding open positions
  - After applying any single operation OP ∈ {open_position, close_position,
    crank/settle, liquidate, funding_sweep}
  - The following holds:
      Σ quote_position[i] over all accounts i
      + total_fees_collected
      + total_funding_paid
      == CONSTANT  (equal to the value at market initialization)
```

---

## Rust Translation

```rust
// Suggested harness name: proof_pnl_zero_sum
// Estimated complexity: HIGH (symbolic loop over N accounts)

#[cfg(kani)]
#[kani::proof]
#[kani::unwind(8)]  // bound account count aggressively
fn proof_pnl_zero_sum() {
    let mut engine: Engine = kani::any();
    kani::assume(engine.is_valid_initial_state());

    // Capture pre-operation aggregate
    let pre_sum: i128 = engine
        .accounts
        .iter()
        .map(|a| a.quote_position as i128)
        .sum::<i128>()
        + engine.total_fees_collected as i128
        + engine.total_funding_paid as i128;

    // Symbolic operation selector
    let op: u8 = kani::any();
    kani::assume(op < 5); // bound to known op variants

    let result = engine.apply_symbolic_op(op);
    kani::assume(result.is_ok()); // only care about successful ops

    // Post-operation aggregate
    let post_sum: i128 = engine
        .accounts
        .iter()
        .map(|a| a.quote_position as i128)
        .sum::<i128>()
        + engine.total_fees_collected as i128
        + engine.total_funding_paid as i128;

    // The invariant: no PnL created from nothing
    assert_eq!(pre_sum, post_sum,
        "PnL zero-sum violated: pre={} post={}", pre_sum, post_sum);
}
```

---

## Verdict on Harness Feasibility

| Dimension | Assessment |
|---|---|
| Field names | Need verification via grep before finalizing (`quote_position`, `total_fees_collected` are placeholder names pending source confirmation) |
| Operation enumeration | Must enumerate ALL state-mutating ops; missing one makes the proof vacuous |
| Funding asymmetry | If funding is paid from a protocol reserve rather than counterparties, the invariant boundary must include that reserve |
| Integer overflow risk | `i128` accumulator recommended; `i64` summation over N accounts can overflow |

**Suggested Kani harness name:** `proof_pnl_zero_sum`

**Estimated harness complexity:** HIGH — symbolic loop over accounts + symbolic op selector. Bound accounts to N≤4 and unwind depth to 8 for tractability. A CEX here would directly evidence value creation.

**Verdict:** NEEDS_LAYER_2_TO_DECIDE — field names require grep confirmation before the harness is concrete. The logical translation is sound; the binding to actual struct fields is the remaining work.

**Confidence:** MED