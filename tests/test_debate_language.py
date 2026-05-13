"""Behavioral tests for Phase 1c — language-aware L1.5 debate.

The challenger prompt's universal failure modes (doc-comment trust,
path collapse, etc.) apply to every audit. For the OSec eval the
challenger also needs LANGUAGE-SPECIFIC failure modes injected — so
it knows to check Solidity-flavored reentrancy variants vs Move-flavored
borrow_global misuse vs C-flavored UAF/UAF.

These tests pin:
  * Per-language context is distinct
  * Each language's context mentions the canonical bug classes
  * Unknown language → fall back to Solana
  * Template has the {LANGUAGE_CONTEXT} placeholder
  * Final rendered prompt contains the language-specific block
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from audit_pipeline.commands.debate import (
    SUPPORTED_LANGUAGES,
    _challenger_language_context,
    debate_cmd,
)


def test_supported_languages_includes_all_four_osec_targets() -> None:
    for lang in ("solana", "c", "solidity", "aptos"):
        assert lang in SUPPORTED_LANGUAGES, f"missing language: {lang}"


def test_each_language_returns_distinct_context() -> None:
    contexts = {
        lang: _challenger_language_context(lang)
        for lang in ("solana", "c", "solidity", "aptos")
    }
    seen: set[str] = set()
    for lang, ctx in contexts.items():
        assert ctx not in seen, f"context for {lang} duplicates another lang"
        seen.add(ctx)


def test_solana_context_mentions_account_validation() -> None:
    ctx = _challenger_language_context("solana")
    assert "Solana" in ctx
    assert "account" in ctx.lower()
    assert "signer" in ctx.lower()


def test_c_context_mentions_uaf_and_overflow() -> None:
    ctx = _challenger_language_context("c")
    assert "off-by-one" in ctx.lower() or "buffer" in ctx.lower()
    assert "uaf" in ctx.lower() or "use-after-free" in ctx.lower() or "double-free" in ctx.lower()
    assert "overflow" in ctx.lower()
    assert "PDA" not in ctx, "C context shouldn't mention Solana PDAs"


def test_solidity_context_mentions_reentrancy_variants() -> None:
    ctx = _challenger_language_context("solidity")
    assert "reentrancy" in ctx.lower()
    assert "erc4626" in ctx.lower() or "erc777" in ctx.lower() or "share" in ctx.lower()
    assert "oracle" in ctx.lower()
    # Note about obfuscated identifiers in OSec contracts
    assert "obfuscated" in ctx.lower() or "_v_" in ctx


def test_aptos_context_mentions_borrow_global_and_signer() -> None:
    ctx = _challenger_language_context("aptos")
    assert "borrow_global" in ctx
    assert "signer" in ctx.lower()
    assert "capability" in ctx.lower() or "capabilities" in ctx.lower()


def test_unknown_language_falls_back_to_solana() -> None:
    assert _challenger_language_context("klingon") == _challenger_language_context("solana")


def test_debate_template_has_language_context_placeholder() -> None:
    """The challenger template must declare the {LANGUAGE_CONTEXT}
    placeholder — without it, _challenger_language_context's output
    has nowhere to go."""
    from audit_pipeline import __file__ as pkg_init
    template_path = (
        Path(pkg_init).parent / "templates" / "agent_prompts" / "13_adversarial_debate.md"
    )
    template = template_path.read_text(encoding="utf-8")
    assert "{LANGUAGE_CONTEXT}" in template, (
        "Phase 1c requires {LANGUAGE_CONTEXT} placeholder in the "
        "challenger template — debate.py renders into this slot."
    )


def test_cli_accepts_all_supported_languages() -> None:
    runner = CliRunner()
    result = runner.invoke(debate_cmd, ["--help"])
    assert result.exit_code == 0
    for lang in SUPPORTED_LANGUAGES:
        assert lang in result.output, (
            f"language {lang!r} missing from --language CLI choices"
        )


def test_cli_rejects_unknown_language() -> None:
    runner = CliRunner()
    # We don't need to provide all required args to test --language validation;
    # Click validates choices before required-options enforcement.
    result = runner.invoke(
        debate_cmd, ["--language", "klingon"],
    )
    assert result.exit_code == 2
    assert "klingon" in result.output.lower() or "invalid" in result.output.lower()
