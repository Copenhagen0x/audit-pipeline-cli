"""Smoke test for SolidityAdapter.validate_test_body — run against a live repo.

Usage:
    python3 scripts/smoke_validate_solidity.py /path/to/foundry/repo
"""
from __future__ import annotations

import sys
from pathlib import Path

from audit_pipeline.poc_adapters.solidity import SolidityAdapter


BAD_HEX = """pragma solidity ^0.8.20;
import "forge-std/Test.sol";
contract HexBugTest is Test {
    address bad = address(0xADMIN);
    function test_bad() public pure {}
}
"""

GOOD = """pragma solidity ^0.8.20;
import "forge-std/Test.sol";
contract GoodTest is Test {
    function test_good() public pure { assertTrue(true); }
}
"""

ABSTRACT_BUG = """pragma solidity ^0.8.20;
import "forge-std/Test.sol";
import "@src/vendor/openzeppelin/token/ERC20/IERC20.sol";
contract BadMock is IERC20 {
    function balanceOf(address) external view override returns (uint256) { return 0; }
    function totalSupply() external view override returns (uint256) { return 0; }
    function transfer(address, uint256) external override returns (bool) { return true; }
    function transferFrom(address, address, uint256) external override returns (bool) { return true; }
    function approve(address, uint256) external override returns (bool) { return true; }
    function allowance(address, address) external view override returns (uint256) { return 0; }
}
contract AbstractBugTest is Test {
    function test_bad() public pure {}
}
"""


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: smoke_validate_solidity.py <repo_root>", file=sys.stderr)
        return 2

    repo = Path(sys.argv[1])
    if not (repo / "foundry.toml").is_file():
        print(f"not a foundry repo: {repo}", file=sys.stderr)
        return 2

    a = SolidityAdapter()

    print("--- BAD HEX (0xADMIN) ---")
    ok, err = a.validate_test_body(BAD_HEX, repo)
    print(f"  valid? {ok}")
    print(f"  err: {(err or 'None')[:200]}")
    assert ok is False, "expected BAD_HEX to fail validation"
    assert "Identifier-start" in (err or "") or "8936" in (err or ""), (
        f"expected solc Error(8936) about hex digits, got: {err}"
    )

    print("--- ABSTRACT BUG (missing decimals) ---")
    ok, err = a.validate_test_body(ABSTRACT_BUG, repo)
    print(f"  valid? {ok}")
    print(f"  err: {(err or 'None')[:200]}")
    assert ok is False, "expected ABSTRACT_BUG to fail validation"
    assert "abstract" in (err or "").lower() or "3656" in (err or ""), (
        f"expected solc Error(3656) about abstract contract, got: {err}"
    )

    print("--- GOOD ---")
    ok, err = a.validate_test_body(GOOD, repo)
    print(f"  valid? {ok}")
    print(f"  err: {err}")
    assert ok is True, f"expected GOOD to validate clean, got err: {err}"

    print("\nALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
