"""Tests for the production-mode upgrades to the 5 force-multiplier commands.

Covers:
- LLM client error handling (missing API key, missing SDK)
- --auto mode dispatch with mocked LLM
- Helper utilities (extract_rust_code_block, _parse_gap_table)
- propagate init-corpus
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from audit_pipeline.cli import main
from audit_pipeline.commands.spec_check import _parse_gap_table, _slugify
from audit_pipeline.utils import LLMResponse, LLMUnavailable, is_available
from audit_pipeline.utils.rust_compile import (
    _extract_compile_errors,
    _parse_kani_verdict,
    extract_rust_code_block,
)


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
            "--engine-sha", "abc123",
            "--wrapper-repo", "https://github.com/example/wrapper",
            "--wrapper-sha", "def456",
            "--output", str(ws),
            "--no-clone",
        ],
    )
    assert result.exit_code == 0, result.output
    return ws


@pytest.fixture
def no_api_key(monkeypatch):
    """Ensure ANTHROPIC_API_KEY is unset for the duration of the test."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# ============================================================================
# LLM client wrapper
# ============================================================================


def test_is_available_false_without_api_key(no_api_key):
    assert is_available() is False


def test_complete_raises_unavailable_without_api_key(no_api_key):
    from audit_pipeline.utils import complete
    with pytest.raises(LLMUnavailable):
        complete("anything")


# ============================================================================
# extract_rust_code_block helper
# ============================================================================


def test_extract_rust_code_block_finds_rust_fence():
    text = "Here's the harness:\n```rust\nfn foo() { let x = 1; }\n```\nDone."
    out = extract_rust_code_block(text)
    assert out == "fn foo() { let x = 1; }"


def test_extract_rust_code_block_finds_rs_fence():
    text = "```rs\nuse foo;\nfn bar() {}\n```"
    out = extract_rust_code_block(text)
    assert out == "use foo;\nfn bar() {}"


def test_extract_rust_code_block_returns_none_for_prose():
    text = "This is just prose with no code at all, definitely no rust here."
    assert extract_rust_code_block(text) is None


def test_extract_rust_code_block_unfenced_fallback():
    """Catch the case where the LLM forgets the fence but writes Rust anyway."""
    text = (
        "Sure! Here it is:\n\n"
        "#![cfg(kani)]\n"
        "use percolator::*;\n"
        "fn main() { }\n"
    )
    out = extract_rust_code_block(text)
    assert out is not None
    assert "fn main()" in out


# ============================================================================
# Cargo error parsing
# ============================================================================


def test_extract_compile_errors_finds_error_blocks():
    output = """
Compiling foo
error[E0425]: cannot find value `x` in this scope
  --> src/foo.rs:42:9
   |
42 |         x + 1
   |         ^ help: a local variable with a similar name exists: `y`

error: aborting due to previous error
"""
    errors = _extract_compile_errors(output)
    assert len(errors) >= 1
    assert "E0425" in errors[0]


def test_parse_kani_verdict_recognizes_pass():
    assert _parse_kani_verdict("VERIFICATION:- SUCCESSFUL") == "PASS"
    assert _parse_kani_verdict("VERIFICATION SUCCESSFUL") == "PASS"


def test_parse_kani_verdict_recognizes_fail():
    assert _parse_kani_verdict("VERIFICATION:- FAILED") == "FAIL"
    assert _parse_kani_verdict("Verification:- FAILED") == "FAIL"


def test_parse_kani_verdict_unknown_default():
    assert _parse_kani_verdict("nothing useful here") == "UNKNOWN"


# ============================================================================
# Gap table parsing (spec-check)
# ============================================================================


def test_parse_gap_table_extracts_rows():
    text = """
## Gaps worth promoting to Layer 1 hypothesis

| Spec claim | Code location | Class | Severity guess | One-line summary |
|---|---|---|---|---|
| spec.md:L42 | src/foo.rs:L100 | DRIFT | HIGH | Function shrinks counter without crediting vault |
| spec.md:L88 | src/foo.rs:L210 | MISSING | MED | No code enforces the matured-pos cap |

## Detailed gap analysis
... rest of response ...
"""
    gaps = _parse_gap_table(text)
    assert len(gaps) == 2
    assert gaps[0]["gap_class"] == "DRIFT"
    assert gaps[0]["severity"] == "HIGH"
    assert "shrinks counter" in gaps[0]["summary"]
    assert gaps[1]["gap_class"] == "MISSING"


def test_parse_gap_table_returns_empty_when_table_missing():
    assert _parse_gap_table("just prose") == []


def test_slugify_produces_safe_ids():
    assert _slugify("Function shrinks insurance!") == "function-shrinks-insurance"
    assert _slugify("") == "untitled"


# ============================================================================
# --auto mode without API key (each command should error gracefully)
# ============================================================================


def test_synth_kani_auto_without_api_key_errors_gracefully(runner, workspace, no_api_key):
    result = runner.invoke(
        main,
        [
            "--workspace", str(workspace),
            "synth-kani",
            "--invariant", "the residual is preserved",
            "--engine-function", "absorb_protocol_loss",
            "--auto",
        ],
    )
    assert result.exit_code != 0
    assert "ANTHROPIC_API_KEY" in result.output


