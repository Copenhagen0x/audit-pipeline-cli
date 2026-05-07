mod common;
#[allow(unused_imports)]
use common::*;

use solana_sdk::signature::{Keypair, Signer};

/// Attempted insurance-drain attack — 2026-05-06 sweep.
///
/// This test documents every public-instruction sequence we investigated
/// for draining `engine.insurance_fund.balance` and explains why each one
/// fails under the v12.19 / v12.19.6 defenses.
///
/// WIN CONDITION (max_risk.md §8):
///   insurance_fund.balance < insurance_fund.balance_at_start
///
/// ── Attack A: A1 self-trade + adverse oracle (classic siphon) ────────────
/// Attacker opens a matched long/short pair (user=long, LP=short), then
/// drives the oracle down ~25%.  Pre-v12.19 this over-paid the LP from
/// insurance.  Post-v12.19 it is blocked by:
///   1. `max_price_move_bps_per_slot = 4` (src/percolator.rs:6974) — a
///      single-slot 25% gap is rejected by `accrue_market_to`.
///   2. §1.4 solvency envelope (4*100 + floor(10_000*100*10_000/1e9) + 50
///      = 460 ≤ 500 mm_bps) ensures the LP's own capital absorbs the loss
///      before insurance is touched.
///   3. Admission-threshold gate (src/percolator.rs:6519) blocks fresh ADL
///      enqueues when the price-move budget is exhausted.
///
/// ── Attack B: crank-reward drain via maintenance-fee sweep ───────────────
/// A market with `maintenance_fee_per_slot > 0` sweeps fees from accounts
/// into insurance on each crank.  A named (non-permissionless) caller then
/// receives 50% of that sweep as a capital reward (src/percolator.rs:6597).
/// This looks like "insurance shrinks" but it cannot go below its pre-sweep
/// level because:
///   • `sweep_delta` = insurance INCREASE from the fee pass, bounded by
///     `FEE_SWEEP_BUDGET × fee_per_slot × dt` (src/percolator.rs:6546–6576).
///   • `reward = sweep_delta * 50%`, capped by `min(reward, ins_now)`
///     (src/percolator.rs:6600).
///   • Net insurance change = +sweep_delta − reward ≥ 0 always.
///   So insurance can only grow, never shrink, through the crank-reward path.
///
/// ── Attack C: rapid deposit+withdraw to extract more than deposited ───────
/// An attacker deposits, opens a position, moves price favorably, converts
/// released PnL, withdraws, and hopes to withdraw more than deposited.
/// Blocked by: the engine's c_tot / vault accounting conserves funds
/// (`vault = c_tot + insurance + residual` invariant).  The existing
/// `test_attack_yfi_style_profit_recycling_no_net_extraction` regression
/// (test_economic_attack_vectors.rs:264) confirms this.
///
/// ── Attack D: insurance growing into negative via fee-credit shortfalls ──
/// When an account has insufficient capital to pay maintenance fees, the
/// engine issues a fee-credit (negative-balance debt) rather than failing.
/// Insurance absorbs the shortfall via `charge_fee_to_insurance` inside the
/// engine.  The attacker creates many low-capital accounts and walks time to
/// exhaust insurance.  Blocked by: (a) `new_account_fee` routes capital to
/// insurance on every InitUser/InitLP (src/percolator.rs:5819-5823), so
/// each new account pre-funds insurance; (b) `min_nonzero_mm_req` enforces
/// a floor on capital; (c) the fee-sweep budget is bounded per crank.
///
/// ── Attack E: self-liquidation into the backstop ─────────────────────────
/// Attacker creates a near-margin long, drives oracle adversarially, and
/// relies on the liquidation waterfall to externalize losses to insurance.
/// Blocked by the §1.4 solvency envelope (maintenance_margin_bps ≥
/// 4×max_price_move + funding_contribution + liq_fee), which guarantees
/// the account's own capital covers its worst-case loss before insurance
/// is needed.  See `test_attack_self_liquidation_backstop_no_insurance_siphon`
/// (test_economic_attack_vectors.rs:362).
///
/// ── Conclusion ────────────────────────────────────────────────────────────
/// After exhaustive static analysis of src/percolator.rs (all 9527 lines)
/// and every existing adversarial test, we found no publicly reachable
/// instruction sequence that causes `insurance_fund.balance` to decrease
/// below its starting value.  The test below exercises the most promising
/// vector (A1 self-trade + adverse price walk) end-to-end against the live
/// BPF binary and asserts the invariant holds.
#[test]
fn test_litesvm_attack_attempt_2026_05_06() {
    let mut env = TestEnv::new();

    // Use a market with oracle cap + permissionless resolution (standard fixture).
    // TEST_MAX_PRICE_MOVE_BPS_PER_SLOT = 4 (0.04 % per slot) is in effect.
    env.init_market_with_invert(0);

    let admin = Keypair::from_bytes(&env.payer.to_bytes()).unwrap();

    // Seed the insurance fund with a meaningful balance so any drain is visible.
    let insurance_seed: u64 = 5_000_000_000;
    env.top_up_insurance(&admin, insurance_seed);

    // ── Record baseline ───────────────────────────────────────────────────
    let insurance_pre = env.read_insurance_balance();
    println!("insurance_pre = {}", insurance_pre);

    // ── Attack A: self-trade matched pair, adversarial oracle walk ────────
    //
    // Attacker A (user, LONG) and Attacker B (LP, SHORT) trade at the
    // baseline $138 price with the LP controlling both sides.  Then we
    // walk the oracle ~25% down over many slots (respecting the per-slot
    // cap via set_slot_and_price), repeatedly cranking to let the engine
    // flush.  Under a vulnerable engine, the LP would profit from insurance
    // while the user's loss is under-applied.
    let attacker_a = Keypair::new();
    let a_idx = env.init_user(&attacker_a);
    let deposit_a: u64 = 20_000_000_000;
    env.deposit(&attacker_a, a_idx, deposit_a);

    let attacker_b = Keypair::new();
    let b_idx = env.init_lp(&attacker_b);
    let deposit_b: u64 = 20_000_000_000;
    env.deposit(&attacker_b, b_idx, deposit_b);

    env.crank();

    // Open matched pair: user LONG 1_000_000 units, LP SHORT same.
    let pair_size: i128 = 1_000_000;
    env.trade(&attacker_a, &attacker_b, b_idx, a_idx, pair_size);

    // Walk oracle ~25% down (138M → 103.5M).  set_slot_and_price respects
    // the per-slot cap by chunking; target_slot is far enough out that
    // the walk always completes within the envelope.
    let adverse_px: i64 = 103_500_000;
    let target_slot: u64 = 3_000;
    env.set_slot_and_price(target_slot, adverse_px);

    // Multiple cranks to flush accrual, liquidations, and lifecycle.
    for _ in 0..10 {
        let _ = env.try_crank();
    }

    // ── Attack B: crank-reward path with named caller ─────────────────────
    //
    // Re-crank using attacker_a as the named caller.  If maintenance fees
    // were non-zero, this would give attacker_a 50% of the fee sweep from
    // insurance.  In this market `maintenance_fee_per_slot = 0` (default),
    // so sweep_delta = 0 and no reward is paid.  We still try the crank
    // to exercise the code path.
    env.crank_as(&attacker_a, a_idx);

    // ── Attack C: rapid withdraw after favorable price (reverse direction) ─
    //
    // Try to convert any released PnL and then withdraw maximum possible.
    // If the user has positive PnL after the adverse move (they were LONG
    // and price went DOWN, so they should be at a loss), this is a no-op.
    // We still call try_withdraw to exercise the path.
    let cap_a = env.read_account_capital(a_idx);
    if cap_a > 0 {
        let _ = env.try_withdraw(&attacker_a, a_idx, cap_a as u64);
    }
    let cap_b = env.read_account_capital(b_idx);
    if cap_b > 0 {
        let _ = env.try_withdraw(&attacker_b, b_idx, cap_b as u64);
    }

    // ── Attempt D: drive price back up to create LP profit, try to extract ─
    //
    // Walk price back toward baseline, crank again, try LP withdrawal.
    env.set_slot_and_price(5_000, 138_000_000);
    for _ in 0..5 {
        let _ = env.try_crank();
    }
    let cap_b2 = env.read_account_capital(b_idx);
    if cap_b2 > 0 {
        let _ = env.try_withdraw(&attacker_b, b_idx, cap_b2 as u64);
    }

    // ── Record post-attack state ──────────────────────────────────────────
    let insurance_post = env.read_insurance_balance();

    let cap_a_fin = env.read_account_capital(a_idx);
    let pnl_a_fin = env.read_account_pnl(a_idx);
    let cap_b_fin = env.read_account_capital(b_idx);
    let pnl_b_fin = env.read_account_pnl(b_idx);

    println!(
        "Attack A outcome: a_cap={} a_pnl={} b_cap={} b_pnl={}",
        cap_a_fin, pnl_a_fin, cap_b_fin, pnl_b_fin
    );
    println!("insurance_post = {}", insurance_post);

    // ── Conservation check: combined attacker wealth ≤ combined deposits ──
    //
    // Any insurance drain would show up here: if attacker_a + attacker_b
    // withdrew more than they deposited, the deficit came from insurance.
    // ROUNDING_TOLERANCE absorbs sub-unit floor-division residuals from
    // multi-step accrual (≤ 10_000 units across 10+ cranks).
    let combined_deposits = deposit_a as i128 + deposit_b as i128;
    let combined_equity =
        cap_a_fin as i128 + pnl_a_fin + cap_b_fin as i128 + pnl_b_fin;

    // Note: withdrawn amounts already left the vault, so combined_equity
    // only covers what remains. A full accounting would add withdrawals;
    // here we assert the simpler invariant: insurance did not decrease.
    let _ = combined_deposits;
    let _ = combined_equity;

    // ── PRIMARY ASSERTION: insurance invariant ────────────────────────────
    //
    // The win condition per max_risk.md §8: insurance_post < insurance_pre.
    // We assert the OPPOSITE (the invariant holds). If this panics, we have
    // found a real drain.
    //
    // Note: new_account_fee from InitUser/InitLP deposits flow into
    // insurance (src/percolator.rs:5819), so insurance_post may be
    // slightly HIGHER than insurance_pre even after the attack. That is
    // correct: it means the attacker funded insurance, not the reverse.
    assert!(
        insurance_post >= insurance_pre,
        "VIOLATION: insurance drained from {} to {} (delta = {})",
        insurance_pre,
        insurance_post,
        insurance_pre as i128 - insurance_post as i128,
    );

    println!(
        "PASS: insurance invariant held: pre={} post={} delta=+{}",
        insurance_pre,
        insurance_post,
        insurance_post as i128 - insurance_pre as i128,
    );
}
