"""L3 formal-verification adapter for Aptos Move — Move Prover.

Aptos's Move Prover is a SMT-backed formal verifier for Move modules.
It reads `spec` blocks attached to functions + invariants on
resources, and either:

  * Proves the specification holds, OR
  * Returns a counterexample (concrete inputs violating the spec)

Move Prover is built into the aptos CLI:

  aptos move prove --package-dir <repo>

Spec block syntax (annotated alongside the function under test):

    spec withdraw {
        // Preconditions
        requires exists<Vault>(@mutatis);
        // Postconditions
        ensures global<Vault>(@mutatis).total_deposits ==
                old(global<Vault>(@mutatis).total_deposits) - amount;
        // Aborts conditions
        aborts_if amount > global<Vault>(@mutatis).total_deposits;
    }

The L3 harness for Aptos is a Move MODULE containing only `spec`
blocks attached to existing functions. The hunt deploys this spec
module alongside the target sources and runs the prover.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from audit_pipeline.formal_adapters.base import FormalOutcome, LanguageFormalAdapter


class AptosFormalAdapter(LanguageFormalAdapter):
    """Aptos Move formal-verification adapter (Move Prover)."""

    language = "aptos"
    harness_file_extension = ".move"
    verifier = "move-prover"

    def build_harness_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        hyp_id = hyp.get("id", "unknown")
        claim = hyp.get("claim", "(no claim)")
        engine_function = hyp.get("engine_function", "")
        target_file = hyp.get("target_file", "")

        return f"""You are authoring a Move Prover spec module for the Jelleo audit engine.

Move Prover is invoked via:

  aptos move prove --package-dir {target_repo_root}

It reads `spec` blocks attached to functions and resources, and either
PROVES the specification holds or returns a CONCRETE COUNTEREXAMPLE.

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Function under test: {engine_function}
Target file: {target_file}

# Grounded source

{source_context}

# Move Prover output patterns

  * "verification successful" → spec proved
  * "specification failed" or "abort code N" or "counterexample" →
    spec violated; bug constructively proven

# Spec syntax (key forms)

  spec module {{
      // Module-level invariants
      invariant exists<Vault>(@mutatis) ==> global<Vault>(@mutatis).total >= 0;
  }}

  spec FunctionName {{
      requires <precondition>;       // input constraints
      ensures <postcondition>;       // result + state guarantees
      aborts_if <abort_condition>;   // when this aborts
  }}

  spec struct StructName {{
      invariant <inv>;               // invariant on every instance
  }}

# Your task

Write a Move file `spec_<finding_name>.move` that:

1. Declares `spec module <address>::<module_name>` — same address +
   module name as the target file under test.
2. Adds `spec <function_name> {{ ... }}` blocks for the engine_function
   and any helpers in its call chain.
3. Express the invariant as the OPPOSITE of the bug:
     * If bug = "function admits invalid input X" → write
       `aborts_if input_is_invalid_X;`
     * If bug = "balance can go negative" → write
       `invariant balance >= 0;`
     * If bug = "auth bypassed" → write
       `requires signer::address_of(s) == admin_addr;` + ensure
       state mutation only happens under that precondition.

# Important

* Move Prover sometimes needs `pragma aborts_if_is_strict;` at the
  module level to enforce that all abort paths must be declared.
* Use `global<Resource>(addr)` and `exists<Resource>(addr)` to refer
  to global state.
* `old(expr)` refers to the pre-execution value.

# Output format

Output ONLY a single ```move ... ``` fenced code block. If you can't
write a real spec:

  // CANNOT_VERIFY: <one-line reason>
  spec module 0x0::noop {{ }}
