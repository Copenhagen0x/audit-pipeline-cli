"""Language-specific Layer-3 formal-verification adapters.

Each adapter knows how to:

  * Build an LLM author-prompt for its language's formal-verification
    framework
  * Extract clean harness/spec source from the LLM response
  * Write the harness to the right place in the workspace
  * Run the formal verifier via its native toolchain
  * Parse the run output for a "proved" / "counterexample" signal

Supported (Phase 1f):

  * ``solana`` — Kani (Rust model checker, wraps existing synth-kani path)
  * ``c``      — CBMC (Bounded Model Checker for C)
  * ``solidity`` — solc SMTChecker (built into the Solidity compiler)
  * ``aptos``  — Move Prover (built into aptos CLI)

L3 outcomes are FormalOutcome objects with proved/counterexample
flags + the verifier's output. The hunt orchestration dispatches L3
ONLY on STRONG fires from L2.5 — the formal verification step is
expensive, so we don't burn cycles on SOFT/FALSE classifications.

A counterexample found by the verifier is the strongest possible
evidence of a bug: a constructive witness that the invariant is
violated under provably-reachable inputs.
"""

from __future__ import annotations

from audit_pipeline.formal_adapters.base import (
    SUPPORTED_LANGUAGES,
    FormalOutcome,
    LanguageFormalAdapter,
    UnsupportedLanguageError,
    get_adapter,
)

__all__ = [
    "SUPPORTED_LANGUAGES",
    "FormalOutcome",
    "LanguageFormalAdapter",
    "UnsupportedLanguageError",
    "get_adapter",
]
