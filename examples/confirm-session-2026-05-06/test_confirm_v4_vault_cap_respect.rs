#![cfg(feature = "test")]

// Test: test_confirm_v4_vault_cap_respect
//
// Finding hypothesis (V4-vault-cap-respect): Vault balance is provably bounded
// by MAX_VAULT_TVL across every reachable state. No accounting helper can push
// vault past this cap silently.
//
// The hunt-deep agent claimed MAX_VAULT_TVL did not exist and no enforcement
// was present. Tool investigation refutes that claim entirely:
//
//   percolator.rs:191  -> pub const MAX_VAULT_TVL: u128 = 10_000_000_000_000_000;
//
//   Enforcement sites (all pre-validate, then reject with Err before any commit):
//   percolator.rs:7339-7346  -> deposit_not_atomic:        v_candidate > MAX_VAULT_TVL => Err
//   percolator.rs:10178-10184 -> top_up_insurance_fund:   new_vault   > MAX_VAULT_TVL => Err
//   percolator.rs:10558-10564 -> deposit_fee_credits path: new_vault  > MAX_VAULT_TVL => Err
//   percolator.rs:6050-6054  -> assert_public_postconditions_fast (post-check belt):
//                               vault > MAX_VAULT_TVL => Err(CorruptState)
//
//   Every public vault-incrementing path guards the cap before committing.
//   The post-condition check runs after every public instruction, so any
//   bypass would be caught there too.
//
// Test strategy:
//   1. Build an engine and make deposits that approach MAX_VAULT_TVL.
//   2. Verify that a deposit that would push vault to exactly MAX_VAULT_TVL
//      succeeds (boundary is inclusive on the safe side).
//   3. Verify that a deposit of even 1 atom beyond MAX_VAULT_TVL is rejected
//      with an error and leaves vault unchanged.
//   4. Verify the same cap is enforced by top_up_insurance_fund.
//   5. Assert check_conservation() holds throughout.
//
// This test PASSES if the invariant holds (the engine correctly enforces the
// cap). It would FAIL on the assertion if the engine silently accepted a
// deposit beyond MAX_VAULT_TVL, confirming a violation.

use percolator::i128::U128;
use percolator::*;

// Minimal zero-fee params cloned from test_confirm_ac1_account_gc_state_leak.rs
// (lines 38-57) and test_confirm_v2_vault_balance_equation.rs (lines 44-68).
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

// Materialize a user slot via the test-visible back-door (percolator.rs:1690+).
// Pattern from amm_tests.rs lines 44-51 and test_confirm_ac1 lines 60-67.
fn add_user_test(engine: &mut RiskEngine, _fee_payment: u128) -> Result<u16> {
    let idx = engine.free_head;
    if idx == u16::MAX || (idx as usize) >= MAX_ACCOUNTS {
        return Err(RiskError::Overflow);
    }
    engine.materialize_at(idx, engine.current_slot)?;
    Ok(idx)
}

