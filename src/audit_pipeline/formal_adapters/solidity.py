"""L3 formal-verification adapter for Solidity — Halmos symbolic executor.

Halmos is a symbolic execution tool designed for Foundry tests. It
exhaustively explores function inputs using SMT-backed reasoning,
finding inputs that violate `assert(...)` statements or proving none
exist within bounded loop unrolling.

Compared to solc's built-in SMTChecker (which we used through
2026-05-17), Halmos:
  * Handles multi-contract harnesses (mock token + vault + attacker)
    where SMTChecker times out.
  * Reads Foundry test conventions natively (`check_*` functions,
    `vm.assume`, `vm.prank` cheatcodes, `forge-std/Test.sol`).
  * Produces structured JSON output we can parse without regex.

Operator switched on 2026-05-17 after SMTChecker returned
"indeterminate" for all 12 solidity-small harnesses (CHC solver
couldn't converge on protocol-level state spaces).

Invocation:
    halmos --root <repo> --match-test '^check_' \\
        --json-output <out.json> \\
        --solver-timeout-assertion 60000

A failing `check_*` function = bug constructively proven
(counterexample = True). A passing `check_*` function = invariant
holds within the bounded exploration (proved = True).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from audit_pipeline.formal_adapters.base import FormalOutcome, LanguageFormalAdapter


def _detect_foundry_test_dir(repo_root: Path) -> Path:
    """Read foundry.toml `test = "..."` key. Defaults to test/.

    Halmos auto-discovers test contracts in this dir.
    """
    manifest = repo_root / "foundry.toml"
    if not manifest.is_file():
        return repo_root / "test"
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return repo_root / "test"
    m = re.search(r"^\s*test\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
    if m:
        return repo_root / m.group(1)
    return repo_root / "test"


class SolidityFormalAdapter(LanguageFormalAdapter):
    """Solidity formal-verification adapter (Halmos symbolic executor)."""

    language = "solidity"
    harness_file_extension = ".sol"
    verifier = "halmos"

    def build_harness_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        hyp_id = hyp.get("id", "unknown")
        claim = hyp.get("claim", "(no claim)")
        engine_function = hyp.get("engine_function", "")

        return f"""You are authoring a Halmos symbolic-execution harness for the Jelleo audit engine.

Halmos is a Foundry-compatible symbolic executor that:
  * Exhaustively explores function inputs via SMT solving.
  * Discovers test contracts by class name + functions starting with `check_`.
  * Reads `vm.assume(...)`, `vm.prank(...)`, `vm.warp(...)` cheatcodes
    natively (use `import "forge-std/Test.sol"; contract X is Test`).
  * Reports a `Counterexample` when it finds inputs that violate
    `assert(...)`, and `[PASS]` when no such inputs exist within
    bounded loop unrolling (default 2 iterations).

Invocation (already wired by the engine):

  halmos --root <repo> --match-test '^check_' \\
      --json-output <out.json>

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Function under test: {engine_function}

# Grounded source

{source_context}

# Repo conventions

Imports use `@src/` remapping (per foundry.toml). Example:
    `import "@src/ContractA.sol";`
    `import "@src/vendor/openzeppelin/token/ERC20/IERC20.sol";`
    `import "@src/interfaces/ExternalInterfaces.sol";` (IOracle, IBridgeAdapter)

# Your task

Write a Halmos symbolic harness named `harness_<finding_name>.sol` that:

1. Uses `pragma solidity ^0.8.20;` + `import "forge-std/Test.sol";`
2. Defines a contract that inherits from `Test`.
3. Has at least one function with the name prefix `check_<short_label>(...)`
   that takes the symbolic inputs you want Halmos to enumerate.
4. Uses `vm.assume(...)` for preconditions (bounds, non-zero, etc).
5. Calls the function under test (or replicates its logic in a minimal
   in-harness form — preferred when the real contract has too many
   external dependencies for halmos).
6. Asserts the invariant via `assert(invariant_holds)`. Halmos finds
   a counterexample if the assertion is violated for any admitted input.

# Patterns

