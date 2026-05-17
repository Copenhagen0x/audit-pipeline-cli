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
    "FIXME",
    "CANNOT_TEST",
    "// placeholder",
    # NOTE: we intentionally DO NOT match bare "TODO" — many real Foundry
    # tests have a `// TODO:` comment for follow-up work and are still
    # exercising the bug. We only block markers that signal "the author
    # gave up authoring this test." See Phase 1d audit finding C-2.
)


def _detect_foundry_test_dir(repo_root: Path) -> Path:
    """Parse foundry.toml and return the directory tests must live in.

    Foundry honors a configurable `test` key under [profile.default];
    OSec repos use `test/` (Foundry default) or `tests/`. We read the
    raw text rather than importing tomli to keep deps minimal — the
    pattern we look for is `test = "<dir>"` under [profile.default].
    """
    manifest = repo_root / "foundry.toml"
    if not manifest.is_file():
        return repo_root / "test"
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return repo_root / "test"
    # Match `test = "tests"` (with optional whitespace + single or double quotes)
    m = re.search(
        r"^\s*test\s*=\s*['\"]([^'\"]+)['\"]",
        text,
        re.MULTILINE,
    )
    if m:
        return repo_root / m.group(1)
    return repo_root / "test"


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
        debate_context: str | None = None,
    ) -> str:
        hyp_id = hyp.get("id", "unknown")
        claim = hyp.get("claim", "(no claim)")
        target_file = hyp.get("target_file", "")
        engine_function = hyp.get("engine_function", "")
        relevant = hyp.get("relevant_instructions") or ""

        # Optional Layer-1.5 challenger context. When present, includes
        # the exact attack chain that survived adversarial review.
        debate_section = ""
        if debate_context and debate_context.strip():
            debate_section = (
                "\n# Layer-1.5 challenger response (exact attack chain)\n\n"
                "The hypothesis already passed Layer-1.5 adversarial debate. "
                "The challenger's response below contains the precise attack "
                "scenario, value choices, and assertion failure that "
                "convinced the second reviewer. USE THIS — it tells you "
                "WHICH ASSERTION TO MAKE that will fail under the bug.\n\n"
                "```\n" + debate_context.strip()[:6000] + "\n```\n"
            )

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
{debate_section}
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

# Solidity syntax pitfalls — these break compilation, avoid them

* **Hex literals must be VALID HEX** (chars 0-9, a-f, A-F). Writing
  `address(0xADMIN)` or `address(0xA77ACK)` is a SYNTAX ERROR because
  letters like M/N/T/K aren't hex digits. For named test addresses use
  `makeAddr("admin")`, `vm.addr(1)`, or a numeric literal like
  `address(0xA11CE)` (valid hex). NEVER write hex digits that look
  like English words unless every character is in `[0-9a-fA-F]`.
* **Contract name MUST differ from its test function names.** Solidity
  treats a function with the same name as its containing contract as
  a constructor (legacy syntax) which is invalid in 0.8.x. Name the
  contract something like `SOLD1_VaultReentrancyTest`, then put the
  test function `test_vault_withdraw_reentrancy()` inside it.
* Imports use `@src/...` (per foundry.toml remappings), e.g.
  `import "@src/ContractA.sol";`. NOT `import "src/ContractA.sol";`.
* `forge-std/Test.sol` is the test base — `is Test` on your contract.
* **DO NOT re-declare interfaces already in scope through imports.**
  When you `import "@src/ContractC.sol";` you transitively pull in
  `IBridgeAdapter`, `IOracle`, `IERC20`. Writing a second
  `interface IBridgeAdapter {{ ... }}` causes "Identifier already
  declared" compile error. Use the imported one.
* **NO C / Rust-style void casts.** Writing `(void)(x);` or
  `(void)x;` is a SYNTAX ERROR — Solidity has no `void` type and no
  C-style cast syntax. To suppress an unused-variable warning, use
  the bare expression `x;` (Solidity tolerates this), or remove the
  variable entirely. Common LLM hallucination caught 2026-05-17 in
  SOLD1 harness — don't repeat it.

# Mock contract templates (COPY-PASTE these, edit only the hook)

If your test needs a mock ERC-20 (reentrancy hook, blocklist, USDT-
style guard) or a mock oracle / bridge, USE THESE TEMPLATES. They are
verified to compile against THIS repo's `IERC20` and `IOracle`.

