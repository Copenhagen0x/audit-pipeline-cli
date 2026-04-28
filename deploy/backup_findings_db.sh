#!/usr/bin/env bash
# Daily backup of the findings DB. Keeps `BACKUPS_KEEP` rolling copies
# in /root/audit_runs/percolator-live/backups/. Cheap to run daily.
#
# Wire into cron or a systemd timer. The script is idempotent and
# safe to run any number of times per day; only date-stamped output.

set -euo pipefail

WORKSPACE="${WORKSPACE:-/root/audit_runs/percolator-live}"
DB_PATH="$WORKSPACE/findings.db"
BACKUP_DIR="$WORKSPACE/backups"
BACKUPS_KEEP="${BACKUPS_KEEP:-30}"

if [[ ! -f "$DB_PATH" ]]; then
    echo "no findings.db at $DB_PATH; nothing to back up"
    exit 0
fi

mkdir -p "$BACKUP_DIR"
DATE=$(date -u +%Y%m%d)
DEST="$BACKUP_DIR/findings.${DATE}.db"

# Use sqlite3 .backup which is safe under live writers
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB_PATH" ".backup '$DEST'"
else
    cp "$DB_PATH" "$DEST"
fi

# Also dump schema + stats for human inspection
{
    echo "# Backup taken $(date -u -Iseconds)"
    echo "# DB: $DB_PATH"
    echo
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$DB_PATH" ".schema"
        echo
        echo "# Counts:"
        sqlite3 "$DB_PATH" "SELECT 'targets:    ' || COUNT(*) FROM targets;"
        sqlite3 "$DB_PATH" "SELECT 'cycles:     ' || COUNT(*) FROM cycles;"
        sqlite3 "$DB_PATH" "SELECT 'findings:   ' || COUNT(*) FROM findings;"
        sqlite3 "$DB_PATH" "SELECT 'transitions:' || COUNT(*) FROM transitions;"
    fi
} > "$BACKUP_DIR/findings.${DATE}.summary.txt"

echo "wrote $DEST"
echo "wrote $BACKUP_DIR/findings.${DATE}.summary.txt"

# Trim old backups (keep BACKUPS_KEEP newest)
ls -1t "$BACKUP_DIR"/findings.*.db 2>/dev/null \
    | tail -n +"$((BACKUPS_KEEP + 1))" \
    | xargs -r rm -v
ls -1t "$BACKUP_DIR"/findings.*.summary.txt 2>/dev/null \
    | tail -n +"$((BACKUPS_KEEP + 1))" \
    | xargs -r rm -v

echo "backup complete; retaining $BACKUPS_KEEP newest copies"
