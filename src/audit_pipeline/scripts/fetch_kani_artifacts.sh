#!/usr/bin/env bash
# fetch_kani_artifacts.sh — pull Kani run artifacts (raw log + summary) from VPS to local.
#
# Usage:
#   bash scripts/fetch_kani_artifacts.sh <vps-host> <ssh-key> <harness-name> [--baseline]
#
# Output (local):
#   ./kani_<harness>.log.gz      — gzipped raw cargo kani output
#   ./kani_<harness>_summary.txt — short pass/fail summary
#   ./kani_<harness>_timings.tsv — per-harness verification time (for --baseline runs)

set -euo pipefail

VPS_HOST="${1:?Usage: fetch_kani_artifacts.sh <vps-host> <ssh-key> <harness>}"
SSH_KEY="${2:?Usage: fetch_kani_artifacts.sh <vps-host> <ssh-key> <harness>}"
HARNESS="${3:?Usage: fetch_kani_artifacts.sh <vps-host> <ssh-key> <harness>}"

SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

if [[ "$HARNESS" == "--baseline" ]]; then
    LOG_PATH="/tmp/audit/results/kani_baseline.log"
    LOCAL_PREFIX="kani_baseline"
else
    LOG_PATH="/tmp/audit/results/kani_$HARNESS.log"
    LOCAL_PREFIX="kani_$HARNESS"
fi

echo "==> Fetching Kani artifacts for $HARNESS"

# 1. Compress the raw log on VPS (saves bandwidth significantly)
echo "  Compressing log on VPS..."
$SSH "$VPS_HOST" "
    if [[ ! -f $LOG_PATH ]]; then
        echo 'ERROR: log file $LOG_PATH does not exist on VPS' >&2
        exit 1
    fi
    gzip -k -f $LOG_PATH
    ls -lh ${LOG_PATH}.gz
"

# 2. Fetch the gzipped log
echo "  Pulling gzipped log..."
$SCP "$VPS_HOST:${LOG_PATH}.gz" "./${LOCAL_PREFIX}.log.gz"

# 3. Build summary
echo "  Generating summary..."
TOTAL=$($SSH "$VPS_HOST" "grep -c 'Checking harness' $LOG_PATH 2>/dev/null || echo 0")
PASS=$($SSH "$VPS_HOST" "grep -c 'VERIFICATION:- SUCCESSFUL' $LOG_PATH 2>/dev/null || echo 0")
FAIL=$($SSH "$VPS_HOST" "grep -c 'VERIFICATION:- FAILED' $LOG_PATH 2>/dev/null || echo 0")

cat > "./${LOCAL_PREFIX}_summary.txt" <<EOF
Kani run summary
================
Source log:     $LOG_PATH (on VPS)
Local archive:  ${LOCAL_PREFIX}.log.gz

Total harnesses checked: $TOTAL
PASS:                    $PASS
FAIL:                    $FAIL

Pulled at: $(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

echo "  Summary written to ./${LOCAL_PREFIX}_summary.txt"

# 4. For baseline runs, also build per-harness timings TSV
if [[ "$HARNESS" == "--baseline" ]]; then
    echo "  Building per-harness timings TSV..."
    $SSH "$VPS_HOST" "
        awk '
            /Checking harness/ { name=\$3; gsub(/\\.\\.\\./, \"\", name) }
            /Verification Time:/ { time=\$3; gsub(/s/, \"\", time) }
            /VERIFICATION:- SUCCESSFUL/ { print name \"\\tPASS\\t\" time }
            /VERIFICATION:- FAILED/ { print name \"\\tFAIL\\t\" time }
        ' $LOG_PATH
    " > "./${LOCAL_PREFIX}_timings.tsv"
    echo "  Per-harness timings → ./${LOCAL_PREFIX}_timings.tsv ($(wc -l < ./${LOCAL_PREFIX}_timings.tsv) entries)"
fi

echo
echo "==> Done. Artifacts:"
ls -lh "./${LOCAL_PREFIX}"* 2>/dev/null
