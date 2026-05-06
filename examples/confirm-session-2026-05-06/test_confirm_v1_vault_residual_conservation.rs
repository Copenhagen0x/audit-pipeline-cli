#![cfg(feature = "test")]

// Test: test_confirm_v1_vault_residual_conservation
//
// Finding hypothesis: V1-vault-residual-conservation
//
// Claim: "if any helper shrinks insurance_fund.balance, it MUST also debit
// vault by the same amount" — i.e., the residual
//   residual = vault - (c_tot + insurance_fund.balance)
// must be conserved across every internal accounting helper.
//
// Evidence from source (percolator.rs):
//
//   Lines 4811-4821: use_insurance_buffer
//     fn use_insurance_buffer(&mut self, loss: u128) -> u128 {
//         let ins_bal = self.insurance_fund.balance.get();
//         let pay = core::cmp::min(loss, ins_bal);
//         if pay > 0 {
//             self.insurance_fund.balance = U128::new(ins_bal - pay); // line 4818
//         }
//         loss - pay
//     }
//   vault is NOT touched anywhere in this function.
//
//   Lines 4844-4852: absorb_protocol_loss wraps use_insurance_buffer and
//   record_uninsured_protocol_loss; vault is also NOT touched.
//
//   Lines 4808-4810 (doc comment): the spec says use_insurance_buffer
//   "deducts loss from insurance down to floor, return the remaining
//   uninsured loss" — it is a pure insurance-side mutation.
//
//   Lines 5971-5981: check_conservation() verifies vault >= c_tot + insurance.
//   It is marked test_visible! (line 5970) so it is pub under feature="test".
//
//   Lines 2282-2291: residual = vault - (c_tot + insurance_fund.balance)
//   is computed in admission_residual_lane.
//
// Test strategy:
//   1. Build a fresh engine and seed the insurance fund via top_up_insurance_fund
//      so that insurance_fund.balance > 0.
//   2. Also deposit a user so that c_tot > 0, giving a realistic engine state.
//   3. Snapshot pre-state: (vault, c_tot, insurance) and compute residual.
//   4. Call absorb_protocol_loss(loss) which internally calls use_insurance_buffer.
//   5. Snapshot post-state and compute new residual.
//   6. Assert residual is conserved (pre == post).
//      If the finding is TRUE (a violation), this assertion will FAIL because
//      use_insurance_buffer shrinks insurance without shrinking vault, so
//      residual = vault - (c_tot + insurance) INCREASES by pay.
//
// Expected outcome: The assertion FAILS, confirming the violation.
// The finding is TRUE — residual is NOT conserved by use_insurance_buffer /
// absorb_protocol_loss acting alone; residual increases by `pay` after the call
// because vault is left untouched while insurance_fund.balance shrinks.

use percolator::i128::U128;
use percolator::*;

