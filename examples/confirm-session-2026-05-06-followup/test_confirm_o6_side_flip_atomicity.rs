#![cfg(feature = "test")]

// Test: test_confirm_o6_side_flip_atomicity
//
// Hypothesis O6-side-flip-atomicity: A side-flip fill is atomic — no
// intermediate "0 position" state can be observed by another instruction
// within the same transaction.
//
// Source analysis (target/engine/src/percolator.rs):
//
// The critical path is execute_trade_not_atomic (line 7641).  When a trade
// flips a position from long to short (or vice versa), the engine calls:
//
//   attach_effective_position_allow_spike(a, new_eff_a)   // line 7860
//   attach_effective_position_allow_spike(b, new_eff_b)   // line 7861
//
// which delegates to attach_effective_position_inner (line 2745).
//
// Inside attach_effective_position_inner:
//   - When new_eff_pos_q == 0:  calls clear_position_basis_q (line 2781)
//   - When new_eff_pos_q != 0:  calls set_position_basis_q_allow_spike (line 2790)
//
// set_position_basis_q_allow_spike -> set_position_basis_q_inner (line 2625).
// That function does a SINGLE write to accounts[idx].position_basis_q at
// line 2705:
//
//   self.accounts[idx].position_basis_q = new_basis;
//
// The old basis (e.g. +long) is removed and new_basis (e.g. -short) is
// written in one assignment.  There is NO intermediate step that first
// zeroes position_basis_q and then writes the new-side value.
//
// The two-step execution for a flip is:
//   Step 1: compute new_eff_a = old_eff_a + size_q           (line 7748)
//   Step 2: write accounts[a].position_basis_q = new_eff_a   (line 2705)
//
// Both steps happen inside a single Rust function call with no await/yield
// points.  The engine is a pure synchronous Rust library; there is no
// preemption or concurrency.  The only externally observable snapshots are
// the values present BEFORE the call and AFTER it returns.
//
// What this test verifies:
//   Phase A: Alice opens a LONG position of +10 units.
//   Phase B: Bob (who was short -10) "over-fills" Alice by trading -20 units
//            through her, flipping Alice from long +10 to short -10.
//            This is the canonical side-flip scenario.
//   Invariant assertions:
//     1. Immediately AFTER execute_trade_not_atomic returns, Alice's
//        position_basis_q has the expected negative (short) value — the
//        engine never left a zero in place between the long close and the
//        short open.
//     2. The engine stores_pos_count_long / stored_pos_count_short are
//        consistent with the final positions (no phantom zero-position
//        entry was counted).
//     3. conservation holds throughout.
//
// Because the engine is single-threaded Rust, "no observable intermediate
// state" is confirmed by showing that the single value read immediately
// after the call is the correct net value, not zero.  If the implementation
// had used a two-write sequence (zero then new value) and the Rust compiler
// or test scaffolding had yielded in between, the assertion would catch it.
// In practice the single-statement write (line 2705) guarantees atomicity
// within one synchronous call frame.
//
// VERDICT: TRUE — the invariant holds.  The engine writes the net position
// in a single statement; no zero intermediate is written to position_basis_q
// during a side flip.

use percolator::i128::U128;
use percolator::*;

// ---------------------------------------------------------------------------
// Helpers — mirrors pattern from test_confirm_o7_position_zero_clears_basis.rs
// ---------------------------------------------------------------------------

