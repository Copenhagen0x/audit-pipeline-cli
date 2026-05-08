# Hunt cycle `20260508-025038` — `percolator-live`

- **Workspace:** `/root/audit_runs/percolator-live`
- **Engine SHA:** `3c9c84908b`
- **Wrapper SHA:** `04b854e571`
- **Started:** 2026-05-08T02:50:38+00:00
- **Elapsed:** 885.9s
- **Cycle cost:** $3.0173
- **Daily spend:** $3.02 / $50

## Summary

- Hypotheses dispatched: **101**
- Layer 2 candidates: **66**
- PoCs scaffolded: **0**
- PoCs that fired: **0**
- Kani harnesses: **0**
- Confirmed findings: **0**

## No confirmed findings this cycle

_All hypotheses returned FALSE / their PoCs did not fire._

## Verdict table

| Hypothesis | Verdict | Confidence |
|---|---|---|
| `A1-permissionless-no-drain` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `A10-upgrade-authority-frozen` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `A2-admin-instructions-signer-check` | FALSE | HIGH |
| `A3-cpi-safety` | FALSE | HIGH |
| `A4-token-authority-validation` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `A5-pda-derivation-canonicality` | TRUE | HIGH |
| `A6-account-discriminator-check` | UNKNOWN | UNKNOWN |
| `A7-wrapper-instruction-signer-routing` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `A8-multisig-threshold` | FALSE | HIGH |
| `A9-pause-gate-coverage` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `AC1-account-gc-state-leak` | TRUE | HIGH |
| `AC2-materialize-fresh-state` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `AC3-touch-idempotent` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `AC4-free-only-on-zero-position` | TRUE | HIGH |
| `AC5-account-capital-conservation` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `AC6-slot-reuse-no-aliasing` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `AC7-account-bound-authority` | TRUE | HIGH |
| `AC8-account-zeroing-on-close` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `AR1-mul-div-floor-no-overflow` | FALSE | HIGH |
| `AR2-pnl-delta-i128-bound` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `AR3-funding-rate-bounds` | UNKNOWN | UNKNOWN |
| `AR4-catchup-no-overflow` | TRUE | HIGH |
| `AR5-fee-calc-overflow` | FALSE | HIGH |
| `AR6-square-root-bounds` | FALSE | HIGH |
| `AR7-saturating-arithmetic-correctness` | FALSE | HIGH |
| `AR8-rounding-direction` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `CI1-deposit-then-withdraw-zero` | FALSE | HIGH |
| `CI10-resolution-final` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `CI2-double-touch-no-drift` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `CI3-fill-then-cancel-impossible` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `CI4-self-trade-net-zero` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `CI5-cross-market-isolation` | TRUE | HIGH |
| `CI6-batch-instruction-atomicity` | TRUE | HIGH |
| `CI7-wrapper-instruction-equivalence` | UNKNOWN | UNKNOWN |
| `CI8-flash-fill-impossible` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `CI9-orderbook-depth-bound` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `IX1-ix-data-validation` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `IX10-error-codes-distinct` | FALSE | HIGH |
| `IX2-account-list-length-check` | TRUE | HIGH |
| `IX3-rent-exemption-check` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `IX4-clock-sysvar-required` | UNKNOWN | UNKNOWN |
| `IX5-no-arbitrary-cpi` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `IX6-account-owner-check` | UNKNOWN | UNKNOWN |
| `IX7-readonly-vs-writable-correctness` | UNKNOWN | UNKNOWN |
| `IX8-replay-protection` | UNKNOWN | UNKNOWN |
| `IX9-compute-budget-respect` | TRUE | HIGH |
| `L1-liquidation-discount-bounded` | FALSE | HIGH |
| `L10-liquidation-touch-pairing` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `L2-liquidation-only-on-mm-breach` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `L3-keeper-crank-cursor-budget` | UNKNOWN | UNKNOWN |
| `L4-keeper-authorization-surface` | FALSE | MED |
| `L5-liquidation-no-fee-enrichment` | UNKNOWN | UNKNOWN |
| `L6-force-closure-conditions` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `L7-keeper-crank-progress` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `L8-partial-liquidation-correctness` | FALSE | HIGH |
| `L9-cascade-liquidation-bound` | TRUE | HIGH |
| `O1-position-q-bound` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `O10-orderbook-side-balance` | UNKNOWN | UNKNOWN |
| `O2-oi-conservation` | UNKNOWN | UNKNOWN |
| `O3-position-authority-binding` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `O4-im-respect-on-open` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `O5-mm-trigger-correctness` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `O6-side-flip-atomicity` | TRUE | HIGH |
| `O7-position-zero-clears-basis` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `O8-cross-margin-equity` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `O9-position-bedge-correct` | TRUE | HIGH |
| `P1-pnl-zero-sum` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `P10-funding-index-monotonic-modulo-direction` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `P2-pnl-pos-tot-monotonic` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `P3-pnl-matured-bound` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `P4-funding-rate-mark-bias` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `P5-funding-payment-zero-sum` | TRUE | HIGH |
| `P6-mark-ewma-bound` | TRUE | UNKNOWN |
| `P7-pnl-on-side-flip` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `P8-self-trade-cash-flow` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `P9-pnl-arithmetic-bounds` | TRUE | MED |
| `R1-reorg-resilience` | TRUE | MED |
| `R2-deterministic-fill-matching` | TRUE | HIGH |
| `R3-finality-gate` | FALSE | HIGH |
| `R4-leader-rotation-safety` | FALSE | HIGH |
| `R5-rpc-staleness-tolerance` | FALSE | HIGH |
| `S1-init-state-invariants` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `S10-rebate-claim-correctness` | FALSE | HIGH |
| `S2-resolved-mode-mature-claim` | NEEDS_LAYER_2_TO_DECIDE | UNKNOWN |
| `S3-settle-after-close` | UNKNOWN | UNKNOWN |
| `S4-touch-account-live-pairing` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `S5-market-mode-transitions` | TRUE | HIGH |
| `S6-time-monotonic` | UNKNOWN | UNKNOWN |
| `S7-epoch-staleness-gate` | FALSE | HIGH |
| `S8-deposit-withdraw-atomicity` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `S9-cancel-correctness` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `V1-vault-residual-conservation` | FALSE | HIGH |
| `V10-claimable-pnl-conservation` | UNKNOWN | UNKNOWN |
| `V2-vault-balance-equation` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `V3-vault-monotonic-on-deposit` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `V4-vault-cap-respect` | FALSE | HIGH |
| `V5-haircut-direction` | TRUE | HIGH |
| `V6-insurance-floor` | UNKNOWN | UNKNOWN |
| `V7-insurance-counter-vault-coupling` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `V8-cash-locked-conservation` | UNKNOWN | UNKNOWN |
| `V9-rebate-accumulation-bounded` | NEEDS_LAYER_2_TO_DECIDE | LOW |