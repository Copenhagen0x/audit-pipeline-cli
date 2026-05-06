Looking at what I've gathered, let me synthesize the findings and write the test.

From my tool reads I have confirmed:

1. `haircut_ratio()` is at `percolator.rs:5457-5481` — it computes `(h_num, h_den)` where `h_num = min(residual, pnl_matured_pos_tot)` and `h_den = pnl_matured_pos_tot`. This guarantees `h_num <= h_den`, so `ratio <= 1`.

2. The exact struct fields accessed in `haircut_ratio`: `self.pnl_matured_pos_tot`, `self.c_tot`, `self.insurance_fund.balance`, `self.vault`.

3. From `proofs_invariants.rs:484-502`, the test at `proof_haircut_ratio_no_division_by_zero` directly sets `engine.pnl_pos_tot`, `engine.pnl_matured_pos_tot`, `engine.vault`, `engine.c_tot`, `engine.insurance_fund.balance` — confirming all are public fields.

4. The test pattern from `test_confirm_v4_vault_cap_respect.rs:1-82`:
   - `#![cfg(feature = "test")]`
   - `use percolator::i128::U128;` and `use percolator::*;`
   - `zero_fee_params()` exact struct with all fields
   - `add_user_test()` uses `engine.free_head` and `engine.materialize_at(idx, engine.current_slot)`
   - `RiskEngine::new(params)`

5. `haircut_ratio()` is `test_visible!` so it's public under `feature = "test"`.

6. `effective_matured_pnl` / `try_effective_matured_pnl` are also `test_visible!` at lines 5523-5527.

7. The invariant: `h_num <= h_den` always, meaning `ratio <= 1`, meaning the haircut can only shrink (never inflate) a claimant's PnL. Proved by the `min()` at line 5475-5479.

## Verdict

The haircut-direction invariant (`h_num <= h_den`, equivalently ratio ≤ 1) is enforced by the `min()` on line 5475 of `percolator.rs`. The test below asserts this property directly against the engine for three representative scenarios: undercollateralised (haircut < 1), exactly-covered (haircut = 1), and overcollateralised (no haircut needed, ratio still ≤ 1 because `h_num` saturates at `pnl_matured_pos_tot`).

#![cfg(feature = "test")]

// test_confirm_v5_haircut_direction
//
// Hypothesis V5-haircut-direction: the haircut ratio (h_num / h_den) returned
// by RiskEngine::haircut_ratio() is ALWAYS <= 1, meaning the mechanism can
// only shrink a claimant's positive PnL, never inflate it.
//
// Source evidence (all from target/engine/src/percolator.rs):
//   Line 5457: fn haircut_ratio(&self) -> (u128, u128)
//   Line 5458: if self.pnl_matured_pos_tot == 0 { return (1, 1) }  -- ratio = 1, harmless
//   Line 5461-5474: residual = max(0, vault - c_tot - insurance)
//   Line 5475-5479: h_num = min(residual, pnl_matured_pos_tot)      -- the key clamp
//   Line 5480: return (h_num, pnl_matured_pos_tot)
//
// Because h_num = min(residual, pnl_matured_pos_tot) we always have
//   h_num <= pnl_matured_pos_tot = h_den
// so ratio = h_num / h_den <= 1 in every reachable state.
//
// The test drives three concrete scenarios:
//   A. Undercollateralised: vault < c_tot + insurance + pnl_matured_pos_tot
//      => residual < pnl_matured_pos_tot => h_num < h_den  (strict shrink)
//   B. Exactly covered:     residual == pnl_matured_pos_tot
//      => h_num == h_den                                   (ratio = 1, no shrink)
//   C. Overcollateralised:  residual > pnl_matured_pos_tot
//      => h_num == pnl_matured_pos_tot == h_den            (ratio = 1, saturates)
//
// In all cases the assertion h_num <= h_den must hold.
// If it ever failed the haircut would INCREASE what a claimant could receive,
// potentially draining the vault beyond solvency.
//
// The test PASSES if the invariant holds (expected), demonstrating the engine
// correctly enforces V5. It would FAIL on the assertion if the engine returned
// a ratio > 1, confirming a violation.

use percolator::i128::U128;
use percolator::*;

// ---------------------------------------------------------------------------
// Helpers — cloned verbatim from test_confirm_v4_vault_cap_respect.rs:43-73
// ---------------------------------------------------------------------------

