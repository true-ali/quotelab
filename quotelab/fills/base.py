"""Fill-model interface: how a passive order resting in the simulator gets executed.

The simulator only sees top-of-book and trade prints, never the full queue, so every model is
an assumption about what would have happened to an order that was never really on the
exchange. Two assumptions are shared by all models:

* No market impact. Our virtual order does not consume the volume it is filled against and
  does not change the market data that follows.
* A passive limit order always fills at its own price, even when the market trades through it.

A model only decides how many lots of which resting orders fill. The exchange applies the
fills, charges fees and removes fully filled orders.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from quotelab.types import BookEvent, Side, TradeEvent


@dataclass(slots=True)
class RestingOrder:
    """One of our passive orders as the exchange sees it.

    ``qty`` is the remaining size in lots and is reduced by the exchange, never by a fill model.
    ``queue_ahead`` and ``traded_at_level`` belong to the queue-aware model: lots estimated to
    be ahead of us at our price (``None`` while our price is behind the visible touch), and lots
    traded at our price since the last book update.
    """

    order_id: int
    side: Side
    px: int
    qty: int
    arrival_ts: int
    queue_ahead: float | None = None
    traded_at_level: int = 0


FillList = list[tuple[RestingOrder, int]]


class FillModel(Protocol):
    def on_new_order(self, order: RestingOrder, book: BookEvent) -> None:
        """Called once when an order starts resting, with the exchange's current book."""
        ...

    def on_trade(
        self, orders: Sequence[RestingOrder], trade: TradeEvent, book: BookEvent
    ) -> FillList:
        """A public trade at exchange time. ``book`` is the state before the trade's book update."""
        ...

    def on_book(self, orders: Sequence[RestingOrder], prev: BookEvent, book: BookEvent) -> FillList:
        """A top-of-book update. ``prev`` is the previous book state."""
        ...
