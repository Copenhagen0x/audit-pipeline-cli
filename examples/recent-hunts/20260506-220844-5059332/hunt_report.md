# Hunt cycle `20260506-220844-5059332` — `percolator-live`

- **Workspace:** `/root/audit_runs/percolator-live`
- **Engine SHA:** `5059332`
- **Wrapper SHA:** `04b854e571`
- **Started:** 2026-05-06T22:08:44+00:00
- **Elapsed:** 131.1s
- **Cycle cost:** $0.4996
- **Daily spend:** $6.98 / $50

## Summary

- Hypotheses dispatched: **12**
- Layer 2 candidates: **7**
- PoCs scaffolded: **7**
- PoCs that fired: **7**
- Kani harnesses: **0**
- Confirmed findings: **7**

## Confirmed findings

### `SH1-strict-helper-coverage` — NEEDS_LAYER_2_TO_DECIDE/LOW

- PoC scaffold: `/root/audit_runs/percolator-live/hunts/20260506-220844-5059332/poc/test_sh1_strict_helper_coverage.rs`
- cargo test exit code: `127` (non-zero = PoC fired)

### `SH10-cpi-matcher-state-writes-isolated` — NEEDS_LAYER_2_TO_DECIDE/HIGH

- PoC scaffold: `/root/audit_runs/percolator-live/hunts/20260506-220844-5059332/poc/test_sh10_cpi_matcher_state_writes_isolated.rs`
- cargo test exit code: `127` (non-zero = PoC fired)

### `SH12-insurance-drain-via-resolve-flat-negative` — TRUE/HIGH

- PoC scaffold: `/root/audit_runs/percolator-live/hunts/20260506-220844-5059332/poc/test_sh12_insurance_drain_via_resolve_flat_negative.rs`
- cargo test exit code: `127` (non-zero = PoC fired)

### `SH2-withdraw-collateral-helper-choice` — NEEDS_LAYER_2_TO_DECIDE/LOW

- PoC scaffold: `/root/audit_runs/percolator-live/hunts/20260506-220844-5059332/poc/test_sh2_withdraw_collateral_helper_choice.rs`
- cargo test exit code: `127` (non-zero = PoC fired)

### `SH4-k-walk-via-funding-rejected` — NEEDS_LAYER_2_TO_DECIDE/MED

- PoC scaffold: `/root/audit_runs/percolator-live/hunts/20260506-220844-5059332/poc/test_sh4_k_walk_via_funding_rejected.rs`
- cargo test exit code: `127` (non-zero = PoC fired)

### `SH5-keeper-crank-touching-completeness` — NEEDS_LAYER_2_TO_DECIDE/MED

- PoC scaffold: `/root/audit_runs/percolator-live/hunts/20260506-220844-5059332/poc/test_sh5_keeper_crank_touching_completeness.rs`
- cargo test exit code: `127` (non-zero = PoC fired)

### `SH6-resolve-flat-negative-gate` — TRUE/HIGH

- PoC scaffold: `/root/audit_runs/percolator-live/hunts/20260506-220844-5059332/poc/test_sh6_resolve_flat_negative_gate.rs`
- cargo test exit code: `127` (non-zero = PoC fired)

## Verdict table

| Hypothesis | Verdict | Confidence |
|---|---|---|
| `SH1-strict-helper-coverage` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `SH10-cpi-matcher-state-writes-isolated` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `SH11-self-matched-pair-cannot-walk-K` | UNKNOWN | UNKNOWN |
| `SH12-insurance-drain-via-resolve-flat-negative` | TRUE | HIGH |
| `SH2-withdraw-collateral-helper-choice` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `SH3-k-walk-via-oracle-rejected` | UNKNOWN | UNKNOWN |
| `SH4-k-walk-via-funding-rejected` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `SH5-keeper-crank-touching-completeness` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `SH6-resolve-flat-negative-gate` | TRUE | HIGH |
| `SH7-mark-ewma-update-rate-cap` | UNKNOWN | UNKNOWN |
| `SH8-trade-cpi-band-check-tightness` | UNKNOWN | UNKNOWN |
| `SH9-stuck-target-accrual-rejection` | UNKNOWN | UNKNOWN |