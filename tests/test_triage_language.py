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


# ────────── Phase 2 — extract_panic_line is language-aware ──────────
#
# Regression for cycle 20260513-191318 (aptos-small): extract_panic_line
# was hardcoded to Rust idioms (``panicked at`` / ``assertion failed``).
# Aptos move-test reports ``[ FAIL ]`` + ``aborted with code <N>``, so
# the function returned "" and the LLM judge classified every Aptos
# fire as FALSE ("fire signal is empty"). STRONG=0 → L3 / L4 gated out.


def test_extract_panic_line_solana_back_compat():
    """Solana extractor still picks up `panicked at` lines (the default
    language argument is `solana` so unspecified callers keep working)."""
    from audit_pipeline.layer25_triage import extract_panic_line
    log = (
        "running 1 test\n"
        "thread 'test_x' panicked at src/lib.rs:42:\n"
        "assertion `left == right` failed\n"
    )
    sig = extract_panic_line(log)  # default language
    assert "panicked at" in sig


def test_extract_panic_line_aptos_picks_up_fail_marker():
    """Aptos move-test [ FAIL ] header + tail is captured."""
    from audit_pipeline.layer25_triage import extract_panic_line
    log = (
        "INCLUDING DEPENDENCY AptosFramework\n"
        "BUILDING mutatis_generated\n"
        "Running Move unit tests\n"
        "[ FAIL    ] 0x42::test_borrow_global_no_auth::test_borrow_global_no_auth\n"
        "\n"
        "Test failures:\n"
        "\n"
        "Failures in 0x42::test_borrow_global_no_auth:\n"
        "\n"
        "┌── test_borrow_global_no_auth ──────\n"
        "│ error[E11001]: test failure\n"
        "│    │\n"
        "│  8 │     fun test_borrow_global_no_auth(...)\n"
        "│    │\n"
        "│ 32 │         assert!(...)\n"
        "│    │ Test was not expected to error, but it aborted with "
        "code 999 originating in the module 0x42::test_borrow_global_no_auth\n"
    )
    sig = extract_panic_line(log, language="aptos")
    assert "[ FAIL" in sig
    assert "aborted with code 999" in sig


def test_extract_panic_line_aptos_falls_back_to_abort_line():
    """When the FAIL header is absent but there's an `aborted with code`
    line, capture from there."""
    from audit_pipeline.layer25_triage import extract_panic_line
    log = (
        "Running test...\n"
        "VMError: aborted with code 9999 in 0x42::test_h\n"
    )
    sig = extract_panic_line(log, language="aptos")
    assert "aborted with code 9999" in sig


def test_extract_panic_line_aptos_empty_when_no_failure():
    """A clean log (all tests passed) returns empty signal."""
    from audit_pipeline.layer25_triage import extract_panic_line
    log = (
        "Running Move unit tests\n"
        "[ PASS    ] 0x42::test_x::test_x\n"
        "Test result: PASSED. Total tests: 1; passed: 1; failed: 0\n"
    )
    sig = extract_panic_line(log, language="aptos")
    assert sig == ""


def test_extract_panic_line_c_captures_asan_report():
    from audit_pipeline.layer25_triage import extract_panic_line
    log = (
        "==12345==ERROR: AddressSanitizer: heap-buffer-overflow on 0x6020\n"
        "    #0 0x4012a4 in main /src/under_test.c:42:5\n"
        "    #1 0x7ffff7a05c87 in __libc_start_main\n"
    )
    sig = extract_panic_line(log, language="c")
    assert "AddressSanitizer" in sig
    assert "heap-buffer-overflow" in sig


def test_extract_panic_line_solidity_captures_fail_marker():
    from audit_pipeline.layer25_triage import extract_panic_line
    log = (
        "Running 1 test for test/Foo.t.sol:FooTest\n"
        "[FAIL. Reason: Assertion failed.] testWithdraw() (gas: 12345)\n"
        "Traces:\n"
        "  ├─ [1234] FooTest::testWithdraw()\n"
    )
    sig = extract_panic_line(log, language="solidity")
    assert "[FAIL" in sig


# ────────── Phase 2 — judge system prompt is language-aware ──────────


def test_judge_system_prompt_per_language():
    """Each language has a distinct system prompt that mentions its own
    failure-mode idioms — the Solana prompt's references to RiskParams
    / Anchor would actively mislead a judge classifying Aptos fires."""
    from audit_pipeline.layer25_triage import _JUDGE_PROMPT_BY_LANGUAGE
    aptos = _JUDGE_PROMPT_BY_LANGUAGE["aptos"]
    solana = _JUDGE_PROMPT_BY_LANGUAGE["solana"]
    c = _JUDGE_PROMPT_BY_LANGUAGE["c"]
    solidity = _JUDGE_PROMPT_BY_LANGUAGE["solidity"]
    # Aptos prompt mentions Move-specific concepts
    assert "Move" in aptos or "move" in aptos
    assert "abort" in aptos.lower()
    assert "inverted assertion" in aptos.lower() or "E_BUG" in aptos
    # Solana prompt still references its Rust-y world
    assert "RiskParams" in solana or "params factory" in solana
    # C prompt mentions sanitizers
    assert "Sanitizer" in c or "ASan" in c
    # Solidity prompt mentions revert
    assert "revert" in solidity.lower()
    # All four are distinct
    assert len({aptos, solana, c, solidity}) == 4
