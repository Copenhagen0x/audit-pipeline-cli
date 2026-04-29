#![cfg(feature = "test")]

// Test: test_confirm_o7_position_zero_clears_basis
//
// Hypothesis O7-position-zero-clears-basis: When position_basis_q reaches
// exactly 0, the four companion basis fields (adl_a_basis, adl_k_snap,
// f_snap, adl_epoch_snap) are zeroed atomically. Subsequent fills do not
// inherit stale basis values.
//
// The invariant is confirmed TRUE by the source analysis:
//
// 1. attach_effective_position_inner zero-branch (percolator.rs:2269-2276):
//    When new_eff_pos_q == 0, ALL four fields are reset in one if-block:
//      adl_a_basis  <- ADL_ONE
//      adl_k_snap   <- 0
//      f_snap       <- 0
//      adl_epoch_snap <- 0
//
// 2. settle_side_effects_live same-epoch dust-out (percolator.rs:2718-2724):
//    Identical four-field reset when q_eff_new == 0.
//
// 3. settle_side_effects_live epoch-mismatch path (percolator.rs:2753-2758):
//    Identical four-field reset unconditionally.
//
// 4. Subsequent non-zero attach (percolator.rs:2290-2302): unconditionally
//    overwrites all four fields with live market-side values, so even a
//    hypothetically stale value after zero would be clobbered before use.
//
// 5. settle_side_effects_live bails early when basis==0 (percolator.rs:2691),
//    so a flat account never reaches the PnL-delta arithmetic that reads
//    adl_k_snap / f_snap.
//
// This test exercises the direct execute_trade_not_atomic path:
//   Phase A: Alice (long) + Bob (short) open matched positions.
//   Phase B: Alice fully closes (trade back to zero).
//   Invariant check: Alice's four companion fields must be in canonical
//                    zero-position state after close.
//   Phase C: Alice reopens a position.
//   Invariant check: Alice's four companion fields must now reflect the
//                    live market-side values, not the stale values from
//                    the earlier non-zero position.
//
// The test PASSES when the invariant holds (i.e., the engine is correct).

use percolator::i128::U128;
use percolator::*;

// ---------------------------------------------------------------------------
// Local helpers (mirrors amm_tests.rs lines 10-85)
// ---------------------------------------------------------------------------

