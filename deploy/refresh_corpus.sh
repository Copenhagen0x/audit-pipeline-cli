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

# ── SUPPLY-CHAIN GATE (audit finding R2-2 5819e8b5) ──────────────────────
# Corpus repos are third-party (anchor, drift, mango, marginfi, phoenix,
# openbook, orca, meteora etc). Any one of them being compromised lands a
# git hook on this host. We run as root via jelleo-corpus-refresh.timer.
#
# Defense: -c core.hooksPath=/dev/null prevents post-checkout / post-merge
# hook execution. -c protocol.file.allow=never blocks file:// SSRF via
# submodule URLs. Both are belt-and-suspenders — if a corpus repo's .git/hooks
# directory contains a malicious script, git would normally execute it after
# update; with hooksPath set to /dev/null, hooks are disabled per-invocation
# without modifying the corpus repo's own config.
#
# These flags must appear on EVERY git invocation that touches a corpus repo,
# not just submodule update — `git pull` can also run merge/post-rewrite
# hooks under some configurations.
# ──────────────────────────────────────────────────────────────────────────
# R5b-3 (2026-05-24): protocol.ext.allow=never closes ext:: RCE
# (corpus repos are third-party — submodule URLs not trusted).
GIT_SAFE=(-c core.hooksPath=/dev/null -c protocol.file.allow=never -c protocol.ext.allow=never)

failed=0
ok=0
for repo_dir in "$CORPUS"/*/; do
    [ -d "$repo_dir/.git" ] || continue
    name=$(basename "$repo_dir")
    if ( cd "$repo_dir" && git "${GIT_SAFE[@]}" pull --quiet --ff-only 2>/dev/null && \
         git "${GIT_SAFE[@]}" submodule update --init --recursive --quiet 2>/dev/null ); then
        ok=$((ok+1))
        echo "$(date -u +%FT%TZ)   ok: $name" >> "$LOG"
    else
        failed=$((failed+1))
        echo "$(date -u +%FT%TZ)   FAILED: $name" >> "$LOG"
    fi
done

echo "$(date -u +%FT%TZ) refresh complete: ok=$ok failed=$failed" >> "$LOG"
