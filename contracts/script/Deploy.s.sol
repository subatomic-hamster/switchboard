// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Vault} from "../src/Vault.sol";

/// Env: USDC (token address), OWNER, APPROVER. Run with --rpc-url and a signer.
contract Deploy is Script {
    function run() external returns (Vault vault) {
        address usdc = vm.envAddress("USDC");
        address owner = vm.envAddress("OWNER");
        address approver = vm.envAddress("APPROVER");
        vm.startBroadcast();
        vault = new Vault(IERC20(usdc), owner, approver);
        vm.stopBroadcast();
        console.log("Vault deployed at", address(vault));
    }
}
