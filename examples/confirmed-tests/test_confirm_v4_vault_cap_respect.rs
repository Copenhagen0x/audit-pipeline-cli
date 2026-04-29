#![cfg(feature = "test")]

// Test: test_confirm_v4_vault_cap_respect
//
// Hypothesis V4-vault-cap-respect: vault is bounded by MAX_VAULT_TVL across
// every reachable state. All vault-increasing paths enforce the cap before
// committing, and assert_public_postconditions_fast provides a backstop.
//
// This test exercises all three vault-increasing public entry points:
//   1. deposit_not_atomic         (guard at percolator.rs:5012)
//   2. top_up_insurance_fund      (guard at percolator.rs:7030)
//   3. deposit_fee_credits        (guard at percolator.rs:7370)
//
// For each path we:
//   (a) drive vault to exactly MAX_VAULT_TVL - 1 via a valid call,
//   (b) confirm the cap-exceeding call returns Err (not panic, not silent bypass),
//   (c) confirm vault was not mutated by the rejected call,
//   (d) confirm the one-unit call that exactly hits MAX_VAULT_TVL is accepted,
//   (e) confirm the next unit call is again rejected.
//
// The invariant is TRUE; the test passes when the engine correctly enforces it.

use percolator::i128::{I128, U128};
use percolator::*;

// ---------------------------------------------------------------------------
// Local helpers (mirrors unit_tests.rs lines 93-114 and 10-43)
// ---------------------------------------------------------------------------

fn default_params() -> RiskParams {
    RiskParams {
        maintenance_margin_bps: 500,
        initial_margin_bps: 1000,
        trading_fee_bps: 10,
        max_accounts: 64,
        liquidation_fee_bps: 100,
        liquidation_fee_cap: U128::new(1_000_000),
        min_liquidation_abs: U128::new(0),
        min_nonzero_mm_req: 10,
        min_nonzero_im_req: 11,
        h_min: 0,
        h_max: 100,
        resolve_price_deviation_bps: 1000,
        max_accrual_dt_slots: 100,
        max_abs_funding_e9_per_slot: 10_000,
        min_funding_lifetime_slots: 10_000_000,
        max_active_positions_per_side: MAX_ACCOUNTS as u64,
        max_price_move_bps_per_slot: 3,
    }
}

fn add_user_test(engine: &mut RiskEngine, _fee_payment: u128) -> Result<u16> {
    let idx = engine.free_head;
    if idx == u16::MAX || (idx as usize) >= MAX_ACCOUNTS {
        return Err(RiskError::Overflow);
    }
    engine.materialize_at(idx, engine.current_slot)?;
    Ok(idx)
}

// ---------------------------------------------------------------------------
// The invariant test
// ---------------------------------------------------------------------------

