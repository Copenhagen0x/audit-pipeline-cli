"""Tests for the 5 force-multiplier commands (debate, spec-check, synth-kani, propagate, shadow)."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from audit_pipeline.cli import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def workspace(tmp_path):
    """Create a minimal initialized workspace."""
    ws = tmp_path / "ws"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "init",
            "--engine-repo", "https://github.com/example/engine",
            "--engine-sha", "abc123",
            "--wrapper-repo", "https://github.com/example/wrapper",
            "--wrapper-sha", "def456",
            "--output", str(ws),
            "--no-clone",
        ],
    )
    assert result.exit_code == 0, result.output
    return ws


# ============================================================================
# Top-level dispatch
# ============================================================================


def test_all_force_multipliers_listed_in_help(runner):
    """`audit-pipeline --help` should list all 5 new commands."""
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for cmd in ("spec-check", "debate", "propagate", "synth-kani", "shadow"):
        assert cmd in result.output, f"missing {cmd} in --help output"


@pytest.mark.parametrize("cmd", ["spec-check", "debate", "propagate", "synth-kani"])
def test_force_multiplier_help_exits_zero(runner, cmd):
    result = runner.invoke(main, [cmd, "--help"])
    assert result.exit_code == 0


def test_shadow_group_help_lists_subcommands(runner):
    result = runner.invoke(main, ["shadow", "--help"])
    assert result.exit_code == 0
    assert "start" in result.output
    assert "tail" in result.output


# ============================================================================
# debate
# ============================================================================


def test_debate_renders_challenger_prompt(runner, workspace, tmp_path):
    """Debate should produce a challenger markdown file with proposer evidence inlined."""
    proposer_response = tmp_path / "proposer_response.md"
    proposer_response.write_text(
        "## Verdict\nFALSE / HIGH - see analysis below.\n\n"
        "## Analysis\nThe doc says MUST NOT drain V, so the bug doesn't exist.\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        main,
        [
            "--workspace", str(workspace),
            "debate",
            "--hypothesis-id", "H1-residual",
            "--proposer-verdict", str(proposer_response),
            "--hypothesis-claim", "residual is preserved across absorb_protocol_loss",
            "--target-file", "src/percolator.rs",
        ],
    )
    assert result.exit_code == 0, result.output

    out = workspace / "recon" / "debate" / "H1-residual_challenger.md"
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "H1-residual" in content
    assert "MUST NOT drain V" in content  # proposer evidence was inlined
    assert "CHALLENGER" in content  # role asserted in prompt


# ============================================================================
# spec-check
# ============================================================================


def test_spec_check_renders_gap_analysis_prompt(runner, workspace, tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(
        "# Spec\n\nThe insurance balance MUST be conserved across calls.\n"
        "Vault balance is a senior claim against c_tot + insurance.\n"
    )
    code = tmp_path / "code.rs"
    code.write_text(
        "fn absorb(loss: u128) {\n"
        "    self.insurance_fund.balance -= loss;\n"
        "    // vault unchanged\n"
        "}\n"
    )

    result = runner.invoke(
        main,
        [
            "--workspace", str(workspace),
            "spec-check",
            "--spec", str(spec),
            "--code", str(code),
            "--name", "test_gap_analysis",
        ],
    )
    assert result.exit_code == 0, result.output

    out = workspace / "recon" / "spec_check" / "test_gap_analysis.md"
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "SPEC-DRIFT auditor" in content
    assert "insurance balance MUST be conserved" in content  # spec inlined
    assert "self.insurance_fund.balance -= loss" in content  # code inlined


# ============================================================================
# synth-kani
# ============================================================================


def test_synth_kani_renders_authoring_prompt(runner, workspace):
    result = runner.invoke(
        main,
        [
            "--workspace", str(workspace),
            "synth-kani",
            "--invariant", "the residual is preserved across absorb_protocol_loss",
            "--engine-function", "absorb_protocol_loss",
            "--mode", "safe",
        ],
    )
    assert result.exit_code == 0, result.output

    expected = workspace / "recon" / "synth_kani" / "absorb_protocol_loss_invariant_safe_prompt.md"
    assert expected.exists(), result.output
    content = expected.read_text(encoding="utf-8")
    assert "the residual is preserved" in content  # invariant inlined
    assert "absorb_protocol_loss" in content
    assert "kani::any" in content or "kani::proof" in content  # structural template was embedded


def test_synth_kani_cex_mode_uses_cex_template(runner, workspace):
    result = runner.invoke(
        main,
        [
            "--workspace", str(workspace),
            "synth-kani",
            "--invariant", "residual must not grow on absorb",
            "--engine-function", "absorb_protocol_loss",
            "--mode", "cex",
        ],
    )
    assert result.exit_code == 0, result.output

    expected = workspace / "recon" / "synth_kani" / "absorb_protocol_loss_invariant_cex_prompt.md"
    assert expected.exists()


# ============================================================================
# propagate
# ============================================================================


def test_propagate_finds_pattern_in_corpus(runner, workspace, tmp_path):
    """propagate should walk a corpus and rank files by signature match count."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    # Two repos; first one matches both signatures; second matches none.
    repo_a = corpus / "protocol_a"
    repo_a.mkdir()
    (repo_a / "lib.rs").write_text(
        "fn absorb(loss: u128) {\n"
        "    self.insurance.balance -= loss;\n"
        "    self.vault -= loss;\n"  # second signature hit
        "}\n"
    )
    repo_b = corpus / "protocol_b"
    repo_b.mkdir()
    (repo_b / "lib.rs").write_text(
        "fn unrelated() { let x = 1; }\n"
    )

    result = runner.invoke(
        main,
        [
            "--workspace", str(workspace),
            "propagate",
            "--corpus", str(corpus),
            "--signature", r"insurance.*balance",
            "--signature", r"vault\s*[-+]?=",
            "--min-score", "1",
        ],
    )
    assert result.exit_code == 0, result.output

    report = workspace / "recon" / "propagate" / "propagation_report.md"
    assert report.exists()
    content = report.read_text(encoding="utf-8")
    assert "protocol_a" in content
    # protocol_b has no matches so should not appear in the ranked section
    assert "protocol_b / `lib.rs`" not in content


