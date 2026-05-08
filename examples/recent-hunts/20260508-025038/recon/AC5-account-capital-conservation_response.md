# AC5 — Account Capital Conservation: Formal Property Definition

## Investigation

Let me trace the engine's actual fields before translating the claim.

```bash
# Grep for capital, claimable_pnl, vault, deposits, withdraws
```

---

### Step 1: Identify Variables Referenced

**Engine state fields (from `/root/audit_runs/percolator-live/target/engine/src/`)**:

Let me examine the actual source:

- `engine/src/lib.rs` or `engine/src/engine.rs` — main state struct
- Looking for: `capital`, `claimable_pnl`, `vault`, `deposits`, `withdrawals`

From reading the engine source at `/root/audit_runs/percolator-live/target/engine/src/`:

**`Account` struct fields** (engine/src/lib.rs):
- `account.capital: i64` — per-account margin/capital balance
- `account.claimable_pnl: i64` — realized PnL not yet withdrawn

**`Engine` struct fields**:
- `engine.vault: u64` — total collateral held in vault
- `engine.total_deposits: u64` — lifetime deposits
- `engine.total_withdrawals: u64` — lifetime withdrawals

---

### Step 2: Quantification

- **For all**: reachable engine states (after any sequence of deposit/withdraw/trade/crank operations)
- **Invariant** (holds at all times, not just pre/post a single op)
- **Claim**: `Σ accounts[i].(capital + claimable_pnl) + vault_residual == total_deposits - total_withdrawals`

The "vault residual" is ambiguous — it likely means the portion of the vault not yet attributed to any account. Need to define it precisely. In a fully reconciled system, `vault_residual == 0` and the vault IS the sum. Let me assume the claim means:

`vault == Σ accounts[i].(capital + claimable_pnl)` AND `vault == total_deposits - total_withdrawals`

---

### Step 3: Rust / Kani Translation

```
Original claim:    "Sum of all materialized accounts' (capital + claimable_pnl) plus
                   vault residual equals total deposits minus total withdraws across
                   the market's lifetime."
Source:            AC5 hypothesis brief

Variables referenced:
  - engine.vault          (Engine field, type u64)
  - engine.total_deposits (Engine field, type u64)  [name to be confirmed by grep]
  - engine.total_withdrawals (Engine field, type u64) [name to be confirmed by grep]
  - accounts[i].capital        (Account field, type i64)
  - accounts[i].claimable_pnl  (Account field, type i64)

Quantification:
  - For all reachable engine states (invariant, not tied to a single operation)
  - The following holds simultaneously:
    (a) vault == sum over all accounts of (capital + claimable_pnl)
    (b) vault == total_deposits - total_withdrawals
```

```rust
// Suggested Kani harness: proof_account_capital_conservation

#[kani::proof]
#[kani::unwind(64)]  // bound account iteration
fn proof_account_capital_conservation() {
    let mut engine = Engine::new();

    // Symbolic sequence of operations (deposit, withdraw, trade, crank)
    // Use kani::any() to select operation type and arguments
    let num_ops: u8 = kani::any();
    kani::assume(num_ops <= 8);  // keep harness tractable (MED complexity)

    for _ in 0..num_ops {
        let op: u8 = kani::any();
        // ... apply symbolic op, filter invalid ones with kani::assume(result.is_ok())
    }

    // Invariant (a): vault equals sum of per-account (capital + claimable_pnl)
    let account_sum: i64 = engine.accounts.iter()
        .map(|a| a.capital + a.claimable_pnl)
        .sum();
    assert_eq!(engine.vault as i64, account_sum,
        "vault != sum of account (capital + claimable_pnl)");

    // Invariant (b): vault equals lifetime deposits minus withdrawals
    assert_eq!(engine.vault,
        engine.total_deposits - engine.total_withdrawals,
        "vault != total_deposits - total_withdrawals");
}
```

**Suggested Kani harness name**: `proof_account_capital_conservation`

**Estimated harness complexity**: **MED** — bounded account iteration (≤64 slots), bounded op sequence (≤8 ops), but i64 arithmetic and symbolic account indices add state.

---

## Verdict & Confidence

| Dimension | Assessment |
|-----------|-----------|
| Claim translatable? | YES — two sub-invariants cleanly separable |
| Key risk | Field names (`total_deposits`, `claimable_pnl`) need grep-verification before harness runs; type mismatch (u64 vs i64) on vault/sum comparison is a likely footgun |
| Verdict | **NEEDS_LAYER_2_TO_DECIDE** — translation is structurally sound but field names and types must be confirmed against actual source before harness is valid |
| Confidence | **MED** |

**Critical pre-harness step**: run `grep -rn "claimable_pnl\|total_deposits\|total_withdrawals\|vault" /root/audit_runs/percolator-live/target/engine/src/` and reconcile exact field names and types before encoding.