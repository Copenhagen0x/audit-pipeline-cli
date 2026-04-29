## Analysis

### What the Hypothesis Claims

The hypothesis states a **symmetric debit rule**: whenever `insurance_fund.balance` (the "insurance_counter") decreases, `vault` MUST decrease by the same amount, so that the post-haircut residual `vault - c_tot - insurance_fund.balance` is conserved.

### Evidence: All Sites Where `insurance_fund.balance` Decreases

From the grep at line 3082, 7078, 7116, 7142:

| Site | Function | ΔInsurance | ΔVault | ΔC_tot | ΔResidual |
|------|----------|-----------|--------|--------|-----------|
| L 3082 | `use_insurance_buffer` | −pay | **0** | 0 | **+pay** |
| L 7078 | `credit_account_from_insurance_not_atomic` | −amount | 0 | +amount | 0 |
| L 7116 | `withdraw_live_insurance_not_atomic` | −amount | −amount | 0 | 0 |
| L 7142 | `withdraw_resolved_insurance_not_atomic` | −payout | −payout | 0 | 0 |

### Site 1 — `use_insurance_buffer` (L 3075–3085): Violates the Stated Invariant

```rust
fn use_insurance_buffer(&mut self, loss: u128) -> u128 {
    // ...
    let ins_bal = self.insurance_fund.balance.get();
    let pay = core::cmp::min(loss, ins_bal);
    if pay > 0 {
        self.insurance_fund.balance = U128::new(ins_bal - pay);  // L 3082
    }
    loss - pay
}
```

`vault` is **never touched**. Insurance shrinks by `pay`; vault stays constant. Therefore `Residual = vault - c_tot - insurance` **increases** by `pay`.

### Site 2 — `credit_account_from_insurance_not_atomic` (L 7078): Partially Violates

```rust
self.insurance_fund.balance = U128::new(ins - amount);  // L 7078
self.set_capital(idx as usize, new_cap)?;               // L 7079, c_tot +amount
```

Insurance decreases by `amount`, `c_tot` increases by `amount`, vault is unchanged. The sum `c_tot + insurance` is conserved, so `check_conservation` (`vault >= c_tot + I`) passes. Residual (`vault - c_tot - I`) is also conserved here. This does NOT violate residual conservation — only the narrow vault-debit rule.

### The Design Intent is Deliberate and Documented

The comment at L 3087–3101 explains **why** vault is NOT debited when insurance is consumed:

```
/// MUST NOT drain V here — doing so would shrink Residual below its
/// natural post-forgiveness value and double-penalize junior holders
/// (first via h < 1, again via V reduction).
///
/// Intuition: Alice +100, Bob -100, V = 50, insurance = 0. Forgiving Bob
/// leaves matured = 100, residual = 50 → h = 0.5, Alice gets 50. If we
/// also drained V by 50, residual would drop to 0 → Alice gets 0.
```

The design **intentionally** does NOT co-debit vault when insurance is consumed, because insurance absorbing a loss means the residual *widens* to cover junior PnL holders. Draining vault would double-penalize them.

### The Actual Enforced Conservation Invariant

The codebase's own stated invariant (L 9) is: `V >= C_tot + I`. This is verified by `check_conservation` (L 3994–4003) and asserted in `assert_public_postconditions_fast` (L 4075–4079) at the end of every public instruction. `use_insurance_buffer` reduces `I` while leaving `V` and `C_tot` unchanged — `V >= C_tot + I` remains satisfied (in fact, more strictly so after the drain).

The hypothesis's **claimed invariant** (`Residual = vault - c_tot - I` is conserved across insurance draws) is **structurally false by design**. Residual *grows* when insurance is consumed via `use_insurance_buffer`. This is the correct economic behaviour: insurance funds act as a buffer that, when spent, widen the residual pool backing junior PnL haircut ratio `h`.

### Evaluating Severity of the Hypothesis's Violation

The hypothesis's stated concern is that "shrinking the insurance counter without debiting vault" would be a **bug**. The code does exactly that — but intentionally. This is not a vulnerability. It is the intended design that:

1. Insurance consumption increases residual.
2. Uninsured losses (via `record_uninsured_protocol_loss` at L 3103, a no-op) are absorbed through the haircut mechanism `h = Residual / matured_pos_tot`.
3. The module-level invariant `V >= C_tot + I` (not `Residual = const`) is the actual safety property enforced on every exit.

The hypothesis as formulated assumes the wrong invariant (`Residual` conservation) instead of the actual enforced invariant (`V >= C_tot + I`).

---

## Verdict

**The claimed invariant is FALSE as stated** — `use_insurance_buffer` (L 3082) decrements `insurance_fund.balance` without debiting `vault`, causing residual to *increase*, not stay constant. However, this is **the intended, documented design** (L 3087–3101). The engine's actual enforced conservation property is `V >= C_tot + I` (L 9, L 4000, L 4075), which `use_insurance_buffer` preserves (it increases the gap). The specific violation of the hypothesis's stated constraint is not a security bug — it is the correct accounting mechanism that prevents double-penalizing junior PnL claimants.

`VERDICT: FALSE`
`CONFIDENCE: HIGH`