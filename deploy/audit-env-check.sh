#!/bin/bash
# audit-env-check.sh — fail-fast pre-start validator for /root/.audit-env.
#
# Wired as ExecStartPre= on every systemd service that reads .audit-env
# (jelleo-watch, jelleo-shadow, jelleo-autoupdate, jelleo-scheduler-*,
# jelleo-heartbeat, jelleo-snapshot, jelleo-backup, jelleo-corpus-refresh).
#
# What it catches:
#   1. `export KEY=value` lines — systemd's EnvironmentFile parser
#      rejects these AND logs the WHOLE LINE (with the value) to journal
#      under "Ignoring invalid environment assignment". That dumped all
#      our credentials into journalctl twice this session before we
#      caught the pattern.
#   2. Unquoted shell metacharacters in values — would parse-error in
#      bash `source` (e.g. `JELLEO_SMTP_FROM=Name <addr@host>` because
#      bash treats `<` as redirection).
#
# Format spec for /root/.audit-env:
#   * One KEY=value per line, NO `export ` prefix.
#   * Values containing whitespace OR shell metachars MUST be quoted
#     with double quotes: `KEY="value with spaces and <chars>"`.
#   * File permissions MUST be 600 (root only).
#
# This script is idempotent + fast (~5ms). Add it as the FIRST
# ExecStartPre= line of any new service that sources the file.
#
# Exit codes:
#   0 — clean
#   1 — file missing
#   2 — has `export ` prefix (systemd would leak values)
#   3 — has unquoted shell metachar in value (bash source would error)
#   4 — wrong file permissions
set -e
F=/root/.audit-env
if [ ! -f "$F" ]; then
    echo 'audit-env-check: MISSING .audit-env' >&2
    exit 1
fi
perms=$(stat -c '%a' "$F")
if [ "$perms" != '600' ]; then
    echo "audit-env-check: REJECT — perms $perms (must be 600)" >&2
    exit 4
fi
# Reject any line starting with `export `.
if grep -qE '^export ' "$F"; then
    echo 'audit-env-check: REJECT — `export` prefix on a line (systemd would leak values on parse error)' >&2
    exit 2
fi
# Reject unquoted shell-metachar values.
if grep -qE '^[A-Z_]+=[^"$]*[<>|&;\\(\\)]' "$F"; then
    echo 'audit-env-check: REJECT — unquoted shell metachar in value (would parse-error in bash source)' >&2
    exit 3
fi
exit 0