#[test]
fn test_confirm_v4_vault_cap_respect() {
    // -----------------------------------------------------------------------
    // PATH 1: deposit_not_atomic enforces MAX_VAULT_TVL (percolator.rs:5012)
    // -----------------------------------------------------------------------
    {
        let mut engine = RiskEngine::new(default_params());

        // Materialize a user slot via the back-door (no vault mutation).
        let idx = add_user_test(&mut engine, 0).expect("materialize user");

        // Seed vault to exactly MAX_VAULT_TVL - 2 by depositing into the account.
        // deposit_not_atomic is the canonical vault-increasing path (line 5041).
        let seed = MAX_VAULT_TVL - 2;
        engine
            .deposit_not_atomic(idx, seed, 0)
            .expect("seed deposit must succeed");
        assert_eq!(
            engine.vault.get(),
            seed,
            "vault must equal seed after valid deposit"
        );

        // Deposit 1 more: vault becomes MAX_VAULT_TVL - 1, still under cap.
        engine
            .deposit_not_atomic(idx, 1, 0)
            .expect("deposit to MAX_VAULT_TVL - 1 must succeed");
        assert_eq!(
            engine.vault.get(),
            MAX_VAULT_TVL - 1,
            "vault must be MAX_VAULT_TVL - 1"
        );

        // Attempt to deposit 2 more: would push vault to MAX_VAULT_TVL + 1.
        // Must be rejected (guard at line 5012).
        let vault_before = engine.vault.get();
        let result = engine.deposit_not_atomic(idx, 2, 0);
        assert!(
            result.is_err(),
            "deposit_not_atomic must reject amount that would exceed MAX_VAULT_TVL"
        );
        assert_eq!(
            engine.vault.get(),
            vault_before,
            "vault must be unchanged after rejected deposit"
        );

        // Deposit exactly 1: vault becomes MAX_VAULT_TVL, still within cap.
        engine
            .deposit_not_atomic(idx, 1, 0)
            .expect("deposit to exactly MAX_VAULT_TVL must succeed");
        assert_eq!(
            engine.vault.get(),
            MAX_VAULT_TVL,
            "vault must equal MAX_VAULT_TVL after exact-cap deposit"
        );

        // Any further deposit — even 1 — must be rejected.
        let result2 = engine.deposit_not_atomic(idx, 1, 0);
        assert!(
            result2.is_err(),
            "deposit_not_atomic must reject any amount when vault == MAX_VAULT_TVL"
        );
        assert_eq!(
            engine.vault.get(),
            MAX_VAULT_TVL,
            "vault must remain at MAX_VAULT_TVL after second rejection"
        );

        // The vault never exceeded MAX_VAULT_TVL.
        assert!(
            engine.vault.get() <= MAX_VAULT_TVL,
            "vault invariant: vault <= MAX_VAULT_TVL after deposit path"
        );
    }

    // -----------------------------------------------------------------------
    // PATH 2: top_up_insurance_fund enforces MAX_VAULT_TVL (percolator.rs:7030)
    // -----------------------------------------------------------------------
    {
        let mut engine = RiskEngine::new(default_params());

        // Drive vault to MAX_VAULT_TVL - 1 directly via top_up_insurance_fund.
        // This is itself a vault-increasing call; it must succeed.
        engine
            .top_up_insurance_fund(MAX_VAULT_TVL - 1, 0)
            .expect("top_up to MAX_VAULT_TVL - 1 must succeed");
        assert_eq!(
            engine.vault.get(),
            MAX_VAULT_TVL - 1,
            "vault must equal MAX_VAULT_TVL - 1 after top_up"
        );
        assert_eq!(
            engine.insurance_fund.balance.get(),
            MAX_VAULT_TVL - 1,
            "insurance must equal vault after top_up (no user capital)"
        );

        // Attempt +2: would exceed cap. Must be rejected (guard at line 7030).
        let vault_before = engine.vault.get();
        let result = engine.top_up_insurance_fund(2, 0);
        assert!(
            result.is_err(),
            "top_up_insurance_fund must reject amount that would exceed MAX_VAULT_TVL"
        );
        assert_eq!(
            engine.vault.get(),
            vault_before,
            "vault must be unchanged after rejected top_up"
        );

        // Attempt +1: lands exactly on MAX_VAULT_TVL. Must succeed.
        engine
            .top_up_insurance_fund(1, 0)
            .expect("top_up to exactly MAX_VAULT_TVL must succeed");
        assert_eq!(
            engine.vault.get(),
            MAX_VAULT_TVL,
            "vault must equal MAX_VAULT_TVL after exact-cap top_up"
        );

        // Any further top_up must be rejected.
        let result2 = engine.top_up_insurance_fund(1, 0);
        assert!(
            result2.is_err(),
            "top_up_insurance_fund must reject any amount when vault == MAX_VAULT_TVL"
        );
        assert_eq!(
            engine.vault.get(),
            MAX_VAULT_TVL,
            "vault must remain at MAX_VAULT_TVL after second rejection (top_up)"
        );

        assert!(
            engine.vault.get() <= MAX_VAULT_TVL,
            "vault invariant: vault <= MAX_VAULT_TVL after top_up path"
        );
    }

    // -----------------------------------------------------------------------
    // PATH 3: deposit_fee_credits enforces MAX_VAULT_TVL (percolator.rs:7370)
    //
    // deposit_fee_credits only increases vault by pay = min(amount, fee_debt).
    // To stress-test the cap we:
    //   (a) seed vault to MAX_VAULT_TVL - 1 via top_up_insurance_fund,
    //   (b) give the account fee_credits debt >= 2,
    //   (c) call deposit_fee_credits(amount=2): pay=2, would push vault to
    //       MAX_VAULT_TVL + 1 → must be rejected (guard at line 7370),
    //   (d) call deposit_fee_credits(amount=1): pay=1 → lands on MAX_VAULT_TVL,
    //       must be accepted,
    //   (e) call deposit_fee_credits(amount=1) again: pay=0 (debt now 0) →
    //       no vault mutation, must succeed with pay==0.
    // -----------------------------------------------------------------------
    {
        let mut engine = RiskEngine::new(default_params());

        // Materialize a user slot.
        let idx = add_user_test(&mut engine, 0).expect("materialize user for fee_credits path");

        // Seed vault to MAX_VAULT_TVL - 1 through the insurance top_up path.
        engine
            .top_up_insurance_fund(MAX_VAULT_TVL - 1, 0)
            .expect("top_up to MAX_VAULT_TVL - 1 must succeed");
        assert_eq!(engine.vault.get(), MAX_VAULT_TVL - 1);

        // Give the account a fee_credits debt of 10 (spec §2.1: fee_credits <= 0).
        engine.accounts[idx as usize].fee_credits = I128::new(-10);

        // deposit_fee_credits(amount=2, now_slot=0):
        // pay = min(2, 10) = 2; new_vault = (MAX_VAULT_TVL - 1) + 2 > MAX_VAULT_TVL.
        // Must be rejected — guard at line 7370.
        let vault_before = engine.vault.get();
        let result = engine.deposit_fee_credits(idx, 2, 0);
        assert!(
            result.is_err(),
            "deposit_fee_credits must reject pay that would exceed MAX_VAULT_TVL (got {:?})",
            result
        );
        assert_eq!(
            engine.vault.get(),
            vault_before,
            "vault must be unchanged after rejected deposit_fee_credits"
        );
        // fee_credits must also be unchanged (validate-then-mutate).
        assert_eq!(
            engine.accounts[idx as usize].fee_credits.get(),
            -10,
            "fee_credits must be unchanged after rejected deposit_fee_credits"
        );

        // deposit_fee_credits(amount=1, now_slot=0):
        // pay = min(1, 10) = 1; new_vault = MAX_VAULT_TVL → exactly at cap.
        // Must succeed.
        let paid = engine
            .deposit_fee_credits(idx, 1, 0)
            .expect("deposit_fee_credits to exactly MAX_VAULT_TVL must succeed");
        assert_eq!(paid, 1, "pay must be 1");
        assert_eq!(
            engine.vault.get(),
            MAX_VAULT_TVL,
            "vault must equal MAX_VAULT_TVL after exact-cap deposit_fee_credits"
        );
        assert_eq!(
            engine.accounts[idx as usize].fee_credits.get(),
            -9,
            "fee_credits must decrease by pay"
        );

        // Vault never exceeded MAX_VAULT_TVL.
        assert!(
            engine.vault.get() <= MAX_VAULT_TVL,
            "vault invariant: vault <= MAX_VAULT_TVL after deposit_fee_credits path"
        );
    }

    // -----------------------------------------------------------------------
    // GLOBAL: overflow / u128::MAX inputs are safely rejected, not panicked
    // -----------------------------------------------------------------------
    {
        let mut engine = RiskEngine::new(default_params());
        let idx = add_user_test(&mut engine, 0).expect("materialize user");

        // u128::MAX into deposit_not_atomic must return Err (checked_add fails).
        let r1 = engine.deposit_not_atomic(idx, u128::MAX, 0);
        assert!(
            r1.is_err(),
            "deposit_not_atomic(u128::MAX) must return Err, not panic"
        );
        assert_eq!(engine.vault.get(), 0, "vault must remain 0 after overflow rejection");

        // u128::MAX into top_up_insurance_fund must return Err.
        let r2 = engine.top_up_insurance_fund(u128::MAX, 0);
        assert!(
            r2.is_err(),
            "top_up_insurance_fund(u128::MAX) must return Err, not panic"
        );
        assert_eq!(engine.vault.get(), 0, "vault must remain 0 after overflow rejection");
    }
}
