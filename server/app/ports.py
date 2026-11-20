"""The seams between P2's code and P1's.

P2 code depends only on these protocols. Today they are satisfied by the stubs in
`app/chain/stub.py` and `app/markets/stub.py`. P1's real modules replace the stubs by
providing objects with the same methods; nothing in P2's code changes.

These protocols are a proposal to P1, recorded in OVERNIGHT_LOG.md. The HTTP routes and
contract events in the plan stay exactly as frozen.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.schemas import (
    ChainTransferRequest,
    MarketListing,
    MarketMatchResponse,
    MarketMoveRequest,
    MarketQuoteResponse,
    Policy,
    Reason,
)


@dataclass(frozen=True)
class SpendResult:
    """What the vault says to `requestSpend`: a permit, or a pending request with a reason."""

    permit_id: str | None = None
    request_id: str | None = None
    reason: Reason | None = None

    @property
    def permitted(self) -> bool:
        return self.permit_id is not None


class VaultPort(Protocol):
    """`chain/vault.py`: the agent and approver operations on the vault contract."""

    async def request_spend(self, payee: str, amount_usd: Decimal, purpose: str) -> SpendResult:
        """Agent key. Returns a single-use permit if within policy, else a pending request.
        A pending request leads to an `approval.requested` event (from the Stream)."""
        ...

    async def redeem(self, permit_id: str) -> None:
        """Agent key. Spends exactly the permitted amount to the permitted payee, once."""
        ...

    async def approve(self, request_id: str) -> str:
        """Approver key. Resolves the request as approved and returns the permit id."""
        ...

    async def deny(self, request_id: str, via: str = "") -> None:
        """Approver key. Resolves the request as denied."""
        ...

    async def expire(self, request_id: str) -> None:
        """Anyone, after the deadline. Resolves the request as denied."""
        ...

    async def freeze(self, frozen: bool, via: str = "") -> None:
        """Owner key. The kill switch."""
        ...

    async def set_policy(self, policy: Policy, via: str = "", rule_text: str = "") -> None:
        """Owner key. The agent key can never call this."""
        ...

    async def get_policy(self) -> Policy: ...

    async def spent_today(self) -> Decimal: ...

    async def is_frozen(self) -> bool: ...


class TransferPort(Protocol):
    """`chain/transfer.py`: start a cross-chain USDC transfer. Publishes `transfer.sent` and
    later `transfer.arrived`."""

    async def start(
        self,
        request: ChainTransferRequest,
        *,
        plan_id: str | None = None,
        payee_label: str = "",
        route_id: str = "",
        eta_s: int = 0,
    ) -> str: ...


class MarketsPort(Protocol):
    """`markets/`: the equivalence check, the move quote, and the move itself."""

    async def match(self, venue: str, slug: str) -> MarketMatchResponse: ...

    async def quote(self, from_venue: str, slug: str, qty: Decimal) -> MarketQuoteResponse: ...

    async def move(self, request: MarketMoveRequest) -> str: ...

    def listing(self) -> list[MarketListing]:
        """Markets with both venues' best bid, ask and bid depth, for the side-by-side panel.

        Not in the plan's frozen routes: P2 needs both sides' prices to show them next to each
        other, and `quote` only carries the sell bid and the buy ask.
        """
        ...


class AnchorPort(Protocol):
    """Records a 32-byte commitment onchain and returns a reference to it.

    Needed for forecast receipts ("hash each opinion onchain before the event"). The plan's
    frozen interface has no contract function or route for this, so P2 proposes this port.
    """

    async def commit(self, commitment_hex: str, label: str) -> str: ...

    async def reveal(self, anchor_ref: str, preimage: bytes) -> None:
        """Publish the preimage of an earlier `commit`, identified by the reference it returned.
        A reference this anchor did not issue is ignored."""
        ...
