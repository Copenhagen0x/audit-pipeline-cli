"""Tests for `audit-pipeline recon --use-tools` (L1 recon Defect 01 fix).

The legacy single-shot mode submits a prompt and parses the verdict.
The tool-using mode lets the agent call read_file/grep/find_function
against the live workspace and iterate until it has concrete evidence.
This file exercises the CLI surface + dispatch branch without hitting
the live Anthropic API (we monkeypatch `run_tool_using_agent`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner


def _seed_workspace(tmp_path: Path) -> Path:
    """Build a minimal workspace + hypotheses YAML the recon CLI accepts."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "engine").mkdir()
    (ws / "wrapper").mkdir()
    (ws / "workspace.json").write_text(
        json.dumps({
            "engine":  {"repo": "x/y", "sha": "aa" * 20,
                        "local": "engine"},
            "wrapper": {"repo": "x/y", "sha": "bb" * 20,
                        "local": "wrapper"},
        }),
        encoding="utf-8",
    )
    # Minimal source tree so prompt rendering succeeds
    (ws / "engine" / "src").mkdir()
    (ws / "engine" / "src" / "lib.rs").write_text(
        "pub fn touch() {}\n", encoding="utf-8"
    )
    hyp_path = tmp_path / "hyps.yaml"
    hyp_path.write_text(
        "hypotheses:\n"
        "  - id: H_TEST_USE_TOOLS\n"
        "    class: implicit_invariant\n"
        "    claim: 'touch fn always returns'\n"
        "    target_file: engine/src/lib.rs\n",
        encoding="utf-8",
    )
    return ws, hyp_path


def test_use_tools_flag_calls_tool_using_agent(tmp_path: Path, monkeypatch) -> None:
    """When --use-tools is set, _dispatch_one calls run_tool_using_agent,
    NOT the single-shot complete()."""
    ws, hyp_path = _seed_workspace(tmp_path)

    calls = {"tool_using": 0, "complete": 0}

    def fake_tool_using_agent(workspace, system_prompt, user_msg, **kw):
        calls["tool_using"] += 1
        from audit_pipeline.utils.llm_tools import ToolUsingResult
        return ToolUsingResult(
            text=(
                "Investigated touch() at engine/src/lib.rs:1. "
                "Function is empty, claim verified.\n\n"
                "## Verdict\nTRUE / HIGH"
            ),
            input_tokens=1500,
            output_tokens=400,
            n_turns=3,
            tool_calls=[{"tool": "read_file", "input": {"path": "engine/src/lib.rs"}}],
            stop_reason="end_turn",
        )

    def fake_complete(prompt):
        calls["complete"] += 1
        class R: pass
        r = R()
        r.text = "## Verdict\nFALSE / LOW"
        r.input_tokens = 100
        r.output_tokens = 50
        r.model = "single-shot"
        r.stop_reason = "end_turn"
        return r

    monkeypatch.setattr(
        "audit_pipeline.utils.llm_tools.run_tool_using_agent",
        fake_tool_using_agent,
    )
    monkeypatch.setattr("audit_pipeline.commands.recon.complete", fake_complete)
    monkeypatch.setattr(
        "audit_pipeline.commands.recon.is_available", lambda: True
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-for-test")

    from audit_pipeline.commands.recon import recon_cmd
    runner = CliRunner()
    r = runner.invoke(
        recon_cmd,
        [
            "--hypotheses", str(hyp_path),
            "--output", str(tmp_path / "recon_out"),
            "--auto", "--use-tools",
            "--max-concurrent", "1",
        ],
        obj={"workspace": str(ws)},
        catch_exceptions=False,
    )
    assert r.exit_code == 0, r.output
    assert calls["tool_using"] == 1, (
        f"--use-tools should call run_tool_using_agent; saw "
        f"tool_using={calls['tool_using']}, complete={calls['complete']}"
    )
    assert calls["complete"] == 0
    # Summary records the use_tools mode
    summary = json.loads(
        (tmp_path / "recon_out" / "recon_summary.json").read_text(encoding="utf-8")
    )
    assert summary["use_tools"] is True


def test_default_mode_uses_single_shot_complete(tmp_path: Path, monkeypatch) -> None:
    """Without --use-tools the legacy single-shot path runs (backward compat)."""
    ws, hyp_path = _seed_workspace(tmp_path)
    calls = {"tool_using": 0, "complete": 0}

    def fake_tool_using_agent(*a, **kw):
        calls["tool_using"] += 1
        raise AssertionError("should not be called without --use-tools")

    def fake_complete(prompt):
        calls["complete"] += 1
        class R: pass
        r = R()
        r.text = "## Verdict\nTRUE / HIGH"
        r.input_tokens = 100
        r.output_tokens = 50
        r.model = "single-shot"
        r.stop_reason = "end_turn"
        return r

    monkeypatch.setattr(
        "audit_pipeline.utils.llm_tools.run_tool_using_agent",
        fake_tool_using_agent,
    )
    monkeypatch.setattr("audit_pipeline.commands.recon.complete", fake_complete)
    monkeypatch.setattr(
        "audit_pipeline.commands.recon.is_available", lambda: True
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-for-test")

    from audit_pipeline.commands.recon import recon_cmd
    runner = CliRunner()
    r = runner.invoke(
        recon_cmd,
        [
            "--hypotheses", str(hyp_path),
            "--output", str(tmp_path / "recon_out_legacy"),
            "--auto",
            "--max-concurrent", "1",
        ],
        obj={"workspace": str(ws)},
    )
    assert r.exit_code == 0, r.output
    assert calls["complete"] == 1
    assert calls["tool_using"] == 0
    summary = json.loads(
        (tmp_path / "recon_out_legacy" / "recon_summary.json").read_text(encoding="utf-8")
    )
    assert summary["use_tools"] is False
