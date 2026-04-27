#!/usr/bin/env bash
# check_kani_status.sh — pull current state of a running Kani session.
#
# Usage:
#   bash scripts/check_kani_status.sh <vps-host> <ssh-key> <harness-name>
#   bash scripts/check_kani_status.sh <vps-host> <ssh-key> --baseline
#
# Returns:
#   - Whether the tmux session is still running
#   - Pass/fail tally so far (for baseline runs)
#   - Last 5 lines of log
#   - Final verdict if the run completed

set -euo pipefail

VPS_HOST="${1:?Usage: check_kani_status.sh <vps-host> <ssh-key> <harness>}"
SSH_KEY="${2:?Usage: check_kani_status.sh <vps-host> <ssh-key> <harness>}"
HARNESS="${3:?Usage: check_kani_status.sh <vps-host> <ssh-key> <harness>}"

SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no"

if [[ "$HARNESS" == "--baseline" ]]; then
    SESSION_NAME="kani_baseline"
    LOG_PATH="/tmp/audit/results/kani_baseline.log"
else
    SESSION_NAME="kani_$HARNESS"
    LOG_PATH="/tmp/audit/results/kani_$HARNESS.log"
fi

echo "=== Status: $SESSION_NAME ==="

# Tmux session state
SESSION_STATE=$($SSH "$VPS_HOST" "tmux has-session -t $SESSION_NAME 2>&1 && echo RUNNING || echo COMPLETED" 2>&1 || echo UNKNOWN)
echo "  Session: $SESSION_STATE"

# Tally (from log)
echo
echo "  Tally so far:"
$SSH "$VPS_HOST" "
    if [[ -f $LOG_PATH ]]; then
        echo '    Checked:    '\$(grep -c 'Checking harness' $LOG_PATH 2>/dev/null || echo 0)
        echo '    PASS:       '\$(grep -c 'VERIFICATION:- SUCCESSFUL' $LOG_PATH 2>/dev/null || echo 0)
        echo '    FAIL:       '\$(grep -c 'VERIFICATION:- FAILED' $LOG_PATH 2>/dev/null || echo 0)
        echo '    Log size:   '\$(du -h $LOG_PATH | cut -f1)
    else
        echo '    Log not yet present at $LOG_PATH'
    fi
"

# Last 5 lines
echo
echo "  Last log lines:"
$SSH "$VPS_HOST" "
    if [[ -f $LOG_PATH ]]; then
        tail -5 $LOG_PATH | sed 's/^/    /'
    fi
"

# Load
echo
echo "  VPS load:"
$SSH "$VPS_HOST" "uptime | sed 's/^/    /'"

# Final summary if completed
if [[ "$SESSION_STATE" == "COMPLETED" ]] && [[ -n "$($SSH $VPS_HOST 'test -f $LOG_PATH && echo yes' 2>&1)" ]]; then
    echo
    echo "  Final summary:"
    $SSH "$VPS_HOST" "grep -E 'Complete|Manual Harness Summary' $LOG_PATH | tail -3 | sed 's/^/    /'"
fi
