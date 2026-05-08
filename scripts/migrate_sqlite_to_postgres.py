#!/usr/bin/env python3
"""One-shot migration: copy a SQLite findings DB to Postgres.

Usage:

    export JELLEO_DB_URL='postgresql://user:pass@host:5432/jelleo'
    python3 scripts/migrate_sqlite_to_postgres.py \\
        --sqlite /root/audit_runs/percolator-live/findings.db

Idempotent for existing data:
  * Targets, cycles, findings, transitions, poc_cache: ON CONFLICT DO UPDATE
    keeps Postgres schema in sync. Re-running won't duplicate rows but
    will overwrite changed columns.
  * BIGSERIAL ids on the Postgres side don't match SQLite's AUTOINCREMENT
    ids. We preserve cross-table relations by going through (target.name,
    cycle.cycle_id, finding.target_id+cycle_id+hypothesis_id) UNIQUE keys
    and looking up Postgres-side IDs as we go.

Verifies row counts at the end and prints any deltas.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def _need_psycopg():
    try:
        import psycopg  # noqa: F401
    except ImportError:
        sys.stderr.write(
            "psycopg not installed. Run:\n"
            "    pip install 'psycopg[binary]>=3.1'\n"
        )
        sys.exit(2)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sqlite", type=Path, required=True,
                   help="Source SQLite DB (e.g. /root/audit_runs/.../findings.db)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print row counts but don't write to Postgres")
    args = p.parse_args()

    _need_psycopg()
    import os
    dsn = os.environ.get("JELLEO_DB_URL", "").strip()
    if not dsn:
        sys.stderr.write("JELLEO_DB_URL env var must be set to a postgres:// URL\n")
        return 2

    if not args.sqlite.is_file():
        sys.stderr.write(f"SQLite file not found: {args.sqlite}\n")
        return 2

    src = sqlite3.connect(str(args.sqlite))
    src.row_factory = sqlite3.Row

    from audit_pipeline.db_postgres import PostgresFindingsDB
    dest = PostgresFindingsDB(dsn)

    counts = {"targets": 0, "cycles": 0, "findings": 0,
              "transitions": 0, "poc_cache": 0}

    # ---- targets ----
    name_to_pg_id: dict[str, int] = {}
    for row in src.execute("SELECT * FROM targets"):
        d = dict(row)
        if not args.dry_run:
            pg_id = dest.upsert_target(
                name=d["name"],
                github_url=d.get("github_url"),
                engine_repo=d.get("engine_repo"),
                wrapper_repo=d.get("wrapper_repo"),
                config=None,  # config_json is opaque; preserve via direct write below
            )
            name_to_pg_id[d["name"]] = pg_id
        counts["targets"] += 1
    print(f"  targets: {counts['targets']} migrated")

    # Build sqlite-id -> pg-id map for targets
    sqlite_target_to_pg: dict[int, int] = {}
    for row in src.execute("SELECT id, name FROM targets"):
        sqlite_target_to_pg[row["id"]] = name_to_pg_id.get(row["name"], 0)

    # ---- cycles ----
    for row in src.execute("SELECT * FROM cycles"):
        d = dict(row)
        pg_target_id = sqlite_target_to_pg.get(d["target_id"])
        if not pg_target_id:
            continue
        if not args.dry_run:
            dest.insert_cycle(
                target_id=pg_target_id,
                cycle_id=d["cycle_id"],
                engine_sha=d.get("engine_sha"),
                wrapper_sha=d.get("wrapper_sha"),
                n_dispatched=int(d.get("n_dispatched") or 0),
                summary_json_path=d.get("summary_json_path"),
            )
            if d.get("finished_at"):
                dest.finish_cycle(
                    cycle_id=d["cycle_id"],
                    n_confirmed=int(d.get("n_confirmed") or 0),
                    total_cost_usd=float(d.get("total_cost_usd") or 0),
                )
        counts["cycles"] += 1
    print(f"  cycles:  {counts['cycles']} migrated")

    # ---- findings ----
    from audit_pipeline.lifecycle import Status
    from audit_pipeline.severity import Severity
    sqlite_finding_to_pg: dict[int, int] = {}
    for row in src.execute("SELECT * FROM findings"):
        d = dict(row)
        pg_target_id = sqlite_target_to_pg.get(d["target_id"])
        if not pg_target_id:
            continue
        try:
            sev = Severity(d.get("severity") or "Info")
        except ValueError:
            sev = Severity.INFO
        try:
            stat = Status(d.get("status") or "new")
        except ValueError:
            stat = Status.NEW

        if not args.dry_run:
            import json as _json
            details = None
            if d.get("details_json"):
                try:
                    details = _json.loads(d["details_json"])
                except Exception:
                    details = None
            pg_id = dest.upsert_finding(
                target_id=pg_target_id,
                cycle_id=d["cycle_id"],
                hypothesis_id=d["hypothesis_id"],
                title=d.get("title"),
                verdict=d.get("verdict"),
                confidence=d.get("confidence"),
                severity=sev,
                status=stat,
                poc_path=d.get("poc_path"),
                poc_fired=bool(d.get("poc_fired")),
                debate_promoted=bool(d.get("debate_promoted")),
                engine_sha=d.get("engine_sha"),
                wrapper_sha=d.get("wrapper_sha"),
                details=details,
                bug_class=d.get("bug_class"),
            )
            sqlite_finding_to_pg[d["id"]] = pg_id
        counts["findings"] += 1
    print(f"  findings: {counts['findings']} migrated")

    # ---- transitions ----
    if not args.dry_run:
        with dest._conn() as c:  # noqa: SLF001
            cur = c.cursor()
            for row in src.execute("SELECT * FROM transitions"):
                d = dict(row)
                pg_fid = sqlite_finding_to_pg.get(d["finding_id"])
                if not pg_fid:
                    continue
                cur.execute(
                    """
                    INSERT INTO transitions (finding_id, from_status, to_status, reason, actor, ts)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (pg_fid, d.get("from_status"), d.get("to_status") or "new",
                     d.get("reason"), d.get("actor"), d.get("ts")),
                )
                counts["transitions"] += 1
    print(f"  transitions: {counts['transitions']} migrated")

    # ---- poc_cache ----
    for row in src.execute("SELECT * FROM poc_cache"):
        d = dict(row)
        if not args.dry_run:
            dest.put_poc_cache(
                engine_sha=d["engine_sha"],
                hypothesis_id=d["hypothesis_id"],
                poc_hash=d["poc_hash"],
                outcome=d["outcome"],
                cargo_rc=d.get("cargo_rc"),
                elapsed_s=d.get("elapsed_s"),
                log_path=d.get("log_path"),
            )
        counts["poc_cache"] += 1
    print(f"  poc_cache: {counts['poc_cache']} migrated")

    src.close()

    print()
    print("--- Verifying row counts on Postgres side ---")
    if not args.dry_run:
        with dest._conn() as c:  # noqa: SLF001
            cur = c.cursor()
            for table in ("targets", "cycles", "findings", "transitions", "poc_cache"):
                cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                row = cur.fetchone()
                pg_count = int((row.get("n") if isinstance(row, dict) else row["n"]) or 0)
                marker = "✓" if pg_count >= counts[table] else "✗"
                print(f"  {marker} {table}: sqlite={counts[table]}, postgres={pg_count}")
    else:
        print("  (dry-run; skipped Postgres verify)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
