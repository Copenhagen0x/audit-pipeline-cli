#!/bin/bash
# publish_cycle.sh — Copies the latest hunt cycle artifact from the VPS
# workspace into the public audit-pipeline-cli repo and pushes it.
#
# Designed to run from the jelleo-watch on-update hook, RIGHT AFTER
# `audit-pipeline hunt` completes. Idempotent — safe to re-run; it skips
# already-published cycles and ignores incomplete ones (no hunt_summary.json).
#
# Side effects:
#   * Copies $WORKSPACE/hunts/<latest>/* to $REPO/examples/recent-hunts/<dir>/
#   * Prepends a manifest entry to $REPO/examples/recent-hunts/index.json
#   * git commit + git push origin main on $REPO
#
# Powers the "live surveillance terminal" on jelleo.com — the website's
# JS fetches the manifest and animates real cycle data.

set -euo pipefail

WORKSPACE="${WORKSPACE:-/root/audit_runs/percolator-live}"
REPO="${REPO:-/root/audit-pipeline-cli}"

if [ ! -d "$WORKSPACE/hunts" ]; then
    echo "publish_cycle: no hunts/ dir at $WORKSPACE — exiting"
    exit 0
fi

# Pick the most-recent COMPLETED cycle (has hunt_summary.json).
# Sort cycle dir names DESCENDING — they're prefixed with timestamps
# (YYYYMMDD-HHMMSS-...) so lexical sort = chronological sort. This is
# robust to mtime weirdness from prior runs of this script.
LATEST=""
for d in $(ls -1 "$WORKSPACE/hunts/" 2>/dev/null | sort -r); do
    if [ -f "$WORKSPACE/hunts/$d/hunt_summary.json" ]; then
        LATEST="$d"
        break
    fi
done

# 12-audit post-cycle QA gate honour check: hunt's post-cycle QA writes
# a ``.publish-blocked`` sentinel into the cycle dir if any confirmed
# finding failed re-checks (hallucinated symbols / pseudo-pass markers /
# missing PoC files). This guard refuses to publish blocked cycles.
if [ -n "$LATEST" ] && [ -f "$WORKSPACE/hunts/$LATEST/.publish-blocked" ]; then
    echo "publish_cycle: ABORTING — $LATEST has a .publish-blocked sentinel"
    echo "publish_cycle: post-cycle QA gate failed; nothing published."
    cat "$WORKSPACE/hunts/$LATEST/.publish-blocked" 2>/dev/null | head -40
    exit 0
fi

if [ -z "$LATEST" ]; then
    echo "publish_cycle: no completed cycles found; nothing to publish"
    exit 0
fi

SUMMARY_PATH="$WORKSPACE/hunts/$LATEST/hunt_summary.json"

# Extract metadata via Python (jq isn't guaranteed installed)
META=$(python3 - "$SUMMARY_PATH" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
fields = [
    'engine_sha', 'started_at', 'elapsed_seconds', 'total_cost_usd',
    'n_hypotheses', 'n_candidates', 'n_confirmed', 'source_repo'
]
out = {k: d.get(k, '') for k in fields}
out['source_repo'] = out['source_repo'] or 'aeyakovenko/percolator'
print(json.dumps(out))
PYEOF
)

ENGINE_SHA=$(echo "$META" | python3 -c "import json,sys; print(json.load(sys.stdin)['engine_sha'])")
STARTED=$(echo "$META" | python3 -c "import json,sys; print(json.load(sys.stdin)['started_at'])")
SOURCE_REPO=$(echo "$META" | python3 -c "import json,sys; print(json.load(sys.stdin)['source_repo'])")

if [ -z "$ENGINE_SHA" ] || [ -z "$STARTED" ]; then
    echo "publish_cycle: missing engine_sha or started_at — bailing"
    exit 1
fi

# Use the cycle_id directly as the published dir name. Cycle ids are
# unique (timestamp-suffixed-with-sha7), so this never collides — even
# for two cycles against the same upstream SHA on the same day.
PUBLISHED_DIR="$LATEST"
DEST="$REPO/examples/recent-hunts/$PUBLISHED_DIR"

# 1. Copy artifact (idempotent — skip if already there with same content)
if [ -d "$DEST" ] && [ -f "$DEST/hunt_summary.json" ]; then
    if cmp -s "$WORKSPACE/hunts/$LATEST/hunt_summary.json" "$DEST/hunt_summary.json"; then
        echo "publish_cycle: $PUBLISHED_DIR already published with identical summary — skipping"
        exit 0
    fi
fi

mkdir -p "$DEST"
cp -r "$WORKSPACE/hunts/$LATEST/"* "$DEST/"
echo "publish_cycle: copied cycle $LATEST -> $PUBLISHED_DIR"

