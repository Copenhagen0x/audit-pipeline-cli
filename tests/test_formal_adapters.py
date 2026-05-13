"""Behavioral tests for Phase 1f — Layer-3 formal-verification adapters.

Pins the contract every formal-verification adapter satisfies. Same
shape as the L2 PoC adapter tests but for the formal-proof layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit_pipeline.formal_adapters import (
    SUPPORTED_LANGUAGES,
    FormalOutcome,
    LanguageFormalAdapter,
    UnsupportedLanguageError,
    get_adapter,
)


def test_supported_languages_includes_all_four() -> None:
    for lang in ("solana", "c", "solidity", "aptos"):
        assert lang in SUPPORTED_LANGUAGES, f"missing language: {lang}"


def test_get_adapter_dispatches_correctly() -> None:
    a_solana = get_adapter("solana")
    a_c = get_adapter("c")
    a_solidity = get_adapter("solidity")
    a_aptos = get_adapter("aptos")
    assert a_solana.verifier == "kani"
    assert a_c.verifier == "cbmc"
    assert a_solidity.verifier == "smtchecker"
    assert a_aptos.verifier == "move-prover"


def test_get_adapter_accepts_aliases() -> None:
    assert get_adapter("rust").verifier == "kani"
    assert get_adapter("evm").verifier == "smtchecker"
    assert get_adapter("move").verifier == "move-prover"


def test_get_adapter_case_insensitive() -> None:
    assert get_adapter("C").verifier == "cbmc"
    assert get_adapter("APTOS").verifier == "move-prover"


def test_get_adapter_rejects_unknown() -> None:
    with pytest.raises(UnsupportedLanguageError) as exc:
        get_adapter("klingon")
    assert "klingon" in str(exc.value)


def test_every_adapter_has_required_class_attrs() -> None:
    for lang in SUPPORTED_LANGUAGES:
        a = get_adapter(lang)
        assert isinstance(a, LanguageFormalAdapter)
        assert a.language == lang
        assert a.harness_file_extension.startswith(".")
        assert a.verifier


def test_extensions_match_languages() -> None:
    """Harness file extensions match the verifier's input language."""
    assert get_adapter("solana").harness_file_extension == ".rs"
    assert get_adapter("c").harness_file_extension == ".c"
    assert get_adapter("solidity").harness_file_extension == ".sol"
    assert get_adapter("aptos").harness_file_extension == ".move"


def test_build_harness_prompt_includes_hyp_id_claim_function() -> None:
    hyp = {
        "id": "PHASE1F-TEST-001",
        "claim": "the L3 test claim — must appear in prompt verbatim",
        "target_file": "src/foo.x",
        "engine_function": "verify_thing",
    }
    for lang in SUPPORTED_LANGUAGES:
        a = get_adapter(lang)
        prompt = a.build_harness_prompt(hyp, "// grounded source\n", Path("/tmp"))
        assert "PHASE1F-TEST-001" in prompt, f"{lang}: prompt missing hyp id"
        assert "the L3 test claim" in prompt, f"{lang}: prompt missing claim"
        assert "verify_thing" in prompt, f"{lang}: prompt missing engine_function"


def test_parse_harness_body_extracts_fenced_block() -> None:
    """All adapters extract from a fenced code block matching their language."""
    fence = {
        "c": "```c\nint main(void) { __CPROVER_assert(1, \"\"); return 0; }\n```\n",
        "solidity": "```solidity\npragma solidity ^0.8.20;\ncontract H {}\n```\n",
        "aptos": "```move\nspec module 0x0::t { }\n```\n",
        "solana": "```rust\n#[kani::proof] fn h() {}\n```\n",
    }
    for lang, response in fence.items():
        a = get_adapter(lang)
        body = a.parse_harness_body(response)
        assert body.strip(), f"{lang}: empty body"


def test_parse_harness_body_rejects_pure_prose() -> None:
    pure_prose = "I cannot write this harness because..."
    for lang in ("c", "solidity", "aptos", "solana"):
        a = get_adapter(lang)
        with pytest.raises(ValueError):
            a.parse_harness_body(pure_prose)


def test_write_harness_file_creates_path(tmp_path: Path) -> None:
    for lang in SUPPORTED_LANGUAGES:
        a = get_adapter(lang)
        body = f"// {lang} harness\n"
        path = a.write_harness_file(tmp_path, "phase1f_smoke", body)
        assert path.is_file(), f"{lang}: file not written"
        assert path.read_text() == body
        assert path.suffix == a.harness_file_extension


def test_run_verifier_missing_file_raises(tmp_path: Path) -> None:
    for lang in SUPPORTED_LANGUAGES:
        a = get_adapter(lang)
        with pytest.raises(FileNotFoundError):
            a.run_verifier(tmp_path, "nonexistent", tmp_path / "fake")


def test_run_verifier_cannot_verify_marker_blocks(tmp_path: Path) -> None:
    """CANNOT_VERIFY in the harness body = no verifier run, returns stub outcome."""
    cases = {
        "c":        "/* CANNOT_VERIFY: stub */\nint main(void){return 0;}\n",
        "solidity": "// CANNOT_VERIFY: stub\ncontract C {}\n",
        "aptos":    "// CANNOT_VERIFY: stub\nspec module 0x0::n {}\n",
        "solana":   "// CANNOT_VERIFY: stub\n#[kani::proof] fn n() {}\n",
    }
    for lang, body in cases.items():
        a = get_adapter(lang)
        a.write_harness_file(tmp_path, "phase1f_cannot_verify", body)
        outcome = a.run_verifier(tmp_path, "phase1f_cannot_verify", tmp_path)
        assert outcome.proved is False
        assert outcome.counterexample is False
        assert "stub" in outcome.reason.lower() or "cannot_verify" in outcome.reason.lower()


def test_formal_outcome_to_json_shape() -> None:
    """FormalOutcome.to_json shape covers all expected keys."""
    outcome = FormalOutcome(
        proved=False,
        counterexample=True,
        harness_path=Path("/tmp/h.c"),
        stdout="VERIFICATION FAILED",
        stderr="",
        returncode=10,
        duration_s=2.5,
        verifier="cbmc",
        reason="CBMC found counterexample",
        metadata={"assertion": "bounds check"},
    )
    j = outcome.to_json()
    assert j["counterexample"] is True
    assert j["proved"] is False
    assert j["verifier"] == "cbmc"
    assert j["duration_s"] == 2.5
    assert j["metadata"]["assertion"] == "bounds check"


def test_proved_and_counterexample_are_mutually_exclusive_in_practice() -> None:
    """We don't enforce the invariant in __init__ (dataclass) but the
    practical contract is: at most one of proved/counterexample is
    True. Verify the CANNOT_VERIFY-stub outcomes hold this."""
    a = get_adapter("c")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        a.write_harness_file(tdp, "muex_test", "/* CANNOT_VERIFY: stub */\nint main(void){return 0;}\n")
        outcome = a.run_verifier(tdp, "muex_test", tdp)
        assert not (outcome.proved and outcome.counterexample), (
            "proved and counterexample MUST NOT both be True"
        )
