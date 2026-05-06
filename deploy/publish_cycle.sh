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

LATEST=$(ls -1t "$WORKSPACE/hunts/" 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
    echo "publish_cycle: no hunt cycles found; nothing to publish"
    exit 0
fi

SUMMARY_PATH="$WORKSPACE/hunts/$LATEST/hunt_summary.json"
if [ ! -f "$SUMMARY_PATH" ]; then
    echo "publish_cycle: cycle $LATEST has no hunt_summary.json (incomplete) — skipping"
    exit 0
fi

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

DATE_PART=$(echo "$STARTED" | cut -c1-10)
PUBLISHED_DIR="${DATE_PART}-cycle-${ENGINE_SHA}"
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
for attempt in 1 2 3; do
    if git push origin main; then
        echo "publish_cycle: pushed cycle $LATEST"
        exit 0
    fi
    echo "publish_cycle: push attempt $attempt failed; pulling --rebase before retry"
    git pull --rebase origin main || true
done

echo "publish_cycle: push failed after 3 attempts — manual intervention required"
exit 1
