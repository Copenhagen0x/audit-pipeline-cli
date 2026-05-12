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
AUDIT_PIPELINE="${AUDIT_PIPELINE:-audit-pipeline}"

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

# 12-audit post-cycle QA gate honour check: hunt's post-cycle QA writes
# a ``.publish-blocked`` sentinel into the cycle dir if any confirmed
# finding failed re-checks. Refuse to publish blocked cycles to the
# signed docroot — the public surface MUST never serve a cycle that the
# pipeline itself flagged as suspect.
if [ -f "$WORKSPACE/hunts/$CYCLE_ID/.publish-blocked" ]; then
    echo "publish_cycle_signed: ABORTING — $CYCLE_ID has a .publish-blocked sentinel"
    cat "$WORKSPACE/hunts/$CYCLE_ID/.publish-blocked" 2>/dev/null | head -40
    exit 0
fi

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

# 4a. Write a minimal index.html so the bare directory URL serves something
# instead of returning 403 (nginx doesn't auto-index, by design). The customer
# dashboard's "verify" link points at /cycles/<id>/ — without this, every
# verify link is broken even when the artefacts are present.
cat > "$STAGING/index.html" <<HTMLEOF
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jelleo cycle ${CYCLE_ID}</title>
<style>
  body{font-family:Inter,system-ui,sans-serif;background:#050504;color:#e6e1d8;margin:0;padding:48px 32px;max-width:760px;margin-inline:auto;line-height:1.55}
  h1{color:#f5b800;font-weight:600;letter-spacing:-0.01em;margin:0 0 8px}
  h2{color:#f5b800;font-weight:500;font-size:1.05rem;margin:32px 0 12px;letter-spacing:-0.005em}
  p{color:#bdb5a8;margin:8px 0}
  a{color:#f5b800;text-decoration:none;border-bottom:1px dashed rgba(245,184,0,0.3)}
  a:hover{border-bottom-style:solid}
  ul{padding-left:20px;color:#e6e1d8}
  ul li{margin:8px 0;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:0.9rem}
  pre{background:rgba(245,184,0,0.04);border:1px solid rgba(245,184,0,0.18);border-radius:6px;padding:14px 18px;overflow-x:auto;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:0.83rem;color:#d4cdc0}
  .meta{color:#7a7163;font-size:0.85rem}
  hr{border:0;border-top:1px solid rgba(245,184,0,0.18);margin:32px 0}
</style>
</head>
<body>
<h1>Jelleo · cycle ${CYCLE_ID}</h1>
<p class="meta">Signed cycle receipt bundle. Every artefact below is attested with the platform's Ed25519 key.</p>

<h2>Artefacts</h2>
<ul>
  <li><a href="cycle.html">cycle.html</a> &middot; branded HTML report</li>
  <li><a href="cycle.html.sig">cycle.html.sig</a> &middot; Ed25519 signature over cycle.html</li>
  <li><a href="cycle.pdf">cycle.pdf</a> &middot; PDF render of the report</li>
  <li><a href="cycle.pdf.sig">cycle.pdf.sig</a> &middot; Ed25519 signature over cycle.pdf</li>
</ul>

<h2>Verify (independent of Jelleo)</h2>
<p>Pin the platform public key once, then verify any cycle artefact against it without trusting the operator:</p>
<pre>curl -O https://api.jelleo.com/keys/jelleo.ed25519.pub
curl -O https://api.jelleo.com/cycles/${CYCLE_ID}/cycle.html
curl -O https://api.jelleo.com/cycles/${CYCLE_ID}/cycle.html.sig

audit-pipeline sign verify --pubkey jelleo.ed25519.pub \\
  --artifact cycle.html --sig cycle.html.sig
# &rarr; "&check; signature valid, signed by &lt;fingerprint&gt;"</pre>

<p>Or with any standard Ed25519 verifier (Python <code>cryptography</code>, <code>openssl</code>, etc.) — the signature is base64 PKCS#8 inside a JELLEO armour block.</p>

<hr>
<p class="meta">Methodology &sect;07 &middot; <a href="https://github.com/Copenhagen0x/audit-pipeline-cli/tree/main/docs/methodology">spec</a> &middot; <a href="https://api.jelleo.com/keys/jelleo.ed25519.pub">platform public key</a></p>
</body>
</html>
HTMLEOF

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

# 4b. Re-wrap the per-cycle landing page in jelleo.com chrome.
# (The heredoc above writes a minimal index.html that survives the atomic
#  swap. This step overwrites it with the full chromed version so the page
#  matches /cycles/ archive + the rest of jelleo.com. Failure is non-fatal
#  — the minimal fallback is already in place.)
WRAP_SCRIPT="$(dirname "$0")/wrap_per_cycle_landing.py"
if [ -f "$WRAP_SCRIPT" ]; then
    python3 "$WRAP_SCRIPT" "$CYCLE_ID" --docroot "$(dirname "$DOCROOT")" 2>&1 | tail -1 || true
    chown www-data:www-data "$DEST/index.html" 2>/dev/null || true
fi

echo "publish_cycle_signed: published $CYCLE_ID -> $DEST"
ls -la "$DEST"

# 5. Regenerate the cycle archive landing page at <docroot>/cycles/index.html
# so the bare URL api.jelleo.com/cycles/ lists every signed cycle (newest
# first). Without this, the bare URL 404s — only per-cycle subdirs serve.
REGEN_SCRIPT="$(dirname "$0")/regen_cycles_index.py"
if [ -f "$REGEN_SCRIPT" ]; then
    python3 "$REGEN_SCRIPT" --docroot "$(dirname "$DOCROOT")" 2>&1 | tail -1 || true
    chown www-data:www-data "$DOCROOT/index.html" 2>/dev/null || true
fi

# 6. Trigger snapshot rebuild so receipts_signed updates within seconds
if systemctl list-units --no-legend --all 'jelleo-snapshot.service' >/dev/null 2>&1; then
    systemctl start jelleo-snapshot.service 2>/dev/null || true
fi
