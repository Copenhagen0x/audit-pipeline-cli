"""L3 formal-verification adapter for Solidity — solc SMTChecker.

The Solidity compiler ships with a built-in SMTChecker that can prove
or disprove safety properties on the AST level using CHC (Constrained
Horn Clauses, the default) or BMC engines, backed by Z3 / Eldarica /
CVC4.

Approach: the LLM-authored Solidity harness contains:

  * An `import` of the contract under test
  * A wrapper contract that exposes the function under test through
    `__VERIFIER` entry points
  * `assert(invariant)` statements expressing what should hold
  * Optional `require(...)` to bound the input space (e.g. balance < 1e30)

solc with `--model-checker-engine chc --model-checker-targets all`
will report:

  * `Warning: CHC: Assertion violation happens here. <counterexample>`
    → bug constructively proven (counterexample = True)
  * `Info: CHC: 0 verification conditions remained` (or all
    assertions proved) → invariant holds (proved = True)
  * Timeout / unsupported → neither flag set

SMTChecker is unique because it's BUILT INTO the compiler — no
separate install. Just call `solc` with the right flags.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from audit_pipeline.formal_adapters.base import FormalOutcome, LanguageFormalAdapter


class SolidityFormalAdapter(LanguageFormalAdapter):
    """Solidity formal-verification adapter (solc SMTChecker)."""

    language = "solidity"
    harness_file_extension = ".sol"
    verifier = "smtchecker"

    def build_harness_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        hyp_id = hyp.get("id", "unknown")
        claim = hyp.get("claim", "(no claim)")
        engine_function = hyp.get("engine_function", "")

        return f"""You are authoring a Solidity SMTChecker harness for the Jelleo audit engine.

The Solidity compiler's built-in SMTChecker proves or disproves
safety properties via CHC. It runs via:

  solc \\
    --model-checker-engine chc \\
    --model-checker-targets all \\
    --model-checker-timeout 30000 \\
    --model-checker-show-unproved \\
    harness_<name>.sol

SMTChecker reports:
  * "CHC: Assertion violation happens here" → counterexample found
  * "CHC: 0 verification conditions remained" → all assertions proved

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Function under test: {engine_function}

# Grounded source

{source_context}

# Your task

Write a single Solidity file `harness_<finding_name>.sol` that:

1. Uses `pragma solidity ^0.8.20;`
2. Imports the contract under test from `src/` using relative paths.
3. Defines a `contract Harness` that:
     (a) Has state variables matching the protocol's relevant state.
     (b) Has functions exercising the bug-claim path. Each function:
         - Uses `require(precondition)` to bound the input space.
         - Calls the function under test.
         - Asserts the invariant via `assert(invariant_holds)`.
4. Express the invariant as the OPPOSITE of the bug — if SMTChecker
   finds a counterexample, the bug is real.

# Examples of invariants

  * Conservation: `assert(totalDeposits == sumOfBalances);`
  * Auth: `assert(msg.sender == owner || !privilegedAction);`
  * Arithmetic: `assert(newBalance <= oldBalance + deposited);`
  * Reentrancy: `assert(_locked || !inAction);`

# Important

* SMTChecker is most effective with SIMPLE state spaces. Keep the
  harness contract minimal — don't import the entire vendor tree.
