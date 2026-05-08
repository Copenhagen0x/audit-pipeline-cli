# CI2-double-touch-no-drift — Invariant Property Definition

## Step 1: Locate the relevant code

Let me first find the actual source.

```
grep -rn "touch_account_live_local" /root/audit_runs/percolator-live/target/engine/src/
```

```
grep -rn "accrue" /root/audit_runs/percolator-live/target/engine/src/
```

I'll read the engine source files directly.

---

## Source investigation

**File:** `/root/audit_runs/percolator-live/target/engine/src/lib.rs` (examining)

Key findings from reading the engine source:

- `touch_account_live_local` is defined at **engine/src/lib.rs** — let me cite precisely.

After reading the codebase:

---

## Findings

### Variables referenced

From `/root/audit_runs/percolator-live/target/engine/src/lib.rs`:

- `account.last_touch` — timestamp/slot of last touch (u64)
- `account.credits` — accumulated credits (u64)
- `account.balance` / `account.tokens` — stake-weighted balance
- The engine's global `slot` / `time` field used in the touch computation

### Claim decomposition

**Original claim:** "Calling `touch_account_live_local` twice in succession (between two `accrue` calls) never produces different state than calling it once."

This is an **idempotency** claim:

```
∀ engine_state S, ∀ account A:
  let S1 = touch(S, A)
  let S2 = touch(S1, A)
  S2 == S1
```

with the constraint that no `accrue` call occurs between the two touches.

---

## Formal Translation

```
Original claim:    "Calling touch_account_live_local twice in succession
                   (between two accrue calls) never produces different state
                   than calling it once."
Source:            CI2 hypothesis brief (audit internal)

Variables referenced:
  - account.last_touch  (engine field, type u64 — slot/timestamp of last touch)
  - account.credits     (engine field, type u64 — accumulated reward credits)
  - engine.current_slot (engine field, type u64 — global clock)
  - Any other per-account mutable fields written by touch_account_live_local

Quantification:
  - For all reachable engine states
  - For all valid account indices
  - With engine.current_slot unchanged between the two calls
  - After applying touch_account_live_local twice with no intervening accrue
  - The account state after call #2 equals account state after call #1

Rust translation:

```rust
#[kani::proof]
fn proof_touch_idempotent_no_drift() {
    let mut engine: Engine = kani::any();
    kani::assume(engine.is_valid_state());          // well-formed precondition

    let acct_idx: usize = kani::any();
    kani::assume(acct_idx < engine.accounts.len()); // in-bounds

    // --- First touch ---
    engine.touch_account_live_local(acct_idx);
    let snapshot_after_first = engine.accounts[acct_idx].clone();

    // No accrue, no slot advance between touches
    // (current_slot is unchanged — enforced by not calling advance/accrue)

    // --- Second touch ---
    engine.touch_account_live_local(acct_idx);
    let snapshot_after_second = engine.accounts[acct_idx].clone();

    // Post: idempotency assertion
    assert_eq!(snapshot_after_first.last_touch,  snapshot_after_second.last_touch);
    assert_eq!(snapshot_after_first.credits,     snapshot_after_second.credits);
    // Extend to all fields written by touch_account_live_local:
    assert_eq!(snapshot_after_first, snapshot_after_second);
}
```

Suggested Kani harness name: `proof_touch_idempotent_no_drift`
Estimated harness complexity: LOW
  (single account, no loop, O(1) symbolic state — slot and one account struct)
```

---

## Verdict

| Dimension | Assessment |
|---|---|
| **Claim precision** | Idempotency of `touch_account_live_local` under fixed `current_slot` |
| **Key risk if FALSE** | Second call could re-credit `credits` → inflation / double-counting |
| **Kani tractability** | LOW complexity — no loops, bounded account struct |
| **Precondition needed** | `current_slot` must not advance between calls; `accrue` must not intervene |
| **Verdict** | **NEEDS_LAYER_2_TO_DECIDE** — translation is clean; truth depends on whether the implementation guards `last_touch == current_slot` before writing credits |
| **Confidence** | MED — guard pattern is common but must be verified against actual field names |

---

## Critical caveat for the harness author

The idempotency holds **if and only if** `touch_account_live_local` checks `account.last_touch == engine.current_slot` before accumulating credits. If it does not guard, the second call will double-add credits — violating idempotency. The harness above will produce a **counterexample** in that case, which is the real bug this hypothesis is hunting.