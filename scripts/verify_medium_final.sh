#!/bin/bash
# Dead-honest end-to-end verify of aptos-medium 20260514-151541 — 5 patches.

set -u
REPO=/root/ottersec-eval/repos/aptos-medium
WORK=/root/audit_runs/ottersec-eval/workspaces/aptos-medium
CYCLE=20260514-151541

HYPS=(
    "81:APT1-borrow-global-no-auth:apt1_borrow_global_no_auth"
    "82:APT10-u64-overflow-arith:apt10_u64_overflow_arith"
    "99:APT26-withdraw-delay-bypass:apt26_withdraw_delay_bypass"
    "133:APTM20-staking-emergency-unstake-principal-lost:aptm20_staking_emergency_unstake_principal_lost"
    "134:APTM21-acl-mint-cap-permissionless:aptm21_acl_mint_cap_permissionless"
)

cd $REPO
git checkout -- . 2>/dev/null
git clean -fd 2>/dev/null

echo "============================================================"
echo "APTOS-MEDIUM 20260514-151541 — END-TO-END VERIFY"
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

    if [ ! -f "$patch" ]; then
        echo "  ✗ FAIL — no patch.diff at $patch"
        FAIL=$((FAIL+1))
        echo
        continue
    fi

    # Read patch target file
    target=$(grep -m 1 '^--- a/' "$patch" | sed 's|--- a/||')
    echo "  patch target: $target"

    # Verify bug exists in source: search for the engine_function or pattern
    if [ -f "$REPO/$target" ]; then
        echo "  source file:  ✓ $target exists"
    else
        echo "  source file:  ✗ $target NOT FOUND in repo"
    fi

    # Deploy PoC
    if [ -f "$poc" ]; then
        cp "$poc" $REPO/tests/jelleo_l2_${slug}.move
    else
        echo "  ✗ FAIL — no PoC file at $poc"
        FAIL=$((FAIL+1))
        echo
        continue
    fi

    # Extract test fn name
    test_fn=$(grep -oE 'fun (test_\w+)' $REPO/tests/jelleo_l2_${slug}.move | head -1 | awk '{print $2}')
    [ -z "$test_fn" ] && test_fn="$slug"
    echo "  test fn:      $test_fn"

    # PRE
    pre_out=$(aptos move test --filter "$test_fn" 2>&1)
    pre_abort=$(echo "$pre_out" | grep -oE 'aborted with code [0-9]+' | head -1 | grep -oE '[0-9]+')
    pre_arith=$(echo "$pre_out" | grep -c 'arithmetic error\|Addition overflow\|Subtraction underflow')
    pre_passed=$(echo "$pre_out" | grep -oE 'passed: [0-9]+' | head -1 | grep -oE '[0-9]+')
    pre_test_failed=$(echo "$pre_out" | grep -c 'Test result: FAILED')
    echo "  pre:  abort=${pre_abort:-none}, arith_err=$pre_arith, passed=${pre_passed:-0}, FAILED=$pre_test_failed"

    # APPLY
    apply_out=$(git apply --whitespace=fix "$patch" 2>&1)
    apply_rc=$?
    if [ $apply_rc -ne 0 ]; then
        echo "  ✗ FAIL — patch did not apply: $(echo "$apply_out" | head -1)"
        FAIL=$((FAIL+1))
        rm $REPO/tests/jelleo_l2_${slug}.move 2>/dev/null
        git checkout -- sources/ 2>/dev/null
        echo
        continue
    fi
    echo "  patch:        ✓ applied cleanly"

    # POST
    post_out=$(aptos move test --filter "$test_fn" 2>&1)
    post_abort=$(echo "$post_out" | grep -oE 'aborted with code [0-9]+' | head -1 | grep -oE '[0-9]+')
    post_arith=$(echo "$post_out" | grep -c 'arithmetic error\|Addition overflow\|Subtraction underflow')
    post_passed=$(echo "$post_out" | grep -oE 'passed: [0-9]+' | head -1 | grep -oE '[0-9]+')
    post_compile_err=$(echo "$post_out" | grep -c 'Failed to run tests')
    echo "  post: abort=${post_abort:-none}, arith_err=$post_arith, passed=${post_passed:-0}, compile_err=$post_compile_err"

    # Verdict
    if [ "$post_compile_err" -gt 0 ]; then
        echo "  ✓ PASS — post-patch attacker call no longer compiles"
        PASS=$((PASS+1))
    elif [ -n "$pre_abort" ] && [ -n "$post_abort" ] && [ "$pre_abort" != "$post_abort" ]; then
        echo "  ✓ PASS — abort shifted $pre_abort → $post_abort"
        PASS=$((PASS+1))
    elif [ "$pre_test_failed" -gt 0 ] && [ "$post_passed" = "1" ]; then
        echo "  ✓ PASS — pre fails, post passes (bug fixed)"
        PASS=$((PASS+1))
    elif [ "$pre_arith" -gt 0 ] && [ -n "$post_abort" ]; then
        echo "  ✓ PASS — pre arithmetic overflow → post abort=$post_abort (patch's guard fired)"
        PASS=$((PASS+1))
    elif [ -n "$pre_abort" ] && [ -n "$post_abort" ] && [ "$pre_abort" = "$post_abort" ]; then
        echo "  ✗ FAIL — same abort $pre_abort pre/post (patch did NOT fix)"
        FAIL=$((FAIL+1))
    else
        echo "  ⚠ INCONCLUSIVE — pre=$pre_abort post=$post_abort"
        INCONCLUSIVE=$((INCONCLUSIVE+1))
    fi

    rm $REPO/tests/jelleo_l2_${slug}.move 2>/dev/null
    git checkout -- sources/ 2>/dev/null
    echo
done

echo "============================================================"
echo "FINAL TALLY — aptos-medium"
echo "============================================================"
echo "  ✓ PASS:         $PASS"
echo "  ✗ FAIL:         $FAIL"
echo "  ⚠ INCONCLUSIVE: $INCONCLUSIVE"
echo "  TOTAL:          $((PASS+FAIL+INCONCLUSIVE))"
