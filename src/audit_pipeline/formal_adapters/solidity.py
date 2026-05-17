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
2. Uses `@src/...` remapped imports if you need to reference the
   contract under test — but PREFER a MINIMAL inlined copy of just
   the function/struct/storage under test. SMTChecker times out on
   full-contract harnesses with vendor chains. Inline the smallest
   self-contained version that exhibits the bug.
3. Defines a `contract Harness` that:
     (a) Has state variables matching the protocol's relevant state.
     (b) Has functions exercising the bug-claim path. Each function:
         - Uses `require(precondition)` to bound the input space.
         - Calls the function under test.
         - Asserts the invariant via `assert(invariant_holds)`.
4. Express the invariant as the OPPOSITE of the bug — if SMTChecker
   finds a counterexample, the bug is real.
5. KEEP THE HARNESS SMALL — under ~80 lines. CHC scales poorly with
   state space size. No vendor imports. No unrelated functions.
   Translate the relevant slice of the bug into pure assertions.

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

        # Deploy harness into a SCRATCH dir, not the target repo's src/.
        # Two reasons:
        #   1. The src/ dir is part of the audited surface — anything we
        #      write there would be picked up by every subsequent
        #      forge build and recon pass, polluting findings.
        #   2. solc resolves imports against --allow-paths, so we can
        #      reference src/<Real.sol> from a scratch directory just
        #      as easily.
        scratch_dir = workspace / "formal" / "solidity" / "scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        deployed = scratch_dir / f"jelleo_l3_{harness_name}.sol"
        deployed.write_text(body, encoding="utf-8")

        # SMTChecker via raw solc. forge doesn't expose --model-checker-*
        # flags directly, so we call solc and let it resolve imports via
        # --allow-paths + remappings. The timeout is in MILLISECONDS,
        # capped to fit within our wall-clock budget minus a safety margin.
        smt_timeout_ms = max(5_000, min(timeout_s * 1000 - 5_000, 60_000))

        # Read foundry.toml to pick up the project's remappings (e.g.
        # `@src/=src/`). The LLM harness imports use these aliases, but
        # raw solc doesn't read foundry.toml — we have to pass them as
        # positional `prefix=path` args before the source file.
        remappings: list[str] = []
        foundry_toml = target_repo_root / "foundry.toml"
        if foundry_toml.is_file():
            try:
                ft_text = foundry_toml.read_text(encoding="utf-8", errors="replace")
            except OSError:
                ft_text = ""
            # Match `remappings = ["@src/=src/", ...]` (list of strings)
            m_block = re.search(r"remappings\s*=\s*\[([\s\S]*?)\]", ft_text)
            if m_block:
                for entry in re.findall(r"['\"]([^'\"]+)['\"]", m_block.group(1)):
                    if "=" in entry and not entry.startswith("forge-std"):
                        prefix, _, rhs = entry.partition("=")
                        # Resolve rhs relative to repo root if relative
                        rhs_path = (target_repo_root / rhs).resolve() if not rhs.startswith("/") else Path(rhs)
                        remappings.append(f"{prefix}={rhs_path}")

        cmd = ["solc"]
        cmd.extend(remappings)
        cmd.extend([
            "--model-checker-engine", "chc",
            "--model-checker-targets", "all",
            "--model-checker-timeout", str(smt_timeout_ms),
            "--model-checker-show-unproved",
            "--allow-paths", f"{target_repo_root},{scratch_dir}",
            "--base-path", str(target_repo_root),
            str(deployed),
        ])

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

        # SMTChecker prints info messages on successful proofs. The
        # operator-precedence around the second clause was a bug —
        # `A or B and C` evaluates as `A or (B and C)`, so the original
        # signal "CHC: All X verified" only fired when BOTH literals
        # appeared in the output. We make the AND explicit with parens
        # and ALSO check for the newer "all checks were verified" form
        # introduced in solc 0.8.20+.
        proved_signal = (
            "CHC: 0 verification conditions remained" in combined
            or ("CHC: All " in combined and "verified" in combined)
            or "CHC: All assertions in this contract are proved" in combined
            or "all checks were verified" in combined.lower()
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
