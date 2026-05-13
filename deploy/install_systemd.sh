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

# Cross-cutting audit Defect 22 (HIGH operational): /root/.audit-env holds
# ANTHROPIC_API_KEY + SMTP creds. World-readable perms would leak them to
# any user the VPS later adds (e.g. a CI bot user). Force 0600 + owner=root.
# Idempotent — run on every install.
chmod 600 /root/.audit-env
chown root:root /root/.audit-env
echo "  hardened /root/.audit-env to 0600 root:root"

if ! command -v audit-pipeline >/dev/null 2>&1; then
    echo "ERROR: audit-pipeline missing from PATH. Install audit-pipeline first."
    echo "  (looked in: $PATH)"
    exit 1
fi

echo "=== Installing systemd units ==="
# Core daemons (shadow + watch)
cp "$DEPLOY_DIR/jelleo-shadow.service" "$UNIT_DIR/"
cp "$DEPLOY_DIR/jelleo-watch.service"  "$UNIT_DIR/"
# Failure alert handler (referenced via OnFailure= on the core daemons)
[[ -f "$DEPLOY_DIR/jelleo-alert-failure@.service" ]] && \
    cp "$DEPLOY_DIR/jelleo-alert-failure@.service" "$UNIT_DIR/" && \
    echo "  installed jelleo-alert-failure@.service (OnFailure handler)"
# Operational (health + backup)
[[ -f "$DEPLOY_DIR/jelleo-health.service" ]] && cp "$DEPLOY_DIR/jelleo-health.service" "$UNIT_DIR/"
[[ -f "$DEPLOY_DIR/jelleo-health.timer"   ]] && cp "$DEPLOY_DIR/jelleo-health.timer"   "$UNIT_DIR/"
[[ -f "$DEPLOY_DIR/jelleo-backup.service" ]] && cp "$DEPLOY_DIR/jelleo-backup.service" "$UNIT_DIR/"
[[ -f "$DEPLOY_DIR/jelleo-backup.timer"   ]] && cp "$DEPLOY_DIR/jelleo-backup.timer"   "$UNIT_DIR/"
chmod +x "$DEPLOY_DIR/backup_findings_db.sh"
# Sprint 3: cadence scheduler + dashboard snapshot
# Tier 5 #29: hourly proof-of-running heartbeat (jelleo-heartbeat)
# P2 #B6: daily corpus refresh (jelleo-corpus-refresh)
# 2026-05-12: jelleo-autoupdate polls origin/main every 5 min so future
#             pushes deploy automatically — no more "did I redeploy?"
for u in jelleo-scheduler-24h jelleo-scheduler-weekly jelleo-scheduler-monthly jelleo-snapshot jelleo-heartbeat jelleo-corpus-refresh jelleo-autoupdate; do
    [[ -f "$DEPLOY_DIR/${u}.service" ]] && cp "$DEPLOY_DIR/${u}.service" "$UNIT_DIR/"
    [[ -f "$DEPLOY_DIR/${u}.timer"   ]] && cp "$DEPLOY_DIR/${u}.timer"   "$UNIT_DIR/"
done
chmod +x "$DEPLOY_DIR/refresh_corpus.sh" 2>/dev/null || true
chmod +x "$DEPLOY_DIR/jelleo-autoupdate.sh" 2>/dev/null || true

# Workspace dirs the new units need (idempotent — won't error if exist)
mkdir -p /root/audit_runs/percolator-live/scheduler
mkdir -p /root/audit_runs/percolator-live/keys
mkdir -p /root/audit_runs/percolator-live/reports

# Ensure cryptography is installed (Sprint 3 sign module needs it). pip
# install is a no-op if already present.
echo "=== Ensuring cryptography is installed ==="
/root/.local/bin/python3 -m pip install --user cryptography 2>/dev/null || \
    python3 -m pip install --user cryptography || \
    echo "  (could not install cryptography automatically — run 'pip install --user cryptography' manually)"

# Generate the signing keypair on first run only — refuses to overwrite.
if [[ ! -f /root/audit_runs/percolator-live/keys/jelleo.ed25519 ]]; then
    echo "=== Generating Ed25519 signing keypair ==="
    audit-pipeline --workspace /root/audit_runs/percolator-live sign keygen || \
        echo "  WARN: keygen failed — re-run manually after fixing"
fi

# Publish the public key under the website docroot if it exists.
WWW_DIR="${JELLEO_WWW_DIR:-/var/www/jelleo.com}"
if [[ -d "$WWW_DIR" ]] && [[ -f /root/audit_runs/percolator-live/keys/jelleo.ed25519.pub ]]; then
    mkdir -p "$WWW_DIR/keys"
    install -m 0644 /root/audit_runs/percolator-live/keys/jelleo.ed25519.pub "$WWW_DIR/keys/jelleo.ed25519.pub"
    echo "  published public key to $WWW_DIR/keys/jelleo.ed25519.pub"
fi

# Logrotate config — keeps shadow/watch/hunt logs from growing unbounded.
if [[ -f "$DEPLOY_DIR/logrotate-jelleo" ]] && [[ -d /etc/logrotate.d ]]; then
    install -m 0644 "$DEPLOY_DIR/logrotate-jelleo" /etc/logrotate.d/jelleo
    echo "  installed /etc/logrotate.d/jelleo"
fi

# notifier.json scaffold — install only if absent (don't clobber real recipients)
if [[ ! -f /root/audit_runs/percolator-live/notifier.json ]]; then
    [[ -f "$DEPLOY_DIR/notifier.example.json" ]] && \
        install -m 0640 "$DEPLOY_DIR/notifier.example.json" /root/audit_runs/percolator-live/notifier.json && \
        echo "  copied notifier.example.json -> notifier.json (EDIT IT before scheduler tick)"
fi

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

# Sprint 3 + Tier 5 + P2 + auto-update timers
for t in jelleo-scheduler-24h jelleo-scheduler-weekly jelleo-scheduler-monthly jelleo-snapshot jelleo-heartbeat jelleo-corpus-refresh jelleo-autoupdate; do
    if [[ -f "$UNIT_DIR/${t}.timer" ]]; then
        systemctl enable "${t}.timer"
        systemctl restart "${t}.timer"
        echo "  enabled ${t}.timer"
    fi
done

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
