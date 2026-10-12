"""Queue-aware fill model: estimates the lots queued ahead of our order at its price.

The exchange publishes only the top-of-book size, so the queue ahead of us is an estimate. Trades
at our price drain it, and book drops at our price that no trade explains are partly cancellations
from ahead of us.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from enum import Enum, auto

from quotelab.fills.base import FillList, RestingOrder
from quotelab.types import BookEvent, Side, TradeEvent


class _Standing(Enum):
    """Where our price sits relative to the best price on our side of the book."""

    AT_TOUCH = auto()
    IMPROVED = auto()
    BEHIND = auto()


def _opposite(side: Side) -> Side:
    return Side.SELL if side == Side.BUY else Side.BUY


def _own_level(side: Side, book: BookEvent) -> tuple[int, int]:
    """Best price and displayed size on the side where an order of ``side`` rests."""
    if side == Side.BUY:
        return book.bid_px, book.bid_qty
    return book.ask_px, book.ask_qty


def _opposite_touch_px(side: Side, book: BookEvent) -> int:
    """Best price on the side an order of ``side`` trades against."""
    if side == Side.BUY:
        return book.ask_px
    return book.bid_px


def _is_better_price(side: Side, px: int, than: int) -> bool:
    """True when ``px`` is strictly better than ``than`` for a resting order of ``side``."""
    if side == Side.BUY:
        return px > than
    return px < than


def _standing(order: RestingOrder, book: BookEvent) -> _Standing:
    """Whether our price joins, beats, or sits behind the best price on our side."""
    level_px, _ = _own_level(order.side, book)
    if level_px == order.px:
        return _Standing.AT_TOUCH
    if _is_better_price(order.side, order.px, level_px):
        return _Standing.IMPROVED
    return _Standing.BEHIND


def _queue_on_sight(order: RestingOrder, book: BookEvent) -> float | None:
    """Queue ahead when the order starts resting: the displayed size when joining the touch,
    zero when beating it, and unknown (None) when the level is not visible at all."""
    standing = _standing(order, book)
    if standing is _Standing.AT_TOUCH:
        _, level_qty = _own_level(order.side, book)
        return float(level_qty)
    if standing is _Standing.IMPROVED:
        return 0.0
    return None


def _crosses(order: RestingOrder, book: BookEvent) -> bool:
    """True when the opposite touch has reached or passed our price (ask <= P, or bid >= P)."""
    opposite_px = _opposite_touch_px(order.side, book)
    if order.side == Side.BUY:
        return opposite_px <= order.px
    return opposite_px >= order.px


class QueueAwareFill:
    """Fills an order only after the estimated queue ahead of it has been consumed.

    ``alpha`` is the share of unexplained size drops at our level assumed to be cancellations from
    ahead of us. ``participation`` is the share of at-level trade volume that reaches our queue;
    lowering it makes small at-level prints count less. A sweep through our price always fills us.
    """

    def __init__(self, alpha: float, participation: float = 1.0) -> None:
        if not 0 <= alpha <= 1:
            raise ValueError(f"alpha must be in [0, 1], got {alpha}")
        if not 0 < participation <= 1:
            raise ValueError(f"participation must be in (0, 1], got {participation}")
        self._alpha = alpha
        self._participation = participation

    def on_new_order(self, order: RestingOrder, book: BookEvent) -> None:
        order.queue_ahead = _queue_on_sight(order, book)

    def on_trade(
        self, orders: Sequence[RestingOrder], trade: TradeEvent, book: BookEvent
    ) -> FillList:
        fills: FillList = []
        for order in orders:
            fills.extend(self._fills_on_trade(order, trade))
        return fills

    def on_book(self, orders: Sequence[RestingOrder], prev: BookEvent, book: BookEvent) -> FillList:
        """A market that crossed our price fills us in full. Otherwise the queue estimate follows
        the book change. ``traded_at_level`` is cleared for every order so that the next book
        update only sees trades that happened after this one."""
        fills: FillList = []
        for order in orders:
            if _crosses(order, book):
                fills.append((order, order.qty))
            else:
                self._track_queue(order, prev, book)
            order.traded_at_level = 0
        return fills

    def _fills_on_trade(self, order: RestingOrder, trade: TradeEvent) -> FillList:
        if _is_better_price(order.side, order.px, trade.px):
            return [(order, order.qty)]
        if trade.px == order.px and trade.aggressor == _opposite(order.side):
            return self._fills_from_level_trade(order, trade.qty)
        return []

    def _fills_from_level_trade(self, order: RestingOrder, qty: int) -> FillList:
        """Only the volume that reaches our queue counts. It first consumes the queue ahead of us,
        and whatever is left over fills us."""
        order.traded_at_level += qty
        if order.queue_ahead is None:
            return []
        effective = self._participation * qty
        lots = math.floor(effective - order.queue_ahead)
        order.queue_ahead = max(0.0, order.queue_ahead - effective)
        return [(order, min(order.qty, lots))] if lots >= 1 else []

    def _track_queue(self, order: RestingOrder, prev: BookEvent, book: BookEvent) -> None:
        if order.queue_ahead is None:
            order.queue_ahead = _queue_on_sight(order, book)
            return
        standing = _standing(order, book)
        if standing is _Standing.IMPROVED:
            order.queue_ahead = 0.0
        elif standing is _Standing.AT_TOUCH:
            order.queue_ahead = self._queue_after_touch_update(order.queue_ahead, order, prev, book)

    def _queue_after_touch_update(
        self, queue: float, order: RestingOrder, prev: BookEvent, book: BookEvent
    ) -> float:
        """Size that disappeared at our level without a matching trade is treated as cancellations,
        and ``alpha`` of those are assumed to have come from ahead of us."""
        prev_px, prev_qty = _own_level(order.side, prev)
        _, level_qty = _own_level(order.side, book)
        if prev_px != order.px:
            return min(queue, level_qty)
        cancelled = max(0, (prev_qty - level_qty) - order.traded_at_level)
        return min(max(0.0, queue - self._alpha * cancelled), level_qty)
