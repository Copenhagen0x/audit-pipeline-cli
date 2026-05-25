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

# Round-6 fix (devils-advocate ROUND-5 MED #4): require bash 4.4+ at the very
# top. The SIGTERM trap handler (`_jelleo_interrupt_rollback`) calls `exit 4`
# inside the handler. On bash 3.x / 4.0-4.3, signal-trap-exit can re-enter
# the trap on some architectures, causing duplicate rollback attempts. The
# rollback is idempotent (reset --hard twice = same outcome) so it's not a
# correctness disaster, but the duplicate sentinel writes obscure the log.
# bash 4.4+ has clean trap-exit semantics. Refuse to run on older versions.
if [[ "${BASH_VERSINFO[0]:-0}" -lt 4 ]] || \
   { [[ "${BASH_VERSINFO[0]:-0}" -eq 4 ]] && [[ "${BASH_VERSINFO[1]:-0}" -lt 4 ]]; }; then
    echo "ERROR: jelleo-autoupdate.sh requires bash 4.4+; found ${BASH_VERSION:-unknown}" >&2
    exit 4
fi

set -uo pipefail
# NOTE: intentional NOT -e. Every non-zero command path in this script is
# explicitly guarded by `if !`, `|| true`, or an explicit exit. `set -e` would
# trip on the deliberate non-zero returns (git fetch on network blip, pip
# install no-op rebuild, systemctl is-active probe). All security-critical
# paths (verify-commit, rollback, submodule sync) DO check non-zero and exit
# 3 on failure. Round-2 review acknowledged this is the correct design.

REPO="${JELLEO_REPO:-/root/audit-pipeline-cli}"
WORKSPACE="${JELLEO_WORKSPACE:-/root/audit_runs/percolator-live}"
LOG="${JELLEO_AUTOUPDATE_LOG:-$WORKSPACE/auto-update.log}"
LOCK="/var/lock/jelleo-autoupdate.lock"
# Round-2 fix (devils-advocate CRITICAL #3): rollback submodule sync MUST use
# hook-disabling flags; without them, the rollback IS the hook-execution
# vector. Same `GIT_SAFE` array as refresh_corpus.sh.
# R5b-3 (2026-05-24): protocol.ext.allow=never closes ext:: RCE.
GIT_SAFE=(-c core.hooksPath=/dev/null -c protocol.file.allow=never -c protocol.ext.allow=never)
# Round-2 fix (devils-advocate CRITICAL #2): pin allowedSignersFile at
# invocation time so an attacker who writes /root/.gitconfig can't redirect
# verify-commit to their own signers file. The file path is the operator's
# absolute path; the file itself is read-only for root (operator setup step).
ALLOWED_SIGNERS="${JELLEO_ALLOWED_SIGNERS:-/root/.ssh/jelleo-allowed-signers}"
# Round-2 fix (code-reviewer LOW #11): persistent sentinel for failed
# rollback. If the rollback bash-fails (rare, but possible if filesystem
# locked), subsequent ticks must keep alerting instead of going silent.
# Round-3 fix (devils-advocate ROUND-2 MEDIUM): default moved from /var/run
# (tmpfs, lost on reboot) to /var/lib/jelleo/ (persistent). The service
# unit shadows JELLEO_AUTOUPDATE_DIRTY so the file-path env var cannot be
# redirected via /root/.audit-env injection.
DIRTY_SENTINEL="${JELLEO_AUTOUPDATE_DIRTY:-/var/lib/jelleo/autoupdate-DIRTY}"
# Belt-and-suspenders mkdir. Round-4 fix (devils-advocate ROUND-3 MED #4):
# the systemd unit has `ExecStartPre=-/bin/mkdir -p /var/lib/jelleo` so
# the dir is guaranteed by the time this script runs. This mkdir covers
# the manual-invocation case (operator runs the script directly, not via
# systemctl). The previous version's comment claimed ExecStartPre but
# the unit didn't have one — now it actually does, so the claim is true.
mkdir -p "$(dirname "$DIRTY_SENTINEL")" 2>/dev/null || true

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

# Round-2 fix (code-reviewer LOW #11): if a prior rollback failed bash-side
# (leaving the working tree in a dirty unknown state), the sentinel file
# blocks all subsequent ticks until the operator manually clears it. Without
# this, one failed rollback = silent indefinite acceptance of attacker code.
if [[ -f "$DIRTY_SENTINEL" ]]; then
    log "BLOCKED: dirty-state sentinel exists at $DIRTY_SENTINEL"
    log "  → a prior rollback failed. Operator must inspect $REPO state and"
    log "  → remove the sentinel manually after restoring the repo:"
    log "  →   rm $DIRTY_SENTINEL"
    exit 3
