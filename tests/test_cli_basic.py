"""Smoke tests for the top-level CLI dispatch."""

import pytest
from click.testing import CliRunner

from audit_pipeline.cli import main


@pytest.fixture
def runner():
    return CliRunner()


def test_main_help(runner):
    """`audit-pipeline --help` should exit 0 and list known subcommands."""
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for cmd in ("init", "provision-vps", "sync", "recon", "poc",
                "kani", "litesvm", "cross-check", "disclose", "run"):
        assert cmd in result.output


def test_subcommand_help_exit_zero(runner):
    """Each registered subcommand should respond to --help with exit 0."""
    for cmd in ("init", "provision-vps", "sync", "recon", "poc",
                "cross-check", "disclose", "run"):
        result = runner.invoke(main, [cmd, "--help"])
        assert result.exit_code == 0, f"{cmd} --help failed: {result.output}"


def test_kani_group_help(runner):
    """`audit-pipeline kani --help` should list author/dispatch/status/fetch."""
    result = runner.invoke(main, ["kani", "--help"])
    assert result.exit_code == 0
    for sub in ("author", "dispatch", "status", "fetch"):
        assert sub in result.output


def test_litesvm_group_help(runner):
    """`audit-pipeline litesvm --help` should list author/dispatch."""
    result = runner.invoke(main, ["litesvm", "--help"])
    assert result.exit_code == 0
    for sub in ("author", "dispatch"):
        assert sub in result.output


def test_run_dry_run_without_workspace(runner, tmp_path):
    """`run --dry-run` against a missing workspace.json should error cleanly."""
    result = runner.invoke(main, ["--workspace", str(tmp_path), "run", "--dry-run"])
    # Click maps ClickException to exit code 1
    assert result.exit_code != 0
    assert "workspace.json" in result.output.lower() or "init" in result.output.lower()


def test_poc_template_choices_include_state_conservation(runner):
    """`poc --help` should list both engine_native_poc and engine_state_conservation_poc."""
    result = runner.invoke(main, ["poc", "--help"])
    assert result.exit_code == 0
    assert "engine_native_poc" in result.output
    assert "engine_state_conservation_poc" in result.output
