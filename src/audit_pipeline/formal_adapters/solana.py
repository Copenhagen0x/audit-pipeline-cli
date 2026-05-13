"""L3 formal-verification adapter for Solana / Rust — Kani.

Wraps the existing ``audit-pipeline kani`` + ``synth-kani`` pipeline
so the unified L3 dispatch can call it through the same
``LanguageFormalAdapter`` interface as CBMC / SMTChecker / Move Prover.

Kani is a Rust-targeted bounded model checker (built on CBMC). It
proves safety properties on Rust code or returns concrete
counterexamples. The existing Solana pipeline already uses Kani
through ``commands/kani.py`` + ``commands/synth_kani.py``; this
adapter doesn't change that path, just exposes it under the L3
adapter abstraction.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from audit_pipeline.formal_adapters.base import FormalOutcome, LanguageFormalAdapter


class SolanaFormalAdapter(LanguageFormalAdapter):
    """Solana / Rust formal-verification adapter (Kani)."""

    language = "solana"
    harness_file_extension = ".rs"
    verifier = "kani"

    def build_harness_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        # The existing synth-kani path has a sophisticated prompt builder.
        # For uniformity we synthesize a Kani-flavored prompt here so any
        # caller that uses the adapter interface gets a working prompt
        # without going through synth-kani's CLI path.
        hyp_id = hyp.get("id", "unknown")
        claim = hyp.get("claim", "(no claim)")
        engine_function = hyp.get("engine_function", "")

        return f"""You are authoring a Kani formal-verification harness for the Jelleo audit engine.

Kani is a Rust bounded model checker. It explores ALL admitted inputs
under bounded unwinding and either PROVES the assertion holds or
returns a concrete COUNTEREXAMPLE.

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Function under test: {engine_function}

# Grounded source

{source_context}

# Run command

  cargo kani --harness kani_<name>

# Your task

Write a Rust file `kani_<finding_name>.rs` that:

1. Declares `#[kani::proof]` on a harness function.
2. Uses `kani::any()` to declare symbolic inputs.
3. Uses `kani::assume(...)` to bound the search.
4. Calls the function under test with the symbolic inputs.
5. Uses `assert!(invariant, "message")` to express the invariant
   (the OPPOSITE of the bug claim — if the assertion fails, the bug
   is real).

# Output format

Output ONLY a single ```rust ... ``` fenced code block. If you can't
write a real harness:

  // CANNOT_VERIFY: <one-line reason>
  #[kani::proof] fn noop() {{ }}
"""

    def parse_harness_body(self, llm_response: str) -> str:
        m = re.search(r"```(?:rust|Rust)\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            return m.group(1).strip() + "\n"
        m = re.search(r"```\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            body = m.group(1).strip()
            if "#[kani::proof]" in body or "fn " in body:
                return body + "\n"
        stripped = llm_response.strip()
        if "#[kani::proof]" in stripped:
            return stripped + "\n"
        raise ValueError("Could not extract a Kani harness from the LLM response.")

    def write_harness_file(
        self,
        workspace: Path,
        harness_name: str,
        body: str,
    ) -> Path:
        out_dir = workspace / "formal" / "kani"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"kani_{harness_name}{self.harness_file_extension}"
        out_path.write_text(body, encoding="utf-8")
        return out_path

    def run_verifier(
        self,
        workspace: Path,
        harness_name: str,
        target_repo_root: Path,
        timeout_s: int = 900,
    ) -> FormalOutcome:
        harness_path = (
            workspace / "formal" / "kani"
            / f"kani_{harness_name}{self.harness_file_extension}"
        )
        if not harness_path.is_file():
            raise FileNotFoundError(
                f"Kani harness not found at {harness_path}."
            )

        body = harness_path.read_text(encoding="utf-8", errors="replace")
        if "CANNOT_VERIFY" in body:
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout="",
                stderr="CANNOT_VERIFY marker — harness stubbed",
                returncode=-1,
                duration_s=0.0,
                verifier=self.verifier,
                reason="harness stub (CANNOT_VERIFY)",
            )

        # Deploy harness under target/tests/ (Cargo's integration test dir)
        deployed = target_repo_root / "tests" / f"jelleo_l3_kani_{harness_name}.rs"
        deployed.parent.mkdir(parents=True, exist_ok=True)
        deployed.write_text(body, encoding="utf-8")

        cmd = [
            "cargo", "kani",
            "--harness", f"kani_{harness_name}",
        ]
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s,
                cwd=str(target_repo_root),
            )
        except FileNotFoundError:
            deployed.unlink(missing_ok=True)
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout="",
                stderr="cargo kani not installed",
                returncode=-3,
                duration_s=time.time() - t0,
                verifier=self.verifier,
                reason="toolchain missing: cargo kani",
                metadata={"infra_error": True},
            )
        except subprocess.TimeoutExpired:
            deployed.unlink(missing_ok=True)
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout="",
                stderr="cargo kani timed out",
                returncode=-5,
                duration_s=time.time() - t0,
                verifier=self.verifier,
                reason="Kani timeout",
            )
        finally:
            deployed.unlink(missing_ok=True)

        duration = time.time() - t0
        stdout = proc.stdout[:8000]
        stderr = proc.stderr[:4000]

        # Kani output patterns mirror CBMC's underlying engine
        if "VERIFICATION FAILED" in stdout or "Failed Checks" in stdout:
            return FormalOutcome(
                proved=False,
                counterexample=True,
                harness_path=harness_path,
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
                duration_s=duration,
                verifier=self.verifier,
                reason="Kani found counterexample",
            )
        if "VERIFICATION SUCCESSFUL" in stdout or "0 of " in stdout and "failed" in stdout:
            return FormalOutcome(
                proved=True,
                counterexample=False,
                harness_path=harness_path,
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
                duration_s=duration,
                verifier=self.verifier,
                reason="Kani verification successful",
            )

        return FormalOutcome(
            proved=False,
            counterexample=False,
            harness_path=harness_path,
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode,
            duration_s=duration,
            verifier=self.verifier,
            reason="Kani inconclusive (likely compile or unwind bound exceeded)",
        )
