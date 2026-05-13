"""Layer-2 PoC adapter for Solidity / EVM source repos.

Test framework: Foundry's ``forge test``. The "fired" signal is a
failing test (Foundry exits non-zero when any test fails). PoC tests
typically use one of:

  * ``vm.expectRevert(...)``  — expect a specific revert
  * Direct ``assertEq`` / ``assertTrue`` / ``assertGt`` — fail on bad state
  * ``invariant`` test that breaks under a specific input

Foundry catches reentrancy, access-control bypass, oracle manipulation,
share inflation, signature replay, governance issues — every bug class
in our ``osec_solidity_class.yaml`` library has a Foundry-expressible
witness.

Output mode: ``--json`` so we can parse the structured pass/fail/
revert reason from stdout instead of brittle string-matching.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from audit_pipeline.poc_adapters.base import LanguagePocAdapter, PocOutcome


# Markers that mean the test didn't actually exercise the bug.
_PSEUDO_PASS_MARKERS = (
    "TODO",
    "FIXME",
    "CANNOT_TEST",
    "// placeholder",
)


class SolidityAdapter(LanguagePocAdapter):
    """Solidity PoC adapter (Foundry `forge test`)."""

    language = "solidity"
    test_file_extension = ".sol"
    framework = "forge"

    def build_author_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        hyp_id = hyp.get("id", "unknown")
        claim = hyp.get("claim", "(no claim)")
        target_file = hyp.get("target_file", "")
        engine_function = hyp.get("engine_function", "")
        relevant = hyp.get("relevant_instructions") or ""

        return f"""You are authoring a Layer-2 Proof-of-Concept Solidity test for the Jelleo audit engine.

Your test will be compiled and run with:

  forge test --match-test {{test_function}} --json

The test FIRES (= bug confirmed) when ANY of these happen:
  * An explicit `assertEq` / `assertTrue` / `assertGt` / `assertLt` fails
  * `vm.expectRevert(...)` does NOT see the expected revert
  * The test crashes with an unexpected revert (forge marks as failed)
  * `invariant` breaks under fuzz inputs

The test PASSES (= bug NOT reachable from your witness state) when
the test function runs to completion with all assertions satisfied.

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Target file: {target_file}
Engine function: {engine_function}
Relevant instructions: {relevant}

# Grounded source

{source_context}

# Repo layout

The target repo is at: {target_repo_root}
Foundry remappings are in {target_repo_root}/foundry.toml
Source files live in {target_repo_root}/src/

# Your task

Write a single self-contained Solidity test file `test_<finding_name>.t.sol`
that:

1. Uses `pragma solidity ^0.8.20;` and imports `forge-std/Test.sol`.
2. Imports the contracts under test from `src/`.
3. Defines a contract that inherits from `Test`.
4. In `setUp()`, deploys the contracts under test with witness-state-
   relevant configuration (specific initial balances, fee rates,
   oracle prices, etc).
5. Defines a `test_<finding_name>` function that:
     (a) Sets up the EXACT preconditions from the hypothesis (specific
         attacker / victim / amounts / timestamps).
     (b) Calls the function under test (or sequence of calls that
         compose the bug).
     (c) Asserts an invariant that SHOULD hold after a normal
         execution but won't hold given the bug. Examples:
           * `assertEq(token.balanceOf(victim), expected);` where
             expected differs from actual under the bug.
           * `assertEq(vault.totalShares(), sumOfBalances);` —
             conservation check.
           * `vm.expectRevert(); vault.withdraw(stolenAmount);` — expect
             the protocol to reject; failure to revert = bug.
6. Uses `vm.prank(attacker)` / `vm.warp(timestamp)` / `vm.deal(addr, n)`
   as needed.

# Important notes

* Contract names in the repo may be obfuscated (e.g. variable names
  like `_v_2a84a346`). DO NOT use those names in your test — use the
  contract names + function signatures as imported.
* Do NOT add `vm.assume(...)` constraints that effectively neuter the
  test. The witness state should be reachable from a permissionless
  caller.
* Do NOT skip the test with `vm.skip(true)` or similar. If the bug
  isn't reachable, just write a normal test that asserts the OK
  invariant and let it pass (PoC won't fire).

# Output format

Output ONLY a single ```solidity ... ``` (or ```sol ... ```) fenced
code block containing the complete test contract. Do not output any
prose, explanation, or markdown outside the fenced block.

If you can't write a real PoC (e.g. the hypothesis is wrong, the bug
isn't reachable, or you don't have enough information), output:

  // CANNOT_TEST: <one-line reason>
  pragma solidity ^0.8.20;
  import "forge-std/Test.sol";
  contract NoOpTest is Test {{
      function test_no_op() public pure {{ }}
  }}