# 2. Update the manifest (prepend new entry, cap at 50 most recent)
MANIFEST="$REPO/examples/recent-hunts/index.json"

python3 - "$SUMMARY_PATH" "$MANIFEST" "$LATEST" "$PUBLISHED_DIR" <<'PYEOF'
import json, sys, os, datetime

summary_path, manifest_path, cycle_id, published_dir = sys.argv[1:5]
with open(summary_path) as f:
    s = json.load(f)

verdicts = s.get('verdicts') or []
verdict_true   = sum(1 for v in verdicts if v.get('verdict') == 'TRUE')
verdict_layer2 = sum(1 for v in verdicts if v.get('verdict') == 'NEEDS_LAYER_2_TO_DECIDE')
verdict_unknown = len(verdicts) - verdict_true - verdict_layer2

new_entry = {
    "id": cycle_id,
    "dir": published_dir,
    "target": s.get('target', 'percolator-live'),
    "source_repo": s.get('source_repo') or 'aeyakovenko/percolator',
    "engine_sha": s.get('engine_sha', ''),
    "started_at": s.get('started_at', ''),
    "elapsed_seconds": s.get('elapsed_seconds'),
    "total_cost_usd": s.get('total_cost_usd'),
    "n_hypotheses": s.get('n_hypotheses', 0),
    "n_candidates": s.get('n_candidates', 0),
    "n_confirmed": s.get('n_confirmed', 0),
    "verdict_true": verdict_true,
    "verdict_layer2": verdict_layer2,
    "verdict_unknown": verdict_unknown,
    "summary_url": f"https://raw.githubusercontent.com/Copenhagen0x/audit-pipeline-cli/main/examples/recent-hunts/{published_dir}/hunt_summary.json",
    "browse_url":  f"https://github.com/Copenhagen0x/audit-pipeline-cli/tree/main/examples/recent-hunts/{published_dir}",
}

if os.path.exists(manifest_path):
    with open(manifest_path) as f:
        manifest = json.load(f)
else:
    manifest = {
        "schema": "jelleo.recent-hunts.manifest.v1",
        "description": "Manifest of recent autonomous hunt cycles published to this repo.",
        "cycles": [],
    }

cycles = [c for c in manifest.get('cycles', []) if c.get('id') != cycle_id]
cycles.insert(0, new_entry)
manifest['cycles'] = cycles[:50]
manifest['updated_at'] = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

with open(manifest_path, 'w') as f:
    json.dump(manifest, f, indent=2)
    f.write('\n')
print(f"publish_cycle: manifest updated with cycle {cycle_id}")
PYEOF

# 3. Commit + push
cd "$REPO"

# Set committer identity if not configured (auto-publisher)
git config user.email "$(git config user.email 2>/dev/null || echo 'auto-publish@jelleo')" >/dev/null 2>&1
git config user.name  "$(git config user.name  2>/dev/null || echo 'Jelleo auto-publisher')" >/dev/null 2>&1

git add "examples/recent-hunts/$PUBLISHED_DIR" "examples/recent-hunts/index.json"

if git diff --staged --quiet; then
    echo "publish_cycle: no staged changes after copy/manifest — nothing to commit"
    exit 0
fi

# Reach into the summary for the commit body
COMMIT_BODY=$(python3 - "$SUMMARY_PATH" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f: s = json.load(f)
print(f"Cycle metrics:")
print(f"- elapsed: {s.get('elapsed_seconds', '?')}s")
print(f"- cost: ${s.get('total_cost_usd', '?')}")
print(f"- hypotheses dispatched: {s.get('n_hypotheses', 0)}")
print(f"- layer-2 candidates: {s.get('n_candidates', 0)}")
print(f"- confirmed findings: {s.get('n_confirmed', 0)}")
print()
print("Auto-pushed by deploy/publish_cycle.sh from the VPS post-cycle hook.")
PYEOF
)

git commit -m "Auto-publish hunt cycle $LATEST ($SOURCE_REPO@$ENGINE_SHA)

$COMMIT_BODY"

# Push with rebase-on-conflict retry — the autonomous loop can race with
# manual pushes when the operator is also pushing. 3 attempts then bail.
PUSH_OK=0
for attempt in 1 2 3; do
    if git push origin main; then
        echo "publish_cycle: pushed cycle $LATEST"
        PUSH_OK=1
        break
    fi
    echo "publish_cycle: push attempt $attempt failed; pulling --rebase before retry"
    git pull --rebase origin main || true
done

# --------------------------------------------------------------------------
# 4. Publish the signed cycle report to /var/www/jelleo.com/cycles/<id>/ so
#    customers can fetch it from https://api.jelleo.com/cycles/<id>/...
#    Best-effort — failures here don't block the cycle.
# --------------------------------------------------------------------------

