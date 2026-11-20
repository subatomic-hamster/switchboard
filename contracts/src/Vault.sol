// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/// @title Switchboard Vault
/// @notice Holds USDC (6 decimals) for an owner. An agent key can only request spends;
///         the vault enforces the owner's policy. Over-policy requests wait for owner/approver.
/// @dev All amounts are USDC base units (6 decimals). Never mixes with 18-decimal native gas.
contract Vault {
    using SafeERC20 for IERC20;

    // ---- Frozen events (no `indexed`, decoded off-chain) ----
    event Spent(uint256 permitId, address agent, address payee, uint256 amount);
    event ApprovalRequested(uint256 requestId, address agent, address payee, uint256 amount, uint8 reason);
    event Resolved(uint256 requestId, bool approved);
    event Frozen(bool frozen);
    event PermitIssued(uint256 permitId, address agent, address payee, uint256 amount, uint64 expiresAt);

    // reasons
    uint8 public constant OVER_TRANSFER_CAP = 1;
    uint8 public constant OVER_DAILY_BUDGET = 2;
    uint8 public constant PAYEE_NOT_ALLOWED = 3;
    uint8 public constant FROZEN = 4;

    uint64 public constant PERMIT_TTL = 10 minutes;
    uint64 public constant WINDOW = 24 hours;

    error NotOwner();
    error NotOwnerOrApprover();
    error NotAgent();
    error AgentNotActive();
    error ZeroAmount();
    error AmountTooLarge();
    error IsFrozen();
    error NotPermitAgent();
    error PermitUsedOrUnknown();
    error PermitExpired();
    error NotPending();
    error DeadlinePassed();
    error DeadlineNotPassed();
    error InsufficientVaultBalance();
    error ZeroAddress();

    struct Policy {
        uint128 perTransferCap;
        uint128 dailyBudget;
        uint64 approvalTtl;
        bool active;
    }

    struct Permit {
        address agent;
        address payee;
        uint128 amount;
        uint64 expiresAt;
        bool used;
    }

    enum Status {
        None,
        Pending,
        Approved,
        Denied
    }

    struct Request {
        address agent;
        address payee;
        uint128 amount;
        uint64 deadline;
        Status status;
        bytes32 purposeHash;
        uint8 reason;
    }

    struct Entry {
        uint64 ts;
        uint128 amount;
    }

    IERC20 public immutable usdc;
    address public immutable owner;
    address public approver;
    bool public frozen;

    mapping(address => Policy) public policies;
    mapping(address => mapping(address => bool)) public payeeAllowed;

    Permit[] private _permits; // id = index + 1
    Request[] private _requests; // id = index + 1

    mapping(address => Entry[]) private _entries;
    mapping(address => uint256) private _head;

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyOwnerOrApprover() {
        if (msg.sender != owner && msg.sender != approver) revert NotOwnerOrApprover();
        _;
    }

    constructor(IERC20 usdc_, address owner_, address approver_) {
        if (address(usdc_) == address(0) || owner_ == address(0)) revert ZeroAddress();
        usdc = usdc_;
        owner = owner_;
        approver = approver_;
    }

    // ------------------------------------------------------------ owner admin

    /// @notice Owner sets the approver key.
    function setApprover(address approver_) external onlyOwner {
        approver = approver_;
    }

    /// @notice Set an agent's policy and mark it active.
    function setPolicy(address agent, uint128 perTransferCap, uint128 dailyBudget, uint64 approvalTtl)
        external
        onlyOwner
    {
        policies[agent] = Policy(perTransferCap, dailyBudget, approvalTtl, true);
    }

    /// @notice Allow or disallow a payee for an agent.
    function setPayeeAllowed(address agent, address payee, bool allowed) external onlyOwner {
        payeeAllowed[agent][payee] = allowed;
    }

    /// @notice Revoke an agent; its outstanding permits stop being redeemable.
    function revokeAgent(address agent) external onlyOwner {
        policies[agent].active = false;
    }

    /// @notice Freeze all spending (owner or approver).
    function freeze() external onlyOwnerOrApprover {
        frozen = true;
        emit Frozen(true);
    }

    /// @notice Unfreeze (owner only).
    function unfreeze() external onlyOwner {
        frozen = false;
        emit Frozen(false);
    }

    // ------------------------------------------------------------ agent flow

    /// @notice Agent asks to pay `payee`. Returns (id, true) with a permit id if within policy,
    ///         else (id, false) with a pending request id.
    function requestSpend(address payee, uint256 amount, bytes32 purposeHash)
        external
        returns (uint256 id, bool isPermit)
    {
        Policy memory p = policies[msg.sender];
        if (!p.active) revert AgentNotActive();
        if (amount == 0) revert ZeroAmount();
        if (amount > type(uint128).max) revert AmountTooLarge();

        (uint256 spent, uint256 head) = _spent(msg.sender);
        _head[msg.sender] = head;

        uint8 reason;
        if (frozen) reason = FROZEN;
        else if (!payeeAllowed[msg.sender][payee]) reason = PAYEE_NOT_ALLOWED;
        else if (amount > p.perTransferCap) reason = OVER_TRANSFER_CAP;
        else if (spent + amount > p.dailyBudget) reason = OVER_DAILY_BUDGET;

        if (reason == 0) {
            _entries[msg.sender].push(Entry(uint64(block.timestamp), uint128(amount)));
            id = _issue(msg.sender, payee, amount);
            emit PermitIssued(id, msg.sender, payee, amount, uint64(block.timestamp) + PERMIT_TTL);
            return (id, true);
        }

        _requests.push(
            Request(
                msg.sender,
                payee,
                uint128(amount),
                uint64(block.timestamp) + p.approvalTtl,
                Status.Pending,
                purposeHash,
                reason
            )
        );
        id = _requests.length;
        emit ApprovalRequested(id, msg.sender, payee, amount, reason);
    }

    /// @notice Agent redeems its permit: pays exactly the permitted amount to the payee, once.
    function redeem(uint256 permitId) external {
        if (frozen) revert IsFrozen();
        Permit storage pm = _permit(permitId);
        if (msg.sender != pm.agent) revert NotPermitAgent();
        if (!policies[pm.agent].active) revert AgentNotActive();
        if (pm.used) revert PermitUsedOrUnknown();
        if (block.timestamp > pm.expiresAt) revert PermitExpired();
        _pay(permitId, pm);
    }

    // ------------------------------------------------------------ approval flow

    /// @notice Approve a pending request and pay immediately.
    function approve(uint256 requestId) external onlyOwnerOrApprover {
        if (frozen) revert IsFrozen();
        Request storage r = _pending(requestId);
        if (block.timestamp > r.deadline) revert DeadlinePassed();
        if (!policies[r.agent].active) revert AgentNotActive();
        r.status = Status.Approved;
        // ponytail: approvals bypass the budget check by design but still count toward the window
        _entries[r.agent].push(Entry(uint64(block.timestamp), r.amount));
        uint256 pid = _issue(r.agent, r.payee, r.amount);
        _pay(pid, _permits[pid - 1]);
        emit Resolved(requestId, true);
    }

    /// @notice Deny a pending request.
    function deny(uint256 requestId) external onlyOwnerOrApprover {
        Request storage r = _pending(requestId);
        r.status = Status.Denied;
        emit Resolved(requestId, false);
    }

    /// @notice Anyone may expire a pending request after its deadline (resolved as denied).
    function expire(uint256 requestId) external {
        Request storage r = _pending(requestId);
        if (block.timestamp <= r.deadline) revert DeadlineNotPassed();
        r.status = Status.Denied;
        emit Resolved(requestId, false);
    }

    // ------------------------------------------------------------ views

    /// @notice Amount counted against the agent's rolling 24h budget right now.
    function spentInWindow(address agent) external view returns (uint256) {
        (uint256 total,) = _spent(agent);
        return total;
    }

    /// @notice Read a request (id starts at 1).
    function getRequest(uint256 id) external view returns (Request memory) {
        if (id == 0 || id > _requests.length) revert NotPending();
        return _requests[id - 1];
    }

    /// @notice Read a permit (id starts at 1).
    function getPermit(uint256 id) external view returns (Permit memory) {
        return _permit(id);
    }

    // ------------------------------------------------------------ internals

    function _issue(address agent, address payee, uint256 amount) internal returns (uint256 id) {
        _permits.push(Permit(agent, payee, uint128(amount), uint64(block.timestamp) + PERMIT_TTL, false));
        id = _permits.length;
    }

    function _pay(uint256 permitId, Permit storage pm) internal {
        pm.used = true; // effects before interaction
        address payee = pm.payee;
        uint256 amount = pm.amount;
        if (usdc.balanceOf(address(this)) < amount) revert InsufficientVaultBalance();
        usdc.safeTransfer(payee, amount);
        emit Spent(permitId, pm.agent, payee, amount);
    }

    function _permit(uint256 id) internal view returns (Permit storage) {
        if (id == 0 || id > _permits.length) revert PermitUsedOrUnknown();
        return _permits[id - 1];
    }

    function _pending(uint256 id) internal view returns (Request storage r) {
        if (id == 0 || id > _requests.length) revert NotPending();
        r = _requests[id - 1];
        if (r.status != Status.Pending) revert NotPending();
    }

    /// @dev Returns sum of live entries and the pruned head index (callers persist it).
    // ponytail: pruning/summing is O(entries in window); ~25k gas per expired entry pruned and
    // ~2.5k per live entry read, so keep it under a few hundred spends per agent per day.
    // Upgrade path: running total + bucketed (hourly) ring buffer.
    function _spent(address agent) internal view returns (uint256 total, uint256 h) {
        Entry[] storage es = _entries[agent];
        h = _head[agent];
        uint256 n = es.length;
        uint256 cutoff = block.timestamp > WINDOW ? block.timestamp - WINDOW : 0;
        while (h < n && es[h].ts <= cutoff) h++;
        for (uint256 i = h; i < n; i++) {
            total += es[i].amount;
        }
    }
}
