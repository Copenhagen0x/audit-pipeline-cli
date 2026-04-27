#!/usr/bin/env bash
# dispatch_litesvm.sh — run a LiteSVM test on the VPS for cross-platform reproduction.
#
# Usage:
#   bash scripts/dispatch_litesvm.sh <vps-host> <ssh-key> <test-name> [--features <feat>]
#
# Examples:
#   bash scripts/dispatch_litesvm.sh root@1.2.3.4 ~/.ssh/audit_vps test_v6_cursor_wrap_consumption_reset
#   bash scripts/dispatch_litesvm.sh root@1.2.3.4 ~/.ssh/audit_vps test_v6_cursor_wrap_consumption_reset --features small
#
# Prerequisites:
#   - VPS provisioned via provision_vps.sh
#   - Wrapper code at /tmp/audit/wrapper/ on VPS
#   - BPF artifact built (target/deploy/<program>.so) — script will rebuild if missing

set -euo pipefail

VPS_HOST="${1:?Usage: dispatch_litesvm.sh <vps-host> <ssh-key> <test> [--features <feat>]}"
SSH_KEY="${2:?Usage: dispatch_litesvm.sh <vps-host> <ssh-key> <test> [--features <feat>]}"
TEST_NAME="${3:?Usage: dispatch_litesvm.sh <vps-host> <ssh-key> <test> [--features <feat>]}"
FEATURES="${4:-}"

SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no"

echo "==> Running LiteSVM test '$TEST_NAME' on $VPS_HOST"
[[ -n "$FEATURES" ]] && echo "    Feature flags: $FEATURES"

# Build BPF artifact if missing OR if features changed
echo "  Ensuring BPF artifact present..."
$SSH "$VPS_HOST" "bash -lc '
    cd /tmp/audit/wrapper
    source ~/.cargo/env
    if [[ ! -f target/deploy/*.so ]] || [[ \"$FEATURES\" != \"\" ]]; then
        echo \"  Building BPF artifact: cargo build-sbf $FEATURES\"
        cargo build-sbf $FEATURES 2>&1 | tail -10
    else
        echo \"  BPF artifact present\"
    fi
'"

# Run the test, stream output, capture for cross-platform compare
LOG_PATH="/tmp/audit/results/litesvm_$TEST_NAME.log"
echo "  Running test, output → $LOG_PATH"
$SSH "$VPS_HOST" "bash -lc '
    cd /tmp/audit/wrapper
    source ~/.cargo/env
    cargo test --test $TEST_NAME $FEATURES -- --nocapture 2>&1 | tee $LOG_PATH
'" || true

# Summary
echo
echo "==> Test complete. Result summary:"
$SSH "$VPS_HOST" "grep -E '^test result|test .* (ok|FAILED)' $LOG_PATH | tail -10 | sed 's/^/    /'"

echo
echo "    Full log: $LOG_PATH (on VPS)"
echo "    Pull locally: scp -i $SSH_KEY $VPS_HOST:$LOG_PATH ./local_litesvm_$TEST_NAME.log"
