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


# ──────────────────────────────────────────────────────────────────────
# Patch #16 (audit-016) — aptos filter_name validation regressions
# ──────────────────────────────────────────────────────────────────────


def _aptos_body_with_funs(funs: list[str], extra_decorators: bool = True) -> str:
    """Build a minimal Move test body with the given fun names.

    Each name is wrapped in `#[test]` so the body looks like a real
    aptos move test source file.
    """
    blocks = []
    for fn in funs:
        if extra_decorators:
            blocks.append(f"    #[test]\n    fun {fn}() {{ }}\n")
        else:
            blocks.append(f"    fun {fn}() {{ }}\n")
    return (
        "module 0x1::jelleo_l2_p16_test {\n"
        + "\n".join(blocks)
        + "\n}\n"
    )


def test_aptos_run_poc_refuses_tainted_filter_name_unicode(tmp_path: Path) -> None:
    """A Move test body with a Unicode-letter function name must be
    refused before subprocess.run is invoked.

    Patch #16 R1 (audit HIGH 4f30bcd3): the upstream `\\w+` capture is
    Unicode-aware in Python 3, but the validation regex is ASCII-only
    per the Move identifier spec.
    """
    from audit_pipeline.poc_adapters import get_adapter
    adapter = get_adapter("aptos")
    # Use chr() to make the codepoint unambiguous regardless of how
    # this source file is encoded on disk. U+00E9 = LATIN SMALL LETTER
    # E WITH ACUTE — a valid Unicode word char that matches Python's
    # `\w` but NOT the ASCII-only `[A-Za-z]` validation regex.
    e_acute = chr(0x00E9)
    fn_name = "test_x" + e_acute + "vil_attack_payload"  # > 4 chars
    body = _aptos_body_with_funs([fn_name])
    finding = "p16_unicode"
    adapter.write_test_file(tmp_path, finding, body)
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    outcome = adapter.run_test(tmp_path, finding, repo)
    assert outcome.fired is False, (
        f"unicode fn name allowed through; reason={outcome.reason!r}"
    )
    assert outcome.returncode == -9, (
        f"expected refused returncode -9 but got {outcome.returncode}; "
        f"reason={outcome.reason!r}"
    )
    assert "tainted" in (outcome.reason or "").lower()
    # R1 F-1 fix: must propagate phase + infra_error to downstream
    assert outcome.metadata.get("phase") == "taint_refused"
    assert outcome.metadata.get("infra_error") is True


def test_aptos_run_poc_refuses_filter_name_below_length_floor(tmp_path: Path) -> None:
    """A single-character filter_name like `a` would pass the ASCII
    fullmatch but combined with aptos move test's substring-filter
    behavior and the unfiltered-fallback in run_test, could produce
    false-positive bug reproductions. Reject below 4 chars.
    """
    from audit_pipeline.poc_adapters import get_adapter
    adapter = get_adapter("aptos")
    body = _aptos_body_with_funs(["a"], extra_decorators=False)
    finding = "p16_short"
    adapter.write_test_file(tmp_path, finding, body)
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    outcome = adapter.run_test(tmp_path, finding, repo)
    assert outcome.fired is False
    assert outcome.returncode == -9
    assert outcome.metadata.get("phase") == "taint_refused"


