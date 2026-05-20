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

        # Resolve target_repo_root to an absolute path. Hunt.py passes
        # the workspace.json `engine.local` string which is typically
        # a relative path like "../../../../ottersec-eval/repos/...".
        # Halmos needs an absolute --root and uses os.path.join with
        # its own cwd-resolving logic that doesn't normalize `..`
        # segments correctly — so we resolve here.
        target_repo_root = Path(target_repo_root).resolve()

        # Deploy harness into the foundry project's test dir so halmos
        # auto-discovers it. The repo's test dir is configured in
        # foundry.toml; honor that path so this works against OSec
        # repos (tests/) and Foundry default (test/) alike.
        repo_test_dir = _detect_foundry_test_dir(target_repo_root)
        repo_test_dir.mkdir(parents=True, exist_ok=True)

        # IMPORTANT: clean up sibling jelleo_l3_*.t.sol files from
        # prior hyp invocations. Halmos auto-discovers ALL tests in
        # the foundry project — leaving leftovers means halmos runs
        # the previous harness's check_* functions too, inflating
        # the JSON output with results for unrelated hyps. Caught
        # 2026-05-17 when SOLD10's run showed SOLD1's check_*
        # results piggybacking.
        for stale in repo_test_dir.glob("jelleo_l3_*.t.sol"):
            try:
                stale.unlink()
            except OSError:
                pass

        # Also wipe forge's build cache for L3 tests. Halmos discovers
        # contracts from the build output (out/<file>/<contract>.json)
        # NOT from .t.sol source — so even after we delete the source,
        # halmos still picks up cached compiled artifacts. Wipe the
        # entire out/<jelleo_l3_*.t.sol>/ subtree.
        build_out_dir = target_repo_root / "out"
        if build_out_dir.is_dir():
            import shutil
            for stale_cache in build_out_dir.glob("jelleo_l3_*.t.sol"):
                try:
                    if stale_cache.is_dir():
                        shutil.rmtree(stale_cache, ignore_errors=True)
                    else:
                        stale_cache.unlink()
                except OSError:
                    pass

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
            "--match-contract", "^.*$",  # match any contract — we filter by test func name
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

        # Parse halmos JSON output. Schema (from halmos 0.1.13):
        # {"exitcode": 0|1|2,
        #  "test_results": {"<file>:<contract>": [
        #      {"name": "check_xxx(...)",
        #       "exitcode": 0,   // 0 = proved, 1 = CE w/ model, 2 = timeout/inconclusive
        #       "num_models": N, // >0 = concrete CE produced
        #       "models": [...]} // CE witnesses (when num_models > 0)
        #  ]}}
        #
        # Verdict mapping:
        #   exitcode 0                  → proved
        #   exitcode != 0 AND models    → counterexample (concrete witness)
        #   exitcode != 0 AND !models   → inconclusive (timeout / unknown sat)
        ce_found = False
        proved_count = 0
        inconclusive_count = 0
        ce_text: str | None = None
        first_failed_name: str | None = None
        if json_out.exists():
            try:
                data = json.loads(json_out.read_text(encoding="utf-8", errors="replace"))
                # top-level may be dict with test_results, or list of contract entries
                if isinstance(data, dict) and "test_results" in data:
                    test_results = data["test_results"]
                    # halmos returns test_results as a DICT keyed by file:contract
                    if isinstance(test_results, dict):
                        all_tests = []
                        for _key, tests in test_results.items():
                            if isinstance(tests, list):
                                all_tests.extend(tests)
                    elif isinstance(test_results, list):
                        all_tests = test_results
                    else:
                        all_tests = []
                else:
                    all_tests = data if isinstance(data, list) else []

                for t in all_tests:
                    if not isinstance(t, dict):
                        continue
                    name = t.get("name") or t.get("test_name") or "?"
                    test_exitcode = t.get("exitcode")
                    num_models = t.get("num_models") or 0
                    models = t.get("models") or []

                    if test_exitcode == 0:
                        proved_count += 1
                    elif num_models > 0 or models:
                        # Concrete CE found
                        ce_found = True
                        if first_failed_name is None:
                            first_failed_name = name
                        if models and ce_text is None:
                            ce_text = str(models[0])[:500]
                    else:
                        # Failed without concrete witness — timeout or unknown sat
                        inconclusive_count += 1
                        if first_failed_name is None:
                            first_failed_name = name
            except (OSError, ValueError, json.JSONDecodeError):
                pass

        # Override: if stdout/stderr clearly shows [TIMEOUT] but no concrete
        # CE, force inconclusive verdict (not CE).
        combined_lower = (stdout + stderr).lower()
        if "[timeout]" in combined_lower and not ce_found and not proved_count:
            inconclusive_count = max(inconclusive_count, 1)

        # PRIMARY parser: halmos stdout text. JSON output is often
        # empty or missing — text is the reliable signal. Halmos
        # output format:
        #   "Counterexample: \n    p_x_uint256 = 0x... (123)\n..." → concrete CE
        #   "[FAIL] check_name(...) (paths: N, time: T)"           → bug found
        #   "[TIMEOUT] check_name(...)"                            → solver gave up
        #   "[PASS] check_name(...)"                               → invariant holds
        #   "Symbolic test result: X passed; Y failed; time: ..."  → summary
        # The ANSI color codes wrap [FAIL] / [TIMEOUT] / [PASS] so we
        # strip them before scanning.
        combined_text = stdout + "\n" + stderr
        # Strip ALL ANSI escape sequences — not just SGR (color) codes.
        # Halmos output also contains cursor-positioning and
        # screen-clear sequences in some terminals that would survive
        # the SGR-only regex and break downstream `[FAIL]` / `[PASS]`
        # marker matches.
        ansi_re = re.compile(r"\x1b\[[^a-zA-Z]*[a-zA-Z]")
        clean_text = ansi_re.sub("", combined_text)

        # Count [FAIL] (with concrete CE), [TIMEOUT], [PASS]
        fail_lines = re.findall(r"\[FAIL\]\s+(check_\w+)", clean_text)
        timeout_lines = re.findall(r"\[TIMEOUT\]\s+(check_\w+)", clean_text)
        pass_lines = re.findall(r"\[PASS\]\s+(check_\w+)", clean_text)

        # Filter to only our hyp's check_ functions. Halmos may have
        # discovered tests from other harnesses if the cleanup didn't
        # catch them — we only count results for tests defined in OUR
        # deployed harness body.
        our_check_names = set(re.findall(r"function\s+(check_\w+)\s*\(", body))
        if our_check_names:
            fail_lines = [n for n in fail_lines if n.split("(")[0] in our_check_names]
            timeout_lines = [n for n in timeout_lines if n.split("(")[0] in our_check_names]
            pass_lines = [n for n in pass_lines if n.split("(")[0] in our_check_names]

        # If we have text-based results (most reliable), prefer them
        # over the JSON parse above which is often missing.
        if fail_lines or timeout_lines or pass_lines:
            ce_found = len(fail_lines) > 0
            inconclusive_count = len(timeout_lines)
            proved_count = len(pass_lines)
            if first_failed_name is None and fail_lines:
                first_failed_name = fail_lines[0]
            # Extract the first counterexample text block
            if ce_found and ce_text is None:
                m = re.search(
                    r"Counterexample:\s*\n([\s\S]+?)(?=\n\[(?:FAIL|TIMEOUT|PASS)\]|\nSymbolic test result|\Z)",
                    clean_text,
                )
                if m:
                    ce_text = m.group(1).strip()[:600]

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
                metadata={"counterexample": ce_text, "failed_count": len(fail_lines) if 'fail_lines' in locals() else 0, "first_failed": first_failed_name},
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

        # Compile error or other infra issue — STRICT detection.
        # Previous check was `"error" in stderr.lower()` which matched
        # any line containing the substring "error" — including
        # `warning[block-timestamp]: ... validators` (no "error" there
        # but: "ParserError", "TypeError", harness-author warnings).
        # That false-positive caused runs that DID produce CE/PROVED
        # to be misclassified as "compile_error" → dashboard rendered
        # "⚠ spec-compile-error" for a hypothesis that was actually
        # proved. Operator caught the SOLD1 PROVED→spec-compile-error
        # flip 2026-05-17.
        #
        # We now require an anchored error token (line start, possibly
        # after a "Compiler run failed:" preamble) or a Solc-style
        # "Error (NNNN):" parser code. This still catches every real
        # compile fail without false-positives from warning text.
        _has_compile_error = bool(
            re.search(r"^(?:Compiler run failed|Error \(\d+\)|error:|ParserError|TypeError)", stderr, re.MULTILINE)
            or re.search(r"Error: Compilation failed", stderr)
            or "Build failed:" in stderr
        )
        if _has_compile_error:
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

        if inconclusive_count > 0:
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout=stdout[:8000],
                stderr=stderr[:8000],
                returncode=proc.returncode,
                duration_s=duration,
                verifier=self.verifier,
                reason=f"halmos timeout/inconclusive on {inconclusive_count} test(s) "
                       f"(no concrete counterexample within solver budget; see .halmos.log)",
                metadata={"timeout": True, "inconclusive_count": inconclusive_count},
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
