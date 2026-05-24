#!/usr/bin/env bash
# jelleo-autoupdate.sh — pull origin/main + redeploy if HEAD moved.
#
# Runs from jelleo-autoupdate.timer every 5 min on the VPS. Self-contained:
# every change pushed to GitHub main lands on the VPS within ~5 min with
# zero operator intervention. No more "did I redeploy?" friction.
#
# Safety:
#   * flock prevents two timer fires from racing
#   * Only fast-forward pulls allowed — never rebases or merges
#   * pip install + systemd restart only run if git HEAD actually moved
#   * Every fail-path is non-fatal (returns 0) so the timer keeps firing;
#     a transient network blip never wedges the auto-deploy loop
#   * All output appended to /root/audit_runs/percolator-live/auto-update.log
#
# Manual force-fire:  systemctl start jelleo-autoupdate.service
# Disable:            systemctl disable --now jelleo-autoupdate.timer

set -uo pipefail

REPO="${JELLEO_REPO:-/root/audit-pipeline-cli}"
WORKSPACE="${JELLEO_WORKSPACE:-/root/audit_runs/percolator-live}"
LOG="${JELLEO_AUTOUPDATE_LOG:-$WORKSPACE/auto-update.log}"
LOCK="/var/lock/jelleo-autoupdate.lock"

# Daemons that need a restart when code lands. Timers self-fire from
# new code on next tick; they don't need restart.
RESTART_UNITS=(
    "jelleo-watch.service"
    "jelleo-shadow.service"
    "jelleo-sse.service"
)

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" | tee -a "$LOG"
}

# Acquire lock — exit silently if another instance is already running.
exec 200>"$LOCK"
if ! flock -n 200; then
    exit 0
fi

if [[ ! -d "$REPO/.git" ]]; then
    log "ERROR: $REPO is not a git checkout — skipping update"
    exit 0
fi

cd "$REPO" || { log "ERROR: cannot cd $REPO"; exit 0; }

# Refuse to update if there are local uncommitted changes — would block
# the fast-forward pull and risk losing operator edits.
if ! git diff --quiet HEAD 2>/dev/null || ! git diff --quiet --cached 2>/dev/null; then
    log "WARN: local uncommitted changes in $REPO — skipping auto-update"
    exit 0
fi

# Fetch latest
if ! git fetch origin main 2>>"$LOG"; then
    # Network blip etc. — try again next tick.
    exit 0
fi

LOCAL=$(git rev-parse HEAD 2>/dev/null || echo "?")
REMOTE=$(git rev-parse origin/main 2>/dev/null || echo "?")

if [[ "$LOCAL" == "$REMOTE" || "$REMOTE" == "?" ]]; then
    # Nothing to do — exit silently to avoid log spam.
    exit 0
fi

log "===== auto-update available ====="
log "  local:  $LOCAL"
log "  remote: $REMOTE"

# Show the commits we're about to apply for the log trail
git log --oneline "$LOCAL..$REMOTE" 2>/dev/null | head -20 | while read line; do
    log "  + $line"
done

# Fast-forward pull only (no merges, no rebases)
if ! git pull --ff-only origin main >>"$LOG" 2>&1; then
    log "ERROR: git pull --ff-only failed — likely diverged branch; skipping"
    exit 0
fi

NEW_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "?")
log "  pulled to $NEW_HEAD"

# ── SUPPLY-CHAIN GATE (audit finding 5c4e072e / 500607134) ────────────────
# Verify the new HEAD is signed by an allowed key BEFORE installing it.
# Without this, a GitHub account compromise = root code execution on the VPS
# within 5 minutes (autoupdate cadence).
#
# Setup (one-time, on each signing machine):
#   1. Generate a signing key:  ssh-keygen -t ed25519 -f ~/.ssh/jelleo-signing
#   2. Tell git to use SSH signatures:
#        git config --global gpg.format ssh
#        git config --global user.signingkey ~/.ssh/jelleo-signing.pub
#        git config --global commit.gpgsign true
#   3. Add the PUBLIC half to ~/.ssh/jelleo-allowed-signers in this format:
#        <email-of-author> ssh-ed25519 AAAA...
#      and point git at it:
#        git config --global gpg.ssh.allowedSignersFile ~/.ssh/jelleo-allowed-signers
#   4. On the VPS, replicate the allowed_signers file (READ-ONLY for root)
#      and point git at it via the same config above.
#
# If verification fails: rollback to the pre-pull HEAD and SKIP this update
# (exit 0 — same pattern as a network blip). The next push WITHOUT a
# verified signature will be REJECTED indefinitely until the operator
# either (a) signs and re-pushes, or (b) sets JELLEO_ALLOW_UNSIGNED=1 to
# temporarily disable the gate (NOT recommended; logged loudly).
if [[ "${JELLEO_ALLOW_UNSIGNED:-0}" == "1" ]]; then
    log "  WARN: JELLEO_ALLOW_UNSIGNED=1 — supply-chain gate DISABLED for this tick"
