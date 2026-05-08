#!/bin/bash
# publish_cycle_signed.sh — Render + sign + copy a hunt cycle's report bundle
# to /var/www/jelleo.com/cycles/<cycle_id>/.
#
# Designed to run AFTER `audit-pipeline hunt` completes (i.e. as the second
# step in jelleo-watch's --on-update hook, right after publish_cycle.sh
# pushes the raw hunt artefact to the GitHub repo).
#
# What this produces under <docroot>/cycles/<cycle_id>/:
#   cycle.html       — branded HTML report (severity rollup + finding cards)
#   cycle.html.sig   — Ed25519 signature over cycle.html
#   cycle.pdf        — chromium-headless PDF render
#   cycle.pdf.sig    — Ed25519 signature over cycle.pdf
#
# Idempotent — re-publishing the same cycle just refreshes the four files.
#
# Usage:
#   publish_cycle_signed.sh                # auto-detects latest completed cycle
#   publish_cycle_signed.sh <cycle-id>     # publish a specific cycle
#
# Side effects:
#   * Writes 4 files to <docroot>/cycles/<cycle_id>/
#   * chowns them to www-data:www-data, mode 644
#   * Triggers a snapshot rebuild so receipts_signed updates in snapshot.json
#
# Powers methodology §07's "every cycle ships a signed receipt" guarantee.

set -euo pipefail

WORKSPACE="${WORKSPACE:-/root/audit_runs/percolator-live}"
DOCROOT="${DOCROOT:-/var/www/jelleo.com/cycles}"
AUDIT_PIPELINE="${AUDIT_PIPELINE:-/root/.local/bin/audit-pipeline}"

# 1. Resolve cycle id
if [ "$#" -ge 1 ]; then
    CYCLE_ID="$1"
else
    # Auto-detect: most recent cycle in findings.db
    if [ ! -f "$WORKSPACE/findings.db" ]; then
        echo "publish_cycle_signed: $WORKSPACE/findings.db missing — exiting"
        exit 0
    fi
    CYCLE_ID=$(sqlite3 "$WORKSPACE/findings.db" \
        "SELECT cycle_id FROM cycles ORDER BY started_at DESC LIMIT 1;" 2>/dev/null || true)
    if [ -z "$CYCLE_ID" ]; then
        echo "publish_cycle_signed: no cycles in DB — nothing to publish"
        exit 0
    fi
fi

DEST="$DOCROOT/$CYCLE_ID"

# 2. Skip if already published (idempotent on identical content)
if [ -f "$DEST/cycle.html" ] && [ -f "$DEST/cycle.html.sig" ]; then
    # Already published. Re-rendering with the same DB state would produce
    # identical bytes (modulo timestamps in the report) so we'd just churn
    # the signature. Bail unless the caller explicitly wants a refresh.
    if [ "${FORCE:-0}" != "1" ]; then
        echo "publish_cycle_signed: $CYCLE_ID already published; pass FORCE=1 to refresh"
        exit 0
    fi
fi

# 3. Render + sign into a tmp dir, then atomically swap into docroot
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "publish_cycle_signed: rendering $CYCLE_ID -> $TMP/"
"$AUDIT_PIPELINE" --workspace "$WORKSPACE" report cycle \
    --cycle-id "$CYCLE_ID" \
    --output "$TMP/cycle.html" \
    --pdf

if [ ! -f "$TMP/cycle.html" ] || [ ! -f "$TMP/cycle.html.sig" ]; then
    echo "publish_cycle_signed: report cycle failed — bailing (no files copied)"
    exit 1
fi

# 4. Atomic swap: write to a sibling tmp dir, then rename into place
mkdir -p "$DOCROOT"
STAGING="$DOCROOT/.staging-$CYCLE_ID-$$"
mkdir -p "$STAGING"
cp "$TMP/cycle.html"     "$STAGING/"
cp "$TMP/cycle.html.sig" "$STAGING/"
[ -f "$TMP/cycle.pdf" ]      && cp "$TMP/cycle.pdf"     "$STAGING/"
[ -f "$TMP/cycle.pdf.sig" ]  && cp "$TMP/cycle.pdf.sig" "$STAGING/"

chown -R www-data:www-data "$STAGING"
chmod -R a+r "$STAGING"
find "$STAGING" -type d -exec chmod a+x {} \;

# Rename: removes any old version, atomically slots the new one in
if [ -d "$DEST" ]; then
    rm -rf "$DEST.old.$$"
    mv "$DEST" "$DEST.old.$$" 2>/dev/null || true
fi
mv "$STAGING" "$DEST"
[ -d "$DEST.old.$$" ] && rm -rf "$DEST.old.$$"

echo "publish_cycle_signed: published $CYCLE_ID -> $DEST"
ls -la "$DEST"

# 5. Trigger snapshot rebuild so receipts_signed updates within seconds
if systemctl list-units --no-legend --all 'jelleo-snapshot.service' >/dev/null 2>&1; then
    systemctl start jelleo-snapshot.service 2>/dev/null || true
fi
