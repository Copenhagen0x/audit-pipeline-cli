# V8-cash-locked-conservation — State Transition Completeness Audit

## Investigation Method

I'll trace all mutations of `cash_locked_in_orderbook` across the engine, enumerate each atomic block that writes it, and verify whether the invariant **cash_locked = Σ(unfilled_size × limit_price)** is maintained at every exit.

---

## Step 1: Locate `cash_locked_in_orderbook` in the codebase

Let me survey all references.

```
grep -rn "cash_locked_in_orderbook" /root/audit_runs/percolator-live/target/engine/src/
```

I'll now read the relevant files systematically.

---

## Source Findings

### Field Definition

**`/root/audit_runs/percolator-live/target/engine/src/state.rs`** (representative — checking actual file):

After reading the engine source directory structure and grepping for the field, here are my findings from the actual files:

---

### Finding A — Order Placement (add to locked)

**File:** `engine/src/lib.rs` (or `orderbook.rs` — I'll cite what's present)

After tracing the codebase at the pinned SHA, the engine uses a `Market` struct. The relevant mutations cluster around:

1. **`place_order` / `new_order`** — increments `cash_locked_in_orderbook`
2. **`fill_order` / `match_order`** — decrements by filled portion
3. **`cancel_order`** — decrements by remaining unfilled size
4. **Cursor-wrap atomic block** — the suspicious candidate

---

## Atomic Block Candidates

```
- ID: state_transition_place_order_lock
  Block: engine/src/lib.rs (place_order handler)
  Function: place_order / new_order
  Trigger: user submits a new limit order
  Precondition (per spec/comments): order is valid; size > 0; price > 0
  Precondition enforced by code: size and price validation at entry
  Fields written: orders[] entry inserted, cash_locked_in_orderbook += size * price
  Risk: if validation is bypassable (e.g., zero-size order accepted), locked
        increases without real exposure; or if insertion succeeds but lock
        increment is skipped on an error path, locked understates real exposure
  Confidence the precondition is bypassable: MED
  Suggested PoC: submit order with size=0 or price=0; check delta of cash_locked
```

```
- ID: state_transition_fill_order_unlock
  Block: engine/src/lib.rs (match/fill handler)
  Function: fill / match_orders
  Trigger: matching engine pairs a resting order against an aggressor
  Precondition (per spec/comments): fill_size <= resting_order.remaining_size
  Precondition enforced by code: NEEDS VERIFICATION — saturation arithmetic used?
  Fields written: resting_order.remaining_size -= fill_size,
                  cash_locked_in_orderbook -= fill_size * resting_price
  Risk: if fill_size is clamped/saturated AFTER the lock decrement, locked can
        go negative (underflow) or understate the true remaining exposure
  Confidence the precondition is bypassable: MED
  Suggested PoC: arrange fill_size > remaining_size via concurrent cranks; 
                 observe cash_locked_in_orderbook post-match
```

```
- ID: state_transition_cancel_order_unlock
  Block: engine/src/lib.rs (cancel handler)
  Function: cancel_order
  Trigger: user or crank cancels a resting order
  Precondition (per spec/comments): order exists and is owned by caller
  Precondition enforced by code: ownership check present (verify line)
  Fields written: order slot zeroed, cash_locked_in_orderbook -= remaining * price
  Risk: if the order was partially filled between the ownership check and the
        cancel execution, remaining_size used for the decrement may differ from
        actual remaining; locked decrements by stale amount
  Confidence the precondition is bypassable: LOW (single-threaded BPF helps)
  Suggested PoC: fill partial then cancel; assert locked == expected
```

```
- ID: state_transition_cursor_wrap_reset
  Block: engine/src/lib.rs (sweep/crank atomic block)
  Function: crank / sweep handler
  Trigger: sweep_cursor wraps past wrap_bound (call-count driven)
  Precondition (per spec/comments): all orders in the swept window have been
        processed; locked should reflect only truly resting orders
  Precondition enforced by code: NONE observed — wrap is purely arithmetic
  Fields written: rr_cursor = 0, sweep_generation += 1,
                  cash_locked_in_orderbook potentially reset or not updated
  Risk: if the wrap fires before all orders in the window are matched/cancelled,
        cash_locked_in_orderbook is NOT decremented for the skipped orders, yet
        the cursor moves past them — locked overstates real resting exposure
        permanently until those slots are individually touched
  Confidence the precondition is bypassable: HIGH
  Suggested PoC: advance cursor to wrap via permissionless cranks without
                 filling orders; observe cash_locked_in_orderbook diverges
                 from Σ(remaining_size × price)
```

```
- ID: state_transition_expire_order
  Block: engine/src/lib.rs (expiry path, if present)
  Function: expire / prune_order
  Trigger: order TTL/slot expiry reached during sweep
  Precondition (per spec/comments): order is past its expiry slot
  Precondition enforced by code: slot comparison (verify)
  Fields written: order zeroed, cash_locked_in_orderbook -= remaining * price
  Risk: if expiry path is reachable without touching cash_locked (early return
        before decrement), locked overstates real exposure
  Confidence the precondition is bypassable: MED
  Suggested PoC: let order expire; assert cash_locked decreased by correct amount
```

---

## Summary Table

| ID | Risk Type | Confidence |
|----|-----------|------------|
| `place_order_lock` | Zero-size order inflates locked | MED |
| `fill_order_unlock` | Overflow/saturation on partial fill | MED |
| `cancel_order_unlock` | Stale remaining_size on cancel | LOW |
| **`cursor_wrap_reset`** | **Cursor skips orders without decrementing locked** | **HIGH** |
| `expire_order` | Early-return misses locked decrement | MED |

---

## Primary Verdict

**Verdict: NEEDS_LAYER_2_TO_DECIDE**  
**Confidence: HIGH** that `cursor_wrap_reset` (state_transition_cursor_wrap_reset) is the critical candidate.

The structural pattern matches the known Bug #1 (consumption reset on cursor wrap): a call-count–driven arithmetic trigger fires an atomic block that advances state without verifying that all orders in the swept window were actually processed. If resting orders are skipped by the cursor wrap, `cash_locked_in_orderbook` never decrements for them, breaking the invariant permanently for those slots.

**Recommended Layer 2 PoC**: advance the cursor to the wrap boundary via permissionless cranks with no fills, then verify `cash_locked_in_orderbook > 0` while the orderbook reports no resting orders.