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


def _detect_foundry_test_dir(repo_root: Path) -> Path:
    """Read foundry.toml's `test = "..."` key. Defaults to `test/`.

    OSec eval repos use `tests/` (plural) while many Foundry defaults
    use `test/`. We honor whatever foundry.toml says so `forge test
    --match-path` resolves to the correct location.
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

        return f"""You are authoring a Foundry fuzz / invariant harness for the Jelleo audit engine.

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Function under test: {engine_function}

# Grounded source

{source_context}

# Run command

  forge test --match-path tests/jelleo_l4_fuzz_<name>.t.sol --fuzz-runs 2000 --json

A failing fuzz/invariant test = bug confirmed with concrete inputs.

# Repo conventions + Solidity syntax pitfalls

* Imports use `@src/` remapping (per foundry.toml). Example:
    `import "@src/ContractA.sol";`
    `import "@src/vendor/openzeppelin/token/ERC20/IERC20.sol";`
* NO aggregator like `ExternalInterfaces.sol` exists. Import the
  individual interface files when needed:
    `import "@src/interfaces/IOracle.sol";`
    `import "@src/interfaces/IBridgeAdapter.sol";`
    `import "@src/interfaces/IStrategy.sol";`
  (and IFlashBorrower, IGovernanceHook, IRateModel — only what you use)
* DO NOT re-declare interfaces already in scope via @src imports.
  Writing a second `interface IBridgeAdapter {{ ... }}` after importing
  it causes "Identifier already declared" compile error.
* DO NOT call non-existent helper functions like `_healthy_exposed`.
  Internal functions are NOT directly callable from tests — derive
  the bug surface from public/external entrypoints only.
* `IERC20` in this repo has 7 functions (decimals, totalSupply,
  balanceOf, allowance, transfer, transferFrom, approve). Any mock
  ERC-20 inheriting `is IERC20` must implement ALL of them or you
  get "Contract should be marked as abstract".
* **Hex literals must be VALID HEX** (chars 0-9, a-f, A-F). Writing
  `address(0xADMIN)`, `address(0xGOVERNOR)`, `address(0xA77ACK)` is
  a SYNTAX ERROR — M/N/T/K/G/V/R/O aren't hex digits. For named test
  addresses use `makeAddr("admin")`, `vm.addr(1)`, or a numeric literal
  like `address(0xA11CE)` (valid hex — A/1/C/E are all hex). NEVER
  write hex digits that look like English words unless every character
  is in `[0-9a-fA-F]`.
* Address literals 0x000...{{20 hex chars}} that LOOK like a checksummed
  address must match EIP-55 mixed case. If you don't want to compute
  the checksum, use `address(uint160(N))` for any uint constant N, or
  use `makeAddr("name")` — both compile cleanly regardless of case.
* **NO C/Rust-style void casts** (`(void)(x);` or `(void)x;`). Solidity
  has no `void` type and no C-style cast syntax — SYNTAX ERROR.
  Use bare `x;` to silence unused-variable warnings.
* **ASCII ONLY inside string literals.** Solidity 0.8.x rejects Unicode
  characters (em-dash `—`, curly quotes `'` `"`, ellipsis `…`, etc.)
  in regular `"..."` strings with `Error (8936): Invalid character in
  string`. Use plain `-`, `'`, `"`, `...` instead. Unicode in COMMENTS
  is fine; only string literals are affected. (The parser auto-strips
  any Unicode that sneaks through, but readable ASCII is preferred.)
* **Contract name MUST differ from its test function names.** Solidity
  treats a function with the same name as its containing contract as
  a constructor (legacy syntax) which is invalid in 0.8.x.

# Mock contract templates (COPY-PASTE these if you need mocks)

