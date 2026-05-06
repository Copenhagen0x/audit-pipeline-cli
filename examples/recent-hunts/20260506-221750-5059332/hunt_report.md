# Hunt cycle `20260506-221750-5059332` — `percolator-live`

- **Workspace:** `/root/audit_runs/percolator-live`
- **Engine SHA:** `5059332`
- **Wrapper SHA:** `04b854e571`
- **Started:** 2026-05-06T22:17:50+00:00
- **Elapsed:** 186.2s
- **Cycle cost:** $0.4922
- **Daily spend:** $7.48 / $50

## Summary

- Hypotheses dispatched: **12**
- Layer 2 candidates: **6**
- PoCs scaffolded: **6**
- PoCs that fired: **6**
- Kani harnesses: **0**
- Confirmed findings: **6**

## Confirmed findings

### `SH11-self-matched-pair-cannot-walk-K` — NEEDS_LAYER_2_TO_DECIDE/MED

- PoC scaffold: `/root/audit_runs/percolator-live/hunts/20260506-221750-5059332/poc/test_sh11_self_matched_pair_cannot_walk_k.rs`
- cargo test exit code: `101` (non-zero = PoC fired)

### `SH2-withdraw-collateral-helper-choice` — NEEDS_LAYER_2_TO_DECIDE/MED

- PoC scaffold: `/root/audit_runs/percolator-live/hunts/20260506-221750-5059332/poc/test_sh2_withdraw_collateral_helper_choice.rs`
- cargo test exit code: `101` (non-zero = PoC fired)

### `SH3-k-walk-via-oracle-rejected` — NEEDS_LAYER_2_TO_DECIDE/LOW

- PoC scaffold: `/root/audit_runs/percolator-live/hunts/20260506-221750-5059332/poc/test_sh3_k_walk_via_oracle_rejected.rs`
- cargo test exit code: `101` (non-zero = PoC fired)

### `SH4-k-walk-via-funding-rejected` — NEEDS_LAYER_2_TO_DECIDE/MED

- PoC scaffold: `/root/audit_runs/percolator-live/hunts/20260506-221750-5059332/poc/test_sh4_k_walk_via_funding_rejected.rs`
- cargo test exit code: `101` (non-zero = PoC fired)

### `SH5-keeper-crank-touching-completeness` — NEEDS_LAYER_2_TO_DECIDE/HIGH

- PoC scaffold: `/root/audit_runs/percolator-live/hunts/20260506-221750-5059332/poc/test_sh5_keeper_crank_touching_completeness.rs`
- cargo test exit code: `101` (non-zero = PoC fired)

### `SH8-trade-cpi-band-check-tightness` — TRUE/HIGH

- PoC scaffold: `/root/audit_runs/percolator-live/hunts/20260506-221750-5059332/poc/test_sh8_trade_cpi_band_check_tightness.rs`
- cargo test exit code: `101` (non-zero = PoC fired)

## Verdict table

| Hypothesis | Verdict | Confidence |
|---|---|---|
| `SH1-strict-helper-coverage` | UNKNOWN | UNKNOWN |
| `SH10-cpi-matcher-state-writes-isolated` | UNKNOWN | UNKNOWN |
| `SH11-self-matched-pair-cannot-walk-K` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `SH12-insurance-drain-via-resolve-flat-negative` | UNKNOWN | UNKNOWN |
| `SH2-withdraw-collateral-helper-choice` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `SH3-k-walk-via-oracle-rejected` | NEEDS_LAYER_2_TO_DECIDE | LOW |
| `SH4-k-walk-via-funding-rejected` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `SH5-keeper-crank-touching-completeness` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `SH6-resolve-flat-negative-gate` | UNKNOWN | UNKNOWN |
| `SH7-mark-ewma-update-rate-cap` | UNKNOWN | UNKNOWN |
| `SH8-trade-cpi-band-check-tightness` | TRUE | HIGH |
| `SH9-stuck-target-accrual-rejection` | UNKNOWN | UNKNOWN |