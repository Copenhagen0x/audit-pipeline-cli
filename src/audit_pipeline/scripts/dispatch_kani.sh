#!/usr/bin/env bash
# dispatch_kani.sh — push a Kani harness to the VPS, run it in tmux, return.
#
# Usage:
#   bash scripts/dispatch_kani.sh <vps-host> <ssh-key> <harness-name>
#   bash scripts/dispatch_kani.sh <vps-host> <ssh-key> --baseline
#
# Examples:
#   bash scripts/dispatch_kani.sh root@1.2.3.4 ~/.ssh/audit_vps proof_finalize_preserves_conservation
#   bash scripts/dispatch_kani.sh root@1.2.3.4 ~/.ssh/audit_vps --baseline
#
# Prerequisites:
#   - VPS provisioned via provision_vps.sh
#   - Engine + wrapper code synced to /tmp/audit/{engine,wrapper}/ on VPS
#   - Kani harness file present in /tmp/audit/engine/tests/
#
# What this does:
#   1. Verifies the harness compiles (cargo check)
#   2. Spawns a tmux session for the Kani run
#   3. Returns immediately (Kani may take minutes to hours)
#
# Check status with:
#   ssh -i <key> <host> "tmux ls"
#   ssh -i <key> <host> "tail -f /tmp/audit/results/kani_<harness>.log"

set -euo pipefail

VPS_HOST="${1:?Usage: dispatch_kani.sh <vps-host> <ssh-key> <harness>}"
SSH_KEY="${2:?Usage: dispatch_kani.sh <vps-host> <ssh-key> <harness>}"
HARNESS="${3:?Usage: dispatch_kani.sh <vps-host> <ssh-key> <harness>}"

SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no"

if [[ "$HARNESS" == "--baseline" ]]; then
    echo "==> Dispatching FULL baseline (cargo kani --tests --features test) on $VPS_HOST"
    SESSION_NAME="kani_baseline"
    LOG_PATH="/tmp/audit/results/kani_baseline.log"
    KANI_CMD="cargo kani --tests --features test"
else
    echo "==> Dispatching Kani harness '$HARNESS' on $VPS_HOST"
    SESSION_NAME="kani_$HARNESS"
    LOG_PATH="/tmp/audit/results/kani_$HARNESS.log"
    KANI_CMD="cargo kani --tests --features test --harness $HARNESS"
fi

# Pre-flight: confirm test target compiles (cheap, prevents wasted Kani time)
echo "  Pre-flight: cargo check --tests --features test"
$SSH "$VPS_HOST" 'bash -lc' <<EOF
set -e
source ~/.cargo/env
cd /tmp/audit/engine
cargo check --tests --features test 2>&1 | tail -5
EOF

# Check for existing tmux session with this name
EXISTING=$($SSH "$VPS_HOST" "tmux has-session -t $SESSION_NAME 2>&1" || true)
if [[ "$EXISTING" != *"can't find session"* ]] && [[ -n "$EXISTING" ]]; then
    echo "  WARNING: tmux session '$SESSION_NAME' already exists. Killing it first."
    $SSH "$VPS_HOST" "tmux kill-session -t $SESSION_NAME"
fi

# Spawn the Kani run in tmux
echo "  Spawning tmux session: $SESSION_NAME"
echo "  Output: $LOG_PATH"
$SSH "$VPS_HOST" "bash -lc 'tmux new-session -d -s $SESSION_NAME \"cd /tmp/audit/engine && source ~/.cargo/env && $KANI_CMD 2>&1 | tee $LOG_PATH\"'"

# Confirm it spawned
sleep 2
$SSH "$VPS_HOST" "tmux ls 2>&1 | grep $SESSION_NAME" || {
    echo "  ERROR: tmux session did not start. Check VPS state."
    exit 1
}

echo
echo "==> Done. Kani is running in the background."
echo "    Session:  $SESSION_NAME"
echo "    Log:      $LOG_PATH"
echo
echo "    Check progress:"
echo "      ssh -i $SSH_KEY $VPS_HOST 'tail -f $LOG_PATH'"
echo
echo "    Or summary status (after some time):"
echo "      bash scripts/check_kani_status.sh $VPS_HOST $SSH_KEY $HARNESS"
