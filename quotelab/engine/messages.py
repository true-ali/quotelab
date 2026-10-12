"""Messages between the trader side and the simulated exchange.

Trader -> exchange messages arrive after the order or cancel latency. Exchange -> trader
messages (and our fills, which are plain ``Fill`` objects) arrive after the data latency.
"""

from __future__ import annotations

from dataclasses import dataclass

from quotelab.types import Side


@dataclass(frozen=True, slots=True)
class NewOrder:
    """A post-only limit order."""

    order_id: int
    side: Side
    px: int
    qty: int


@dataclass(frozen=True, slots=True)
class CancelOrder:
    order_id: int


@dataclass(frozen=True, slots=True)
class HedgeOrder:
    """A marketable order that takes liquidity at the touch for its full size."""

    order_id: int
    side: Side
    qty: int


@dataclass(frozen=True, slots=True)
class Rejected:
    """The exchange refused an order. ``reason`` is ``"post_only"`` or ``"no_book"``."""

    order_id: int
    reason: str


@dataclass(frozen=True, slots=True)
class CancelDone:
    """Cancel processed. ``removed`` is False when the order had already filled completely."""

    order_id: int
    removed: bool


@dataclass(frozen=True, slots=True)
class FundingPaid:
    """A funding settlement applied to our position. ``payment`` > 0 means we paid."""

    ts: int
    rate: float
    mark: float
    payment: float


ToExchange = NewOrder | CancelOrder | HedgeOrder
