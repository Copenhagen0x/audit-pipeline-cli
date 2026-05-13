"""Tests for ``_resolve_findings_db_path`` — multi-workspace DB resolution.

Regression for cycle 20260513-191318 (osec-aptos-small): bundle and
narrative subcommands emitted "Finding 41 not found" because they
opened the empty per-workspace DB at ``workspaces/aptos-small/
findings.db`` while the real finding rows lived in the shared customer
DB at ``ottersec-eval/findings.db``. Fix walks up the directory tree
when the per-workspace DB is missing AND the layout looks like a
customer rollup (``<root>/<cust>-eval/workspaces/<cell>/``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit_pipeline.db import _resolve_findings_db_path


class TestResolveFindingsDbPath:
    def test_returns_default_when_per_workspace_db_exists(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "findings.db").write_bytes(b"")  # empty file is fine
        assert _resolve_findings_db_path(ws) == ws / "findings.db"

    def test_walks_up_to_customer_eval_root_when_per_workspace_missing(
        self, tmp_path: Path
    ):
        # Simulate the OSec multi-workspace layout:
        #   <tmp>/ottersec-eval/workspaces/aptos-small/
        eval_root = tmp_path / "ottersec-eval"
        cell_ws = eval_root / "workspaces" / "aptos-small"
        cell_ws.mkdir(parents=True)
        # Shared customer DB exists at the eval root
        (eval_root / "findings.db").write_bytes(b"")
        # Per-workspace DB does NOT exist at cell_ws/findings.db
        resolved = _resolve_findings_db_path(cell_ws)
        assert resolved == eval_root / "findings.db", resolved

    def test_does_not_walk_up_when_parent_is_not_workspaces(self, tmp_path: Path):
        """Don't accidentally walk up for layouts that aren't customer rollups."""
        ws = tmp_path / "some-other-layout" / "subdir" / "ws"
        ws.mkdir(parents=True)
        # Shared file at grandparent — but grandparent isn't named *-eval
        # so we should NOT use it.
        (ws.parent.parent / "findings.db").write_bytes(b"")
        assert _resolve_findings_db_path(ws) == ws / "findings.db"

    def test_does_not_walk_up_when_grandparent_not_eval(self, tmp_path: Path):
        """Parent is `workspaces` but grandparent doesn't end with `-eval`."""
        root = tmp_path / "plain-rollup"
        cell_ws = root / "workspaces" / "cell1"
        cell_ws.mkdir(parents=True)
        (root / "findings.db").write_bytes(b"")
        # Default returned (cell DB missing), no walk-up
        assert _resolve_findings_db_path(cell_ws) == cell_ws / "findings.db"

    def test_does_not_walk_up_when_shared_db_missing(self, tmp_path: Path):
        """Layout matches, but the shared DB doesn't exist either —
        fall back to the default per-workspace path so the caller gets
        a clean ENOENT error rather than silently picking a wrong path."""
        eval_root = tmp_path / "ottersec-eval"
        cell_ws = eval_root / "workspaces" / "aptos-small"
        cell_ws.mkdir(parents=True)
        # No findings.db anywhere
        assert _resolve_findings_db_path(cell_ws) == cell_ws / "findings.db"

    def test_shared_db_takes_precedence_when_both_exist(self, tmp_path: Path):
        """When the layout is a customer-eval rollup, prefer the SHARED
        DB at the eval root over the per-cell ``findings.db`` even if
        both exist. Per-cell DBs from prior runs may be present as
        empty clutter — hunt.py writes ALL rows into the shared DB so
        that's the authoritative source for cross-cell correlation
        (bundle, narrative, propagate).

        Regression for cycle 20260513-191318: per-cell findings.db
        existed empty, shared had the 7 confirmed rows. Choosing
        per-cell silently broke every downstream subcommand.
        """
        eval_root = tmp_path / "ottersec-eval"
        cell_ws = eval_root / "workspaces" / "aptos-small"
        cell_ws.mkdir(parents=True)
        (cell_ws / "findings.db").write_bytes(b"per-cell-empty")
        (eval_root / "findings.db").write_bytes(b"shared-with-rows")
        assert _resolve_findings_db_path(cell_ws) == eval_root / "findings.db"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