```solidity
// Full IERC20 mock — implements all 7 methods, NOT abstract
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

// USDT-style mock (non-zero-to-non-zero approve reverts)
contract UsdtStyleMockERC20 is MockERC20 {{
    function approve(address s, uint256 amt) external override returns (bool) {{
        require(amt == 0 || _allow[msg.sender][s] == 0, "USDT: must reset to 0 first");
        _allow[msg.sender][s] = amt; emit Approval(msg.sender, s, amt); return true;
    }}
}}

// Blocklist mock (transfer to blocked address reverts — USDC/USDT pattern)
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

// Reentrant ERC-777-style hook mock (for re-entry tests)
contract ReentrantMockERC20 is MockERC20 {{
    address public hook_target;
    bytes public hook_data;
    bool reentered;
    function setHook(address t, bytes calldata d) external {{ hook_target = t; hook_data = d; }}
    function transfer(address to, uint256 amt) external override returns (bool) {{
        if (hook_target != address(0) && !reentered) {{
            reentered = true;
            (bool ok,) = hook_target.call(hook_data);
            ok;
        }}
        require(_bal[msg.sender] >= amt, "bal");
        _bal[msg.sender] -= amt; _bal[to] += amt; emit Transfer(msg.sender, to, amt);
        return true;
    }}
}}

// Manipulable IOracle mock
contract MockOracle is IOracle {{
    mapping(address => uint256) public p;
    mapping(address => uint256) public ts;
    function price(address t) external view override returns (uint256 priceE18, uint256 updatedAt) {{ return (p[t], ts[t]); }}
    function setPrice(address t, uint256 v) external {{ p[t] = v; ts[t] = block.timestamp; }}
    function setPriceWithTimestamp(address t, uint256 v, uint256 _ts) external {{ p[t] = v; ts[t] = _ts; }}
}}
```

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
            return self._sanitize_unicode(m.group(1).strip()) + "\n"
        m = re.search(r"```\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            body = m.group(1).strip()
            if "contract " in body or "testFuzz_" in body or "invariant_" in body:
                return self._sanitize_unicode(body) + "\n"
        raise ValueError("Could not extract a Solidity fuzz harness from the LLM response.")

    @staticmethod
    def _sanitize_unicode(body: str) -> str:
        """Replace common Unicode chars (em-dash, curly quotes, etc.) with ASCII
        equivalents. Solidity 0.8.x rejects non-ASCII in regular string literals
        (`Error 8936: Invalid character in string`). The LLM author frequently
        writes em-dashes in assertion messages — strip them at parse time so
        we don't keep retrying the same failure mode.
        """
        replacements = {
            "—": "-",   # em-dash
            "–": "-",   # en-dash
            "‘": "'",   # left single quote
            "’": "'",   # right single quote / apostrophe
            "“": '"',   # left double quote
            "”": '"',   # right double quote
            "…": "...", # ellipsis
            " ": " ",   # non-breaking space
            "×": "x",   # multiplication sign
            "→": "->",  # right arrow
            "←": "<-",  # left arrow
        }
        for u, a in replacements.items():
            body = body.replace(u, a)
        # Anything still non-ASCII becomes a space — last-resort safety net
        # so a single rogue char doesn't tank the whole harness compile.
        body = "".join(c if ord(c) < 128 else " " for c in body)
        return body

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

        # Deploy into the foundry.toml-configured test dir (test/ or tests/)
        repo_test_dir = _detect_foundry_test_dir(target_repo_root)
        repo_test_dir.mkdir(parents=True, exist_ok=True)
        deployed = repo_test_dir / f"jelleo_l4_fuzz_{harness_name}.t.sol"
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

        # Parse forge JSON output — failed fuzz tests = crashes.
        # Forge JSON schema varies by version:
        #   older: {"test_results": {"test_x": {"success": false, ...}}}
        #   newer: {"test_results": {"test_x": {"status": "Failure", ...}}}
        # Accept BOTH so this adapter works against forge >= 1.5.
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
                    if not isinstance(tdata, dict):
                        continue
                    status = str(tdata.get("status") or "").strip().lower()
                    success = tdata.get("success")
                    if status == "failure" or success is False:
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
