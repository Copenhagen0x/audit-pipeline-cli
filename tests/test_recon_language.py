"""Behavioral tests for Phase 1b — language-aware recon system prompts.

The OSec eval audits 4 languages (Solana / C / Solidity / Aptos Move)
through the same tool-using agent loop. The recon's system prompt is
the agent's framing — wrong vocabulary leads to wrong bug-class focus
and stale verdicts. These tests pin:

  * Each language returns a DISTINCT prompt
  * Each language's prompt mentions the canonical bug classes for that
    language (proxy: keyword match against known patterns)
  * Unknown languages fall back to the Solana prompt (safe default)
  * Every prompt ends with the verdict-discipline boilerplate (so
    downstream parsing works uniformly)
  * The CLI rejects unknown --language values
"""

from __future__ import annotations

from click.testing import CliRunner

from audit_pipeline.commands.recon import (
    LANGUAGE_SYSTEM_PROMPTS,
    SUPPORTED_LANGUAGES,
    _system_prompt_for,
    recon_cmd,
)


def test_supported_languages_includes_all_four_osec_targets() -> None:
    """The OSec eval needs all 4 languages registered."""
    for lang in ("solana", "c", "solidity", "aptos"):
        assert lang in SUPPORTED_LANGUAGES, f"missing language: {lang}"


def test_each_language_returns_distinct_prompt() -> None:
    """Each language gets its own prompt — no duplicates."""
    prompts = {
        lang: _system_prompt_for(lang)
        for lang in ("solana", "c", "solidity", "aptos")
    }
    seen: set[str] = set()
    for lang, prompt in prompts.items():
        assert prompt not in seen, f"prompt for {lang} duplicates another lang"
        seen.add(prompt)


def test_solana_prompt_mentions_anchor_and_pdas() -> None:
    p = _system_prompt_for("solana")
    # Canonical Solana vocabulary
    assert "Solana" in p or "Anchor" in p
    assert "PDA" in p or "account" in p


def test_c_prompt_mentions_memory_safety_classes() -> None:
    p = _system_prompt_for("c")
    # Must reference the major C bug classes
    assert "buffer overflow" in p.lower()
    assert "use-after-free" in p.lower() or "uaf" in p.lower()
    assert "format-string" in p.lower() or "format string" in p.lower()
    # Must NOT mention Solana-only terms
    assert "Anchor" not in p
    assert "PDA" not in p


def test_solidity_prompt_mentions_evm_bug_classes() -> None:
    p = _system_prompt_for("solidity")
    assert "reentrancy" in p.lower()
    assert "oracle" in p.lower()
    assert "erc4626" in p.lower() or "share" in p.lower()
    assert "signature" in p.lower()
    # Must NOT mention Solana terms
    assert "Anchor" not in p
    assert "PDA" not in p


def test_aptos_prompt_mentions_move_specific_classes() -> None:
    p = _system_prompt_for("aptos")
    assert "borrow_global" in p.lower()
    assert "signer" in p.lower()
    assert "capability" in p.lower() or "capabilities" in p.lower()
    assert "resource" in p.lower()
    # Must NOT mention Solana terms
    assert "Anchor" not in p
    assert "PDA" not in p


def test_unknown_language_falls_back_to_solana() -> None:
    """Unknown languages don't crash — they get the Solana prompt as a safe
    default (the engine's original calibration). The CLI rejects unknowns
    upstream so this fallback only fires under operator typos."""
    p_unknown = _system_prompt_for("klingon")
    p_solana = _system_prompt_for("solana")
    assert p_unknown == p_solana


def test_every_prompt_ends_with_verdict_discipline() -> None:
    """Every language prompt must end with the verdict-discipline section so
    downstream verdict parsing (`_parse_verdict`) works uniformly."""
    for lang in SUPPORTED_LANGUAGES:
        p = _system_prompt_for(lang)
        # The base discipline includes the verdict + confidence rubric
        assert "TRUE / FALSE / INCONCLUSIVE" in p, (
            f"language={lang} prompt missing verdict rubric"
        )
        assert "HIGH / MEDIUM / LOW" in p, (
            f"language={lang} prompt missing confidence rubric"
        )
        assert "## Verdict" in p, (
            f"language={lang} prompt missing required `## Verdict` section"
        )


def test_cli_accepts_all_supported_languages() -> None:
    """The CLI --language flag's choices match SUPPORTED_LANGUAGES."""
    runner = CliRunner()
    result = runner.invoke(recon_cmd, ["--help"])
    assert result.exit_code == 0
    # Click renders choices like "[aptos|c|solana|solidity]" or similar.
    # Verify each language appears in the help output.
    for lang in SUPPORTED_LANGUAGES:
        assert lang in result.output, (
            f"language {lang!r} missing from --language CLI choices"
        )


def test_cli_rejects_unknown_language() -> None:
    """Operator typo (e.g. --language go) is rejected by Click's choice
    validator, not silently swallowed."""
    runner = CliRunner()
    result = runner.invoke(
        recon_cmd, ["--language", "klingon", "--hypotheses", "/dev/null"],
    )
    # Click exits 2 on bad option choice
    assert result.exit_code == 2
    assert "klingon" in result.output.lower() or "invalid" in result.output.lower()
