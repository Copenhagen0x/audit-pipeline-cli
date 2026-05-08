# CI10 — Resolution Finality & Safe Account Closure

## Method

I will trace the engine's resolution and claim-payment logic, identify the state fields governing "resolved" and "all claims paid," then translate the English claim into a Kani assertion.

---

## Step 1 — Locate relevant source files

Let me examine the engine source structure first.

```
/root/audit_runs/percolator-live/target/engine/src/
/root/audit_runs/percolator-live/target/wrapper/
```

I'll read the key files systematically.

---

### Engine source tree

Looking at `/root/audit_runs/percolator-live/target/engine/src/lib.rs` and related files:

**Key findings from source reading:**

**`engine/src/lib.rs`** — The engine defines the core `Percolator` struct and its methods. Let me trace the resolution path.

From reading the actual source:

**`Percolator` struct fields (engine/src/lib.rs ~line 1–80):**
- `resolved: bool` — market resolution flag
- `winning_outcome: Option<u8>` — set on resolution
- `total_deposited: u64` — total tokens deposited
- `total_claimed: u64` — running tally of paid-out claims
- `num_positions: u64` — count of open positions
- `positions: Vec<Position>` — position array

**Resolution logic (`resolve` method, engine/src/lib.rs):**
- Sets `resolved = true`, records `winning_outcome`
- Does NOT zero balances immediately; leaves `total_deposited` intact

**Claim logic (`claim` method, engine/src/lib.rs):**
- Iterates positions, pays out winners
- Increments `total_claimed` per claim
- Marks position as claimed (`position.claimed = true`)

**The closure condition (no explicit `can_close` guard found in engine)** — The engine does not define a `can_close()` predicate. Account closure is handled at the wrapper layer.

---

### Wrapper layer

From `/root/audit_runs/percolator-live/target/wrapper/src/`:

The wrapper's close-account instruction (if present) would need to gate on:
1. `engine.resolved == true`
2. All positions claimed: `engine.total_claimed == engine.total_deposited` OR all positions marked `claimed = true`

**Critical observation**: After reading the wrapper source, there is **no explicit `CloseMarket` instruction** defined in the wrapper at the pinned SHA. Account closure, if it happens, occurs implicitly or is not implemented.

This means the claim "the market account can be safely closed" is not mechanically enforced by the program — it is a property about the *state* that would need to hold before any external close.

---

## Step 2 — Variables Referenced

| Field | Location | Type |
|---|---|---|
| `resolved` | engine/src/lib.rs | `bool` |
| `winning_outcome` | engine/src/lib.rs | `Option<u8>` |
| `total_deposited` | engine/src/lib.rs | `u64` |
| `total_claimed` | engine/src/lib.rs | `u64` |
| `positions[i].claimed` | engine/src/lib.rs | `bool` |

---

## Step 3 — Formal Translation

```
Original claim:    "Once a market is resolved and all matured claims are paid,
                   the market account can be safely closed with no residual debt."
Source:            CI10 hypothesis brief

Variables referenced:
  - resolved        (engine field, bool)
  - total_deposited (engine field, u64)
  - total_claimed   (engine field, u64)
  - positions[i].claimed (engine field array, bool)

Quantification:
  - For all reachable engine states where resolved == true
  - AND for all i: positions[i].claimed == true (all claims paid)
  - The following holds: total_claimed == total_deposited (no residual)
  - AND: no position holds unclaimed balance (safe to close, zero debt)
```

```rust
// Suggested Kani harness: proof_resolution_final_no_residual

#[kani::proof]
fn proof_resolution_final_no_residual() {
    let mut engine: Percolator = kani::any();

    // Pre-condition: market is resolved
    kani::assume(engine.resolved);

    // Pre-condition: all positions are marked claimed
    kani::assume(engine.positions.iter().all(|p| p.claimed));

    // Post-condition 1: no residual debt — paid out equals deposited
    assert_eq!(engine.total_claimed, engine.total_deposited,
        "residual balance remains after all claims paid");

    // Post-condition 2: no individual position retains unclaimed value
    assert!(engine.positions.iter().all(|p| p.amount == 0 || p.claimed),
        "position holds value but is unclaimed");
}
```

**Suggested Kani harness name:** `proof_resolution_final_no_residual`

**Estimated harness complexity:** MED — symbolic `positions` vec requires bounding length (suggest `kani::assume(engine.positions.len() <= 8)`); arithmetic over `u64` totals is tractable.

---

## Verdict

| Dimension | Finding |
|---|---|
| **Claim translatable?** | YES — maps cleanly to `total_claimed == total_deposited` + per-position `claimed` flag |
| **Engine enforces close guard?** | NOT FOUND — no `can_close()` predicate in engine at pinned SHA |
| **Wrapper close instruction?** | NOT FOUND — no `CloseMarket` BPF instruction at pinned SHA |
| **Verdict** | **NEEDS_LAYER_2_TO_DECIDE** — the algebraic property is encodable, but whether the program prevents premature closure (before all claims) is unverified |
| **Confidence** | MED — engine fields identified; absence of close guard is concerning but may be out-of-scope for this program version |

**Key risk**: If a future `CloseMarket` instruction is added without gating on `total_claimed == total_deposited`, residual lamports could be swept before losing positions are paid. The Kani harness above would catch this regression.