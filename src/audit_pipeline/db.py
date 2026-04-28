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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from audit_pipeline.lifecycle import Status, assert_transition
from audit_pipeline.severity import Severity


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
    "CREATE INDEX IF NOT EXISTS idx_findings_target  ON findings(target_id)",
    "CREATE INDEX IF NOT EXISTS idx_findings_status  ON findings(status)",
    "CREATE INDEX IF NOT EXISTS idx_findings_severity ON findings(severity)",
    "CREATE INDEX IF NOT EXISTS idx_cycles_target    ON cycles(target_id)",
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
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            for stmt in SCHEMA:
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
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        verdict, confidence, severity.value,
                        title, poc_path, int(poc_fired), int(debate_promoted),
                        engine_sha, wrapper_sha, details_json, now, row["id"],
                    ),
                )
                return int(row["id"])

            cur = c.execute(
                """
                INSERT INTO findings
                  (target_id, cycle_id, hypothesis_id, title,
                   verdict, confidence, severity, status,
                   poc_path, poc_fired, debate_promoted,
                   engine_sha, wrapper_sha, created_at, updated_at, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_id, cycle_id, hypothesis_id, title,
                    verdict, confidence, severity.value, status.value,
                    poc_path, int(poc_fired), int(debate_promoted),
                    engine_sha, wrapper_sha, now, now, details_json,
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
    ) -> None:
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

    def get_finding(self, finding_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM findings WHERE id = ?", (finding_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_findings(
        self,
        target_id: int | None = None,
        status: Status | None = None,
        severity: Severity | None = None,
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
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM findings{where} ORDER BY updated_at DESC LIMIT ?",
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
