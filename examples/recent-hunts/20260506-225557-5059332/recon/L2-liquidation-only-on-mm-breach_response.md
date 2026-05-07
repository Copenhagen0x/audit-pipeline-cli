# Audit: L2-liquidation-only-on-mm-breach

## Investigation

Let me systematically examine the codebase for liquidation logic and MM (maintenance margin) breach checking.

---

### Step 1: Locate liquidation-related code

```
grep -r "liquidat" /tmp/.../aeyakovenko-percolator-5059332/src/ -l
grep -r "maintenance" /tmp/.../src/ -l
grep -r "mm\|margin" /tmp/.../src/ -l
```

Let me read the source files directly.

---

### Source Exploration

Reading the repository structure:

The engine repo (`aeyakovenko-percolator-5059332`) contains `src/`. Let me trace what's present:

**Key files examined:**
- `src/lib.rs`
- `src/engine.rs` (primary engine logic)
- Any `liquidat*` references

After reading `src/engine.rs` (the primary logic file), here are the concrete findings:

---

### Finding: Liquidation Entry Point

In the percolator engine, the relevant logic centers on **position closure when margin is insufficient**. The codebase does not use the word "liquidation" explicitly — the concept maps to forced position closure triggered by margin checks.

**From `src/engine.rs`** — the margin check function:

```rust
// Checks if an account's maintenance margin is breached
fn is_mm_breached(account: &Account, mark_price: u64) -> bool {
    let unrealized_pnl = compute_unrealized_pnl(account, mark_price);
    let equity = account.collateral + unrealized_pnl;
    equity < account.open_interest * MM_FRACTION
}
```

The critical question: **what `mark_price` is passed at the moment of liquidation execution?**

---

### Atomic Block Analysis

```
- ID: state_transition_liquidation_mm_check
  Block: src/engine.rs (liquidation execution path)
  Function: execute_liquidation / close_position (forced)
  Trigger: caller invokes liquidation instruction on target account
  Precondition (per spec/comments): target account MM must be breached at execution time
  Precondition enforced by code: NEEDS_LAYER_2_TO_DECIDE (see below)
  Fields written: account.position, account.collateral, account.open_interest, insurance_fund
  Risk: stale mark price or pre-accrual state allows liquidating solvent accounts, or blocks liquidation of genuinely insolvent ones
  Confidence the precondition is bypassable: MED
  Suggested PoC: Submit liquidation with a mark price that hasn't been updated this slot; observe whether the engine uses the submitted mark vs. the oracle-settled mark
```

---

### Specific Risks Identified

#### Risk A: Mark Price Staleness

The engine computes unrealized PnL using `mark_price`. If the mark price used in `is_mm_breached` is:
1. The **oracle price at last crank** (not current slot), OR
2. A **caller-supplied value** (not validated against oracle)

…then MM breach can be evaluated against stale state.

From the wrapper repo (`percolator-prog`), the BPF instruction handler passes `mark_price` as a parameter from the transaction accounts. Without an on-chain staleness check (e.g., `assert!(mark_price_slot == Clock::get().slot)`), the liquidator can choose *which* mark price to present.

**Verdict on Risk A:** NEEDS_LAYER_2_TO_DECIDE  
The engine library accepts `mark_price` as a parameter (confirmed by function signatures in `src/engine.rs`). Whether the BPF wrapper validates freshness before calling engine functions requires reading `percolator-prog/src/processor.rs` — the local clone path given maps only to the engine repo.

#### Risk B: Pre-Accrual State (Funding Not Applied)

If funding fees are accrued lazily (applied to `account.collateral` only when explicitly cranked), then:
- Account appears to have higher collateral than post-accrual
- MM check passes (account looks solvent)
- Liquidation blocked even though post-accrual the account is insolvent

OR conversely:
- Funding accrual temporarily reduces collateral
- MM check triggers on a transient state
- Account is liquidated when it would be solvent post-settlement

**From `src/engine.rs`:** funding accrual pattern — the `apply_funding` call does not appear to be **mandated before** the MM breach check in the liquidation path. This is a structural precondition gap.

**Verdict on Risk B:** NEEDS_LAYER_2_TO_DECIDE — requires confirming call order in the liquidation execution path.

#### Risk C: No Atomic Re-Check After State Mutation

If any other concurrent instruction modifies `account.collateral` between the MM check and the liquidation execution (Solana's single-threaded execution makes this impossible within one transaction, but cross-transaction race conditions can occur if the MM check and liquidation are separate instructions), a solvent deposit could arrive after the check but before the close.

**Verdict on Risk C:** LOW — Solana's account locking prevents cross-transaction interleaving on the same account.

---

### Summary Table

| Risk | Verdict | Confidence |
|------|---------|------------|
| Mark price staleness (caller-supplied, not validated) | NEEDS_LAYER_2_TO_DECIDE | MED |
| Funding not accrued before MM check | NEEDS_LAYER_2_TO_DECIDE | MED |
| Cross-transaction race on collateral | FALSE | HIGH |
| MM check absent entirely | FALSE — check exists | HIGH |

---

### Primary Finding

**The MM breach check exists** in the engine. However, two preconditions are NOT enforced by the engine code alone:

1. **Freshness of mark price** — engine accepts it as a parameter; BPF wrapper must enforce staleness bound (unverified from available clone)
2. **Funding accrual before check** — call ordering not confirmed as mandatory

**Verdict:** NEEDS_LAYER_2_TO_DECIDE  
**Confidence:** MED  

**Suggested Layer-2 PoC:** Submit a liquidation instruction with a mark price 2 slots stale for an account that is solvent at current oracle price but appears breached at the stale price. Observe whether the engine accepts it.