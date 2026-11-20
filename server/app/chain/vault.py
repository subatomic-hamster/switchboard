"""The real vault: `VaultPort` over `contracts/src/Vault.sol` (web3.py).

Importing this module needs no environment and no network. The web3 client is built on first
use. web3.py is synchronous, so every call runs in a worker thread.

`ChainVault` publishes no vault events. On the real chain the QuickNode Stream webhook
(`app/chain/stream.py`) publishes `approval.requested`, `approval.resolved` and `frozen` from the
logs; publishing them here as well would show every one twice. The one exception is
`policy.updated`: the contract has no event for a policy change, so nothing else would say it.

Contract ids are two separate counters: request ids and permit ids both start at 1. The port only
ever uses a request id with `approve`/`deny`/`expire` and a permit id with `redeem`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from decimal import Decimal
from functools import cache
from typing import Any

from eth_abi.abi import decode
from web3 import Web3
from web3.exceptions import ContractLogicError, Web3RPCError

from app.config import REPO_ROOT, Settings
from app.events import EventBus
from app.ports import SpendResult
from app.schemas import Policy, PolicyData, Reason

log = logging.getLogger("switchboard.chain")

ARTIFACT = REPO_ROOT / "contracts" / "out" / "Vault.sol" / "Vault.json"
USDC_DECIMALS = 6
EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Shared with the Ledger and Escrow clients (`contract.py`): they sign with the same agent key.
TX_LOCK = threading.Lock()

# The contract's reason codes (Vault.sol: OVER_TRANSFER_CAP .. FROZEN).
REASONS: dict[int, Reason] = {
    1: Reason.OVER_TRANSFER_CAP,
    2: Reason.OVER_DAILY_BUDGET,
    3: Reason.PAYEE_NOT_ALLOWED,
    4: Reason.FROZEN,
}


class VaultRevert(ValueError):
    """A vault transaction reverted. `error` is the contract's custom error name, if known.

    A `ValueError`, like the stub's refusals, so P2's `except ValueError` paths (a request that
    was already resolved, an expiry that came early) work unchanged.
    """

    def __init__(self, error: str | None, detail: str = "") -> None:
        self.error = error
        super().__init__(f"vault reverted: {error or 'unknown error'} {detail}".strip())


def is_evm(address: str) -> bool:
    return bool(EVM_ADDRESS.match(address))


def to_units(amount_usd: Decimal) -> int:
    scaled = amount_usd * 10**USDC_DECIMALS
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{amount_usd} has more than {USDC_DECIMALS} decimal places")
    return int(scaled)


def from_units(units: int) -> Decimal:
    return Decimal(int(units)) / Decimal(10**USDC_DECIMALS)


@cache
def _abi() -> list[dict[str, Any]]:
    if not ARTIFACT.exists():
        raise FileNotFoundError(f"{ARTIFACT} not found: run `forge build` in contracts/ first")
    abi: list[dict[str, Any]] = json.loads(ARTIFACT.read_text(encoding="utf-8"))["abi"]
    return abi


def _signature(item: dict[str, Any]) -> str:
    return f"{item['name']}({','.join(i['type'] for i in item['inputs'])})"


@cache
def _tables() -> tuple[dict[str, tuple[str, list[str], list[str]]], dict[str, str]]:
    """topic0 -> (event name, types, field names); 4-byte selector -> custom error name."""
    events: dict[str, tuple[str, list[str], list[str]]] = {}
    errors: dict[str, str] = {}
    for item in _abi():
        if item["type"] == "event":
            topic = _hex(Web3.keccak(text=_signature(item)))
            events[topic] = (
                item["name"],
                [i["type"] for i in item["inputs"]],
                [i["name"] for i in item["inputs"]],
            )
        elif item["type"] == "error":
            errors[_hex(Web3.keccak(text=_signature(item))[:4])] = item["name"]
    return events, errors


def _hex(value: object) -> str:
    if isinstance(value, bytes | bytearray):
        return "0x" + bytes(value).hex()
    text = str(value).lower()
    return text if text.startswith("0x") else "0x" + text


def decode_vault_log(log_entry: dict[str, Any]) -> dict[str, Any] | None:
    """A raw log (`topics` and `data` as hex) -> `{"event": name, **fields}`, or None if the log
    is not one of the vault's events."""
    events, _ = _tables()
    topics = log_entry.get("topics") or []
    entry = events.get(_hex(topics[0])) if topics else None
    if entry is None:
        return None
    name, types, fields = entry
    values = decode(types, bytes.fromhex(_hex(log_entry.get("data") or "0x")[2:]))
    out: dict[str, Any] = {"event": name}
    for field, type_, value in zip(fields, types, values, strict=True):
        out[field] = Web3.to_checksum_address(value) if type_ == "address" else value
    return out


