#!/usr/bin/env bash
# cross_platform_compare.sh — diff a test's output between local and VPS.
#
# Usage:
#   bash scripts/cross_platform_compare.sh <vps-host> <ssh-key> <repo-path> <test-name> [--features <feat>]
#
# Examples:
#   bash scripts/cross_platform_compare.sh root@1.2.3.4 ~/.ssh/audit_vps \
#       /path/to/wrapper test_v6_cursor_wrap_consumption_reset
#
# What this does:
#   1. Runs the test locally; captures output to /tmp/local_<test>.log
#   2. Runs the test on VPS; captures output to /tmp/vps_<test>.log
#   3. Diffs the two outputs
#   4. Reports BIT-IDENTICAL or DIVERGENT

set -euo pipefail

VPS_HOST="${1:?Usage: cross_platform_compare.sh <vps-host> <ssh-key> <repo> <test> [--features <feat>]}"
SSH_KEY="${2:?Usage: cross_platform_compare.sh <vps-host> <ssh-key> <repo> <test> [--features <feat>]}"
REPO_PATH="${3:?Usage: cross_platform_compare.sh <vps-host> <ssh-key> <repo> <test> [--features <feat>]}"
TEST_NAME="${4:?Usage: cross_platform_compare.sh <vps-host> <ssh-key> <repo> <test> [--features <feat>]}"
FEATURES="${5:-}"

LOCAL_LOG="/tmp/local_$TEST_NAME.log"
VPS_LOG="/tmp/vps_$TEST_NAME.log"
DIFF_LOG="/tmp/diff_$TEST_NAME.txt"

echo "==> Cross-platform comparison: $TEST_NAME"
echo

# Local run
echo "  Step 1/3: running locally"
(cd "$REPO_PATH" && cargo test --test "$TEST_NAME" $FEATURES 2>&1) > "$LOCAL_LOG" || true
LOCAL_RESULT=$(grep -E '^test result' "$LOCAL_LOG" | head -1 || echo "no test result line")
echo "    Local:  $LOCAL_RESULT"

# VPS run
echo "  Step 2/3: running on VPS"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no"
$SSH "$VPS_HOST" "bash -lc '
    cd /tmp/audit/wrapper
    source ~/.cargo/env
    cargo test --test $TEST_NAME $FEATURES 2>&1
'" > "$VPS_LOG" || true
VPS_RESULT=$(grep -E '^test result' "$VPS_LOG" | head -1 || echo "no test result line")
echo "    VPS:    $VPS_RESULT"

# Compare
echo "  Step 3/3: comparing outputs"

# Extract just the test result lines (ignoring timestamps, paths, build noise)
LOCAL_VERDICT=$(grep -E 'test .* (ok|FAILED)' "$LOCAL_LOG" | sort)
VPS_VERDICT=$(grep -E 'test .* (ok|FAILED)' "$VPS_LOG" | sort)

if [[ "$LOCAL_VERDICT" == "$VPS_VERDICT" ]]; then
    echo
    echo "==> BIT-IDENTICAL pass/fail across both platforms ✓"
    echo "    Both report:"
    echo "$LOCAL_VERDICT" | sed 's/^/      /'
    rm -f "$DIFF_LOG"
else
    echo
    echo "==> DIVERGENT — local and VPS do not agree ✗"
    diff <(echo "$LOCAL_VERDICT") <(echo "$VPS_VERDICT") > "$DIFF_LOG" || true
    echo "    Diff saved to $DIFF_LOG"
    echo "    Local: $LOCAL_LOG"
    echo "    VPS:   $VPS_LOG"
    echo
    echo "    Investigate before disclosing — divergent results mean the"
    echo "    finding may be platform-specific, not a real protocol issue."
    exit 1
fi
