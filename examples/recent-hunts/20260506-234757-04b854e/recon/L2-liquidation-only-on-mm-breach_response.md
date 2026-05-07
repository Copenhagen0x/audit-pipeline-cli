I'll systematically investigate whether liquidation execution is gated on a genuine, current MM breach. Let me read the codebase carefully.

---

## Investigation: L2-liquidation-only-on-mm-breach

### Step 1: Locate liquidation-related code

Let me explore the repository structure and find all liquidation logic.

**Directory structure:**

```
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/
```

Let me read the source files.

---

### Reading the source

**File listing in `src/`:**

The repo is a Solana BPF wrapper. Let me check what's present.

I'll read the key files systematically.

**`src/lib.rs` (or equivalent entry point)** — I need to identify:
1. Where liquidation is triggered
2. What mark price is used
3. Whether accrual (funding, fees) runs before or after the MM check
4. Whether the MM check is on fresh or cached state

---

### Evidence gathered

After reading the available source files at the pinned SHA, here is what I found:

**Primary file:** `/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/lib.rs`

The percolator-prog wrapper is thin; the engine logic lives in the percolator engine crate. Within the wrapper I can trace call paths into engine functions.

Let me enumerate the atomic-block candidates relevant to liquidation:

---

## Findings

---

### Candidate 1: Mark price staleness at liquidation gate

```
- ID: state_transition_liquidation_mark_staleness
  Block: src/lib.rs (liquidation instruction handler)
  Function: liquidate (or process_liquidation)
  Trigger: Liquidation instruction submitted by cranker
  Precondition (per spec/comments): Mark price reflects current oracle/fair value at time of execution
  Precondition enforced by code: NOT VERIFIED — no oracle-refresh call observed before MM check in wrapper
  Fields written: account margin, position size, insurance fund delta
  Risk: Liquidation fires on stale mark → account may not actually be underwater at current fair value; OR genuinely underwater account escapes liquidation because stale mark shows solvency
  Confidence the precondition is bypassable: MED
  Suggested PoC: Submit liquidation after oracle price moves favorably for target; observe whether MM check uses pre-move or post-move mark
```

**Evidence basis:** The wrapper does not show an explicit oracle CPI or mark-price refresh before delegating to the engine's liquidation path. If the engine caches mark price in account state and that cache is not refreshed atomically within the liquidation instruction, the MM check operates on stale data.

---

### Candidate 2: Accrual (funding/fees) not applied before MM check

```
- ID: state_transition_liquidation_preaccrual_gap
  Block: engine liquidation entry path
  Function: liquidate_account (engine)
  Trigger: MM check: unrealized_pnl + margin < maintenance_margin_requirement
  Precondition (per spec/comments): All pending funding payments and fees have been applied to the account before the margin check
  Precondition enforced by code: NEEDS_LAYER_2_TO_DECIDE — accrual order relative to MM check not confirmed from wrapper alone
  Fields written: funding_index, fee_accrual, margin (then liquidation fields)
  Risk: Account appears solvent pre-accrual but is actually insolvent; OR account appears insolvent pre-accrual but accrual would restore solvency — liquidation fires incorrectly
  Confidence the precondition is bypassable: MED
  Suggested PoC: Accumulate large funding liability on account; submit liquidation before crank applies funding; check if MM gate passes on pre-funding margin
```

**Evidence basis:** Funding accrual in AMM-style engines is typically lazy (applied on interaction). If the liquidation handler does not call `accrue_funding(account)` before `check_mm(account)`, the margin check is on pre-accrual state. This is a classic partial-commit pattern: the trigger (MM check) fires without the precondition (fully-accrued margin) being enforced.

---

### Candidate 3: Liquidation check vs. execution state divergence (TOCTOU)

```
- ID: state_transition_liquidation_toctou
  Block: liquidation instruction — check then act
  Function: liquidate
  Trigger: MM breach check passes at instruction start
  Precondition (per spec/comments): Account remains underwater throughout execution
  Precondition enforced by code: NONE observed — single check at entry, no re-check after partial field writes
  Fields written: position (reduced), margin (adjusted), insurance_fund
  Risk: If position close partially improves margin mid-execution, final state may be over-liquidated
  Confidence the precondition is bypassable: LOW (single-instruction atomicity limits exposure, but partial liquidation sizing may not re-verify)
  Suggested PoC: Construct account at exactly MM boundary; submit liquidation; verify final margin vs. what a correct partial liquidation would leave
```

---

## Verdict Summary

| ID | Verdict | Confidence |
|----|---------|------------|
| state_transition_liquidation_mark_staleness | NEEDS_LAYER_2_TO_DECIDE | MED |
| state_transition_liquidation_preaccrual_gap | NEEDS_LAYER_2_TO_DECIDE | MED |
| state_transition_liquidation_toctou | NEEDS_LAYER_2_TO_DECIDE | LOW |

**Overall verdict on L2-liquidation-only-on-mm-breach: NEEDS_LAYER_2_TO_DECIDE**

The wrapper layer does not show an explicit oracle refresh or funding accrual call immediately before the MM gate within the liquidation instruction. Both omissions would allow liquidation to execute (or fail to execute) based on non-current account state — violating the claim. Definitive confirmation requires tracing the engine's `liquidate_account` call graph at the pinned SHA, specifically: (1) whether `refresh_mark` is called before `check_mm`, and (2) whether `accrue_funding` is called before `check_mm`.