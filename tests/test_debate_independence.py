"""Tests for L1.5 debate independence (audit cross-cutting Defect).

Two layers of independence the challenger now supports:

  1. --challenger-model  — challenger uses a different model than the
     proposer, so a same-mistake bias in one model doesn't propagate.
  2. --redact-proposer-evidence — the challenger sees only the proposer's
     verdict LINE, not the line citations/code reads the proposer pulled.
     Forces the challenger to re-derive evidence.

Both are opt-in (no behavior change for default callers).
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner


def _seed_proposer(tmp_path: Path) -> Path:
    """Write a fake proposer verdict markdown."""
    p = tmp_path / "H_TEST_proposer.md"
    p.write_text(
        "# Hypothesis H_TEST\n\n"
        "## Evidence\n\n"
        "I read engine/src/lib.rs line 42 and confirmed FOO_CONSTANT = 7.\n"
        "BAR_FN is implemented at line 88. Therefore the claim holds.\n\n"
        "## Verdict\nTRUE / HIGH\n",
        encoding="utf-8",
    )
    return p


def test_redact_proposer_evidence_strips_evidence(tmp_path: Path, monkeypatch) -> None:
    """When --redact-proposer-evidence is set, the rendered challenger
    prompt must NOT contain the proposer's evidence section."""
    proposer_path = _seed_proposer(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-for-test")
    monkeypatch.setattr(
        "audit_pipeline.commands.debate.is_available", lambda: True
    )

    captured = {}

    def fake_complete(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        class R: pass
        r = R()
        r.text = "## Verdict\nAGREE / HIGH\n"
        r.input_tokens = 100
        r.output_tokens = 50
        r.model = "test"
        r.stop_reason = "end_turn"
        return r

    monkeypatch.setattr("audit_pipeline.commands.debate.complete", fake_complete)

    from audit_pipeline.commands.debate import debate_cmd
    runner = CliRunner()
    r = runner.invoke(
        debate_cmd,
        [
            "-i", "H_TEST",
            "-v", str(proposer_path),
            "-c", "FOO_CONSTANT is constant",
            "-t", "engine/src/lib.rs",
            "--output", str(tmp_path / "debate_out"),
            "--auto",
            "--redact-proposer-evidence",
        ],
        obj={"workspace": str(tmp_path)},
    )
    assert r.exit_code == 0, r.output
    # The proposer's line citation MUST NOT be in the challenger prompt
    assert "FOO_CONSTANT = 7" not in captured["prompt"]
    assert "redacted" in captured["prompt"].lower()


def test_full_proposer_evidence_is_default(tmp_path: Path, monkeypatch) -> None:
    """Without --redact-proposer-evidence the legacy behavior preserves
    (full proposer text reaches the challenger)."""
    proposer_path = _seed_proposer(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-for-test")
    monkeypatch.setattr(
        "audit_pipeline.commands.debate.is_available", lambda: True
    )

    captured = {}

    def fake_complete(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        class R: pass
        r = R()
        r.text = "## Verdict\nAGREE / HIGH\n"
        r.input_tokens = 100
        r.output_tokens = 50
        r.model = "test"
        r.stop_reason = "end_turn"
        return r

    monkeypatch.setattr("audit_pipeline.commands.debate.complete", fake_complete)

    from audit_pipeline.commands.debate import debate_cmd
    runner = CliRunner()
    r = runner.invoke(
        debate_cmd,
        [
            "-i", "H_TEST",
            "-v", str(proposer_path),
            "-c", "FOO_CONSTANT is constant",
            "-t", "engine/src/lib.rs",
            "--output", str(tmp_path / "debate_out"),
            "--auto",
        ],
        obj={"workspace": str(tmp_path)},
    )
    assert r.exit_code == 0, r.output
    # Default: full evidence visible
    assert "FOO_CONSTANT = 7" in captured["prompt"]


def test_challenger_model_passed_to_complete(tmp_path: Path, monkeypatch) -> None:
    """When --challenger-model is set, that model name must appear in the
    complete() call so the challenger truly runs on a different backend."""
    proposer_path = _seed_proposer(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-for-test")
    monkeypatch.setattr(
        "audit_pipeline.commands.debate.is_available", lambda: True
    )

    captured = {}

    def fake_complete(prompt, **kwargs):
        captured["kwargs"] = kwargs
        class R: pass
        r = R()
        r.text = "## Verdict\nAGREE / HIGH\n"
        r.input_tokens = 100
        r.output_tokens = 50
        r.model = kwargs.get("model", "default")
        r.stop_reason = "end_turn"
        return r

    monkeypatch.setattr("audit_pipeline.commands.debate.complete", fake_complete)

    from audit_pipeline.commands.debate import debate_cmd
    runner = CliRunner()
    r = runner.invoke(
        debate_cmd,
        [
            "-i", "H_TEST",
            "-v", str(proposer_path),
            "-c", "claim",
            "-t", "x.rs",
            "--output", str(tmp_path / "debate_out"),
            "--auto",
            "--challenger-model", "claude-opus-4-5",
        ],
        obj={"workspace": str(tmp_path)},
    )
    assert r.exit_code == 0, r.output
    assert captured["kwargs"].get("model") == "claude-opus-4-5"


def test_default_no_model_argument_uses_complete_default(tmp_path: Path, monkeypatch) -> None:
    """Without --challenger-model, complete() must be called without an
    explicit model arg so the runtime default (DEFAULT_MODEL) kicks in."""
    proposer_path = _seed_proposer(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-for-test")
    monkeypatch.setattr(
        "audit_pipeline.commands.debate.is_available", lambda: True
    )

    captured = {}

    def fake_complete(prompt, **kwargs):
        captured["kwargs"] = kwargs
        class R: pass
        r = R()
        r.text = "## Verdict\nAGREE / HIGH\n"
        r.input_tokens = 100
        r.output_tokens = 50
        r.model = "default"
        r.stop_reason = "end_turn"
        return r

    monkeypatch.setattr("audit_pipeline.commands.debate.complete", fake_complete)

    from audit_pipeline.commands.debate import debate_cmd
    runner = CliRunner()
    r = runner.invoke(
        debate_cmd,
        [
            "-i", "H_TEST",
            "-v", str(proposer_path),
            "-c", "claim",
            "-t", "x.rs",
            "--output", str(tmp_path / "debate_out"),
            "--auto",
        ],
        obj={"workspace": str(tmp_path)},
    )
    assert r.exit_code == 0, r.output
    # The kwargs must NOT include model (so DEFAULT_MODEL applies in complete)
    assert "model" not in captured["kwargs"]
