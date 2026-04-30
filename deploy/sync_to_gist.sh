#!/usr/bin/env bash
# Sync the live shadow + watch logs to a public GitHub Gist every hour.
# Run via cron on the VPS:
#   0 * * * * /home/audit/audit-pipeline-cli/deploy/sync_to_gist.sh
#
# Requires:
#   - GH_TOKEN env var with `gist` scope (or `gh auth login` already done)
#   - GIST_ID env var pointing at the Gist to update
#   - WORKSPACE env var pointing at the audit workspace dir

set -euo pipefail

WORKSPACE="${WORKSPACE:-$HOME/audit_runs/percolator-live}"
GIST_ID="${GIST_ID:?GIST_ID env var required}"
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

ALERTS_PATH="$WORKSPACE/shadow/alerts.jsonl"
WATCH_LOG_PATH="$WORKSPACE/watch/watch.log"
SHADOW_LOG_PATH="$WORKSPACE/shadow/poll.log"
STATE_PATH="$WORKSPACE/shadow/state.json"

# Build the public summary
SUMMARY_FILE=$(mktemp)
cat <<EOF >"$SUMMARY_FILE"
# Jelleo — Live operational status
**Last updated:** $TS
**Target:** Percolator perpetual DEX (mainnet program \`6qWZvUtfyShbxTQkwjCayk3LuGqTGJwBo2QfkePK5jdJ\`)

## Snapshot

EOF

# Source-watch: how many commits tracked + most recent
if [[ -f "$STATE_PATH" ]]; then
  echo "### Source-code watch state" >>"$SUMMARY_FILE"
  echo '```json' >>"$SUMMARY_FILE"
  cat "$STATE_PATH" >>"$SUMMARY_FILE"
  echo '```' >>"$SUMMARY_FILE"
  echo "" >>"$SUMMARY_FILE"
fi

# Alerts: count + recent
if [[ -f "$ALERTS_PATH" ]]; then
  ALERT_COUNT=$(wc -l <"$ALERTS_PATH" || echo 0)
  echo "### Alerts" >>"$SUMMARY_FILE"
  echo "**Total alerts logged:** $ALERT_COUNT" >>"$SUMMARY_FILE"
  echo "" >>"$SUMMARY_FILE"
  if [[ "$ALERT_COUNT" -gt 0 ]]; then
    echo "Most recent (up to 10):" >>"$SUMMARY_FILE"
    echo '```json' >>"$SUMMARY_FILE"
    tail -n 10 "$ALERTS_PATH" >>"$SUMMARY_FILE"
    echo '```' >>"$SUMMARY_FILE"
  else
    echo "_No alerts in window. Pipeline polling normally._" >>"$SUMMARY_FILE"
  fi
  echo "" >>"$SUMMARY_FILE"
fi

# Source-watch log tail
if [[ -f "$WATCH_LOG_PATH" ]]; then
  echo "### Source-watch log (last 20 lines)" >>"$SUMMARY_FILE"
  echo '```' >>"$SUMMARY_FILE"
  tail -n 20 "$WATCH_LOG_PATH" >>"$SUMMARY_FILE"
  echo '```' >>"$SUMMARY_FILE"
  echo "" >>"$SUMMARY_FILE"
fi

# Shadow poll log tail
if [[ -f "$SHADOW_LOG_PATH" ]]; then
  echo "### Shadow poll log (last 20 lines)" >>"$SUMMARY_FILE"
  echo '```' >>"$SUMMARY_FILE"
  tail -n 20 "$SHADOW_LOG_PATH" >>"$SUMMARY_FILE"
  echo '```' >>"$SUMMARY_FILE"
  echo "" >>"$SUMMARY_FILE"
fi

cat <<EOF >>"$SUMMARY_FILE"

---
*Generated automatically by Jelleo pipeline. Updates every hour.*
*Methodology: https://github.com/Copenhagen0x/solana-audit-pipeline*
*CLI: https://github.com/Copenhagen0x/audit-pipeline-cli*
EOF

# Update the gist
gh gist edit "$GIST_ID" -f "STATUS.md" "$SUMMARY_FILE"

rm -f "$SUMMARY_FILE"
echo "[$TS] Gist $GIST_ID updated."
