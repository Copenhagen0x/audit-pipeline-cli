# Hunt cycle `20260506-225557-5059332` — `percolator-live`

- **Workspace:** `/root/audit_runs/percolator-live`
- **Engine SHA:** `5059332`
- **Wrapper SHA:** `04b854e571`
- **Started:** 2026-05-06T22:55:57+00:00
- **Elapsed:** 778.2s
- **Cycle cost:** $3.3089
- **Daily spend:** $10.79 / $50

## Summary

- Hypotheses dispatched: **101**
- Layer 2 candidates: **52**
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
| `A10-upgrade-authority-frozen` | UNKNOWN | UNKNOWN |
| `A2-admin-instructions-signer-check` | FALSE | HIGH |
| `A3-cpi-safety` | FALSE | HIGH |
| `A4-token-authority-validation` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `A5-pda-derivation-canonicality` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `A6-account-discriminator-check` | FALSE | HIGH |
| `A7-wrapper-instruction-signer-routing` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `A8-multisig-threshold` | FALSE | HIGH |
| `A9-pause-gate-coverage` | TRUE | HIGH |
| `AC1-account-gc-state-leak` | NEEDS_LAYER_2_TO_DECIDE | UNKNOWN |
| `AC2-materialize-fresh-state` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `AC3-touch-idempotent` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `AC4-free-only-on-zero-position` | FALSE | HIGH |
| `AC5-account-capital-conservation` | UNKNOWN | UNKNOWN |
| `AC6-slot-reuse-no-aliasing` | UNKNOWN | UNKNOWN |
| `AC7-account-bound-authority` | TRUE | HIGH |
| `AC8-account-zeroing-on-close` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `AR1-mul-div-floor-no-overflow` | FALSE | HIGH |
| `AR2-pnl-delta-i128-bound` | FALSE | HIGH |
| `AR3-funding-rate-bounds` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `AR4-catchup-no-overflow` | TRUE | MED |
| `AR5-fee-calc-overflow` | FALSE | HIGH |
| `AR6-square-root-bounds` | FALSE | HIGH |
| `AR7-saturating-arithmetic-correctness` | UNKNOWN | UNKNOWN |
| `AR8-rounding-direction` | FALSE | HIGH |
| `CI1-deposit-then-withdraw-zero` | FALSE | MED |
| `CI10-resolution-final` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `CI2-double-touch-no-drift` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `CI3-fill-then-cancel-impossible` | UNKNOWN | UNKNOWN |
| `CI4-self-trade-net-zero` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `CI5-cross-market-isolation` | UNKNOWN | UNKNOWN |
| `CI6-batch-instruction-atomicity` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `CI7-wrapper-instruction-equivalence` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `CI8-flash-fill-impossible` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `CI9-orderbook-depth-bound` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `IX1-ix-data-validation` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `IX10-error-codes-distinct` | TRUE | HIGH |
| `IX2-account-list-length-check` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `IX3-rent-exemption-check` | UNKNOWN | UNKNOWN |
| `IX4-clock-sysvar-required` | UNKNOWN | UNKNOWN |
| `IX5-no-arbitrary-cpi` | TRUE | HIGH |
| `IX6-account-owner-check` | FALSE | HIGH |
| `IX7-readonly-vs-writable-correctness` | UNKNOWN | UNKNOWN |
| `IX8-replay-protection` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `IX9-compute-budget-respect` | UNKNOWN | UNKNOWN |
| `L1-liquidation-discount-bounded` | FALSE | HIGH |
| `L10-liquidation-touch-pairing` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `L2-liquidation-only-on-mm-breach` | UNKNOWN | UNKNOWN |
| `L3-keeper-crank-cursor-budget` | FALSE | HIGH |
| `L4-keeper-authorization-surface` | UNKNOWN | UNKNOWN |
| `L5-liquidation-no-fee-enrichment` | UNKNOWN | UNKNOWN |
| `L6-force-closure-conditions` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `L7-keeper-crank-progress` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `L8-partial-liquidation-correctness` | FALSE | HIGH |
| `L9-cascade-liquidation-bound` | FALSE | HIGH |
| `O1-position-q-bound` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `O10-orderbook-side-balance` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `O2-oi-conservation` | UNKNOWN | HIGH |
| `O3-position-authority-binding` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `O4-im-respect-on-open` | TRUE | MED |
| `O5-mm-trigger-correctness` | UNKNOWN | UNKNOWN |
| `O6-side-flip-atomicity` | TRUE | HIGH |
| `O7-position-zero-clears-basis` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `O8-cross-margin-equity` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `O9-position-bedge-correct` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `P1-pnl-zero-sum` | UNKNOWN | UNKNOWN |
| `P10-funding-index-monotonic-modulo-direction` | UNKNOWN | UNKNOWN |
| `P2-pnl-pos-tot-monotonic` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `P3-pnl-matured-bound` | NEEDS_LAYER_2_TO_DECIDE | UNKNOWN |
| `P4-funding-rate-mark-bias` | UNKNOWN | UNKNOWN |
| `P5-funding-payment-zero-sum` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `P6-mark-ewma-bound` | UNKNOWN | UNKNOWN |
| `P7-pnl-on-side-flip` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `P8-self-trade-cash-flow` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `P9-pnl-arithmetic-bounds` | FALSE | MED |
| `R1-reorg-resilience` | FALSE | HIGH |
| `R2-deterministic-fill-matching` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `R3-finality-gate` | FALSE | HIGH |
| `R4-leader-rotation-safety` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `R5-rpc-staleness-tolerance` | UNKNOWN | UNKNOWN |
| `S1-init-state-invariants` | UNKNOWN | MED |
| `S10-rebate-claim-correctness` | FALSE | HIGH |
| `S2-resolved-mode-mature-claim` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `S3-settle-after-close` | UNKNOWN | UNKNOWN |
| `S4-touch-account-live-pairing` | FALSE | MED |
| `S5-market-mode-transitions` | TRUE | HIGH |
| `S6-time-monotonic` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `S7-epoch-staleness-gate` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `S8-deposit-withdraw-atomicity` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `S9-cancel-correctness` | UNKNOWN | UNKNOWN |
| `V1-vault-residual-conservation` | UNKNOWN | UNKNOWN |
| `V10-claimable-pnl-conservation` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `V2-vault-balance-equation` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `V3-vault-monotonic-on-deposit` | UNKNOWN | UNKNOWN |
| `V4-vault-cap-respect` | TRUE | HIGH |
| `V5-haircut-direction` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `V6-insurance-floor` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `V7-insurance-counter-vault-coupling` | FALSE | MED |
| `V8-cash-locked-conservation` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `V9-rebate-accumulation-bounded` | FALSE | HIGH |