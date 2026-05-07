# V1 — Vault Residual Conservation: Formal Property Definition

## Original Claim

**ID**: V1-vault-residual-conservation
**Claim**: The post-haircut residual cash (`vault - cash_locked_in_orderbook - claimable_pnl - insurance_counter`) is conserved across every internal accounting helper. Specifically: if any helper shrinks the insurance counter, it MUST also debit the vault by the same amount.

---

## Step 1: Variables Referenced

Mapping the English claim to actual engine fields (from `src/percolator.rs`):

| English term | Engine field | Evidence |
|---|---|---|
| `vault` | `self.vault` | line 2288–2290 |
| `cash_locked_in_orderbook` | `self.c_tot` | line 2282–2284 |
| `claimable_pnl` | `self.pnl_matured_pos_tot` (proxy) | line 2333 |
| `insurance_counter` | `self.insurance_fund.balance` | line 2285, 4815–4818 |
| `explicit_unallocated_protocol_loss` | `self.explicit_unallocated_protocol_loss` | line 4834 |

**Key observation**: The "senior" quantity computed in two places (lines 2285–2286, 2327–2329) is:
```
senior = c_tot + insurance_fund.balance
residual = vault - senior
```
The claim's "insurance_counter" maps directly to `insurance_fund.balance`.

---

## Step 2: Operations to Quantify Over

The helpers that mutate `insurance_fund.balance`:

1. **`use_insurance_buffer`** (line 4811–4821): decrements `insurance_fund.balance` by `pay = min(loss, ins_bal)`. Does **NOT** touch `self.vault`.
2. **`absorb_protocol_loss`** (line 4845–4850): calls `use_insurance_buffer`, then `record_uninsured_protocol_loss`. Does **NOT** touch `self.vault`.
3. **`record_uninsured_protocol_loss`** (line 4825–4839): increments `explicit_unallocated_protocol_loss`. Does **NOT** touch `self.vault`.

---

## Step 3: Timing

- **Invariant claim**: `vault - c_tot - pnl_matured_pos_tot - insurance_fund.balance` should remain constant (or only decrease by debits to vault) when `insurance_fund.balance` decreases.
- **Specific post-condition**: After `use_insurance_buffer(loss)` executes, `vault` is unchanged but `insurance_fund.balance` decreased by `pay`. This means the residual **increases** by `pay` — vault funds that were previously "covered" by insurance are now exposed.

---

## Step 4: Formal Translation

```
Original claim:    "if any helper shrinks the insurance counter, it MUST also debit the vault by the same amount"
Source:            V1 hypothesis brief

Variables referenced:
  - self.vault                             (percolator.rs, ~line 2288, type U128)
  - self.c_tot                             (percolator.rs, ~line 2283, type U128)
  - self.insurance_fund.balance            (percolator.rs, lines 2285/4815-4818, type U128)
  - self.explicit_unallocated_protocol_loss (percolator.rs, line 4834, type U128)

Quantification:
  - For all reachable engine states
  - After applying use_insurance_buffer(loss) or absorb_protocol_loss(loss)
  - The following conservation law holds:
      (vault_post - vault_pre) == -(insurance_balance_pre - insurance_balance_post)
    i.e., vault decreases by exactly the amount insurance decreases
```

```rust
// Pre-state snapshot
let vault_pre = engine.vault.get();
let ins_pre   = engine.insurance_fund.balance.get();

// Operation
let _uninsured = engine.use_insurance_buffer(loss);

// Post-state
let vault_post = engine.vault.get();
let ins_post   = engine.insurance_fund.balance.get();
let ins_paid   = ins_pre - ins_post; // how much insurance shrunk

// V1 conservation assertion:
// If the claim is TRUE, vault must also decrease by ins_paid.
assert_eq!(
    vault_pre.wrapping_sub(vault_post),
    ins_paid,
    "vault must be debited by same amount insurance buffer was consumed"
);
```

**Suggested Kani harness name**: `proof_v1_vault_insurance_conservation`
**Estimated harness complexity**: LOW (two scalar fields, one arithmetic operation, no loops)

---

## Step 5: Evidence Assessment

From the source code (lines 4811–4821), `use_insurance_buffer` **only** modifies `self.insurance_fund.balance`. `self.vault` is **never touched**. Therefore:

- `vault_post == vault_pre` always
- `ins_post < ins_pre` when `loss > 0` and `ins_bal > 0`
- The conservation assertion `vault_pre - vault_post == ins_paid` reduces to `0 == ins_paid`, which is **FALSE whenever insurance is consumed**

**Verdict**: The claim as stated (vault debited by same amount insurance shrinks) is **FALSE** by direct code inspection. `use_insurance_buffer` shrinks `insurance_fund.balance` without any corresponding `vault` debit.

**Confidence**: HIGH

**Finding**: The residual `vault - c_tot - insurance_fund.balance` **increases** whenever insurance absorbs a loss — vault funds are not removed, but insurance coverage shrinks. Whether this is a bug or intentional design (insurance acts as internal bookkeeping, vault is the external collateral pool) requires architectural context, but the conservation invariant as literally stated does not hold.