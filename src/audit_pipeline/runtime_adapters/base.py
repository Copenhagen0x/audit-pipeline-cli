"""Base interface for the per-language Layer-4 runtime/fuzz adapters."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RuntimeOutcome:
    """The result of running an LLM-authored Layer-4 fuzz harness."""

    # Did the fuzzer find a crash / invariant violation? True if
    # at least one witness input that broke the contract was found.
    crash_found: bool

    # Did the fuzzer run to its time budget without finding crashes?
    # Mutually exclusive with crash_found.
    ran_clean: bool

    # Path to the LLM-authored fuzz harness.
    harness_path: Path

    # Truncated runner output.
    stdout: str
    stderr: str

    # Process exit code.
    returncode: int

    # Wall time.
    duration_s: float

    # Fuzzer name (afl++ / forge-fuzz / move-test / litesvm).
    fuzzer: str

    # Human-readable reason.
    reason: str

    # When crash_found=True: the concrete input bytes (b64) or seed
    # values that triggered the violation. Used by L2.5 to write a
    # deterministic regression test from the fuzzed witness.
    witness_inputs: list[dict[str, Any]] = field(default_factory=list)

    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "crash_found":    self.crash_found,
            "ran_clean":      self.ran_clean,
            "harness_path":   str(self.harness_path),
            "stdout":         self.stdout[:4000],
            "stderr":         self.stderr[:4000],
            "returncode":     self.returncode,
            "duration_s":     round(self.duration_s, 3),
            "fuzzer":         self.fuzzer,
            "reason":         self.reason,
            "witness_inputs": self.witness_inputs,
            "metadata":       self.metadata,
        }


class UnsupportedLanguageError(ValueError):
    pass


class LanguageRuntimeAdapter(abc.ABC):
    """Contract every L4 runtime adapter satisfies."""

    language: str
    harness_file_extension: str
    fuzzer: str

    @abc.abstractmethod
    def build_harness_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        ...

    @abc.abstractmethod
    def parse_harness_body(self, llm_response: str) -> str:
        ...

    @abc.abstractmethod
    def write_harness_file(
        self,
        workspace: Path,
        harness_name: str,
        body: str,
    ) -> Path:
        ...

    @abc.abstractmethod
    def run_fuzzer(
        self,
        workspace: Path,
        harness_name: str,
        target_repo_root: Path,
        time_budget_s: int = 120,
    ) -> RuntimeOutcome:
        ...


def get_adapter(language: str) -> LanguageRuntimeAdapter:
    """Return the L4 runtime adapter for a language. Lazy imports."""
    key = (language or "").strip().lower()
    if key in ("solana", "rust", "anchor"):
        from audit_pipeline.runtime_adapters.solana import SolanaRuntimeAdapter
        return SolanaRuntimeAdapter()
    if key == "c":
        from audit_pipeline.runtime_adapters.c import CRuntimeAdapter
        return CRuntimeAdapter()
    if key in ("solidity", "evm"):
        from audit_pipeline.runtime_adapters.solidity import SolidityRuntimeAdapter
        return SolidityRuntimeAdapter()
    if key in ("aptos", "move"):
        from audit_pipeline.runtime_adapters.aptos import AptosRuntimeAdapter
        return AptosRuntimeAdapter()
    raise UnsupportedLanguageError(
        f"No L4 runtime adapter for language={language!r}. "
        f"Supported: solana, c, solidity, aptos."
    )


SUPPORTED_LANGUAGES: tuple[str, ...] = ("solana", "c", "solidity", "aptos")
