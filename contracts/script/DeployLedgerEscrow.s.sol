// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Ledger} from "../src/Ledger.sol";
import {Escrow} from "../src/Escrow.sol";

/// Env: USDC (token address), OWNER (Ledger owner), optional LEDGER_WRITER (the agent address, set
/// as a Ledger writer; the broadcaster must then be OWNER). Run with --rpc-url and a signer.
contract DeployLedgerEscrow is Script {
    function run() external returns (Ledger ledger, Escrow escrow) {
        address usdc = vm.envAddress("USDC");
        address owner = vm.envAddress("OWNER");
        address writer = vm.envOr("LEDGER_WRITER", address(0));
        vm.startBroadcast();
        ledger = new Ledger(owner);
        escrow = new Escrow(IERC20(usdc));
        if (writer != address(0)) ledger.setWriter(writer, true);
        vm.stopBroadcast();
        console.log("Ledger deployed at", address(ledger));
        console.log("Escrow deployed at", address(escrow));
    }
}
