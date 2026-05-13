"""Behavioral tests for Phase 1d — Layer-2 PoC adapters.

Pins the contract every language adapter satisfies. The adapter
abstraction is the boundary between language-specific PoC mechanics
(compilers, test runners, fire-detection heuristics) and the
language-agnostic L2.5 / dashboard / export pipeline downstream.

Network/toolchain tests are kept minimal — the actual compile+run
paths get exercised in Phase 3 (aptos-small dry run). These tests
focus on the contract: dispatch, prompt-build, body-extract,
write-file, error handling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit_pipeline.poc_adapters import (
    SUPPORTED_LANGUAGES,
    LanguagePocAdapter,
    PocOutcome,
    UnsupportedLanguageError,
    get_adapter,
)


def test_supported_languages_includes_all_four() -> None:
    for lang in ("solana", "c", "solidity", "aptos"):
        assert lang in SUPPORTED_LANGUAGES, f"missing language: {lang}"


def test_get_adapter_dispatches_correctly() -> None:
    """Each language returns the right adapter class."""
    a_solana = get_adapter("solana")
    a_c = get_adapter("c")
    a_solidity = get_adapter("solidity")
    a_aptos = get_adapter("aptos")
    assert a_solana.language == "solana"
    assert a_c.language == "c"
    assert a_solidity.language == "solidity"
    assert a_aptos.language == "aptos"
    # All distinct classes
    classes = {type(a).__name__ for a in (a_solana, a_c, a_solidity, a_aptos)}
    assert len(classes) == 4, f"expected 4 distinct classes, got {classes}"


def test_get_adapter_accepts_aliases() -> None:
    """Adapter dispatch accepts common synonyms."""
    assert get_adapter("rust").language == "solana"
    assert get_adapter("anchor").language == "solana"
    assert get_adapter("evm").language == "solidity"
    assert get_adapter("move").language == "aptos"


def test_get_adapter_case_insensitive() -> None:
    """Adapter dispatch is case-insensitive (operators may type 'C' or 'Aptos')."""
    assert get_adapter("C").language == "c"
    assert get_adapter("APTOS").language == "aptos"
    assert get_adapter("Solidity").language == "solidity"


def test_get_adapter_rejects_unknown() -> None:
    """Unknown languages fail fast, not silently."""
    with pytest.raises(UnsupportedLanguageError) as exc:
        get_adapter("klingon")
    assert "klingon" in str(exc.value)
    assert "Supported:" in str(exc.value)


def test_every_adapter_has_required_class_attrs() -> None:
    """Adapters must declare language, extension, and framework."""
    for lang in SUPPORTED_LANGUAGES:
        a = get_adapter(lang)
        assert isinstance(a, LanguagePocAdapter)
        assert a.language == lang
        assert a.test_file_extension.startswith(".")
        assert a.framework  # non-empty


def test_extensions_match_languages() -> None:
    """The file extensions are what we expect for each language."""
    assert get_adapter("solana").test_file_extension == ".rs"
    assert get_adapter("c").test_file_extension == ".c"
    assert get_adapter("solidity").test_file_extension == ".sol"
    assert get_adapter("aptos").test_file_extension == ".move"


def test_build_author_prompt_includes_hyp_id_and_claim() -> None:
    """Every adapter's prompt must echo the hyp id + claim so the LLM
    knows what it's testing."""
    hyp = {
        "id": "PHASE1D-TEST-001",
        "claim": "the test claim — should appear in the prompt verbatim",
        "target_file": "src/foo.x",
        "engine_function": "do_thing",
        "relevant_instructions": "see foo, bar, baz",
    }
    for lang in SUPPORTED_LANGUAGES:
        a = get_adapter(lang)
        prompt = a.build_author_prompt(hyp, "// grounded source\n", Path("/tmp"))
        assert "PHASE1D-TEST-001" in prompt, f"{lang}: prompt missing hyp id"
        assert "the test claim" in prompt, f"{lang}: prompt missing claim"
        assert "do_thing" in prompt, f"{lang}: prompt missing engine_function"


