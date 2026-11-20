"""The real anchor: `AnchorPort` over `contracts/src/Ledger.sol` (web3.py).

`commit` records a 32-byte hash and returns `ledger:<id>:<tx hash>`. `reveal` takes that same
reference and publishes the preimage. The agent key must be a Ledger writer (the owner runs
`setWriter(agent, true)` once after deploying).

The Ledger accepts a commitment that is either keccak256 or sha256 of the preimage, which is how
the forecast module hashes its opinions (SHA-256 over canonical JSON).

No bus events here: the Stream webhook (`app/chain/stream.py`) publishes from the Ledger's logs.
"""

from __future__ import annotations

import asyncio
import logging

from app.chain.contract import Client
from app.chain.vault import _hex
from app.config import Settings

log = logging.getLogger("switchboard.chain")

REF_PREFIX = "ledger:"


class ChainAnchor:
    """`AnchorPort` on a real `Ledger.sol`."""

    def __init__(self, settings: Settings) -> None:
        self._client = Client(settings, "Ledger", settings.ledger_address)

    async def commit(self, commitment_hex: str, label: str) -> str:
        return await asyncio.to_thread(self._commit, commitment_hex, label)

    def _commit(self, commitment_hex: str, label: str) -> str:
        commitment = bytes.fromhex(commitment_hex.removeprefix("0x"))
        if len(commitment) != 32:
            raise ValueError("a commitment is 32 bytes")
        _, contract = self._client.web3()
        receipt = self._client.send(contract.functions.commit(commitment, label))
        for event in self._client.events(receipt):
            if event["event"] == "Committed":
                return f"{REF_PREFIX}{event['id']}:{_hex(receipt['transactionHash'])}"
        raise RuntimeError("commit produced no Committed event")

    async def reveal(self, anchor_ref: str, preimage: bytes) -> None:
        """Publish the preimage for a reference `commit` returned. Other references (a forecast
        committed while the stub anchor was in use) are ignored."""
        if not anchor_ref.startswith(REF_PREFIX):
            return
        await asyncio.to_thread(self._reveal, int(anchor_ref.split(":")[1]), preimage)

    def _reveal(self, ledger_id: int, preimage: bytes) -> None:
        _, contract = self._client.web3()
        self._client.send(contract.functions.reveal(ledger_id, preimage))
