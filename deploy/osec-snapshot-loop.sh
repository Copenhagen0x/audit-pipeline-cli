#!/bin/bash
# Live snapshot loop. Writes:
#   /var/www/jelleo.com/customer/<id>/manifest.json   (via audit-pipeline dashboard)
#   /var/www/jelleo.com/customer/ottersec/heartbeat.json   (via osec-heartbeat-writer.py)
# Both are read by the customer dashboard. Loop fires every 5s.
#
# Hardening:
#   * `set -uo pipefail` — fail fast on undefined vars + pipeline errors.
#   * Atomic writes via tmp + mv. The old form `python writer.py > heartbeat.json`
#     truncates the destination FIRST then streams, so an nginx fetch
#     during the ~20-50ms write window can return empty / partial JSON
#     and the dashboard crashes on JSON.parse.
#   * Per-iteration error tolerance: a single failure does NOT kill
#     the loop — we log + continue.

set -uo pipefail
source /root/.audit-env

DASHBOARD_TMP=/tmp/osec-snapshot.log
HEARTBEAT_PATH=/var/www/jelleo.com/customer/ottersec/heartbeat.json
HEARTBEAT_TMP=${HEARTBEAT_PATH}.tmp

while true; do
  # 1. Customer manifest — audit-pipeline dashboard handles its own
  #    atomic write internally (or should — see dashboard.py).
  audit-pipeline --workspace /root/audit_runs/ottersec-eval dashboard \
    --snapshot-json /tmp/osec-snapshot.json \
    --customer-manifest-dir /var/www/jelleo.com/customer/ \
    > "$DASHBOARD_TMP" 2>&1 || true

  # 2. Heartbeat — explicit tmp + rename. mv is atomic on the same
  #    filesystem (POSIX rename(2)), so nginx never sees a partial
  #    write. If the writer crashes mid-run, the existing
  #    heartbeat.json is left untouched (stale beats empty).
  if python3 /root/osec-heartbeat-writer.py > "$HEARTBEAT_TMP" 2>/tmp/osec-heartbeat-writer.err; then
    mv -f "$HEARTBEAT_TMP" "$HEARTBEAT_PATH"
  else
    # writer crashed — keep the previous heartbeat.json alive,
    # log the error so an operator can see it
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') heartbeat-writer crashed:" \
      >> /var/log/osec-snapshot-loop.log
    cat /tmp/osec-heartbeat-writer.err \
      >> /var/log/osec-snapshot-loop.log
    rm -f "$HEARTBEAT_TMP"
  fi

  sleep 5
done