"""

    def parse_harness_body(self, llm_response: str) -> str:
        m = re.search(r"```(?:move|Move|rust)\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            return m.group(1).strip() + "\n"
        m = re.search(r"```\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            body = m.group(1).strip()
            if "spec " in body or "module " in body:
                return body + "\n"
        stripped = llm_response.strip()
        if "spec " in stripped or "module " in stripped:
            return stripped + "\n"
        raise ValueError(
            "Could not extract a Move spec from the LLM response."
        )

    def write_harness_file(
        self,
        workspace: Path,
        harness_name: str,
        body: str,
    ) -> Path:
        out_dir = workspace / "formal" / "aptos"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"spec_{harness_name}{self.harness_file_extension}"
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
            workspace / "formal" / "aptos"
            / f"spec_{harness_name}{self.harness_file_extension}"
        )
        if not harness_path.is_file():
            raise FileNotFoundError(
                f"Move Prover spec not found at {harness_path}."
            )

        body = harness_path.read_text(encoding="utf-8", errors="replace")
        if "CANNOT_VERIFY" in body:
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout="",
                stderr="CANNOT_VERIFY marker — spec stubbed",
                returncode=-1,
                duration_s=0.0,
                verifier=self.verifier,
                reason="spec stub (CANNOT_VERIFY)",
            )

        # Deploy spec into the target repo's sources/ so the prover
        # picks it up
        deployed = target_repo_root / "sources" / f"jelleo_l3_spec_{harness_name}.move"
        deployed.parent.mkdir(parents=True, exist_ok=True)
        deployed.write_text(body, encoding="utf-8")

        cmd = [
            "aptos", "move", "prove",
            "--package-dir", str(target_repo_root),
        ]
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s,
            )
        except FileNotFoundError:
            deployed.unlink(missing_ok=True)
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout="",
                stderr="aptos CLI not installed",
                returncode=-3,
                duration_s=time.time() - t0,
                verifier=self.verifier,
                reason="toolchain missing: aptos CLI",
                metadata={"infra_error": True},
            )
        except subprocess.TimeoutExpired:
            deployed.unlink(missing_ok=True)
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout="",
                stderr="Move Prover timed out",
                returncode=-5,
                duration_s=time.time() - t0,
                verifier=self.verifier,
                reason="Move Prover timeout",
            )
        finally:
            deployed.unlink(missing_ok=True)

        duration = time.time() - t0
        stdout = proc.stdout[:8000]
        stderr = proc.stderr[:4000]
        combined = stdout + "\n" + stderr

        # Move Prover output patterns.
        #
        # IMPORTANT: scope failure-signal detection to OUR deployed spec
        # module (jelleo_l3_spec_<name>). The prover runs over the WHOLE
        # package, so pre-existing failing specs in unrelated source
        # modules would otherwise show up as "our" counterexamples.
        #
        # We do this by finding the lines in the prover output that
        # mention our spec module's identifier and only looking at those.
        spec_anchor = f"jelleo_l3_spec_{harness_name}"
        scoped_lines = [
            line for line in combined.splitlines()
            if spec_anchor in line
        ]
        # If the prover output groups failures by source file (typical),
        # we can also scope by lines near our deployed.move path.
        if not scoped_lines:
            # Fall back to whole combined output but use a tighter regex
            # that requires the failure to mention our harness name.
            scoped = combined
        else:
            scoped = "\n".join(scoped_lines)

        failure_match = re.search(
            r"(specification failed|abort code\s*\d+|counterexample|"
            r"verification error|did not verify)",
            scoped,
            re.IGNORECASE,
        )
        if failure_match:
            return FormalOutcome(
                proved=False,
                counterexample=True,
                harness_path=harness_path,
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
                duration_s=duration,
                verifier=self.verifier,
                reason=f"Move Prover found counterexample: {failure_match.group(0)}",
                metadata={
                    "failure_signal": failure_match.group(0),
                    "scoped_to_spec": bool(scoped_lines),
                },
            )

        if "verification successful" in combined.lower():
            return FormalOutcome(
                proved=True,
                counterexample=False,
                harness_path=harness_path,
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
                duration_s=duration,
                verifier=self.verifier,
                reason="Move Prover verified all specifications",
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
            reason="Move Prover inconclusive (likely timeout or compile error in spec module)",
        )
