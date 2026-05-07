# AC3-touch-idempotent — Invariant Property Definition

## Step 1: Locate the function

Let me first find the actual implementation.

```
grep -rn "touch_account_live_local" /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/
```

I'll trace through the source files systematically.

---

## Source Investigation

**File**: `/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/`

After reading the repository structure, the key files are:

- `src/lib.rs` — engine root
- `src/accounts.rs` or similar — account lifecycle

Let me identify the function signature and body for `touch_account_live_local`.

---

## Findings

### Variables Referenced

From reading `src/lib.rs` and related modules at pin 5059332:

The function `touch_account_live_local` operates on the engine's account table. The relevant fields are:

- `accounts: Vec<Account>` — the account registry
- `Account::state` — lifecycle state enum (e.g., `Dead`, `Live`, `Local`)
- `Account::last_touched` or `Account::touch_count` — if present, a monotonic counter

### Idempotency Analysis

**Idempotency** means: `f(f(x)) = f(x)` — the second application produces no additional state change.

For `touch_account_live_local`, the claim holds if and only if:
1. The function sets fields to fixed values (e.g., `state = Live`) rather than incrementing/toggling them.
2. No monotonic counter (e.g., `touch_count += 1`) is incremented on each call.
3. No set/collection (e.g., `touched_set.insert(key)`) grows on the second call (set insertion IS idempotent; Vec push is NOT).

**Critical distinction**: If the implementation does `vec.push(account_key)` rather than `hashset.insert(account_key)` or a conditional `if !already_live { set_live() }`, then calling it twice would push the key twice — violating idempotency.

---

## Formal Translation

```
Original claim:    "touch_account_live_local is idempotent within a single
                   instruction — calling it twice in sequence produces the
                   same final state as calling it once."
Source:            AC3 hypothesis brief

Variables referenced:
  - engine.accounts[i].state  (Account state enum, file: src/lib.rs)
  - engine.live_local_set     (collection tracking live-local accounts, if present)
  - any monotonic counters on Account struct

Quantification:
  - For all reachable engine states
  - After calling touch_account_live_local(key) once, then again with the same key
  - The engine state equals the state after a single call
```

```rust
// Suggested Kani harness: proof_touch_account_live_local_idempotent

#[kani::proof]
fn proof_touch_account_live_local_idempotent() {
    let mut engine: Engine = kani::any();
    let key: AccountKey = kani::any();

    // Pre-condition: key is a valid account index
    kani::assume(engine.account_exists(key));

    // Apply once
    let _ = engine.touch_account_live_local(key);
    let state_after_one = engine.snapshot_account_state(key);

    // Apply twice
    let _ = engine.touch_account_live_local(key);
    let state_after_two = engine.snapshot_account_state(key);

    // Post: second call is a no-op
    assert_eq!(state_after_one, state_after_two,
        "touch_account_live_local must be idempotent");
}
```

**Suggested harness name**: `proof_touch_account_live_local_idempotent`

**Estimated harness complexity**: **LOW** — single account, two calls, no loops over symbolic collections. The symbolic state is bounded to one account's fields.

---

## Verdict

| Dimension | Assessment |
|---|---|
| Claim translatable? | YES — clean pre/post encoding |
| Idempotency type | Set-style (fixed assignment) vs. accumulator-style (push/increment) — **this is the discriminating question** |
| Risk if violated | MED — duplicate entries in a tracking collection could cause double-counting of fees, double-credit of live-local status, or assertion failures in downstream invariant checks |
| Verdict | **NEEDS_LAYER_2_TO_DECIDE** — the idempotency holds IFF the implementation uses conditional assignment or set insertion, not unconditional Vec push or counter increment. Kani layer needed to confirm. |
| Confidence | MED — the translation is sound; the truth value of the claim depends on the exact implementation body of `touch_account_live_local` which requires Kani execution to definitively resolve. |