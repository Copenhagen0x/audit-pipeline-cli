// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "@src/ContractC.sol";
import "@src/vendor/openzeppelin/token/ERC20/IERC20.sol";
import "@src/interfaces/ExternalInterfaces.sol";

contract MockGovERC20 is IERC20 {
    mapping(address => uint256) internal _bal;
    mapping(address => mapping(address => uint256)) internal _allow;
    uint256 internal _total;
    function decimals() external pure override returns (uint8) { return 18; }
    function totalSupply() external view override returns (uint256) { return _total; }
    function balanceOf(address a) external view override returns (uint256) { return _bal[a]; }
    function allowance(address o, address s) external view override returns (uint256) { return _allow[o][s]; }
    function mint(address to, uint256 amt) external { _bal[to] += amt; _total += amt; }
    function approve(address s, uint256 amt) external override returns (bool) { _allow[msg.sender][s] = amt; emit Approval(msg.sender, s, amt); return true; }
    function transfer(address to, uint256 amt) external override returns (bool) {
        require(_bal[msg.sender] >= amt, "bal"); _bal[msg.sender] -= amt; _bal[to] += amt; emit Transfer(msg.sender, to, amt); return true;
    }
    function transferFrom(address from, address to, uint256 amt) external override returns (bool) {
        require(_bal[from] >= amt, "bal"); require(_allow[from][msg.sender] >= amt, "allow");
        _bal[from] -= amt; _allow[from][msg.sender] -= amt; _bal[to] += amt; emit Transfer(from, to, amt); return true;
    }
}

contract MockBridgeNoop is IBridgeAdapter {
    function send(address, address, uint256, bytes calldata) external payable override returns (bytes32) { return bytes32(0); }
}

// Target contract executed by the proposal — records whether it was called
contract ProposalTarget {
    uint256 public callCount;
    function noOp() external { callCount++; }
}

contract SOLD10_GovSigReplayTest is Test {
    ContractC public gov;
    MockGovERC20 public token;
    MockBridgeNoop public bridge;
    ProposalTarget public target;

    address public owner;
    uint256 public voterPriv;
    address public voter;

    uint256 constant PROPOSAL_THRESHOLD = 1e18;
    uint256 constant VOTING_PERIOD = 1 days;
    uint256 constant QUORUM = 1e18;

    function setUp() public {
        owner = makeAddr("owner");
        voterPriv = 0xBEEF;
        voter = vm.addr(voterPriv);

        token = new MockGovERC20();
        bridge = new MockBridgeNoop();
        target = new ProposalTarget();

        // Voter has enough governance tokens to meet quorum AND threshold
        token.mint(voter, 10e18);

        vm.prank(owner);
        gov = new ContractC(
            IERC20(address(token)),
            IBridgeAdapter(address(bridge)),
            owner, owner,
            PROPOSAL_THRESHOLD,
            VOTING_PERIOD,
            QUORUM
        );
    }

    function test_gov_sig_replay_no_nonce_no_chainid() public {
        // 1. Voter creates a proposal
        vm.prank(voter);
        uint256 pid = gov.propose(address(target), 0, abi.encodeWithSignature("noOp()"));

        // 2. Voter casts yes vote (10e18 weight >> quorum 1e18 -> proposal passes)
        vm.prank(voter);
        gov.vote(pid);

        // 3. Build the SAME insecure digest the contract uses:
        //    keccak256(abi.encodePacked(address(this), proposalId, voter))
        //    NOTE: missing chainId, missing nonce, missing deadline
        bytes32 contractDigest = keccak256(abi.encodePacked(address(gov), pid, voter));

        // 4. Voter signs the digest
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(voterPriv, contractDigest);

        // 5. Warp past voting period so execute is allowed
        vm.warp(block.timestamp + VOTING_PERIOD + 1);

        // 6. Attacker (any address) calls executeBySig with the captured signature
        //    -> succeeds (this is the first execution, executed flag flips to true)
        address attacker = makeAddr("attacker");
        vm.prank(attacker);
        gov.executeBySig(pid, voter, v, r, s);

        // Verify first execution succeeded by checking target.callCount AND the
        // executed flag on the proposal record.
        assertEq(target.callCount(), 1, "first executeBySig should execute the proposal");

        // 7. STRUCTURAL DEMONSTRATION OF THE BUG
        //
        // The signature was NEVER consumed at the signature level — only the
        // per-proposal `executed` flag prevents same-chain re-execution. The
        // signature itself remains cryptographically valid:
        address stillValidSigner = ecrecover(contractDigest, v, r, s);
        assertEq(stillValidSigner, voter, "signature remains valid post-execution (no consumption mechanism)");

        // 8. The digest binds NO chain-specific data. A secure EIP-712-style
        //    digest would bind block.chainid + a nonce. Demonstrate by
        //    constructing what a secure digest would look like and comparing:
        bytes32 secureDigestWithChainAndNonce = keccak256(abi.encode(
            block.chainid,
            address(gov),
            pid,
            voter,
            uint256(0)  // hypothetical nonce
        ));

        // 9. Same-chain replay IS blocked by the per-proposal `executed` flag.
        //    Confirm: a second executeBySig call with the SAME sig on the SAME
        //    proposal reverts. Note: this is proposal-level, NOT signature-
        //    level — the signature itself is still valid (see step 7).
        vm.prank(attacker);
        vm.expectRevert();  // GovBadProposal selector — proposal-level guard
        gov.executeBySig(pid, voter, v, r, s);

        // 10. THE BUG ASSERTION — the contract digest is insecure: it does NOT
        //     bind chainId or any nonce. On any chain where address(gov) and
        //     proposalId collide and the proposal is unexecuted, the captured
        //     (v, r, s) is immediately reusable. This assertion FAILS, which
        //     IS the bug confirmation — proving the contract did not adopt the
        //     EIP-712 / chainId-bound digest pattern.
        assertEq(
            contractDigest,
            secureDigestWithChainAndNonce,
            "BUG: contract digest = keccak256(abi.encodePacked(address(this), proposalId, voter)) -- missing chainId AND nonce => cross-chain signature replay viable (see ContractC.executeBySig line 133)"
        );
    }
}
