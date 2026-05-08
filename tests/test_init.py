"""Tests for `audit-pipeline init`."""

import json

import pytest
from click.testing import CliRunner

from audit_pipeline.cli import main


@pytest.fixture
def runner():
    return CliRunner()


def test_init_creates_workspace_layout(runner, tmp_path):
    """init should create the expected directory structure."""
    output = tmp_path / "audit_ws"

    result = runner.invoke(
        main,
        [
            "init",
            "--engine-repo", "https://github.com/example/engine",
            "--engine-sha", "abc123",
            "--wrapper-repo", "https://github.com/example/wrapper",
            "--wrapper-sha", "def456",
            "--output", str(output),
            "--no-clone",  # don't actually clone in tests
        ],
    )

    assert result.exit_code == 0, result.output
    assert output.exists()
    assert (output / "tests" / "engine").is_dir()
    assert (output / "tests" / "wrapper").is_dir()
    assert (output / "results").is_dir()
    assert (output / "recon").is_dir()
    assert (output / "workspace.json").is_file()
    assert (output / "README.md").is_file()


def test_init_writes_workspace_json(runner, tmp_path):
    """workspace.json should record the config."""
    output = tmp_path / "audit_ws"

    runner.invoke(
        main,
        [
            "init",
            "--engine-repo", "https://github.com/example/engine",
            "--engine-sha", "abc123",
            "--wrapper-repo", "https://github.com/example/wrapper",
            "--wrapper-sha", "def456",
            "--output", str(output),
            "--no-clone",
            "--target-name", "example-target",
        ],
    )

    config = json.loads((output / "workspace.json").read_text())
    assert config["target_name"] == "example-target"
    assert config["engine"]["repo"] == "https://github.com/example/engine"
    assert config["engine"]["sha"] == "abc123"
    assert config["wrapper"]["repo"] == "https://github.com/example/wrapper"
    assert config["wrapper"]["sha"] == "def456"
    assert config["audit_pipeline_version"] == "0.1.0"


def test_init_refuses_non_empty_output(runner, tmp_path):
    """init should fail if the output dir is non-empty."""
    output = tmp_path / "existing"
    output.mkdir()
    (output / "stale_file.txt").write_text("oops")

    result = runner.invoke(
        main,
        [
            "init",
            "--engine-repo", "https://github.com/example/engine",
            "--engine-sha", "abc123",
            "--wrapper-repo", "https://github.com/example/wrapper",
            "--wrapper-sha", "def456",
            "--output", str(output),
            "--no-clone",
        ],
    )

    assert result.exit_code != 0
    assert "exists and is not empty" in result.output
