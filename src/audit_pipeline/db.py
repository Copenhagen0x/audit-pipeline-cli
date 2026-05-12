"""SQLite findings database.

Single source of truth for every target, cycle, and finding the pipeline
has ever produced. Lives at `<workspace>/findings.db` by default.

Schema (4 tables):
  targets      — every program being audited (one row per target)
  cycles       — every hunt cycle we've ever run
  findings     — every hypothesis verdict + evidence
  transitions  — append-only log of every status change on a finding

The DB is intentionally simple: no ORM, no migrations framework. Schema
is created idempotently on every connection. Adding a column means a
new ALTER TABLE in `_init_schema`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audit_pipeline.lifecycle import Status, assert_transition
from audit_pipeline.severity import Severity

# Hook target wrappers — module-level so they can be passed by reference
# into _run_one_hook without importing inside the hot path. Each wrapper
# imports its module lazily (avoiding circular imports at startup).

def _derive_target(workspace: Path, finding_id: int) -> None:
    from audit_pipeline.commands.derive_siblings import derive_siblings_async
    derive_siblings_async(workspace, finding_id)


def _propagate_target(workspace: Path, finding_id: int) -> None:
    from audit_pipeline.commands.propagate import propagate_from_finding_async
    propagate_from_finding_async(workspace, finding_id)


def open_findings_db(workspace_path: Path) -> Any:
    """Factory: pick SQLite or Postgres backend based on JELLEO_DB_URL.

    Wave 8c. If the env var is set to a postgres:// URL, use the Postgres
    backend (PostgresFindingsDB). Otherwise SQLite at <workspace>/findings.db
    — the long-standing default. The returned object exposes the same
    public API regardless of backend.

    Why this lives in db.py rather than a separate factory module:
    callers already import FindingsDB; making the factory available
    alongside it preserves muscle memory and avoids touching every
    call site.
    """
    import os
    url = os.environ.get("JELLEO_DB_URL", "").strip()
    if url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
        # Lazy-import so the SQLite-only dev path doesn't pay the import
        # cost (and doesn't crash if psycopg isn't installed)
        from audit_pipeline.db_postgres import PostgresFindingsDB
        return PostgresFindingsDB(url)
    return FindingsDB(workspace_path / "findings.db")

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS targets (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id         INTEGER NOT NULL,
        cycle_id          TEXT UNIQUE NOT NULL,
        engine_sha        TEXT,
        wrapper_sha       TEXT,
        started_at        TEXT NOT NULL,
        finished_at       TEXT,
        n_dispatched      INTEGER DEFAULT 0,
        n_confirmed       INTEGER DEFAULT 0,
        total_cost_usd    REAL DEFAULT 0,
        summary_json_path TEXT,
        FOREIGN KEY(target_id) REFERENCES targets(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS findings (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id       INTEGER NOT NULL,
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
        UNIQUE(target_id, cycle_id, hypothesis_id),
        FOREIGN KEY(target_id) REFERENCES targets(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transitions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        finding_id  INTEGER NOT NULL,
        from_status TEXT,
        to_status   TEXT NOT NULL,
        reason      TEXT,
        actor       TEXT,
        ts          TEXT NOT NULL,
        FOREIGN KEY(finding_id) REFERENCES findings(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS poc_cache (
        engine_sha     TEXT NOT NULL,
        hypothesis_id  TEXT NOT NULL,
        poc_hash       TEXT NOT NULL,
        outcome        TEXT NOT NULL,
        cargo_rc       INTEGER,
        elapsed_s      REAL,
        log_path       TEXT,
        n_hits         INTEGER NOT NULL DEFAULT 1,
        first_seen_at  TEXT NOT NULL,
        last_seen_at   TEXT NOT NULL,
        PRIMARY KEY(engine_sha, hypothesis_id, poc_hash)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_findings_target    ON findings(target_id)",
    "CREATE INDEX IF NOT EXISTS idx_findings_status    ON findings(status)",
    "CREATE INDEX IF NOT EXISTS idx_findings_severity  ON findings(severity)",
    "CREATE INDEX IF NOT EXISTS idx_findings_bug_class ON findings(bug_class)",
    "CREATE INDEX IF NOT EXISTS idx_cycles_target      ON cycles(target_id)",
    "CREATE INDEX IF NOT EXISTS idx_poc_cache_hyp      ON poc_cache(hypothesis_id)",
]


# Idempotent column migrations for existing DBs that pre-date schema changes.
# Each entry is (table, column, definition). _init_schema applies any that
# are missing. Adding a column means appending here, not editing CREATE TABLE.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("findings", "bug_class", "TEXT"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FindingsDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # Cross-cutting audit Defect 03 (HIGH): default sqlite3.connect has
        # no WAL mode and no busy_timeout. Concurrent writers (shadow daemon
        # + watch hook + scheduled hunts + heartbeat) racing against a
        # single findings.db raise ``OperationalError: database is locked``
        # which hook daemon threads silently swallow, leaving audit-log
        # state diverged from the actual table. WAL allows readers
        # concurrent with a single writer; busy_timeout makes the writer
        # back off + retry rather than fail immediately. Apply once per
        # connection — WAL itself is a persistent DB-level mode.
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            # ``isolation_level=None`` would give us autocommit; we
            # explicitly commit at the end so leave it as DEFERRED.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")     # 30s
            conn.execute("PRAGMA synchronous=NORMAL")     # safe + faster under WAL
        except sqlite3.OperationalError:
            # If the DB is already open by another process and locks at
            # PRAGMA time, fall through — the busy_timeout=30s on the
            # connection itself will still apply to subsequent statements.
            pass
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            # Phase 1: CREATE TABLEs (idempotent — won't replace existing).
            for stmt in SCHEMA:
                if "CREATE TABLE" in stmt:
                    c.execute(stmt)
            # Phase 2: column migrations on existing DBs (must happen before
            # any CREATE INDEX that references newly-added columns).
            for table, column, definition in _MIGRATIONS:
                cols = {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            # Phase 3: CREATE INDEXes (now safe — all referenced columns exist).
            for stmt in SCHEMA:
                if "CREATE INDEX" in stmt:
                    c.execute(stmt)

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
            row = c.execute(
                "SELECT id FROM targets WHERE name = ?", (name,)
            ).fetchone()
            if row:
                c.execute(
                    """
                    UPDATE targets
                       SET github_url = COALESCE(?, github_url),
                           engine_repo = COALESCE(?, engine_repo),
                           wrapper_repo = COALESCE(?, wrapper_repo),
                           config_json = COALESCE(?, config_json),
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (github_url, engine_repo, wrapper_repo, config_json, now, row["id"]),
                )
                return int(row["id"])
            cur = c.execute(
                """
                INSERT INTO targets
                  (name, github_url, engine_repo, wrapper_repo,
                   created_at, updated_at, config_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (name, github_url, engine_repo, wrapper_repo, now, now, config_json),
            )
            return int(cur.lastrowid)

    def list_targets(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM targets ORDER BY name").fetchall()
            return [dict(r) for r in rows]

    def get_target(self, name: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM targets WHERE name = ?", (name,)
            ).fetchone()
            return dict(row) if row else None

    # ---------- cycles ----------

    def insert_cycle(
        self,
        target_id: int,
        cycle_id: str,
        engine_sha: str | None = None,
        wrapper_sha: str | None = None,
        summary_json_path: str | None = None,
    ) -> int:
        now = _now()
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT OR REPLACE INTO cycles
                  (target_id, cycle_id, engine_sha, wrapper_sha,
                   started_at, summary_json_path)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (target_id, cycle_id, engine_sha, wrapper_sha, now, summary_json_path),
            )
            return int(cur.lastrowid)

    def finish_cycle(
        self,
        cycle_id: str,
        n_dispatched: int,
        n_confirmed: int,
        total_cost_usd: float,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                UPDATE cycles
                   SET finished_at = ?,
                       n_dispatched = ?,
                       n_confirmed = ?,
                       total_cost_usd = ?
                 WHERE cycle_id = ?
                """,
                (_now(), n_dispatched, n_confirmed, total_cost_usd, cycle_id),
            )

    def list_cycles(self, target_id: int | None = None, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            if target_id is not None:
                rows = c.execute(
                    "SELECT * FROM cycles WHERE target_id = ? ORDER BY started_at DESC LIMIT ?",
                    (target_id, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM cycles ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    # ---------- findings ----------

    def upsert_finding(
        self,
        target_id: int,
        cycle_id: str,
        hypothesis_id: str,
        verdict: str,
        confidence: str,
        severity: Severity,
        status: Status,
        title: str | None = None,
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
            row = c.execute(
                """
                SELECT id, status FROM findings
                 WHERE target_id = ? AND cycle_id = ? AND hypothesis_id = ?
                """,
                (target_id, cycle_id, hypothesis_id),
            ).fetchone()
            if row:
                c.execute(
                    """
                    UPDATE findings
                       SET verdict = ?, confidence = ?, severity = ?,
                           title = COALESCE(?, title),
                           poc_path = COALESCE(?, poc_path),
                           poc_fired = ?, debate_promoted = ?,
                           engine_sha = COALESCE(?, engine_sha),
                           wrapper_sha = COALESCE(?, wrapper_sha),
                           details_json = COALESCE(?, details_json),
                           bug_class = COALESCE(?, bug_class),
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        verdict, confidence, severity.value,
                        title, poc_path, int(poc_fired), int(debate_promoted),
                        engine_sha, wrapper_sha, details_json, bug_class, now, row["id"],
                    ),
                )
                return int(row["id"])

            cur = c.execute(
                """
                INSERT INTO findings
                  (target_id, cycle_id, hypothesis_id, title,
                   verdict, confidence, severity, status,
                   poc_path, poc_fired, debate_promoted,
                   engine_sha, wrapper_sha, created_at, updated_at, details_json,
                   bug_class)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_id, cycle_id, hypothesis_id, title,
                    verdict, confidence, severity.value, status.value,
                    poc_path, int(poc_fired), int(debate_promoted),
                    engine_sha, wrapper_sha, now, now, details_json,
                    bug_class,
                ),
            )
            finding_id = int(cur.lastrowid)
            c.execute(
                """
                INSERT INTO transitions (finding_id, from_status, to_status, reason, actor, ts)
                VALUES (?, NULL, ?, ?, ?, ?)
                """,
                (finding_id, status.value, "initial classification from hunt cycle", "system", now),
            )
            return finding_id

    def transition_finding(
        self,
        finding_id: int,
        to_status: Status,
        reason: str,
        actor: str = "system",
        run_hooks: bool = True,
    ) -> None:
        """Transition a finding to a new lifecycle status.

        After a successful transition (commit), fires post-transition hooks
        in a background thread:
          confirmed  → auto-derive siblings (Tier 2 #8)
                       + auto-fire cross-protocol propagation (Tier 2 #9)
          disclosed  → currently no-op (placeholder for future hooks)

        Hooks are fire-and-forget — failures are silenced and never block
        the transition. Pass `run_hooks=False` to suppress hook scheduling
        (e.g. during bulk imports / tests).
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT status FROM findings WHERE id = ?", (finding_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"finding {finding_id} not found")
            frm = Status(row["status"])
            assert_transition(frm, to_status)
            now = _now()
            c.execute(
                "UPDATE findings SET status = ?, updated_at = ? WHERE id = ?",
                (to_status.value, now, finding_id),
            )
            c.execute(
                """
                INSERT INTO transitions (finding_id, from_status, to_status, reason, actor, ts)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (finding_id, frm.value, to_status.value, reason, actor, now),
            )

        # Post-transition hooks (Tier 2 #8 + #9). Run AFTER commit so the
        # finding's new status is observable to the hook code; run in a
        # background thread so a slow LLM call doesn't block the DB write.
        if run_hooks and to_status == Status.CONFIRMED:
            self._fire_confirmed_hooks(finding_id)

    def _fire_confirmed_hooks(self, finding_id: int) -> None:
        """Fire the on-confirmed hooks: sibling derivation + propagation.

        Each hook runs in its OWN daemon thread (P2 Wave 7a — concurrent
        execution). At low volume both finish quickly; at higher volume
        (multiple findings confirming in one cycle) parallelism keeps the
        wall-clock latency from compounding linearly.

        F21: every hook invocation is logged to
        ``<workspace>/hooks/<finding_id>-<hook_name>-<ts>.log``. The log
        records started_at, completed_at, exit status, and (on failure)
        a short exception traceback. Without this, the daemon-thread
        silenced exceptions are a debug nightmare — operators can't tell
        whether the hook ran, succeeded, or crashed.

        E20 (per-cycle rate limit): a global counter at
        ``<workspace>/hooks/cycle-rate-limit.json`` tracks total propagation
        events fired in the current UTC hour. Default cap = 50 events/hour
        (configurable via env JELLEO_HOOK_RATE_LIMIT_PER_HOUR). Above the
        cap, propagation is queued but not executed. Sibling derivation
        is NOT rate-limited globally — it has its own daily budget cap
        (D15 in derive_siblings.py).
        """
        import threading
        ws = self.path.parent

        derive_thread = threading.Thread(
            target=self._run_one_hook,
            args=(ws, finding_id, "derive_siblings", _derive_target),
            daemon=True,
        )
        propagate_thread = threading.Thread(
            target=self._run_one_hook,
            args=(ws, finding_id, "propagate", _propagate_target),
            daemon=True,
        )
        derive_thread.start()
        propagate_thread.start()

    def _run_one_hook(
        self,
        workspace: Path,
        finding_id: int,
        hook_name: str,
        target: Callable[[Path, int], None],
    ) -> None:
        """Run one hook with structured logging (F21).

        Writes a single log file per invocation under workspace/hooks/.
        Schema is intentionally simple — one JSON line per phase
        (started, completed_or_failed) so it's grep-friendly.
        """
        import json as _json
        import traceback as _tb
        from datetime import datetime, timezone

        hooks_dir = workspace / "hooks"
        try:
            hooks_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return  # filesystem read-only or similar — fail silently

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = hooks_dir / f"{finding_id}-{hook_name}-{ts}.log"
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")

        def _write(record: dict) -> None:
            try:
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(_json.dumps(record, sort_keys=True) + "\n")
            except OSError:
                pass

        _write({
            "phase":       "started",
            "ts":          started,
            "finding_id":  finding_id,
            "hook":        hook_name,
            "workspace":   str(workspace),
        })

        try:
            target(workspace, finding_id)
            _write({
                "phase":        "completed",
                "ts":           datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "finding_id":   finding_id,
                "hook":         hook_name,
                "outcome":      "ok",
            })
        except Exception as e:  # noqa: BLE001
            _write({
                "phase":        "failed",
                "ts":           datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "finding_id":   finding_id,
                "hook":         hook_name,
                "outcome":      "error",
                "error_type":   type(e).__name__,
                "error_msg":    str(e)[:500],
                "traceback":    _tb.format_exc()[-2000:],
            })

    def get_finding(self, finding_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM findings WHERE id = ?", (finding_id,)
            ).fetchone()
            return dict(row) if row else None

    # ─────────────────────────── PoC test cache (Tier 2 #10) ──────────────────

    def get_poc_cache(
        self,
        engine_sha: str,
        hypothesis_id: str,
        poc_hash: str,
    ) -> dict | None:
        """Look up a cached PoC outcome.

        Returns the cached row (dict) on hit, None on miss. The cache
        key is (engine_sha, hypothesis_id, poc_hash) — same engine SHA
        + same hyp ID + same test bytes always produces the same outcome
        because cargo test is deterministic on a fixed input.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM poc_cache WHERE engine_sha = ? AND hypothesis_id = ? AND poc_hash = ?",
                (engine_sha, hypothesis_id, poc_hash),
            ).fetchone()
            if not row:
                return None
            # Touch the last_seen_at + bump n_hits so we can see hot entries.
            c.execute(
                "UPDATE poc_cache SET last_seen_at = ?, n_hits = n_hits + 1 "
                "WHERE engine_sha = ? AND hypothesis_id = ? AND poc_hash = ?",
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
        """Insert (or update) a PoC cache entry.

        Idempotent — re-running the same PoC re-populates the row.
        """
        now = _now()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO poc_cache (
                    engine_sha, hypothesis_id, poc_hash,
                    outcome, cargo_rc, elapsed_s, log_path,
                    n_hits, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(engine_sha, hypothesis_id, poc_hash) DO UPDATE SET
                    outcome   = excluded.outcome,
                    cargo_rc  = excluded.cargo_rc,
                    elapsed_s = excluded.elapsed_s,
                    log_path  = excluded.log_path,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    engine_sha, hypothesis_id, poc_hash,
                    outcome, cargo_rc, elapsed_s, log_path,
                    now, now,
                ),
            )

    def list_poc_cache(self, limit: int = 100) -> list[dict]:
        """List recent cache entries (debugging / `audit-pipeline cache list`)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM poc_cache ORDER BY last_seen_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def flush_poc_cache(
        self,
        engine_sha: str | None = None,
        hypothesis_id: str | None = None,
    ) -> int:
        """Delete cache entries. Returns # rows deleted.

        With no args: deletes ALL entries. Filtered args narrow scope.
        Used by `audit-pipeline cache flush`.
        """
        clauses = []
        params: list[Any] = []
        if engine_sha is not None:
            clauses.append("engine_sha = ?")
            params.append(engine_sha)
        if hypothesis_id is not None:
            clauses.append("hypothesis_id = ?")
            params.append(hypothesis_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as c:
            cur = c.execute(f"DELETE FROM poc_cache{where}", tuple(params))
            return cur.rowcount or 0

    def list_findings(
        self,
        target_id: int | None = None,
        status: Status | None = None,
        severity: Severity | None = None,
        bug_class: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        clauses = []
        params: list[Any] = []
        if target_id is not None:
            clauses.append("target_id = ?")
            params.append(target_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity.value)
        if bug_class is not None:
            clauses.append("bug_class = ?")
            params.append(bug_class)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM findings{where} ORDER BY updated_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_findings_by_cycle(self, cycle_id: str) -> list[dict]:
        """Return every finding belonging to a single cycle.

        Avoids `list_findings(limit=N)` truncation when a cycle has
        more findings than the global default limit. Used by the
        Merkle root computation, which must hash the COMPLETE finding
        set or the root is silently wrong.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM findings WHERE cycle_id = ? ORDER BY id ASC",
                (cycle_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_confirmed_findings_by_bug_class(
        self,
        bug_class: str,
        exclude_target_id: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List confirmed findings sharing a bug_class — used by the propagation engine.

        Excludes a single target_id if provided (the protocol where the
        finding originally confirmed) so propagation only fans out to OTHER
        protocols. Limited to non-rejected lifecycle states.
        """
        terminal_excluded = (Status.REJECTED.value,)
        params: list[Any] = [bug_class, *terminal_excluded]
        clause = "bug_class = ? AND status NOT IN (?)"
        if exclude_target_id is not None:
            clause += " AND target_id != ?"
            params.append(exclude_target_id)
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM findings WHERE {clause} ORDER BY updated_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
            return [dict(r) for r in rows]

    def transitions_for(self, finding_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM transitions WHERE finding_id = ? ORDER BY ts",
                (finding_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def stats(self) -> dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
            by_status = {
                r["status"]: r["n"] for r in
                c.execute("SELECT status, COUNT(*) AS n FROM findings GROUP BY status").fetchall()
            }
            by_sev = {
                r["severity"]: r["n"] for r in
                c.execute("SELECT severity, COUNT(*) AS n FROM findings GROUP BY severity").fetchall()
            }
            n_targets = c.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
            n_cycles = c.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
        return {
            "n_findings": total,
            "n_targets": n_targets,
            "n_cycles": n_cycles,
            "by_status": by_status,
            "by_severity": by_sev,
        }
