// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Ledger} from "../src/Ledger.sol";

contract LedgerTest is Test {
    event Committed(uint256 id, address committer, bytes32 commitment, string label, uint64 timestamp);
    event Revealed(uint256 id, bytes preimage);
    event WriterSet(address writer, bool allowed);

    Ledger ledger;
    address owner = makeAddr("owner");
    address writer = makeAddr("writer");
    address stranger = makeAddr("stranger");

    function setUp() public {
        vm.warp(1_000_000);
        ledger = new Ledger(owner);
        vm.prank(owner);
        ledger.setWriter(writer, true);
    }

    function test_commitAndReveal() public {
        bytes memory pre = bytes("forecast: yes, 0.62");
        vm.expectEmit(address(ledger));
        emit Committed(1, writer, keccak256(pre), "forecast:x", uint64(block.timestamp));
        vm.prank(writer);
        uint256 id = ledger.commit(keccak256(pre), "forecast:x");
        assertEq(id, 1);

        vm.expectEmit(address(ledger));
        emit Revealed(1, pre);
        vm.prank(writer);
        ledger.reveal(id, pre);
        assertTrue(ledger.getEntry(id).revealed);
    }

    function test_revealAcceptsSha256Commitment() public {
        bytes memory pre = bytes("{\"salt\":\"ab\"}");
        vm.startPrank(writer);
        uint256 id = ledger.commit(sha256(pre), "forecast:y");
        ledger.reveal(id, pre);
        vm.stopPrank();
        assertTrue(ledger.getEntry(id).revealed);
    }

    function test_wrongPreimageReverts() public {
        vm.startPrank(writer);
        uint256 id = ledger.commit(keccak256("right"), "t");
        vm.expectRevert(Ledger.PreimageMismatch.selector);
        ledger.reveal(id, bytes("wrong"));
        vm.stopPrank();
        assertFalse(ledger.getEntry(id).revealed);
    }

    function test_doubleRevealReverts() public {
        vm.startPrank(writer);
        uint256 id = ledger.commit(keccak256("a"), "t");
        ledger.reveal(id, bytes("a"));
        vm.expectRevert(Ledger.AlreadyRevealed.selector);
        ledger.reveal(id, bytes("a"));
        vm.stopPrank();
    }

    function test_unknownIdReverts() public {
        vm.startPrank(writer);
        vm.expectRevert(Ledger.UnknownCommitment.selector);
        ledger.reveal(0, bytes("a"));
        vm.expectRevert(Ledger.UnknownCommitment.selector);
        ledger.reveal(1, bytes("a"));
        vm.stopPrank();
    }

    function test_nonWriterCannotCommitOrReveal() public {
        vm.prank(stranger);
        vm.expectRevert(Ledger.NotWriter.selector);
        ledger.commit(keccak256("a"), "t");

        vm.prank(writer);
        uint256 id = ledger.commit(keccak256("a"), "t");
        vm.prank(stranger);
        vm.expectRevert(Ledger.NotWriter.selector);
        ledger.reveal(id, bytes("a"));
    }

    function test_onlyOwnerManagesWriters() public {
        vm.prank(stranger);
        vm.expectRevert(Ledger.NotOwner.selector);
        ledger.setWriter(stranger, true);

        vm.expectEmit(address(ledger));
        emit WriterSet(writer, false);
        vm.prank(owner);
        ledger.setWriter(writer, false);
        vm.prank(writer);
        vm.expectRevert(Ledger.NotWriter.selector);
        ledger.commit(keccak256("a"), "t");
    }

    function test_idsIncrease() public {
        vm.startPrank(writer);
        assertEq(ledger.commit(keccak256("a"), "t"), 1);
        assertEq(ledger.commit(keccak256("b"), "t"), 2);
        vm.stopPrank();
        assertEq(ledger.count(), 2);
    }
}
