"""Tests for `audit-pipeline freshness` + `audit-pipeline watch`."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from audit_pipeline.cli import main
from audit_pipeline.utils.github import parse_github_repo


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "init",
            "--engine-repo", "https://github.com/example/engine",
            "--engine-sha", "deadbeef",
            "--wrapper-repo", "https://github.com/example/wrapper",
            "--wrapper-sha", "cafebabe",
            "--output", str(ws),
            "--no-clone",
        ],
    )
    assert result.exit_code == 0, result.output
    return ws


# ============================================================================
# parse_github_repo helper
# ============================================================================


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/foo/bar", ("foo", "bar")),
    ("https://github.com/foo/bar.git", ("foo", "bar")),
    ("https://github.com/foo/bar/", ("foo", "bar")),
    ("https://www.github.com/foo/bar", ("foo", "bar")),
])
def test_parse_github_repo_valid(url, expected):
    assert parse_github_repo(url) == expected


@pytest.mark.parametrize("url", [
    "https://gitlab.com/foo/bar",
    "https://github.com/foo",
    "https://example.com",
])
def test_parse_github_repo_invalid(url):
    with pytest.raises(ValueError):
        parse_github_repo(url)


# ============================================================================
# freshness command
# ============================================================================


def test_freshness_help(runner):
    result = runner.invoke(main, ["freshness", "--help"])
    assert result.exit_code == 0
    assert "stale" in result.output.lower()
    assert "--update" in result.output


def test_freshness_without_workspace_errors(runner, tmp_path):
    result = runner.invoke(main, ["--workspace", str(tmp_path), "freshness"])
    assert result.exit_code != 0
    assert "workspace.json" in result.output


def test_freshness_reports_when_up_to_date(runner, workspace):
    """When pinned == latest, report up-to-date for both components."""
    fake_commit = {
        "sha": "deadbeef",  # matches the pinned engine sha (we'll use cafebabe for wrapper)
        "commit": {
            "message": "Pinned commit",
            "author": {"date": "2026-04-27T22:00:00Z"},
        },
    }
    fake_wrapper = {
        "sha": "cafebabe",
        "commit": {
            "message": "Pinned wrapper commit",
            "author": {"date": "2026-04-27T22:00:00Z"},
        },
    }

    def fake_get(owner, repo, ref="HEAD", timeout=30):
        return fake_commit if "engine" in repo else fake_wrapper

    with patch(
        "audit_pipeline.commands.freshness.get_latest_commit", side_effect=fake_get,
    ), patch(
        "audit_pipeline.commands.freshness.list_commits_since", return_value=[],
    ):
        result = runner.invoke(main, ["--workspace", str(workspace), "freshness"])

    assert result.exit_code == 0, result.output
    assert "up-to-date" in result.output


def test_freshness_reports_stale_with_commit_count(runner, workspace):
    """When pinned is behind, surface the commit count + recent commits."""
    fake_commit = {
        "sha": "9999abcd",
        "commit": {
            "message": "Newer commit",
            "author": {"date": "2026-04-28T01:00:00Z"},
        },
    }
    fake_commits_list = [
        {
            "sha": "9999abcd",
            "commit": {
                "message": "Newest fix",
                "author": {"date": "2026-04-28T01:00:00Z"},
            },
        },
        {
            "sha": "8888bcde",
            "commit": {
                "message": "Earlier fix",
                "author": {"date": "2026-04-27T23:00:00Z"},
            },
        },
    ]

    with patch(
        "audit_pipeline.commands.freshness.get_latest_commit",
        return_value=fake_commit,
    ), patch(
        "audit_pipeline.commands.freshness.list_commits_since",
        return_value=fake_commits_list,
    ):
        result = runner.invoke(main, ["--workspace", str(workspace), "freshness"])

    assert result.exit_code == 0, result.output
    # +2 indicates 2 commits behind
    assert "+2" in result.output


# ============================================================================
# watch command
# ============================================================================


def test_watch_help(runner):
    result = runner.invoke(main, ["watch", "--help"])
    assert result.exit_code == 0
    assert "--auto-pull" in result.output
    assert "--update-pin" in result.output
    assert "--on-update" in result.output


def test_watch_without_workspace_errors(runner, tmp_path):
    result = runner.invoke(main, [
        "--workspace", str(tmp_path), "watch", "--once",
    ])
    assert result.exit_code != 0


def test_watch_once_no_new_commits(runner, workspace):
    """Watch in --once mode reports unchanged when latest == pinned."""
    # Engine pinned: deadbeef, wrapper pinned: cafebabe
    def fake_get(owner, repo, ref="HEAD", timeout=30):
        sha = "deadbeef" if "engine" in repo else "cafebabe"
        return {
            "sha": sha,
            "commit": {
                "message": "Pinned commit",
                "author": {"date": "2026-04-27T22:00:00Z"},
            },
        }

    with patch(
        "audit_pipeline.commands.watch.get_latest_commit", side_effect=fake_get,
    ):
        result = runner.invoke(
            main,
            ["--workspace", str(workspace), "watch", "--once"],
        )

    assert result.exit_code == 0, result.output
    assert "One-shot poll complete" in result.output


def test_watch_once_new_commit_detected_logs_only(runner, workspace):
    """Watch in --once mode without --auto-pull surfaces NEW COMMIT but doesn't pull."""
    def fake_get(owner, repo, ref="HEAD", timeout=30):
        sha = "9999new1" if "engine" in repo else "cafebabe"
        return {
            "sha": sha,
            "commit": {
                "message": ("Brand new engine fix" if "engine" in repo
                            else "Pinned wrapper"),
                "author": {"date": "2026-04-28T03:00:00Z"},
            },
        }

    with patch(
        "audit_pipeline.commands.watch.get_latest_commit", side_effect=fake_get,
    ):
        result = runner.invoke(
            main,
            ["--workspace", str(workspace), "watch", "--once"],
        )

    assert result.exit_code == 0, result.output
    assert "NEW COMMIT" in result.output
    assert "engine" in result.output
    # Without --update-pin, workspace.json shouldn't be rewritten
    config = json.loads((workspace / "workspace.json").read_text())
    assert config["engine"]["sha"] == "deadbeef"


def test_watch_once_with_update_pin_rewrites_workspace_json(runner, workspace):
    """With --update-pin, the new SHA should land in workspace.json (no auto-pull needed for this asset)."""
    def fake_get(owner, repo, ref="HEAD", timeout=30):
        sha = "9999new1" if "engine" in repo else "cafebabe"
        return {
            "sha": sha,
            "commit": {
                "message": "x",
                "author": {"date": "2026-04-28T03:00:00Z"},
            },
        }

    # Engine clone target dir doesn't have a .git, so auto-pull will skip
    # the actual git fetch — but --update-pin still rewrites workspace.json
    with patch(
        "audit_pipeline.commands.watch.get_latest_commit", side_effect=fake_get,
    ):
        result = runner.invoke(
            main,
            [
                "--workspace", str(workspace),
                "watch", "--once", "--update-pin",
            ],
        )

    assert result.exit_code == 0, result.output
    config = json.loads((workspace / "workspace.json").read_text())
    assert config["engine"]["sha"] == "9999new1"
    assert config["wrapper"]["sha"] == "cafebabe"  # unchanged
