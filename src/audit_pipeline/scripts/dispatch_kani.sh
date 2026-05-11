#!/usr/bin/env bash
# dispatch_kani.sh — push a Kani harness to the VPS, run it in tmux, return.
#
# Usage:
#   bash scripts/dispatch_kani.sh <vps-host> <ssh-key> <harness-name> [engine-dir]
#   bash scripts/dispatch_kani.sh <vps-host> <ssh-key> --baseline       [engine-dir]
#
# Args:
#   vps-host:    user@host (or just "localhost" / 127.0.0.1 if running on VPS)
#   ssh-key:     path to SSH private key (or "-" to skip SSH and run locally)
#   harness:     Kani harness function name, or "--baseline" for the full suite
#   engine-dir:  Optional. Path on the target host where the engine's Cargo.toml
#                lives. Default: /tmp/audit/engine (legacy). Override to your
#                actual workspace, e.g. /root/audit_runs/percolator-live/target/engine
#
# Examples:
#   bash scripts/dispatch_kani.sh root@1.2.3.4 ~/.ssh/audit_vps my_harness /root/audit_runs/percolator-live/target/engine
#   bash scripts/dispatch_kani.sh - - my_harness /root/audit_runs/percolator-live/target/engine    # run locally
#
# Prerequisites:
#   - Target host has cargo + kani installed
#   - Kani harness file present at $ENGINE_DIR/tests/
#   - $ENGINE_DIR/Cargo.toml exists

set -euo pipefail

VPS_HOST="${1:?Usage: dispatch_kani.sh <vps-host> <ssh-key> <harness> [engine-dir]}"
SSH_KEY="${2:?Usage: dispatch_kani.sh <vps-host> <ssh-key> <harness> [engine-dir]}"
HARNESS="${3:?Usage: dispatch_kani.sh <vps-host> <ssh-key> <harness> [engine-dir]}"
ENGINE_DIR="${4:-/tmp/audit/engine}"
RESULTS_DIR="$(dirname "$ENGINE_DIR")/results"

# If host == "-" and key == "-", run commands locally (no SSH wrap)
if [[ "$VPS_HOST" == "-" || "$SSH_KEY" == "-" ]]; then
    SSH_EXEC=("bash" "-lc")
else
    SSH_EXEC=("ssh" "-i" "$SSH_KEY" "-o" "StrictHostKeyChecking=no" "$VPS_HOST")
fi

run_remote() {
    "${SSH_EXEC[@]}" "$@"
}

if [[ "$HARNESS" == "--baseline" ]]; then
    echo "==> Dispatching FULL baseline (cargo kani --tests --features test) on $VPS_HOST"
    SESSION_NAME="kani_baseline"
    LOG_PATH="$RESULTS_DIR/kani_baseline.log"
    KANI_CMD="cargo kani --tests --features test"
else
    echo "==> Dispatching Kani harness '$HARNESS' on $VPS_HOST (engine=$ENGINE_DIR)"
    SESSION_NAME="kani_$HARNESS"
    LOG_PATH="$RESULTS_DIR/kani_$HARNESS.log"
    KANI_CMD="cargo kani --tests --features test --harness $HARNESS"
fi

# Ensure results dir exists
run_remote "mkdir -p $RESULTS_DIR"

# Pre-flight: confirm test target compiles (cheap, prevents wasted Kani time)
echo "  Pre-flight: cargo check --tests --features test in $ENGINE_DIR"
run_remote "set -e; source ~/.cargo/env 2>/dev/null || true; cd $ENGINE_DIR && cargo check --tests --features test 2>&1 | tail -5"

# Check for existing tmux session with this name
EXISTING=$(run_remote "tmux has-session -t $SESSION_NAME 2>&1" || true)
if [[ "$EXISTING" != *"can't find session"* ]] && [[ -n "$EXISTING" ]]; then
    echo "  WARNING: tmux session '$SESSION_NAME' already exists. Killing it first."
    run_remote "tmux kill-session -t $SESSION_NAME"
fi

# Spawn the Kani run in tmux
echo "  Spawning tmux session: $SESSION_NAME"
echo "  Output: $LOG_PATH"
run_remote "tmux new-session -d -s $SESSION_NAME \"cd $ENGINE_DIR && source ~/.cargo/env && $KANI_CMD 2>&1 | tee $LOG_PATH\""

# Confirm it spawned
sleep 2
run_remote "tmux ls 2>&1 | grep $SESSION_NAME" || {
    echo "  ERROR: tmux session did not start. Check target host state."
    exit 1
}

echo
echo "==> Done. Kani is running in the background."
echo "    Session:  $SESSION_NAME"
echo "    Log:      $LOG_PATH"
echo
echo "    Check progress:"
echo "      tail -f $LOG_PATH    (on target host)"
