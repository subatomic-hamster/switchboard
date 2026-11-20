// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

/// @notice Test double for CCTP V2's TokenMessengerV2 burn entry points. Pulls the USDC and logs
///         every argument, so the server's burn call can be checked against a local chain.
///         The two signatures match Circle's (developers.circle.com/cctp/references/contract-interfaces).
contract MockTokenMessenger {
    event Burn(
        uint256 amount,
        uint32 destinationDomain,
        bytes32 mintRecipient,
        address burnToken,
        bytes32 destinationCaller,
        uint256 maxFee,
        uint32 minFinalityThreshold,
        bytes hookData
    );

    function depositForBurn(
        uint256 amount,
        uint32 destinationDomain,
        bytes32 mintRecipient,
        address burnToken,
        bytes32 destinationCaller,
        uint256 maxFee,
        uint32 minFinalityThreshold
    ) external {
        _burn(amount, destinationDomain, mintRecipient, burnToken, destinationCaller, maxFee, minFinalityThreshold, "");
    }

    function depositForBurnWithHook(
        uint256 amount,
        uint32 destinationDomain,
        bytes32 mintRecipient,
        address burnToken,
        bytes32 destinationCaller,
        uint256 maxFee,
        uint32 minFinalityThreshold,
        bytes calldata hookData
    ) external {
        _burn(
            amount, destinationDomain, mintRecipient, burnToken, destinationCaller, maxFee, minFinalityThreshold, hookData
        );
    }

    function _burn(
        uint256 amount,
        uint32 destinationDomain,
        bytes32 mintRecipient,
        address burnToken,
        bytes32 destinationCaller,
        uint256 maxFee,
        uint32 minFinalityThreshold,
        bytes memory hookData
    ) private {
        require(IERC20(burnToken).transferFrom(msg.sender, address(this), amount), "transferFrom failed");
        emit Burn(
            amount, destinationDomain, mintRecipient, burnToken, destinationCaller, maxFee, minFinalityThreshold, hookData
        );
    }
}
