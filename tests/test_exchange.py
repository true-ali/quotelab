import pytest

from quotelab.engine.exchange import SimExchange
from quotelab.engine.messages import (
    CancelDone,
    CancelOrder,
    FundingPaid,
    HedgeOrder,
    NewOrder,
    Rejected,
)
from quotelab.fills.base import FillList, RestingOrder
from quotelab.types import BookEvent, Fill, FundingEvent, InstrumentSpec, Side, TradeEvent

SPEC = InstrumentSpec("X", tick_size=0.1, lot_size=0.001, min_notional=5.0)
SYMBOL = "X"
MAKER_BPS = 2.0
TAKER_BPS = 5.0


class FillOnTouch:
    """Fills an order in full once the market prints through its price or the touch crosses it."""

    def on_new_order(self, order: RestingOrder, book: BookEvent) -> None:
        return None

    def on_trade(self, orders: list[RestingOrder], trade: TradeEvent, book: BookEvent) -> FillList:
        return [
            (order, order.qty)
            for order in orders
            if _traded_through(order, trade.px) or _crossed_by(order, book)
        ]

    def on_book(self, orders: list[RestingOrder], prev: BookEvent, book: BookEvent) -> FillList:
        return [(order, order.qty) for order in orders if _crossed_by(order, book)]


class NeverFill:
    """A fill model under which no resting order ever executes."""

    def on_new_order(self, order: RestingOrder, book: BookEvent) -> None:
        return None

    def on_trade(self, orders: list[RestingOrder], trade: TradeEvent, book: BookEvent) -> FillList:
        return []

    def on_book(self, orders: list[RestingOrder], prev: BookEvent, book: BookEvent) -> FillList:
        return []


def _traded_through(order: RestingOrder, trade_px: int) -> bool:
    """A trade at or beyond our price, on the side that would be hit."""
    if order.side == Side.BUY:
        return trade_px <= order.px
    return trade_px >= order.px


def _crossed_by(order: RestingOrder, book: BookEvent) -> bool:
    """The touch has moved to or through our price, on the side that would be hit."""
    if order.side == Side.BUY:
        return book.ask_px <= order.px
    return book.bid_px >= order.px


def make_book(ts: int, bid_px: int, ask_px: int) -> BookEvent:
    return BookEvent(ts, SYMBOL, bid_px, 1000, ask_px, 1000)


def exchange_with_book(fill_model) -> SimExchange:
    exchange = SimExchange(SPEC, fill_model, MAKER_BPS, TAKER_BPS)
    exchange.on_book(make_book(1000, 400000, 400010))
    return exchange


def test_place_before_any_book_is_rejected_as_no_book():
    exchange = SimExchange(SPEC, NeverFill(), MAKER_BPS, TAKER_BPS)

    assert exchange.place(NewOrder(1, Side.BUY, 399990, 100), t_ns=0) == Rejected(1, "no_book")
    assert exchange.orders == {}


def test_post_only_rejects_buy_at_ask_and_sell_at_bid():
    exchange = exchange_with_book(NeverFill())

    assert exchange.place(NewOrder(1, Side.BUY, 400010, 100), t_ns=0) == Rejected(1, "post_only")
    assert exchange.place(NewOrder(2, Side.SELL, 400000, 100), t_ns=0) == Rejected(2, "post_only")
    assert exchange.post_only_rejects == 2
    assert exchange.orders == {}


def test_resting_buy_fills_on_touching_trade_with_maker_fee_cash_and_position():
    exchange = exchange_with_book(FillOnTouch())
    assert exchange.place(NewOrder(1, Side.BUY, 399990, 100), t_ns=1500) is None

    [fill] = exchange.on_trade(TradeEvent(2000, SYMBOL, 399990, 5, Side.SELL))

    notional = 39999.0 * 0.1
    fee = notional * MAKER_BPS / 10_000
    assert (fill.order_id, fill.side, fill.px, fill.qty, fill.is_maker) == (
        1,
        Side.BUY,
        399990,
        100,
        True,
    )
    assert fill.fee == pytest.approx(fee)
    assert exchange.portfolio.position == 100
    assert exchange.portfolio.cash == pytest.approx(-(notional + fee))
    assert exchange.orders == {}
    row = exchange.fill_rows[0]
    assert row["ts"] == 2000
    assert row["kind"] == "quote"
    assert row["mid"] == pytest.approx(40000.5)
    assert row["position"] == pytest.approx(0.1)


def test_book_cross_fill_is_recorded_at_the_previous_books_mid():
    exchange = exchange_with_book(FillOnTouch())
    exchange.place(NewOrder(1, Side.BUY, 399990, 100), t_ns=1500)

    fills = exchange.on_book(make_book(3000, bid_px=399980, ask_px=399990))

    assert [fill.order_id for fill in fills] == [1]
    row = exchange.fill_rows[-1]
    assert row["ts"] == 3000
    assert row["mid"] == pytest.approx(40000.5)
    assert row["mid"] != pytest.approx(39998.5)


def test_cancel_removes_live_order_and_reports_false_once_filled():
    exchange = exchange_with_book(FillOnTouch())
    exchange.place(NewOrder(1, Side.BUY, 399990, 100), t_ns=1500)
    exchange.place(NewOrder(2, Side.BUY, 399980, 100), t_ns=1500)

    assert exchange.cancel(CancelOrder(2)) == CancelDone(2, True)
    exchange.on_trade(TradeEvent(2000, SYMBOL, 399990, 5, Side.SELL))

    assert exchange.cancel(CancelOrder(1)) == CancelDone(1, False)


def test_hedge_buy_takes_the_ask_with_taker_fee_and_hedge_kind():
    exchange = exchange_with_book(NeverFill())

    fill = exchange.hedge(HedgeOrder(5, Side.BUY, 100), t_ns=2500)

    notional = 40001.0 * 0.1
    assert isinstance(fill, Fill)
    assert (fill.order_id, fill.side, fill.px, fill.qty, fill.is_maker) == (
        5,
        Side.BUY,
        400010,
        100,
        False,
    )
    assert fill.fee == pytest.approx(notional * TAKER_BPS / 10_000)
    assert exchange.portfolio.position == 100
    assert exchange.fill_rows[-1]["kind"] == "hedge"


def test_funding_charges_a_long_position_when_rate_is_positive():
    exchange = exchange_with_book(NeverFill())
    exchange.hedge(HedgeOrder(5, Side.BUY, 100), t_ns=2500)

    paid = exchange.on_funding(FundingEvent(3000, SYMBOL, 0.0001))

    expected_payment = 0.1 * 40000.5 * 0.0001
    assert isinstance(paid, FundingPaid)
    assert paid.mark == pytest.approx(40000.5)
    assert paid.payment == pytest.approx(expected_payment)
    assert paid.payment > 0
    assert exchange.funding_rows[-1]["position"] == pytest.approx(0.1)