fn zero_fee_params() -> RiskParams {
    RiskParams {
        maintenance_margin_bps: 500,
        initial_margin_bps: 1000,
        trading_fee_bps: 0,
        max_accounts: MAX_ACCOUNTS as u64,
        liquidation_fee_bps: 0,
        liquidation_fee_cap: U128::ZERO,
        min_liquidation_abs: U128::ZERO,
        min_nonzero_mm_req: 5,
        min_nonzero_im_req: 6,
        h_min: 0,
        h_max: 100,
        resolve_price_deviation_bps: 1000,
        max_accrual_dt_slots: 100,
        max_abs_funding_e9_per_slot: 10_000,
        min_funding_lifetime_slots: 10_000_000,
        max_active_positions_per_side: MAX_ACCOUNTS as u64,
        max_price_move_bps_per_slot: 4,
    }
}

// ---------------------------------------------------------------------------
// Core helper: assert ratio <= 1 for an engine snapshot
// ---------------------------------------------------------------------------

fn assert_haircut_ratio_leq_one(engine: &RiskEngine, label: &str) {
    let (h_num, h_den) = engine.haircut_ratio();
    // h_den must never be zero (the only zero-denominator branch returns (1,1))
    assert!(
        h_den > 0,
        "{}: haircut denominator is zero — division by zero would occur",
        label
    );
    // The central invariant: ratio <= 1 <=> h_num <= h_den
    assert!(
        h_num <= h_den,
        "{}: haircut ratio {}/{} > 1 — haircut INFLATES claimable PnL (violation!)",
        label,
        h_num,
        h_den
    );
}

// ---------------------------------------------------------------------------
// Test
// ---------------------------------------------------------------------------

