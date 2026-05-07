#![cfg(feature = "test")]

// test_confirm_p8_self_trade_cash_flow
//
// Hypothesis P8-self-trade-cash-flow: A self-trade (same authority on both
// sides of a fill) is cash-flow neutral up to fees + IM transitions.
// No fund extraction occurs via self-trades.
//
// Source evidence (target/engine/src/percolator.rs):
//
// 1. percolator.rs:7623-7625: The engine EXPLICITLY BLOCKS a == b (same slot
//    index) self-trades with Err(RiskError::Overflow). So a literal single-
//    account self-trade is impossible.
//
// 2. percolator.rs:563-568 (Account.owner comment): "Authorization is a
//    wrapper responsibility; the engine never reads `owner` for any
//    spec-normative decision." Two accounts with identical `owner` bytes
//    are treated identically to two accounts with different owners.
//
// 3. percolator.rs:7875-7895: Fee charging via charge_fee_to_insurance.
//    BOTH sides pay `fee = ceil(trade_notional * trading_fee_bps / 10_000)`.
//    The fee goes to insurance_fund.balance. There are NO negative fees.
//    So a self-trade (two accounts, same owner) costs the authority 2 * fee.
//
// 4. percolator.rs:5971-5980: check_conservation() asserts
//    vault >= c_tot + insurance_fund.balance at all times.
//
// 5. percolator.rs:7928: assert_public_postconditions() (which calls
//    check_conservation) runs after every execute_trade_not_atomic call.
//
// Test strategy:
//   Phase 0: Engine with non-zero trading_fee_bps (10 bps).
//   Phase 1: Two accounts with identical owner bytes; deposit equal capital.
//   Phase 2: Snapshot baseline state.
//   Phase 3: Execute "self-trade" alice->bob.
//   Phase 4: Snapshot post state.
//   Phase 5: Assert (a) trade succeeded; (b) vault unchanged; (c) combined
//   capital decreased by fees; (d) insurance grew by exactly the fees;
//   (e) conservation held; (f) positions established + OI balanced.

use percolator::i128::U128;
use percolator::*;