def _capture_aptos_filter_argv(
    tmp_path: Path,
    finding: str,
    body: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str] | None, "PocOutcome"]:
    """Run the aptos adapter with subprocess.run captured.

    Returns (captured_argv_list_or_None, outcome). If the validation
    rejected the filter_name before subprocess.run was invoked, the
    captured argv is None.

    Patch #16 R2: the previous tests asserted on outcome.reason which
    was set by the FileNotFoundError path (aptos CLI absent) → the
    assertion was vacuously true regardless of filter_name choice.
    This helper mocks subprocess.run to actually capture the argv so
    we can verify the comment-stripping fix executes.
    """
    from audit_pipeline.poc_adapters import get_adapter, PocOutcome
    from audit_pipeline.poc_adapters import aptos as aptos_mod

    captured: dict[str, list[str] | None] = {"argv": None}

    class _FakeCompleted:
        def __init__(self) -> None:
            # Format that aptos.py:911-918 expects: a single PASS line
            # whose anchor is the captured filter_name. We synthesize
            # it dynamically below once we know the filter_name.
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

    def _fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        fc = _FakeCompleted()
        # Build a fake PASS line for the captured filter so the parse
        # logic at aptos.py:919-928 finds a pass_lines entry and
        # exits with a clean fired=False outcome (not infra error).
        try:
            i = argv.index("--filter")
            filter_name = argv[i + 1]
        except (ValueError, IndexError):
            filter_name = "unknown"
        fc.stdout = (
            f"Running Move unit tests\n"
            f"[ PASS    ] 0x1::jelleo_l2_test::{filter_name}\n"
            f"Test result: OK. Total tests: 1; passed: 1; failed: 0\n"
        )
        return fc

    monkeypatch.setattr(aptos_mod.subprocess, "run", _fake_run)

    adapter = get_adapter("aptos")
    adapter.write_test_file(tmp_path, finding, body)
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    outcome = adapter.run_test(tmp_path, finding, repo)
    return captured["argv"], outcome


def test_aptos_run_poc_strips_line_comments_before_filter_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threat E (comment-injected decoy): a body with a `// fun test_decoy()`
    line comment before the real `fun test_real_bug()` must NOT pick the
    decoy as the filter_name — that would run zero tests and silently
    report a false-negative bug discovery.

    Patch #16 R2: this test mocks subprocess.run to capture the actual
    --filter argv that the adapter computed, asserting the real (not
    decoy) name was selected. Without the comment-stripping fix at
    aptos.py:838-839, the decoy name would be in argv and the
    assertion would fail.
    """
    body = (
        "module 0x1::jelleo_l2_p16_decoy_test {\n"
        "    // fun test_decoy_should_not_be_picked()\n"
        "    #[test]\n"
        "    fun test_real_bug_must_be_picked() { }\n"
        "}\n"
    )
    argv, _outcome = _capture_aptos_filter_argv(
        tmp_path, "p16_decoy", body, monkeypatch,
    )
    assert argv is not None, "subprocess.run was never invoked"
    assert "--filter" in argv
    filter_idx = argv.index("--filter")
    filter_arg = argv[filter_idx + 1]
    assert filter_arg == "test_real_bug_must_be_picked", (
        f"expected 'test_real_bug_must_be_picked' (the real test) but "
        f"got {filter_arg!r} — comment-stripping at aptos.py:838-839 "
        f"failed; full argv: {argv!r}"
    )


def test_aptos_run_poc_strips_nested_block_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 hardening (paranoid-goober R2 HIGH): Move's block comments
    CAN nest (unlike Rust). Attacker exploit:
        /* /* fun test_decoy_inner() */
        fun test_decoy_exposed_between_close_tokens() */
        #[test]
        fun test_real_bug() { }

    Move parser semantics: depth tracking; whole `/* /* ... */ ... */`
    is ONE comment block; after it ends, only `fun test_real_bug()`
    is real code.

    Python's NAIVE lazy regex `/\\*.*?\\*/`:
        - Matches `/* /* fun test_decoy_inner() */` (stops at first `*/`)
        - Result: ` fun test_decoy_exposed_between_close_tokens() */`
          followed by the real test
        - `fn_matches[0]` = `test_decoy_exposed_between_close_tokens`
        - aptos move test --filter ... runs zero tests
        - False-negative bug discovery.

    R3 iterative innermost-first stripping correctly handles nesting:
        - Iter 1: strips inner `/* fun test_decoy_inner() */`
        - Iter 2: strips outer `/* \\nfun test_decoy_exposed_*_*/`
        - Result: only `#[test]\\nfun test_real_bug()` visible
        - `filter_name = "test_real_bug"` ✓
    """
    body = (
        "module 0x1::jelleo_l2_p16_nested_test {\n"
        "    /* /* fun test_decoy_inner() */\n"
        "    fun test_decoy_exposed_between_close_tokens() */\n"
        "    #[test]\n"
        "    fun test_real_bug_after_nested() { }\n"
        "}\n"
    )
    argv, _outcome = _capture_aptos_filter_argv(
        tmp_path, "p16_nested", body, monkeypatch,
    )
    assert argv is not None, "subprocess.run was never invoked"
    assert "--filter" in argv
    filter_idx = argv.index("--filter")
    filter_arg = argv[filter_idx + 1]
    assert filter_arg == "test_real_bug_after_nested", (
        f"expected 'test_real_bug_after_nested' but got {filter_arg!r} — "
        f"nested-block-comment stripping failed; a decoy hidden between "
        f"two `*/` tokens survived; full argv: {argv!r}"
    )


