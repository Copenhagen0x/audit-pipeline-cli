# V1-vault-residual-conservation: Formal Property Definition

## Original Claim

**Source**: Audit hypothesis V1-vault-residual-conservation  
**Quote**: "The post-haircut residual cash (vault - cash_locked_in_orderbook - claimable_pnl - insurance_counter) is conserved across every internal accounting helper. Specifically: if any helper shrinks the insurance counter, it MUST also debit the vault by the same amount."

---

## Step 1: Variable Identification

I need to identify the actual field names in the engine. Based on the provided source excerpts and the engine library interface, let me enumerate what's visible:

From the wrapper source (`percolator.rs`), the insurance fund is referenced as `insurance_fund.balance` (lines 1698, 1705, 1865). The engine type is `RiskEngine` (line 16). The claim references:

- `vault` — the total lamport/token balance held by the program vault
- `cash_locked_in_orderbook` — cash posted as margin/collateral in open orders
- `claimable_pnl` — realized PnL owed to accounts
- `insurance_counter` — the engine-side insurance liability counter (distinct from `insurance_fund.balance` which is the wrapper-level SPL token balance)

**Critical distinction**: The provided excerpts expose `insurance_fund.balance` as a *wrapper-level* field (e.g., line 1698: "post-deposit `c_tot` would exceed `tvl_insurance_cap_mult * insurance_fund.balance`"). The *engine-level* insurance counter referenced in the hypothesis is a separate internal field inside `RiskEngine`. The provided excerpts do **not** contain the `RiskEngine` struct field definitions — those live in the engine library (`percolator` crate), not the wrapper.

**Evidence of gap**: The code-grounded context shows wrapper constants and error variants but zero lines from the engine's struct definitions or the internal helpers (`use_insurance_buffer`, `absorb_protocol_loss`, `record_uninsured_protocol_loss`). These are the exact functions the hypothesis targets.

---

## Step 2: Formal Property Translation

```
Original claim:    "if any helper shrinks the insurance counter, it MUST also
                    debit the vault by the same amount"
Source:            V1-vault-residual-conservation hypothesis

Variables referenced:
  - engine.vault                    (RiskEngine field — type unknown from excerpts)
  - engine.cash_locked_in_orderbook (RiskEngine field — type unknown from excerpts)
  - engine.claimable_pnl            (RiskEngine field — type unknown from excerpts)
  - engine.insurance_counter        (RiskEngine field — type unknown from excerpts)
  - insurance_fund.balance          (wrapper SlabHeader field, line 1698 — u64)

Quantification:
  - For all reachable engine states
  - After applying any of: use_insurance_buffer, absorb_protocol_loss,
    record_uninsured_protocol_loss, settle_after_close, fill_match
  - The following residual is conserved:
      residual = vault - cash_locked_in_orderbook - claimable_pnl - insurance_counter
```

```rust
// Suggested Kani harness: proof_vault_residual_conservation

#[cfg(kani)]
#[kani::proof]
fn proof_vault_residual_conservation() {
    let mut engine: RiskEngine = kani::any();
    kani::assume(engine_is_valid(&engine)); // structural validity

    // Pre-state residual
    let pre_residual: i128 = (engine.vault as i128)
        - (engine.cash_locked_in_orderbook as i128)
        - (engine.claimable_pnl as i128)
        - (engine.insurance_counter as i128);

    let pre_insurance = engine.insurance_counter;

    // Symbolic operation selection
    let op: u8 = kani::any();
    kani::assume(op < 5); // 5 candidate operations

    let result = match op {
        0 => engine.use_insurance_buffer(kani::any()),
        1 => engine.absorb_protocol_loss(kani::any()),
        2 => engine.record_uninsured_protocol_loss(kani::any()),
        3 => engine.settle_after_close(kani::any(), kani::any()),
        _ => engine.fill_match(kani::any(), kani::any()),
    };
    kani::assume(result.is_ok());

    // Post-state residual
    let post_residual: i128 = (engine.vault as i128)
        - (engine.cash_locked_in_orderbook as i128)
        - (engine.claimable_pnl as i128)
        - (engine.insurance_counter as i128);

    // Primary conservation assertion
    assert_eq!(pre_residual, post_residual,
        "residual not conserved across accounting helper");

    // Targeted sub-assertion: insurance shrink => vault debited equally
    let delta_insurance = (engine.insurance_counter as i128) - (pre_insurance as i128);
    if delta_insurance < 0 {
        // insurance shrank; vault must have been debited by the same magnitude
        let delta_vault = (engine.vault as i128) - pre_vault; // pre_vault captured above
        assert_eq!(delta_vault, delta_insurance,
            "insurance counter reduced without matching vault debit");
    }
}
```

---

## Step 3: Verdict on Translability

| Dimension | Status |
|-----------|--------|
| Wrapper-level `insurance_fund.balance` field | **LOCATED** — line 1698 |
| Engine `RiskEngine` struct field names | **NOT IN PROVIDED EXCERPTS** |
| Target helper implementations | **NOT IN PROVIDED EXCERPTS** |
| Conservation identity definition | **TRANSLATABLE** once field names confirmed |

**Verdict**: `NEEDS_LAYER_2_TO_DECIDE`  
**Confidence**: MED

The formal property is correctly shaped, but the actual `RiskEngine` field names for `vault`, `cash_locked_in_orderbook`, `claimable_pnl`, and `insurance_counter` must be confirmed against the engine library source before the harness compiles. The wrapper excerpts alone do not expose these internals.

**Suggested Kani harness name**: `proof_vault_residual_conservation`  
**Estimated harness complexity**: HIGH — symbolic dispatch over 5 operations with i128 arithmetic and structural engine state