The `CANNOT_TEST:` marker is recognized by the post-cycle gate as a
non-fire — it doesn't count as a passed test. Don't use it lightly.
"""

    def parse_test_body(self, llm_response: str) -> str:
        # Primary: solidity or sol fenced block
        m = re.search(r"```(?:solidity|sol|Solidity)\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            return m.group(1).strip() + "\n"
        # Secondary: any fenced block that looks like Solidity
        m = re.search(r"```\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            body = m.group(1).strip()
            if "pragma solidity" in body or "contract " in body:
                return body + "\n"
        # Tertiary: bare body
        stripped = llm_response.strip()
        if "pragma solidity" in stripped or "contract " in stripped:
            return stripped + "\n"
        raise ValueError(
            "Could not extract a Solidity source block from the LLM response. "
            "Expected ```solidity ... ``` or ```sol ... ``` fenced code block."
        )

    def write_test_file(
        self,
        workspace: Path,
        test_name: str,
        body: str,
    ) -> Path:
        # Foundry's standard test layout: <repo_root>/test/*.t.sol
        # But for OSec eval we keep tests under the workspace's
        # tests/solidity/ dir, then copy/symlink into the target repo's
        # test/ dir at run time. Simplest: write directly into the
        # target repo's test/ dir (target_repo_root is provided to run_test).
        out_dir = workspace / "tests" / "solidity"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"test_{test_name}.t{self.test_file_extension}"
        out_path.write_text(body, encoding="utf-8")
        return out_path

    def run_test(
        self,
        workspace: Path,
        test_name: str,
        target_repo_root: Path,
        timeout_s: int = 300,
    ) -> PocOutcome:
        test_path = (
            workspace / "tests" / "solidity"
            / f"test_{test_name}.t{self.test_file_extension}"
        )
        if not test_path.is_file():
            raise FileNotFoundError(
                f"PoC test file not found at {test_path}. Did write_test_file run?"
            )

        body = test_path.read_text(encoding="utf-8", errors="replace")
        for marker in _PSEUDO_PASS_MARKERS:
            if marker in body:
                return PocOutcome(
                    fired=False,
                    test_path=test_path,
                    stdout="",
                    stderr=f"pseudo-pass marker {marker!r} present",
                    returncode=-1,
                    duration_s=0.0,
                    framework=self.framework,
                    reason=f"pseudo-pass: contains {marker!r}",
                    metadata={"pseudo_pass": True, "marker": marker},
                )

        # Foundry expects tests in <repo>/test/. Copy the test there for
        # the run; remove after. Use a unique filename to avoid collisions
        # with other concurrent runs.
        repo_test_dir = target_repo_root / "test"
        repo_test_dir.mkdir(parents=True, exist_ok=True)
        deployed_test = repo_test_dir / f"jelleo_l2_{test_name}.t.sol"
        deployed_test.write_text(body, encoding="utf-8")

        t0 = time.time()
        try:
            run_proc = subprocess.run(
                [
                    "forge", "test",
                    "--match-path", str(deployed_test.relative_to(target_repo_root)),
                    "--json",
                    "-vvv",
                ],
                cwd=str(target_repo_root),
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except FileNotFoundError:
            deployed_test.unlink(missing_ok=True)
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout="",
                stderr="forge not installed — install via https://book.getfoundry.sh/getting-started/installation",
                returncode=-3,
                duration_s=time.time() - t0,
                framework=self.framework,
                reason="toolchain missing: forge",
                metadata={"infra_error": True},
            )
        except subprocess.TimeoutExpired:
            deployed_test.unlink(missing_ok=True)
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout="",
                stderr="forge test timed out",
                returncode=-5,
                duration_s=time.time() - t0,
                framework=self.framework,
                reason="forge test timeout",
            )
        finally:
            # Always clean up the deployed test file from the target repo
            # (even on success) so we don't pollute the repo for future
            # runs and so concurrent runs don't see each other's tests.
            deployed_test.unlink(missing_ok=True)

        duration = time.time() - t0
        stdout = run_proc.stdout[:8000]
        stderr = run_proc.stderr[:4000]

        # Parse forge's JSON output to know precisely what happened.
        # Forge --json emits one JSON object per file under test;
        # each contains test_results: { test_name: { success: bool, reason: str } }
        fired_tests: list[str] = []
        failure_reason: str | None = None
        try:
            for line in run_proc.stdout.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Forge JSON shape: {"<src/...>": {"test_results": {...}}}
                for _file, file_data in obj.items():
                    if not isinstance(file_data, dict):
                        continue
                    results = file_data.get("test_results") or {}
                    for tname, tdata in results.items():
                        if not isinstance(tdata, dict):
                            continue
                        if tdata.get("success") is False:
                            fired_tests.append(tname)
                            failure_reason = (
                                tdata.get("reason")
                                or tdata.get("decoded_logs", [""])[0]
                                or "test failed"
                            )
        except (TypeError, KeyError):
            pass

        if fired_tests:
            return PocOutcome(
                fired=True,
                test_path=test_path,
                stdout=stdout,
                stderr=stderr,
                returncode=run_proc.returncode,
                duration_s=duration,
                framework=self.framework,
                reason=f"forge test failed: {failure_reason[:200] if failure_reason else 'no reason'}",
                metadata={
                    "fired_tests": fired_tests,
                    "failure_reason": (failure_reason or "")[:500],
                },
            )

        # Forge exit code: 0 = all pass, 1 = test failures, 2+ = infra
        # If returncode != 0 but no JSON failure parsed, treat as build/infra
        if run_proc.returncode != 0:
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout=stdout,
                stderr=stderr,
                returncode=run_proc.returncode,
                duration_s=duration,
                framework=self.framework,
                reason=f"forge exited {run_proc.returncode} with no parseable test failure (likely compile error)",
                metadata={"phase": "compile"},
            )

        return PocOutcome(
            fired=False,
            test_path=test_path,
            stdout=stdout,
            stderr=stderr,
            returncode=0,
            duration_s=duration,
            framework=self.framework,
            reason="all tests passed — bug not reachable from witness state",
        )
