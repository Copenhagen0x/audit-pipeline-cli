#!/usr/bin/env bash
# Restart the jelleo-watch tmux session with --on-update wired to
# trigger `audit-pipeline hunt` on every new commit detected.
#
# Run on the VPS as root after dropping ANTHROPIC_API_KEY into
# /root/.audit-env.

set -euo pipefail

WORKSPACE="${WORKSPACE:-/root/audit_runs/percolator-live}"
ENV_FILE="${ENV_FILE:-/root/.audit-env}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE missing — create it with ANTHROPIC_API_KEY first."
    echo
    echo "Example (run as root):"
    echo "  umask 077"
    echo "  cat > $ENV_FILE <<EOF"
    echo '  export ANTHROPIC_API_KEY="sk-ant-..."'
    echo "EOF"
    echo "  chmod 600 $ENV_FILE"
    exit 1
fi

# Sanity: source it and verify the key is set
# shellcheck disable=SC1090
source "$ENV_FILE"
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "ERROR: ANTHROPIC_API_KEY missing in $ENV_FILE"
    exit 1
fi

# Kill the existing jelleo-watch session (preserve jelleo-shadow)
tmux kill-session -t jelleo-watch 2>/dev/null || true

# The on-update command runs from $WORKSPACE on every detected commit.
# It sources the env file inside its own shell so the API key is
# available, then runs hunt with sane defaults.
ON_UPDATE_CMD='source /root/.audit-env && audit-pipeline --workspace '"$WORKSPACE"' hunt --skip-poc 2>&1 | tee -a '"$WORKSPACE"'/watch/hunt-on-update.log'

# Restart watch with the hunt trigger wired in.
tmux new-session -d -s jelleo-watch \
    "source $ENV_FILE && audit-pipeline --workspace $WORKSPACE watch \
        --auto-pull \
        --update-pin \
        --interval 300 \
        --on-update '$ON_UPDATE_CMD' \
        2>&1 | tee -a $WORKSPACE/watch/daemon.log"

sleep 2
echo "=== restarted ==="
tmux ls
echo
echo "Watch is now wired to fire 'audit-pipeline hunt' on every new commit."
echo "Every detected push to engine or wrapper -> full hunt cycle on the VPS."
echo
echo "Tail the action: ssh ...; tail -f $WORKSPACE/watch/daemon.log"
echo "Hunt logs:        ssh ...; tail -f $WORKSPACE/watch/hunt-on-update.log"
