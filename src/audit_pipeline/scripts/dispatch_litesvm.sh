#!/usr/bin/env bash
# dispatch_litesvm.sh — run a LiteSVM BPF reachability/bound test.
#
# Usage:
#   bash scripts/dispatch_litesvm.sh <vps-host> <ssh-key> <test-name> [wrapper-dir] [features]
#
# Args:
#   vps-host:     user@host (or "-" to run locally without SSH)
#   ssh-key:      path to SSH key (or "-" to run locally)
#   test-name:    cargo test name (e.g. test_my_finding_bound_analysis)
#   wrapper-dir:  Optional. Path to wrapper Cargo.toml dir. Default
#                 /tmp/audit/wrapper. For our deployment, pass
#                 /root/audit_runs/percolator-live/target/wrapper.
#   features:     Optional cargo features (e.g. "small")
#
# Examples:
#   bash scripts/dispatch_litesvm.sh root@1.2.3.4 ~/.ssh/key test_my_lit /root/audit_runs/percolator-live/target/wrapper
#   bash scripts/dispatch_litesvm.sh - - test_my_lit /root/audit_runs/percolator-live/target/wrapper    # local

set -euo pipefail

VPS_HOST="${1:?Usage: dispatch_litesvm.sh <vps-host> <ssh-key> <test-name> [wrapper-dir] [features]}"
SSH_KEY="${2:?Usage: dispatch_litesvm.sh <vps-host> <ssh-key> <test-name> [wrapper-dir] [features]}"
TEST_NAME="${3:?Usage: dispatch_litesvm.sh <vps-host> <ssh-key> <test-name> [wrapper-dir] [features]}"
WRAPPER_DIR="${4:-/tmp/audit/wrapper}"
FEATURES="${5:-}"

RESULTS_DIR="$(dirname "$WRAPPER_DIR")/results"

if [[ "$VPS_HOST" == "-" || "$SSH_KEY" == "-" ]]; then
    SSH_EXEC=("bash" "-lc")
else
    SSH_EXEC=("ssh" "-i" "$SSH_KEY" "-o" "StrictHostKeyChecking=no" "$VPS_HOST")
fi

run_remote() {
    "${SSH_EXEC[@]}" "$@"
}

LOG_PATH="$RESULTS_DIR/litesvm_$TEST_NAME.log"
run_remote "mkdir -p $RESULTS_DIR"

echo "==> Running LiteSVM test '$TEST_NAME' on $VPS_HOST (wrapper=$WRAPPER_DIR)"
echo "    Log: $LOG_PATH"

if [[ -n "$FEATURES" ]]; then
    CARGO_CMD="cargo test --features '$FEATURES' --test $TEST_NAME -- --nocapture --test-threads=1"
else
    CARGO_CMD="cargo test --test $TEST_NAME -- --nocapture --test-threads=1"
fi

run_remote "set -e; source ~/.cargo/env 2>/dev/null || true; cd $WRAPPER_DIR && $CARGO_CMD 2>&1 | tee $LOG_PATH"
