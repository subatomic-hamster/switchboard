// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title Switchboard Ledger
/// @notice Append-only commitment log for forecast receipts and trade records. A writer commits a
///         32-byte hash before the event and reveals the preimage after it.
/// @dev Events carry no `indexed` fields: the server decodes them from the log data.
///      A commitment may be either keccak256(preimage) or sha256(preimage). The server's forecast
///      receipts are SHA-256 over canonical JSON, so both are accepted.
contract Ledger {
    event Committed(uint256 id, address committer, bytes32 commitment, string label, uint64 timestamp);
    event Revealed(uint256 id, bytes preimage);
    event WriterSet(address writer, bool allowed);

    error NotOwner();
    error NotWriter();
    error UnknownCommitment();
    error AlreadyRevealed();
    error PreimageMismatch();
    error ZeroAddress();

    struct Entry {
        address committer;
        uint64 timestamp;
        bool revealed;
        bytes32 commitment;
    }

    address public immutable owner;
    mapping(address => bool) public isWriter;

    Entry[] private _entries; // id = index + 1

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyWriter() {
        if (!isWriter[msg.sender]) revert NotWriter();
        _;
    }

    constructor(address owner_) {
        if (owner_ == address(0)) revert ZeroAddress();
        owner = owner_;
    }

    /// @notice Owner allows or disallows a writer.
    function setWriter(address writer, bool allowed) external onlyOwner {
        isWriter[writer] = allowed;
        emit WriterSet(writer, allowed);
    }

    /// @notice Record a commitment. Ids start at 1.
    function commit(bytes32 commitment, string calldata label) external onlyWriter returns (uint256 id) {
        _entries.push(Entry(msg.sender, uint64(block.timestamp), false, commitment));
        id = _entries.length;
        emit Committed(id, msg.sender, commitment, label, uint64(block.timestamp));
    }

    /// @notice Reveal the preimage of commitment `id`, once.
    function reveal(uint256 id, bytes calldata preimage) external onlyWriter {
        if (id == 0 || id > _entries.length) revert UnknownCommitment();
        Entry storage e = _entries[id - 1];
        if (e.revealed) revert AlreadyRevealed();
        if (keccak256(preimage) != e.commitment && sha256(preimage) != e.commitment) {
            revert PreimageMismatch();
        }
        e.revealed = true;
        emit Revealed(id, preimage);
    }

    /// @notice Read an entry (id starts at 1).
    function getEntry(uint256 id) external view returns (Entry memory) {
        if (id == 0 || id > _entries.length) revert UnknownCommitment();
        return _entries[id - 1];
    }

    function count() external view returns (uint256) {
        return _entries.length;
    }
}