fi

# Refuse to update if there are local uncommitted changes — would block
# the fast-forward pull and risk losing operator edits.
# Round-3 systemic fix: GIT_SAFE applied even to pre-fetch read ops for
# rule-consistency (`git-safe-on-all-ops` checklist rule). `git diff --quiet`
# doesn't trigger hooks, but applying GIT_SAFE keeps the lint clean and
# matches the surrounding pattern.
if ! git "${GIT_SAFE[@]}" diff --quiet HEAD 2>/dev/null || ! git "${GIT_SAFE[@]}" diff --quiet --cached 2>/dev/null; then
    log "WARN: local uncommitted changes in $REPO — skipping auto-update"
    exit 0
fi

# Fetch latest. Round-2 fix (devils-advocate HIGH #7): apply GIT_SAFE flags
# to BOTH fetch and pull. Without `core.hooksPath=/dev/null`, an attacker
# who pushes a commit that ships a malicious `.git/hooks/post-merge` script
# in the repo's hook dir would execute that script BEFORE the verify-commit
# gate runs.
if ! git "${GIT_SAFE[@]}" fetch origin main 2>>"$LOG"; then
    # Network blip etc. — try again next tick.
    exit 0
fi

# Round-4 fix (code-reviewer ROUND-3 HIGH #1): GIT_SAFE on rev-parse too.
# After fetch, the attacker's refs are in the object store. rev-parse can
# trigger external commands via gitconfig-driven helpers, so apply
# core.hooksPath=/dev/null + protocol.file.allow=never per the
# `git-safe-on-all-ops` checklist rule.
LOCAL=$(git "${GIT_SAFE[@]}" rev-parse HEAD 2>/dev/null || echo "?")
REMOTE=$(git "${GIT_SAFE[@]}" rev-parse origin/main 2>/dev/null || echo "?")

if [[ "$LOCAL" == "$REMOTE" || "$REMOTE" == "?" ]]; then
    # Nothing to do — exit silently to avoid log spam.
    exit 0
fi
# Round-5 fix (devils-advocate ROUND-4 MED #7): if LOCAL rev-parse failed
# (returned literal "?"), the trap handler later would call
# `git reset --hard "?"` which git would reject — leaving the post-pull
# working tree at NEW_HEAD with no recovery. Refuse before installing
# the trap. Also forces an alert (exit 3, distinct from silent exit 0).
if [[ "$LOCAL" == "?" ]]; then
    log "ERROR: pre-pull rev-parse HEAD failed (LOCAL='?'). Repo may be corrupt."
    log "  Refusing to proceed: trap handler depends on a valid LOCAL ref."
    log "  Operator action: inspect $REPO/.git state."
    # Round-6 fix (devils-advocate ROUND-5 HIGH #1): use exit 4 (NOT in
    # SuccessExitStatus) so OnFailure=jelleo-alert-failure@%n.service FIRES
    # for this extraordinary-state case. exit 3 was being silently absorbed
    # alongside expected signature-fail ticks, hiding genuine corruption.
    # exit 3 = "expected blocked state, don't alert per tick" (sig fail, sentinel)
    # exit 4 = "extraordinary state, operator must see this" (corrupt repo, interrupt-rollback)
    exit 4
fi

log "===== auto-update available ====="
log "  local:  $LOCAL"
log "  remote: $REMOTE"

# Show the commits we're about to apply for the log trail.
# Round-6 fix (devils-advocate ROUND-5 HIGH #4): GIT_SAFE applied to
# `git log` too. After fetch, the attacker's refs are in the object
# store; even a read op like `git log` can trigger external commands
# via core.pager or log.showSignature. ALL git invocations after fetch
# need GIT_SAFE, not just write ops.
git "${GIT_SAFE[@]}" log --oneline "$LOCAL..$REMOTE" 2>/dev/null | head -20 | while read line; do
    log "  + $line"
done

# Fast-forward pull only (no merges, no rebases). Round-2 fix: GIT_SAFE
# applied so post-merge / post-rewrite hooks can't execute on the freshly
# pulled commit BEFORE verify-commit gates it.
if ! git "${GIT_SAFE[@]}" pull --ff-only origin main >>"$LOG" 2>&1; then
    log "ERROR: git pull --ff-only failed — likely diverged branch; skipping"
    exit 0
fi

NEW_HEAD=$(git "${GIT_SAFE[@]}" rev-parse HEAD 2>/dev/null || echo "?")
log "  pulled to $NEW_HEAD"