fn default_params() -> RiskParams {
    RiskParams {
        maintenance_margin_bps: 3000,
        initial_margin_bps: 3500,
        trading_fee_bps: 10,
        max_accounts: 64,
        liquidation_fee_bps: 50,
        liquidation_fee_cap: U128::new(100_000),
        min_liquidation_abs: U128::new(0),
        min_nonzero_mm_req: 31,
        min_nonzero_im_req: 32,
        h_min: 0,
        h_max: 100,
        resolve_price_deviation_bps: 1000,
        max_accrual_dt_slots: 200,
        max_abs_funding_e9_per_slot: 10_000,
        min_funding_lifetime_slots: 10_000_000,
        max_active_positions_per_side: MAX_ACCOUNTS as u64,
        max_price_move_bps_per_slot: 14,
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

// Scale an integer quantity into the engine's internal position units.
// POS_SCALE = 1_000_000 (percolator.rs:100).
fn pos_q(qty: i64) -> i128 {
    let abs_val = (qty as i128).unsigned_abs();
    let scaled = abs_val.checked_mul(POS_SCALE).unwrap();
    if qty < 0 {
        -(scaled as i128)
    } else {
        scaled as i128
    }
}

// ---------------------------------------------------------------------------
// The invariant test
// ---------------------------------------------------------------------------

#[test]
fn test_confirm_o7_position_zero_clears_basis() {
    // -----------------------------------------------------------------------
    // Setup: fresh engine, insurance fund, two users with capital.
    // -----------------------------------------------------------------------
    let mut engine = Box::new(RiskEngine::new(default_params()));

    // Seed the insurance fund so the engine has a non-zero vault floor.
    engine
        .top_up_insurance_fund(500_000, 0)
        .expect("insurance top-up must succeed");

    let alice = add_user_test(&mut engine, 0).expect("alice materialize");
    let bob = add_user_test(&mut engine, 0).expect("bob materialize");

    let oracle_price: u64 = 100;
    let slot0: u64 = 0;

    engine
        .deposit_not_atomic(alice, 500_000, slot0)
        .expect("alice deposit");
    engine
        .deposit_not_atomic(bob, 500_000, slot0)
        .expect("bob deposit");

    // Initial crank: advance market to slot0 with oracle_price.
    let _ = engine.keeper_crank_not_atomic(
        slot0,
        oracle_price,
        &[],
        64,
        0i128,
        0,
        100,
        None,
        0,
    );

    // -----------------------------------------------------------------------
    // Phase A: Open positions — Alice long 10, Bob short 10.
    //
    // execute_trade_not_atomic signature (percolator.rs:5291-5303):
    //   a, b, oracle_price, now_slot, size_q, exec_price,
    //   funding_rate_e9, admit_h_min, admit_h_max,
    //   admit_h_max_consumption_threshold_bps_opt
    //
    // size_q > 0 means `a` receives that many base units (goes long),
    // `b` gives them (goes short).
    // -----------------------------------------------------------------------
    let trade_size = pos_q(10); // 10 base units in engine units

    engine
        .execute_trade_not_atomic(
            alice,
            bob,
            oracle_price,
            slot0,
            trade_size,
            oracle_price,
            0i128,
            0,
            100,
            None,
        )
        .expect("phase-A open trade must succeed");

    // Verify positions were established.
    let alice_eff_after_open = engine.effective_pos_q(alice as usize);
    let bob_eff_after_open = engine.effective_pos_q(bob as usize);
    assert!(
        alice_eff_after_open > 0,
        "alice must be long after phase-A open (got {})",
        alice_eff_after_open
    );
    assert!(
        bob_eff_after_open < 0,
        "bob must be short after phase-A open (got {})",
        bob_eff_after_open
    );

    // Record alice's companion-field values while she holds a live position.
    // These should be non-canonical (adl_a_basis != ADL_ONE, or epoch != 0),
    // confirming the fields were actually written by attach_effective_position_inner
    // (percolator.rs:2290-2302).
    let alice_a_basis_open = engine.accounts[alice as usize].adl_a_basis;
    let _alice_k_snap_open = engine.accounts[alice as usize].adl_k_snap;
    let _alice_f_snap_open = engine.accounts[alice as usize].f_snap;
    let _alice_epoch_snap_open = engine.accounts[alice as usize].adl_epoch_snap;

    // adl_a_basis for a live position must be set (ADL_ONE at market init,
    // so it equals ADL_ONE here since no ADL has occurred yet — but it was
    // written by the non-zero branch of attach_effective_position_inner).
    assert_eq!(
        alice_a_basis_open, ADL_ONE,
        "adl_a_basis must equal ADL_ONE at a freshly opened position (market at ADL_ONE)"
    );

    // position_basis_q must equal alice's effective position.
    let alice_basis_open = engine.accounts[alice as usize].position_basis_q;
    assert_eq!(
        alice_basis_open, alice_eff_after_open,
        "position_basis_q must match effective_pos_q for a live long position"
    );

    // -----------------------------------------------------------------------
    // Phase B: Alice fully closes her position.
    //
    // To close alice (long +trade_size), bob buys from alice:
    // We call execute_trade_not_atomic(bob, alice, ..., trade_size, ...)
    // so alice gives trade_size units (goes from +trade_size to 0).
    // -----------------------------------------------------------------------
    let slot1: u64 = 1;
    engine
        .execute_trade_not_atomic(
            bob,
            alice,
            oracle_price,
            slot1,
            trade_size,
            oracle_price,
            0i128,
            0,
            100,
            None,
        )
        .expect("phase-B close trade must succeed");

    // -----------------------------------------------------------------------
    // Core invariant check: after close, alice's position_basis_q == 0 and
    // ALL four companion fields must be in canonical zero-position state.
    //
    // Zero-state canonical values (percolator.rs:2269-2276, also init at
    // percolator.rs:1494-1498):
    //   position_basis_q == 0
    //   adl_a_basis      == ADL_ONE  (= 1_000_000_000_000_000)
    //   adl_k_snap       == 0
    //   f_snap           == 0
    //   adl_epoch_snap   == 0
    // -----------------------------------------------------------------------
    let alice_eff_after_close = engine.effective_pos_q(alice as usize);
    assert_eq!(
        alice_eff_after_close, 0,
        "alice effective position must be exactly 0 after full close"
    );

    let alice_basis_after_close = engine.accounts[alice as usize].position_basis_q;
    assert_eq!(
        alice_basis_after_close, 0,
        "alice position_basis_q must be 0 after full close (invariant: zero clears basis)"
    );

    let alice_adl_a_basis_after_close = engine.accounts[alice as usize].adl_a_basis;
    assert_eq!(
        alice_adl_a_basis_after_close, ADL_ONE,
        "alice adl_a_basis must be reset to ADL_ONE when position reaches zero \
         (percolator.rs:2273 — atomic zero-branch in attach_effective_position_inner)"
    );

    let alice_adl_k_snap_after_close = engine.accounts[alice as usize].adl_k_snap;
    assert_eq!(
        alice_adl_k_snap_after_close, 0i128,
        "alice adl_k_snap must be zeroed when position_basis_q reaches 0 \
         (percolator.rs:2274 — atomic zero-branch in attach_effective_position_inner)"
    );

    let alice_f_snap_after_close = engine.accounts[alice as usize].f_snap;
    assert_eq!(
        alice_f_snap_after_close, 0i128,
        "alice f_snap must be zeroed when position_basis_q reaches 0 \
         (percolator.rs:2275 — atomic zero-branch in attach_effective_position_inner)"
    );

    let alice_epoch_snap_after_close = engine.accounts[alice as usize].adl_epoch_snap;
    assert_eq!(
        alice_epoch_snap_after_close, 0u64,
        "alice adl_epoch_snap must be zeroed when position_basis_q reaches 0 \
         (percolator.rs:2276 — atomic zero-branch in attach_effective_position_inner)"
    );

    // settle_side_effects_live must return Ok(()) immediately for a flat
    // account (percolator.rs:2691: if basis == 0 { return Ok(()); }).
    // Confirm by calling it and checking it does not mutate PnL.
    let alice_pnl_before_settle = engine.accounts[alice as usize].pnl;
    {
        let mut ctx = InstructionContext::new_with_admission(0, 100);
        engine
            .settle_side_effects_live(alice as usize, &mut ctx)
            .expect("settle_side_effects_live on flat account must succeed");
    }
    let alice_pnl_after_settle = engine.accounts[alice as usize].pnl;
    assert_eq!(
        alice_pnl_after_settle, alice_pnl_before_settle,
        "settle_side_effects_live on flat account must be a no-op (early return at \
         percolator.rs:2691) — PnL must not change"
    );

    // The four fields must still be canonical after the no-op settle.
    assert_eq!(
        engine.accounts[alice as usize].position_basis_q, 0,
        "position_basis_q must remain 0 after no-op settle"
    );
    assert_eq!(
        engine.accounts[alice as usize].adl_a_basis, ADL_ONE,
        "adl_a_basis must remain ADL_ONE after no-op settle"
    );
    assert_eq!(
        engine.accounts[alice as usize].adl_k_snap, 0i128,
        "adl_k_snap must remain 0 after no-op settle"
    );
    assert_eq!(
        engine.accounts[alice as usize].f_snap, 0i128,
        "f_snap must remain 0 after no-op settle"
    );
    assert_eq!(
        engine.accounts[alice as usize].adl_epoch_snap, 0u64,
        "adl_epoch_snap must remain 0 after no-op settle"
    );

    // Conservation must hold throughout.
    assert!(
        engine.check_conservation(),
        "conservation must hold after close"
    );

    // -----------------------------------------------------------------------
    // Phase C: Alice reopens a position (long 5 base units).
    // Verify the companion fields are written fresh (not stale from before
    // the close), consistent with percolator.rs:2290-2302 unconditional
    // overwrite in the non-zero branch of attach_effective_position_inner.
    // -----------------------------------------------------------------------
    let reopen_size = pos_q(5);
    let slot2: u64 = 2;
    engine
        .execute_trade_not_atomic(
            alice,
            bob,
            oracle_price,
            slot2,
            reopen_size,
            oracle_price,
            0i128,
            0,
            100,
            None,
        )
        .expect("phase-C reopen trade must succeed");

    let alice_eff_after_reopen = engine.effective_pos_q(alice as usize);
    assert!(
        alice_eff_after_reopen > 0,
        "alice must be long again after phase-C reopen (got {})",
        alice_eff_after_reopen
    );

    // After a new position is attached, the companion fields must reflect
    // fresh market-side values (percolator.rs:2290-2302).
    // In particular, adl_a_basis must still equal ADL_ONE (market state
    // has not moved), and the epoch/k/f snaps are taken from current
    // market state — no stale value from the pre-close position is present.
    let alice_a_basis_reopen = engine.accounts[alice as usize].adl_a_basis;
    assert_eq!(
        alice_a_basis_reopen,
        engine.adl_mult_long,
        "adl_a_basis after reopen must equal current adl_mult_long (no stale inheritance)"
    );

    let alice_k_snap_reopen = engine.accounts[alice as usize].adl_k_snap;
    assert_eq!(
        alice_k_snap_reopen,
        engine.adl_coeff_long,
        "adl_k_snap after reopen must equal current adl_coeff_long (no stale inheritance)"
    );

    let alice_f_snap_reopen = engine.accounts[alice as usize].f_snap;
    assert_eq!(
        alice_f_snap_reopen,
        engine.f_long_num,
        "f_snap after reopen must equal current f_long_num (no stale inheritance)"
    );

    let alice_epoch_snap_reopen = engine.accounts[alice as usize].adl_epoch_snap;
    assert_eq!(
        alice_epoch_snap_reopen,
        engine.adl_epoch_long,
        "adl_epoch_snap after reopen must equal current adl_epoch_long (no stale inheritance)"
    );

    // Final conservation check.
    assert!(
        engine.check_conservation(),
        "conservation must hold after reopen"
    );
}