**Imports the mocks need** (add to the top of your test file):

```solidity
import "@src/vendor/openzeppelin/token/ERC20/IERC20.sol";  // IERC20
import "@src/interfaces/ExternalInterfaces.sol";  // IOracle, IBridgeAdapter
```

THIS repo's `IERC20` has 7 functions (decimals, totalSupply, balanceOf,
allowance, transfer, transferFrom, approve) — implement ALL of them
on any mock that `is IERC20`, otherwise the "Contract should be marked
as abstract" compile error fires.

```solidity
// Full IERC20 mock — implements ALL 7 methods on THIS repo's IERC20
// (decimals, totalSupply, balanceOf, allowance, transfer, transferFrom,
// approve). Compiles non-abstract.
contract MockERC20 is IERC20 {{
    mapping(address => uint256) internal _bal;
    mapping(address => mapping(address => uint256)) internal _allow;
    uint256 internal _total;
    function decimals() external pure override returns (uint8) {{ return 18; }}
    function totalSupply() external view override returns (uint256) {{ return _total; }}
    function balanceOf(address a) external view override returns (uint256) {{ return _bal[a]; }}
    function allowance(address o, address s) external view override returns (uint256) {{ return _allow[o][s]; }}
    function mint(address to, uint256 amt) external {{ _bal[to] += amt; _total += amt; }}
    function approve(address s, uint256 amt) external virtual override returns (bool) {{ _allow[msg.sender][s] = amt; emit Approval(msg.sender, s, amt); return true; }}
    function transfer(address to, uint256 amt) external virtual override returns (bool) {{
        require(_bal[msg.sender] >= amt, "bal"); _bal[msg.sender] -= amt; _bal[to] += amt; emit Transfer(msg.sender, to, amt); return true;
    }}
    function transferFrom(address from, address to, uint256 amt) external virtual override returns (bool) {{
        require(_bal[from] >= amt, "bal"); require(_allow[from][msg.sender] >= amt, "allow");
        _bal[from] -= amt; _allow[from][msg.sender] -= amt; _bal[to] += amt; emit Transfer(from, to, amt); return true;
    }}
}}
```

For an ERC-777-style HOOK mock (reentrancy attacks), inherit MockERC20
and override `transfer` to call back into the target before completing:

```solidity
contract ReentrantMockERC20 is MockERC20 {{
    address public hook_target;
    bytes public hook_data;
    bool reentered;
    function setHook(address t, bytes calldata d) external {{ hook_target = t; hook_data = d; }}
    function transfer(address to, uint256 amt) external override returns (bool) {{
        if (hook_target != address(0) && !reentered) {{
            reentered = true;
            (bool ok,) = hook_target.call(hook_data);
            ok; // ignore — the inner call IS the re-entry
        }}
        require(_bal[msg.sender] >= amt, "bal");
        _bal[msg.sender] -= amt; _bal[to] += amt; emit Transfer(msg.sender, to, amt);
        return true;
    }}
}}
```

For a USDT-style "non-zero-to-non-zero approve fails" mock:

```solidity
contract UsdtStyleMockERC20 is MockERC20 {{
    function approve(address s, uint256 amt) external override returns (bool) {{
        require(amt == 0 || _allow[msg.sender][s] == 0, "USDT: must reset to 0 first");
        _allow[msg.sender][s] = amt; emit Approval(msg.sender, s, amt); return true;
    }}
}}
```

For a revert-on-transfer blocklist mock:

```solidity
contract BlocklistMockERC20 is MockERC20 {{
    mapping(address => bool) public blocked;
    function block_(address a) external {{ blocked[a] = true; }}
    function transfer(address to, uint256 amt) external override returns (bool) {{
        require(!blocked[to], "BLOCKED");
        require(_bal[msg.sender] >= amt, "bal");
        _bal[msg.sender] -= amt; _bal[to] += amt; emit Transfer(msg.sender, to, amt);
        return true;
    }}
}}
```

For a manipulable IOracle mock:

```solidity
contract MockOracle is IOracle {{
    mapping(address => uint256) public p;
    function price(address t) external view override returns (uint256) {{ return p[t]; }}
    function setPrice(address t, uint256 v) external {{ p[t] = v; }}
}}
```

