#![cfg(feature = "test")]

// Test: test_confirm_s5_market_mode_transitions
//
// Finding hypothesis (S5-market-mode-transitions): Market mode transitions
// (Live -> Resolved) are one-way and irreversible. There is no code path that
// transitions the engine back from Resolved to Live without re-initialising
// the entire engine.
//
// Evidence gathered from percolator.rs (line numbers verified by tool reads):
//
//   percolator.rs:242-244  -- MarketMode enum: Live=0, Resolved=1 (only two variants,
//                             no "Halted" variant exists in this codebase)
//   percolator.rs:743      -- RiskEngine.market_mode: MarketMode field is pub
//   percolator.rs:9526-9528 -- resolve_market_not_atomic: first guard is
//                              `if self.market_mode != MarketMode::Live { return Err(Unauthorized) }`
//                              meaning it is idempotent-blocked once Resolved
//   percolator.rs:9618     -- only write of MarketMode::Resolved inside resolve_market_not_atomic
//   percolator.rs:9446     -- second write of MarketMode::Resolved inside
//                              resolve_counter_or_epoch_overflow_recovery_not_atomic
//   percolator.rs:1840     -- only write of MarketMode::Live is inside init_in_place
//                             (the full reset constructor) -- no transition-back function exists
//   percolator.rs:7322-7324 -- deposit_not_atomic: returns Err(Unauthorized) if not Live
//   percolator.rs:4079     -- accrue_market_to internal: returns Ok(()) early if not Live
//   percolator.rs:4701     -- accrue_market_segment: returns Ok(()) if not Live
//
// Test strategy:
//   1. Create a fresh engine in Live mode. Assert market_mode == Live.
//   2. Resolve the market via resolve_market_not_atomic. Assert mode == Resolved.
//   3. Attempt to call resolve_market_not_atomic a second time (Live->Resolved
//      transition) on an already-Resolved engine. Assert it is rejected with
//      Err(Unauthorized) -- the one-way invariant holds.
//   4. Attempt deposit_not_atomic on the Resolved engine. Assert it is rejected
//      with Err(Unauthorized) -- Live-only operations are gated.
//   5. Assert the market_mode field is still Resolved and was not flipped back.
//
// This test PASSES if the invariant holds (transitions are one-way and the
// engine correctly blocks both re-resolution and Live-only operations after
// resolution). It would FAIL if any of these operations silently accepted and
// mutated market_mode back to Live.

use percolator::i128::U128;
use percolator::*;

// Minimal zero-fee params following the exact pattern from
// test_confirm_ac1_account_gc_state_leak.rs lines 38-57 and
// test_confirm_v4_vault_cap_respect.rs lines 43-63.
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
        // Wide deviation band so resolve_market_not_atomic ordinary branch
        // does not reject on price deviation (percolator.rs:9577-9587).
        resolve_price_deviation_bps: 10_000,
        max_accrual_dt_slots: 100,
        max_abs_funding_e9_per_slot: 10_000,
        min_funding_lifetime_slots: 10_000_000,
        max_active_positions_per_side: MAX_ACCOUNTS as u64,
        max_price_move_bps_per_slot: 4,
    }
}

// Materialize a user slot via the test-visible back-door.
// Pattern identical to test_confirm_ac1_account_gc_state_leak.rs lines 60-67
// and test_confirm_v4_vault_cap_respect.rs lines 67-74.
fn add_user_test(engine: &mut RiskEngine, _fee_payment: u128) -> Result<u16> {
    let idx = engine.free_head;
    if idx == u16::MAX || (idx as usize) >= MAX_ACCOUNTS {
        return Err(RiskError::Overflow);
    }
    engine.materialize_at(idx, engine.current_slot)?;
    Ok(idx)
}

