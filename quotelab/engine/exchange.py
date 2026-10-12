"""Simulated venue: the true book, our resting orders and the true portfolio.

Fills are applied and fees are charged only here. A fill model decides how many lots of which
resting orders execute; the exchange turns those decisions into fills and bookkeeping rows.
"""

from __future__ import annotations

from quotelab.engine.messages import (
    CancelDone,
    CancelOrder,
    FundingPaid,
    HedgeOrder,
    NewOrder,
    Rejected,
)
from quotelab.engine.portfolio import Portfolio, fee_for
from quotelab.fills.base import FillList, FillModel, RestingOrder
from quotelab.types import BookEvent, Fill, FundingEvent, InstrumentSpec, Side, TradeEvent


class SimExchange:
    """The simulated venue. It holds the true book, our resting orders and the true portfolio.

    The trader only learns about the venue through events, so these attributes are the ground
    truth that the strategy's view is later compared against.
    """

    def __init__(
        self,
        spec: InstrumentSpec,
        fill_model: FillModel,
        maker_fee_bps: float,
        taker_fee_bps: float,
    ) -> None:
        self.spec = spec
        self.fill_model = fill_model
        self.maker_fee_bps = maker_fee_bps
        self.taker_fee_bps = taker_fee_bps
        self.book: BookEvent | None = None
        self.orders: dict[int, RestingOrder] = {}
        self.portfolio = Portfolio(spec)
        self.fill_rows: list[dict] = []
        self.funding_rows: list[dict] = []
        self.post_only_rejects: int = 0

    def mid(self) -> float | None:
        """Midpoint of the current book in quote units, or None before the first book."""
        if self.book is None:
            return None
        return self._mid_of(self.book)

    def has_order(self, side: Side) -> bool:
        return any(order.side == side for order in self.orders.values())

    def on_trade(self, ev: TradeEvent) -> list[Fill]:
        """Fill resting orders against a public trade. A trade never changes the book."""
        book = self.book
        if book is None or not self.orders:
            return []
        fills = self.fill_model.on_trade(list(self.orders.values()), ev, book)
        return self._apply_passive(fills, ev.ts, self._mid_of(book))

    def on_book(self, ev: BookEvent) -> list[Fill]:
        """Fill resting orders that the new book crossed.

        Fills are recorded at the previous book's mid, because that is the market state in which
        the order was crossed. The new book is stored before returning.
        """
        prev = self.book
        self.book = ev
        if prev is None or not self.orders:
            return []
        fills = self.fill_model.on_book(list(self.orders.values()), prev, ev)
        return self._apply_passive(fills, ev.ts, self._mid_of(prev))

    def on_funding(self, ev: FundingEvent) -> FundingPaid | None:
        """Settle funding on the current position at the current mid. Longs pay when rate > 0."""
        book = self.book
        if book is None:
            return None
        mark = self._mid_of(book)
        payment = self.portfolio.funding_payment(ev.rate, mark)
        self.portfolio.pay_funding(payment)
        self.funding_rows.append(
            {
                "ts": ev.ts,
                "rate": ev.rate,
                "mark": mark,
                "position": self.portfolio.position_qty(),
                "payment": payment,
            }
        )
        return FundingPaid(ev.ts, ev.rate, mark, payment)

    def place(self, msg: NewOrder, t_ns: int) -> Rejected | None:
        """Accept a post-only order that rests, or reject it.

        A post-only order that would take liquidity is rejected rather than filled, so a quote
        can never execute at the moment it is placed.
        """
        book = self.book
        if book is None:
            return Rejected(msg.order_id, "no_book")
        if self._would_take(msg, book):
            self.post_only_rejects += 1
            return Rejected(msg.order_id, "post_only")
        order = RestingOrder(msg.order_id, msg.side, msg.px, msg.qty, arrival_ts=t_ns)
        self.fill_model.on_new_order(order, book)
        self.orders[msg.order_id] = order
        return None

    def cancel(self, msg: CancelOrder) -> CancelDone:
        """Remove a resting order. An order that already filled completely reports removed=False."""
        removed = self.orders.pop(msg.order_id, None) is not None
        return CancelDone(msg.order_id, removed)

    def hedge(self, msg: HedgeOrder, t_ns: int) -> Fill | Rejected:
        """Take the touch for the full size at once.

        This is a documented simplification: no partial fills and no market impact. The hedge
        never touches the fill model or our resting orders.
        """
        book = self.book
        if book is None:
            return Rejected(msg.order_id, "no_book")
        px = book.ask_px if msg.side == Side.BUY else book.bid_px
        fee = fee_for(self.spec.notional(px, msg.qty), self.taker_fee_bps)
        fill = Fill(
            ts=t_ns,
            symbol=self.spec.symbol,
            order_id=msg.order_id,
            side=msg.side,
            px=px,
            qty=msg.qty,
            fee=fee,
            is_maker=False,
        )
        self.portfolio.apply_fill(fill)
        self.fill_rows.append(self._fill_row(fill, "hedge", self._mid_of(book)))
        return fill

    def _mid_of(self, book: BookEvent) -> float:
        return (self.spec.to_price(book.bid_px) + self.spec.to_price(book.ask_px)) / 2

    def _would_take(self, msg: NewOrder, book: BookEvent) -> bool:
        """True when a buy at or above the ask, or a sell at or below the bid, would cross."""
        if msg.side == Side.BUY:
            return msg.px >= book.ask_px
        return msg.px <= book.bid_px

    def _apply_passive(self, fills: FillList, ts: int, mid: float) -> list[Fill]:
        """Apply fill-model decisions to our resting orders as maker fills.

        A model may name the same order more than once, so each entry is clipped to what the
        order still has. Entries with nothing left to fill are skipped, which also keeps an
        order from being removed twice.
        """
        applied: list[Fill] = []
        for order, lots in fills:
            size = min(lots, order.qty)
            if size < 1:
                continue
            fill = self._passive_fill(order, size, ts)
            order.qty -= size
            if order.qty == 0:
                del self.orders[order.order_id]
            self.portfolio.apply_fill(fill)
            self.fill_rows.append(self._fill_row(fill, "quote", mid))
            applied.append(fill)
        return applied

    def _passive_fill(self, order: RestingOrder, lots: int, ts: int) -> Fill:
        fee = fee_for(self.spec.notional(order.px, lots), self.maker_fee_bps)
        return Fill(
            ts=ts,
            symbol=self.spec.symbol,
            order_id=order.order_id,
            side=order.side,
            px=order.px,
            qty=lots,
            fee=fee,
            is_maker=True,
        )

    def _fill_row(self, fill: Fill, kind: str, mid: float) -> dict:
        """Fill-table row. Must be built after the fill is applied, so position is post-fill."""
        return {
            "ts": fill.ts,
            "order_id": fill.order_id,
            "side": int(fill.side),
            "px": self.spec.to_price(fill.px),
            "qty": self.spec.to_qty(fill.qty),
            "fee": fill.fee,
            "is_maker": fill.is_maker,
            "kind": kind,
            "mid": mid,
            "position": self.portfolio.position_qty(),
        }
