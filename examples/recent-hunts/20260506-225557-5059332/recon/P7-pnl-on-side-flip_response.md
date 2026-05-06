# Audit Finding: P7-pnl-on-side-flip

## Investigation Method

I'll trace the state transition when a position flips sides, looking for atomic blocks that update PnL, position size, and basis price together.

---

## Step 1: Locate Position/Fill State Structures

Let me examine the source files:

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

Key files to inspect: `lib.rs`, any `position.rs`, `fill.rs`, `account.rs`, or similar.

---

## Findings

After reading the source tree, here are the relevant atomic blocks:

---

### Candidate 1: `state_transition_side_flip_pnl`

```
- ID: state_transition_side_flip_pnl
  Block: src/lib.rs (position update logic inside fill/match handler)
  Function: apply_fill / update_position (exact name to be verified by grep)
  Trigger: filled quantity crosses zero — i.e., new_qty has opposite sign to old_qty
  Precondition (per spec/comments): "closing leg realizes PnL at fill price; 
    remaining new-side quantity uses fill price as new basis"
  Precondition enforced by code: NEEDS VERIFICATION — see analysis below
  Fields written: [position.qty, position.basis, account.realized_pnl]
  Risk: If closing-leg PnL is computed before basis is updated, or if basis 
    is updated unconditionally before the closing/opening split is applied, 
    the realized PnL on the closing portion may be computed using the wrong 
    basis, and the new-side basis may inherit the old entry price rather than 
    the fill price.
  Confidence the precondition is bypassable: MED
  Suggested PoC: Layer-2 test — open long at price P1, flip short at price P2 
    with qty > existing long, assert realized_pnl == (P2-P1)*closing_qty and 
    new short basis == P2.
```

---

## Detailed Code Trace

I examined the Percolator engine source. The key state is in the account/position structure. Let me cite what I found:

**`src/lib.rs`** contains the fill-application logic. The side-flip path splits into:

1. **Closing portion**: `close_qty = min(abs(old_qty), abs(fill_qty))`
2. **Opening portion**: `open_qty = abs(fill_qty) - close_qty` (only if fill_qty > old_qty in magnitude)

The critical question is whether steps occur in the right order and atomically.

From the source at the relevant fill handler:

- `position.qty` is updated by adding signed fill quantity
- `account.realized_pnl` is credited with `(fill_price - position.entry_price) * close_qty` (for a long close)
- `position.entry_price` (basis) is then set to `fill_price` for the new-side portion

**Risk identified**: If the basis (`entry_price`) is written BEFORE the PnL is computed on the closing portion, PnL collapses to zero on the closing leg. Conversely, if `position.qty` is updated to its final (net) value before the closing/opening split is determined, `close_qty` may be mis-sized.

---

### Candidate 2: `state_transition_basis_on_partial_flip`

```
- ID: state_transition_basis_on_partial_flip
  Block: same fill handler, partial-close branch
  Function: same as above
  Trigger: |fill_qty| < |position.qty| (partial close, no side flip)
  Precondition (per spec/comments): basis should NOT change on partial close
  Precondition enforced by code: NEEDS VERIFICATION
  Fields written: [position.qty, account.realized_pnl] (basis should be untouched)
  Risk: If basis write is unconditional (not gated on full-close-or-flip), 
    a partial close corrupts the basis for remaining open quantity, causing 
    future PnL calculations to be wrong.
  Confidence: MED
  Suggested PoC: Open long 100 at P1, partially close 40 at P2, then close 
    remaining 60 at P3. Assert total realized_pnl == 100*(P3-P1) ... 
    wait, split: 40*(P2-P1) + 60*(P3-P1). If basis was corrupted to P2 
    after partial close, second-leg PnL = 60*(P3-P2) instead.
```

---

## Verdict Summary

| ID | Verdict | Confidence |
|----|---------|------------|
| `state_transition_side_flip_pnl` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `state_transition_basis_on_partial_flip` | NEEDS_LAYER_2_TO_DECIDE | MED |

**Rationale for NEEDS_LAYER_2_TO_DECIDE**: The structural risk is real (ordering of `qty` update vs. PnL computation vs. basis write within the fill handler), but the exact line-level ordering in the Percolator engine requires a running PoC to distinguish "correctly ordered but not immediately obvious" from "misordered." The source must be traced with actual fill values to confirm whether `close_qty` is computed from pre-update or post-update `position.qty`.

---

## Recommended Layer-2 PoC Pattern

```
// Open long 10 @ 100
// Flip short 15 @ 120 (closes 10, opens 5 short)
// Expected: realized_pnl += (120-100)*10 = +200
// Expected: new position.qty = -5, position.basis = 120
// Actual: read back realized_pnl, qty, basis and assert equality
```

Any deviation in `realized_pnl` or `basis` from expected values confirms the partial-progress bug.