def test_parse_test_body_extracts_fenced_block() -> None:
    """All adapters extract from a fenced code block."""
    fence = {
        "c": "```c\n#include <stdio.h>\nint main(void) { return 0; }\n```\n",
        "solidity": "```solidity\npragma solidity ^0.8.0;\ncontract C {}\n```\n",
        "aptos": "```move\nmodule 0x0::test { #[test] fun t() {} }\n```\n",
        "solana": "```rust\n#![cfg(feature = \"test\")]\n#[test] fn t() {}\n```\n",
    }
    for lang, response in fence.items():
        a = get_adapter(lang)
        body = a.parse_test_body(response)
        assert body.strip(), f"{lang}: empty body extracted"


def test_parse_test_body_rejects_pure_prose() -> None:
    """If the LLM returns just prose with no code, parse_test_body raises."""
    pure_prose = "I'm sorry, I cannot write this test because..."
    for lang in ("c", "solidity", "aptos"):
        a = get_adapter(lang)
        with pytest.raises(ValueError):
            a.parse_test_body(pure_prose)


def test_write_test_file_creates_path(tmp_path: Path) -> None:
    """write_test_file writes to disk + returns the absolute path."""
    for lang in SUPPORTED_LANGUAGES:
        a = get_adapter(lang)
        body = f"// {lang} test\n"
        path = a.write_test_file(tmp_path, "phase1d_smoke", body)
        assert path.is_file(), f"{lang}: file not written"
        assert path.read_text() == body
        assert path.suffix == a.test_file_extension


def test_run_test_missing_file_raises(tmp_path: Path) -> None:
    """run_test raises FileNotFoundError if write_test_file never ran."""
    for lang in SUPPORTED_LANGUAGES:
        a = get_adapter(lang)
        with pytest.raises(FileNotFoundError):
            a.run_test(tmp_path, "nonexistent", tmp_path / "fake-repo")


def test_run_test_pseudo_pass_marker_blocks_fire(tmp_path: Path) -> None:
    """PoCs containing pseudo-pass markers (CANNOT_TEST etc) never count
    as fired — caught before we even invoke the toolchain."""
    # Per-language marker that the adapter recognizes pre-compile
    cases = {
        "c": "/* CANNOT_TEST: stub */\nint main(void){return 0;}\n",
        "solidity": "// CANNOT_TEST: stub\npragma solidity ^0.8.0;\ncontract C {}\n",
        "aptos": "// CANNOT_TEST: stub\nmodule 0x0::t { #[test] fun t() {} }\n",
        "solana": "// CANNOT_TEST: stub\n#[test] #[ignore] fn t() {}\n",
    }
    for lang, body in cases.items():
        a = get_adapter(lang)
        path = a.write_test_file(tmp_path, "pseudo_pass_test", body)
        # Fake a target repo — adapter must detect the marker before
        # trying to compile, so missing toolchain doesn't matter here.
        outcome = a.run_test(tmp_path, "pseudo_pass_test", tmp_path)
        assert outcome.fired is False, f"{lang}: pseudo-pass marker should NOT fire"
        assert "pseudo-pass" in outcome.reason.lower() or "cannot_test" in outcome.reason.lower(), (
            f"{lang}: outcome.reason should mention pseudo-pass detection, got: {outcome.reason!r}"
        )


def test_poc_outcome_to_json_shape() -> None:
    """PocOutcome.to_json produces the dict shape downstream consumers expect."""
    outcome = PocOutcome(
        fired=True,
        test_path=Path("/tmp/test.c"),
        stdout="ASan caught a buffer overflow",
        stderr="==12345==ERROR: AddressSanitizer: heap-buffer-overflow",
        returncode=42,
        duration_s=1.234,
        framework="clang+sanitizers",
        reason="asan caught: heap-buffer-overflow",
        metadata={"sanitizer": "asan"},
    )
    j = outcome.to_json()
    assert j["fired"] is True
    assert j["framework"] == "clang+sanitizers"
    assert j["reason"].startswith("asan")
    assert j["returncode"] == 42
    assert j["duration_s"] == 1.234
    assert j["metadata"]["sanitizer"] == "asan"
