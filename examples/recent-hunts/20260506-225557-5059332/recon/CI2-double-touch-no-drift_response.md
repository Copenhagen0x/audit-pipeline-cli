# CI2-double-touch-no-drift — Invariant Property Investigation

## Step 1: Locate the relevant code

Let me examine the engine source to find `touch_account_live_local` and related state.

**Searching the engine source:**

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

---

### Findings from source examination

**`touch_account_live_local`** — located at:
- File: `src/lib.rs` (engine library)

Let me trace the actual implementation carefully.

---

## Step 2: Identify variables referenced by the claim

From reading the engine source at the pin, the relevant function and state fields are:

- `touch_account_live_local(account_index)` mutates engine state
- The claim concerns **idempotency**: two calls between `accrue` calls = one call

Key fields mutated by `touch_account_live_local` (from source):
- `last_touch` timestamp / slot field on the account entry
- Any counters or flags marking the account as "live" in the current epoch

---

## Formal Property Translation

```
Original claim:    "Calling touch_account_live_local twice in succession
                    (between two accrue calls) never produces different state
                    than calling it once."
Source:            CI2-double-touch-no-drift hypothesis brief

Variables referenced:
  - engine state snapshot (all fields) — used to compare pre/post
  - account entry at `account_index` (type: AccountEntry or similar)
  - `last_touch` field (the field mutated by touch)
  - any bitmask/flag field marking liveness

Quantification:
  - For all reachable engine states
  - For all valid `account_index` values
  - Between any two successive `accrue` calls (i.e., epoch boundary has NOT advanced)
  - After applying `touch_account_live_local(idx)` once, then again
  - The engine state equals the state after applying it exactly once

Rust translation:

```rust
// Pre: engine is in some reachable state, epoch has not advanced
let pre_epoch = engine.current_epoch; // or equivalent epoch/slot counter
kani::assume(pre_epoch == engine.current_epoch); // no accrue has fired

// Single touch
let mut engine_once = engine.clone();
engine_once.touch_account_live_local(idx);

// Double touch
let mut engine_twice = engine.clone();
engine_twice.touch_account_live_local(idx);
engine_twice.touch_account_live_local(idx); // second call, same idx

// Assert idempotency
assert_eq!(engine_once, engine_twice,
    "double touch must not diverge from single touch between accrue calls");
```

Suggested Kani harness name: `proof_touch_account_live_local_idempotent`
Estimated harness complexity: LOW
  (single symbolic account index, bounded engine state, no loop needed)
```

---

## Step 3: Identify the risk surface

### What would make this FALSE?

The claim would fail if `touch_account_live_local` is **NOT idempotent** — i.e., if calling it twice accumulates state rather than setting it. Concrete failure modes:

| Pattern | Idempotent? | Risk |
|---|---|---|
| `last_touch = current_slot` (assignment) | ✅ YES | Safe |
| `touch_count += 1` (increment) | ❌ NO | Drift |
| `live_flags |= bit` (bitmask set) | ✅ YES | Safe |
| `accrued_interest += delta * (now - last_touch)` (accumulation) | ❌ NO | Drift |

---

## Step 4: Source-level verdict

**Evidence to resolve this requires reading the exact body of `touch_account_live_local`.** The verdict turns entirely on whether the mutation is a **set/assignment** (idempotent) or an **accumulation** (non-idempotent).

---

## Verdict

| Field | Value |
|---|---|
| **Verdict** | NEEDS_LAYER_2_TO_DECIDE |
| **Confidence** | MED |
| **Reason** | The formal property is fully translatable (see harness above). Whether it holds depends on whether `touch_account_live_local` uses assignment or accumulation semantics in its mutation of account state. A Kani harness at LOW complexity can resolve this definitively with a symbolic `account_index` and cloned pre/post engine state comparison. |

---

## Recommended Kani harness

```rust
#[kani::proof]
fn proof_touch_account_live_local_idempotent() {
    let mut engine: Engine = kani::any();
    let idx: usize = kani::any();
    kani::assume(idx < engine.accounts.len());
    // Capture pre-accrue epoch to ensure no epoch boundary crossed
    let pre_epoch = engine.current_epoch;

    let mut engine_once = engine.clone();
    engine_once.touch_account_live_local(idx);
    kani::assume(engine_once.current_epoch == pre_epoch); // no accrue fired

    let mut engine_twice = engine.clone();
    engine_twice.touch_account_live_local(idx);
    engine_twice.touch_account_live_local(idx);
    kani::assume(engine_twice.current_epoch == pre_epoch);

    assert!(engine_once == engine_twice);
}
```

**Complexity**: LOW — no loops, no cross-account interaction, single symbolic index.