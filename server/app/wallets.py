"""The wallet book: whose wallets exist on which chain, and which are on the vault allowlist.

Loaded from `server/fixtures/wallets.json` (placeholder testnet addresses until P1 supplies the
real Circle wallets).
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import FIXTURES_DIR
from app.schemas import Chain, Wallet


class WalletBook:
    def __init__(self, path: Path | None = None) -> None:
        raw = json.loads((path or FIXTURES_DIR / "wallets.json").read_text(encoding="utf-8"))
        self.wallets: list[Wallet] = [Wallet(**item) for item in raw["wallets"]]

    def find(self, owner: str, chain: Chain) -> Wallet | None:
        owner = owner.lower()
        for wallet in self.wallets:
            if wallet.owner == owner and wallet.chain == chain:
                return wallet
        return None

    def owners(self) -> set[str]:
        return {wallet.owner for wallet in self.wallets}

    def by_address(self, address: str) -> Wallet | None:
        for wallet in self.wallets:
            if wallet.address.lower() == address.lower():
                return wallet
        return None

    def allowlist(self) -> list[str]:
        # One EVM address can back several wallets (Arc and Ethereum); list it once.
        return list(dict.fromkeys(wallet.address for wallet in self.wallets if wallet.allowlisted))

    def label_for(self, address: str) -> str:
        """A speakable name for an address: its wallet-book label, or its last four characters."""
        wallet = self.by_address(address)
        if wallet is not None:
            return wallet.label
        return f"new address ending {address[-4:]}"
