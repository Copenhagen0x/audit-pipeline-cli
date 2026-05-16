#!/usr/bin/env bash
# watch_on_update.sh — fired by jelleo-watch.service when a new commit is detected.
# NEUTERED 2026-05-14 per operator directive: NO auto-fire, NO auto-publish.
# Original script preserved at watch_on_update.sh.bak-20260514.
# This shim logs the trigger and exits 0 — no hunt invocation, no publish.

set -euo pipefail

WORKSPACE="${JELLEO_WORKSPACE:-/root/audit_runs/percolator-live}"
LOG="$WORKSPACE/watch/hunt-on-update.log"
mkdir -p "$(dirname "$LOG")"

{
    echo "=== watch_on_update SKIPPED $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
    echo "args: $@"
    echo "NEUTERED: no auto-fire, no auto-publish (operator directive 2026-05-14)"
    echo "=== watch_on_update done $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
} 2>&1 | tee -a "$LOG"
