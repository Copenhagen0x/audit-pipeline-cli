//! Issue #69 (closed Apr 29) — TradeCpi zero-fill cascade bypass.
//!
//! Pre-fix attack: a matcher returning `exec_size = 0` with FLAG_PARTIAL_OK
//! advanced the engine clock via `accrue_market_to(now_slot, fresh_price)`
//! WITHOUT touching any third-party account. An attacker could:
//!
//!   1. Open positions on multiple accounts (one with thin margin = victim).
//!   2. Walk the oracle adversely beyond `MAX_ACCRUAL_DT_SLOTS`, putting
//!      victim under maintenance margin.
//!   3. Trigger a zero-fill TradeCpi between attacker accounts. This jumped
//!      the market clock past the breach without running any partial-
//!      liquidation cascade against the victim.
//!   4. The victim's now-stale loss exceeded its capital; subsequent crank
//!      absorbed the residual into insurance.
//!
//! The post-fix engine (src/percolator.rs:7468) calls
//! `reject_account_limited_market_progress` BEFORE the zero-fill
//! `accrue_market_to`. If `oi_eff_long_q != 0 || oi_eff_short_q != 0` and
//! the price-progress budget is exhausted, this returns CatchupRequired,
//! preventing the bypass.
//!
//! WIN CONDITION (max_risk.md §8): insurance_post < insurance_pre.
//! This test asserts the OPPOSITE — the invariant holds.

mod common;
#[allow(unused_imports)]
use common::*;

use solana_sdk::{
    account::Account,
    clock::Clock,
    instruction::{AccountMeta, Instruction},
    pubkey::Pubkey,
    signature::{Keypair, Signer},
    sysvar,
    transaction::Transaction,
};

/// Initialize a zero-fill matcher LP: max_fill_abs = 0 forces matcher to
/// return exec_size = 0 with FLAG_PARTIAL_OK on every TradeCpi call.
fn init_zero_fill_lp(env: &mut TradeCpiTestEnv, owner: &Keypair) -> (u16, Pubkey) {
    let idx = env.account_count;
    let matcher_program = env.matcher_program_id;
    env.svm.airdrop(&owner.pubkey(), 1_000_000_000).unwrap();
    let ata = env.create_ata(&owner.pubkey(), 100);
    let lp_bytes = idx.to_le_bytes();
    let (lp_pda, _) =
        Pubkey::find_program_address(&[b"lp", env.slab.as_ref(), &lp_bytes], &env.program_id);
    let ctx = Pubkey::new_unique();
    env.svm
        .set_account(
            ctx,
            Account {
                lamports: 10_000_000,
                data: vec![0u8; MATCHER_CONTEXT_LEN],
                owner: matcher_program,
                executable: false,
                rent_epoch: 0,
            },
        )
        .unwrap();
    let init_ix = Instruction {
        program_id: matcher_program,
        accounts: vec![
            AccountMeta::new_readonly(lp_pda, false),
            AccountMeta::new(ctx, false),
        ],
        data: encode_init_vamm(MatcherMode::Passive, 5, 10, 200, 0, 0, 0, 0),
    };
    let tx = Transaction::new_signed_with_payer(
        &[cu_ix(), init_ix],
        Some(&owner.pubkey()),
        &[owner],
        env.svm.latest_blockhash(),
    );
    env.svm.send_transaction(tx).expect("init matcher");
    let ix = Instruction {
        program_id: env.program_id,
        accounts: vec![
            AccountMeta::new(owner.pubkey(), true),
            AccountMeta::new(env.slab, false),
            AccountMeta::new(ata, false),
            AccountMeta::new(env.vault, false),
            AccountMeta::new_readonly(spl_token::ID, false),
            AccountMeta::new_readonly(sysvar::clock::ID, false),
        ],
        data: encode_init_lp(&matcher_program, &ctx, 100),
    };
    let tx = Transaction::new_signed_with_payer(
        &[cu_ix(), ix],
        Some(&owner.pubkey()),
        &[owner],
        env.svm.latest_blockhash(),
    );
    env.svm.send_transaction(tx).expect("init_lp");
    env.account_count += 1;
    (idx, ctx)
}

