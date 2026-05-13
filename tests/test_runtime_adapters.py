"""Behavioral tests for Phase 1g — Layer-4 runtime/fuzz adapters."""

from __future__ import annotations

from pathlib import Path

import pytest

from audit_pipeline.runtime_adapters import (
    SUPPORTED_LANGUAGES,
    LanguageRuntimeAdapter,
    RuntimeOutcome,
    UnsupportedLanguageError,
    get_adapter,
)


def test_supported_languages_includes_all_four() -> None:
    for lang in ("solana", "c", "solidity", "aptos"):
        assert lang in SUPPORTED_LANGUAGES


def test_get_adapter_dispatches_correctly() -> None:
    assert get_adapter("solana").fuzzer == "litesvm"
    assert get_adapter("c").fuzzer == "afl++"
    assert get_adapter("solidity").fuzzer == "forge-fuzz"
    assert get_adapter("aptos").fuzzer == "aptos-move-test"


def test_get_adapter_accepts_aliases() -> None:
    assert get_adapter("rust").fuzzer == "litesvm"
    assert get_adapter("evm").fuzzer == "forge-fuzz"
    assert get_adapter("move").fuzzer == "aptos-move-test"


def test_get_adapter_rejects_unknown() -> None:
    with pytest.raises(UnsupportedLanguageError):
        get_adapter("klingon")


def test_every_adapter_has_required_class_attrs() -> None:
    for lang in SUPPORTED_LANGUAGES:
        a = get_adapter(lang)
        assert isinstance(a, LanguageRuntimeAdapter)
        assert a.language == lang
        assert a.harness_file_extension.startswith(".")
        assert a.fuzzer


def test_build_harness_prompt_includes_hyp_metadata() -> None:
    hyp = {"id": "PHASE1G", "claim": "the claim", "engine_function": "fn_x"}
    for lang in SUPPORTED_LANGUAGES:
        a = get_adapter(lang)
        prompt = a.build_harness_prompt(hyp, "// grounded\n", Path("/tmp"))
        assert "PHASE1G" in prompt
        assert "the claim" in prompt
        assert "fn_x" in prompt


def test_parse_harness_body_extracts_fenced_block() -> None:
    fence = {
        "c":        "```c\nint main(int argc, char **argv) { return 0; }\n```\n",
        "solidity": "```solidity\ncontract H { function testFuzz_x(uint256 a) public {} }\n```\n",
        "aptos":    "```move\nmodule 0x0::p { #[test] fun property_x() {} }\n```\n",
        "solana":   "```rust\nuse litesvm::*;\n#[test] fn x() {}\n```\n",
    }
    for lang, response in fence.items():
        a = get_adapter(lang)
        body = a.parse_harness_body(response)
        assert body.strip()


def test_parse_harness_body_rejects_pure_prose() -> None:
    for lang in SUPPORTED_LANGUAGES:
        a = get_adapter(lang)
        with pytest.raises(ValueError):
            a.parse_harness_body("Sorry, can't write this.")


def test_write_harness_file_creates_path(tmp_path: Path) -> None:
    for lang in SUPPORTED_LANGUAGES:
        a = get_adapter(lang)
        path = a.write_harness_file(tmp_path, "phase1g_smoke", f"// {lang}\n")
        assert path.is_file()


def test_run_fuzzer_missing_file_raises(tmp_path: Path) -> None:
    for lang in SUPPORTED_LANGUAGES:
        a = get_adapter(lang)
        with pytest.raises(FileNotFoundError):
            a.run_fuzzer(tmp_path, "missing", tmp_path)


def test_run_fuzzer_cannot_fuzz_marker_blocks(tmp_path: Path) -> None:
    cases = {
        "c": "/* CANNOT_FUZZ: stub */\nint main(int a, char **b){return 0;}\n",
        "solidity": "// CANNOT_FUZZ: stub\ncontract H {}\n",
        "aptos": "// CANNOT_FUZZ: stub\nmodule 0x0::p { #[test] fun p() {} }\n",
        "solana": "// CANNOT_FUZZ: stub\nuse litesvm::*; #[test] fn t() {}\n",
    }
    for lang, body in cases.items():
        a = get_adapter(lang)
        a.write_harness_file(tmp_path, "phase1g_stub", body)
        outcome = a.run_fuzzer(tmp_path, "phase1g_stub", tmp_path, time_budget_s=10)
        assert outcome.crash_found is False
        assert outcome.ran_clean is False
        assert "stub" in outcome.reason.lower()


def test_runtime_outcome_to_json_shape() -> None:
    outcome = RuntimeOutcome(
        crash_found=True, ran_clean=False,
        harness_path=Path("/tmp/h.c"),
        stdout="afl++ found crash",
        stderr="",
        returncode=0, duration_s=120.5,
        fuzzer="afl++",
        reason="afl found 3 unique crashes",
        witness_inputs=[{"name": "id:000000", "size": 64, "b64": "AAAA"}],
        metadata={"n_crashes": 3},
    )
    j = outcome.to_json()
    assert j["crash_found"] is True
    assert j["fuzzer"] == "afl++"
    assert len(j["witness_inputs"]) == 1
    assert j["metadata"]["n_crashes"] == 3
