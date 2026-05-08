# V1-vault-residual-conservation — Formal Property Definition

## Original Claim

> The post-haircut residual cash (`vault - cash_locked_in_orderbook - claimable_pnl - insurance_counter`) is conserved across every internal accounting helper. Specifically: if any helper shrinks the insurance counter, it MUST also debit the vault by the same amount.

**Source:** Audit hypothesis V1 (internal)

---

## Step 1: Variables Referenced

| Field name | Type | Evidence |
|---|---|---|
| `self.vault` | `U128` | percolator.rs:2288–2290 |
| `self.insurance_fund.balance` | `U128` | percolator.rs:4815–4818 |
| `self.c_tot` | `U128` | percolator.rs:2283–2285 (`c_tot` = "cash locked in orderbook") |
| `self.explicit_unallocated_protocol_loss` | `U128` | percolator.rs:4830–4834 |

**"claimable_pnl"** is not a single named field. The code uses `pnl_matured_pos_tot` and related fields; for this property, `c_tot` (total user collateral locked) is the closest to "cash locked in orderbook" as seen at percolator.rs:2283–2285.

**Residual** as computed in-code (percolator.rs:2287–2291):
```
residual = vault - (c_tot + insurance_fund.balance)
```

---

## Step 2: Operation(s) Quantified Over

Three helpers mutate `insurance_fund.balance`:

1. **`use_insurance_buffer`** (percolator.rs:4811–4821): decrements `insurance_fund.balance` by `pay = min(loss, ins_bal)`. Does **not** touch `vault`.
2. **`absorb_protocol_loss`** (percolator.rs:4845–4850): calls `use_insurance_buffer`, then `record_uninsured_protocol_loss`. Does **not** touch `vault`.
3. **`record_uninsured_protocol_loss`** (percolator.rs:4825–4839): increments `explicit_unallocated_protocol_loss`. Does **not** touch `vault` or `insurance_fund.balance`.

---

## Step 3: Timing

This is a **post-condition** (after each helper call): the residual must not have changed.

**Precondition:** `vault >= c_tot + insurance_fund.balance` (the engine is in a non-corrupt state, as checked at percolator.rs:2290–2291).

**Postcondition:** `vault_post - (c_tot_post + insurance_fund_balance_post) == vault_pre - (c_tot_pre + insurance_fund_balance_pre)`

---

## Step 4: Conservation Analysis

For `use_insurance_buffer`:
- `vault` unchanged
- `insurance_fund.balance` decreases by `pay`
- `c_tot` unchanged
- **Residual increases by `pay`** — conservation is **VIOLATED**

This is the core finding: the residual is NOT conserved. When `use_insurance_buffer` debits the insurance fund without debiting `vault`, the computed residual `vault - (c_tot + insurance_fund.balance)` **grows** by exactly `pay`. The insurance fund shrinkage "frees" residual that was never actually in the vault.

---

## Rust Translation

```rust
// Pre:
let vault_pre = engine.vault.get();
let c_tot_pre = engine.c_tot.get();
let ins_pre = engine.insurance_fund.balance.get();
assert!(vault_pre >= c_tot_pre + ins_pre, "precondition: non-corrupt state");
let residual_pre = vault_pre - (c_tot_pre + ins_pre);

// Operation:
let symbolic_loss: u128 = kani::any();
kani::assume(symbolic_loss <= ins_pre); // insurance can cover it
let _uninsured = engine.use_insurance_buffer(symbolic_loss);

// Post:
let vault_post = engine.vault.get();
let c_tot_post = engine.c_tot.get();
let ins_post = engine.insurance_fund.balance.get();
let residual_post = vault_post - (c_tot_post + ins_post);

// Conservation claim — this assertion is EXPECTED TO FAIL:
assert_eq!(residual_post, residual_pre,
    "residual not conserved: insurance shrinkage freed phantom residual");
```

**Suggested Kani harness name:** `proof_vault_residual_conservation_use_insurance_buffer`

**Estimated harness complexity:** LOW (only three `u128` fields, one helper, no loops)

---

## Verdict

| Claim | Verdict | Confidence |
|---|---|---|
| "if any helper shrinks insurance counter it MUST debit vault by same amount" | **FALSE** | **HIGH** |

**Evidence:** `use_insurance_buffer` at percolator.rs:4811–4821 decrements `insurance_fund.balance` (line 4818) without any corresponding decrement to `vault`. `absorb_protocol_loss` (percolator.rs:4845–4850) inherits this same gap. The residual `vault - (c_tot + insurance_fund.balance)` grows by exactly the `pay` amount whenever insurance is consumed. This is not a latent or conditional gap — it is present on every invocation where `pay > 0`.

**Why this matters:** Any caller of `use_insurance_buffer` or `absorb_protocol_loss` can witness a post-call residual that is larger than the pre-call residual, meaning funds that were never deposited appear as distributable surplus in the `calc_admit_h` path (percolator.rs:2282–2291) and the stress-gate residual path (percolator.rs:2327–2332).