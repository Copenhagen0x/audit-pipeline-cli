"""Language-specific Layer-4 runtime / fuzz adapters.

L4 is the runtime-verification layer: take a hypothesis + a target,
generate a fuzz harness, run it for a time budget, collect crashes /
invariant violations. Catches bugs that static analysis misses (e.g.
deep state-space corner cases that BMC times out on, race conditions
that only manifest under stress).

Supported (Phase 1g):

  * ``solana`` — LiteSVM (BPF runtime; wraps existing litesvm command)
  * ``c``      — AFL++ (American Fuzzy Lop, coverage-guided fuzzer)
  * ``solidity`` — Foundry ``forge fuzz`` / ``forge invariant``
  * ``aptos``  — ``aptos move test`` with property-randomized inputs

L4 outcomes are RuntimeOutcome objects with crash/violation flags +
collected witness inputs (for FALSE POC reproduction). The hunt
dispatches L4 ONLY on STRONG fires from L2.5 + L3 — runtime fuzzing
is the most expensive layer (default 60-300s per harness), so we
don't burn cycles on classifications below STRONG.

A counterexample crash collected by the fuzzer is constructive
evidence of a bug AND a ready-to-replay input for the PoC.
"""

from __future__ import annotations

from audit_pipeline.runtime_adapters.base import (
    SUPPORTED_LANGUAGES,
    LanguageRuntimeAdapter,
    RuntimeOutcome,
    UnsupportedLanguageError,
    get_adapter,
)

__all__ = [
    "SUPPORTED_LANGUAGES",
    "LanguageRuntimeAdapter",
    "RuntimeOutcome",
    "UnsupportedLanguageError",
    "get_adapter",
]
