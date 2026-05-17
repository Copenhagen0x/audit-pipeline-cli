"""Base interfaces for the per-language Layer-2 PoC adapters.

Defines the abstract contract every language adapter implements + the
``PocOutcome`` dataclass that downstream consumers (L2.5 auto-judge,
post-cycle gate, findings DB, dashboard) read regardless of which
language produced the result.

Adapter registration is lazy (imports happen inside ``get_adapter``)
so importing the package doesn't pull in adapters for languages the
operator isn't using on this cycle. That keeps the cold-start fast
and keeps optional toolchain dependencies (foundry binaries, aptos
CLI, etc) from being import-time required.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PocOutcome:
    """The result of running an LLM-authored Layer-2 PoC test.

    Adapter-agnostic shape so downstream consumers (L2.5 auto-judge,
    findings DB, post-cycle gate) don't need to branch on language.
    """

    # Did the test demonstrably trigger the hypothesized bug?
    # True ONLY when the language-specific fire signal is present
    # (e.g. cargo test assertion failure, ASan report, forge revert).
    fired: bool

    # Path to the test file the LLM authored. Used by the post-cycle
    # gate for symbol-grep and pseudo-pass-marker validation.
    test_path: Path

    # Raw runner output (truncated by the adapter to a reasonable size
    # so DB rows don't bloat). stderr separately because sanitizer
    # output usually goes there.
    stdout: str
    stderr: str

    # Exit code of the compile+run pipeline. 0 = passed, non-zero =
    # failed (which for fault-injection tests is the FIRED signal).
    returncode: int

    # Wall time of the compile+run pipeline (for cost/perf telemetry).
    duration_s: float

    # The test framework that ran. Useful for the dashboard's per-target
    # "framework" column + for L2.5 auto-judge prompts so the judge
    # knows what kind of output it's reading.
    framework: str

    # Human-readable one-line explanation of WHY fired is True/False.
    # Goes into the finding details_json so operators have context
    # without re-reading raw runner output.
    reason: str

    # Free-form per-adapter metadata (sanitizer kind, test name,
    # compile vs runtime failure, etc). Optional; never required.
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "fired":       self.fired,
            "test_path":   str(self.test_path),
            "stdout":      self.stdout[:4000],
            "stderr":      self.stderr[:4000],
            "returncode":  self.returncode,
            "duration_s":  round(self.duration_s, 3),
            "framework":   self.framework,
            "reason":      self.reason,
            "metadata":    self.metadata,
        }


class UnsupportedLanguageError(ValueError):
    """Raised when ``get_adapter`` receives an unknown language tag."""


class LanguagePocAdapter(abc.ABC):
    """The contract every language adapter satisfies.

    Concrete implementations live in ``poc_adapters/<language>.py``
    and are wired into ``get_adapter`` below. The abstract methods are
    enough to drive the full L2 PoC pipeline (author → write → run →
    parse) without the hunt orchestration code knowing anything
    language-specific.
    """

    # Subclasses set these as class attributes.
    language: str          # e.g. "c", "solidity"
    test_file_extension: str   # e.g. ".c", ".sol", ".move"
    framework: str          # e.g. "clang+sanitizers", "forge", "aptos-cli"

    @abc.abstractmethod
    def build_author_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
        debate_context: str | None = None,
    ) -> str:
        """Build the LLM prompt that asks an agent to author the PoC test.

        Args:
            hyp: The hypothesis dict from the loaded YAML library
                 (id, claim, target_file, engine_function,
                 relevant_instructions, etc.).
            source_context: Grounded code excerpts the agent should
                            read before writing the test (function
                            bodies + struct defs + relevant constants).
            target_repo_root: Path on disk where the source under test
                              lives. Adapters use this to reference
                              the right include paths / module paths /
                              package directories in the prompt.
            debate_context: Optional Layer-1.5 challenger response text
                            for this hypothesis. When present, gives the
                            L2 author the exact attack chain that survived
                            adversarial review — typically reduces
                            passed-no-fire by ~3x on Solidity targets.
                            Adapters MAY ignore (Solana/Aptos baseline
                            don't yet use it) but MUST accept the param.
        Returns: a prompt string ready to send to ``complete()``.
        """

    @abc.abstractmethod
    def parse_test_body(self, llm_response: str) -> str:
        """Extract the clean test source from the LLM's response.

        LLMs tend to wrap code in markdown fences and add prose. This
        method strips that down to just the test source ready for
        ``write_test_file``. Raises ``ValueError`` if nothing extractable.
        """

    @abc.abstractmethod
    def write_test_file(
        self,
        workspace: Path,
        test_name: str,
        body: str,
    ) -> Path:
        """Write the test body to the right path for this language.

        ``test_name`` is the slug derived from the hypothesis id
        (alphanumeric + underscores, capped at 60 chars by
        ``slug_for_hypothesis``). Returns the absolute path to the
        written file.
        """

    @abc.abstractmethod
    def run_test(
        self,
        workspace: Path,
        test_name: str,
        target_repo_root: Path,
        timeout_s: int = 180,
    ) -> PocOutcome:
        """Compile + run the test, return a structured outcome.

        Adapters set ``PocOutcome.fired = True`` ONLY when the test
        demonstrably triggered the hypothesized bug. A test that
        compiles cleanly + runs to a pass (no failure) means the bug
        is NOT reachable from the witness state — adapter sets
        fired=False with an explanatory reason.

        Raises only on infrastructure errors (toolchain missing,
        permission denied). Bug-finding failures are encoded as
        fired=False outcomes, not exceptions.
        """


def get_adapter(language: str) -> LanguagePocAdapter:
    """Return the adapter for a language tag.

    Imports are lazy so installing the package doesn't require every
    toolchain (foundry, aptos CLI) to be present at import time.

    Raises ``UnsupportedLanguageError`` for unknown languages so the
    caller fails fast with a clear error instead of falling back to
    a Solana adapter that would produce nonsense output on C source.
    """
    key = (language or "").strip().lower()
    if key in ("solana", "rust", "anchor"):
        from audit_pipeline.poc_adapters.solana import SolanaAdapter
        return SolanaAdapter()
    if key == "c":
        from audit_pipeline.poc_adapters.c import CAdapter
        return CAdapter()
    if key in ("solidity", "evm"):
        from audit_pipeline.poc_adapters.solidity import SolidityAdapter
        return SolidityAdapter()
    if key in ("aptos", "move"):
        from audit_pipeline.poc_adapters.aptos import AptosAdapter
        return AptosAdapter()
    raise UnsupportedLanguageError(
        f"No L2 PoC adapter for language={language!r}. "
        f"Supported: solana, c, solidity, aptos."
    )


SUPPORTED_LANGUAGES: tuple[str, ...] = ("solana", "c", "solidity", "aptos")
