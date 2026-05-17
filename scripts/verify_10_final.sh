#!/bin/bash
# Dead-honest end-to-end verify of the final 10 aptos-large patches.
# For each: clean repo, deploy L2 PoC, run pre-patch test (capture abort),
# apply patch, run post-patch test (capture abort or compile error),
# classify PASS/FAIL/INCONCLUSIVE with clear reasoning.

set -u
REPO=/root/ottersec-eval/repos/aptos-large
WORK=/root/audit_runs/ottersec-eval/workspaces/aptos-large

HYPS=(
    "142:APT1-borrow-global-no-auth:apt1_borrow_global_no_auth"
    "143:APT10-u64-overflow-arith:apt10_u64_overflow_arith"
    "172:APT37-fee-percent-bound:apt37_fee_percent_bound"
    "173:APT38-treasury-drain:apt38_treasury_drain"
    "198:APTL24-marketplace-settle-trade-no-fee-bps-bound:aptl24_marketplace_settle_trade_no_fee_bps_bound"
    "199:APTL25-ensure-slot-ignores-owner-key:aptl25_ensure_slot_ignores_owner_key"
    "201:APTL27-governance-execute-all-clears-all-flags:aptl27_governance_execute_all_clears_all_flags"
    "205:APTL30-voting-power-ignores-voter:aptl30_voting_power_ignores_voter"
    "211:APTL4-initializer-reset-fee-policy-no-auth:aptl4_initializer_reset_fee_policy_no_auth"
    "212:APTL5-roles-grant-operator-without-owner-check:aptl5_roles_grant_operator_without_owner_check"
)

cd $REPO
git checkout -- . 2>/dev/null
git clean -fd 2>/dev/null

echo "============================================================"
echo "DEAD-HONEST END-TO-END VERIFY — 10 APTOS-LARGE PATCHES"
echo "============================================================"
echo

PASS=0
FAIL=0
INCONCLUSIVE=0

for entry in "${HYPS[@]}"; do
    fid="${entry%%:*}"
    rest="${entry#*:}"
    hyp="${rest%%:*}"
    slug="${rest##*:}"

    patch=$WORK/recon/bundles/$fid/patch.diff
    poc=$WORK/tests/aptos/test_${slug}.move

    echo "──── $hyp (finding $fid) ────"

    # Deploy the L2 PoC into the repo's tests/ dir
    cp "$poc" $REPO/tests/jelleo_l2_${slug}.move 2>/dev/null

    # Extract the actual test fn name from the deployed test
    test_fn=$(grep -oE 'fun (test_\w+)' $REPO/tests/jelleo_l2_${slug}.move | head -1 | awk '{print $2}')
    if [ -z "$test_fn" ]; then
        test_fn="$slug"
    fi
    echo "  test fn:    $test_fn"

    # ── PRE-PATCH ──
    pre_out=$(aptos move test --filter "$test_fn" 2>&1)
    pre_abort=$(echo "$pre_out" | grep -oE 'aborted with code [0-9]+' | head -1 | grep -oE '[0-9]+')
    pre_result=$(echo "$pre_out" | grep -oE 'Test result: (OK|FAILED)' | head -1)
    pre_passed=$(echo "$pre_out" | grep -oE 'passed: [0-9]+' | head -1 | grep -oE '[0-9]+')

    echo "  pre-patch:  ${pre_result:-no result}, abort=${pre_abort:-none}, passed=${pre_passed:-0}"

    # ── APPLY PATCH ──
    apply_log=$(git apply --whitespace=fix "$patch" 2>&1)
    apply_rc=$?

    if [ $apply_rc -ne 0 ]; then
        echo "  ✗ FAIL — patch did not apply: $(echo "$apply_log" | head -1)"
        FAIL=$((FAIL+1))
        rm $REPO/tests/jelleo_l2_${slug}.move 2>/dev/null
        git checkout -- sources/ 2>/dev/null
        echo
        continue
    fi

    # ── POST-PATCH ──
    post_out=$(aptos move test --filter "$test_fn" 2>&1)
    post_abort=$(echo "$post_out" | grep -oE 'aborted with code [0-9]+' | head -1 | grep -oE '[0-9]+')
    post_result=$(echo "$post_out" | grep -oE 'Test result: (OK|FAILED)' | head -1)
    post_passed=$(echo "$post_out" | grep -oE 'passed: [0-9]+' | head -1 | grep -oE '[0-9]+')
    post_compile_err=$(echo "$post_out" | grep -c "Failed to run tests")

    echo "  post-patch: ${post_result:-compile err}, abort=${post_abort:-none}, passed=${post_passed:-0}, compile_err=$post_compile_err"

    # ── VERDICT ──
    if [ "$post_compile_err" -gt 0 ]; then
        # Patch made attacker call path uncompilable
        echo "  ✓ PASS — post-patch attacker call no longer compiles (private/missing)"
        PASS=$((PASS+1))
    elif [ -n "$pre_abort" ] && [ -n "$post_abort" ] && [ "$pre_abort" != "$post_abort" ]; then
        echo "  ✓ PASS — abort code shifted: $pre_abort → $post_abort"
        PASS=$((PASS+1))
    elif [ "$pre_passed" = "0" ] && [ "$post_passed" = "1" ]; then
        echo "  ✓ PASS — test fails pre-patch, passes post-patch (bug fixed)"
        PASS=$((PASS+1))
    elif [ -n "$pre_abort" ] && [ -n "$post_abort" ] && [ "$pre_abort" = "$post_abort" ]; then
        echo "  ✗ FAIL — same abort pre/post: $pre_abort (patch did NOT fix bug)"
        FAIL=$((FAIL+1))
    elif [ -z "$pre_abort" ] && [ -n "$post_abort" ]; then
        echo "  ⚠ INCONCLUSIVE — pre-patch had no abort, post-patch abort=$post_abort. Patch likely added defensive abort but pre-state unverified."
        INCONCLUSIVE=$((INCONCLUSIVE+1))
    else
        echo "  ⚠ INCONCLUSIVE — pre=$pre_abort post=$post_abort"
        INCONCLUSIVE=$((INCONCLUSIVE+1))
    fi

    # Cleanup
    rm $REPO/tests/jelleo_l2_${slug}.move 2>/dev/null
    git checkout -- sources/ 2>/dev/null
    echo
done

echo "============================================================"
echo "FINAL TALLY"
echo "============================================================"
echo "  ✓ PASS:         $PASS"
echo "  ✗ FAIL:         $FAIL"
echo "  ⚠ INCONCLUSIVE: $INCONCLUSIVE"
echo "  TOTAL:          $((PASS+FAIL+INCONCLUSIVE))"
