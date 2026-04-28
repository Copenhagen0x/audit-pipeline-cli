#!/usr/bin/env bash
# Install Sentinel systemd units, stop existing tmux sessions,
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
cp "$DEPLOY_DIR/sentinel-shadow.service" "$UNIT_DIR/"
cp "$DEPLOY_DIR/sentinel-watch.service" "$UNIT_DIR/"
[[ -f "$DEPLOY_DIR/sentinel-health.service" ]] && cp "$DEPLOY_DIR/sentinel-health.service" "$UNIT_DIR/"
[[ -f "$DEPLOY_DIR/sentinel-health.timer"   ]] && cp "$DEPLOY_DIR/sentinel-health.timer"   "$UNIT_DIR/"
[[ -f "$DEPLOY_DIR/sentinel-backup.service" ]] && cp "$DEPLOY_DIR/sentinel-backup.service" "$UNIT_DIR/"
[[ -f "$DEPLOY_DIR/sentinel-backup.timer"   ]] && cp "$DEPLOY_DIR/sentinel-backup.timer"   "$UNIT_DIR/"
chmod +x "$DEPLOY_DIR/backup_findings_db.sh"

echo "=== Stopping legacy tmux sessions (if any) ==="
tmux kill-session -t sentinel-shadow 2>/dev/null && echo "  killed sentinel-shadow tmux" || echo "  no sentinel-shadow tmux"
tmux kill-session -t sentinel-watch 2>/dev/null  && echo "  killed sentinel-watch tmux"  || echo "  no sentinel-watch tmux"

echo "=== Enabling + (re)starting units ==="
systemctl daemon-reload
systemctl enable sentinel-shadow.service
systemctl enable sentinel-watch.service
systemctl restart sentinel-shadow.service
systemctl restart sentinel-watch.service

if [[ -f "$UNIT_DIR/sentinel-health.timer" ]]; then
    systemctl enable sentinel-health.timer
    systemctl restart sentinel-health.timer
fi
if [[ -f "$UNIT_DIR/sentinel-backup.timer" ]]; then
    systemctl enable sentinel-backup.timer
    systemctl restart sentinel-backup.timer
fi

sleep 3

echo
echo "=== sentinel-shadow status ==="
systemctl status sentinel-shadow.service --no-pager -l | head -15

echo
echo "=== sentinel-watch status ==="
systemctl status sentinel-watch.service --no-pager -l | head -15

echo
echo "=== Done. Both services will auto-restart on failure and survive reboots. ==="
echo "Tail shadow:  journalctl -fu sentinel-shadow"
echo "Tail watch:   journalctl -fu sentinel-watch"
echo "Tail hunts:   tail -f /root/audit_runs/percolator-live/watch/hunt-on-update.log"
