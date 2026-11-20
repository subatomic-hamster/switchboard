// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Vault} from "../src/Vault.sol";

contract MockUSDC is ERC20 {
    constructor() ERC20("USDC", "USDC") {}

    function decimals() public pure override returns (uint8) {
        return 6;
    }

    function mint(address to, uint256 a) external {
        _mint(to, a);
    }
}

contract VaultTest is Test {
    event Spent(uint256 permitId, address agent, address payee, uint256 amount);
    event ApprovalRequested(uint256 requestId, address agent, address payee, uint256 amount, uint8 reason);
    event Resolved(uint256 requestId, bool approved);
    event Frozen(bool frozen);
    event PermitIssued(uint256 permitId, address agent, address payee, uint256 amount, uint64 expiresAt);

    MockUSDC usdc;
    Vault vault;
    address owner = makeAddr("owner");
    address approver = makeAddr("approver");
    address agent = makeAddr("agent");
    address payee = makeAddr("payee");
    address stranger = makeAddr("stranger");

    uint128 constant CAP = 100e6;
    uint128 constant BUDGET = 250e6;
    uint64 constant TTL = 1 hours;

    function setUp() public {
        vm.warp(1_000_000);
        usdc = new MockUSDC();
        vault = new Vault(IERC20(address(usdc)), owner, approver);
        usdc.mint(address(vault), 10_000e6);
        vm.startPrank(owner);
        vault.setPolicy(agent, CAP, BUDGET, TTL);
        vault.setPayeeAllowed(agent, payee, true);
        vm.stopPrank();
    }

    function _req(uint256 amt) internal returns (uint256 id, bool isPermit) {
        vm.prank(agent);
        return vault.requestSpend(payee, amt, bytes32("p"));
    }

    function _pendingReq(uint256 amt) internal returns (uint256 id) {
        bool isPermit;
        (id, isPermit) = _req(amt);
        assertFalse(isPermit);
    }

    // ---- permit path

    function test_permitIssueAndRedeem() public {
        vm.expectEmit(address(vault));
        emit PermitIssued(1, agent, payee, 50e6, uint64(block.timestamp + 10 minutes));
        (uint256 id, bool isPermit) = _req(50e6);
        assertTrue(isPermit);
        assertEq(vault.spentInWindow(agent), 50e6);

        vm.expectEmit(address(vault));
        emit Spent(id, agent, payee, 50e6);
        vm.prank(agent);
        vault.redeem(id);
        assertEq(usdc.balanceOf(payee), 50e6);
        assertTrue(vault.getPermit(id).used);
    }

    function test_replayReverts() public {
        (uint256 id,) = _req(50e6);
        vm.startPrank(agent);
        vault.redeem(id);
        vm.expectRevert(Vault.PermitUsedOrUnknown.selector);
        vault.redeem(id);
        vm.stopPrank();
    }

    function test_redeemWrongCaller() public {
        (uint256 id,) = _req(50e6);
        vm.prank(stranger);
        vm.expectRevert(Vault.NotPermitAgent.selector);
        vault.redeem(id);
    }

    function test_redeemExpired() public {
        (uint256 id,) = _req(50e6);
        vm.warp(block.timestamp + 10 minutes + 1);
        vm.prank(agent);
        vm.expectRevert(Vault.PermitExpired.selector);
        vault.redeem(id);
    }

    function test_unknownAgentReverts() public {
        vm.prank(stranger);
        vm.expectRevert(Vault.AgentNotActive.selector);
        vault.requestSpend(payee, 1, 0);
    }

    // ---- reasons

    function test_reasonPayeeNotAllowed() public {
        address other = makeAddr("other");
        vm.expectEmit(address(vault));
        emit ApprovalRequested(1, agent, other, 1e6, 3);
        vm.prank(agent);
        (uint256 id, bool isPermit) = vault.requestSpend(other, 1e6, 0);
        assertFalse(isPermit);
        assertEq(vault.getRequest(id).deadline, block.timestamp + TTL);
    }

    function test_reasonOverCap() public {
        vm.expectEmit(address(vault));
        emit ApprovalRequested(1, agent, payee, CAP + 1, 1);
        _req(CAP + 1);
    }

    function test_reasonOverBudget() public {
        _req(100e6);
        _req(100e6);
        vm.expectEmit(address(vault));
        emit ApprovalRequested(1, agent, payee, 60e6, 2);
        _req(60e6);
    }

    function test_reasonFrozen() public {
        vm.prank(owner);
        vault.freeze();
        vm.expectEmit(address(vault));
        emit ApprovalRequested(1, agent, payee, 1e6, 4);
        _req(1e6);
    }

    function test_reasonOrder() public {
        // frozen beats payee; payee beats cap
        address other = makeAddr("other");
        vm.prank(agent);
        (uint256 id,) = vault.requestSpend(other, CAP + 1, 0);
        assertEq(vault.getRequest(id).reason, 3);
        vm.prank(owner);
        vault.freeze();
        vm.prank(agent);
        (id,) = vault.requestSpend(other, CAP + 1, 0);
        assertEq(vault.getRequest(id).reason, 4);
    }

    // ---- approval flow

    function test_approvePays() public {
        uint256 id = _pendingReq(CAP + 1);
        vm.expectEmit(address(vault));
        emit Spent(1, agent, payee, CAP + 1);
        vm.expectEmit(address(vault));
        emit Resolved(id, true);
        vm.prank(approver);
        vault.approve(id);
        assertEq(usdc.balanceOf(payee), CAP + 1);
        assertEq(vault.spentInWindow(agent), CAP + 1);
        assertEq(uint8(vault.getRequest(id).status), 2);
    }

    function test_approveByOwner() public {
        uint256 id = _pendingReq(CAP + 1);
        vm.prank(owner);
        vault.approve(id);
        assertEq(usdc.balanceOf(payee), CAP + 1);
    }

    function test_approveNonApproverReverts() public {
        uint256 id = _pendingReq(CAP + 1);
        vm.prank(agent);
        vm.expectRevert(Vault.NotOwnerOrApprover.selector);
        vault.approve(id);
        vm.prank(stranger);
        vm.expectRevert(Vault.NotOwnerOrApprover.selector);
        vault.approve(id);
    }

    function test_approveTwiceReverts() public {
        uint256 id = _pendingReq(CAP + 1);
        vm.startPrank(approver);
        vault.approve(id);
        vm.expectRevert(Vault.NotPending.selector);
        vault.approve(id);
        vm.stopPrank();
    }

    function test_deny() public {
        uint256 id = _pendingReq(CAP + 1);
        vm.expectEmit(address(vault));
        emit Resolved(id, false);
        vm.prank(approver);
        vault.deny(id);
        assertEq(usdc.balanceOf(payee), 0);
        vm.prank(approver);
        vm.expectRevert(Vault.NotPending.selector);
        vault.approve(id);
    }

    function test_denyNonApproverReverts() public {
        uint256 id = _pendingReq(CAP + 1);
        vm.prank(stranger);
        vm.expectRevert(Vault.NotOwnerOrApprover.selector);
        vault.deny(id);
    }

    function test_expire() public {
        uint256 id = _pendingReq(CAP + 1);
        vm.prank(stranger);
        vm.expectRevert(Vault.DeadlineNotPassed.selector);
        vault.expire(id);

        vm.warp(block.timestamp + TTL + 1);
        vm.expectEmit(address(vault));
        emit Resolved(id, false);
        vm.prank(stranger);
        vault.expire(id);
        assertEq(uint8(vault.getRequest(id).status), 3);
    }

    function test_approveAfterDeadlineReverts() public {
        uint256 id = _pendingReq(CAP + 1);
        vm.warp(block.timestamp + TTL + 1);
        vm.prank(approver);
        vm.expectRevert(Vault.DeadlinePassed.selector);
        vault.approve(id);
    }

    function test_approveInsufficientBalanceReverts() public {
        uint256 big = 50_000e6;
        uint256 id = _pendingReq(big);
        vm.prank(approver);
        vm.expectRevert(Vault.InsufficientVaultBalance.selector);
        vault.approve(id);
        // state rolled back: still pending, nothing counted
        assertEq(uint8(vault.getRequest(id).status), 1);
        assertEq(vault.spentInWindow(agent), 0);
    }

    // ---- access control

    function test_agentCannotAdmin() public {
        vm.startPrank(agent);
        vm.expectRevert(Vault.NotOwner.selector);
        vault.setPolicy(agent, 1e12, 1e12, 1);
        vm.expectRevert(Vault.NotOwner.selector);
        vault.setPayeeAllowed(agent, stranger, true);
        vm.expectRevert(Vault.NotOwner.selector);
        vault.unfreeze();
        vm.expectRevert(Vault.NotOwner.selector);
        vault.revokeAgent(agent);
        vm.expectRevert(Vault.NotOwner.selector);
        vault.setApprover(agent);
        vm.expectRevert(Vault.NotOwnerOrApprover.selector);
        vault.freeze();
        vm.stopPrank();
    }

    // ---- freeze

    function test_freezeBlocksRedeemAndApprove() public {
        (uint256 pid,) = _req(10e6);
        uint256 rid = _pendingReq(CAP + 1);
        vm.expectEmit(address(vault));
        emit Frozen(true);
        vm.prank(owner);
        vault.freeze();

        vm.prank(agent);
        vm.expectRevert(Vault.IsFrozen.selector);
        vault.redeem(pid);
        vm.prank(approver);
        vm.expectRevert(Vault.IsFrozen.selector);
        vault.approve(rid);

        (uint256 id, bool isPermit) = _req(1e6);
        assertFalse(isPermit);
        assertEq(vault.getRequest(id).reason, 4);

        vm.prank(owner);
        vault.unfreeze();
        vm.prank(agent);
        vault.redeem(pid);
    }

    function test_approverFreezesButCannotUnfreeze() public {
        vm.prank(approver);
        vault.freeze();
        assertTrue(vault.frozen());
        vm.prank(approver);
        vm.expectRevert(Vault.NotOwner.selector);
        vault.unfreeze();
    }

    function test_revokedAgent() public {
        (uint256 pid,) = _req(10e6);
        vm.prank(owner);
        vault.revokeAgent(agent);
        vm.startPrank(agent);
        vm.expectRevert(Vault.AgentNotActive.selector);
        vault.redeem(pid);
        vm.expectRevert(Vault.AgentNotActive.selector);
        vault.requestSpend(payee, 1e6, 0);
        vm.stopPrank();
    }

    function test_revokedAgentPendingCannotBeApproved() public {
        uint256 rid = _pendingReq(CAP + 1);
        vm.prank(owner);
        vault.revokeAgent(agent);
        vm.prank(approver);
        vm.expectRevert(Vault.AgentNotActive.selector);
        vault.approve(rid);
    }

    // ---- rolling window

    function test_rollingBudgetFreesAfter24h() public {
        (uint256 a,) = _req(100e6);
        vm.warp(block.timestamp + 12 hours);
        (uint256 b,) = _req(100e6);
        assertEq(vault.spentInWindow(agent), 200e6);
        vm.prank(agent);
        vault.redeem(b); // not expired? permit ttl 10m from issue; fine
        assertEq(vault.getRequest(_pendingReq(100e6)).reason, 2);

        // first spend leaves the window at +24h, second still inside
        vm.warp(block.timestamp + 12 hours);
        assertEq(vault.spentInWindow(agent), 100e6);
        (, bool ok) = _req(100e6);
        assertTrue(ok);
        a;
    }

    // ---- fuzz

    function testFuzz_neverExceedsBudget(uint96[8] memory amts, uint16[8] memory gaps) public {
        uint256 paid;
        uint256 start = block.timestamp;
        for (uint256 i; i < 8; i++) {
            vm.warp(block.timestamp + (gaps[i] % 2 hours)); // 8 * <2h < 24h: one window
            uint256 amt = uint256(amts[i]) % (CAP + 1);
            if (amt == 0) continue;
            (uint256 id, bool isPermit) = _req(amt);
            if (isPermit) {
                vm.prank(agent);
                vault.redeem(id);
                paid += amt;
            }
            assertLe(paid, BUDGET);
            assertLe(vault.spentInWindow(agent), BUDGET);
        }
        assertLt(block.timestamp - start, 24 hours);
        assertEq(usdc.balanceOf(payee), paid);
    }
}
