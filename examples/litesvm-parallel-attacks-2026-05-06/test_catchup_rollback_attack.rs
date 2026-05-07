//! CatchupAccrue partial-mode rollback attack — issue #76 follow-up.
//!
//! ATTACK HYPOTHESIS (closed disclosure thread #76):
//!   In the partial-catchup branch of `Instruction::CatchupAccrue`, when the
//!   wrapper hit `CATCHUP_CHUNKS_MAX` it performed a config rollback via
//!   `restored = config_pre`. The fix preserved SOME timestamp fields but the
//!   original bug missed `last_oracle_publish_time`. A partial catchup
//!   followed by an oracle push at the same timestamp could advance state
//!   with stale oracle metadata and bypass freshness checks, eventually
//!   triggering insurance absorption.
//!
//! THIS TEST exercises the public-instruction sequence end-to-end against the
//! live BPF binary and asserts the win condition (insurance drain) DOES NOT
//! occur.
//!
//! KEY FINDING (verified in this test):
//!   Tag 31 (`CatchupAccrue`) is **fully retired** in the v12.19.6 binary.
//!   It rejects with `InvalidInstructionData` for any caller, regardless of
//!   timestamp / chunk-budget construction. Public market-clock progress is
//!   routed exclusively through `KeeperCrank`, which performs the
//!   accrue-to-target loop atomically without an exposed partial / rollback
//!   surface. The fix path for #76 was to remove the bug-prone branch
//!   entirely, not patch the missing rollback field.
//!
//! WIN CONDITION (max_risk.md §8): insurance_post < insurance_pre.

mod common;
#[allow(unused_imports)]
use common::*;

use solana_sdk::signature::{Keypair, Signer};

#[test]
fn test_catchup_rollback_attack() {
    let mut env = TestEnv::new();
    env.init_market_with_invert(0);

    let admin = Keypair::from_bytes(&env.payer.to_bytes()).unwrap();

    // Seed insurance so any drain is visible.
    let insurance_seed: u64 = 5_000_000_000;
    env.top_up_insurance(&admin, insurance_seed);

    // Open positions so accrual has economic effect.
    let lp = Keypair::new();
    let lp_idx = env.init_lp(&lp);
    env.deposit(&lp, lp_idx, 50_000_000_000);

    let user = Keypair::new();
    let user_idx = env.init_user(&user);
    env.deposit(&user, user_idx, 10_000_000_000);

    env.crank();

    // Matched pair so the LP carries directional exposure that funding /
    // accrual can move against insurance under a vulnerable engine.
    env.trade(&user, &lp, lp_idx, user_idx, 1_000_000);
    env.crank();

    let insurance_pre = env.read_insurance_balance();
    let slot_before_attack = env.read_last_market_slot();
    println!(
        "insurance_pre = {}, slot_before_attack = {}",
        insurance_pre, slot_before_attack
    );

    // ── Step 1: walk slot far enough that a partial catchup WOULD be needed
    //
    // MAX_ACCRUAL_DT_SLOTS = 100 (per wrapper §1.4). To force the historical
    // partial-mode branch we need a gap > MAX_ACCRUAL_DT_SLOTS *
    // CATCHUP_CHUNKS_MAX. Jump 5_000 slots without walking / cranking.
    let attack_slot = slot_before_attack + 5_000;
    env.set_slot_and_price_raw_no_walk(attack_slot, 138_000_000);

    // ── Step 2: attempt the retired CatchupAccrue
    //
    // Pre-fix this would have run the partial branch, hit CATCHUP_CHUNKS_MAX,
    // and rolled back config — but missed `last_oracle_publish_time`. The
    // post-fix binary rejects the instruction outright with
    // `InvalidInstructionData`. Atomicity guarantee: no slab mutation, no
    // insurance movement.
    let result = env.try_catchup_accrue();
    assert!(
        result.is_err(),
        "Tag 31 CatchupAccrue must reject post-fix; got Ok which would imply the retired branch is live"
    );
    let err_str = result.unwrap_err();
    assert!(
        err_str.contains("InvalidInstructionData") || err_str.contains("invalid instruction data"),
        "expected InvalidInstructionData for retired CatchupAccrue tag, got: {err_str}"
    );

    // Atomicity: the failed instruction must not have moved insurance or
    // advanced the market slot.
    let insurance_after_reject = env.read_insurance_balance();
    let slot_after_reject = env.read_last_market_slot();
    assert_eq!(
        insurance_after_reject, insurance_pre,
        "rejected CatchupAccrue must not mutate insurance"
    );
    assert_eq!(
        slot_after_reject, slot_before_attack,
        "rejected CatchupAccrue must not advance market slot"
    );

    // ── Step 3: push oracle at same / advanced timestamp
    //
    // In the original attack chain, an oracle push at the same publish_time
    // as the partial-rollback would let stale metadata persist while the
    // engine slot advanced. With Tag 31 retired, oracle freshness must now
    // route through KeeperCrank's atomic gate. We re-stamp the oracle and
    // crank: the engine should accrue cleanly (or refuse if the gap exceeds
    // the per-crank envelope), but never silently corrupt insurance.
    env.set_slot_and_price_raw_no_walk(attack_slot + 1, 138_500_000);

    // ── Step 4: trigger more accrual via the public KeeperCrank path
    //
    // try_crank lets us see whether the gap is gracefully refused or
    // gracefully absorbed. Either outcome is acceptable as long as the
    // insurance invariant holds.
    for _ in 0..15 {
        let _ = env.try_crank();
    }

    // ── Step 5: simulate retroactive accrual with adverse oracle walk
    //
    // If timestamp rollback were broken, retroactive accrual at the wrong
    // rate would compound here. We move the oracle adversely (within the
    // per-slot cap) and crank repeatedly to surface any silent corruption.
    let walked_slot = attack_slot + 200;
    env.set_slot_and_price(walked_slot, 134_000_000);
    for _ in 0..10 {
        let _ = env.try_crank();
    }

    // ── Snapshot post-attack state ───────────────────────────────────────
    let insurance_post = env.read_insurance_balance();
    println!("insurance_post = {}", insurance_post);
    println!(
        "delta = {}",
        insurance_post as i128 - insurance_pre as i128
    );

    // ── PRIMARY ASSERTION: insurance invariant ───────────────────────────
    //
    // Win condition: insurance_post < insurance_pre. We assert the inverse.
    // If this panics, the retired CatchupAccrue surface (or some other path
    // touched by the same sequence) leaked into a real drain.
    assert!(
        insurance_post >= insurance_pre,
        "VIOLATION (issue #76 attack succeeded): insurance drained from {} to {} (delta = {})",
        insurance_pre,
        insurance_post,
        insurance_pre as i128 - insurance_post as i128,
    );

    println!(
        "PASS: CatchupAccrue partial-rollback attack blocked. insurance_pre={} insurance_post={}",
        insurance_pre, insurance_post
    );
}