# ============================================================================
# shadow (start --once + tail) — uses public RPC, network-dependent;
# we mock by writing a stub tx into the alerts.jsonl directly and tail it.
# ============================================================================


def test_shadow_tail_handles_missing_log_gracefully(runner, workspace):
    """`shadow tail` should not crash when no alerts log exists yet."""
    result = runner.invoke(
        main,
        ["--workspace", str(workspace), "shadow", "tail"],
    )
    assert result.exit_code == 0
    assert "No alerts log" in result.output


def test_shadow_tail_displays_recorded_alerts(runner, workspace):
    """`shadow tail` should render alerts written to alerts.jsonl."""
    shadow_dir = workspace / "shadow"
    shadow_dir.mkdir(parents=True)
    alerts = shadow_dir / "alerts.jsonl"
    alerts.write_text(
        json.dumps({
            "timestamp": "2026-04-27T22:30:00+00:00",
            "signature": "5x" * 32,
            "slot": 999_888_777,
            "pattern_matched": "panic_in_program_log",
            "log_excerpt": "Program log: panicked at src/lib.rs:42:13",
            "program": "TestProgram1111111111111111111111111111111",
        }) + "\n"
    )

    result = runner.invoke(
        main,
        ["--workspace", str(workspace), "shadow", "tail"],
    )
    assert result.exit_code == 0
    # Rich tables truncate column content with "..." when narrow; check for
    # substrings short enough to survive default column widths.
    assert "panic_in" in result.output  # pattern column (may be truncated)
    assert "Recent alerts" in result.output  # table title
