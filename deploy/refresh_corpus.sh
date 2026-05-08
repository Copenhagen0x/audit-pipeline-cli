#!/bin/bash
# refresh_corpus.sh — daily git-pull across every cloned protocol in the
# propagation corpus. Without freshness, propagation searches stale code,
# which means hits surface against already-patched issues.
#
# Idempotent. Best-effort: a single repo failing pull doesn't block others.
# Skips submodules' submodules (only top-level submodule update).

set -euo pipefail

CORPUS="${CORPUS:-/root/audit_runs/percolator-live/recon/propagate/corpus}"
LOG="${LOG:-/root/audit_runs/percolator-live/recon/propagate/refresh.log}"

if [ ! -d "$CORPUS" ]; then
    echo "$(date -u +%FT%TZ) corpus dir missing: $CORPUS" >> "$LOG"
    exit 0
fi

echo "$(date -u +%FT%TZ) refresh started" >> "$LOG"

failed=0
ok=0
for repo_dir in "$CORPUS"/*/; do
    [ -d "$repo_dir/.git" ] || continue
    name=$(basename "$repo_dir")
    if ( cd "$repo_dir" && git pull --quiet --ff-only 2>/dev/null && \
         git submodule update --init --recursive --quiet 2>/dev/null ); then
        ok=$((ok+1))
        echo "$(date -u +%FT%TZ)   ok: $name" >> "$LOG"
    else
        failed=$((failed+1))
        echo "$(date -u +%FT%TZ)   FAILED: $name" >> "$LOG"
    fi
done

echo "$(date -u +%FT%TZ) refresh complete: ok=$ok failed=$failed" >> "$LOG"