#[test]
fn test_confirm_v4_vault_cap_respect() {
    // -------------------------------------------------------------------------
    // Phase 0: Establish engine at slot 0.
    // RiskEngine::new() calls new_with_market(params, 0, 1) per percolator.rs:1691.
    // -------------------------------------------------------------------------
    let mut engine = Box::new(RiskEngine::new(zero_fee_params()));

    // Materialize one user account. deposit_not_atomic requires the account to
    // exist (or creates it on first deposit with amount>0, percolator.rs:7356-7361).
    // We use the test back-door so we control the slot independently.
    let alice = add_user_test(&mut engine, 0).unwrap();

    // -------------------------------------------------------------------------
    // Phase 1: Deposit a large amount that sits just below the cap.
    //
    // MAX_VAULT_TVL = 10_000_000_000_000_000 (percolator.rs line 191).
    // We deposit (MAX_VAULT_TVL - 1) to bring vault to one atom below the cap.
    // This must succeed because the check at percolator.rs:7344 is strict (>),
    // so vault == MAX_VAULT_TVL - 1 is within the allowed range.
    // -------------------------------------------------------------------------
    let cap: u128 = MAX_VAULT_TVL;
    let below_cap: u128 = cap - 1;

    let result_below = engine.deposit_not_atomic(alice, below_cap, 0);
    assert!(
        result_below.is_ok(),
        "deposit of MAX_VAULT_TVL-1 must succeed; got {:?}",
        result_below
    );

    let vault_after_below = engine.vault.get();
    assert_eq!(
        vault_after_below,
        below_cap,
        "vault must equal the deposited amount ({}) after first deposit, got {}",
        below_cap,
        vault_after_below
    );

    // conservation must hold immediately after the deposit
    assert!(
        engine.check_conservation(),
        "conservation violated after below-cap deposit"
    );

    // -------------------------------------------------------------------------
    // Phase 2: Deposit exactly 1 more atom to reach the cap boundary.
    //
    // vault is currently MAX_VAULT_TVL - 1. Adding 1 yields exactly
    // MAX_VAULT_TVL. The guard at percolator.rs:7344 is:
    //   if v_candidate > MAX_VAULT_TVL { return Err(...) }
    // So v_candidate == MAX_VAULT_TVL (not strictly greater) must be allowed.
    // -------------------------------------------------------------------------

    // We need a second user slot because deposit_not_atomic calls
    // validate_touched_account_shape_at_fee_slot which touches the last-fee
    // slot; reusing alice at the same slot=0 is fine as long as we haven't
    // advanced. Use alice again — slot is still 0, which is valid.
    let result_at_cap = engine.deposit_not_atomic(alice, 1, 0);
    assert!(
        result_at_cap.is_ok(),
        "deposit bringing vault to exactly MAX_VAULT_TVL must succeed; got {:?}",
        result_at_cap
    );

    let vault_at_cap = engine.vault.get();
    assert_eq!(
        vault_at_cap,
        cap,
        "vault must equal MAX_VAULT_TVL ({}) at boundary, got {}",
        cap,
        vault_at_cap
    );

    assert!(
        engine.check_conservation(),
        "conservation violated at cap boundary"
    );

    // -------------------------------------------------------------------------
    // Phase 3: Attempt to deposit 1 more atom OVER the cap (the critical test).
    //
    // vault is now MAX_VAULT_TVL. Any positive deposit would make
    // v_candidate = MAX_VAULT_TVL + 1 > MAX_VAULT_TVL, which must be
    // rejected by percolator.rs:7344-7346 before any state mutation.
    //
    // The test FAILS here (asserting is_err()) if the engine silently accepts
    // the over-cap deposit, demonstrating a violation of the invariant.
    // -------------------------------------------------------------------------
    let vault_before_over = engine.vault.get();

    let result_over = engine.deposit_not_atomic(alice, 1, 0);
    assert!(
        result_over.is_err(),
        "deposit exceeding MAX_VAULT_TVL must be rejected; engine accepted it (vault = {})",
        engine.vault.get()
    );

    // Vault must not have changed — validate-then-mutate contract (percolator.rs:7338).
    let vault_after_over = engine.vault.get();
    assert_eq!(
        vault_after_over,
        vault_before_over,
        "vault must be unchanged after rejected over-cap deposit; was {} now {}",
        vault_before_over,
        vault_after_over
    );

    // Cap must still hold after the failed attempt.
    assert!(
        vault_after_over <= MAX_VAULT_TVL,
        "vault {} exceeds MAX_VAULT_TVL {} after rejected deposit",
        vault_after_over,
        MAX_VAULT_TVL
    );

    assert!(
        engine.check_conservation(),
        "conservation violated after rejected over-cap deposit"
    );

    // -------------------------------------------------------------------------
    // Phase 4: Same enforcement via top_up_insurance_fund.
    //
    // top_up_insurance_fund ALSO increments vault (percolator.rs:10178-10184).
    // With vault == MAX_VAULT_TVL, any positive top-up must be rejected.
    // -------------------------------------------------------------------------
    let result_ins_over = engine.top_up_insurance_fund(1, 0);
    assert!(
        result_ins_over.is_err(),
        "top_up_insurance_fund exceeding MAX_VAULT_TVL must be rejected; engine accepted it"
    );

    let vault_after_ins = engine.vault.get();
    assert_eq!(
        vault_after_ins,
        MAX_VAULT_TVL,
        "vault must remain at MAX_VAULT_TVL after rejected top-up; got {}",
        vault_after_ins
    );

    assert!(
        engine.check_conservation(),
        "conservation violated after rejected insurance top-up"
    );

    // -------------------------------------------------------------------------
    // Phase 5: Verify the post-condition check (belt-and-suspenders layer).
    //
    // percolator.rs:6050-6054: assert_public_postconditions_fast() independently
    // checks vault <= MAX_VAULT_TVL and returns Err(CorruptState) if violated.
    // Since the engine already enforces the cap at every mutation site, the
    // post-condition check must also pass (no CorruptState from vault overflow).
    //
    // We verify this by confirming vault is still exactly at cap and the engine
    // state is self-consistent.
    // -------------------------------------------------------------------------
    assert_eq!(
        engine.vault.get(),
        MAX_VAULT_TVL,
        "final vault must equal MAX_VAULT_TVL; got {}",
        engine.vault.get()
    );

    // The invariant holds: vault is bounded by MAX_VAULT_TVL and every
    // over-cap operation was rejected before mutating state.
    // Finding V4-vault-cap-respect verdict: TRUE (invariant holds, the
    // hunt-deep analysis was incorrect — the constant and all enforcement
    // sites exist at percolator.rs lines 191, 7344, 10183, 10563, 6053).
}
