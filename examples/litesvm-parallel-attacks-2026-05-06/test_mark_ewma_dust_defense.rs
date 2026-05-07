//! Mark-EWMA dust-defense regression — adjacent to disclosure #62 (funding-K-walk).
//!
//! PROPERTY UNDER TEST
//! -------------------
//! Many sub-`mark_min_fee`-weight TradeNoCpi calls cannot be used to advance
//! `config.mark_ewma_e6` far from oracle in a way that scales the funding-rate
//! magnitude up enough to drain `engine.insurance_fund.balance` via funding
//! transfers between long/short.
//!
//! ATTACK MODEL
//! ------------
//! 1. Attacker controls both an LP (small_trader, paired LP for self-trade) and
//!    a separate dust-self-trader.
//! 2. Attacker emits hundreds of tiny TradeNoCpi calls. Each individual trade
//!    is below `mark_min_fee` weight, so by design the EWMA should advance
//!    only at fractional weight per trade.
//! 3. Attacker periodically cranks to drive funding accruals. If the EWMA
//!    drift compounds, the funding rate magnitude swells, and any rounding /
//!    routing bug in the long↔short funding transfer surfaces as an
//!    insurance leak.
//! 4. Attacker has the option to walk the oracle slightly between batches so
//!    the trade exec price stays away from oracle without tripping the
//!    per-slot price-move cap.
//!
//! VICTIM BASELINE
//! ---------------
//! `large_passive` is a passive third-party LP/user holding a steady position
//! across the entire dust storm; it absorbs funding but never trades, so any
//! rounding-driven leak shows up cleanly against insurance.
//!
//! WIN CONDITION (per max_risk.md §8)
//! ----------------------------------
//!     insurance_fund.balance < insurance_fund.balance_at_start
//!
//! This test asserts the OPPOSITE — invariant holds across N dust trades.

mod common;
#[allow(unused_imports)]
use common::*;

use solana_sdk::signature::{Keypair, Signer};

