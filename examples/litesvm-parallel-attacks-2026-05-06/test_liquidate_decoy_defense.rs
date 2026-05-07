//! Verifies the wrapper's defense against zero-position decoy liquidation.
//!
//! Disclosure threads #58/#59: a `LiquidateAtOracle` (now routed through
//! KeeperCrank with explicit candidates) must not advance market state in a
//! way that drains `engine.insurance_fund.balance` when invoked against a
//! flat (zero-position) decoy account. The expected defense is one of:
//!   (a) reject the call against a flat decoy at entry, OR
//!   (b) run the partial-liquidation cascade on real positioned candidates
//!       irrespective of the decoy's flat state.
//!
//! This test focuses on the conservative invariant: insurance must be
//! preserved across the decoy-liquidation sequence.
//!
//! Scenario:
//!   - Market with 5B insurance topped up post-init.
//!   - LP + three users: U1 (target with real position), U2 (decoy who
//!     opens then closes their position to be flat), U3 (uninvolved).
//!   - U1 is walked toward margin breach via an adverse price walk.
//!   - LiquidateAtOracle is invoked against U2 (the flat decoy).
//!   - Crank.
//!   - Insurance pre/post must be (approximately) equal — no drain.

mod common;
#[allow(unused_imports)]
use common::*;

use solana_sdk::signature::Keypair;

#[test]
fn test_liquidate_decoy_defense() {
    let mut env = TestEnv::new();
    env.init_market_with_cap(0, 100);

    // Top up insurance to ~5B (same dial used by the self-liquidation probe).
    let admin = Keypair::from_bytes(&env.payer.to_bytes()).unwrap();
    env.top_up_insurance(&admin, 5_000_000_000);

    // LP carries the matching short for U1's long.
    let lp = Keypair::new();
    let lp_idx = env.init_lp(&lp);
    let lp_deposit: u64 = 60_000_000_000;
    env.deposit(&lp, lp_idx, lp_deposit);

    // U1 — target with a real, large long that will drift toward margin
    // breach when we walk price down.
    let u1 = Keypair::new();
    let u1_idx = env.init_user(&u1);
    let u1_deposit: u64 = 16_000_000_000;
    env.deposit(&u1, u1_idx, u1_deposit);

    // U2 — decoy. Opens a small long, then immediately closes it so its
    // position == 0 by the time we point LiquidateAtOracle at it.
    let u2 = Keypair::new();
    let u2_idx = env.init_user(&u2);
    let u2_deposit: u64 = 4_000_000_000;
    env.deposit(&u2, u2_idx, u2_deposit);

    // U3 — uninvolved bystander; never trades.
    let u3 = Keypair::new();
    let u3_idx = env.init_user(&u3);
    env.deposit(&u3, u3_idx, 1_000_000_000);
    let _ = u3_idx;

    // ── Set up positions ──────────────────────────────────────────────
    let u1_size: i128 = 1_000_000_000;
    env.trade(&u1, &lp, lp_idx, u1_idx, u1_size);

    let u2_size: i128 = 50_000_000;
    env.trade(&u2, &lp, lp_idx, u2_idx, u2_size);
    // Close U2 immediately — flat decoy.
    env.trade(&u2, &lp, lp_idx, u2_idx, -u2_size);
    assert_eq!(
        env.read_account_position(u2_idx),
        0,
        "U2 must be flat before the decoy liquidation call"
    );

    let insurance_before = env.read_insurance_balance();
    let vault_before = env.read_engine_vault();
    let spl_before = env.vault_balance();

    // ── Drive U1 toward margin breach via adverse price walk ─────────
    // ~25% adverse move for the long. Walk + crank through the price
    // envelope as a responsive keeper would.
    env.set_slot_and_price(900, 103_500_000);
    for slot in (950..=1_800).step_by(50) {
        env.set_slot_and_price(slot, 103_500_000);
        let _ = env.try_crank();
    }

    // ── The probe: LiquidateAtOracle against the flat decoy U2 ───────
    // Defense should EITHER reject (Err) OR complete without draining
    // insurance (the cascade-on-real-candidates path).
    let liq_result = env.try_liquidate(u2_idx);

    // Continue cranking — anything the keeper would do post-liquidation.
    for slot in (1_850..=2_200).step_by(50) {
        env.set_slot_and_price(slot, 103_500_000);
        let _ = env.try_crank();
    }

    let insurance_after = env.read_insurance_balance();
    let vault_after = env.read_engine_vault();
    let spl_after = env.vault_balance();

    // U2 must still be flat — the probe did not flip them into a position.
    assert_eq!(
        env.read_account_position(u2_idx),
        0,
        "decoy U2 must remain flat after liquidation probe"
    );

    // The core defense: insurance is not drained.
    assert!(
        insurance_before.saturating_sub(insurance_after) <= 10_000,
        "decoy-liquidation drained insurance: before={} after={} delta={}",
        insurance_before,
        insurance_after,
        insurance_before as i128 - insurance_after as i128,
    );

    // Vault accounting stays synced.
    assert_eq!(
        vault_after as u64, spl_after,
        "engine vault and SPL vault desynced after decoy liquidation"
    );

    println!(
        "decoy-liquidation defended: liq_result={:?} insurance {} -> {} (Δ={}) vault {} -> {} spl {} -> {}",
        liq_result,
        insurance_before,
        insurance_after,
        insurance_before as i128 - insurance_after as i128,
        vault_before,
        vault_after,
        spl_before,
        spl_after,
    );
}
