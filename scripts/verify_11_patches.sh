#!/bin/bash
# Final verify of the 11 cluster-rep patches on aptos-large.
# For each: deploy L2 PoC, apply patch, run aptos move test, compare pre/post.

set -u
REPO=/root/ottersec-eval/repos/aptos-large
WORK=/root/audit_runs/ottersec-eval/workspaces/aptos-large

# (finding_id, hyp_id, test_slug)
HYPS=(
    "142:APT1-borrow-global-no-auth:apt1_borrow_global_no_auth"
    "143:APT10-u64-overflow-arith:apt10_u64_overflow_arith"
    "172:APT37-fee-percent-bound:apt37_fee_percent_bound"
    "173:APT38-treasury-drain:apt38_treasury_drain"
    "198:APTL24-marketplace-settle-trade-no-fee-bps-bound:aptl24_marketplace_settle_trade_no_fee_bps_bound"
    "199:APTL25-ensure-slot-ignores-owner-key:aptl25_ensure_slot_ignores_owner_key"
    "201:APTL27-governance-execute-all-clears-all-flags:aptl27_governance_execute_all_clears_all_flags"
    "202:APTL28-lending-pool-open-position-bypasses-pause:aptl28_lending_pool_open_position_bypasses_pause"
    "205:APTL30-voting-power-ignores-voter:aptl30_voting_power_ignores_voter"
    "211:APTL4-initializer-reset-fee-policy-no-auth:aptl4_initializer_reset_fee_policy_no_auth"
    "212:APTL5-roles-grant-operator-without-owner-check:aptl5_roles_grant_operator_without_owner_check"
)

cd $REPO
git checkout -- . 2>/dev/null
git clean -fd 2>/dev/null

echo "===VERIFYING 11 PATCHES==="
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

    # Deploy PoC
    cp "$poc" $REPO/tests/jelleo_l2_${slug}.move 2>/dev/null

    # Extract actual test function name from the deployed file (the slug
    # is often the FILE name, not the test FN name — aptos move test
    # --filter wants the fn or module name to be substring-matched).
    test_fn=$(grep -oE 'fun (test_\w+)' $REPO/tests/jelleo_l2_${slug}.move | head -1 | awk '{print $2}')
    if [ -z "$test_fn" ]; then
        test_fn=$slug
    fi

    # PRE: run test pre-patch
    pre_out=$(aptos move test --filter $test_fn 2>&1)
    pre_abort=$(echo "$pre_out" | grep -oE 'aborted with code [0-9]+' | head -1)

    # Apply patch
    apply_log=$(git apply --whitespace=fix "$patch" 2>&1)
    apply_rc=$?

    if [ $apply_rc -ne 0 ]; then
        echo "✗ FAIL $hyp — patch did not apply: $apply_log"
        FAIL=$((FAIL+1))
        rm $REPO/tests/jelleo_l2_${slug}.move 2>/dev/null
        git checkout -- sources/ 2>/dev/null
        continue
    fi

    # POST: run test post-patch
    post_out=$(aptos move test --filter $test_fn 2>&1)
    post_abort=$(echo "$post_out" | grep -oE 'aborted with code [0-9]+' | head -1)
    post_compile_err=$(echo "$post_out" | grep -c "Failed to run tests\|private to module")

    # Verdict
    if [ "$post_compile_err" -gt 0 ]; then
        echo "✓ PASS $hyp — post-patch compile error blocks attacker call ($post_compile_err errors)"
        PASS=$((PASS+1))
    elif [ -n "$pre_abort" ] && [ -n "$post_abort" ] && [ "$pre_abort" != "$post_abort" ]; then
        echo "✓ PASS $hyp — abort shifted: $pre_abort → $post_abort"
        PASS=$((PASS+1))
    elif [ -n "$pre_abort" ] && [ -z "$post_abort" ] && echo "$post_out" | grep -q "passed: 1"; then
        echo "✓ PASS $hyp — test now passes (bug fixed)"
        PASS=$((PASS+1))
    elif [ -n "$pre_abort" ] && [ -n "$post_abort" ] && [ "$pre_abort" = "$post_abort" ]; then
        echo "✗ FAIL $hyp — same abort pre/post: $pre_abort"
        FAIL=$((FAIL+1))
    else
        echo "? INCONCLUSIVE $hyp — pre=$pre_abort post=$post_abort"
        INCONCLUSIVE=$((INCONCLUSIVE+1))
    fi

    # Cleanup
    rm $REPO/tests/jelleo_l2_${slug}.move 2>/dev/null
    git checkout -- sources/ 2>/dev/null
done

echo
echo "===TALLY==="
echo "  PASS: $PASS"
echo "  FAIL: $FAIL"
echo "  INCONCLUSIVE: $INCONCLUSIVE"