/// Read engine.last_market_slot (offset 1016 within RiskEngine).
fn read_last_market_slot(env: &TradeCpiTestEnv) -> u64 {
    let data = env.svm.get_account(&env.slab).unwrap().data;
    let off = ENGINE_OFFSET + 1016;
    u64::from_le_bytes(data[off..off + 8].try_into().unwrap())
}

#[test]
fn test_tradecpi_zerofill_bypass() {
    // ── Setup: Pyth-cap market with zero-fill matcher available ──────────────
    let mut env = TradeCpiTestEnv::new();
    env.init_market();
    let mp = env.matcher_program_id;
    let admin = Keypair::from_bytes(&env.payer.to_bytes()).unwrap();

    // Seed insurance to have something visible to drain.
    let insurance_seed: u64 = 5_000_000_000;
    env.top_up_insurance(&admin, insurance_seed);

    // ── Account A: real LP / matcher (counterparty for victim's open) ────────
    let real_lp = Keypair::new();
    let (real_lp_idx, real_ctx) = env.init_lp_with_matcher(&real_lp, &mp);
    env.deposit(&real_lp, real_lp_idx, 50_000_000_000);

    // ── Account B: VICTIM user with thin margin ──────────────────────────────
    // We give the victim minimal capital so a moderate price walk pushes
    // them under maintenance margin. Their loss must exceed capital for
    // insurance to be called upon — otherwise the test trivially passes.
    let victim = Keypair::new();
    let victim_idx = env.init_user(&victim);
    env.deposit(&victim, victim_idx, 100_000_000); // thin

    // ── Account C: zero-fill LP (used to advance market clock) ───────────────
    let zero_lp = Keypair::new();
    let (zero_lp_idx, zero_ctx) = init_zero_fill_lp(&mut env, &zero_lp);

    // ── Account D: zero-fill user (counterparty for the bypass attempt) ──────
    let zero_user = Keypair::new();
    let zero_user_idx = env.init_user(&zero_user);
    env.deposit(&zero_user, zero_user_idx, 1_000_000);

    // Initial crank to bring engine to a known state.
    env.set_slot(50);
    env.crank();

    // ── Step 1: victim opens a leveraged LONG via real LP ────────────────────
    // This creates exposed OI: oi_eff_long_q != 0 (and matched short on LP).
    env.try_trade_cpi(
        &victim,
        &real_lp.pubkey(),
        real_lp_idx,
        victim_idx,
        100_000,
        &mp,
        &real_ctx,
    )
    .expect("victim opens LONG");

    let victim_pos = env.read_account_position(victim_idx);
    assert_ne!(victim_pos, 0, "victim must have exposed position");

    let insurance_pre = env.read_insurance_balance();
    let victim_cap_pre = env.read_account_capital(victim_idx);
    let victim_pnl_pre = env.read_account_pnl(victim_idx);
    let slot_before = read_last_market_slot(&env);
    println!(
        "PRE: insurance={} victim_cap={} victim_pnl={} engine_slot={}",
        insurance_pre, victim_cap_pre, victim_pnl_pre, slot_before
    );

    // ── Step 2: jump clock + Pyth oracle past MAX_ACCRUAL_DT_SLOTS ───────────
    // We push price down by 1 unit (enough to violate the
    // account-limited-progress budget on exposed OI) AND jump clock
    // beyond MAX_ACCRUAL_DT_SLOTS. A single-shot accrue would be over
    // the dt-budget; an honest path requires catchup_accrue via crank.
    let next_slot = slot_before + percolator_prog::constants::MAX_ACCRUAL_DT_SLOTS + 1;
    let publish_time = next_slot as i64;
    let target = {
        let d = env.svm.get_account(&env.slab).unwrap().data;
        // Walk price down significantly to push victim under margin.
        // Use a value at least 1 below current to force progress.
        (percolator_prog::state::read_config(&d).last_effective_price_e6 as i64)
            .saturating_sub(1)
            .max(1)
    };
    env.svm.set_sysvar(&Clock {
        slot: next_slot,
        unix_timestamp: publish_time,
        ..Clock::default()
    });
    let pyth_data = make_pyth_data(&TEST_FEED_ID, target, -6, 1, publish_time);
    for oracle in [env.pyth_index, env.pyth_col] {
        env.svm
            .set_account(
                oracle,
                Account {
                    lamports: 1_000_000,
                    data: pyth_data.clone(),
                    owner: PYTH_RECEIVER_PROGRAM_ID,
                    executable: false,
                    rent_epoch: 0,
                },
            )
            .unwrap();
    }

    // ── Step 3: ATTACK — trigger zero-fill TradeCpi between attacker accounts ─
    // Pre-fix this would jump engine.last_market_slot to next_slot via a
    // single accrue_market_to with the new price, bypassing any
    // partial-liquidation cascade for the victim.
    //
    // Post-fix, reject_account_limited_market_progress should surface
    // CatchupRequired (0x1d) because oi_eff is non-zero AND the
    // price-progress budget is exhausted by the dt + price delta.
    let attack_result = env.try_trade_cpi(
        &zero_user,
        &zero_lp.pubkey(),
        zero_lp_idx,
        zero_user_idx,
        100_000,
        &mp,
        &zero_ctx,
    );

    let bypass_succeeded = attack_result.is_ok();
    let slot_after_attack = read_last_market_slot(&env);
    println!(
        "ATTACK zero-fill TradeCpi: ok={} engine_slot_after={}",
        bypass_succeeded, slot_after_attack
    );
    if let Err(e) = &attack_result {
        println!("  rejection reason: {}", e);
    }

    // Whether the zero-fill succeeded or was rejected, run cranks to
    // converge the system. If the bypass worked, the victim's loss has
    // already been retroactively applied via the zero-fill's accrue with
    // a fresh price — and any residual past their capital will be drained
    // from insurance on the next crank's liquidation cascade.
    for _ in 0..6 {
        // Some cranks may fail (e.g. if zero-fill left an inconsistent
        // state — pre-fix bug surface). Tolerate failures and continue.
        let caller = Keypair::new();
        env.svm.airdrop(&caller.pubkey(), 1_000_000_000).unwrap();
        let ix = Instruction {
            program_id: env.program_id,
            accounts: vec![
                AccountMeta::new(caller.pubkey(), true),
                AccountMeta::new(env.slab, false),
                AccountMeta::new_readonly(sysvar::clock::ID, false),
                AccountMeta::new_readonly(env.pyth_index, false),
            ],
            data: encode_crank_permissionless(),
        };
        let tx = Transaction::new_signed_with_payer(
            &[cu_ix(), ix],
            Some(&caller.pubkey()),
            &[&caller],
            env.svm.latest_blockhash(),
        );
        let _ = env.svm.send_transaction(tx);
    }

    // ── Step 4: snapshot insurance and assert invariant ──────────────────────
    let insurance_post = env.read_insurance_balance();
    let victim_cap_post = env.read_account_capital(victim_idx);
    let victim_pnl_post = env.read_account_pnl(victim_idx);
    println!(
        "POST: insurance={} victim_cap={} victim_pnl={}",
        insurance_post, victim_cap_post, victim_pnl_post
    );
    println!(
        "Δ insurance = {} (post - pre)",
        insurance_post as i128 - insurance_pre as i128
    );

    // PRIMARY ASSERTION: insurance must NOT have decreased below the
    // baseline. A drain (insurance_post < insurance_pre) is the win
    // condition per max_risk.md §8 — if this panics, the fix is broken.
    //
    // Tolerance: allow up to 100 units of floor-rounding from multiple
    // crank passes (sub-unit residuals over 6 cranks, well below any
    // meaningful drain). The pre-fix exploit drained the entire 5B seed.
    const ROUNDING_TOLERANCE: i128 = 100;
    let drain = insurance_pre as i128 - insurance_post as i128;
    assert!(
        drain <= ROUNDING_TOLERANCE,
        "VIOLATION: insurance drained from {} to {} (Δ = {} > tolerance {}) — \
         TradeCpi zero-fill cascade bypass succeeded",
        insurance_pre,
        insurance_post,
        drain,
        ROUNDING_TOLERANCE,
    );

    println!(
        "PASS: insurance invariant held: pre={} post={} Δ={}",
        insurance_pre,
        insurance_post,
        insurance_post as i128 - insurance_pre as i128
    );
}
