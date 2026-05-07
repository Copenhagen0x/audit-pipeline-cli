# Hunt cycle `20260506-234757-04b854e` — `percolator-wrapper-live`

- **Workspace:** `/root/audit_runs/percolator-live`
- **Engine SHA:** `04b854e`
- **Wrapper SHA:** `04b854e571`
- **Started:** 2026-05-06T23:47:57+00:00
- **Elapsed:** 989.9s
- **Cycle cost:** $4.3024
- **Daily spend:** $4.30 / $50

## Summary

- Hypotheses dispatched: **101**
- Layer 2 candidates: **49**
- PoCs scaffolded: **0**
- PoCs that fired: **0**
- Kani harnesses: **0**
- Confirmed findings: **0**

## No confirmed findings this cycle

_All hypotheses returned FALSE / their PoCs did not fire._

## Verdict table

| Hypothesis | Verdict | Confidence |
|---|---|---|
| `A1-permissionless-no-drain` | UNKNOWN | UNKNOWN |
| `A10-upgrade-authority-frozen` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `A2-admin-instructions-signer-check` | FALSE | HIGH |
| `A3-cpi-safety` | TRUE | HIGH |
| `A4-token-authority-validation` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `A5-pda-derivation-canonicality` | UNKNOWN | UNKNOWN |
| `A6-account-discriminator-check` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `A7-wrapper-instruction-signer-routing` | FALSE | MED |
| `A8-multisig-threshold` | UNKNOWN | UNKNOWN |
| `A9-pause-gate-coverage` | UNKNOWN | UNKNOWN |
| `AC1-account-gc-state-leak` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `AC2-materialize-fresh-state` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `AC3-touch-idempotent` | TRUE | HIGH |
| `AC4-free-only-on-zero-position` | UNKNOWN | UNKNOWN |
| `AC5-account-capital-conservation` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `AC6-slot-reuse-no-aliasing` | FALSE | HIGH |
| `AC7-account-bound-authority` | UNKNOWN | UNKNOWN |
| `AC8-account-zeroing-on-close` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `AR1-mul-div-floor-no-overflow` | UNKNOWN | UNKNOWN |
| `AR2-pnl-delta-i128-bound` | UNKNOWN | UNKNOWN |
| `AR3-funding-rate-bounds` | UNKNOWN | UNKNOWN |
| `AR4-catchup-no-overflow` | UNKNOWN | UNKNOWN |
| `AR5-fee-calc-overflow` | UNKNOWN | UNKNOWN |
| `AR6-square-root-bounds` | FALSE | HIGH |
| `AR7-saturating-arithmetic-correctness` | TRUE | HIGH |
| `AR8-rounding-direction` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `CI1-deposit-then-withdraw-zero` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `CI10-resolution-final` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `CI2-double-touch-no-drift` | TRUE | HIGH |
| `CI3-fill-then-cancel-impossible` | TRUE | HIGH |
| `CI4-self-trade-net-zero` | UNKNOWN | UNKNOWN |
| `CI5-cross-market-isolation` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `CI6-batch-instruction-atomicity` | UNKNOWN | UNKNOWN |
| `CI7-wrapper-instruction-equivalence` | UNKNOWN | UNKNOWN |
| `CI8-flash-fill-impossible` | UNKNOWN | UNKNOWN |
| `CI9-orderbook-depth-bound` | TRUE | HIGH |
| `IX1-ix-data-validation` | TRUE | HIGH |
| `IX10-error-codes-distinct` | FALSE | MED |
| `IX2-account-list-length-check` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `IX3-rent-exemption-check` | FALSE | HIGH |
| `IX4-clock-sysvar-required` | FALSE | HIGH |
| `IX5-no-arbitrary-cpi` | TRUE | HIGH |
| `IX6-account-owner-check` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `IX7-readonly-vs-writable-correctness` | UNKNOWN | UNKNOWN |
| `IX8-replay-protection` | UNKNOWN | UNKNOWN |
| `IX9-compute-budget-respect` | UNKNOWN | UNKNOWN |
| `L1-liquidation-discount-bounded` | TRUE | HIGH |
| `L10-liquidation-touch-pairing` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `L2-liquidation-only-on-mm-breach` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `L3-keeper-crank-cursor-budget` | FALSE | HIGH |
| `L4-keeper-authorization-surface` | UNKNOWN | UNKNOWN |
| `L5-liquidation-no-fee-enrichment` | FALSE | HIGH |
| `L6-force-closure-conditions` | UNKNOWN | UNKNOWN |
| `L7-keeper-crank-progress` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `L8-partial-liquidation-correctness` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `L9-cascade-liquidation-bound` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `O1-position-q-bound` | UNKNOWN | UNKNOWN |
| `O10-orderbook-side-balance` | UNKNOWN | UNKNOWN |
| `O2-oi-conservation` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `O3-position-authority-binding` | UNKNOWN | UNKNOWN |
| `O4-im-respect-on-open` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `O5-mm-trigger-correctness` | UNKNOWN | UNKNOWN |
| `O6-side-flip-atomicity` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `O7-position-zero-clears-basis` | UNKNOWN | UNKNOWN |
| `O8-cross-margin-equity` | UNKNOWN | UNKNOWN |
| `O9-position-bedge-correct` | UNKNOWN | UNKNOWN |
| `P1-pnl-zero-sum` | UNKNOWN | MED |
| `P10-funding-index-monotonic-modulo-direction` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `P2-pnl-pos-tot-monotonic` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `P3-pnl-matured-bound` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `P4-funding-rate-mark-bias` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `P5-funding-payment-zero-sum` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `P6-mark-ewma-bound` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `P7-pnl-on-side-flip` | UNKNOWN | UNKNOWN |
| `P8-self-trade-cash-flow` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `P9-pnl-arithmetic-bounds` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `R1-reorg-resilience` | TRUE | MED |
| `R2-deterministic-fill-matching` | TRUE | HIGH |
| `R3-finality-gate` | FALSE | HIGH |
| `R4-leader-rotation-safety` | TRUE | HIGH |
| `R5-rpc-staleness-tolerance` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `S1-init-state-invariants` | TRUE | HIGH |
| `S10-rebate-claim-correctness` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `S2-resolved-mode-mature-claim` | UNKNOWN | UNKNOWN |
| `S3-settle-after-close` | UNKNOWN | UNKNOWN |
| `S4-touch-account-live-pairing` | UNKNOWN | UNKNOWN |
| `S5-market-mode-transitions` | FALSE | HIGH |
| `S6-time-monotonic` | UNKNOWN | UNKNOWN |
| `S7-epoch-staleness-gate` | TRUE | HIGH |
| `S8-deposit-withdraw-atomicity` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `S9-cancel-correctness` | UNKNOWN | UNKNOWN |
| `V1-vault-residual-conservation` | UNKNOWN | UNKNOWN |
| `V10-claimable-pnl-conservation` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `V2-vault-balance-equation` | UNKNOWN | UNKNOWN |
| `V3-vault-monotonic-on-deposit` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `V4-vault-cap-respect` | FALSE | HIGH |
| `V5-haircut-direction` | UNKNOWN | UNKNOWN |
| `V6-insurance-floor` | UNKNOWN | MED |
| `V7-insurance-counter-vault-coupling` | UNKNOWN | UNKNOWN |
| `V8-cash-locked-conservation` | FALSE | HIGH |
| `V9-rebate-accumulation-bounded` | NEEDS_LAYER_2_TO_DECIDE | LOW |