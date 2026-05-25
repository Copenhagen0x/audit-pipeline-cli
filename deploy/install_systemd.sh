#!/usr/bin/env bash
# Install Jelleo systemd units, stop existing tmux sessions,
# enable + start the units. Idempotent. Run as root on the VPS.
#
# Replaces the tmux-based deployment (which dies on reboot) with
# proper systemd services that auto-restart on failure and survive
# reboots.

set -euo pipefail

# P13 R2 (all 3 reviewers): explicit bash 4+ requirement. `declare -A`
# below is bash 4+ only; bash 3.x (macOS system default, ancient CentOS)
# would crash with a cryptic "declare: -A: invalid option" mid-script.
# Production VPS is Ubuntu 22.04 (bash 5.1) so this is a clarity guard,
# not a functional fix — but the explicit error helps any operator who
# tries to dry-run on a dev box.
if (( BASH_VERSINFO[0] < 4 )); then
    echo "ERROR: install_systemd.sh requires bash 4+ (got $BASH_VERSION)" >&2
    exit 1
fi

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
# Patch #13 R1 (all 3 reviewers CRITICAL): capture pre-cp hashes of
# all destination unit files BEFORE the cp block so the
# `_changed_or_inactive` check below can compare against the OLD
# content (pre-overwrite). The R0 patch did `cmp -s "$src" "$dst"`
# AFTER cp, which always returned identical (cp had just overwritten
# $dst with $src) — the change-detection branch was dead code and
# the function reduced to `! is-active`. A real unit-file change
# would NEVER trigger a restart of an active service.
declare -A _PRE_CP_HASH
_snapshot_pre_cp_hash() {
    local unit="$1"
    local dst="$UNIT_DIR/$unit"
    if [[ -f "$dst" ]]; then
        _PRE_CP_HASH["$unit"]=$(sha256sum "$dst" 2>/dev/null | awk '{print $1}')
    else
        _PRE_CP_HASH["$unit"]=""  # absent → treat as "changed" later
    fi
}
for _u in jelleo-shadow.service jelleo-watch.service \
          jelleo-alert-failure@.service jelleo-token-auth.service \
          jelleo-health.service jelleo-health.timer \
          jelleo-backup.service jelleo-backup.timer \
          jelleo-scheduler-24h.service jelleo-scheduler-24h.timer \
          jelleo-scheduler-weekly.service jelleo-scheduler-weekly.timer \
          jelleo-scheduler-monthly.service jelleo-scheduler-monthly.timer \
          jelleo-snapshot.service jelleo-snapshot.timer \
          jelleo-heartbeat.service jelleo-heartbeat.timer \
          jelleo-corpus-refresh.service jelleo-corpus-refresh.timer \
          jelleo-autoupdate.service jelleo-autoupdate.timer; do
    _snapshot_pre_cp_hash "$_u"
done

# Core daemons (shadow + watch)
cp "$DEPLOY_DIR/jelleo-shadow.service" "$UNIT_DIR/"
cp "$DEPLOY_DIR/jelleo-watch.service"  "$UNIT_DIR/"
# Failure alert handler (referenced via OnFailure= on the core daemons)
[[ -f "$DEPLOY_DIR/jelleo-alert-failure@.service" ]] && \
    cp "$DEPLOY_DIR/jelleo-alert-failure@.service" "$UNIT_DIR/" && \
    echo "  installed jelleo-alert-failure@.service (OnFailure handler)"
# HMAC token verification sidecar for customer manifest gating. Stops
# /customer/<id>/manifest.json from being readable on customer_id
# obscurity alone. Wired into nginx via deploy/nginx-customer-auth-snippet.conf
# (operator-opt-in — requires nginx reload after editing config).
[[ -f "$DEPLOY_DIR/jelleo-token-auth.service" ]] && \
    cp "$DEPLOY_DIR/jelleo-token-auth.service" "$UNIT_DIR/" && \
    echo "  installed jelleo-token-auth.service (HMAC customer URL gate)"
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
# Round-4 fix (devils-advocate ROUND-3 MED #4): create the persistent
# state dir for jelleo-autoupdate's dirty sentinel. The systemd unit's
# ExecStartPre=-/bin/mkdir also covers this at runtime, but creating it
# here means a fresh install has the dir + correct ownership before the
# FIRST timer fire.
# Round-6 fix (devils-advocate ROUND-5 MED #6): refuse install if
# /var/lib/jelleo is a SYMLINK (attacker pre-placed it pointing to a
# controlled target). `mkdir -p` is a no-op on existing dirs INCLUDING
# symlinks, and the sentinel writes would follow the symlink target.
if [[ -L /var/lib/jelleo ]]; then
    echo "ERROR: /var/lib/jelleo exists as a symlink — refusing to install" >&2
    echo "       (attacker pre-placement attack surface)" >&2
    echo "       remove manually and re-run: rm /var/lib/jelleo" >&2
    exit 1
