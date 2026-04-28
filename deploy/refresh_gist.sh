#!/usr/bin/env bash
# Pull fresh STATUS.md from the VPS and update the public Gist.
# Run from your laptop until VPS-side cron is set up.
#
# Setup:
#   - Make sure `gh` is installed and authenticated locally with `gist` scope
#   - Make sure ~/.ssh/percolator_vps exists and is the right key
#   - Run:  bash deploy/refresh_gist.sh
#
# To automate on Windows: create a Task Scheduler task that runs this hourly
# via Git Bash:  C:\Program Files\Git\bin\bash.exe -c "/c/Users/btrco/...refresh_gist.sh"

set -euo pipefail

SSH_KEY="${SSH_KEY:-$HOME/.ssh/percolator_vps}"
VPS="${VPS:-root@193.24.234.91}"
WORKSPACE="${WORKSPACE:-/root/audit_runs/percolator-live}"
GIST_ID="${GIST_ID:-c3181bdc906e599522adc2030bfa698e}"

LOCAL_TMP="$(mktemp -t sentinel-status.XXXXXX.md)"
trap 'rm -f "$LOCAL_TMP"' EXIT

# 1. Regenerate STATUS.md on the VPS from current state
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VPS" \
    "python3 /root/audit-pipeline-cli/deploy/generate_status.py $WORKSPACE /tmp/STATUS.md" \
    >/dev/null

# 2. Pull it locally
scp -i "$SSH_KEY" -o StrictHostKeyChecking=no -q "$VPS:/tmp/STATUS.md" "$LOCAL_TMP"

# 3. Update the Gist
gh gist edit "$GIST_ID" "$LOCAL_TMP" -f STATUS.md

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Gist $GIST_ID updated from $VPS"
