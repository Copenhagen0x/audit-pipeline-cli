"""L4 runtime adapter for Solidity — Foundry forge fuzz / forge invariant.

Foundry's built-in fuzzer mutates function arguments (via property-
based randomization) and runs each test function against many inputs.
``forge invariant`` runs a stateful fuzzer — it constructs random call
sequences and verifies the invariant holds across all states reached.

Test patterns:

  // Property-based fuzz: forge runs N random (a, b) pairs
  function testFuzz_addCommutative(uint256 a, uint256 b) public {
      assertEq(a + b, b + a);
  }

  // Stateful invariant: forge generates random call sequences
  function invariant_totalSupplyIntegrity() public {
      assertEq(token.totalSupply(), token.sumOfBalances());
  }

A failing fuzz test means the fuzzer found inputs that broke the
invariant — same constructive bug evidence as CBMC counterexamples.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from audit_pipeline.runtime_adapters.base import LanguageRuntimeAdapter, RuntimeOutcome


class SolidityRuntimeAdapter(LanguageRuntimeAdapter):
    language = "solidity"
    harness_file_extension = ".sol"
    fuzzer = "forge-fuzz"

    def build_harness_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        hyp_id = hyp.get("id", "unknown")
        claim = hyp.get("claim", "(no claim)")
        engine_function = hyp.get("engine_function", "")

        return f"""You are authoring a Foundry fuzz harness for the Jelleo audit engine.

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Function under test: {engine_function}

# Grounded source

{source_context}

# Run command

  forge test --match-path test/fuzz_<name>.t.sol --fuzz-runs 2000 --json

A failing fuzz/invariant test = bug confirmed with concrete inputs.

# Harness patterns

```solidity
// Property-based fuzz
function testFuzz_invariantHolds(uint256 amount, address user) public {{
    vm.assume(amount > 0 && amount < 1e30);
    vm.assume(user != address(0));
    // Set up state...
    // Call function under test
    // Assert invariant
}}

// Stateful invariant (forge generates random call sequences)
function invariant_balanceSum() public {{
    uint256 sum;
    for (uint256 i = 0; i < holders.length; i++) {{
        sum += token.balanceOf(holders[i]);
    }}
    assertEq(token.totalSupply(), sum);
}}
```

# Your task

Write `fuzz_<finding_name>.t.sol` that:
1. Inherits from forge-std `Test`.
2. setUp() deploys the contract under test in a realistic state.
3. testFuzz_* functions exercise the bug hypothesis with vm.assume()
   to bound inputs.
4. Asserts the invariant — failure = constructive bug.

# Output format

Output ONLY a single ```solidity ... ``` fenced code block.
If unable: `// CANNOT_FUZZ: <reason>` stub.
"""

    def parse_harness_body(self, llm_response: str) -> str:
        m = re.search(r"```(?:solidity|sol)\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            return m.group(1).strip() + "\n"
        m = re.search(r"```\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            body = m.group(1).strip()
            if "contract " in body or "testFuzz_" in body or "invariant_" in body:
                return body + "\n"
        raise ValueError("Could not extract a Solidity fuzz harness from the LLM response.")

    def write_harness_file(
        self,
        workspace: Path,
        harness_name: str,
        body: str,
    ) -> Path:
        out_dir = workspace / "fuzz" / "solidity"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"fuzz_{harness_name}.t{self.harness_file_extension}"
        out_path.write_text(body, encoding="utf-8")
        return out_path

    def run_fuzzer(
        self,
        workspace: Path,
        harness_name: str,
        target_repo_root: Path,
        time_budget_s: int = 180,
    ) -> RuntimeOutcome:
        harness_path = (
            workspace / "fuzz" / "solidity"
            / f"fuzz_{harness_name}.t{self.harness_file_extension}"
        )
        if not harness_path.is_file():
            raise FileNotFoundError(f"Forge fuzz harness not found at {harness_path}")

        body = harness_path.read_text(encoding="utf-8", errors="replace")
        if "CANNOT_FUZZ" in body:
            return RuntimeOutcome(
                crash_found=False, ran_clean=False, harness_path=harness_path,
                stdout="", stderr="CANNOT_FUZZ stub", returncode=-1,
                duration_s=0.0, fuzzer=self.fuzzer, reason="harness stub",
            )

        # Deploy into the target repo's test/ dir
        deployed = target_repo_root / "test" / f"jelleo_l4_fuzz_{harness_name}.t.sol"
        deployed.parent.mkdir(parents=True, exist_ok=True)
        deployed.write_text(body, encoding="utf-8")

        # Number of fuzz runs scales with time budget; ~2000 runs/sec
        # is realistic for a small harness.
        n_runs = max(500, time_budget_s * 200)
        cmd = [
            "forge", "test",
            "--match-path", str(deployed.relative_to(target_repo_root)),
            "--fuzz-runs", str(n_runs),
            "--json", "-vvv",
        ]
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, cwd=str(target_repo_root),
                capture_output=True, text=True,
                timeout=time_budget_s + 60,
            )
        except FileNotFoundError:
            deployed.unlink(missing_ok=True)
            return RuntimeOutcome(
                crash_found=False, ran_clean=False, harness_path=harness_path,
                stdout="", stderr="forge not installed",
                returncode=-3, duration_s=time.time() - t0,
                fuzzer=self.fuzzer, reason="toolchain missing: forge",
                metadata={"infra_error": True},
            )
        except subprocess.TimeoutExpired:
            deployed.unlink(missing_ok=True)
            return RuntimeOutcome(
                crash_found=False, ran_clean=False, harness_path=harness_path,
                stdout="", stderr="forge fuzz timeout",
                returncode=-5, duration_s=time.time() - t0,
                fuzzer=self.fuzzer, reason="forge timeout",
            )
        finally:
            deployed.unlink(missing_ok=True)

        duration = time.time() - t0
        stdout = proc.stdout[:8000]
        stderr = proc.stderr[:4000]

        # Parse forge JSON output — failed fuzz tests = crashes
        failed: list[str] = []
        counter_inputs: list[dict[str, Any]] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            for _file, fdata in obj.items():
                if not isinstance(fdata, dict):
                    continue
                results = fdata.get("test_results") or {}
                for tname, tdata in results.items():
                    if isinstance(tdata, dict) and tdata.get("success") is False:
                        failed.append(tname)
                        ce = tdata.get("counterexample") or tdata.get("decoded_logs")
                        if ce:
                            counter_inputs.append({"test": tname, "ce": str(ce)[:500]})

        if failed:
            return RuntimeOutcome(
                crash_found=True, ran_clean=False, harness_path=harness_path,
                stdout=stdout, stderr=stderr,
                returncode=proc.returncode, duration_s=duration,
                fuzzer=self.fuzzer,
                reason=f"forge fuzz found {len(failed)} failing test(s)",
                witness_inputs=counter_inputs,
                metadata={"failed_tests": failed[:5]},
            )

        if proc.returncode != 0 and not failed:
            return RuntimeOutcome(
                crash_found=False, ran_clean=False, harness_path=harness_path,
                stdout=stdout, stderr=stderr,
                returncode=proc.returncode, duration_s=duration,
                fuzzer=self.fuzzer,
                reason="forge exit != 0 without parseable failure (likely compile error)",
                metadata={"phase": "compile"},
            )

        return RuntimeOutcome(
            crash_found=False, ran_clean=True, harness_path=harness_path,
            stdout=stdout, stderr=stderr,
            returncode=0, duration_s=duration,
            fuzzer=self.fuzzer,
            reason=f"forge fuzz ran {n_runs} cases without failures",
        )
