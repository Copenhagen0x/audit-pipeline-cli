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
// The test drives four concrete scenarios:
//   A. Undercollateralised: vault < c_tot + insurance + pnl_matured_pos_tot
//      => residual < pnl_matured_pos_tot => h_num < h_den  (strict shrink)
//   B. Exactly covered:     residual == pnl_matured_pos_tot
//      => h_num == h_den                                   (ratio = 1, no shrink)
//   C. Overcollateralised:  residual > pnl_matured_pos_tot
//      => h_num == pnl_matured_pos_tot == h_den            (ratio = 1, saturates)
//   D. Zero pnl_matured_pos_tot: early-return (1,1)        (ratio = 1)
//
// In all cases the assertion h_num <= h_den must hold.
// If it ever failed the haircut would INCREASE what a claimant could receive,
// potentially draining the vault beyond solvency.

use percolator::i128::U128;
use percolator::*;

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

fn assert_haircut_ratio_leq_one(engine: &RiskEngine, label: &str) {
    let (h_num, h_den) = engine.haircut_ratio();
    assert!(
        h_den > 0,
        "{}: haircut denominator is zero — division by zero would occur",
        label
    );
    assert!(
        h_num <= h_den,
        "{}: haircut ratio {}/{} > 1 — haircut INFLATES claimable PnL (violation!)",
        label,
        h_num,
        h_den
    );
}

#[test]
fn test_confirm_v5_haircut_direction() {
    // Scenario A: Undercollateralised
    {
        let mut engine = Box::new(RiskEngine::new(zero_fee_params()));
        engine.vault = U128::new(1_000u128);
        engine.c_tot = U128::new(600u128);
        engine.insurance_fund.balance = U128::new(100u128);
        engine.pnl_pos_tot = 800u128;
        engine.pnl_matured_pos_tot = 800u128;

        assert_haircut_ratio_leq_one(&engine, "Scenario A: undercollateralised");

        let (h_num, h_den) = engine.haircut_ratio();
        // residual = 1000 - 600 - 100 = 300 < 800 = pnl_matured_pos_tot
        assert_eq!(h_num, 300u128, "Scenario A: h_num should equal residual (300)");
        assert_eq!(h_den, 800u128, "Scenario A: h_den must equal pnl_matured_pos_tot");
    }

    // Scenario B: Exactly covered
    {
        let mut engine = Box::new(RiskEngine::new(zero_fee_params()));
        engine.vault = U128::new(1_500u128);
        engine.c_tot = U128::new(500u128);
        engine.insurance_fund.balance = U128::new(200u128);
        engine.pnl_pos_tot = 800u128;
        engine.pnl_matured_pos_tot = 800u128;

        assert_haircut_ratio_leq_one(&engine, "Scenario B: exactly covered");

        let (h_num, h_den) = engine.haircut_ratio();
        // residual = 1500 - 500 - 200 = 800 == pnl_matured_pos_tot
        assert_eq!(h_num, 800u128, "Scenario B: h_num must equal pnl_matured_pos_tot");
        assert_eq!(h_den, 800u128, "Scenario B: h_den must equal pnl_matured_pos_tot");
        assert_eq!(h_num, h_den, "Scenario B: ratio must be exactly 1");
    }

    // Scenario C: Overcollateralised — clamp must fire
    {
        let mut engine = Box::new(RiskEngine::new(zero_fee_params()));
        engine.vault = U128::new(5_000u128);
        engine.c_tot = U128::new(500u128);
        engine.insurance_fund.balance = U128::new(200u128);
        engine.pnl_pos_tot = 800u128;
        engine.pnl_matured_pos_tot = 800u128;

        assert_haircut_ratio_leq_one(&engine, "Scenario C: overcollateralised");

        let (h_num, h_den) = engine.haircut_ratio();
        // residual = 5000 - 500 - 200 = 4300 > 800; clamp must reduce to 800
        assert_eq!(
            h_num, 800u128,
            "Scenario C: h_num must be clamped to pnl_matured_pos_tot (800), not raw residual (4300)"
        );
        assert_eq!(h_den, 800u128, "Scenario C: h_den must equal pnl_matured_pos_tot");
        assert_eq!(h_num, h_den, "Scenario C: ratio must be exactly 1 when overcollateralised");
    }

    // Scenario D: Zero pnl_matured_pos_tot — early-return path
    {
        let mut engine = Box::new(RiskEngine::new(zero_fee_params()));
        engine.vault = U128::new(10_000u128);
        engine.c_tot = U128::new(5_000u128);
        engine.insurance_fund.balance = U128::new(1_000u128);
        // pnl_matured_pos_tot left at 0

        assert_haircut_ratio_leq_one(&engine, "Scenario D: zero pnl_matured_pos_tot");

        let (h_num, h_den) = engine.haircut_ratio();
        assert_eq!(h_num, 1u128, "Scenario D: early-return must yield h_num=1");
        assert_eq!(h_den, 1u128, "Scenario D: early-return must yield h_den=1");
    }
}