#[test]
fn test_confirm_s5_market_mode_transitions() {
    // -------------------------------------------------------------------------
    // Phase 0: Construct engine in Live mode.
    //
    // RiskEngine::new(params) delegates to new_with_market(params, 0, 1)
    // (percolator.rs:1690-1692) which sets market_mode = MarketMode::Live
    // (percolator.rs:1715).
    // -------------------------------------------------------------------------
    let mut engine = Box::new(RiskEngine::new(zero_fee_params()));

    // Engine must start in Live mode.
    assert_eq!(
        engine.market_mode,
        MarketMode::Live,
        "freshly constructed engine must be in Live mode"
    );

    // -------------------------------------------------------------------------
    // Phase 1: Resolve the market (Live -> Resolved).
    //
    // resolve_market_not_atomic (percolator.rs:9518) with ResolveMode::Degenerate
    // requires live_oracle_price == last_oracle_price (percolator.rs:9552-9553)
    // and funding_rate_e9 == 0 (percolator.rs:9555-9557).
    //
    // The engine was initialised with init_oracle_price=1 (via new->new_with_market),
    // so last_oracle_price=1. We resolve at that same price.
    //
    // There are no open positions, so the side drain steps in lines 9647-9663
    // are no-ops and oi_eff_{long,short}_q == 0 satisfies the step-21 check
    // at percolator.rs:9668-9670.
    // -------------------------------------------------------------------------
    let init_price: u64 = 1; // matches last_oracle_price set in new_with_market
    let resolve_slot: u64 = 10;
    engine.current_slot = resolve_slot;
    engine.last_market_slot = resolve_slot;

    let resolve_result = engine.resolve_market_not_atomic(
        ResolveMode::Degenerate,
        init_price,   // resolved_price
        init_price,   // live_oracle_price == last_oracle_price (degenerate requirement)
        resolve_slot,
        0,            // funding_rate_e9 == 0 (degenerate requirement)
    );
    assert!(
        resolve_result.is_ok(),
        "resolve_market_not_atomic on a clean Live engine must succeed; got {:?}",
        resolve_result
    );

    // Engine must now be in Resolved mode (percolator.rs:9618).
    assert_eq!(
        engine.market_mode,
        MarketMode::Resolved,
        "engine must be in Resolved mode after resolve_market_not_atomic"
    );

    // Resolved price and slot must be recorded correctly.
    assert_eq!(
        engine.resolved_price, init_price,
        "resolved_price must equal the settlement price supplied"
    );
    assert_eq!(
        engine.resolved_slot, resolve_slot,
        "resolved_slot must equal the slot supplied to resolve_market_not_atomic"
    );

    // -------------------------------------------------------------------------
    // Phase 2: Attempt to call resolve_market_not_atomic again on an already-
    // Resolved engine.
    //
    // percolator.rs:9526-9528 is the guard:
    //   if self.market_mode != MarketMode::Live {
    //       return Err(RiskError::Unauthorized);
    //   }
    //
    // The transition is one-way: Resolved -> Resolved is blocked. The engine
    // cannot be re-resolved without re-initialisation.
    // -------------------------------------------------------------------------
    let second_resolve = engine.resolve_market_not_atomic(
        ResolveMode::Degenerate,
        init_price,
        init_price,
        resolve_slot + 1,
        0,
    );
    assert!(
        second_resolve.is_err(),
        "resolve_market_not_atomic on an already-Resolved engine must be rejected"
    );
    assert_eq!(
        second_resolve.unwrap_err(),
        RiskError::Unauthorized,
        "the error must be Unauthorized (percolator.rs:9527), not a different variant"
    );

    // market_mode must still be Resolved -- the rejection must not have flipped it.
    assert_eq!(
        engine.market_mode,
        MarketMode::Resolved,
        "market_mode must remain Resolved after a rejected re-resolution attempt"
    );

    // -------------------------------------------------------------------------
    // Phase 3: Attempt deposit_not_atomic on the Resolved engine.
    //
    // deposit_not_atomic (percolator.rs:7322-7324) also checks:
    //   if self.market_mode != MarketMode::Live {
    //       return Err(RiskError::Unauthorized);
    //   }
    //
    // We first need a user slot. Since we are in Resolved mode, deposit will
    // be rejected before it materialises the account, so we call
    // add_user_test (materialize_at) directly to obtain a slot index.
    // Then we pass it to deposit_not_atomic to exercise the mode guard.
    // -------------------------------------------------------------------------
    let alice = add_user_test(&mut engine, 0)
        .expect("materialize_at must work regardless of market_mode");

    let deposit_result = engine.deposit_not_atomic(alice, 1_000_000, resolve_slot + 1);
    assert!(
        deposit_result.is_err(),
        "deposit_not_atomic on a Resolved engine must be rejected"
    );
    assert_eq!(
        deposit_result.unwrap_err(),
        RiskError::Unauthorized,
        "deposit rejection must be Unauthorized (percolator.rs:7323), not a different variant"
    );

    // market_mode must STILL be Resolved -- deposit rejection must not flip it.
    assert_eq!(
        engine.market_mode,
        MarketMode::Resolved,
        "market_mode must remain Resolved after a rejected deposit attempt"
    );

    // -------------------------------------------------------------------------
    // Phase 4: Confirm there is no function that sets market_mode back to Live
    // after init_in_place.
    //
    // The only assignment `self.market_mode = MarketMode::Live` in the entire
    // codebase is inside init_in_place (percolator.rs:1840), which is the full
    // re-initialisation constructor. It is NOT reachable from any transition
    // function that accepts a Resolved engine and returns it to Live.
    //
    // We confirm the engine field is still Resolved at the end of all tests,
    // meaning no code path silently reverted the mode.
    // -------------------------------------------------------------------------
    assert_eq!(
        engine.market_mode,
        MarketMode::Resolved,
        "final check: market_mode must be Resolved throughout -- no reverse path exists"
    );

    // is_resolved() is a test_visible! helper (percolator.rs:10507-10511).
    // It provides a second, independent read of the same field.
    assert!(
        engine.is_resolved(),
        "is_resolved() must return true at end of test"
    );

    // resolved_context() returns (resolved_price, resolved_slot)
    // (percolator.rs:10514-10518). Verify it is consistent with what we set.
    let (ctx_price, ctx_slot) = engine.resolved_context();
    assert_eq!(
        ctx_price, init_price,
        "resolved_context price must match the settlement price"
    );
    assert_eq!(
        ctx_slot, resolve_slot,
        "resolved_context slot must match the resolution slot"
    );
}