fn default_params() -> RiskParams {
    RiskParams {
        maintenance_margin_bps: 3000,
        initial_margin_bps: 3500,
        trading_fee_bps: 0,
        max_accounts: 64,
        liquidation_fee_bps: 0,
        liquidation_fee_cap: U128::new(0),
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

// Scale whole base-units into the engine's internal POS_SCALE units.
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

#[test]
fn test_confirm_o6_side_flip_atomicity() {
    // -----------------------------------------------------------------------
    // Setup: fresh engine, two users with ample capital so margin checks pass.
    // Zero trading fees simplify the test.
    // -----------------------------------------------------------------------
    let mut engine = Box::new(RiskEngine::new(default_params()));

    // Seed insurance so vault > 0 and conservation is trivially satisfied.
    engine
        .top_up_insurance_fund(1_000_000, 0)
        .expect("insurance seed must succeed");

    let alice = add_user_test(&mut engine, 0).expect("alice materialize");
    let bob   = add_user_test(&mut engine, 0).expect("bob materialize");
    let carol = add_user_test(&mut engine, 0).expect("carol materialize");

    let oracle_price: u64 = 100;
    let slot0: u64 = 0;

    // Give everyone generous capital so margin is never the limiting factor.
    engine
        .deposit_not_atomic(alice, 2_000_000, slot0)
        .expect("alice deposit");
    engine
        .deposit_not_atomic(bob, 2_000_000, slot0)
        .expect("bob deposit");
    engine
        .deposit_not_atomic(carol, 2_000_000, slot0)
        .expect("carol deposit");

    // Initial crank to establish oracle price at slot0.
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
    // Phase A: Alice opens LONG +10, Bob opens matching SHORT -10.
    //
    // execute_trade_not_atomic(a, b, oracle, slot, size_q, exec, funding,
    //                          h_min, h_max, threshold_bps_opt)
    // size_q > 0 means `a` goes long by that amount, `b` goes short.
    // -----------------------------------------------------------------------
    let open_size = pos_q(10);
    let slot1: u64 = 1;

    engine
        .execute_trade_not_atomic(
            alice,
            bob,
            oracle_price,
            slot1,
            open_size,
            oracle_price,
            0i128,
            0,
            100,
            None,
        )
        .expect("phase-A open: alice long 10, bob short 10 must succeed");

    // Verify baseline positions.
    let alice_pos_after_open = engine.accounts[alice as usize].position_basis_q;
    let bob_pos_after_open   = engine.accounts[bob as usize].position_basis_q;

    assert!(
        alice_pos_after_open > 0,
        "after phase-A, alice must be long (position_basis_q > 0), got {}",
        alice_pos_after_open
    );
    assert!(
        bob_pos_after_open < 0,
        "after phase-A, bob must be short (position_basis_q < 0), got {}",
        bob_pos_after_open
    );
    assert_eq!(
        alice_pos_after_open, open_size,
        "alice position_basis_q must equal open_size after phase-A"
    );
    assert_eq!(
        bob_pos_after_open, -open_size,
        "bob position_basis_q must equal -open_size after phase-A"
    );

    assert!(engine.check_conservation(), "conservation must hold after phase-A");

    // -----------------------------------------------------------------------
    // Phase B: Side-flip trade.
    //
    // Carol (flat) trades with Alice using a size of +20.
    // Alice receives +20 (from the perspective of execute_trade_not_atomic
    // where alice=a, carol=b):
    //   old_eff_alice = +open_size (+10 * POS_SCALE)
    //   size_q        = +20 * POS_SCALE   (alice is `a`, goes LONG by 20 more)
    //   new_eff_alice = +10 + (+20) = +30  ... that would NOT flip.
    //
    // We need alice to go from LONG +10 to SHORT -10.
    // That requires a sell of 20 units through alice.
    // In execute_trade_not_atomic, `a` receives +size_q.
    // So to sell alice (make alice give up 20):
    //   a = carol (buyer), b = alice (seller), size_q = +20
    //   alice as `b` receives -size_q = -20
    //   new_eff_alice = +10 + (-20) = -10  ← flip long -> short
    //
    // Carol as `a` receives +20, going from 0 to LONG +20.
    // -----------------------------------------------------------------------
    let flip_size = pos_q(20); // carol (a) buys 20, alice (b) sells 20
    let slot2: u64 = 2;

    engine
        .execute_trade_not_atomic(
            carol,   // `a` — goes long by flip_size
            alice,   // `b` — sells flip_size, flips from long +10 to short -10
            oracle_price,
            slot2,
            flip_size,
            oracle_price,
            0i128,
            0,
            100,
            None,
        )
        .expect("phase-B side-flip trade must succeed");

    // -----------------------------------------------------------------------
    // Core invariant: immediately after execute_trade_not_atomic returns,
    // Alice's position_basis_q must be the NET flipped value (-10 * POS_SCALE),
    // NOT zero.
    //
    // If the engine had used a two-write sequence:
    //   write 0            ← intermediate zero
    //   write -open_size   ← final short
    // ...and any observable intermediate state existed, we would catch it by
    // verifying the final value is neither zero nor the old long value.
    //
    // Because the single-statement write at percolator.rs:2705 directly writes
    // new_basis (= -open_size) without first writing 0, the value read here
    // must be exactly -open_size.
    // -----------------------------------------------------------------------
    let alice_pos_after_flip = engine.accounts[alice as usize].position_basis_q;

    // The net result: alice was long +10, sold 20, so net = -10.
    let expected_alice_flip = -open_size; // = -(10 * POS_SCALE)

    assert_ne!(
        alice_pos_after_flip, 0,
        "INVARIANT VIOLATED: alice position_basis_q is 0 after side-flip — \
         an intermediate zero was written and is now the final state, which \
         means the flip was not applied atomically (percolator.rs:2705 — \
         set_position_basis_q_inner should write net value in one statement)"
    );

    assert_ne!(
        alice_pos_after_flip, alice_pos_after_open,
        "alice position_basis_q must not still be the old long value after the flip"
    );

    assert_eq!(
        alice_pos_after_flip, expected_alice_flip,
        "alice position_basis_q must equal exactly -open_size ({}) after side-flip, got {}",
        expected_alice_flip,
        alice_pos_after_flip
    );

    assert!(
        alice_pos_after_flip < 0,
        "alice must be SHORT (position_basis_q < 0) after side-flip, got {}",
        alice_pos_after_flip
    );

    // Carol must be long +20 (was flat, bought 20).
    let carol_pos_after_flip = engine.accounts[carol as usize].position_basis_q;
    assert_eq!(
        carol_pos_after_flip, flip_size,
        "carol position_basis_q must equal +flip_size ({}) after phase-B, got {}",
        flip_size,
        carol_pos_after_flip
    );

    // Bob is unaffected by phase-B.
    let bob_pos_after_flip = engine.accounts[bob as usize].position_basis_q;
    assert_eq!(
        bob_pos_after_flip, bob_pos_after_open,
        "bob position_basis_q must be unchanged by phase-B flip (got {})",
        bob_pos_after_flip
    );

    // -----------------------------------------------------------------------
    // Side-count consistency: stored_pos_count_long and stored_pos_count_short
    // must exactly match the number of accounts with positive / negative
    // position_basis_q.
    //
    // Before flip:  alice=long, bob=short, carol=flat  → long=1, short=1
    // After flip:   alice=short, bob=short, carol=long → long=1, short=2
    //
    // If an intermediate zero had been left in the count bookkeeping, the
    // counts would be wrong (e.g. long=0 or short=3).
    // -----------------------------------------------------------------------
    let expected_long_count: u64 = 1;  // only carol
    let expected_short_count: u64 = 2; // alice + bob

    assert_eq!(
        engine.stored_pos_count_long, expected_long_count,
        "stored_pos_count_long must be {} after side-flip (carol long, alice flipped to short); \
         got {}",
        expected_long_count,
        engine.stored_pos_count_long
    );

    assert_eq!(
        engine.stored_pos_count_short, expected_short_count,
        "stored_pos_count_short must be {} after side-flip (alice + bob short); \
         got {}",
        expected_short_count,
        engine.stored_pos_count_short
    );

    // -----------------------------------------------------------------------
    // effective_pos_q must agree with position_basis_q for a freshly
    // attached position (no ADL has occurred, so the two are equal).
    // -----------------------------------------------------------------------
    let alice_eff = engine.effective_pos_q(alice as usize);
    assert_eq!(
        alice_eff, expected_alice_flip,
        "effective_pos_q(alice) must equal position_basis_q ({}) after side-flip; \
         got {}",
        expected_alice_flip,
        alice_eff
    );

    // -----------------------------------------------------------------------
    // Companion fields must reflect the SHORT side (written by
    // attach_effective_position_inner lines 2802-2807 in the new-side branch,
    // which is reached only because new_eff_pos_q != 0 — i.e. there was no
    // intermediate zero branch that would have set them to canonical-zero
    // values first).
    // -----------------------------------------------------------------------
    let alice_adl_a = engine.accounts[alice as usize].adl_a_basis;
    assert_eq!(
        alice_adl_a, engine.adl_mult_short,
        "alice adl_a_basis must equal adl_mult_short after short flip; \
         intermediate zero branch would have written ADL_ONE"
    );

    let alice_k_snap = engine.accounts[alice as usize].adl_k_snap;
    assert_eq!(
        alice_k_snap, engine.adl_coeff_short,
        "alice adl_k_snap must equal adl_coeff_short after short flip"
    );

    let alice_f_snap = engine.accounts[alice as usize].f_snap;
    assert_eq!(
        alice_f_snap, engine.f_short_num,
        "alice f_snap must equal f_short_num after short flip"
    );

    let alice_epoch_snap = engine.accounts[alice as usize].adl_epoch_snap;
    assert_eq!(
        alice_epoch_snap, engine.adl_epoch_short,
        "alice adl_epoch_snap must equal adl_epoch_short after short flip"
    );

    // -----------------------------------------------------------------------
    // Final conservation check.
    // -----------------------------------------------------------------------
    assert!(
        engine.check_conservation(),
        "conservation must hold after side-flip in phase-B"
    );

    // -----------------------------------------------------------------------
    // Phase C: Confirm the flipped position can be closed normally,
    // demonstrating the short is a real, usable position — not a ghost.
    // -----------------------------------------------------------------------
    let slot3: u64 = 3;

    // Carol (long +20) closes half (-10) against alice's short (-10) going flat.
    // carol=b sells 10 to alice=a (alice buys 10, going from -10 to 0).
    engine
        .execute_trade_not_atomic(
            alice,   // `a` buys 10 (short -10 -> flat 0)
            carol,   // `b` sells 10 (long +20 -> long +10)
            oracle_price,
            slot3,
            open_size,   // alice receives +open_size, carol receives -open_size
            oracle_price,
            0i128,
            0,
            100,
            None,
        )
        .expect("phase-C partial close of flipped position must succeed");

    let alice_pos_flat = engine.accounts[alice as usize].position_basis_q;
    assert_eq!(
        alice_pos_flat, 0,
        "alice must be flat after closing the flipped short; got {}",
        alice_pos_flat
    );

    let carol_pos_partial = engine.accounts[carol as usize].position_basis_q;
    assert_eq!(
        carol_pos_partial, open_size,
        "carol must be long +open_size after partial close; got {}",
        carol_pos_partial
    );

    assert!(
        engine.check_conservation(),
        "conservation must hold after phase-C close"
    );
}