```solidity
// Pattern A: minimal in-harness model (preferred — exhaustive enumeration)
contract Harness is Test {{
    function check_dustLoss(uint256 total, uint256 n) public pure {{
        vm.assume(n > 0 && n <= 16);
        vm.assume(total > 0 && total <= type(uint128).max);
        uint256 perVoter = total / n;
        uint256 paidOut = perVoter * n;
        // Invariant: no dust. Halmos finds total=1, n=2 → paidOut=0 != total=1.
        assert(paidOut == total);
    }}
}}

// Pattern B: call into real contract (only if minimal model isn't possible)
contract Harness is Test {{
    ContractA vault;
    function setUp() public {{
        vault = new ContractA(...);
    }}
    function check_authBypass(address attacker, uint256 newOracle) public {{
        vm.assume(attacker != address(0));
        vm.assume(attacker != owner);  // bug: any non-owner should fail
        vm.prank(attacker);
        vault.setOracle(IOracle(address(uint160(newOracle))));
        // Bug: setOracle has NO access control. assertion fails → CE.
        assert(false);  // if we reach here, the call didn't revert → bug
    }}
}}
```

# Halmos vs SMTChecker — key differences

* Halmos uses Foundry's test format. SMTChecker was raw solc.
* Use `check_*` prefix (not `test_*` or `prove_*`).
* Loops are bounded by `--loop 2` default. Use `vm.assume(i < 8)` if more iterations needed.
* External calls to symbolic addresses return symbolic data — usually fine.
* Halmos handles `keccak256(abi.encodePacked(...))` better than SMTChecker.

# Minimal-harness rule (CRITICAL)

PREFER the minimal in-harness model (Pattern A) over Pattern B whenever
the bug can be expressed as pure arithmetic / authorization / signature
math. Halmos converges on small state spaces; full-contract harnesses
with mock tokens etc may time out.

# Output format

Output ONLY a single ```solidity ... ``` fenced code block.

