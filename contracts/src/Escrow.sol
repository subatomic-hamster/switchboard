// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/// @title Switchboard Escrow
/// @notice (a) Holds USDC (6 decimals) for a recipient who has no wallet yet, released by a secret
///         shared off-chain as a claim link, and refundable to the sender after expiry.
///         (b) Payment requests: someone asks an address (or anyone) to pay them an amount.
/// @dev Events carry no `indexed` fields: the server decodes them from the log data.
///
///      Front-running design (commit-reveal claim). `claimHash = keccak256(abi.encode(secret))`.
///      The secret is visible to everyone once the claiming transaction is in the mempool, so a
///      bot could copy it and pay itself. To prevent that, claiming takes two transactions:
///        1. `commitClaim(id, keccak256(abi.encode(id, secret, to)))` shows only a hash. It binds
///           the secret to the recipient `to` without revealing either.
///        2. `claim(id, secret, to)` in a LATER block. It pays `to` only if that exact
///           (id, secret, to) was committed in an earlier block.
///      A bot that copies the secret out of step 2 can commit its own address, but that commit
///      lands in the same block as, or after, the victim's claim, and it cannot claim until a
///      later block, by which time the deposit is settled. The residual risk is a block producer
///      that censors the victim's claim for two consecutive blocks.
///      Settlement is also bound to `to` by the commitment, so the secret alone never redirects.
contract Escrow {
    using SafeERC20 for IERC20;

    event Deposited(uint256 id, address depositor, bytes32 claimHash, uint256 amount, uint64 expiry, address refundTo);
    event ClaimCommitted(uint256 id, bytes32 commitment);
    event Claimed(uint256 id, address to, uint256 amount);
    event Refunded(uint256 id, address refundTo, uint256 amount);
    event PaymentRequested(uint256 id, address requester, address payer, uint256 amount, string memo);
    event PaymentPaid(uint256 id, address requester, address payer, uint256 amount);

    error ZeroAddress();
    error ZeroAmount();
    error AmountTooLarge();
    error ExpiryInPast();
    error UnknownDeposit();
    error AlreadySettled();
    error Expired();
    error NotExpired();
    error WrongSecret();
    error ClaimNotCommitted();
    error UnknownRequest();
    error AlreadyPaid();
    error NotPayer();

    struct Deposit {
        address refundTo;
        uint128 amount;
        uint64 expiry;
        bool settled;
        bytes32 claimHash;
    }

    struct PaymentRequest {
        address requester;
        address payer; // address(0): anyone may pay
        uint128 amount;
        bool paid;
    }

    IERC20 public immutable usdc;

    Deposit[] private _deposits; // id = index + 1
    PaymentRequest[] private _requests; // id = index + 1

    /// @notice Block number at which a claim commitment was recorded (0 = never).
    mapping(bytes32 => uint256) public commitBlock;

    constructor(IERC20 usdc_) {
        if (address(usdc_) == address(0)) revert ZeroAddress();
        usdc = usdc_;
    }

    // ------------------------------------------------------------ escrow

    /// @notice Pull `amount` USDC from the caller into escrow. Returns the deposit id (from 1).
    function deposit(bytes32 claimHash, uint256 amount, uint64 expiry, address refundTo) external returns (uint256 id) {
        if (amount == 0) revert ZeroAmount();
        if (amount > type(uint128).max) revert AmountTooLarge();
        if (expiry <= block.timestamp) revert ExpiryInPast();
        if (refundTo == address(0)) revert ZeroAddress();
        _deposits.push(Deposit(refundTo, uint128(amount), expiry, false, claimHash));
        id = _deposits.length;
        usdc.safeTransferFrom(msg.sender, address(this), amount);
        emit Deposited(id, msg.sender, claimHash, amount, expiry, refundTo);
    }

    /// @notice Step 1 of a claim: record a hash of (id, secret, to). Anyone may call.
    function commitClaim(uint256 id, bytes32 commitment) external {
        Deposit storage d = _open(id);
        if (block.timestamp >= d.expiry) revert Expired();
        if (commitBlock[commitment] == 0) commitBlock[commitment] = block.number;
        emit ClaimCommitted(id, commitment);
    }

    /// @notice Step 2 of a claim: reveal the secret and pay `to`. Needs a prior-block commitment
    ///         of exactly (id, secret, to). Only before expiry.
    function claim(uint256 id, bytes32 secret, address to) external {
        Deposit storage d = _open(id);
        if (block.timestamp >= d.expiry) revert Expired();
        if (keccak256(abi.encode(secret)) != d.claimHash) revert WrongSecret();
        if (to == address(0)) revert ZeroAddress();
        uint256 at = commitBlock[commitmentFor(id, secret, to)];
        if (at == 0 || at >= block.number) revert ClaimNotCommitted();
        d.settled = true; // effects before interaction
        usdc.safeTransfer(to, d.amount);
        emit Claimed(id, to, d.amount);
    }

    /// @notice Anyone may return an unclaimed deposit to `refundTo` once it has expired.
    function refund(uint256 id) external {
        Deposit storage d = _open(id);
        if (block.timestamp < d.expiry) revert NotExpired();
        d.settled = true;
        usdc.safeTransfer(d.refundTo, d.amount);
        emit Refunded(id, d.refundTo, d.amount);
    }

    /// @notice The commitment a claimer must pass to `commitClaim`.
    function commitmentFor(uint256 id, bytes32 secret, address to) public pure returns (bytes32) {
        return keccak256(abi.encode(id, secret, to));
    }

    /// @notice Read a deposit (id starts at 1).
    function getDeposit(uint256 id) external view returns (Deposit memory) {
        if (id == 0 || id > _deposits.length) revert UnknownDeposit();
        return _deposits[id - 1];
    }

    // ------------------------------------------------------------ payment requests

    /// @notice Ask `payer` (or anyone, if address(0)) to pay the caller `amount` USDC.
    function request(address payer, uint256 amount, string calldata memo) external returns (uint256 id) {
        if (amount == 0) revert ZeroAmount();
        if (amount > type(uint128).max) revert AmountTooLarge();
        _requests.push(PaymentRequest(msg.sender, payer, uint128(amount), false));
        id = _requests.length;
        emit PaymentRequested(id, msg.sender, payer, amount, memo);
    }

    /// @notice Pay request `id` from the caller to its requester, once.
    function pay(uint256 id) external {
        if (id == 0 || id > _requests.length) revert UnknownRequest();
        PaymentRequest storage r = _requests[id - 1];
        if (r.paid) revert AlreadyPaid();
        if (r.payer != address(0) && r.payer != msg.sender) revert NotPayer();
        r.paid = true;
        usdc.safeTransferFrom(msg.sender, r.requester, r.amount);
        emit PaymentPaid(id, r.requester, msg.sender, r.amount);
    }

    /// @notice Read a payment request (id starts at 1).
    function getRequest(uint256 id) external view returns (PaymentRequest memory) {
        if (id == 0 || id > _requests.length) revert UnknownRequest();
        return _requests[id - 1];
    }

    // ------------------------------------------------------------ internals

    function _open(uint256 id) internal view returns (Deposit storage d) {
        if (id == 0 || id > _deposits.length) revert UnknownDeposit();
        d = _deposits[id - 1];
        if (d.settled) revert AlreadySettled();
    }
}