#[test]
fn test_mark_ewma_dust_defense() {
    let mut env = TestEnv::new();
    env.init_market_with_invert(0);

    let admin = Keypair::from_bytes(&env.payer.to_bytes()).unwrap();

    // ── Seed insurance to a clearly visible balance ──────────────────────
    let insurance_seed: u64 = 5_000_000_000;
    env.top_up_insurance(&admin, insurance_seed);
    let insurance_pre = env.read_insurance_balance();
    println!("insurance_pre = {}", insurance_pre);

    // ── Actors ───────────────────────────────────────────────────────────
    // LP1: counterparty for the dust storm
    let lp1 = Keypair::new();
    let lp1_idx = env.init_lp(&lp1);
    env.deposit(&lp1, lp1_idx, 200_000_000_000);

    // Small_trader: emits hundreds of tiny matched trades against lp1
    let small_trader = Keypair::new();
    let small_idx = env.init_user(&small_trader);
    env.deposit(&small_trader, small_idx, 50_000_000_000);

    // Large_passive: opens one position and holds. Its capital is the
    // canary — funding-driven leakage routes through it on its way to
    // insurance under the suspected vector.
    let large_passive = Keypair::new();
    let lp2 = Keypair::new();
    let lp2_idx = env.init_lp(&lp2);
    env.deposit(&lp2, lp2_idx, 100_000_000_000);
    let lp_passive_idx = env.init_user(&large_passive);
    env.deposit(&large_passive, lp_passive_idx, 50_000_000_000);

    env.crank();

    // Open a steady passive position so funding has something to transfer
    // against during the dust storm.
    let passive_size: i128 = 5_000_000;
    env.trade(&large_passive, &lp2, lp2_idx, lp_passive_idx, passive_size);

    let insurance_after_setup = env.read_insurance_balance();

    // ── Dust storm: alternating tiny trades ─────────────────────────────
    //
    // Each trade routes through TradeNoCpi and touches the EWMA update
    // path. Sizes below the engine's hard minimum are rejected outright
    // (this is itself a defense layer); we use the smallest size the
    // engine consistently accepts, so the per-trade fee is at the
    // rounding floor — well below any realistic `mark_min_fee`
    // parameterization. EWMA advancement per trade is therefore at
    // fractional / clamped weight, exactly the scenario the property
    // worries about.
    //
    // Sign alternation matters: matched +/- pairs keep the small_trader's
    // nominal exposure near zero across the storm so we genuinely
    // probe the EWMA update path, not directional PnL accumulation.
    let dust_size: i128 = 10_000;
    let total_dust_trades = 600usize;
    let mut succeeded = 0usize;
    let mut rejected = 0usize;
    let crank_every = 30usize;
    let oracle_walk_every = 60usize;

    // Slowly walk the oracle between batches to keep mark drift alive
    // without violating the per-slot price-move cap
    // (TEST_MAX_PRICE_MOVE_BPS_PER_SLOT = 4 ⇒ 0.04 % per slot).
    let mut current_slot: u64 = 200;
    let mut current_px: i64 = 138_000_000;

    let mut first_err: Option<String> = None;
    for i in 0..total_dust_trades {
        let signed_size = if i & 1 == 0 { dust_size } else { -dust_size };
        // Each tx needs a unique blockhash; expire forces a fresh one.
        env.svm.expire_blockhash();
        match env.try_trade(&small_trader, &lp1, lp1_idx, small_idx, signed_size) {
            Ok(_) => succeeded += 1,
            Err(e) => {
                rejected += 1;
                if first_err.is_none() {
                    first_err = Some(e);
                }
            }
        }

        // Periodic crank — drive funding accruals so any EWMA-driven
        // funding-rate leakage surfaces.
        if (i + 1) % crank_every == 0 {
            let _ = env.try_crank();
        }

        // Periodic oracle walk: stay within the per-slot cap. We crawl
        // a few bps every so often so mark and oracle don't sit identically.
        if (i + 1) % oracle_walk_every == 0 {
            current_slot += 100;
            // ±5 bps drift, alternating sign
            if (i / oracle_walk_every) & 1 == 0 {
                current_px = current_px.saturating_add(40_000); // +~3 bps
            } else {
                current_px = current_px.saturating_sub(40_000);
            }
            env.set_slot_and_price(current_slot, current_px);
            for _ in 0..3 {
                let _ = env.try_crank();
            }
        }
    }

    // Final flush — give the engine a chance to settle pending funding.
    env.set_slot_and_price(current_slot + 200, current_px);
    for _ in 0..10 {
        let _ = env.try_crank();
    }

    // ── Snapshot ─────────────────────────────────────────────────────────
    let insurance_post = env.read_insurance_balance();
    let engine_vault = env.read_engine_vault();
    let large_passive_pnl = env.read_account_pnl(lp_passive_idx);
    let small_pnl = env.read_account_pnl(small_idx);
    let lp1_pnl = env.read_account_pnl(lp1_idx);

    println!(
        "dust trades attempted = {}, succeeded = {}, rejected = {}",
        total_dust_trades, succeeded, rejected
    );
    if let Some(e) = &first_err {
        println!("first rejection reason: {}", e);
    }
    println!("insurance_after_setup = {}", insurance_after_setup);
    println!("insurance_post = {}", insurance_post);
    println!("engine_vault = {}", engine_vault);
    println!(
        "large_passive_pnl = {}, small_pnl = {}, lp1_pnl = {}",
        large_passive_pnl, small_pnl, lp1_pnl
    );

    // ── PRIMARY ASSERTION ───────────────────────────────────────────────
    // Insurance must not have been drained below its starting level.
    assert!(
        insurance_post >= insurance_pre,
        "INSURANCE DRAINED via dust-EWMA: pre={} post={} (delta = -{})",
        insurance_pre,
        insurance_post,
        insurance_pre.saturating_sub(insurance_post),
    );
}