If unable: `// CANNOT_VERIFY: <one-line reason>` + a no-op contract:

  // CANNOT_VERIFY: <reason>
  pragma solidity ^0.8.20;
  import "forge-std/Test.sol";
  contract NoOpHarness is Test {{
      function check_noop() public pure {{ }}
  }}
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
                f"Halmos harness not found at {harness_path}."
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

        # Deploy harness into the foundry project's test dir so halmos
        # auto-discovers it. The repo's test dir is configured in
        # foundry.toml; honor that path so this works against OSec
        # repos (tests/) and Foundry default (test/) alike.
        repo_test_dir = _detect_foundry_test_dir(target_repo_root)
        repo_test_dir.mkdir(parents=True, exist_ok=True)
        deployed = repo_test_dir / f"jelleo_l3_{harness_name}.t.sol"
        deployed.write_text(body, encoding="utf-8")

        # Halmos auto-discovers contract names. We pre-build the project
        # with forge so halmos doesn't re-compile every time.
        json_out = harness_path.with_suffix(".halmos.json")
        if json_out.exists():
            json_out.unlink()

        cmd = [
            "halmos",
            "--root", str(target_repo_root),
            "--match-test", "^check_",
            "--match-contract", f"^.*$",  # match any contract — we filter by test func name
            "--json-output", str(json_out),
            "--solver-timeout-assertion", "60000",  # 60s per assertion
            "--solver-timeout-branching", "1000",
            "--early-exit",  # stop at first counterexample per function
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
                stderr="halmos not installed",
                returncode=-3,
                duration_s=time.time() - t0,
                verifier=self.verifier,
                reason="toolchain missing: halmos",
                metadata={"infra_error": True},
            )
        except subprocess.TimeoutExpired as e:
            deployed.unlink(missing_ok=True)
            partial_out = ""
            partial_err = ""
            try:
                if e.stdout:
                    partial_out = e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, (bytes, bytearray)) else str(e.stdout)
                if e.stderr:
                    partial_err = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, (bytes, bytearray)) else str(e.stderr)
            except Exception:  # noqa: BLE001
                pass
            try:
                log_path = harness_path.with_suffix(".halmos.log")
                log_path.write_text(
                    f"=== TIMED OUT AFTER {timeout_s}s ===\n"
                    f"=== STDOUT (partial, {len(partial_out)} bytes) ===\n{partial_out}\n"
                    f"=== STDERR (partial, {len(partial_err)} bytes) ===\n{partial_err}\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout=partial_out[:8000],
                stderr=(partial_err or "halmos timed out")[:8000],
                returncode=-5,
                duration_s=time.time() - t0,
                verifier=self.verifier,
                reason=f"halmos timeout after {timeout_s}s (partial output saved to .halmos.log)",
            )
        finally:
            deployed.unlink(missing_ok=True)

        duration = time.time() - t0
        stdout = proc.stdout
        stderr = proc.stderr

        # Persist full output for post-cycle inspection.
        try:
            log_path = harness_path.with_suffix(".halmos.log")
            log_path.write_text(
                f"=== STDOUT ({len(stdout)} bytes) ===\n{stdout}\n"
                f"=== STDERR ({len(stderr)} bytes) ===\n{stderr}\n"
                f"=== RETURNCODE: {proc.returncode} ===\n",
                encoding="utf-8",
            )
        except OSError:
            pass

        # Parse halmos JSON output. Schema (typical):
        # {"test_results": [{"name": "...", "passed": true/false,
        #                    "counter_example": ..., "error": "..."}],
        #  ...}
        ce_found = False
        proved_count = 0
        failed_count = 0
        ce_text: str | None = None
        first_failed_name: str | None = None
        if json_out.exists():
            try:
                data = json.loads(json_out.read_text(encoding="utf-8", errors="replace"))
                # data may be a list of contract results or have a "test_results" key
                contracts = data if isinstance(data, list) else [data]
                for contract_entry in contracts:
                    if not isinstance(contract_entry, dict):
                        continue
                    results = (
                        contract_entry.get("test_results")
                        or contract_entry.get("tests")
                        or contract_entry.get("results")
                        or []
                    )
                    if not isinstance(results, list):
                        continue
                    for t in results:
                        if not isinstance(t, dict):
                            continue
                        name = t.get("name") or t.get("test_name") or "?"
                        # Halmos uses "exitcode" or "passed" depending on version
                        passed_field = t.get("passed")
                        exitcode = t.get("exitcode")
                        if passed_field is True or exitcode == 0:
                            proved_count += 1
                        elif passed_field is False or (exitcode is not None and exitcode != 0):
                            failed_count += 1
                            ce_found = True
                            if first_failed_name is None:
                                first_failed_name = name
                            ce = t.get("counter_example") or t.get("counterexample") or t.get("model")
                            if ce and ce_text is None:
                                ce_text = str(ce)[:500]
            except (OSError, ValueError, json.JSONDecodeError):
                pass

        # Fallback: parse stdout text if JSON missing
        if not ce_found and proved_count == 0:
            combined = stdout + "\n" + stderr
            if "Counterexample" in combined or "[FAIL]" in combined or "[ERROR]" in combined:
                ce_found = True
                m = re.search(r"Counterexample[:\s]*\n?([\s\S]+?)(?:\n\n|\Z)", combined)
                if m:
                    ce_text = m.group(1).strip()[:500]
            elif "[PASS]" in combined or "passed" in combined.lower():
                proved_count = 1

        if ce_found:
            return FormalOutcome(
                proved=False,
                counterexample=True,
                harness_path=harness_path,
                stdout=stdout[:8000],
                stderr=stderr[:8000],
                returncode=proc.returncode,
                duration_s=duration,
                verifier=self.verifier,
                reason=f"Halmos found counterexample{(': ' + (ce_text or first_failed_name or '?')[:120]) if (ce_text or first_failed_name) else ''}",
                metadata={"counterexample": ce_text, "failed_count": failed_count, "first_failed": first_failed_name},
            )

        if proved_count > 0:
            return FormalOutcome(
                proved=True,
                counterexample=False,
                harness_path=harness_path,
                stdout=stdout[:8000],
                stderr=stderr[:8000],
                returncode=proc.returncode,
                duration_s=duration,
                verifier=self.verifier,
                reason=f"Halmos proved invariant (no counterexample within bounded exploration; {proved_count} test(s) passed)",
            )

        # Compile error or other infra issue
        if "Error" in stderr or "error" in stderr.lower():
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout=stdout[:8000],
                stderr=stderr[:8000],
                returncode=proc.returncode,
                duration_s=duration,
                verifier=self.verifier,
                reason="halmos compile or runtime error (see .halmos.log)",
                metadata={"compile_error": True},
            )

        return FormalOutcome(
            proved=False,
            counterexample=False,
            harness_path=harness_path,
            stdout=stdout[:8000],
            stderr=stderr[:8000],
            returncode=proc.returncode,
            duration_s=duration,
            verifier=self.verifier,
            reason="halmos inconclusive (no test results parsed; see .halmos.log)",
        )