PUBLIC_CYCLE_DIR="${PUBLIC_CYCLE_DIR:-/var/www/jelleo.com/cycles/$LATEST}"
if mkdir -p "$PUBLIC_CYCLE_DIR" 2>/dev/null; then
    # Copy summary + report + signature
    cp -f "$WORKSPACE/hunts/$LATEST/hunt_summary.json" "$PUBLIC_CYCLE_DIR/" 2>/dev/null || true
    [ -f "$WORKSPACE/hunts/$LATEST/hunt_report.html"     ] && \
        cp -f "$WORKSPACE/hunts/$LATEST/hunt_report.html"     "$PUBLIC_CYCLE_DIR/" || true
    [ -f "$WORKSPACE/hunts/$LATEST/hunt_report.html.sig" ] && \
        cp -f "$WORKSPACE/hunts/$LATEST/hunt_report.html.sig" "$PUBLIC_CYCLE_DIR/" || true

    # Render HTML -> PDF via chromium (if installed) and sign the PDF
    if [ -f "$PUBLIC_CYCLE_DIR/hunt_report.html" ]; then
        CHROMIUM_BIN=""
        # NOTE: search order matters. chromium-browser on Ubuntu is the snap
        # build, which is AppArmor-confined and silently fails to write to
        # /var/www/ or /tmp (it writes inside the snap sandbox instead). The
        # native google-chrome / google-chrome-stable / chromium debs work
        # fine, so try them first.
        for c in google-chrome google-chrome-stable chromium chromium-browser; do
            if command -v "$c" >/dev/null 2>&1; then CHROMIUM_BIN="$c"; break; fi
        done
        if [ -n "$CHROMIUM_BIN" ]; then
            "$CHROMIUM_BIN" --headless --disable-gpu --no-sandbox --no-pdf-header-footer \
                --print-to-pdf="$PUBLIC_CYCLE_DIR/hunt_report.pdf" \
                "file://$PUBLIC_CYCLE_DIR/hunt_report.html" >/dev/null 2>&1 || true
            if [ -f "$PUBLIC_CYCLE_DIR/hunt_report.pdf" ]; then
                audit-pipeline --workspace "$WORKSPACE" \
                    sign sign "$PUBLIC_CYCLE_DIR/hunt_report.pdf" >/dev/null 2>&1 || true
                echo "publish_cycle: rendered + signed hunt_report.pdf"
            fi
        else
            echo "publish_cycle: chromium not installed — skipping PDF render (HTML+sig published)"
        fi
    fi

    # Lock down ownership so nginx can serve, but only root can write
    chown -R www-data:www-data "$PUBLIC_CYCLE_DIR" 2>/dev/null || true
    chmod -R a+r "$PUBLIC_CYCLE_DIR" 2>/dev/null || true

    echo "publish_cycle: published to $PUBLIC_CYCLE_DIR"

    # 4b. Regenerate the cycle archive index page so the new cycle shows up
    # at https://api.jelleo.com/cycles/. Best-effort — non-fatal.
    REGEN_SCRIPT="$REPO/deploy/regen_cycles_index.py"
    if [ -x /usr/bin/python3 ] && [ -f "$REGEN_SCRIPT" ]; then
        if python3 "$REGEN_SCRIPT" --docroot /var/www/jelleo.com >/dev/null 2>&1; then
            echo "publish_cycle: regenerated /cycles/ index"
        else
            echo "publish_cycle: regen_cycles_index failed (non-fatal)"
        fi
    fi
else
    echo "publish_cycle: could not create $PUBLIC_CYCLE_DIR — skipping public copy"
fi

# --------------------------------------------------------------------------
# 5. Email immediate alerts for confirmed Critical/High findings in this cycle.
#    Uses `audit-pipeline notify critical --finding-id N` which routes via
#    notifier.json's critical_oncall + critical_team channels (SMTP).
#    Best-effort — non-fatal on failure.
# --------------------------------------------------------------------------

if [ -f "$WORKSPACE/findings.db" ] && command -v sqlite3 >/dev/null 2>&1; then
    REPRO_BASE="https://api.jelleo.com/cycles/$LATEST"
    # Orchestration audit Defect 07 (MED): $LATEST flows directly into a
    # SQL string. Although LATEST today is a hunt-dir name (timestamped +
    # secrets.token_hex(2) suffixed — operator-controlled), the brittle
    # pattern needs locking down. Validate against the strict cycle-id
    # shape BEFORE interpolation; refuse anything else.
    if ! printf '%s' "$LATEST" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'; then
        echo "publish_cycle: refusing unsafe cycle id $LATEST" >&2
        exit 1
    fi
    CONFIRMED_IDS=$(sqlite3 "$WORKSPACE/findings.db" \
        "SELECT id FROM findings WHERE cycle_id='$LATEST' AND status='confirmed' AND severity IN ('Critical','High');" \
        2>/dev/null || true)
    if [ -n "$CONFIRMED_IDS" ]; then
        N_NOTIFIED=0
        for FID in $CONFIRMED_IDS; do
            if audit-pipeline --workspace "$WORKSPACE" \
                notify critical --finding-id "$FID" --repro-link "$REPRO_BASE/" \
                >/dev/null 2>&1; then
                N_NOTIFIED=$((N_NOTIFIED + 1))
            fi
        done
        echo "publish_cycle: emailed $N_NOTIFIED critical/high alerts"
    else
        echo "publish_cycle: no confirmed Critical/High in cycle $LATEST — no email alerts"
    fi
