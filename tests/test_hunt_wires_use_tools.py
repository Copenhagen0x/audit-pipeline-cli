"""Regression test for the hunt → recon argv wiring (L1 Defect 01).

Cycle 20260511-183154 was retracted because Layer 1 ran in speculation-
mode — single-shot `complete()` instead of the tool-using agent. The
fix added `--use-tools` to the recon CLI, but until this commit the
autonomous hunt loop's recon subprocess invocation did NOT pass it,
defeating the fix on the only code path the operator actually runs.

This test pins the wiring: build the exact recon argv the hunt loop
would invoke and assert `--use-tools` is in there. If a future
refactor drops the flag, this test fails immediately rather than
silently regressing into the bug that cost $400.

Same coverage for the L1.5 debate independence flags.
"""

from __future__ import annotations

from pathlib import Path


def _build_recon_argv(workspace: Path, *,
                      use_tools: bool, tool_max_turns: int = 12) -> list[str]:
    """Mirror the argv shape hunt._hunt_run builds at the recon dispatch site.

    Kept in lockstep with the real construction in hunt.py; if hunt.py
    changes shape, this helper changes with it (and the assertions below
    catch any flag dropouts).
    """
    argv = [
        "audit-pipeline", "--workspace", str(workspace),
        "recon",
        "--hypotheses", "hyps.yaml",
        "--output", str(workspace / "recon"),
        "--auto",
        "--max-concurrent", "4",
        "--budget-cap-usd", "5.00",
        "--refinement-rounds", "0",
        "--ground-code",
    ]
    argv += ["--use-tools" if use_tools else "--no-use-tools"]
    if use_tools:
        argv += ["--tool-max-turns", str(tool_max_turns)]
    return argv


def _build_debate_argv(workspace: Path, *,
                       redact: bool, challenger_model: str | None) -> list[str]:
    argv = [
        "audit-pipeline", "--workspace", str(workspace),
        "debate",
        "--hypothesis-id", "H_X",
        "--proposer-verdict", "verdict.md",
        "--hypotheses-file", "hyps.yaml",
        "--output", str(workspace / "debate"),
        "--auto",
    ]
    argv += ["--redact-proposer-evidence" if redact else "--full-proposer-evidence"]
    if challenger_model:
        argv += ["--challenger-model", challenger_model]
    return argv


def test_hunt_recon_argv_includes_use_tools_when_set(tmp_path: Path) -> None:
    argv = _build_recon_argv(tmp_path, use_tools=True, tool_max_turns=12)
    assert "--use-tools" in argv
    assert "--no-use-tools" not in argv
    assert "--tool-max-turns" in argv
    idx = argv.index("--tool-max-turns")
    assert argv[idx + 1] == "12"


def test_hunt_recon_argv_includes_no_use_tools_when_unset(tmp_path: Path) -> None:
    argv = _build_recon_argv(tmp_path, use_tools=False)
    assert "--no-use-tools" in argv
    assert "--use-tools" not in argv
    # tool-max-turns only relevant under --use-tools
    assert "--tool-max-turns" not in argv


def test_hunt_debate_argv_includes_redaction_default(tmp_path: Path) -> None:
    argv = _build_debate_argv(tmp_path, redact=True, challenger_model=None)
    assert "--redact-proposer-evidence" in argv
    assert "--full-proposer-evidence" not in argv


def test_hunt_debate_argv_includes_challenger_model_when_set(tmp_path: Path) -> None:
    argv = _build_debate_argv(tmp_path, redact=True, challenger_model="claude-opus-4-5")
    assert "--challenger-model" in argv
    idx = argv.index("--challenger-model")
    assert argv[idx + 1] == "claude-opus-4-5"


def test_hunt_debate_argv_omits_challenger_model_when_unset(tmp_path: Path) -> None:
    argv = _build_debate_argv(tmp_path, redact=True, challenger_model=None)
    assert "--challenger-model" not in argv


# ─────────────────── live wiring assertion ───────────────────


def test_hunt_cmd_declares_use_tools_flag() -> None:
    """The hunt CLI must expose --use-tools so operators can override.
    Default ON is asserted separately (see below)."""
    from audit_pipeline.commands.hunt import hunt_cmd
    flag_names = {p.name for p in hunt_cmd.params}
    assert "use_tools" in flag_names
    assert "tool_max_turns" in flag_names
    assert "challenger_model" in flag_names
    assert "redact_proposer_evidence" in flag_names


def test_hunt_cmd_use_tools_default_is_on() -> None:
    """Operators who fire `audit-pipeline hunt` with no extra flags MUST
    get source-grounded recon. The retracted cycle ran on the legacy
    speculation path; the default has to be ON to prevent regression."""
    from audit_pipeline.commands.hunt import hunt_cmd
    for p in hunt_cmd.params:
        if p.name == "use_tools":
            assert p.default is True, (
                "regression: hunt --use-tools default is no longer True"
            )
        if p.name == "redact_proposer_evidence":
            assert p.default is True, (
                "regression: hunt --redact-proposer-evidence default is "
                "no longer True — debate would rubber-stamp the proposer"
            )


def test_hunt_recon_argv_matches_real_construction(tmp_path: Path) -> None:
    """Read the real hunt.py source and assert the recon argv we test
    against still matches what the code actually emits. If a refactor
    drops the wiring this assertion fails on the next pytest."""
    import audit_pipeline.commands.hunt as hunt_mod
    src = Path(hunt_mod.__file__).read_text(encoding="utf-8")
    assert '"--use-tools" if use_tools else "--no-use-tools"' in src, (
        "regression: hunt.py no longer threads use_tools into recon argv"
    )
    assert '"--tool-max-turns"' in src, (
        "regression: hunt.py no longer passes --tool-max-turns"
    )
    assert '"--redact-proposer-evidence" if redact_proposer_evidence' in src, (
        "regression: hunt.py no longer threads redact_proposer_evidence "
        "into debate argv"
    )
    assert '"--challenger-model"' in src, (
        "regression: hunt.py no longer passes --challenger-model to debate"
    )
