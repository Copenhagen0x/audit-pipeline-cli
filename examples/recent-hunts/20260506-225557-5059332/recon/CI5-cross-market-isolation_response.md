# CI5 — Cross-Market Isolation: Formal Property Definition

## Investigation

Let me first examine the actual engine source to identify relevant state structures and fields.

---

**Examining engine state and market structures:**

The repository at `/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/` needs to be surveyed for:
1. The market/state struct definitions
2. Any shared mutable state across markets
3. Operations that touch multiple markets

Let me trace this systematically.

---

### Step 1: Identify Variables Referenced

From the engine source (percolator `src/`), the claim references:

- **Market identity**: each market is identified by its own account/state struct instance
- **Vault accounts**: per-market vault balances
- **Order/position state**: per-market order books or position ledgers
- **Any global/shared mutable state** (e.g., a shared fee accumulator, global RR cursor, global sequence number)

Key fields to check:
- `Engine` struct fields that are indexed by market ID vs. fields that are global singletons
- Whether any arithmetic operation on market A's state writes to a field also read by market B

---

### Step 2: Formal Translation

```
Original claim:    "State changes in market A cannot affect accounts or vault
                   in market B. Markets are fully isolated."
Source:            CI5-cross-market-isolation (audit hypothesis)

Variables referenced:
  - market_a: Engine state for market A (distinct account/struct instance)
  - market_b: Engine state for market B (distinct account/struct instance)
  - vault_a / vault_b: per-market vault token balances
  - Any shared mutable global field (e.g., global_fee_pool, sequence_number,
    rr_cursor) — must enumerate after grep

Quantification:
  - For all reachable engine states where market_a and market_b are both
    initialized and distinct (market_a.id ≠ market_b.id)
  - After applying ANY state-mutating operation O on market_a
    (e.g., place_order, cancel_order, crank, deposit, withdraw)
  - The following holds:
      market_b.vault_balance == pre_vault_b
      AND market_b.open_interest == pre_open_interest_b
      AND market_b.order_state == pre_order_state_b
      AND (all other market_b fields) == their pre-operation values

Rust translation:

```rust
// Pre-state snapshot of market B
let pre_vault_b        = engine.market_b.vault_balance;
let pre_oi_b           = engine.market_b.open_interest;
// ... snapshot all market_b fields

// Symbolic operation on market A only
// (Kani makes op_args fully symbolic)
let result = engine.apply_operation_on_market_a(kani::any(), kani::any());
kani::assume(result.is_ok()); // restrict to non-panic paths

// Post: market B is entirely unchanged
assert_eq!(engine.market_b.vault_balance,   pre_vault_b);
assert_eq!(engine.market_b.open_interest,   pre_oi_b);
// ... assert all market_b fields
```

Suggested Kani harness name: `proof_cross_market_isolation`
Estimated harness complexity: MED–HIGH
```

---

### Step 3: Evidence Gathering — Shared State Risk Paths

**Critical grep targets** (to be verified before encoding the harness):

| Pattern | Risk if found |
|---|---|
| Any field on `Engine` (not on a per-market sub-struct) that is mutated during order/crank operations | Shared global state = isolation violation |
| A global fee accumulator written on every trade | Market A trade → global field → market B reads it |
| A single `rr_cursor` covering all markets | Advance on market A sweep could skip market B entries |
| Vault accounts passed by index into a flat array | Off-by-one → wrong market's vault debited |

---

### Step 4: Verdict on Formal Encodability

| Dimension | Assessment |
|---|---|
| **Precondition identifiable?** | YES — two distinct initialized market structs |
| **Operation set bounded?** | MED — must enumerate all mutating entrypoints |
| **Postcondition expressible?** | YES — field-by-field equality on market B snapshot |
| **Shared state risk known?** | NEEDS_LAYER_2_TO_DECIDE — grep for global fields required |

---

## Output

```
Original claim:    "State changes in market A cannot affect accounts or vault in market B."
Source:            CI5 audit hypothesis

Variables referenced:
  - market_{a,b}.vault_balance     (per-market vault, type: u64 or TokenAmount)
  - market_{a,b}.open_interest     (per-market OI accumulator)
  - market_{a,b}.order_book        (per-market order state)
  - [UNKNOWN] any Engine-level global fields shared across markets

Quantification:
  - For all reachable states, ∀ market_a ≠ market_b
  - After any mutating operation on market_a
  - All market_b fields equal their pre-operation values

Rust translation: (see above)

Suggested harness: proof_cross_market_isolation
Complexity: MED (if markets are structurally separate) → HIGH (if shared global fields exist)
```

**Verdict**: `NEEDS_LAYER_2_TO_DECIDE`
**Confidence**: MED
**Blocker**: Must grep engine source for global/shared mutable fields on the `Engine` struct before the harness can be finalized. If any such field is found that is written during per-market operations, the isolation claim is FALSE and the Kani harness will produce a counterexample.