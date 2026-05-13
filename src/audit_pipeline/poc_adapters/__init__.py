"""Language-specific Layer-2 PoC adapters.

Each adapter knows how to:

  * Build an LLM author-prompt for its language's test framework
  * Extract clean test code from the LLM response
  * Write the test to the right place in the workspace
  * Compile + run the test via the language's toolchain
  * Parse the run output for a "fired" signal (the test demonstrably
    triggered the hypothesized bug)

Supported (Phase 1d):

  * ``solana`` — Rust + Anchor + cargo test (wraps existing poc_llm path)
  * ``c``      — clang + ASan/UBSan
  * ``solidity`` — Foundry ``forge test``
  * ``aptos``  — ``aptos move test``

The hunt pipeline picks the right adapter via the workspace's
``language`` tag and runs L2 PoC uniformly across languages. Each
adapter is responsible for its own fire-detection heuristics — the
"did this PoC actually trigger the bug?" question has a different
answer in each toolchain (test assertion in cargo, sanitizer report
in clang, JSON failure in forge, abort code in move).

L2.5 auto-judge runs on top of these PocOutcome objects regardless of
language, so the downstream pipeline is language-agnostic past this
point.
"""

from __future__ import annotations

from audit_pipeline.poc_adapters.base import (
    SUPPORTED_LANGUAGES,
    LanguagePocAdapter,
    PocOutcome,
    UnsupportedLanguageError,
    get_adapter,
)

__all__ = [
    "SUPPORTED_LANGUAGES",
    "LanguagePocAdapter",
    "PocOutcome",
    "UnsupportedLanguageError",
    "get_adapter",
]