# Round-4 fix (devils-advocate ROUND-3 HIGH #1): SIGTERM race trap.
# Between this point and the verify-commit gate, the working tree is at
# the (possibly unsigned) NEW_HEAD. If the process is killed mid-flight
# (operator `systemctl stop`, kernel OOM, reboot), the unsigned commit
# stays on disk + on the next tick LOCAL==REMOTE so the gate is skipped
# silently. Trap SIGTERM/SIGINT/SIGHUP between pull and verify-commit to
# force rollback before exit. Trap is CLEARED on the success path below
# so post-verify pip-install / restart aren't interrupted destructively.
_jelleo_interrupt_rollback() {
    log "INTERRUPT received post-pull pre-verify — rolling back to $LOCAL"
    if ! git "${GIT_SAFE[@]}" reset --hard "$LOCAL" >>"$LOG" 2>&1; then
        log "  ERROR: interrupt-rollback git reset failed; writing dirty sentinel"
        echo "interrupt-rollback failed at $(date -u +%FT%TZ) HEAD=$NEW_HEAD LOCAL=$LOCAL" \
            >> "$DIRTY_SENTINEL" 2>>"$LOG" || true
    fi
    if ! git "${GIT_SAFE[@]}" submodule update --init --recursive >>"$LOG" 2>&1; then
        log "  WARN: submodule sync after interrupt-rollback non-zero"
        # Round-5 fix (code-reviewer ROUND-4 LOW #3): mirror the verify-fail
        # sentinel write here. If submodules exist AND the sync failed during
        # interrupt-rollback, attacker code is checked out in the submodule
        # working tree. Sentinel forces operator attention on next tick.
        # `git submodule status` exits 0 + empty output if no submodules exist,
        # so the existence-guarded write is a no-op on the current submodule-
        # free repo while staying correct for any future submodule introduction.
        if git "${GIT_SAFE[@]}" submodule status 2>/dev/null | grep -q .; then
            echo "interrupt-submodule-sync failed at $(date -u +%FT%TZ) HEAD=$NEW_HEAD LOCAL=$LOCAL" \
                >> "$DIRTY_SENTINEL" 2>>"$LOG" || true
        fi
    fi
    # Round-6 fix (devils-advocate ROUND-5 HIGH #1): exit 4 (not 3) so this
    # path TRIGGERS OnFailure=jelleo-alert-failure@. systemctl stop or OOM
    # killing the autoupdate mid-pull IS something the operator should know
    # about — silent suppression would let dirty-tree state hide.
    exit 4
}
trap _jelleo_interrupt_rollback SIGTERM SIGINT SIGHUP

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
# (exit 3 — distinct security event; unit declares SuccessExitStatus=3 so
# the timer keeps firing without alerting on every blocked-unsigned tick).
# The next push WITHOUT a verified signature will be REJECTED indefinitely
# until the operator either (a) signs and re-pushes, or (b) sets
# JELLEO_ALLOW_UNSIGNED=1 to temporarily disable the gate (NOT recommended;
# logged loudly when a commit is pending).
# Round-2 fix (code-reviewer MED #8): comment said "(exit 0)" but code
# was `exit 3` — corrected.
if [[ "${JELLEO_ALLOW_UNSIGNED:-0}" == "1" ]]; then
    log "  WARN: JELLEO_ALLOW_UNSIGNED=1 — supply-chain gate DISABLED for this tick"
    trap - SIGTERM SIGINT SIGHUP  # clear trap — bypass path is intentional