* Use `require(...)` to bound `block.timestamp` and balance values.
* Add `pragma experimental SMTChecker;` only if the contract uses
  pre-0.8 syntax (most don't).
* Loops are unrolled — keep loop counts bounded by `require(i < 16)`.

# Output format

Output ONLY a single ```solidity ... ``` fenced code block. If you can't
write a real harness:

  // CANNOT_VERIFY: <one-line reason>
  pragma solidity ^0.8.20;
  contract NoOpHarness {{ }}
"""

    def parse_harness_body(self, llm_response: str) -> str:
        m = re.search(r"```(?:solidity|sol|Solidity)\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            return m.group(1).strip() + "\n"
        m = re.search(r"```\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            body = m.group(1).strip()
            if "pragma solidity" in body or "contract " in body:
                return body + "\n"
        stripped = llm_response.strip()
        if "pragma solidity" in stripped or "contract " in stripped:
            return stripped + "\n"
        raise ValueError(
            "Could not extract a Solidity harness from the LLM response."
        )

    def write_harness_file(
        self,
        workspace: Path,
        harness_name: str,
        body: str,
    ) -> Path:
        out_dir = workspace / "formal" / "solidity"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"harness_{harness_name}{self.harness_file_extension}"
        out_path.write_text(body, encoding="utf-8")
        return out_path

    def run_verifier(
        self,
        workspace: Path,
        harness_name: str,
        target_repo_root: Path,
        timeout_s: int = 600,
    ) -> FormalOutcome:
        harness_path = (
            workspace / "formal" / "solidity"
            / f"harness_{harness_name}{self.harness_file_extension}"
        )
        if not harness_path.is_file():
            raise FileNotFoundError(
                f"SMTChecker harness not found at {harness_path}."
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

        # Deploy harness into the target repo so its imports resolve
        deployed_dir = target_repo_root / "src"
        deployed_dir.mkdir(parents=True, exist_ok=True)
        deployed = deployed_dir / f"jelleo_l3_{harness_name}.sol"
        deployed.write_text(body, encoding="utf-8")

        # Use Foundry's solc invocation (foundry resolves remappings)
        cmd = [
            "forge", "build",
            "--via-ir",  # SMTChecker needs IR
            "--use", "solc",
            "--extra-output-files", "abi",
            # SMTChecker config via solc args
            "--config-path", "foundry.toml",
        ]
        # Fall back to raw solc if forge isn't suitable for SMTChecker
        # (forge doesn't expose --model-checker-* flags directly).
        # Use solc directly:
        cmd = [
            "solc",
            "--model-checker-engine", "chc",
            "--model-checker-targets", "all",
            "--model-checker-timeout", str(min(timeout_s * 1000 // 2, 60000)),
            "--model-checker-show-unproved",
            "--allow-paths", str(target_repo_root),
            str(deployed),
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
                stderr="solc not installed",
                returncode=-3,
                duration_s=time.time() - t0,
                verifier=self.verifier,
                reason="toolchain missing: solc",
                metadata={"infra_error": True},
            )
        except subprocess.TimeoutExpired:
            deployed.unlink(missing_ok=True)
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout="",
                stderr="solc SMTChecker timed out",
                returncode=-5,
                duration_s=time.time() - t0,
                verifier=self.verifier,
                reason="SMTChecker timeout",
            )
        finally:
            deployed.unlink(missing_ok=True)

        duration = time.time() - t0
        stdout = proc.stdout[:8000]
        stderr = proc.stderr[:4000]
        combined = stdout + "\n" + stderr

        # SMTChecker output patterns
        violation_found = (
            "CHC: Assertion violation happens" in combined
            or "BMC: Assertion violation" in combined
            or "Assertion violation found" in combined
        )
        # Counterexample line follows the violation
        ce_match = re.search(
            r"Counterexample:\s*\n([\s\S]+?)(?:\n\n|\Z)",
            combined,
        )

        if violation_found:
            ce_text = ce_match.group(1)[:500] if ce_match else "(no counterexample text)"
            return FormalOutcome(
                proved=False,
                counterexample=True,
                harness_path=harness_path,
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
                duration_s=duration,
                verifier=self.verifier,
                reason=f"SMTChecker found assertion violation: {ce_text[:120]}",
                metadata={"counterexample": ce_text},
            )

        # SMTChecker prints info messages on successful proofs
        proved_signal = (
            "CHC: 0 verification conditions remained" in combined
            or "CHC: All " in combined and "verified" in combined
        )
        if proved_signal:
            return FormalOutcome(
                proved=True,
                counterexample=False,
                harness_path=harness_path,
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
                duration_s=duration,
                verifier=self.verifier,
                reason="SMTChecker proved all assertions",
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
            reason="SMTChecker inconclusive (likely timeout, unsupported feature, or compile error)",
        )