If you need a custom mock NOT covered above, ENSURE it implements
ALL methods on the interface it claims to satisfy. Use `forge build`
mentally: "would this compile against THIS repo's IERC20/IOracle?"

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

    def validate_test_body(
        self,
        body: str,
        engine_repo_root: Path,
    ) -> tuple[bool, str | None]:
        """Real-compile check: run ``forge build`` against the LLM's test.

        The previous validator was regex-only (hex literals, marker
        presence) which let through valid-looking but uncompilable
        Solidity — "Contract should be marked as abstract", re-declared
        interfaces, missing IERC20 methods. By writing the candidate
        to a uniquely-named temp file in the repo's test dir and
        invoking ``forge build``, we surface the exact solc error to
        the retry loop so the LLM can fix the specific issue. Forge's
        incremental cache makes the per-attempt build ~1-3s.

        Returns (True, None) on clean compile. Returns (False, err)
        with the last ~2KB of solc output on failure.
        """
        import hashlib

        test_dir = _detect_foundry_test_dir(engine_repo_root)
        try:
            test_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return (True, None)  # can't validate; let run_test surface it

        h = hashlib.md5(body.encode("utf-8", errors="replace")).hexdigest()[:12]
        tmp = test_dir / f"_jelleo_validate_{h}.t.sol"
        try:
            tmp.write_text(body, encoding="utf-8")
        except OSError:
            return (True, None)

        try:
            proc = subprocess.run(
                ["forge", "build"],
                cwd=str(engine_repo_root),
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            tmp.unlink(missing_ok=True)
            return (True, None)  # forge missing — let run_test fail with clear toolchain error
        except subprocess.TimeoutExpired:
            tmp.unlink(missing_ok=True)
            return (False, "forge build timed out during validation (>120s)")
        finally:
            tmp.unlink(missing_ok=True)

        if proc.returncode == 0:
            return (True, None)

        # Compile failed. Surface the actual error so the LLM gets a
        # specific issue to fix on next attempt.
        err_text = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
        # Filter to error-relevant lines (skip warnings) for token budget.
        lines = err_text.splitlines()
        relevant: list[str] = []
        capture = 0
        for ln in lines:
            stripped = ln.strip()
            if stripped.startswith("Error") or stripped.startswith("error"):
                capture = 8  # capture next 8 lines after each error
                relevant.append(ln)
            elif capture > 0:
                relevant.append(ln)
                capture -= 1
        msg = "\n".join(relevant) if relevant else err_text
        msg = msg[-2000:]
        return (False, f"forge build failed:\n{msg}")

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

        # Foundry's test dir is configurable in foundry.toml (key `test`
        # under [profile.default]). Different OSec repos use `test/` or
        # `tests/` — read the manifest to pick the right one. Default
        # to `test/` if foundry.toml is missing or unparseable.
        repo_test_dir = _detect_foundry_test_dir(target_repo_root)
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
        # Forge --json schema varies by version:
        #   * Older forge: test_results: { test_name: { success: bool, reason } }
        #   * Newer forge: test_results: { test_name: { status: "Success"|"Failure", reason } }
        # We accept BOTH. A test fires if EITHER success is False OR status
        # is "Failure" (case-insensitive). The status field also occasionally
        # appears as "Skipped" — we treat that as not-fired-and-not-clean
        # (the test never actually ran).
        fired_tests: list[str] = []
        skipped_tests: list[str] = []
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
                        status = str(tdata.get("status") or "").strip().lower()
                        success = tdata.get("success")
                        if status == "failure" or success is False:
                            fired_tests.append(tname)
                            decoded_logs = tdata.get("decoded_logs") or [""]
                            first_log = decoded_logs[0] if decoded_logs else ""
                            failure_reason = (
                                tdata.get("reason")
                                or first_log
                                or "test failed"
                            )
                        elif status == "skipped":
                            skipped_tests.append(tname)
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

        if skipped_tests and not fired_tests:
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout=stdout,
                stderr=stderr,
                returncode=0,
                duration_s=duration,
                framework=self.framework,
                reason=f"forge skipped {len(skipped_tests)} test(s) — PoC didn't actually run",
                metadata={"skipped_tests": skipped_tests[:5], "phase": "skipped"},
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