# Round-2 fix (devils-advocate CRITICAL #2): pin allowedSignersFile at
# invocation time so a write to /root/.gitconfig can't redirect verify-commit
# to an attacker-controlled signers file. The path is the operator's
# read-only-for-root setup file.
# Round-4 fix (code-reviewer ROUND-3 invariant violation): GIT_SAFE applied
# here too. The `git-safe-on-all-ops` rule says ALL post-fetch git invocations
# need core.hooksPath=/dev/null. verify-commit COULD trigger the gpg.program
# hook if the commit's gpg signature header pointed to a malicious helper.
# Defense-in-depth + rule-consistency.
elif ! git "${GIT_SAFE[@]}" \
            -c gpg.format=ssh \
            -c "gpg.ssh.allowedSignersFile=$ALLOWED_SIGNERS" \
            verify-commit HEAD >>"$LOG" 2>&1; then
    log "BLOCKED: HEAD signature verification failed for $NEW_HEAD"
    log "  → allowed-signers file: $ALLOWED_SIGNERS"
    log "  → rolling back to $LOCAL (no install, no restart)"
    log "  → set JELLEO_ALLOW_UNSIGNED=1 to bypass (logs WARN every tick)"
    # Round-4 fix (devils-advocate ROUND-3 CRITICAL #1): apply GIT_SAFE to
    # `git reset --hard` too. `reset --hard` FIRES the post-checkout hook
    # (git treats it as a branch-level working-tree update). Without
    # core.hooksPath=/dev/null, an attacker who pushed a commit containing
    # a malicious .git/hooks/post-checkout file would have it execute as
    # root at the precise moment of defensive rollback. The previous
    # rounds fixed fetch/pull/submodule but missed this last invocation.
    if ! git "${GIT_SAFE[@]}" reset --hard "$LOCAL" >>"$LOG" 2>&1; then
        log "  ERROR: rollback failed — repo may be in dirty state, operator action required"
        # Round-2 fix (code-reviewer LOW #11): write persistent sentinel so
        # subsequent ticks BLOCK with a loud alert instead of going silent
        # once LOCAL/REMOTE converge.
        # Round-4 fix (devils-advocate ROUND-3 HIGH #3): use `>>` (append)
        # instead of `>` (overwrite) so forensic data is preserved when
        # multiple failure modes occur on the same tick (e.g., reset-fail
        # then submodule-fail). Without this, the second write LOSES the
        # first message, hiding the more critical reset failure.
        if ! echo "rollback failed at $(date -u +%FT%TZ) HEAD=$NEW_HEAD LOCAL=$LOCAL" \
                >> "$DIRTY_SENTINEL" 2>>"$LOG"; then
            log "  ERROR: could not write dirty sentinel $DIRTY_SENTINEL"
        fi
    fi
    # Also sync submodules to the rolled-back pointer. `git reset --hard` only
    # updates the .gitmodules POINTER, not the submodule working tree — leaving
    # the submodule with rejected code still checked out. (Code-reviewer flag.)
    # Round-2 fix (devils-advocate CRITICAL #3): GIT_SAFE applied — the
    # defensive rollback was previously the hook-execution vector. Without
    # core.hooksPath=/dev/null, malicious post-checkout / post-reset hooks
    # in the submodules of the just-rejected commit would execute as root.
    # NOTE (round-3 acknowledged gap): `git -c core.hooksPath=/dev/null
    # submodule update` does NOT propagate the -c flag to per-submodule child
    # git processes. If submodules are ever introduced, switch to the env-var
    # form (GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath
    # GIT_CONFIG_VALUE_0=/dev/null) which DOES propagate to child processes.
    # Repo currently has no submodules, so this is latent rather than live.
    if ! git "${GIT_SAFE[@]}" submodule update --init --recursive >>"$LOG" 2>&1; then
        log "  WARN: submodule sync after rollback non-zero"
        # Round-4 fix (code-reviewer ROUND-3 MED #1): only write the sentinel
        # if SUBMODULES ACTUALLY EXIST. Without this guard, a benign
        # submodule update non-zero on a repo with no submodules (the
        # current `audit-pipeline-cli` reality) would write the sentinel
        # and indefinitely block the operator on every subsequent tick —
        # all over a non-failure. `git submodule status` exits 0 + empty
        # output on a repo with no submodules.
        # Round-6 fix (devils-advocate ROUND-5 HIGH #3 + code-reviewer #1):
        # apply GIT_SAFE to the existence-check too. Bare git after attacker
        # refs are in the object store = potential hook vector.
        if git "${GIT_SAFE[@]}" submodule status 2>/dev/null | grep -q .; then
            # Round-3 fix (code-reviewer ROUND-2 LOW #2): write dirty sentinel
            # so attacker's rejected submodule code can't linger silently.
            # Round-4 fix: append (>>) not overwrite (>) to preserve any
            # prior reset-failure message in the same tick (devils-advocate
            # ROUND-3 HIGH #3).
            if ! echo "submodule sync failed at $(date -u +%FT%TZ) HEAD=$NEW_HEAD LOCAL=$LOCAL" \
                    >> "$DIRTY_SENTINEL" 2>>"$LOG"; then
                log "  ERROR: could not write dirty sentinel after submodule failure: $DIRTY_SENTINEL"
            fi
        else
            log "  (no submodules in repo — non-zero is expected, not writing sentinel)"
        fi
    fi
    # Exit non-zero on signature failure so systemd / journalctl shows this as
    # a distinct security event rather than a clean success. All OTHER failure
    # paths still exit 0 (network blips etc.) — only signature failure uses 3.
    exit 3
else
    log "  signature verified for $NEW_HEAD"
    # Round-4 fix (devils-advocate ROUND-3 HIGH #1): clear the SIGTERM trap
    # NOW that the commit is verified. The post-verify pip-install + restart
    # should NOT roll back if interrupted — they're operating on verified
    # code, and rolling back would also be destructive to running daemons.
    # An interrupt past this point is just a normal abort; systemd will
    # restart the daemons cleanly on the next tick.
    trap - SIGTERM SIGINT SIGHUP
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
