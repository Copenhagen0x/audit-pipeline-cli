#!/usr/bin/env bash
# Install Jelleo systemd units, stop existing tmux sessions,
# enable + start the units. Idempotent. Run as root on the VPS.
#
# Replaces the tmux-based deployment (which dies on reboot) with
# proper systemd services that auto-restart on failure and survive
# reboots.

set -euo pipefail

UNIT_DIR=/etc/systemd/system
DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ ! -f /root/.audit-env ]]; then
    echo "ERROR: /root/.audit-env missing. Create it first with ANTHROPIC_API_KEY."
    exit 1
fi

if [[ ! -x /root/.local/bin/audit-pipeline ]]; then
    echo "ERROR: /root/.local/bin/audit-pipeline missing. Install audit-pipeline first."
    exit 1
fi

echo "=== Installing systemd units ==="
cp "$DEPLOY_DIR/jelleo-shadow.service" "$UNIT_DIR/"
cp "$DEPLOY_DIR/jelleo-watch.service" "$UNIT_DIR/"
[[ -f "$DEPLOY_DIR/jelleo-health.service" ]] && cp "$DEPLOY_DIR/jelleo-health.service" "$UNIT_DIR/"
[[ -f "$DEPLOY_DIR/jelleo-health.timer"   ]] && cp "$DEPLOY_DIR/jelleo-health.timer"   "$UNIT_DIR/"
[[ -f "$DEPLOY_DIR/jelleo-backup.service" ]] && cp "$DEPLOY_DIR/jelleo-backup.service" "$UNIT_DIR/"
[[ -f "$DEPLOY_DIR/jelleo-backup.timer"   ]] && cp "$DEPLOY_DIR/jelleo-backup.timer"   "$UNIT_DIR/"
chmod +x "$DEPLOY_DIR/backup_findings_db.sh"

echo "=== Stopping legacy tmux sessions (if any) ==="
tmux kill-session -t jelleo-shadow 2>/dev/null && echo "  killed jelleo-shadow tmux" || echo "  no jelleo-shadow tmux"
tmux kill-session -t jelleo-watch 2>/dev/null  && echo "  killed jelleo-watch tmux"  || echo "  no jelleo-watch tmux"

echo "=== Enabling + (re)starting units ==="
systemctl daemon-reload
systemctl enable jelleo-shadow.service
systemctl enable jelleo-watch.service
systemctl restart jelleo-shadow.service
systemctl restart jelleo-watch.service

if [[ -f "$UNIT_DIR/jelleo-health.timer" ]]; then
    systemctl enable jelleo-health.timer
    systemctl restart jelleo-health.timer
fi
if [[ -f "$UNIT_DIR/jelleo-backup.timer" ]]; then
    systemctl enable jelleo-backup.timer
    systemctl restart jelleo-backup.timer
fi

sleep 3

echo
echo "=== jelleo-shadow status ==="
systemctl status jelleo-shadow.service --no-pager -l | head -15

echo
echo "=== jelleo-watch status ==="
systemctl status jelleo-watch.service --no-pager -l | head -15

echo
echo "=== Done. Both services will auto-restart on failure and survive reboots. ==="
echo "Tail shadow:  journalctl -fu jelleo-shadow"
echo "Tail watch:   journalctl -fu jelleo-watch"
echo "Tail hunts:   tail -f /root/audit_runs/percolator-live/watch/hunt-on-update.log"