fn default_params() -> RiskParams {
    RiskParams {
        maintenance_margin_bps: 500,
        initial_margin_bps: 1000,
        trading_fee_bps: 10,
        max_accounts: 64,
        liquidation_fee_bps: 100,
        liquidation_fee_cap: U128::new(1_000_000),
        min_liquidation_abs: U128::new(0),
        min_nonzero_mm_req: 10,
        min_nonzero_im_req: 11,
        h_min: 0,
        h_max: 100,
        resolve_price_deviation_bps: 1000,
        max_accrual_dt_slots: 100,
        max_abs_funding_e9_per_slot: 10_000,
        min_funding_lifetime_slots: 10_000_000,
        max_active_positions_per_side: MAX_ACCOUNTS as u64,
        max_price_move_bps_per_slot: 3,
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

#[test]
fn test_confirm_v1_vault_residual_conservation() {
    // =========================================================================
    // Phase 0: Construct engine (RiskEngine::new per percolator.rs ~line 1691).
    // All accounting fields start at zero.
    // =========================================================================
    let mut engine = RiskEngine::new(default_params());
    engine.current_slot = 0;

    // Sanity: everything is zero at start.
    assert_eq!(engine.vault.get(), 0, "vault must be zero at construction");
    assert_eq!(engine.c_tot.get(), 0, "c_tot must be zero at construction");
    assert_eq!(
        engine.insurance_fund.balance.get(),
        0,
        "insurance must be zero at construction"
    );

    // =========================================================================
    // Phase 1: Seed the insurance fund.
    // top_up_insurance_fund (percolator.rs lines 7569-7570 per v2 test comments):
    //   vault += amount, insurance_fund.balance += amount.
    // After this: vault == insurance, c_tot == 0, residual == 0.
    // =========================================================================
    let ins_seed: u128 = 50_000;
    engine.top_up_insurance_fund(ins_seed, 0).unwrap();

    assert_eq!(engine.vault.get(), ins_seed, "vault must equal ins_seed after top_up");
    assert_eq!(
        engine.insurance_fund.balance.get(),
        ins_seed,
        "insurance must equal ins_seed after top_up"
    );
    assert_eq!(engine.c_tot.get(), 0, "c_tot must remain 0 after top_up");

    // =========================================================================
    // Phase 2: Add a user and deposit capital so that c_tot > 0.
    // deposit_not_atomic (percolator.rs lines 5420/5428): vault += amount,
    // c_tot += amount.
    // After this: vault = ins_seed + dep, c_tot = dep, insurance = ins_seed.
    // residual = vault - (c_tot + insurance) = (ins_seed + dep) - (dep + ins_seed) = 0.
    // =========================================================================
    let alice = add_user_test(&mut engine, 0).unwrap();
    let dep_alice: u128 = 100_000;
    engine.deposit_not_atomic(alice, dep_alice, 0).unwrap();

    let vault_post_dep = engine.vault.get();
    let c_tot_post_dep = engine.c_tot.get();
    let ins_post_dep = engine.insurance_fund.balance.get();

    assert_eq!(vault_post_dep, ins_seed + dep_alice, "vault after deposit");
    assert_eq!(c_tot_post_dep, dep_alice, "c_tot after deposit");
    assert_eq!(ins_post_dep, ins_seed, "insurance unchanged by deposit");

    // Verify conservation holds: vault >= c_tot + insurance.
    assert!(
        engine.check_conservation(),
        "conservation must hold after setup"
    );

    // =========================================================================
    // Phase 3: Snapshot pre-absorb state.
    //
    // residual = vault - (c_tot + insurance_fund.balance)
    // (percolator.rs lines 2287-2291)
    // =========================================================================
    let pre_vault = engine.vault.get();
    let pre_c_tot = engine.c_tot.get();
    let pre_ins = engine.insurance_fund.balance.get();

    let pre_senior = pre_c_tot
        .checked_add(pre_ins)
        .expect("pre-senior must not overflow");
    assert!(
        pre_vault >= pre_senior,
        "pre: vault must be >= c_tot + insurance: vault={} senior={}",
        pre_vault,
        pre_senior
    );
    let pre_residual = pre_vault - pre_senior;

    // =========================================================================
    // Phase 4: Call absorb_protocol_loss(loss) where 0 < loss <= ins_seed.
    //
    // absorb_protocol_loss (percolator.rs lines 4844-4852):
    //   calls use_insurance_buffer(loss) (lines 4811-4821) then
    //   record_uninsured_protocol_loss for any remainder.
    //
    // use_insurance_buffer (lines 4811-4821):
    //   pay = min(loss, ins_bal)
    //   self.insurance_fund.balance -= pay  (line 4818)
    //   vault is NOT TOUCHED
    //
    // With loss=10_000 and ins_seed=50_000: pay=10_000, rem=0.
    // insurance_fund.balance shrinks by 10_000.
    // vault stays the same.
    // Therefore: post_residual = vault - (c_tot + (insurance - pay))
    //                          = pre_residual + pay
    //                          != pre_residual  (VIOLATION)
    // =========================================================================
    let loss: u128 = 10_000;
    assert!(
        pre_ins >= loss,
        "precondition: insurance must cover the full loss (pay == loss, rem == 0)"
    );

    engine.absorb_protocol_loss(loss);

    // =========================================================================
    // Phase 5: Snapshot post-absorb state and compute post-residual.
    // =========================================================================
    let post_vault = engine.vault.get();
    let post_c_tot = engine.c_tot.get();
    let post_ins = engine.insurance_fund.balance.get();

    // Verify the insurance decreased by exactly `loss` (pay == loss since ins >= loss).
    let expected_post_ins = pre_ins - loss;
    assert_eq!(
        post_ins,
        expected_post_ins,
        "insurance must decrease by loss={} after absorb_protocol_loss; \
         pre_ins={} post_ins={}",
        loss,
        pre_ins,
        post_ins
    );

    // Verify vault was NOT changed by absorb_protocol_loss / use_insurance_buffer.
    // This is a factual check of what the code does (lines 4811-4821 never touch vault).
    assert_eq!(
        post_vault,
        pre_vault,
        "vault must be unchanged by absorb_protocol_loss (no vault debit in helper); \
         pre_vault={} post_vault={}",
        pre_vault,
        post_vault
    );

    // Verify c_tot was NOT changed.
    assert_eq!(
        post_c_tot,
        pre_c_tot,
        "c_tot must be unchanged by absorb_protocol_loss; \
         pre_c_tot={} post_c_tot={}",
        pre_c_tot,
        post_c_tot
    );

    let post_senior = post_c_tot
        .checked_add(post_ins)
        .expect("post-senior must not overflow");
    let post_residual = post_vault - post_senior;

    // =========================================================================
    // Phase 6: The conservation assertion.
    //
    // The hypothesis claims: pre_residual == post_residual.
    //
    // Actual behaviour (from source lines 4811-4821, 4844-4852):
    //   Δinsurance = -pay = -loss = -10_000
    //   Δvault     = 0
    //   Δresiudal  = -Δinsurance = +10_000 != 0
    //
    // Therefore this assertion is expected to FAIL, confirming the violation:
    //   the residual INCREASES by `loss` (= `pay`) whenever absorb_protocol_loss
    //   absorbs a loss from the insurance buffer without a matching vault debit.
    //
    // If it unexpectedly PASSES (pre == post), that would mean the implementation
    // was changed to also debit vault, and the finding would not hold.
    // =========================================================================
    assert_eq!(
        pre_residual,
        post_residual,
        "VIOLATION CONFIRMED: residual is not conserved across absorb_protocol_loss. \
         pre_residual={} post_residual={} delta=+{} \
         (insurance shrank by {} but vault was not debited; \
         source: percolator.rs lines 4811-4821, 4844-4852)",
        pre_residual,
        post_residual,
        post_residual.saturating_sub(pre_residual),
        loss
    );

    // =========================================================================
    // Phase 7: Confirm check_conservation() still passes post-absorb.
    //
    // Even though the residual grew (a conservation-of-residual violation),
    // the weaker invariant vault >= c_tot + insurance still holds because
    // insurance shrank while vault stayed the same — so vault is now FURTHER
    // above the senior claim.
    //
    // This confirms the engine does NOT panic or corrupt state on the call,
    // the violation is purely an accounting semantics violation: the residual
    // (the "free" portion of the vault above all senior claims) silently grows
    // when insurance absorbs a loss, meaning that value "appears from nowhere"
    // in the residual bucket.
    // =========================================================================
    assert!(
        engine.check_conservation(),
        "vault >= c_tot + insurance must still hold after absorb_protocol_loss \
         (weak conservation is preserved even though residual grew)"
    );
}
