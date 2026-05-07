mod common;
#[allow(unused_imports)]
use common::*;

use solana_sdk::signature::{Keypair, Signer};

/// Resolved-mode reconciliation insurance-drain probe (2026-05-06).
///
/// Hypothesis under test:
///   absorb_protocol_loss (engine line 4845) calls use_insurance_buffer
///   without crediting vault — the F7 helper-level violation is still
///   empirically present at SHA 5059332 (test_confirm_v1_vault_residual_
///   conservation panics today). Reconciliation paths post-resolve
///   (ForceCloseResolved, KeeperCrank in Resolved mode, AdminForceClose)
///   call settle_losses + resolve_flat_negative which fan into
///   absorb_protocol_loss when an account ends with negative PnL.
///
/// WIN CONDITION:
///   insurance_pre - insurance_post  >  legitimate_loss_total
///   where legitimate_loss_total = pre-resolve sum of |neg_PnL_i|.
///
/// Strategy:
///   - Seed insurance fat enough to be a real target.
///   - Set up an LP (counterparty) plus 3 traders with deposits.
///   - Open positions, drive oracle adversarially via set_slot_and_price
///     (which respects the per-slot cap — multi-slot walk).
///   - Crank repeatedly so the engine recognizes losses on losing side.
///   - Snapshot pre-resolve neg-PnL sum.
///   - Resolve (admin or permissionless) — engine enters Resolved mode.
///   - Permissionlessly call ForceCloseResolved on each user.
///   - Compare insurance delta to legitimate loss footprint.
///
/// Even if no asymmetric drain shows up, the test prints all the
/// numbers needed to interpret the result and asserts the soundness
/// invariant insurance_decrease ≤ legitimate_loss_total.
#[test]
fn test_resolved_reconciliation_drain() {
    let mut env = TestEnv::new();
    env.init_market_with_invert(0);

    let admin = Keypair::from_bytes(&env.payer.to_bytes()).unwrap();

    // ── Seed insurance generously ────────────────────────────────────────
    let insurance_seed: u64 = 10_000_000_000;
    env.top_up_insurance(&admin, insurance_seed);

    let insurance_pre = env.read_insurance_balance();
    println!("=== RESOLVED RECONCILIATION DRAIN TEST ===");
    println!("insurance_pre = {}", insurance_pre);

    // ── Spin up LP (counterparty) ───────────────────────────────────────
    let lp = Keypair::new();
    let lp_idx = env.init_lp(&lp);
    let lp_deposit: u64 = 200_000_000_000;
    env.deposit(&lp, lp_idx, lp_deposit);

    // ── Three traders going LONG against the LP at $138 ─────────────────
    let trader_count = 3;
    let trader_deposit: u64 = 5_000_000_000; // 5 SOL each
    let trade_size: i128 = 500_000;          // long 0.5e6 each

    let mut traders: Vec<(Keypair, u16)> = Vec::new();
    for _ in 0..trader_count {
        let t = Keypair::new();
        let t_idx = env.init_user(&t);
        env.deposit(&t, t_idx, trader_deposit);
        traders.push((t, t_idx));
    }

    env.crank();

    for (t, t_idx) in &traders {
        env.trade(t, &lp, lp_idx, *t_idx, trade_size);
    }

    // ── Drive price DOWN from 138M → 110M (~20%). ──────────────────────
    // set_slot_and_price chunks the walk so per-slot bps cap is respected.
    let target_slot: u64 = 4_000;
    let adverse_px: i64 = 110_000_000;
    env.set_slot_and_price(target_slot, adverse_px);

    // Many cranks to flush accrual, liquidations, etc.
    for _ in 0..15 {
        let _ = env.try_crank();
    }

    // ── Snapshot pre-resolve PnL of every account ───────────────────────
    let mut legitimate_loss_total: i128 = 0;
    let mut all_idxs: Vec<u16> = Vec::new();
    all_idxs.push(lp_idx);
    for (_, t_idx) in &traders {
        all_idxs.push(*t_idx);
    }

    println!("--- pre-resolve account snapshot ---");
    let lp_pnl_pre = env.read_account_pnl(lp_idx);
    let lp_cap_pre = env.read_account_capital(lp_idx);
    println!(
        "LP   idx={} cap={} pnl={}",
        lp_idx, lp_cap_pre, lp_pnl_pre
    );
    if lp_pnl_pre < 0 {
        legitimate_loss_total = legitimate_loss_total.saturating_add(-lp_pnl_pre);
    }

    for (_, t_idx) in &traders {
        let cap = env.read_account_capital(*t_idx);
        let pnl = env.read_account_pnl(*t_idx);
        println!("USER idx={} cap={} pnl={}", t_idx, cap, pnl);
        if pnl < 0 {
            legitimate_loss_total = legitimate_loss_total.saturating_add(-pnl);
        }
    }
    println!(
        "legitimate_loss_total (sum of |neg_PnL_i|) = {}",
        legitimate_loss_total
    );

    let engine_vault_pre = env.read_engine_vault();
    let spl_vault_pre = env.vault_balance();
    println!(
        "engine_vault_pre = {} | spl_vault_pre = {}",
        engine_vault_pre, spl_vault_pre
    );

    // ── ResolveMarket (admin path) ──────────────────────────────────────
    env.try_resolve_market(&admin, 0)
        .expect("admin ResolveMarket should succeed");
    assert!(
        env.is_market_resolved(),
        "market must be resolved after ResolveMarket"
    );
    println!("market resolved");

    // Crank in resolved mode (early-return branch) and then run reconciliation.
    let _ = env.try_crank();
    let _ = env.try_crank();

    // ── Permissionless ForceCloseResolved on every account ──────────────
    // After ResolveMarket the force_close_delay window may need to elapse.
    // Walk clock forward generously.
    let now_slot = env.svm.get_sysvar::<solana_sdk::clock::Clock>().slot;
    let mut clk = env.svm.get_sysvar::<solana_sdk::clock::Clock>();
    clk.slot = now_slot.saturating_add(10_000);
    clk.unix_timestamp = clk.unix_timestamp.saturating_add(10_001);
    env.svm.set_sysvar(&clk);

    // Try force-close resolved on traders first (they're the loss side).
    for (t, t_idx) in &traders {
        let r = env.try_force_close_resolved(*t_idx, &t.pubkey());
        println!(
            "force_close_resolved trader idx={} result={:?}",
            t_idx,
            r.as_ref().map(|_| "OK").map_err(|e| e.as_str())
        );
        // If permissionless force-close needs admin retry path:
        if r.is_err() {
            let r2 = env.try_admin_force_close_account(&admin, *t_idx, &t.pubkey());
            println!(
                "admin_force_close_account fallback trader idx={} result={:?}",
                t_idx,
                r2.as_ref().map(|_| "OK").map_err(|e| e.as_str())
            );
            // Second pass for ProgressOnly two-phase semantics.
            let r3 = env.try_admin_force_close_account(&admin, *t_idx, &t.pubkey());
            println!(
                "admin_force_close_account 2nd-pass trader idx={} result={:?}",
                t_idx,
                r3.as_ref().map(|_| "OK").map_err(|e| e.as_str())
            );
        }
    }

    // Then LP.
    let r_lp = env.try_force_close_resolved(lp_idx, &lp.pubkey());
    println!(
        "force_close_resolved LP idx={} result={:?}",
        lp_idx,
        r_lp.as_ref().map(|_| "OK").map_err(|e| e.as_str())
    );
    if r_lp.is_err() {
        let r2 = env.try_admin_force_close_account(&admin, lp_idx, &lp.pubkey());
        println!(
            "admin_force_close_account fallback LP idx={} result={:?}",
            lp_idx,
            r2.as_ref().map(|_| "OK").map_err(|e| e.as_str())
        );
        let r3 = env.try_admin_force_close_account(&admin, lp_idx, &lp.pubkey());
        println!(
            "admin_force_close_account 2nd-pass LP idx={} result={:?}",
            lp_idx,
            r3.as_ref().map(|_| "OK").map_err(|e| e.as_str())
        );
    }

    // ── Snapshot post-reconciliation state ──────────────────────────────
    let insurance_post = env.read_insurance_balance();
    let engine_vault_post = env.read_engine_vault();
    let spl_vault_post = env.vault_balance();
    println!(
        "insurance_post = {} | engine_vault_post = {} | spl_vault_post = {}",
        insurance_post, engine_vault_post, spl_vault_post
    );

    let lp_pnl_post = env.read_account_pnl(lp_idx);
    let lp_cap_post = env.read_account_capital(lp_idx);
    println!(
        "LP   post idx={} cap={} pnl={}",
        lp_idx, lp_cap_post, lp_pnl_post
    );
    for (_, t_idx) in &traders {
        let cap = env.read_account_capital(*t_idx);
        let pnl = env.read_account_pnl(*t_idx);
        println!("USER post idx={} cap={} pnl={}", t_idx, cap, pnl);
    }

    // ── Soundness invariant ─────────────────────────────────────────────
    let insurance_decrease: i128 = insurance_pre as i128 - insurance_post as i128;
    println!(
        "insurance_decrease = {} | legitimate_loss_total = {}",
        insurance_decrease, legitimate_loss_total
    );

    // Allow a small rounding tolerance for accrual residuals.
    const ROUNDING_TOLERANCE: i128 = 100_000;

    if insurance_decrease > legitimate_loss_total.saturating_add(ROUNDING_TOLERANCE) {
        panic!(
            "FINDING: insurance dropped by {} (pre={} post={}) but legitimate \
             pre-resolve loss total was only {} — drain delta {} exceeds tolerance",
            insurance_decrease,
            insurance_pre,
            insurance_post,
            legitimate_loss_total,
            insurance_decrease - legitimate_loss_total,
        );
    }

    println!(
        "PASS: insurance soundness held (decrease {} ≤ legitimate_loss {} + tol {})",
        insurance_decrease, legitimate_loss_total, ROUNDING_TOLERANCE
    );
}