def test_aptos_run_poc_strips_block_comments_before_filter_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same as the line-comment variant, but with Move /* */ block
    comments. Block comments can span lines, so the regex must use
    DOTALL stripping.
    """
    body = (
        "module 0x1::jelleo_l2_p16_block_comment_test {\n"
        "    /* fun test_decoy_block_should_not_be_picked()\n"
        "       and even spans multiple lines */\n"
        "    #[test]\n"
        "    fun test_real_bug_after_block() { }\n"
        "}\n"
    )
    argv, _outcome = _capture_aptos_filter_argv(
        tmp_path, "p16_block_decoy", body, monkeypatch,
    )
    assert argv is not None, "subprocess.run was never invoked"
    assert "--filter" in argv
    filter_idx = argv.index("--filter")
    filter_arg = argv[filter_idx + 1]
    assert filter_arg == "test_real_bug_after_block", (
        f"expected 'test_real_bug_after_block' but got {filter_arg!r} — "
        f"block-comment stripping at aptos.py:839 failed; "
        f"full argv: {argv!r}"
    )


def test_aptos_run_poc_cleans_up_deployed_test_on_refusal(
    tmp_path: Path,
) -> None:
    """When the filter_name validation refuses, the deployed test file
    in `<target_repo>/tests/jelleo_l2_<name>.move` must be unlinked
    so a stale tainted body doesn't persist.
    """
    from audit_pipeline.poc_adapters import get_adapter
    adapter = get_adapter("aptos")
    body = _aptos_body_with_funs(["a"], extra_decorators=False)  # short name -> refusal
    finding = "p16_cleanup"
    adapter.write_test_file(tmp_path, finding, body)
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    outcome = adapter.run_test(tmp_path, finding, repo)
    assert outcome.fired is False
    assert outcome.returncode == -9
    deployed = repo / "tests" / f"jelleo_l2_{finding}.move"
    assert not deployed.exists(), (
        f"deployed test file not cleaned up after refusal: {deployed}"
    )


def test_aptos_run_poc_accepts_legitimate_filter_name(tmp_path: Path) -> None:
    """Sanity: a normal Move test name like `test_bug_reachable` must
    NOT trigger the validation refusal — it should proceed to the
    subprocess invocation (which will fail with FileNotFoundError if
    aptos isn't installed, but that's a different failure mode).
    """
    from audit_pipeline.poc_adapters import get_adapter
    adapter = get_adapter("aptos")
    body = _aptos_body_with_funs(["test_bug_reachable_with_admin"])
    finding = "p16_legit"
    adapter.write_test_file(tmp_path, finding, body)
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    outcome = adapter.run_test(tmp_path, finding, repo)
    # The aptos CLI is probably not installed on this runtime, so the
    # outcome is FileNotFoundError → returncode=-3, NOT -9.
    assert outcome.returncode != -9, (
        f"legitimate filter_name was incorrectly refused: "
        f"reason={outcome.reason!r}"
    )
