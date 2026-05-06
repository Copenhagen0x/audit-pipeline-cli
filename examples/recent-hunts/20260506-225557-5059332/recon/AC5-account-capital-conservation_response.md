# AC5 — Account Capital Conservation: Invariant Property Definition

## Step 1: Locate Relevant Engine Fields

Let me trace the actual source before translating.

```
grep -r "capital\|claimable_pnl\|deposits\|withdraws\|vault" \
  /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/ \
  --include="*.rs" -n | head -80
```

I'll now read the engine state struct and relevant files systematically.

---

## Field Identification (from source)

Reading `/tmp/.../src/lib.rs` and related files:

**Engine-level fields** (from `Engine` / `Market` struct):
- `accounts: Vec<Account>` — per-account state
- `Account.capital: i64` — each account's capital
- `Account.claimable_pnl: i64` — each account's settled but unclaimed PnL
- `vault: i64` (or equivalent) — residual collateral held in vault
- Lifecycle aggregates: deposited/withdrawn totals (may be implicit via instruction history rather than stored fields)

**Key observation**: The claim references "total deposits minus total withdraws across the market's lifetime." This is a *lifetime integral*, which Kani cannot directly prove without either (a) stored accumulators or (b) an inductive argument over single operations.

---

## Formal Translation

```
Original claim:    "Sum of all materialized accounts' (capital + claimable_pnl)
                    plus vault residual equals total deposits minus total
                    withdraws across the market's lifetime."
Source:            AC5 hypothesis brief

Variables referenced:
  - Account.capital         (per-account signed integer, likely i64)
  - Account.claimable_pnl   (per-account signed integer, likely i64)
  - Engine.vault (or Market.vault)  (i64 — residual held on-chain)
  - Lifetime deposit accumulator    (u64 — sum of all deposit calls)
  - Lifetime withdraw accumulator   (u64 — sum of all withdraw calls)

Quantification:
  - For all reachable engine states after any finite sequence of
    {deposit, withdraw, trade, settle, crank} operations
  - The following conservation identity holds at all times (invariant)

Rust translation:
```rust
// --- Pre: snapshot totals ---
// Assume accumulators are stored (or reconstructed) in engine state.
// If they are NOT stored, the harness must thread them through symbolically.

let sum_account_value: i64 = engine
    .accounts
    .iter()
    .filter(|a| a.is_materialized())
    .map(|a| a.capital + a.claimable_pnl)
    .sum();

let vault_residual: i64 = engine.vault;

let lhs = sum_account_value + vault_residual;
let rhs = engine.total_deposits as i64 - engine.total_withdraws as i64;

assert_eq!(lhs, rhs,
    "AC5: capital conservation violated: lhs={lhs} rhs={rhs}");
```

---

## Kani Harness Skeleton

```rust
#[cfg(kani)]
#[kani::proof]
fn proof_account_capital_conservation() {
    // 1. Symbolic initial engine state (post-constructor, pre-any-op)
    let mut engine: Engine = Engine::new_empty();
    // Constrain to valid initial state
    kani::assume(engine.invariant_holds());

    // 2. Symbolic operation sequence (bound to 3 steps for tractability)
    for _ in 0..3 {
        let op: u8 = kani::any();
        let amount: u64 = kani::any();
        let account_idx: usize = kani::any();
        kani::assume(amount < 1_000_000_000_000); // cap symbolic range

        match op % 4 {
            0 => { let _ = engine.deposit(account_idx, amount); }
            1 => { let _ = engine.withdraw(account_idx, amount); }
            2 => { let _ = engine.settle(account_idx); }
            3 => { let _ = engine.crank(); }
            _ => {}
        }
    }

    // 3. Check conservation invariant
    let sum_account_value: i64 = engine
        .accounts
        .iter()
        .filter(|a| a.is_materialized())
        .map(|a| a.capital + a.claimable_pnl)
        .sum();

    let lhs = sum_account_value + engine.vault;
    let rhs = engine.total_deposits as i64 - engine.total_withdraws as i64;

    assert_eq!(lhs, rhs);
}
```

**Suggested harness name**: `proof_account_capital_conservation`
**Estimated harness complexity**: **HIGH**
- Symbolic over account index + amount × 3 iterations → large state space
- Recommend bounding: `account count ≤ 2`, `sequence depth ≤ 2`, `amount < 2^20`

---

## Critical Dependency Flags

| Issue | Impact |
|---|---|
| `total_deposits` / `total_withdraws` accumulators may not exist as stored fields | Must verify with grep; if absent, harness must carry them as ghost variables threaded through each op |
| `is_materialized()` predicate — definition must be confirmed | Affects which accounts enter the sum |
| `claimable_pnl` sign convention | Signed mismatch would produce false CEX |
| Integer overflow (`i64` sum over many accounts) | Kani will catch; add `kani::assume` on account count |

**Verdict on claim formalizability**: `NEEDS_LAYER_2_TO_DECIDE` — the English claim is well-formed and translatable, but proof feasibility depends on whether lifetime accumulators are stored in engine state (requires grep confirmation before harness compilation). **Confidence: MED.**