fi

# Telegram alert (best-effort — doesn't change exit code).
# Sends only if both env vars are set. Different message tone for routine
# cycles vs. cycles with TRUE verdicts vs. cycles with confirmed PoC fires.
if [ -n "${HUNT_TELEGRAM_TOKEN:-}" ] && [ -n "${HUNT_TELEGRAM_CHAT_ID:-}" ]; then
    TG_MESSAGE=$(python3 - "$SUMMARY_PATH" "$PUBLISHED_DIR" "$PUSH_OK" <<'PYEOF'
import json, sys

summary_path, published_dir, push_ok = sys.argv[1:4]
with open(summary_path) as f:
    s = json.load(f)

verdicts = s.get('verdicts') or []
n_true     = sum(1 for v in verdicts if v.get('verdict') == 'TRUE')
n_layer2   = sum(1 for v in verdicts if v.get('verdict') == 'NEEDS_LAYER_2_TO_DECIDE')
n_unknown  = len(verdicts) - n_true - n_layer2
n_confirmed = int(s.get('n_confirmed', 0) or 0)

cycle_id = s.get('cycle_id', 'unknown')
target   = s.get('source_repo', '?')
sha      = s.get('engine_sha', '?')
elapsed  = s.get('elapsed_seconds', 0) or 0
cost     = s.get('total_cost_usd', 0) or 0
n_hyps   = s.get('n_hypotheses', len(verdicts)) or len(verdicts)
started  = (s.get('started_at') or '').replace('+00:00', '').replace('T', ' ')[:16] + ' UTC'

artifact_url = f"https://github.com/Copenhagen0x/audit-pipeline-cli/tree/main/examples/recent-hunts/{published_dir}"

# Severity selection
if n_confirmed > 0:
    headline = "🚨 <b>Jelleo · CONFIRMED FINDING</b>"
    finding  = f"<b>⚠ {n_confirmed} PoC fired — empirically confirmed bug</b>"
elif n_true > 0:
    headline = "🔍 <b>Jelleo · cycle flagged</b>"
    finding  = f"<b>{n_true} TRUE verdict · {n_layer2} layer-2 candidate(s)</b>"
else:
    headline = "✅ <b>Jelleo · cycle complete</b>"
    finding  = f"All {n_hyps} hypotheses returned a clean verdict"

# List TRUE verdicts inline (max 5 to keep the message readable)
true_lines = []
for v in verdicts:
    if v.get('verdict') == 'TRUE':
        hyp_id = v.get('hypothesis_id', '?')
        conf   = v.get('confidence', 'UNKNOWN')
        true_lines.append(f"• <code>{hyp_id}</code> · {conf}")
true_lines = true_lines[:5]
true_block = ('\n' + '\n'.join(true_lines)) if true_lines else ''

# Push status note (only shown if push failed)
push_note = '' if push_ok == '1' else '\n<i>⚠ git push failed — artifact only on VPS, not yet public</i>'

message = f"""{headline}

<i>{started}</i>
🎯 <code>{target}@{sha[:7]}</code>
🆔 <code>{cycle_id}</code>
⏱ {elapsed:.0f}s · 💰 ${cost:.2f}

{finding}{true_block}

<b>Verdict counts</b>
• TRUE       {n_true}
• LAYER_2    {n_layer2}
• UNKNOWN    {n_unknown}
• PoC fired  {n_confirmed}{push_note}

<a href="{artifact_url}">View full artifact →</a>"""

print(message, end='')
PYEOF
)
    if curl -s -X POST "https://api.telegram.org/bot${HUNT_TELEGRAM_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${HUNT_TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${TG_MESSAGE}" \
        -d "parse_mode=HTML" \
        -d "disable_web_page_preview=true" >/dev/null 2>&1; then
        echo "publish_cycle: telegram alert sent"
    else
        echo "publish_cycle: telegram alert failed (non-fatal)"
    fi
fi

if [ "$PUSH_OK" = "1" ]; then
    exit 0
fi

echo "publish_cycle: push failed after 3 attempts — manual intervention required"
exit 1
