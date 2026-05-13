"""Base interface for the per-language Layer-3 formal-verification adapters.

Mirrors the poc_adapters/base.py shape so the hunt orchestration code
treats both L2 (empirical PoC) and L3 (formal proof) uniformly. The
key difference: a PocOutcome has a single ``fired`` flag, while a
FormalOutcome has TWO distinct truth values:

  * ``proved``           — verifier finished + invariant holds
  * ``counterexample``   — verifier finished + found a witness violating
                            the invariant (= bug constructively proven)

Both can be false (verifier timed out, ran out of memory, hit
incompleteness). Only one can be true at a time. The hunt promotes a
finding to "formally-verified bug" when ``counterexample == True``.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FormalOutcome:
    """The result of running an LLM-authored Layer-3 formal harness."""

    # The verifier completed AND proved the invariant holds for all
    # admitted inputs. Strongest possible "this is correct" signal.
    proved: bool

    # The verifier found a constructive counterexample — inputs that
    # violate the claimed invariant. Strongest possible "this is a
    # bug" signal. Mutually exclusive with proved.
    counterexample: bool

    # Path to the formal-harness source the LLM authored.
    harness_path: Path

    # Raw verifier output (truncated to keep DB rows manageable).
    stdout: str
    stderr: str

    # Exit code of the verifier process. 0 = success (verified or CE),
    # non-zero = infra failure (timeout, OOM, syntax error in harness).
    returncode: int

    # Wall time of the verifier run.
    duration_s: float

    # Which verifier ran. Useful for the dashboard's per-target column
    # and for L2.5/L3 attestation pages.
    verifier: str

    # Human-readable one-line reason for the outcome.
    reason: str

    # Per-adapter metadata (counterexample inputs, unwind bound,
    # SMTChecker engine, etc).
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "proved":         self.proved,
            "counterexample": self.counterexample,
            "harness_path":   str(self.harness_path),
            "stdout":         self.stdout[:4000],
            "stderr":         self.stderr[:4000],
            "returncode":     self.returncode,
            "duration_s":     round(self.duration_s, 3),
            "verifier":       self.verifier,
            "reason":         self.reason,
            "metadata":       self.metadata,
        }


class UnsupportedLanguageError(ValueError):
    """Raised when ``get_adapter`` receives an unknown language tag."""


class LanguageFormalAdapter(abc.ABC):
    """Contract every formal-verification adapter satisfies."""

    language: str
    harness_file_extension: str
    verifier: str  # e.g. "cbmc", "smtchecker", "move-prover", "kani"

    @abc.abstractmethod
    def build_harness_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        """Build the LLM prompt that asks an agent to author the formal harness."""

    @abc.abstractmethod
    def parse_harness_body(self, llm_response: str) -> str:
        """Extract the harness source from the LLM response. Raises ValueError if not extractable."""

    @abc.abstractmethod
    def write_harness_file(
        self,
        workspace: Path,
        harness_name: str,
        body: str,
    ) -> Path:
        """Write the harness to the right disk location for this language."""

    @abc.abstractmethod
    def run_verifier(
        self,
        workspace: Path,
        harness_name: str,
        target_repo_root: Path,
        timeout_s: int = 600,
    ) -> FormalOutcome:
        """Run the verifier. Return structured outcome.

        Raises only on infra errors (toolchain missing). Verification
        results (proved / counterexample / timeout) are encoded as
        FormalOutcome fields, not exceptions.
        """


def get_adapter(language: str) -> LanguageFormalAdapter:
    """Return the L3 formal adapter for a language. Lazy imports so
    individual verifier toolchains don't have to be present at
    import time."""
    key = (language or "").strip().lower()
    if key in ("solana", "rust", "anchor"):
        from audit_pipeline.formal_adapters.solana import SolanaFormalAdapter
        return SolanaFormalAdapter()
    if key == "c":
        from audit_pipeline.formal_adapters.c import CFormalAdapter
        return CFormalAdapter()
    if key in ("solidity", "evm"):
        from audit_pipeline.formal_adapters.solidity import SolidityFormalAdapter
        return SolidityFormalAdapter()
    if key in ("aptos", "move"):
        from audit_pipeline.formal_adapters.aptos import AptosFormalAdapter
        return AptosFormalAdapter()
    raise UnsupportedLanguageError(
        f"No L3 formal adapter for language={language!r}. "
        f"Supported: solana, c, solidity, aptos."
    )


SUPPORTED_LANGUAGES: tuple[str, ...] = ("solana", "c", "solidity", "aptos")
