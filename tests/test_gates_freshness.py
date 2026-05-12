"""Tests for ``audit_pipeline.gates.freshness_gate`` (Gate 1).

GitHub API calls are mocked — we never hit the real network in tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from audit_pipeline.gates.freshness_gate import check_freshness


def _write_workspace(tmp_path: Path, engine_sha: str, wrapper_sha: str) -> None:
    cfg = {
        "engine":  {"sha": engine_sha,  "repo": "https://github.com/owner/engine",  "local": "target/engine"},
        "wrapper": {"sha": wrapper_sha, "repo": "https://github.com/owner/wrapper", "local": "target/wrapper"},
    }
    (tmp_path / "workspace.json").write_text(json.dumps(cfg))


def _make_commit(sha: str, hours_ago: float = 0.0, msg: str = "head commit"):
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {
        "sha": sha,
        "commit": {
            "author": {"date": dt.isoformat()},
            "message": msg,
        },
    }


class TestCheckFreshness:
    def test_missing_workspace_json_fails(self, tmp_path):
        r = check_freshness(workspace=tmp_path)
        assert r.passed is False
        assert "workspace.json" in r.reason

    def test_invalid_workspace_json_fails(self, tmp_path):
        (tmp_path / "workspace.json").write_text("not valid json")
        r = check_freshness(workspace=tmp_path)
        assert r.passed is False
        assert "invalid" in r.reason

    def test_pinned_equals_head_passes(self, tmp_path):
        _write_workspace(tmp_path, engine_sha="aaaa1111", wrapper_sha="bbbb2222")
        def fake_latest(owner, repo, ref="HEAD", timeout=30):
            if "engine" in repo:
                return _make_commit("aaaa1111", 0.5)
            return _make_commit("bbbb2222", 1.2)
        with patch("audit_pipeline.utils.github.get_latest_commit", side_effect=fake_latest):
            r = check_freshness(workspace=tmp_path, max_stale_hours=6.0)
        assert r.passed is True
        assert "fresh" in r.reason

    def test_stale_engine_fails_closed(self, tmp_path):
        _write_workspace(tmp_path, engine_sha="oldold11", wrapper_sha="bbbb2222")
        head_engine  = _make_commit("newnew99", hours_ago=0.5)
        pinned_engine = _make_commit("oldold11", hours_ago=72.0)   # 3 days old
        head_wrapper = _make_commit("bbbb2222", hours_ago=10)
        def fake_latest(owner, repo, ref="HEAD", timeout=30):
            if "engine" in repo:
                return head_engine if ref == "HEAD" else pinned_engine
            return head_wrapper
        with patch("audit_pipeline.utils.github.get_latest_commit", side_effect=fake_latest):
            r = check_freshness(workspace=tmp_path, max_stale_hours=6.0)
        assert r.passed is False
        assert "stale" in r.reason
        engines = [c for c in r.details["components"] if c["component"] == "engine"]
        assert engines[0]["status"] == "stale"

    def test_grace_window_just_within_passes(self, tmp_path):
        _write_workspace(tmp_path, engine_sha="oldold11", wrapper_sha="bbbb2222")
        head_engine  = _make_commit("newnew99", hours_ago=0.5)
        pinned_engine = _make_commit("oldold11", hours_ago=5.0)
        head_wrapper = _make_commit("bbbb2222", hours_ago=10)
        def fake_latest(owner, repo, ref="HEAD", timeout=30):
            if "engine" in repo:
                return head_engine if ref == "HEAD" else pinned_engine
            return head_wrapper
        with patch("audit_pipeline.utils.github.get_latest_commit", side_effect=fake_latest):
            r = check_freshness(workspace=tmp_path, max_stale_hours=6.0)
        # head_age - pinned_age ≈ 4.5h, within 6h grace
        assert r.passed is True

    def test_zero_grace_strict_mode(self, tmp_path):
        """max_stale_hours=0 → ANY drift fails."""
        _write_workspace(tmp_path, engine_sha="oldold11", wrapper_sha="bbbb2222")
        head_engine  = _make_commit("newnew99", hours_ago=0.5)
        pinned_engine = _make_commit("oldold11", hours_ago=1.0)
        head_wrapper = _make_commit("bbbb2222", hours_ago=10)
        def fake_latest(owner, repo, ref="HEAD", timeout=30):
            if "engine" in repo:
                return head_engine if ref == "HEAD" else pinned_engine
            return head_wrapper
        with patch("audit_pipeline.utils.github.get_latest_commit", side_effect=fake_latest):
            r = check_freshness(workspace=tmp_path, max_stale_hours=0.0)
        assert r.passed is False

    def test_all_unreachable_returns_skip(self, tmp_path):
        _write_workspace(tmp_path, engine_sha="x", wrapper_sha="y")
        def fake_latest(owner, repo, ref="HEAD", timeout=30):
            raise RuntimeError("network down")
        with patch("audit_pipeline.utils.github.get_latest_commit", side_effect=fake_latest):
            r = check_freshness(workspace=tmp_path)
        assert r.passed is None
        assert "could not reach upstream" in r.reason

    def test_partial_unreachable_one_fresh_passes(self, tmp_path):
        """If at least one component is verified fresh and others are
        merely unreachable (transient), we don't fail-closed."""
        _write_workspace(tmp_path, engine_sha="aaaa1111", wrapper_sha="bbbb2222")
        def fake_latest(owner, repo, ref="HEAD", timeout=30):
            if "engine" in repo:
                return _make_commit("aaaa1111", 0.5)
            raise RuntimeError("transient wrapper failure")
        with patch("audit_pipeline.utils.github.get_latest_commit", side_effect=fake_latest):
            r = check_freshness(workspace=tmp_path)
        # engine fresh, wrapper unreachable → not stale → pass with warning in details
        assert r.passed is True
        wrappers = [c for c in r.details["components"] if c["component"] == "wrapper"]
        assert wrappers[0]["status"] == "unreachable"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
