"""Optimistic fill model: a resting order fills in full as soon as the market touches its price."""

from __future__ import annotations

from collections.abc import Sequence

from quotelab.fills.base import FillList, RestingOrder
from quotelab.types import BookEvent, Side, TradeEvent


class OptimisticFill:
    """Most generous model. Any print at or through our price, from either aggressor, fills us in
    full, and so does a book whose opposite touch has reached our price. For a given order at a
    given moment no model fills more; over a whole run it is not a strict bound, because other
    fills change every later quote. Useful to see how much PnL depends on being filled at all."""

    def on_new_order(self, order: RestingOrder, book: BookEvent) -> None:
        """No state is needed: the order fills on the first touch regardless of queue position."""

    def on_trade(
        self, orders: Sequence[RestingOrder], trade: TradeEvent, book: BookEvent
    ) -> FillList:
        return [(order, order.qty) for order in orders if _trade_reaches(order, trade.px)]

    def on_book(self, orders: Sequence[RestingOrder], prev: BookEvent, book: BookEvent) -> FillList:
        return [(order, order.qty) for order in orders if _book_reaches(order, book)]


def _trade_reaches(order: RestingOrder, px: int) -> bool:
    """A buy is reached by trades at or below its price, a sell by trades at or above it."""
    if order.side == Side.BUY:
        return px <= order.px
    return px >= order.px


def _book_reaches(order: RestingOrder, book: BookEvent) -> bool:
    """True when the opposite touch has reached the order's price (ask at or below a buy)."""
    if order.side == Side.BUY:
        return book.ask_px <= order.px
    return book.bid_px >= order.px