fn fee_params() -> RiskParams {
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

fn add_user_test(engine: &mut RiskEngine) -> Result<u16> {
    let idx = engine.free_head;
    if idx == u16::MAX || (idx as usize) >= MAX_ACCOUNTS {
        return Err(RiskError::Overflow);
    }
    engine.materialize_at(idx, engine.current_slot)?;
    Ok(idx)
}

fn pos_q(qty: i64) -> i128 {
    let abs_val = (qty as i128).unsigned_abs();
    let scaled = abs_val.checked_mul(POS_SCALE).unwrap();
    if qty < 0 {
        -(scaled as i128)
    } else {
        scaled as i128
    }
}

#[test]
fn test_confirm_p8_self_trade_cash_flow() {
    // Phase 0: Engine setup with non-zero trading fee
    let mut engine = Box::new(RiskEngine::new(fee_params()));

    // Seed insurance fund so vault > 0 from the start
    engine
        .top_up_insurance_fund(500_000, 0)
        .expect("insurance seed must succeed");

    // Phase 1: Two accounts with the SAME owner bytes
    let alice = add_user_test(&mut engine).expect("alice materialize");
    let bob = add_user_test(&mut engine).expect("bob materialize");

    let shared_owner: [u8; 32] = [0xABu8; 32];
    engine.accounts[alice as usize].owner = shared_owner;
    engine.accounts[bob as usize].owner = shared_owner;

    assert_ne!(
        alice, bob,
        "alice and bob must be distinct slot indices; a==b self-trades blocked by engine"
    );
    assert_eq!(
        engine.accounts[alice as usize].owner,
        engine.accounts[bob as usize].owner,
        "both accounts must share the same owner bytes to model a self-trade"
    );

    let oracle_price: u64 = 1_000;
    let slot0: u64 = 0;

    engine
        .deposit_not_atomic(alice, 1_000_000, slot0)
        .expect("alice deposit");
    engine
        .deposit_not_atomic(bob, 1_000_000, slot0)
        .expect("bob deposit");

    // Advance market to slot0 with oracle_price
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

    // Phase 2: Baseline state
    let vault_before = engine.vault.get();
    let insurance_before = engine.insurance_fund.balance.get();
    let cap_alice_before = engine.accounts[alice as usize].capital.get();
    let cap_bob_before = engine.accounts[bob as usize].capital.get();
    let combined_capital_before = cap_alice_before
        .checked_add(cap_bob_before)
        .expect("combined capital before must not overflow");
    let c_tot_before = engine.c_tot.get();

    assert!(
        engine.check_conservation(),
        "conservation must hold before self-trade; vault={} c_tot={} insurance={}",
        vault_before, c_tot_before, insurance_before
    );

    // Phase 3: Execute self-trade — alice (long) vs bob (short), same owner
    let trade_size = pos_q(10);
    let trade_result = engine.execute_trade_not_atomic(
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
    );

    assert!(
        trade_result.is_ok(),
        "same-owner self-trade between different slot indices must be accepted; got {:?}",
        trade_result
    );

    // Phase 4: Post-trade state
    let vault_after = engine.vault.get();
    let insurance_after = engine.insurance_fund.balance.get();
    let cap_alice_after = engine.accounts[alice as usize].capital.get();
    let cap_bob_after = engine.accounts[bob as usize].capital.get();
    let combined_capital_after = cap_alice_after
        .checked_add(cap_bob_after)
        .expect("combined capital after must not overflow");
    let c_tot_after = engine.c_tot.get();

    // Phase 5: Invariant assertions

    // (a) Conservation holds after
    assert!(
        engine.check_conservation(),
        "conservation must hold after self-trade; vault={} c_tot={} insurance={}",
        vault_after, c_tot_after, insurance_after
    );

    // (b) Vault unchanged — no external funds entered or left
    assert_eq!(
        vault_after, vault_before,
        "vault must be unchanged by a self-trade; before={} after={}",
        vault_before, vault_after
    );

    // (c) Combined authority capital can ONLY decrease (this is the core claim)
    assert!(
        combined_capital_after <= combined_capital_before,
        "VIOLATION: combined authority capital INCREASED after self-trade (extraction!); \
         before={} after={} delta=+{}",
        combined_capital_before,
        combined_capital_after,
        combined_capital_after.saturating_sub(combined_capital_before)
    );

    // (d) Insurance grew by exactly the capital lost (vault is fixed)
    let insurance_growth = insurance_after
        .checked_sub(insurance_before)
        .expect("insurance fund must not decrease after fee collection");

    let capital_loss = combined_capital_before
        .checked_sub(combined_capital_after)
        .expect("capital can only decrease in a self-trade");

    let c_tot_loss = c_tot_before
        .checked_sub(c_tot_after)
        .expect("c_tot can only decrease after fees");

    assert_eq!(
        c_tot_loss, capital_loss,
        "c_tot decrease must equal sum of individual capital decreases; \
         c_tot_loss={} capital_loss={}",
        c_tot_loss, capital_loss
    );

    assert_eq!(
        insurance_growth, capital_loss,
        "insurance growth ({}) must equal combined capital loss ({}) — \
         vault is fixed, fees flow directly to insurance",
        insurance_growth, capital_loss
    );

    assert!(
        insurance_growth > 0,
        "non-zero fee parameters must produce non-zero fee collection; insurance_growth={}",
        insurance_growth
    );

    // (e) Positions established (trade was not a no-op)
    let alice_pos = engine.effective_pos_q(alice as usize);
    let bob_pos = engine.effective_pos_q(bob as usize);
    assert!(alice_pos > 0, "alice must hold a long position (got {})", alice_pos);
    assert!(bob_pos < 0, "bob must hold a short position (got {})", bob_pos);

    // (f) OI balanced
    assert_eq!(
        engine.oi_eff_long_q, engine.oi_eff_short_q,
        "OI must remain balanced after self-trade; long={} short={}",
        engine.oi_eff_long_q, engine.oi_eff_short_q
    );
}