def _revert_name(exc: Exception) -> str | None:
    _, errors = _tables()
    for selector in re.findall(r"0x[0-9a-fA-F]{8}", f"{getattr(exc, 'data', '')} {exc}"):
        if selector.lower() in errors:
            return errors[selector.lower()]
    return None


class ChainVault:
    """`VaultPort` on a real `Vault.sol`. Needs the vault address, RPC URL and the three keys."""

    def __init__(
        self, settings: Settings, wallet_allowlist: list[str], bus: EventBus | None = None
    ) -> None:
        self._settings = settings
        self._bus = bus
        # Permits that `approve` already paid (the contract pays in the approving transaction),
        # so the planner's `redeem` of them is a no-op.
        self._prepaid: set[str] = set()
        self._tx_lock = TX_LOCK  # one transaction at a time, so nonces never clash
        # The allowlist as the rest of P2 sees it. Starts from the wallet book. EVM entries are
        # looked up on chain each time (the contract is the authority); entries that are not EVM
        # addresses are kept here only.
        self._evm = [a for a in wallet_allowlist if is_evm(a)]
        self._other = [a for a in wallet_allowlist if not is_evm(a)]
        self._client: Any = None

    # ------------------------------------------------------------------ plumbing

    def _web3(self) -> Any:
        """The web3 client and contract, built on first use."""
        if self._client is None:
            s = self._settings
            w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 30}))
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(s.vault_address), abi=_abi()
            )
            self._client = (w3, contract)
        return self._client

    def _key(self, role: str) -> str:
        key: str = getattr(self._settings, f"{role}_private_key")
        if not key:
            raise RuntimeError(f"{role.upper()}_PRIVATE_KEY is not set")
        return key

    def _address(self, role: str) -> str:
        w3, _ = self._web3()
        return str(w3.eth.account.from_key(self._key(role)).address)

    def _send(self, role: str, fn: Any) -> dict[str, Any]:
        """Sign and send one contract call as `role`; return the receipt."""
        w3, _ = self._web3()
        account = w3.eth.account.from_key(self._key(role))
        with self._tx_lock:
            try:
                # build_transaction runs eth_estimateGas, which is where a revert shows up.
                tx = fn.build_transaction(
                    {
                        "from": account.address,
                        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
                        "chainId": self._settings.arc_chain_id,
                    }
                )
            except (ContractLogicError, Web3RPCError) as exc:
                raise VaultRevert(_revert_name(exc), str(exc)) from exc
            tx_hash = w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)
            receipt: dict[str, Any] = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt["status"] != 1:
            raise VaultRevert(None, f"transaction {_hex(tx_hash)} failed")
        return receipt

    def _events(self, receipt: dict[str, Any]) -> list[dict[str, Any]]:
        _, contract = self._web3()
        out = []
        for entry in receipt["logs"]:
            if str(entry["address"]).lower() != contract.address.lower():
                continue
            decoded = decode_vault_log({"topics": entry["topics"], "data": entry["data"]})
            if decoded is not None:
                out.append(decoded)
        return out

    # ------------------------------------------------------------------ VaultPort: agent

    async def request_spend(self, payee: str, amount_usd: Decimal, purpose: str) -> SpendResult:
        return await asyncio.to_thread(self._request_spend, payee, amount_usd, purpose)

    def _request_spend(self, payee: str, amount_usd: Decimal, purpose: str) -> SpendResult:
        _, contract = self._web3()
        if not is_evm(payee):
            # ponytail: a payee on another chain (Solana, say) cannot be a vault payee, so the
            # vault pays the agent's own address, which funds the cross-chain leg. The per-payee
            # allowlist is not enforced onchain for non-EVM payees until the Phase 2 transfer
            # module exists.
            payee = self._address("agent")
        fn = contract.functions.requestSpend(
            Web3.to_checksum_address(payee), to_units(amount_usd), Web3.keccak(text=purpose)
        )
        for event in self._events(self._send("agent", fn)):
            if event["event"] == "PermitIssued":
                return SpendResult(permit_id=str(event["permitId"]))
            if event["event"] == "ApprovalRequested":
                reason = REASONS[event["reason"]]
                if reason is Reason.FROZEN:
                    # Nothing to ask the owner about: the planner treats it as blocked. The
                    # request stays open onchain until it expires.
                    return SpendResult(reason=reason)
                return SpendResult(request_id=str(event["requestId"]), reason=reason)
        raise RuntimeError("requestSpend produced neither PermitIssued nor ApprovalRequested")

    async def redeem(self, permit_id: str) -> None:
        if permit_id in self._prepaid:
            self._prepaid.discard(permit_id)
            return
        _, contract = self._web3()
        await asyncio.to_thread(self._send, "agent", contract.functions.redeem(int(permit_id)))

    # ------------------------------------------------------------------ VaultPort: approver

    async def approve(self, request_id: str) -> str:
        return await asyncio.to_thread(self._approve, request_id)

    def _approve(self, request_id: str) -> str:
        _, contract = self._web3()
        receipt = self._send("approver", contract.functions.approve(int(request_id)))
        for event in self._events(receipt):
            if event["event"] == "Spent":
                permit_id = str(event["permitId"])
                self._prepaid.add(permit_id)  # the contract already paid
                return permit_id
        raise RuntimeError("approve produced no Spent event")

    async def deny(self, request_id: str, via: str = "") -> None:
        _, contract = self._web3()
        await asyncio.to_thread(self._send, "approver", contract.functions.deny(int(request_id)))

    async def expire(self, request_id: str) -> None:
        _, contract = self._web3()
        await asyncio.to_thread(self._send, "approver", contract.functions.expire(int(request_id)))

    async def freeze(self, frozen: bool, via: str = "") -> None:
        _, contract = self._web3()
        if frozen:
            await asyncio.to_thread(self._send, "approver", contract.functions.freeze())
        else:
            await asyncio.to_thread(self._send, "owner", contract.functions.unfreeze())

    # ------------------------------------------------------------------ VaultPort: owner, reads

    async def set_policy(self, policy: Policy, via: str = "", rule_text: str = "") -> None:
        before = await self.get_policy()
        await asyncio.to_thread(self._set_policy, policy, before)
        wanted = {a.lower() for a in policy.allowlist}
        self._other = [a for a in policy.allowlist if not is_evm(a)]
        self._evm = [*{a.lower(): a for a in [*self._evm, *policy.allowlist] if is_evm(a)}.values()]
        if self._bus is not None:
            await self._bus.publish(
                "policy.updated",
                PolicyData(
                    transfer_cap_usd=policy.transfer_cap_usd,
                    daily_budget_usd=policy.daily_budget_usd,
                    allowlist_size=len(wanted),
                    via=via,
                    rule_text=rule_text,
                ),
            )

    def _set_policy(self, policy: Policy, before: Policy) -> None:
        _, contract = self._web3()
        agent = self._address("agent")
        limits = (
            policy.transfer_cap_usd,
            policy.daily_budget_usd,
            policy.approval_expiry_s,
        )
        if limits != (before.transfer_cap_usd, before.daily_budget_usd, before.approval_expiry_s):
            self._send(
                "owner",
                contract.functions.setPolicy(
                    agent,
                    to_units(policy.transfer_cap_usd),
                    to_units(policy.daily_budget_usd),
                    policy.approval_expiry_s,
                ),
            )
        old = {a.lower() for a in before.allowlist if is_evm(a)}
        new = {a.lower(): a for a in policy.allowlist if is_evm(a)}
        for lowered, address in new.items():
            if lowered not in old:
                self._allow(contract, agent, address, True)
        for address in before.allowlist:
            if is_evm(address) and address.lower() not in new:
                self._allow(contract, agent, address, False)

    def _allow(self, contract: Any, agent: str, payee: str, allowed: bool) -> None:
        fn = contract.functions.setPayeeAllowed(agent, Web3.to_checksum_address(payee), allowed)
        self._send("owner", fn)

    async def get_policy(self) -> Policy:
        return await asyncio.to_thread(self._get_policy)

    def _get_policy(self) -> Policy:
        _, contract = self._web3()
        agent = self._address("agent")
        cap, budget, ttl, _active = contract.functions.policies(agent).call()
        allowed = [
            a
            for a in self._evm
            if contract.functions.payeeAllowed(agent, Web3.to_checksum_address(a)).call()
        ]
        return Policy(
            transfer_cap_usd=from_units(cap),
            daily_budget_usd=from_units(budget),
            allowlist=[*allowed, *self._other],
            approval_expiry_s=ttl,
        )

    async def spent_today(self) -> Decimal:
        _, contract = self._web3()
        units = await asyncio.to_thread(
            contract.functions.spentInWindow(self._address("agent")).call
        )
        return from_units(units)

    async def is_frozen(self) -> bool:
        _, contract = self._web3()
        return bool(await asyncio.to_thread(contract.functions.frozen().call))