elif ! git verify-commit HEAD >>"$LOG" 2>&1; then
    log "BLOCKED: HEAD signature verification failed for $NEW_HEAD"
    log "  → rolling back to $LOCAL (no install, no restart)"
    log "  → set JELLEO_ALLOW_UNSIGNED=1 to bypass (logs WARN every tick)"
    if ! git reset --hard "$LOCAL" >>"$LOG" 2>&1; then
        log "  ERROR: rollback failed — repo may be in dirty state, operator action required"
    fi
    # Also sync submodules to the rolled-back pointer. `git reset --hard` only
    # updates the .gitmodules POINTER, not the submodule working tree — leaving
    # the submodule with rejected code still checked out. (Code-reviewer flag.)
    if ! git submodule update --init --recursive >>"$LOG" 2>&1; then
        log "  WARN: submodule sync after rollback non-zero (may be OK if no submodules)"
    fi
    # Exit non-zero on signature failure so systemd / journalctl shows this as
    # a distinct security event rather than a clean success. The unit declares
    # SuccessExitStatus=3 so the timer keeps firing without alerting on every
    # blocked-unsigned tick (operator sets up signing once, then it goes back
    # to silent "no commits to apply"). All OTHER failure paths still exit 0
    # (network blips etc.) — only signature failure uses 3. (Code-reviewer flag.)
    exit 3
else
    log "  signature verified for $NEW_HEAD"
fi
# ──────────────────────────────────────────────────────────────────────────

# Reinstall Python package — picks up any new modules, new CLI commands,
# and pinned dep changes.
# Use --no-user when running under systemd to avoid /root/.local/ path-hijack
# of security-critical imports (audit finding R2-3 chain). Combined with
# PYTHONNOUSERSITE=1 in the service unit, this forces a clean site-packages
# install with no shadow modules.
if PIP_NO_USER=1 pip install -e . >>"$LOG" 2>&1; then
    log "  pip install: ok"
else
    log "  WARN: pip install non-zero (continuing — may be a no-op rebuild)"
fi

# Re-install systemd units — idempotent. Picks up any new unit files
# (e.g. jelleo-alert-failure@.service) without manual operator steps.
if bash "$REPO/deploy/install_systemd.sh" >>"$LOG" 2>&1; then
    log "  install_systemd.sh: ok"
else
    log "  WARN: install_systemd.sh non-zero (continuing)"
fi

# Restart long-running daemons so they pick up the new code.
for unit in "${RESTART_UNITS[@]}"; do
    if systemctl is-active --quiet "$unit"; then
        if systemctl restart "$unit" >>"$LOG" 2>&1; then
            log "  restarted $unit"
        else
            log "  WARN: restart $unit failed"
        fi
    else
        log "  $unit not active — leaving alone"
    fi
done

log "===== auto-update complete: $NEW_HEAD ====="

# Emit a deploy event into the active cycle's hunt.log.jsonl (if any)
# so subscribed customer dashboards see "engine_updated" in real time.
LATEST_CYCLE_LOG=""
if [[ -d "$WORKSPACE/hunts" ]]; then
    LATEST_CYCLE_LOG=$(ls -1t "$WORKSPACE/hunts"/*/hunt.log.jsonl 2>/dev/null | head -1 || true)
fi
if [[ -n "$LATEST_CYCLE_LOG" && -w "$LATEST_CYCLE_LOG" ]]; then
    printf '{"event":"engine_updated","ts":%s,"sha":"%s","previous_sha":"%s"}\n' \
        "$(date +%s)" "$NEW_HEAD" "$LOCAL" >>"$LATEST_CYCLE_LOG" 2>/dev/null || true
fi

exit 0
