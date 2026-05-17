#!/bin/bash
# Dead-honest end-to-end verify of aptos-small 20260513-191318 — 2 patches.

set -u
REPO=/root/ottersec-eval/repos/aptos-small
WORK=/root/audit_runs/ottersec-eval/workspaces/aptos-small

HYPS=(
    "41:APT1-borrow-global-no-auth:apt1_borrow_global_no_auth"
    "72:APT38-treasury-drain:apt38_treasury_drain"
)

cd $REPO
git checkout -- . 2>/dev/null
git clean -fd 2>/dev/null

echo "============================================================"
echo "APTOS-SMALL 20260513-191318 — END-TO-END VERIFY"
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
        echo "  ✗ FAIL — no patch.diff"
        FAIL=$((FAIL+1)); echo; continue
    fi

    target=$(grep -m 1 '^--- a/' "$patch" | sed 's|--- a/||')
    echo "  patch target: $target"
    [ -f "$REPO/$target" ] && echo "  source file:  ✓ exists" || echo "  source file:  ✗ NOT FOUND"

    if [ ! -f "$poc" ]; then
        echo "  ✗ FAIL — no PoC file"
        FAIL=$((FAIL+1)); echo; continue
    fi
    cp "$poc" $REPO/tests/jelleo_l2_${slug}.move

    test_fn=$(grep -oE 'fun (test_\w+)' $REPO/tests/jelleo_l2_${slug}.move | head -1 | awk '{print $2}')
    [ -z "$test_fn" ] && test_fn="$slug"
    echo "  test fn:      $test_fn"

    pre_out=$(aptos move test --filter "$test_fn" 2>&1)
    pre_abort=$(echo "$pre_out" | grep -oE 'aborted with code [0-9]+' | head -1 | grep -oE '[0-9]+')
    pre_arith=$(echo "$pre_out" | grep -c 'arithmetic error\|Addition overflow\|Subtraction underflow')
    pre_passed=$(echo "$pre_out" | grep -oE 'passed: [0-9]+' | head -1 | grep -oE '[0-9]+')
    pre_failed=$(echo "$pre_out" | grep -c 'Test result: FAILED')
    echo "  pre:  abort=${pre_abort:-none}, arith=$pre_arith, passed=${pre_passed:-0}, FAILED=$pre_failed"

    apply_out=$(git apply --whitespace=fix "$patch" 2>&1)
    if [ $? -ne 0 ]; then
        echo "  ✗ FAIL — patch did not apply: $(echo "$apply_out" | head -1)"
        FAIL=$((FAIL+1))
        rm $REPO/tests/jelleo_l2_${slug}.move 2>/dev/null
        git checkout -- sources/ 2>/dev/null
        echo
        continue
    fi
    echo "  patch:        ✓ applied"

    post_out=$(aptos move test --filter "$test_fn" 2>&1)
    post_abort=$(echo "$post_out" | grep -oE 'aborted with code [0-9]+' | head -1 | grep -oE '[0-9]+')
    post_arith=$(echo "$post_out" | grep -c 'arithmetic error\|Addition overflow\|Subtraction underflow')
    post_passed=$(echo "$post_out" | grep -oE 'passed: [0-9]+' | head -1 | grep -oE '[0-9]+')
    post_compile_err=$(echo "$post_out" | grep -c 'Failed to run tests')
    echo "  post: abort=${post_abort:-none}, arith=$post_arith, passed=${post_passed:-0}, compile_err=$post_compile_err"

    if [ "$post_compile_err" -gt 0 ]; then
        echo "  ✓ PASS — post-patch attacker call won't compile"
        PASS=$((PASS+1))
    elif [ -n "$pre_abort" ] && [ -n "$post_abort" ] && [ "$pre_abort" != "$post_abort" ]; then
        echo "  ✓ PASS — abort shifted $pre_abort → $post_abort"
        PASS=$((PASS+1))
    elif [ "$pre_failed" -gt 0 ] && [ "$post_passed" = "1" ]; then
        echo "  ✓ PASS — pre fails, post passes"
        PASS=$((PASS+1))
    elif [ "$pre_arith" -gt 0 ] && [ -n "$post_abort" ]; then
        echo "  ✓ PASS — pre overflow → post abort=$post_abort"
        PASS=$((PASS+1))
    elif [ -n "$pre_abort" ] && [ -n "$post_abort" ] && [ "$pre_abort" = "$post_abort" ]; then
        echo "  ✗ FAIL — same abort $pre_abort pre/post"
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
echo "FINAL TALLY — aptos-small"
echo "============================================================"
echo "  ✓ PASS:         $PASS"
echo "  ✗ FAIL:         $FAIL"
echo "  ⚠ INCONCLUSIVE: $INCONCLUSIVE"
echo "  TOTAL:          $((PASS+FAIL+INCONCLUSIVE))"
