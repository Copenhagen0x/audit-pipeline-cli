## Analysis Summary

### What I found — complete inventory of `self.vault` mutations

There are exactly **8 assignment sites** to `self.vault` in the engine (confirmed by grep):

| Line | Direction | Guard |
|------|-----------|-------|
| 5041 | **UP** (`+amount`) | `checked_add` + `> MAX_VAULT_TVL` check at line 5012 |
| 5080 | DOWN (`-amount`) | `checked_sub` only |
| 7041 | **UP** (`+amount`) | `checked_add` + `> MAX_VAULT_TVL` check at line 7030 |
| 7117 | DOWN (`-amount`) | `checked_sub` only |
| 7143 | DOWN (`-payout`) | `checked_sub` only |
| 7385 | **UP** (`+pay`) | `checked_add` + `> MAX_VAULT_TVL` check at line 7370 |
| 6492 | DOWN (`-capital`) | guarded by `capital > self.vault` check at 6489 |
| 6928 | DOWN (`-capital`) | guarded by `capital > self.vault` check at 6925 |

Every vault-**increasing** path has the cap check. Every vault-**decreasing** path uses either `checked_sub` (explicit error on underflow, line 5080) or a pre-checked subtraction (lines 6492, 6928, 7117, 7143).

### The `U128` operator `Sub` — saturates, not panics, but only for subtractions

From `i128.rs` lines 813–818 (BPF, `#[cfg(not(kani))]`):
```rust
impl core::ops::Sub<U128> for U128 {
    fn sub(self, rhs: U128) -> Self {
        Self::new(self.get().saturating_sub(rhs.get()))
    }
}
```
The two direct `self.vault = self.vault - capital` uses at lines 6492 and 6928 use this saturating operator — but both are pre-guarded by explicit `if capital > self.vault { return Err(...); }` checks at lines 6489 and 6925 respectively. So saturating_sub cannot produce an incorrect result there (it would only trigger after the error return).

### The `assert_public_postconditions_fast` backstop

Lines 4072–4076:
```rust
let vault = self.vault.get();
...
if vault > MAX_VAULT_TVL || capital > vault || insurance > vault {
    return Err(RiskError::CorruptState);
}
```
This check is called at the **end** of every public `_not_atomic` entrypoint. Even if a hypothetical path somehow bypassed the upfront guard, this post-condition check would catch it and return an error — because the engine uses validate-then-mutate with a belt-and-suspenders post-check.

### Internal helpers that increase `insurance_fund.balance` WITHOUT touching vault

- `fee_debt_sweep` (line 4858–4860): increases `insurance_fund.balance` by moving capital into insurance, but **vault stays unchanged** — this is correctly a redistribution within vault, not new external money.
- `charge_fee_to_insurance` (line 5582–5584): similarly moves capital to insurance within vault.
- `sweep_empty_market_surplus_to_insurance` (lines 4050–4054): moves surplus (vault − insurance) into insurance balance; vault unchanged — again a redistribution.
- `absorb_protocol_loss` → `use_insurance_buffer` (lines 3075–3084): only decreases `insurance_fund.balance`.

None of these internal helpers increase `self.vault`. They only redistribute tokens already inside vault.

### Paths that do NOT touch vault

- `accrue_market_to` (lines 2813–3022): touches K coefficients, F numerators, slot counters — **never mutates `self.vault`**
- `execute_trade_not_atomic`: calls `accrue_market_to` + local PnL book-keeping — **never mutates `self.vault`**
- `convert_released_pnl_not_atomic`: calls `set_capital` (line 6347) and `fee_debt_sweep` — **never mutates `self.vault`**
- `settle_losses`, `resolve_flat_negative`: PnL and capital book-keeping only — **never mutates `self.vault`**

### Three guarded vault-increase paths — all enforce the cap

1. **`deposit_not_atomic`** (lines 5006–5013):
   ```rust
   let v_candidate = self.vault.get().checked_add(amount).ok_or(RiskError::Overflow)?;
   if v_candidate > MAX_VAULT_TVL { return Err(RiskError::Overflow); }
   ...
   self.vault = U128::new(v_candidate);  // line 5041
   ```

2. **`top_up_insurance_fund`** (lines 7025–7031):
   ```rust
   let new_vault = self.vault.get().checked_add(amount).ok_or(RiskError::Overflow)?;
   if new_vault > MAX_VAULT_TVL { return Err(RiskError::Overflow); }
   ...
   self.vault = U128::new(new_vault);  // line 7041
   ```

3. **`deposit_fee_credits`** (lines 7365–7371):
   ```rust
   let new_vault = self.vault.get().checked_add(pay).ok_or(RiskError::Overflow)?;
   if new_vault > MAX_VAULT_TVL { return Err(RiskError::Overflow); }
   ...
   self.vault = U128::new(new_vault);  // line 7385
   ```

### Wrapper-level check

The wrapper (`target/wrapper/src/percolator.rs` line 4924) also independently validates against `MAX_VAULT_TVL` at the fee-configuration level, but this is defense-in-depth, not the primary guard.

### Edge cases that strain but still hold

1. **Initial state**: `vault` starts at `U128::ZERO` (line 1336) — already within cap.
2. **Saturating subtraction in U128 operator**: could in theory produce 0 instead of error, but all subtraction sites are either `checked_sub` (returns error) or pre-guarded with explicit comparison.
3. **`assert_public_postconditions_fast`**: the post-check at line 4075 (`vault > MAX_VAULT_TVL`) provides a belt-and-suspenders catch for any missed path.
4. **`credit_account_from_insurance_not_atomic`** (lines 7067–7087): moves money from insurance to capital without touching vault — correctly does not need a cap check since vault doesn't change.
5. **`withdraw_resolved_insurance_not_atomic`** (line 7132): calls `sweep_empty_market_surplus_to_insurance` which can move vault surplus into insurance, but vault itself is never increased by that function (surplus = vault - insurance, new insurance = vault, vault unchanged).

All reachable state transitions that could increase `self.vault` enforce the `MAX_VAULT_TVL` bound before committing, and a mandatory post-condition check at every public entry point enforces it as a second line of defense.

## Verdict

The invariant holds. Every vault-increasing path in the engine (`deposit_not_atomic` at line 5012, `top_up_insurance_fund` at line 7030, `deposit_fee_credits` at line 7370) performs a `checked_add` followed by an explicit `> MAX_VAULT_TVL` rejection before writing the new value. Internal helpers that redistribute balances within the vault (`fee_debt_sweep`, `charge_fee_to_insurance`, `sweep_empty_market_surplus_to_insurance`, `absorb_protocol_loss`) never increase `self.vault` itself. The `assert_public_postconditions_fast` backstop at line 4075 would independently catch any violation as a corrupt-state error on every public instruction exit.

`VERDICT: TRUE`
`CONFIDENCE: HIGH`