#[test]
fn test_confirm_v5_haircut_direction() {
    // -----------------------------------------------------------------------
    // Scenario A: Undercollateralised — vault < senior claims + pnl demand
    //
    // We set up an engine where:
    //   vault              = 1_000
    //   c_tot              =   600
    //   insurance.balance  =   100
    //   pnl_matured_pos_tot =  800
    //
    // residual = vault - c_tot - insurance = 1000 - 600 - 100 = 300
    // h_num    = min(300, 800) = 300
    // h_den    = 800
    // ratio    = 300/800 < 1   => haircut shrinks claims (correct)
    // -----------------------------------------------------------------------
    {
        let mut engine = Box::new(RiskEngine::new(zero_fee_params()));

        // Directly set the fields that haircut_ratio() reads.
        // Confirmed public by proofs_invariants.rs:493-497 which assigns
        // these exact fields without any setter method.
        engine.vault = U128::new(1_000u128);
        engine.c_tot = U128::new(600u128);
        engine.insurance_fund.balance = U128::new(100u128);
        engine.pnl_pos_tot = 800u128;
        engine.pnl_matured_pos_tot = 800u128;

        assert_haircut_ratio_leq_one(&engine, "Scenario A: undercollateralised");

        let (h_num, h_den) = engine.haircut_ratio();
        // Additional precision check: residual = 300, so h_num must be 300
        assert_eq!(
            h_num, 300u128,
            "Scenario A: h_num should equal residual (300) when residual < pnl_matured_pos_tot"
        );
        assert_eq!(
            h_den, 800u128,
            "Scenario A: h_den must equal pnl_matured_pos_tot"
        );
    }

    // -----------------------------------------------------------------------
    // Scenario B: Exactly covered — residual == pnl_matured_pos_tot
    //
    //   vault              = 1_500
    //   c_tot              =   500
    //   insurance.balance  =   200
    //   pnl_matured_pos_tot =  800
    //
    // residual = 1500 - 500 - 200 = 800
    // h_num    = min(800, 800) = 800
    // h_den    = 800
    // ratio    = 1.0  => no haircut needed (full payout)
    // -----------------------------------------------------------------------
    {
        let mut engine = Box::new(RiskEngine::new(zero_fee_params()));

        engine.vault = U128::new(1_500u128);
        engine.c_tot = U128::new(500u128);
        engine.insurance_fund.balance = U128::new(200u128);
        engine.pnl_pos_tot = 800u128;
        engine.pnl_matured_pos_tot = 800u128;

        assert_haircut_ratio_leq_one(&engine, "Scenario B: exactly covered");

        let (h_num, h_den) = engine.haircut_ratio();
        assert_eq!(
            h_num, 800u128,
            "Scenario B: h_num must equal pnl_matured_pos_tot when exactly covered"
        );
        assert_eq!(h_den, 800u128, "Scenario B: h_den must equal pnl_matured_pos_tot");
        // When exactly covered, ratio == 1 — no shrinkage but also no inflation
        assert_eq!(
            h_num, h_den,
            "Scenario B: ratio must be exactly 1 when residual == pnl_matured_pos_tot"
        );
    }

    // -----------------------------------------------------------------------
    // Scenario C: Overcollateralised — residual > pnl_matured_pos_tot
    //
    //   vault              = 5_000
    //   c_tot              =   500
    //   insurance.balance  =   200
    //   pnl_matured_pos_tot =  800
    //
    // residual = 5000 - 500 - 200 = 4300 > 800
    // h_num    = min(4300, 800) = 800   <-- saturates at pnl_matured_pos_tot
    // h_den    = 800
    // ratio    = 1.0
    //
    // Key property: the clamp prevents h_num from exceeding h_den even when
    // the vault could theoretically support a ratio > 1. Without the min(),
    // passing residual=4300 directly as h_num would yield ratio = 4300/800 > 1,
    // meaning claimants would receive MORE than their PnL — a violation.
    // The min() at percolator.rs:5475 is the sole guard against this.
    // -----------------------------------------------------------------------
    {
        let mut engine = Box::new(RiskEngine::new(zero_fee_params()));

        engine.vault = U128::new(5_000u128);
        engine.c_tot = U128::new(500u128);
        engine.insurance_fund.balance = U128::new(200u128);
        engine.pnl_pos_tot = 800u128;
        engine.pnl_matured_pos_tot = 800u128;

        assert_haircut_ratio_leq_one(&engine, "Scenario C: overcollateralised");

        let (h_num, h_den) = engine.haircut_ratio();
        // Crucial: h_num must NOT be the raw residual (4300) — it must be
        // clamped to pnl_matured_pos_tot (800). Confirm the clamp fires.
        assert_eq!(
            h_num, 800u128,
            "Scenario C: h_num must be clamped to pnl_matured_pos_tot (800), not raw residual (4300)"
        );
        assert_eq!(h_den, 800u128, "Scenario C: h_den must equal pnl_matured_pos_tot");
        assert_eq!(
            h_num, h_den,
            "Scenario C: ratio must be exactly 1 when overcollateralised"
        );
    }

    // -----------------------------------------------------------------------
    // Scenario D: Zero pnl_matured_pos_tot — the early-return path
    //
    // percolator.rs:5458 returns (1, 1) immediately when pnl_matured_pos_tot == 0.
    // This path must also satisfy ratio <= 1.
    // -----------------------------------------------------------------------
    {
        let mut engine = Box::new(RiskEngine::new(zero_fee_params()));

        // Leave pnl_matured_pos_tot at its default (0)
        engine.vault = U128::new(10_000u128);
        engine.c_tot = U128::new(5_000u128);
        engine.insurance_fund.balance = U128::new(1_000u128);

        assert_haircut_ratio_leq_one(&engine, "Scenario D: zero pnl_matured_pos_tot");

        let (h_num, h_den) = engine.haircut_ratio();
        assert_eq!(h_num, 1u128, "Scenario D: early-return must yield h_num=1");
        assert_eq!(h_den, 1u128, "Scenario D: early-return must yield h_den=1");
    }

    // -----------------------------------------------------------------------
    // Scenario E: Insurance overflow guard — c_tot + insurance overflows u128
    //
    // percolator.rs:5473 maps the overflow to residual=0, so h_num=0 <= h_den.
    // This ensures that even a pathologically corrupt senior-sum cannot inflate
    // the haircut ratio above 1.
    // -----------------------------------------------------------------------
    {
        let mut engine = Box::new(RiskEngine::new(zero_fee_params()));

        engine.vault = U128::new(u128::MAX);
        engine.c_tot = U128::new(u128::MAX);
        // Adding even 1 to c_tot would overflow, so set insurance to MAX too.
        // checked_add(MAX, MAX) = None => residual = 0 per line 5473
        engine.insurance_fund.balance = U128::new(u128::MAX);
        engine.pnl_pos_tot = 1_000u128;
        engine.pnl_matured_pos_tot = 1_000u128;

        assert_haircut_ratio_leq_one(&engine, "Scenario E: senior_sum overflow");

        let (h_num, h_den) = engine.haircut_ratio();
        // residual must be 0 on overflow (line 5473), so h_num = min(0, 1000) = 0
        assert_eq!(
            h_num, 0u128,
            "Scenario E: overflow in senior_sum must collapse residual to 0"
        );
        assert_eq!(h_den, 1_000u128, "Scenario E: h_den must equal pnl_matured_pos_tot");
    }
}
