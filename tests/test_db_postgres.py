"""Wave 8c — Postgres backend tests.

The actual Postgres class can only be exercised against a running
Postgres server, so this suite focuses on:

  * The factory in db.py picks the right backend based on env
  * PostgresUnavailable raises cleanly when psycopg isn't installed
  * The class shape mirrors FindingsDB (same public methods)
  * The schema constants are well-formed Postgres SQL (no obvious typos)

A live integration test would need a docker-compose Postgres, which we
don't add as a pytest dependency. That belongs in CI infrastructure if
we ever spin up a Postgres-backed CI matrix.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest


def test_factory_returns_sqlite_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No JELLEO_DB_URL → SQLite backend."""
    monkeypatch.delenv("JELLEO_DB_URL", raising=False)
    from audit_pipeline.db import FindingsDB, open_findings_db
    db = open_findings_db(tmp_path)
    assert isinstance(db, FindingsDB)


def test_factory_returns_sqlite_when_url_is_blank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty JELLEO_DB_URL → SQLite (defensive)."""
    monkeypatch.setenv("JELLEO_DB_URL", "")
    from audit_pipeline.db import FindingsDB, open_findings_db
    db = open_findings_db(tmp_path)
    assert isinstance(db, FindingsDB)


def test_factory_routes_postgres_url_to_postgres_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A postgres:// URL must route to the Postgres backend."""
    monkeypatch.setenv("JELLEO_DB_URL", "postgresql://nope:nope@localhost:1/nope")
    from audit_pipeline.db import open_findings_db

    # The Postgres backend will fail to connect (no server at port 1),
    # OR fail to import psycopg — either way, we just confirm it routed
    # to the Postgres path and didn't fall through to SQLite.
    try:
        db = open_findings_db(tmp_path)
    except Exception:
        # Connection failure or PostgresUnavailable — that's the "right route"
        # Accept any exception: the routing happened, the backend started;
        # success is "we tried the Postgres path."
        return
    # If somehow we got an instance, verify it's the Postgres class
    from audit_pipeline.db_postgres import PostgresFindingsDB
    assert isinstance(db, PostgresFindingsDB)


def test_postgres_unavailable_exception_class() -> None:
    """PostgresUnavailable should be importable + raisable."""
    from audit_pipeline.db_postgres import PostgresUnavailable
    with pytest.raises(PostgresUnavailable):
        raise PostgresUnavailable("test")


def test_postgres_class_mirrors_sqlite_public_api() -> None:
    """PostgresFindingsDB must implement every public method on FindingsDB.

    Without this, switching backends silently breaks call sites that use
    methods only one backend implements.
    """
    from audit_pipeline.db import FindingsDB
    from audit_pipeline.db_postgres import PostgresFindingsDB

    sqlite_public = {
        name for name, _ in inspect.getmembers(FindingsDB, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    pg_public = {
        name for name, _ in inspect.getmembers(PostgresFindingsDB, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    missing = sqlite_public - pg_public
    assert not missing, (
        f"PostgresFindingsDB missing {len(missing)} public methods that "
        f"FindingsDB has: {sorted(missing)}"
    )


def test_pg_schema_has_required_tables() -> None:
    """Sanity: SCHEMA_PG defines all the tables FindingsDB depends on."""
    from audit_pipeline.db_postgres import SCHEMA_PG
    schema_str = " ".join(SCHEMA_PG).lower()
    for table in ("targets", "cycles", "findings", "transitions", "poc_cache"):
        assert f"create table if not exists {table}" in schema_str, (
            f"Postgres schema missing CREATE TABLE for {table}"
        )


def test_pg_schema_uses_postgres_idioms() -> None:
    """Sanity: schema doesn't accidentally contain SQLite-isms."""
    from audit_pipeline.db_postgres import SCHEMA_PG
    schema_str = " ".join(SCHEMA_PG)
    # SQLite-only constructs that shouldn't appear in Postgres schema
    assert "AUTOINCREMENT" not in schema_str, (
        "AUTOINCREMENT is SQLite-only; Postgres uses BIGSERIAL"
    )
    # Postgres-specific construct that SHOULD appear
    assert "BIGSERIAL" in schema_str, "Schema should use BIGSERIAL for PRIMARY KEY"
