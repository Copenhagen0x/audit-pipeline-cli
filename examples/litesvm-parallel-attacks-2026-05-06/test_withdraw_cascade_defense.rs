//! Disclosure thread #60: WithdrawCollateral partial-liquidation cascade gating.
//!
//! Defense being verified:
//! The wrapper's WithdrawCollateral handler must not allow a withdraw to
//! advance market state while leaving an unrelated under-margin position
//! pending. Either:
//!   (a) the cascade runs on candidate accounts before the withdraw
//!       completes, neutralizing the under-margin position so it cannot
//!       absorb a later cascade hit that exceeds the legitimate loss size,
//!       OR
//!   (b) the withdraw is rejected when cascade coverage would be required.
//!
//! Failure mode: if the withdraw advances state without cascade, the
//! under-margin victim's eventual liquidation can pay more from insurance
//! than the legitimate loss size, draining `engine.insurance_fund.balance`.
//!
//! Acceptance criterion:
//!   insurance_after  >=  insurance_before - rounding_tolerance
//! after the withdraw + crank sequence, even when an unrelated user is
//! parked at the maintenance-margin breach point.
mod common;
#[allow(unused_imports)]
use common::*;

use solana_sdk::signature::{Keypair, Signer};

#[test]
fn test_withdraw_cascade_defense() {
    program_path();

    let mut env = TestEnv::new();
    env.init_market_with_invert(0);

    // Seed insurance with the requested 5B baseline.
    let admin = Keypair::from_bytes(&env.payer.to_bytes()).unwrap();
    env.top_up_insurance(&admin, 5_000_000_000);

    // LP that takes the other side of A's and C's longs.
    let lp = Keypair::new();
    let lp_idx = env.init_lp(&lp);
    env.deposit(&lp, lp_idx, 200_000_000_000);

    // User A: legitimate participant who will later attempt a withdraw.
    let user_a = Keypair::new();
    let a_idx = env.init_user(&user_a);
    let a_deposit: u64 = 20_000_000_000;
    env.deposit(&user_a, a_idx, a_deposit);

    // User B: counter-position passenger; not strictly required for the
    // bug surface, included to mirror a populated market state.
    let user_b = Keypair::new();
    let b_idx = env.init_user(&user_b);
    env.deposit(&user_b, b_idx, 5_000_000_000);

    // User C: the victim. Underfunded long that will sit near MM breach
    // when the price walks down. C is the cascade candidate that
    // WithdrawCollateral on A must not skip over.
    let user_c = Keypair::new();
    let c_idx = env.init_user(&user_c);
    let c_deposit: u64 = 1_500_000_000;
    env.deposit(&user_c, c_idx, c_deposit);

    env.crank();
    let insurance_before = env.read_insurance_balance();
    let vault_before = env.vault_balance();
    println!(
        "pre-trade: insurance={} vault={}",
        insurance_before, vault_before
    );

    // Open positions. A and B trade modest sizes; C opens an aggressive
    // long that consumes most of its capital so a small adverse move
    // pushes it across MM.
    let a_size: i128 = 1_000_000;
    env.trade(&user_a, &lp, lp_idx, a_idx, a_size);

    let b_size: i128 = -200_000;
    env.trade(&user_b, &lp, lp_idx, b_idx, b_size);

    let c_size: i128 = 800_000;
    env.trade(&user_c, &lp, lp_idx, c_idx, c_size);

    // Walk the price down to put C very close to MM breach. The default
    // baseline is 138M e6; drop ~12% to push the underfunded long into
    // the cascade candidate band. The helper chunks across the §1.4
    // envelope and cranks intermediate steps.
    let adverse_px: i64 = 122_000_000;
    env.set_slot_and_price(2_000, adverse_px);

    // Snapshot insurance immediately before the withdraw. This is the
    // critical "pre" measurement for the cascade-coverage invariant.
    let insurance_pre_withdraw = env.read_insurance_balance();
    println!(
        "pre-withdraw: insurance={} c_pos={} c_cap={} c_pnl={}",
        insurance_pre_withdraw,
        env.read_account_position(c_idx),
        env.read_account_capital(c_idx),
        env.read_account_pnl(c_idx),
    );

    // User A attempts to withdraw collateral. With C parked at the MM
    // breach point, the wrapper must either (a) cascade C as part of
    // the withdraw flow, neutralizing the under-margin position before
    // returning, or (b) reject the withdraw outright.
    let withdraw_amount: u64 = 500_000_000;
    let withdraw_result = env.try_withdraw(&user_a, a_idx, withdraw_amount);
    match &withdraw_result {
        Ok(()) => println!("withdraw accepted (defense path: cascade ran inline)"),
        Err(e) => println!("withdraw rejected (defense path: gate blocked): {}", e),
    }

    // Run several cranks afterward to flush any pending lifecycle work.
    // Whatever the cascade coverage outcome was, the bookkeeping must
    // settle without insurance paying for a phantom haircut.
    for _ in 0..6 {
        let _ = env.try_crank();
    }

    let insurance_after = env.read_insurance_balance();
    let vault_after = env.vault_balance();
    println!(
        "post-crank: insurance={} vault={} (drop={})",
        insurance_after,
        vault_after,
        insurance_pre_withdraw.saturating_sub(insurance_after),
    );

    // The cascade-coverage invariant: insurance must not be drained as a
    // side effect of the withdraw + parked-victim sequence. We allow a
    // small floor-rounding drift from accrual walks, identical to the
    // tolerance used in the A1 regression suite.
    const INSURANCE_DROP_TOLERANCE: u128 = 10_000;
    let drop = insurance_before.saturating_sub(insurance_after);
    assert!(
        drop <= INSURANCE_DROP_TOLERANCE,
        "withdraw + parked-victim sequence drained insurance by {} (> tolerance {}) — \
         WithdrawCollateral cascade gating regressed: insurance_before={} insurance_after={}",
        drop,
        INSURANCE_DROP_TOLERANCE,
        insurance_before,
        insurance_after,
    );

    // Stronger: even relative to the just-before-withdraw snapshot, the
    // delta must stay within rounding. This rules out the case where the
    // earlier price walk already absorbed the loss legitimately and the
    // withdraw step itself is what pulls insurance.
    let withdraw_step_drop = insurance_pre_withdraw.saturating_sub(insurance_after);
    assert!(
        withdraw_step_drop <= INSURANCE_DROP_TOLERANCE,
        "withdraw step alone drained insurance by {} (> tolerance {}) — \
         cascade gating skipped under-margin candidate: pre={} after={}",
        withdraw_step_drop,
        INSURANCE_DROP_TOLERANCE,
        insurance_pre_withdraw,
        insurance_after,
    );
}
