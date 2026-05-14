#!/usr/bin/env bash
# watch_on_update.sh — fired by jelleo-watch.service when a new commit is detected
# on the engine OR wrapper repo. Runs a CHEAP triage cycle (recon-only) — full
# Step-3-class audits are user-triggered explicitly.
#
# This script exists because the systemd `--on-update` arg path is fragile
# with nested quotes / shell builtins like `source`. Putting the logic here
# means the watch service spawns a clean bash interpreter every time.
#
# Args: forwarded by watch.py — typically extra --source-repo + --source-sha
# flags in source-mode.

set -euo pipefail
source /root/.audit-env

WORKSPACE="${JELLEO_WORKSPACE:-/root/audit_runs/percolator-live}"
LOG="$WORKSPACE/watch/hunt-on-update.log"

mkdir -p "$(dirname "$LOG")"

{
    echo "=== watch_on_update $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
    echo "args: $@"
    # Cheap triage cycle: recon + Pillar 4 only. No PoC / debate / Kani / P2 / P3.
    # Budget cap removed 2026-05-13 per operator request — hunt now runs
    # with hunt.py's default (effectively unlimited). Scope is constrained
    # by --skip-* flags instead.
    # Cycle 20260514-151541 fix: --auto-publish is now passed explicitly
    # to preserve the percolator-live auto-publish workflow after the
    # hunt-default flag was flipped to --no-auto-publish. Without this
    # explicit pass, every percolator-live commit-triggered cycle would
    # stop publishing to jelleo.com/cycles/.
    audit-pipeline --workspace "$WORKSPACE" hunt \
        --skip-poc --skip-debate --skip-narrative \
        --skip-kani --skip-propagate --skip-bundle \
        --auto-publish \
        "$@"
    echo "=== watch_on_update done $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
} 2>&1 | tee -a "$LOG"
