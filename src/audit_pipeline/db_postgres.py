"""Postgres backend for the findings DB (Wave 8c).

Mirrors the SQLite-backed `FindingsDB` API class-for-class, method-for-method.
Switched in by setting the ``JELLEO_DB_URL`` env var to a postgres:// URL.
``audit_pipeline.db.open_findings_db()`` is the factory that picks the
right backend.

Why a parallel class instead of a unified interface refactor:
  * The SQLite code is battle-tested through every commit on `main`. A
    refactor to a shared abstraction layer carries non-trivial regression
    risk on the hot path (hunt cycle insertion + lifecycle transitions).
  * Solana protocols don't run a global Postgres dependency by default,
    so making Postgres a hard requirement would break dev workflows.
  * Postgres is for production scale (5+ active customers); SQLite stays
    fine for dev + first-customer pilot.

The schema is byte-equivalent in semantics to the SQLite schema (same
columns, same constraints, same indexes), translated to Postgres syntax:

  INTEGER PRIMARY KEY AUTOINCREMENT  ->  BIGSERIAL PRIMARY KEY
  TEXT                                ->  TEXT
  REAL                                ->  DOUBLE PRECISION
  ?  placeholders                     ->  %s placeholders
  PRAGMA table_info(t)                ->  information_schema.columns

Migration: see scripts/migrate_sqlite_to_postgres.py
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from audit_pipeline.lifecycle import Status, assert_transition
from audit_pipeline.severity import Severity

SCHEMA_PG = [
    """
    CREATE TABLE IF NOT EXISTS targets (
        id            BIGSERIAL PRIMARY KEY,
        name          TEXT UNIQUE NOT NULL,
        github_url    TEXT,
        engine_repo   TEXT,
        wrapper_repo  TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        config_json   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cycles (
        id                BIGSERIAL PRIMARY KEY,
        target_id         BIGINT NOT NULL REFERENCES targets(id),
        cycle_id          TEXT UNIQUE NOT NULL,
        engine_sha        TEXT,
        wrapper_sha       TEXT,
        started_at        TEXT NOT NULL,
        finished_at       TEXT,
        n_dispatched      INTEGER DEFAULT 0,
        n_confirmed       INTEGER DEFAULT 0,
        total_cost_usd    DOUBLE PRECISION DEFAULT 0,
        summary_json_path TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS findings (
        id              BIGSERIAL PRIMARY KEY,
        target_id       BIGINT NOT NULL REFERENCES targets(id),
        cycle_id        TEXT NOT NULL,
        hypothesis_id   TEXT NOT NULL,
        title           TEXT,
        verdict         TEXT,
        confidence      TEXT,
        severity        TEXT,
        status          TEXT NOT NULL,
        poc_path        TEXT,
        poc_fired       INTEGER DEFAULT 0,
        debate_promoted INTEGER DEFAULT 0,
        engine_sha      TEXT,
        wrapper_sha     TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        details_json    TEXT,
        bug_class       TEXT,
        UNIQUE(target_id, cycle_id, hypothesis_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transitions (
        id          BIGSERIAL PRIMARY KEY,
        finding_id  BIGINT NOT NULL REFERENCES findings(id),
        from_status TEXT,
        to_status   TEXT NOT NULL,
        reason      TEXT,
        actor       TEXT,
        ts          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS poc_cache (
        engine_sha     TEXT NOT NULL,
        hypothesis_id  TEXT NOT NULL,
        poc_hash       TEXT NOT NULL,
        outcome        TEXT NOT NULL,
        cargo_rc       INTEGER,
        elapsed_s      DOUBLE PRECISION,
        log_path       TEXT,
        n_hits         INTEGER NOT NULL DEFAULT 1,
        first_seen_at  TEXT NOT NULL,
        last_seen_at   TEXT NOT NULL,
        PRIMARY KEY(engine_sha, hypothesis_id, poc_hash)
    )
    """,
    # Hook idempotency table — mirrors the SQLite schema. Without this,
    # a re-CONFIRMED finding on the Postgres backend would double-fire
    # propagation + sibling derivation. Defect 14 fix was originally
    # SQLite-only; this brings Postgres to parity.
    """
    CREATE TABLE IF NOT EXISTS hook_runs (
        id             BIGSERIAL PRIMARY KEY,
        finding_id     BIGINT NOT NULL,
        hook_name      TEXT NOT NULL,
        started_at     TEXT NOT NULL,
        completed_at   TEXT,
        outcome        TEXT,
        error_msg      TEXT,
        UNIQUE(finding_id, hook_name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_findings_target    ON findings(target_id)",
    "CREATE INDEX IF NOT EXISTS idx_findings_status    ON findings(status)",
    "CREATE INDEX IF NOT EXISTS idx_findings_severity  ON findings(severity)",
    "CREATE INDEX IF NOT EXISTS idx_findings_bug_class ON findings(bug_class)",
    "CREATE INDEX IF NOT EXISTS idx_cycles_target      ON cycles(target_id)",
    "CREATE INDEX IF NOT EXISTS idx_poc_cache_hyp      ON poc_cache(hypothesis_id)",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PostgresUnavailable(Exception):
    """Raised when psycopg isn't installed but a Postgres URL was provided."""


class PostgresFindingsDB:
    """Postgres-backed mirror of FindingsDB.

    Public methods match FindingsDB exactly so the rest of the codebase
    can use either backend interchangeably (via the open_findings_db
    factory). Internal SQL uses %s placeholders + ON CONFLICT clauses
    instead of SQLite's ? + INSERT OR IGNORE.
    """

    def __init__(self, dsn: str):
        try:
            import psycopg  # noqa: F401
        except ImportError as e:
            raise PostgresUnavailable(
                "psycopg (psycopg3) is required for the Postgres backend. "
                "Install: pip install 'psycopg[binary]>=3.1'"
            ) from e
        self.dsn = dsn
        # Surface a path-like attribute for callers that expect db.path
        # (e.g. dashboard.py uses db.path.parent for workspace resolution).
        # For Postgres, "workspace" is set explicitly; we store None so
        # accidental .path uses surface clearly.
        self.path = None
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(self.dsn, row_factory=dict_row)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            cur = c.cursor()
            for stmt in SCHEMA_PG:
                cur.execute(stmt)
            # Versioned migrations (P3+P4 audit Defect 12 — parity with SQLite).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version     INTEGER PRIMARY KEY,
                    applied_at  TEXT    NOT NULL,
                    description TEXT
                )
                """
            )
            from audit_pipeline.db import VERSIONED_MIGRATIONS
            cur.execute("SELECT version FROM schema_version")
            applied = {row["version"] for row in cur.fetchall()}
            for version, description, statements in VERSIONED_MIGRATIONS:
                if version in applied:
                    continue
                for stmt in statements:
                    # The v1 migration creates the schema_version table —
                    # already created above. Skip CREATE TABLE for it.
                    if "CREATE TABLE" in stmt and "schema_version" in stmt:
                        continue
                    cur.execute(stmt)
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at, description) "
                    "VALUES (%s, %s, %s) ON CONFLICT (version) DO NOTHING",
                    (version, _now(), description),
                )

    def get_schema_version(self) -> int:
        """Return the highest applied schema migration version (0 if none)."""
        with self._conn() as c:
            cur = c.cursor()
            try:
                cur.execute("SELECT MAX(version) AS v FROM schema_version")
                row = cur.fetchone()
            except Exception:
                return 0
            return int(row["v"]) if row and row.get("v") is not None else 0

    # ---------- targets ----------

    def upsert_target(
        self,
        name: str,
        github_url: str | None = None,
        engine_repo: str | None = None,
        wrapper_repo: str | None = None,
        config: dict | None = None,
    ) -> int:
        now = _now()
        config_json = json.dumps(config) if config else None
        with self._conn() as c:
            cur = c.cursor()
            cur.execute("SELECT id FROM targets WHERE name = %s", (name,))
            row = cur.fetchone()
            if row:
                cur.execute(
                    """
                    UPDATE targets
                       SET github_url = COALESCE(%s, github_url),
                           engine_repo = COALESCE(%s, engine_repo),
                           wrapper_repo = COALESCE(%s, wrapper_repo),
                           config_json = COALESCE(%s, config_json),
                           updated_at = %s
                     WHERE id = %s
                    """,
                    (github_url, engine_repo, wrapper_repo, config_json, now, row["id"]),
                )
                return int(row["id"])
            cur.execute(
                """
                INSERT INTO targets (name, github_url, engine_repo, wrapper_repo,
                                     created_at, updated_at, config_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (name, github_url, engine_repo, wrapper_repo, now, now, config_json),
            )
            return int(cur.fetchone()["id"])

    def list_targets(self) -> list[dict]:
        with self._conn() as c:
            cur = c.cursor()
            cur.execute("SELECT * FROM targets ORDER BY id")
            return [dict(r) for r in cur.fetchall()]

    def get_target(self, name: str) -> dict | None:
        with self._conn() as c:
            cur = c.cursor()
            cur.execute("SELECT * FROM targets WHERE name = %s", (name,))
            row = cur.fetchone()
            return dict(row) if row else None

    # ---------- cycles ----------

    def insert_cycle(
        self,
        target_id: int,
        cycle_id: str,
        engine_sha: str | None = None,
        wrapper_sha: str | None = None,
        n_dispatched: int = 0,
        summary_json_path: str | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.cursor()
            cur.execute(
                """
                INSERT INTO cycles (target_id, cycle_id, engine_sha, wrapper_sha,
                                     started_at, n_dispatched, summary_json_path)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (cycle_id) DO UPDATE SET
                    n_dispatched = EXCLUDED.n_dispatched,
                    summary_json_path = EXCLUDED.summary_json_path
                RETURNING id
                """,
                (target_id, cycle_id, engine_sha, wrapper_sha, _now(),
                 n_dispatched, summary_json_path),
            )
            return int(cur.fetchone()["id"])

    def finish_cycle(
        self,
        cycle_id: str,
        n_confirmed: int = 0,
        total_cost_usd: float = 0.0,
    ) -> None:
        with self._conn() as c:
            c.cursor().execute(
                """
                UPDATE cycles
                   SET finished_at = %s, n_confirmed = %s, total_cost_usd = %s
                 WHERE cycle_id = %s
                """,
                (_now(), n_confirmed, total_cost_usd, cycle_id),
            )

    def mark_cycle_finished(
        self,
        cycle_id: str,
        finished_at: str | None = None,
    ) -> bool:
        """Set finished_at on a cycle without disturbing other columns.

        Used by ``audit-pipeline cycle finish`` to retroactively close
        out cycles killed mid-run.
        """
        ts = finished_at or _now()
        with self._conn() as c:
            cur = c.cursor()
            cur.execute(
                "UPDATE cycles SET finished_at = %s "
                "WHERE cycle_id = %s AND finished_at IS NULL",
                (ts, cycle_id),
            )
            return cur.rowcount > 0

    def get_cycle(self, cycle_id: str) -> dict | None:
        with self._conn() as c:
            cur = c.cursor()
            cur.execute("SELECT * FROM cycles WHERE cycle_id = %s", (cycle_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def list_cycles(self, target_id: int | None = None, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            cur = c.cursor()
            if target_id is not None:
                cur.execute(
                    "SELECT * FROM cycles WHERE target_id = %s ORDER BY started_at DESC LIMIT %s",
                    (target_id, limit),
                )
            else:
                cur.execute("SELECT * FROM cycles ORDER BY started_at DESC LIMIT %s", (limit,))
            return [dict(r) for r in cur.fetchall()]

    # ---------- findings ----------

    def upsert_finding(
        self,
        target_id: int,
        cycle_id: str,
        hypothesis_id: str,
        title: str | None = None,
        verdict: str | None = None,
        confidence: str | None = None,
        severity: Severity = Severity.INFO,
        status: Status = Status.NEW,
        poc_path: str | None = None,
        poc_fired: bool = False,
        debate_promoted: bool = False,
        engine_sha: str | None = None,
        wrapper_sha: str | None = None,
        details: dict | None = None,
        bug_class: str | None = None,
    ) -> int:
        now = _now()
        details_json = json.dumps(details) if details else None
        with self._conn() as c:
            cur = c.cursor()
            cur.execute(
                """
                INSERT INTO findings (target_id, cycle_id, hypothesis_id, title, verdict,
                                       confidence, severity, status, poc_path, poc_fired,
                                       debate_promoted, engine_sha, wrapper_sha, created_at,
                                       updated_at, details_json, bug_class)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (target_id, cycle_id, hypothesis_id) DO UPDATE SET
                    title           = EXCLUDED.title,
                    verdict         = EXCLUDED.verdict,
                    confidence      = EXCLUDED.confidence,
                    severity        = EXCLUDED.severity,
                    status          = EXCLUDED.status,
                    poc_path        = EXCLUDED.poc_path,
                    poc_fired       = EXCLUDED.poc_fired,
                    debate_promoted = EXCLUDED.debate_promoted,
                    engine_sha      = EXCLUDED.engine_sha,
                    wrapper_sha     = EXCLUDED.wrapper_sha,
                    updated_at      = EXCLUDED.updated_at,
                    details_json    = EXCLUDED.details_json,
                    bug_class       = EXCLUDED.bug_class
                RETURNING id
                """,
                (target_id, cycle_id, hypothesis_id, title, verdict, confidence,
                 severity.value, status.value, poc_path, int(poc_fired),
                 int(debate_promoted), engine_sha, wrapper_sha, now, now,
                 details_json, bug_class),
            )
            return int(cur.fetchone()["id"])

    def transition_finding(
        self,
        finding_id: int,
        to_status: Status,
        reason: str = "",
        actor: str = "system",
        run_hooks: bool = True,
    ) -> None:
        with self._conn() as c:
            cur = c.cursor()
            cur.execute("SELECT status FROM findings WHERE id = %s", (finding_id,))
            row = cur.fetchone()
            if not row:
                return
            current = Status(row["status"])
            assert_transition(current, to_status)
            now = _now()
            cur.execute(
                "UPDATE findings SET status = %s, updated_at = %s WHERE id = %s",
                (to_status.value, now, finding_id),
            )
            cur.execute(
                """
                INSERT INTO transitions (finding_id, from_status, to_status, reason, actor, ts)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (finding_id, current.value, to_status.value, reason, actor, now),
            )
        # Hooks fire after commit, same pattern as SQLite class
        if run_hooks and to_status == Status.CONFIRMED:
            self._fire_confirmed_hooks(finding_id)

    def _fire_confirmed_hooks(self, finding_id: int) -> None:
        """Hook fanout. Same shape as FindingsDB._fire_confirmed_hooks but
        Postgres-aware. The hook targets need a Path-like workspace; for
        Postgres mode, set the `workspace_path` attribute on the instance
        before calling, or rely on the JELLEO_WORKSPACE env var.
        """
        import os
        import threading
        from pathlib import Path
        ws_str = os.environ.get("JELLEO_WORKSPACE", "")
        ws = Path(ws_str) if ws_str else Path(".")
        from audit_pipeline.db import _derive_target, _propagate_target
        threading.Thread(
            target=_run_one_hook_pg,
            args=(ws, finding_id, "derive_siblings", _derive_target),
            daemon=True,
        ).start()
        threading.Thread(
            target=_run_one_hook_pg,
            args=(ws, finding_id, "propagate", _propagate_target),
            daemon=True,
        ).start()

    def get_finding(self, finding_id: int) -> dict | None:
        with self._conn() as c:
            cur = c.cursor()
            cur.execute("SELECT * FROM findings WHERE id = %s", (finding_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def list_findings(
        self,
        target_id: int | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        with self._conn() as c:
            cur = c.cursor()
            clauses, params = [], []
            if target_id is not None:
                clauses.append("target_id = %s")
                params.append(target_id)
            if status:
                clauses.append("status = %s")
                params.append(status)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(limit)
            cur.execute(
                f"SELECT * FROM findings {where} ORDER BY updated_at DESC LIMIT %s",
                tuple(params),
            )
            return [dict(r) for r in cur.fetchall()]

    def list_findings_by_cycle(self, cycle_id: str) -> list[dict]:
        """Return every finding belonging to a single cycle.

        Mirror of FindingsDB.list_findings_by_cycle — used by Merkle
        root computation to avoid silent truncation under list_findings's
        default limit.
        """
        with self._conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT * FROM findings WHERE cycle_id = %s ORDER BY id ASC",
                (cycle_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def list_confirmed_findings_by_bug_class(self, bug_class: str) -> list[dict]:
        with self._conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT * FROM findings WHERE status='confirmed' AND bug_class=%s "
                "ORDER BY updated_at DESC",
                (bug_class,),
            )
            return [dict(r) for r in cur.fetchall()]

    def transitions_for(self, finding_id: int) -> list[dict]:
        with self._conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT * FROM transitions WHERE finding_id = %s ORDER BY ts",
                (finding_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def stats(self) -> dict:
        with self._conn() as c:
            cur = c.cursor()
            cur.execute("SELECT COUNT(*) AS n FROM targets")
            n_targets = int(cur.fetchone()["n"] or 0)
            cur.execute("SELECT COUNT(*) AS n FROM cycles")
            n_cycles = int(cur.fetchone()["n"] or 0)
            cur.execute("SELECT COUNT(*) AS n FROM findings")
            n_findings = int(cur.fetchone()["n"] or 0)
        return {"n_targets": n_targets, "n_cycles": n_cycles, "n_findings": n_findings}

    # ---------- poc cache ----------

    def get_poc_cache(self, engine_sha: str, hypothesis_id: str, poc_hash: str) -> dict | None:
        with self._conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT * FROM poc_cache WHERE engine_sha=%s AND hypothesis_id=%s AND poc_hash=%s",
                (engine_sha, hypothesis_id, poc_hash),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                "UPDATE poc_cache SET n_hits = n_hits + 1, last_seen_at = %s "
                "WHERE engine_sha=%s AND hypothesis_id=%s AND poc_hash=%s",
                (_now(), engine_sha, hypothesis_id, poc_hash),
            )
            return dict(row)

    def put_poc_cache(
        self,
        engine_sha: str,
        hypothesis_id: str,
        poc_hash: str,
        outcome: str,
        cargo_rc: int | None = None,
        elapsed_s: float | None = None,
        log_path: str | None = None,
    ) -> None:
        now = _now()
        with self._conn() as c:
            c.cursor().execute(
                """
                INSERT INTO poc_cache (engine_sha, hypothesis_id, poc_hash, outcome,
                                       cargo_rc, elapsed_s, log_path, n_hits,
                                       first_seen_at, last_seen_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s)
                ON CONFLICT (engine_sha, hypothesis_id, poc_hash) DO UPDATE SET
                    outcome      = EXCLUDED.outcome,
                    cargo_rc     = EXCLUDED.cargo_rc,
                    elapsed_s    = EXCLUDED.elapsed_s,
                    log_path     = EXCLUDED.log_path,
                    last_seen_at = EXCLUDED.last_seen_at
                """,
                (engine_sha, hypothesis_id, poc_hash, outcome, cargo_rc, elapsed_s,
                 log_path, now, now),
            )

    def list_poc_cache(self, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            cur = c.cursor()
            cur.execute("SELECT * FROM poc_cache ORDER BY last_seen_at DESC LIMIT %s", (limit,))
            return [dict(r) for r in cur.fetchall()]

    def flush_poc_cache(
        self,
        engine_sha: str | None = None,
        hypothesis_id: str | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.cursor()
            clauses, params = [], []
            if engine_sha:
                clauses.append("engine_sha = %s")
                params.append(engine_sha)
            if hypothesis_id:
                clauses.append("hypothesis_id = %s")
                params.append(hypothesis_id)
            sql = "DELETE FROM poc_cache"
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            cur.execute(sql, tuple(params))
            return cur.rowcount


def _run_one_hook_pg(workspace, finding_id: int, hook_name: str, target) -> None:
    """Postgres equivalent of FindingsDB._run_one_hook (writes to <ws>/hooks/).

    POST-AUDIT FIX: now claims `hook_runs(finding_id, hook_name)` in the
    Postgres backend before executing — mirrors the SQLite-side Defect 14
    fix. Without this, a re-CONFIRMED finding double-fires propagation
    and sibling derivation on the Postgres backend.
    """
    import json as _json
    import os as _os
    import traceback as _tb
    from datetime import datetime, timezone

    try:
        hooks_dir = workspace / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = hooks_dir / f"{finding_id}-{hook_name}-{ts}.log"

    def _write(record: dict) -> None:
        try:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(_json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            pass

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Idempotency claim — same logic as SQLite path.
    dsn = _os.environ.get("JELLEO_DB_URL", "").strip()
    pg_conn = None
    if dsn:
        try:
            import psycopg
            pg_conn = psycopg.connect(dsn, autocommit=True)
            cur = pg_conn.cursor()
            cur.execute(
                "SELECT outcome FROM hook_runs WHERE finding_id = %s AND hook_name = %s",
                (finding_id, hook_name),
            )
            existing = cur.fetchone()
            if existing and existing[0] == "ok":
                pg_conn.close()
                return  # already succeeded — skip silently
            if existing:
                cur.execute(
                    "DELETE FROM hook_runs WHERE finding_id = %s AND hook_name = %s",
                    (finding_id, hook_name),
                )
            cur.execute(
                "INSERT INTO hook_runs (finding_id, hook_name, started_at) "
                "VALUES (%s, %s, %s) ON CONFLICT (finding_id, hook_name) DO NOTHING",
                (finding_id, hook_name, started),
            )
            cur.close()
        except Exception:  # noqa: BLE001 — never crash hook over idempotency
            if pg_conn:
                try: pg_conn.close()
                except Exception: pass
            pg_conn = None

    _write({"phase": "started", "ts": started, "finding_id": finding_id,
            "hook": hook_name, "backend": "postgres"})

    try:
        target(workspace, finding_id)
        completed = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write({"phase": "completed", "ts": completed,
                "finding_id": finding_id, "hook": hook_name, "outcome": "ok"})
        if pg_conn:
            try:
                cur = pg_conn.cursor()
                cur.execute(
                    "UPDATE hook_runs SET completed_at = %s, outcome = %s "
                    "WHERE finding_id = %s AND hook_name = %s",
                    (completed, "ok", finding_id, hook_name),
                )
                cur.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        completed = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write({"phase": "failed", "ts": completed,
                "finding_id": finding_id, "hook": hook_name, "outcome": "error",
                "error_type": type(e).__name__, "error_msg": str(e)[:500],
                "traceback": _tb.format_exc()[-2000:]})
        if pg_conn:
            try:
                cur = pg_conn.cursor()
                cur.execute(
                    "UPDATE hook_runs SET completed_at = %s, outcome = %s, error_msg = %s "
                    "WHERE finding_id = %s AND hook_name = %s",
                    (completed, "error", str(e)[:500], finding_id, hook_name),
                )
                cur.close()
            except Exception:  # noqa: BLE001
                pass
    finally:
        if pg_conn:
            try: pg_conn.close()
            except Exception: pass
