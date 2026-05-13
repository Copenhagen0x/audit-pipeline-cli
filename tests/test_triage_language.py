"""Behavioral tests for Phase 1e — language-aware L2.5 auto-judge.

Pins the contract that the L2.5 fast-path FALSE patterns + LLM judge
prompt are language-aware. The auto-judge is THE filter between L2
(PoC fire) and the customer dashboard for the OSec eval; if it
silently treats every fire as Solana-shape, we'd publish noise
findings on Solidity / C / Aptos repos.
"""

from __future__ import annotations

from audit_pipeline.layer25_triage import (
    _APTOS_FALSE_PATTERNS,
    _C_FALSE_PATTERNS,
    _SOLANA_FALSE_PATTERNS,
    _SOLIDITY_FALSE_PATTERNS,
    build_judge_user_prompt,
    classify_by_pattern,
)

# ────────── Fast-path FALSE patterns per language ──────────


def test_solana_pattern_matches_riskparams_overflow() -> None:
    """The cycle-20260511 panic pattern still fires under language='solana'."""
    panic = "thread 'test_x' panicked at 'invalid RiskParams: Overflow', ..."
    result = classify_by_pattern(panic, language="solana")
    assert result is not None
    assert result[0] == "FALSE"
    assert "RiskParams" in result[1]


def test_c_pattern_matches_asan_inside_test_file() -> None:
    """ASan hit in the test_*.c file itself is a setup-error fast-path FALSE."""
    panic = (
        "==12345==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x6020...\n"
        "    #0 0x4012a4 in main /tmp/poc/test_foo.c:42:5"
    )
    result = classify_by_pattern(panic, language="c")
    assert result is not None
    assert result[0] == "FALSE"
    assert "test_*.c" in result[1] or "test file itself" in result[1]


def test_solidity_pattern_matches_setup_failure() -> None:
    """Foundry setUp() failure is a setup-phase FALSE."""
    panic = "setUp() failed: Error: TokenNotDeployed"
    result = classify_by_pattern(panic, language="solidity")
    assert result is not None
    assert result[0] == "FALSE"


def test_aptos_pattern_matches_move_compile_error() -> None:
    """Move compile error → FALSE (broken PoC, not a real fire)."""
    panic = "error[E03002]: unbound module 'aptos_framework::missing'"
    result = classify_by_pattern(panic, language="aptos")
    assert result is not None
    assert result[0] == "FALSE"


def test_unknown_language_falls_back_to_solana_patterns() -> None:
    """klingon → Solana patterns (engine's original calibration)."""
    panic = "thread 'test_x' panicked at 'invalid RiskParams: Overflow', ..."
    result = classify_by_pattern(panic, language="klingon")
    assert result is not None
    assert result[0] == "FALSE"


def test_empty_panic_line_returns_none() -> None:
    """No panic line → no fast-path match."""
    assert classify_by_pattern("", language="solana") is None
    assert classify_by_pattern("   ", language="c") is None


def test_legitimate_fire_passes_through() -> None:
    """A genuine engine-side panic doesn't match any FALSE pattern —
    falls through to the LLM judge."""
    panic = (
        "thread 'test_residual_grows' panicked at "
        "'assertion failed: residual_before == residual_after', "
        "src/percolator.rs:1684"
    )
    assert classify_by_pattern(panic, language="solana") is None


def test_each_language_has_at_least_one_pattern() -> None:
    """Every supported language has a non-empty FALSE pattern set."""
    assert len(_SOLANA_FALSE_PATTERNS) > 0
    assert len(_C_FALSE_PATTERNS) > 0
    assert len(_SOLIDITY_FALSE_PATTERNS) > 0
    assert len(_APTOS_FALSE_PATTERNS) > 0


def test_pattern_sets_are_distinct_per_language() -> None:
    """No two languages share the same pattern set (defense against
    accidentally aliasing language → wrong pattern dict)."""
    assert _SOLANA_FALSE_PATTERNS is not _C_FALSE_PATTERNS
    assert _SOLANA_FALSE_PATTERNS is not _SOLIDITY_FALSE_PATTERNS
    assert _SOLANA_FALSE_PATTERNS is not _APTOS_FALSE_PATTERNS
    assert _C_FALSE_PATTERNS is not _SOLIDITY_FALSE_PATTERNS


# ────────── Judge user-prompt language awareness ──────────


def test_judge_prompt_uses_rust_fence_for_solana() -> None:
    prompt = build_judge_user_prompt(
        "H1", "claim", "bug-class", "fn_name", "fn foo() {}", "panic msg",
        language="solana",
    )
    assert "```rust" in prompt


def test_judge_prompt_uses_c_fence_for_c() -> None:
    prompt = build_judge_user_prompt(
        "H1", "claim", "bug-class", "fn_name",
        "int main(void) { return 0; }", "ASan report",
        language="c",
    )
    assert "```c" in prompt
    assert "Language: c" in prompt


def test_judge_prompt_uses_solidity_fence_for_solidity() -> None:
    prompt = build_judge_user_prompt(
        "H1", "claim", "bug-class", "fn_name",
        "contract C {}", "forge revert",
        language="solidity",
    )
    assert "```solidity" in prompt
    assert "Language: solidity" in prompt


def test_judge_prompt_uses_move_fence_for_aptos() -> None:
    prompt = build_judge_user_prompt(
        "H1", "claim", "bug-class", "fn_name",
        "module 0x0::t {}", "abort code 42",
        language="aptos",
    )
    assert "```move" in prompt
    assert "Language: aptos" in prompt


def test_judge_prompt_includes_framework_when_provided() -> None:
    prompt = build_judge_user_prompt(
        "H1", "claim", "bug-class", "fn_name", "// body", "// signal",
        language="c", framework="clang+sanitizers",
    )
    assert "clang+sanitizers" in prompt


def test_judge_prompt_omits_framework_line_when_not_provided() -> None:
    prompt = build_judge_user_prompt(
        "H1", "claim", "bug-class", "fn_name", "// body", "// signal",
        language="solana",
    )
    assert "Test framework:" not in prompt