def test_debate_auto_without_api_key_errors_gracefully(runner, workspace, no_api_key, tmp_path):
    proposer = tmp_path / "proposer.md"
    proposer.write_text("## Verdict\nFALSE / HIGH\n", encoding="utf-8")
    result = runner.invoke(
        main,
        [
            "--workspace", str(workspace),
            "debate",
            "-i", "H1",
            "-v", str(proposer),
            "--auto",
        ],
    )
    assert result.exit_code != 0
    assert "ANTHROPIC_API_KEY" in result.output


def test_spec_check_auto_without_api_key_errors_gracefully(runner, workspace, no_api_key, tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nclaim X\n", encoding="utf-8")
    code = tmp_path / "code.rs"
    code.write_text("fn foo() {}\n", encoding="utf-8")
    result = runner.invoke(
        main,
        [
            "--workspace", str(workspace),
            "spec-check",
            "--spec", str(spec),
            "--code", str(code),
            "--auto",
        ],
    )
    assert result.exit_code != 0
    assert "ANTHROPIC_API_KEY" in result.output


# ============================================================================
# --auto mode with mocked LLM
# ============================================================================


def _mock_response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text, input_tokens=100, output_tokens=200,
        model="mock-claude", stop_reason="end_turn",
    )


def test_debate_auto_with_mocked_llm(runner, workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")
    proposer = tmp_path / "proposer.md"
    proposer.write_text(
        "## Verdict\nFALSE / HIGH\n\n## Analysis\nThe doc says don't drain V.\n",
        encoding="utf-8",
    )

    mock_response_text = """## Verdict
DISAGREE / HIGH - the doc comment is on the wrong function

## Strongest counter-argument
The proposer trusted a doc comment on `record_uninsured_protocol_loss` but
the actual mutation happens in `use_insurance_buffer` at line 3063.
"""

    with patch(
        "audit_pipeline.commands.debate.complete",
        return_value=_mock_response(mock_response_text),
    ):
        result = runner.invoke(
            main,
            [
                "--workspace", str(workspace),
                "debate",
                "-i", "H1-residual",
                "-v", str(proposer),
                "--hypothesis-claim", "residual is preserved",
                "--auto",
            ],
        )

    assert result.exit_code == 0, result.output
    response_path = workspace / "recon" / "debate" / "H1-residual_challenger_response.md"
    assert response_path.exists()
    assert "DISAGREE" in response_path.read_text(encoding="utf-8")
    # The CLI should have surfaced the disagreement
    assert "Disagree" in result.output


def test_spec_check_auto_writes_hypotheses_yaml(runner, workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nThe vault is conserved.\n", encoding="utf-8")
    code = tmp_path / "code.rs"
    code.write_text("fn drain() { vault -= 100; }\n", encoding="utf-8")

    mock_response_text = """## Summary
- Claims extracted: 1
- DRIFT: 1

## Gaps worth promoting to Layer 1 hypothesis

| Spec claim | Code location | Class | Severity guess | One-line summary |
|---|---|---|---|---|
| spec.md:L3 | code.rs:L1 | DRIFT | HIGH | Vault is decremented without conservation |
"""

    with patch(
        "audit_pipeline.commands.spec_check.complete",
        return_value=_mock_response(mock_response_text),
    ):
        result = runner.invoke(
            main,
            [
                "--workspace", str(workspace),
                "spec-check",
                "--spec", str(spec),
                "--code", str(code),
                "--name", "test_auto",
                "--auto",
            ],
        )

    assert result.exit_code == 0, result.output
    yaml_path = workspace / "recon" / "spec_check" / "hypotheses_from_gaps.yaml"
    assert yaml_path.exists()
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert "hypotheses" in data
    assert len(data["hypotheses"]) == 1
    h = data["hypotheses"][0]
    assert h["id"].startswith("GAP01-")
    assert "vault is decremented" in h["claim"].lower()


# ============================================================================
# propagate init-corpus
# ============================================================================


def test_propagate_init_corpus_with_empty_list(runner, tmp_path):
    """init-corpus with an empty list should succeed without cloning anything."""
    list_file = tmp_path / "empty.json"
    list_file.write_text("[]", encoding="utf-8")
    corpus = tmp_path / "corpus"

    result = runner.invoke(
        main,
        [
            "propagate", "init-corpus",
            "--corpus", str(corpus),
            "--list-file", str(list_file),
        ],
    )
    assert result.exit_code == 0
    assert corpus.exists()


# ============================================================================
# Shadow: account state delta — no live RPC, but verify the CLI accepts the
# new --watch-account / --watch-fields flags
# ============================================================================


def test_shadow_start_help_lists_new_flags(runner):
    result = runner.invoke(main, ["shadow", "start", "--help"])
    assert result.exit_code == 0
    assert "--watch-account" in result.output
    assert "--watch-fields" in result.output