fi
mkdir -p /var/lib/jelleo
chmod 0700 /var/lib/jelleo  # operator-only access; contains sentinel forensic data

# Ensure cryptography is installed (Sprint 3 sign module needs it). pip
# install is a no-op if already present.
#
# PIP_NO_USER=1 (audit finding R2-3): never install to /root/.local/lib/.../site-packages.
# That path is the Python-path-hijack vector — a malicious shim placed there
# is loaded before system site-packages and intercepts every sign() / verify() call
# in any Python service running as root. System install is the safer surface
# (still vulnerable to system-package compromise, but smaller blast radius and
# easier to audit).
echo "=== Ensuring cryptography is installed ==="
PIP_NO_USER=1 /root/.local/bin/python3 -m pip install cryptography 2>/dev/null || \
    PIP_NO_USER=1 python3 -m pip install cryptography || \
    echo "  (could not install cryptography automatically — run 'PIP_NO_USER=1 pip install cryptography' manually)"

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

# Patch #13 (audit HIGH 46ca2392): idempotency-safe restart. The
# previous unconditional `systemctl restart` interrupted running
# daemons every install — even when the unit file hadn't changed.
# Helper: restart only when the unit content ACTUALLY changed (vs
# the pre-cp snapshot captured at the top of this script), OR when
# the service isn't currently active. Reduces midnight cron-driven
# outages where install_systemd.sh re-runs as part of an autoupdate
# cycle and momentarily kills working units.
#
# R1 (all 3 reviewers CRITICAL): R0 compared $src to $dst AFTER cp
# had already overwritten $dst with $src — the cmp was tautologically
# identical. Restart-on-change branch was dead code. R1 uses the
# pre-cp hash snapshot.
_changed_or_inactive() {
    local unit="$1"
    local dst="$UNIT_DIR/$unit"
    local pre_hash="${_PRE_CP_HASH[$unit]:-}"
    local post_hash=""
    if [[ -f "$dst" ]]; then
        post_hash=$(sha256sum "$dst" 2>/dev/null | awk '{print $1}')
    fi
    # Content changed (pre != post, including absent → present) →
    # restart. Otherwise check active state.
    if [[ "$pre_hash" != "$post_hash" ]]; then
        return 0  # do restart
    fi
    if systemctl is-active --quiet "$unit"; then
        return 1  # unchanged + active → skip
    fi
    return 0  # unchanged but inactive → restart (recovery)
}

# Wrapper applied uniformly to all unit restarts so the
# idempotency guarantee is consistent across shadow, watch,
# token-auth, and every timer (R1 — goober + threat-modeler HIGH).
_maybe_restart() {
    local unit="$1"
    if _changed_or_inactive "$unit"; then
        systemctl restart "$unit"
        echo "  restarted $unit"
    else
        echo "  $unit unchanged + active — skipped restart"
    fi
}

echo "=== Enabling + (re)starting units ==="
systemctl daemon-reload
systemctl enable jelleo-shadow.service
systemctl enable jelleo-watch.service
_maybe_restart jelleo-shadow.service
_maybe_restart jelleo-watch.service
# HMAC token-auth sidecar (loopback :8766). Listens for nginx auth_request
# subrequests. Safe to enable always — does nothing until nginx is wired
# via deploy/nginx-customer-auth-snippet.conf.
if [[ -f "$UNIT_DIR/jelleo-token-auth.service" ]]; then
    systemctl enable jelleo-token-auth.service
    _maybe_restart jelleo-token-auth.service
fi

if [[ -f "$UNIT_DIR/jelleo-health.timer" ]]; then
    systemctl enable jelleo-health.timer
    _maybe_restart jelleo-health.timer
fi
if [[ -f "$UNIT_DIR/jelleo-backup.timer" ]]; then
    systemctl enable jelleo-backup.timer
    _maybe_restart jelleo-backup.timer
fi

# Sprint 3 + Tier 5 + P2 + auto-update timers
for t in jelleo-scheduler-24h jelleo-scheduler-weekly jelleo-scheduler-monthly jelleo-snapshot jelleo-heartbeat jelleo-corpus-refresh jelleo-autoupdate; do
    if [[ -f "$UNIT_DIR/${t}.timer" ]]; then
        systemctl enable "${t}.timer"
        _maybe_restart "${t}.timer"
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
