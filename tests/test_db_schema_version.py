"""Tests for the versioned schema_version migration system.

Closes the P3+P4 audit Defect 12 (MED) — until now findings.db had no
way to record which schema migrations have run. Re-running ALTER TABLE
on a column that already exists is a SQLite error, so the legacy column
migration list grew unwieldy and any "data migration" (backfill,
rename, split) had no safe place to live.

Tests:
  - fresh DB: schema_version table exists, baseline version recorded
  - second open of same DB: doesn't re-apply (idempotent)
  - get_schema_version: returns the highest version
  - legacy DB without schema_version: gets initialized on first open
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def test_fresh_db_initializes_schema_version(tmp_path: Path) -> None:
    from audit_pipeline.db import FindingsDB
    db = FindingsDB(tmp_path / "findings.db")
    # The base version (1) should be recorded
    assert db.get_schema_version() >= 1


def test_schema_version_table_exists_after_init(tmp_path: Path) -> None:
    from audit_pipeline.db import FindingsDB
    FindingsDB(tmp_path / "findings.db")
    conn = sqlite3.connect(str(tmp_path / "findings.db"))
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='schema_version'"
    ))
    conn.close()
    assert len(rows) == 1


def test_reopening_db_is_idempotent(tmp_path: Path) -> None:
    """Opening the DB a second time must NOT re-apply migrations."""
    from audit_pipeline.db import FindingsDB
    FindingsDB(tmp_path / "findings.db")
    # First open: version 1 recorded. Read row count.
    conn = sqlite3.connect(str(tmp_path / "findings.db"))
    n_rows_first = conn.execute(
        "SELECT COUNT(*) FROM schema_version"
    ).fetchone()[0]
    conn.close()

    # Second open: should not insert duplicates
    FindingsDB(tmp_path / "findings.db")
    conn = sqlite3.connect(str(tmp_path / "findings.db"))
    n_rows_second = conn.execute(
        "SELECT COUNT(*) FROM schema_version"
    ).fetchone()[0]
    conn.close()

    assert n_rows_second == n_rows_first


def test_legacy_db_without_schema_version_gets_initialized(tmp_path: Path) -> None:
    """A pre-migration DB (no schema_version table) must be brought
    up to current version on the next open."""
    db_path = tmp_path / "findings.db"
    # Hand-craft a legacy DB shape: targets/cycles/findings/transitions
    # but no schema_version table.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            github_url TEXT,
            engine_repo TEXT,
            wrapper_repo TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            config_json TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    # Open via FindingsDB — should add schema_version and record version 1.
    from audit_pipeline.db import FindingsDB
    db = FindingsDB(db_path)
    assert db.get_schema_version() >= 1


def test_versioned_migrations_list_is_monotonic() -> None:
    """Each migration version must be strictly greater than the previous —
    a stale duplicate version would silently mask a real migration."""
    from audit_pipeline.db import VERSIONED_MIGRATIONS
    versions = [v for v, _, _ in VERSIONED_MIGRATIONS]
    assert versions == sorted(set(versions))
    assert len(versions) == len(set(versions)), "duplicate migration versions"
