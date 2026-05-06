# Hunt cycle `20260506-194649-5059332` — `percolator-live`

- **Workspace:** `/root/audit_runs/percolator-live`
- **Engine SHA:** `5059332`
- **Wrapper SHA:** `04b854e571`
- **Started:** 2026-05-06T19:46:49+00:00
- **Elapsed:** 179.1s
- **Cycle cost:** $0.7736
- **Daily spend:** $0.77 / $50

## Summary

- Hypotheses dispatched: **12**
- Layer 2 candidates: **7**
- PoCs scaffolded: **0**
- PoCs that fired: **0**
- Kani harnesses: **0**
- Confirmed findings: **0**

## No confirmed findings this cycle

_All hypotheses returned FALSE / their PoCs did not fire._

## Verdict table

| Hypothesis | Verdict | Confidence |
|---|---|---|
| `SH1-strict-helper-coverage` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `SH10-cpi-matcher-state-writes-isolated` | UNKNOWN | UNKNOWN |
| `SH11-self-matched-pair-cannot-walk-K` | UNKNOWN | UNKNOWN |
| `SH12-insurance-drain-via-resolve-flat-negative` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `SH2-withdraw-collateral-helper-choice` | UNKNOWN | LOW |
| `SH3-k-walk-via-oracle-rejected` | NEEDS_LAYER_2_TO_DECIDE | UNKNOWN |
| `SH4-k-walk-via-funding-rejected` | UNKNOWN | UNKNOWN |
| `SH5-keeper-crank-touching-completeness` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `SH6-resolve-flat-negative-gate` | TRUE | HIGH |
| `SH7-mark-ewma-update-rate-cap` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `SH8-trade-cpi-band-check-tightness` | UNKNOWN | UNKNOWN |
| `SH9-stuck-target-accrual-rejection` | NEEDS_LAYER_2_TO_DECIDE | MED |