// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Escrow} from "../src/Escrow.sol";
import {MockUSDC} from "./Vault.t.sol";

contract EscrowTest is Test {
    event Deposited(uint256 id, address depositor, bytes32 claimHash, uint256 amount, uint64 expiry, address refundTo);
    event ClaimCommitted(uint256 id, bytes32 commitment);
    event Claimed(uint256 id, address to, uint256 amount);
    event Refunded(uint256 id, address refundTo, uint256 amount);
    event PaymentRequested(uint256 id, address requester, address payer, uint256 amount, string memo);
    event PaymentPaid(uint256 id, address requester, address payer, uint256 amount);

    MockUSDC usdc;
    Escrow escrow;
    address sender = makeAddr("sender");
    address refundTo = makeAddr("refundTo");
    address friend = makeAddr("friend");
    address attacker = makeAddr("attacker");
    address anyone = makeAddr("anyone");

    bytes32 constant SECRET = keccak256("the claim link secret");
    uint64 expiry;

    function setUp() public {
        vm.warp(1_000_000);
        expiry = uint64(block.timestamp + 1 days);
        usdc = new MockUSDC();
        escrow = new Escrow(IERC20(address(usdc)));
        usdc.mint(sender, 1_000e6);
        vm.prank(sender);
        usdc.approve(address(escrow), type(uint256).max);
    }

    function _deposit(uint256 amt) internal returns (uint256 id) {
        vm.prank(sender);
        id = escrow.deposit(keccak256(abi.encode(SECRET)), amt, expiry, refundTo);
    }

    /// Commit in one block, claim in the next, as a recipient would.
    function _claim(uint256 id, address to) internal {
        vm.prank(friend);
        escrow.commitClaim(id, escrow.commitmentFor(id, SECRET, to));
        vm.roll(block.number + 1);
        vm.prank(friend);
        escrow.claim(id, SECRET, to);
    }

    // ---- escrow

    function test_depositPullsFunds() public {
        vm.expectEmit(address(escrow));
        emit Deposited(1, sender, keccak256(abi.encode(SECRET)), 100e6, expiry, refundTo);
        uint256 id = _deposit(100e6);
        assertEq(id, 1);
        assertEq(usdc.balanceOf(address(escrow)), 100e6);
        assertEq(usdc.balanceOf(sender), 900e6);
    }

    function test_depositRejectsBadInput() public {
        vm.startPrank(sender);
        bytes32 h = keccak256(abi.encode(SECRET));
        vm.expectRevert(Escrow.ZeroAmount.selector);
        escrow.deposit(h, 0, expiry, refundTo);
        vm.expectRevert(Escrow.ExpiryInPast.selector);
        escrow.deposit(h, 1e6, uint64(block.timestamp), refundTo);
        vm.expectRevert(Escrow.ZeroAddress.selector);
        escrow.deposit(h, 1e6, expiry, address(0));
        vm.stopPrank();
    }

    function test_claimPaysRecipient() public {
        uint256 id = _deposit(100e6);
        bytes32 c = escrow.commitmentFor(id, SECRET, friend);
        vm.expectEmit(address(escrow));
        emit ClaimCommitted(id, c);
        escrow.commitClaim(id, c);
        vm.roll(block.number + 1);
        vm.expectEmit(address(escrow));
        emit Claimed(id, friend, 100e6);
        escrow.claim(id, SECRET, friend);
        assertEq(usdc.balanceOf(friend), 100e6);
        assertEq(usdc.balanceOf(address(escrow)), 0);
    }

    function test_doubleClaimReverts() public {
        uint256 id = _deposit(100e6);
        _claim(id, friend);
        vm.expectRevert(Escrow.AlreadySettled.selector);
        escrow.claim(id, SECRET, friend);
        vm.expectRevert(Escrow.AlreadySettled.selector);
        escrow.commitClaim(id, bytes32(uint256(1)));
        assertEq(usdc.balanceOf(friend), 100e6);
    }

    function test_claimAfterExpiryReverts() public {
        uint256 id = _deposit(100e6);
        vm.prank(friend);
        escrow.commitClaim(id, escrow.commitmentFor(id, SECRET, friend));
        vm.roll(block.number + 1);
        vm.warp(expiry);
        vm.expectRevert(Escrow.Expired.selector);
        escrow.claim(id, SECRET, friend);
        vm.expectRevert(Escrow.Expired.selector);
        escrow.commitClaim(id, bytes32(uint256(1)));
    }

    function test_refundBeforeExpiryReverts() public {
        uint256 id = _deposit(100e6);
        vm.warp(expiry - 1);
        vm.expectRevert(Escrow.NotExpired.selector);
        escrow.refund(id);
    }

    function test_refundAfterExpiryByAnyone() public {
        uint256 id = _deposit(100e6);
        vm.warp(expiry);
        vm.expectEmit(address(escrow));
        emit Refunded(id, refundTo, 100e6);
        vm.prank(anyone);
        escrow.refund(id);
        assertEq(usdc.balanceOf(refundTo), 100e6);
        vm.expectRevert(Escrow.AlreadySettled.selector);
        escrow.refund(id);
    }

    function test_refundAfterClaimReverts() public {
        uint256 id = _deposit(100e6);
        _claim(id, friend);
        vm.warp(expiry);
        vm.expectRevert(Escrow.AlreadySettled.selector);
        escrow.refund(id);
    }

    function test_wrongSecretReverts() public {
        uint256 id = _deposit(100e6);
        bytes32 bad = keccak256("guess");
        escrow.commitClaim(id, escrow.commitmentFor(id, bad, friend));
        vm.roll(block.number + 1);
        vm.expectRevert(Escrow.WrongSecret.selector);
        escrow.claim(id, bad, friend);
    }

    function test_claimWithoutCommitReverts() public {
        uint256 id = _deposit(100e6);
        vm.expectRevert(Escrow.ClaimNotCommitted.selector);
        escrow.claim(id, SECRET, friend);
    }

    function test_claimInSameBlockAsCommitReverts() public {
        uint256 id = _deposit(100e6);
        escrow.commitClaim(id, escrow.commitmentFor(id, SECRET, friend));
        vm.expectRevert(Escrow.ClaimNotCommitted.selector);
        escrow.claim(id, SECRET, friend);
    }

    function test_unknownDepositReverts() public {
        vm.expectRevert(Escrow.UnknownDeposit.selector);
        escrow.refund(1);
        vm.expectRevert(Escrow.UnknownDeposit.selector);
        escrow.claim(0, SECRET, friend);
    }

    /// The attacker copies the secret and `to` from the victim's pending claim and tries to
    /// redirect the funds. The victim's commitment was made in an earlier block, so the victim's
    /// claim goes through first; the attacker's copy cannot be claimed in the same block.
    function test_frontRunAttemptFails() public {
        uint256 id = _deposit(100e6);
        // Victim commits, then a later block carries the victim's claim.
        vm.prank(friend);
        escrow.commitClaim(id, escrow.commitmentFor(id, SECRET, friend));
        vm.roll(block.number + 1);

        // Attacker sees the victim's claim(id, SECRET, friend) in the mempool and, in the same
        // block, tries to claim to itself outright, and by commit-then-claim.
        vm.startPrank(attacker);
        vm.expectRevert(Escrow.ClaimNotCommitted.selector);
        escrow.claim(id, SECRET, attacker);
        escrow.commitClaim(id, escrow.commitmentFor(id, SECRET, attacker));
        vm.expectRevert(Escrow.ClaimNotCommitted.selector);
        escrow.claim(id, SECRET, attacker);
        vm.stopPrank();

        // The victim's claim lands in that block, ahead of or behind the attacker's attempts.
        vm.prank(friend);
        escrow.claim(id, SECRET, friend);
        assertEq(usdc.balanceOf(friend), 100e6);

        // Next block: the attacker's commitment is now old enough but the deposit is settled.
        vm.roll(block.number + 1);
        vm.prank(attacker);
        vm.expectRevert(Escrow.AlreadySettled.selector);
        escrow.claim(id, SECRET, attacker);
        assertEq(usdc.balanceOf(attacker), 0);
    }

    /// Someone else's commitment to a different recipient does not let the secret redirect funds.
    function test_commitmentBindsRecipient() public {
        uint256 id = _deposit(100e6);
        escrow.commitClaim(id, escrow.commitmentFor(id, SECRET, friend));
        vm.roll(block.number + 1);
        vm.expectRevert(Escrow.ClaimNotCommitted.selector);
        escrow.claim(id, SECRET, attacker);
    }

    // ---- fuzz: conservation of funds

    /// However deposits settle (claim, refund, or still open), the escrow holds exactly the
    /// open deposits and no USDC is created or lost.
    function testFuzz_conservation(uint96 a1, uint96 a2, uint96 a3, uint8 mode) public {
        uint256[3] memory amts = [uint256(a1) % 300e6 + 1, uint256(a2) % 300e6 + 1, uint256(a3) % 300e6 + 1];
        uint256 supply = usdc.totalSupply();
        uint256 open;
        uint256[3] memory ids;
        for (uint256 i; i < 3; i++) {
            ids[i] = _deposit(amts[i]);
            open += amts[i];
        }
        assertEq(usdc.balanceOf(address(escrow)), open);

        // deposit 0: claimed, 1: refunded after expiry, 2: left open (mode picks which is open)
        bool claim0 = mode % 2 == 0;
        if (claim0) {
            _claim(ids[0], friend);
            open -= amts[0];
        }
        vm.warp(expiry);
        escrow.refund(ids[1]);
        open -= amts[1];
        if (mode % 3 == 0) {
            escrow.refund(ids[2]);
            open -= amts[2];
        }
        if (!claim0) {
            escrow.refund(ids[0]);
            open -= amts[0];
        }

        assertEq(usdc.balanceOf(address(escrow)), open);
        assertEq(
            usdc.balanceOf(address(escrow)) + usdc.balanceOf(friend) + usdc.balanceOf(refundTo)
                + usdc.balanceOf(sender),
            1_000e6
        );
        assertEq(usdc.totalSupply(), supply);
    }

    // ---- payment requests

    function test_requestAndPay() public {
        usdc.mint(friend, 50e6);
        vm.prank(friend);
        usdc.approve(address(escrow), 50e6);

        vm.expectEmit(address(escrow));
        emit PaymentRequested(1, sender, friend, 20e6, "dinner");
        vm.prank(sender);
        uint256 id = escrow.request(friend, 20e6, "dinner");

        vm.prank(attacker);
        vm.expectRevert(Escrow.NotPayer.selector);
        escrow.pay(id);

        vm.expectEmit(address(escrow));
        emit PaymentPaid(id, sender, friend, 20e6);
        vm.prank(friend);
        escrow.pay(id);
        assertEq(usdc.balanceOf(sender), 1_020e6);
        assertEq(usdc.balanceOf(friend), 30e6);

        vm.prank(friend);
        vm.expectRevert(Escrow.AlreadyPaid.selector);
        escrow.pay(id);
    }

    function test_openRequestCanBePaidByAnyone() public {
        usdc.mint(anyone, 5e6);
        vm.prank(anyone);
        usdc.approve(address(escrow), 5e6);
        vm.prank(sender);
        uint256 id = escrow.request(address(0), 5e6, "tip jar");
        vm.prank(anyone);
        escrow.pay(id);
        assertEq(usdc.balanceOf(sender), 1_005e6);
    }

    function test_requestRejectsZeroAndUnknown() public {
        vm.expectRevert(Escrow.ZeroAmount.selector);
        escrow.request(friend, 0, "x");
        vm.expectRevert(Escrow.UnknownRequest.selector);
        escrow.pay(1);
    }
}
