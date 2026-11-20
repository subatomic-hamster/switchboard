"""Shared web3 plumbing for the Ledger and Escrow clients (`anchor.py`, `escrow.py`).

Same patterns as `vault.py`: nothing needs the environment or the network at import time, the
web3 client is built on first use, web3.py is synchronous so callers wrap these in
`asyncio.to_thread`, and every transaction goes through one process-wide lock so the agent
key's nonces never clash (`TX_LOCK` is shared with `ChainVault`).
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from eth_abi.abi import decode
from web3 import Web3
from web3.exceptions import ContractLogicError, Web3RPCError

from app.chain.vault import TX_LOCK, _hex
from app.config import REPO_ROOT, Settings

OUT = REPO_ROOT / "contracts" / "out"


class ContractRevert(ValueError):
    """A transaction reverted. `error` is the contract's custom error name, if known."""

    def __init__(self, contract: str, error: str | None, detail: str = "") -> None:
        self.error = error
        super().__init__(f"{contract} reverted: {error or 'unknown error'} {detail}".strip())


class Artifact:
    """A compiled contract's ABI with event and custom-error lookup tables."""

    def __init__(self, name: str) -> None:
        self.name = name
        path: Path = OUT / f"{name}.sol" / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"{path} not found: run `forge build` in contracts/ first")
        self.abi: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["abi"]
        self.events: dict[str, tuple[str, list[str], list[str]]] = {}
        self.errors: dict[str, str] = {}
        for item in self.abi:
            if item["type"] not in ("event", "error"):
                continue
            sig = f"{item['name']}({','.join(i['type'] for i in item['inputs'])})"
            if item["type"] == "event":
                self.events[_hex(Web3.keccak(text=sig))] = (
                    item["name"],
                    [i["type"] for i in item["inputs"]],
                    [i["name"] for i in item["inputs"]],
                )
            else:
                self.errors[_hex(Web3.keccak(text=sig)[:4])] = item["name"]

    def decode(self, log_entry: dict[str, Any]) -> dict[str, Any] | None:
        """A raw log -> `{"event": name, **fields}`, or None if it is not one of this ABI's."""
        topics = log_entry.get("topics") or []
        entry = self.events.get(_hex(topics[0])) if topics else None
        if entry is None:
            return None
        name, types, fields = entry
        values = decode(types, bytes.fromhex(_hex(log_entry.get("data") or "0x")[2:]))
        out: dict[str, Any] = {"event": name}
        for field, type_, value in zip(fields, types, values, strict=True):
            out[field] = Web3.to_checksum_address(value) if type_ == "address" else value
        return out

    def revert_name(self, exc: Exception) -> str | None:
        for selector in re.findall(r"0x[0-9a-fA-F]{8}", f"{getattr(exc, 'data', '')} {exc}"):
            if selector.lower() in self.errors:
                return self.errors[selector.lower()]
        return None


@cache
def load_artifact(name: str) -> Artifact:
    return Artifact(name)


class Client:
    """One deployed contract, called with the agent key."""

    def __init__(self, settings: Settings, name: str, address: str) -> None:
        self.settings = settings
        self.name = name
        self.address = address
        self._web3: tuple[Any, Any] | None = None

    @property
    def artifact(self) -> Artifact:
        return load_artifact(self.name)

    def web3(self) -> tuple[Any, Any]:
        """(web3 client, contract), built on first use."""
        if self._web3 is None:
            s = self.settings
            w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 30}))
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(self.address), abi=self.artifact.abi
            )
            self._web3 = (w3, contract)
        return self._web3

    def agent_address(self) -> str:
        w3, _ = self.web3()
        return str(w3.eth.account.from_key(self._key()).address)

    def _key(self) -> str:
        if not self.settings.agent_private_key:
            raise RuntimeError("AGENT_PRIVATE_KEY is not set")
        return self.settings.agent_private_key

    def send(self, fn: Any, gas: int | None = None) -> dict[str, Any]:
        """Sign and send one contract call as the agent; return the receipt.

        `gas` skips estimation. Needed when the call depends on an earlier block than the one the
        node simulates in (Escrow.claim needs a commitment from a previous block, and a node
        estimates against the latest block, which is the commit's own).
        """
        w3, _ = self.web3()
        account = w3.eth.account.from_key(self._key())
        with TX_LOCK:
            try:
                # build_transaction runs eth_estimateGas, which is where a revert shows up.
                tx = fn.build_transaction(
                    {
                        "from": account.address,
                        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
                        "chainId": self.settings.arc_chain_id,
                        **({"gas": gas} if gas else {}),
                    }
                )
            except (ContractLogicError, Web3RPCError) as exc:
                name = self.artifact.revert_name(exc)
                # A known custom error says it all; the raw node message is only useful without one.
                raise ContractRevert(self.name, name, "" if name else str(exc)) from exc
            tx_hash = w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)
            receipt: dict[str, Any] = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt["status"] != 1:
            raise ContractRevert(self.name, None, f"transaction {_hex(tx_hash)} failed")
        return receipt

    def events(self, receipt: dict[str, Any]) -> list[dict[str, Any]]:
        """This contract's events in a receipt, decoded."""
        out = []
        for entry in receipt["logs"]:
            if str(entry["address"]).lower() != self.address.lower():
                continue
            decoded = self.artifact.decode({"topics": entry["topics"], "data": entry["data"]})
            if decoded is not None:
                out.append(decoded)
        return out
