"""Trade-through fill model: a resting order fills only when a print goes strictly past it."""

from __future__ import annotations

from collections.abc import Sequence

from quotelab.fills.base import FillList, RestingOrder
from quotelab.types import BookEvent, Side, TradeEvent


class TradeThroughFill:
    """Most conservative model. Touching our price is not enough: only a trade strictly through it
    fills us, and book updates never do. It is expected to understate fill rates, though not as a
    strict bound over a run, because other fills change every later quote."""

    def on_new_order(self, order: RestingOrder, book: BookEvent) -> None:
        """No state is needed: the decision depends only on the trade price."""

    def on_trade(
        self, orders: Sequence[RestingOrder], trade: TradeEvent, book: BookEvent
    ) -> FillList:
        return [(order, order.qty) for order in orders if _trades_through(order, trade.px)]

    def on_book(self, orders: Sequence[RestingOrder], prev: BookEvent, book: BookEvent) -> FillList:
        return []


def _trades_through(order: RestingOrder, px: int) -> bool:
    """A buy is passed by a trade strictly below its price, a sell by one strictly above it."""
    if order.side == Side.BUY:
        return px < order.px
    return px > order.px
