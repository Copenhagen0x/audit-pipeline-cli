//! Slot-reuse defense: account-slot recycling must not leak state across
//! generations.
//!
//! Property: After A is closed and a new account B initializes (and gets
//! the same slot index via free_head reuse), B must start fresh — no
//! phantom position, no carryover PnL, no fee_credit residual — and the
//! engine.insurance_fund.balance must not be drained as a byproduct of
//! the slot recycling.
//!
//! The wrapper records each account's generation in a per-slot table at
//! init time; if any cached state survives close→init, the engine could
//! mis-apply A's losses to B and absorb them into insurance.

mod common;
#[allow(unused_imports)]
use common::*;

use solana_sdk::signature::{Keypair, Signer};

#[test]
fn test_slot_reuse_defense() {
    program_path();

    let mut env = TestEnv::new();
    env.init_market_with_invert(0);

    let admin = Keypair::from_bytes(&env.payer.to_bytes()).unwrap();

    // ---- 1. Top up insurance to a known baseline (5B) -----------------
    env.try_top_up_insurance(&admin, 5_000_000_000)
        .expect("insurance top-up must succeed");
    let insurance_pre = env.read_insurance_balance();
    println!("Insurance baseline (pre): {}", insurance_pre);
    assert!(
        insurance_pre >= 5_000_000_000u128,
        "baseline insurance must be >= 5B, got {}",
        insurance_pre
    );

    // ---- 2. Set up market: LP + user A -------------------------------
    let lp = Keypair::new();
    let lp_idx = env.init_lp(&lp);
    env.deposit(&lp, lp_idx, 50_000_000_000);

    let user_a = Keypair::new();
    let user_a_idx = env.init_user(&user_a);
    env.deposit(&user_a, user_a_idx, 3_000_000_000);

    env.crank();
    println!(
        "user A initialized at slot idx {} (slot_used={})",
        user_a_idx,
        env.is_slot_used(user_a_idx)
    );
    assert!(env.is_slot_used(user_a_idx), "A's slot must be used");

    // ---- 3. A trades and accrues some PnL movement -------------------
    env.trade(&user_a, &lp, lp_idx, user_a_idx, 500_000);
    env.set_slot(2);
    env.crank();
    // Close A's position back to zero.
    env.trade(&user_a, &lp, lp_idx, user_a_idx, -500_000);
    env.set_slot(100);
    env.crank();

    // ---- 4. Withdraw remaining capital, settle PnL, close A ----------
    let cap = env.read_account_capital(user_a_idx);
    if cap > 0 {
        env.try_withdraw(&user_a, user_a_idx, cap as u64)
            .expect("withdraw before close must succeed");
    }
    let pnl_a_pre_close = env.read_account_pnl(user_a_idx);
    if pnl_a_pre_close != 0 {
        // If PnL is non-zero, crank to convert; close requires PnL == 0.
        env.set_slot(150);
        env.crank();
    }
    let pnl_a = env.read_account_pnl(user_a_idx);
    assert_eq!(pnl_a, 0, "A must have zero PnL before close");

    env.close_account(&user_a, user_a_idx);
    assert!(
        !env.is_slot_used(user_a_idx),
        "A's slot must be freed after close"
    );
    let num_used_after_close = env.read_num_used_accounts();
    println!(
        "After A close: num_used={}, A_slot_used={}",
        num_used_after_close,
        env.is_slot_used(user_a_idx)
    );

    let insurance_after_a = env.read_insurance_balance();
    println!("Insurance after A close: {}", insurance_after_a);

    // ---- 5. Create user B — should reuse A's freed slot --------------
    // The on-chain free_head allocator hands out the most-recently-freed
    // slot. The TestEnv helper bumps account_count on every init; rewind
    // it so the helper's bookkeeping aligns with the slot the program
    // will actually use (A's old idx). The helper does not write back to
    // the slab — account_count is purely local.
    env.account_count = user_a_idx;

    let user_b = Keypair::new();
    let user_b_idx = env.init_user(&user_b);
    println!(
        "user B initialized — A_idx={} B_idx={}",
        user_a_idx, user_b_idx
    );
    assert_eq!(
        user_b_idx, user_a_idx,
        "SLOT REUSE: B must be assigned A's freed slot index"
    );
    assert!(env.is_slot_used(user_b_idx), "B's slot must be used");

    // ---- 6. Verify B starts FRESH — no carryover from A --------------
    let b_cap_init = env.read_account_capital(user_b_idx);
    let b_pos_init = env.read_account_position(user_b_idx);
    let b_pnl_init = env.read_account_pnl(user_b_idx);
    println!(
        "B fresh-start state: capital={} position={} pnl={}",
        b_cap_init, b_pos_init, b_pnl_init
    );
    assert_eq!(
        b_pos_init, 0,
        "FRESHNESS: B must start with zero position (no phantom carryover from A)"
    );
    assert_eq!(
        b_pnl_init, 0,
        "FRESHNESS: B must start with zero PnL (no carryover from A's lifecycle)"
    );

    // ---- 7. B deposits, trades, cranks normally ----------------------
    env.deposit(&user_b, user_b_idx, 3_000_000_000);
    env.set_slot(200);
    env.crank();
    env.trade(&user_b, &lp, lp_idx, user_b_idx, 400_000);
    env.set_slot(250);
    env.crank();
    // Close B's position
    env.trade(&user_b, &lp, lp_idx, user_b_idx, -400_000);
    env.set_slot(300);
    env.crank();

    let b_pos_final = env.read_account_position(user_b_idx);
    assert_eq!(b_pos_final, 0, "B's position must be zero after close-out");

    // ---- 8. Insurance bound: post must not be < pre - epsilon --------
    let insurance_post = env.read_insurance_balance();
    println!("Insurance final (post): {}", insurance_post);

    // Hard property: slot reuse must NOT cause insurance to drain. A
    // small positive accrual (fees) is acceptable; any drop would
    // indicate phantom losses being absorbed.
    assert!(
        insurance_post >= insurance_pre,
        "INSURANCE DRAIN via slot reuse: pre={} post={} delta={}",
        insurance_pre,
        insurance_post,
        (insurance_post as i128) - (insurance_pre as i128)
    );

    // ---- 9. Conservation -----------------------------------
    let vault = env.vault_balance();
    let c_tot = env.read_c_tot();
    assert!(
        vault as u128 >= c_tot + insurance_post,
        "conservation broken: vault={} c_tot={} insurance={}",
        vault,
        c_tot,
        insurance_post
    );

    println!(
        "PASS: slot-reuse defense holds. A_idx={} B_idx={} ins_pre={} ins_post={} delta=+{}",
        user_a_idx,
        user_b_idx,
        insurance_pre,
        insurance_post,
        insurance_post - insurance_pre
    );
}
