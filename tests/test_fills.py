import pytest

from quotelab.fills.base import RestingOrder
from quotelab.fills.factory import fill_model_from_config
from quotelab.fills.optimistic import OptimisticFill
from quotelab.fills.queue_aware import QueueAwareFill
from quotelab.fills.trade_through import TradeThroughFill
from quotelab.types import BookEvent, Side, TradeEvent


def book(bid_px: int, bid_qty: int, ask_px: int, ask_qty: int, ts: int = 0) -> BookEvent:
    return BookEvent(ts, "X", bid_px, bid_qty, ask_px, ask_qty)


def trade(px: int, qty: int, aggressor: Side, ts: int = 0) -> TradeEvent:
    return TradeEvent(ts, "X", px, qty, aggressor)


def buy(px: int, qty: int) -> RestingOrder:
    return RestingOrder(order_id=1, side=Side.BUY, px=px, qty=qty, arrival_ts=0)


def sell(px: int, qty: int) -> RestingOrder:
    return RestingOrder(order_id=1, side=Side.SELL, px=px, qty=qty, arrival_ts=0)


def test_optimistic_buy_fills_fully_when_a_trade_touches_its_price():
    order = buy(100, 5)
    model = OptimisticFill()

    assert model.on_trade([order], trade(100, 2, Side.SELL), book(99, 1, 101, 1)) == [(order, 5)]
    assert model.on_trade([order], trade(101, 2, Side.SELL), book(99, 1, 101, 1)) == []


def test_optimistic_buy_fills_fully_when_ask_reaches_its_price():
    order = buy(100, 5)

    assert OptimisticFill().on_book([order], book(99, 1, 101, 1), book(99, 1, 100, 1)) == [
        (order, 5)
    ]


def test_optimistic_sell_fills_fully_when_bid_reaches_its_price():
    order = sell(100, 5)

    assert OptimisticFill().on_book([order], book(99, 1, 101, 1), book(100, 1, 101, 1)) == [
        (order, 5)
    ]


def test_trade_through_buy_fills_only_on_a_print_strictly_below_its_price():
    order = buy(100, 5)
    model = TradeThroughFill()

    assert model.on_trade([order], trade(100, 2, Side.SELL), book(99, 1, 101, 1)) == []
    assert model.on_trade([order], trade(99, 2, Side.SELL), book(99, 1, 101, 1)) == [(order, 5)]


def test_trade_through_ignores_book_updates():
    order = buy(100, 5)

    assert TradeThroughFill().on_book([order], book(99, 1, 101, 1), book(99, 1, 99, 1)) == []


def test_queue_s1_queue_drains_by_trades_then_cancels_then_fills():
    model = QueueAwareFill(alpha=0.5)
    order = buy(100, 5)

    model.on_new_order(order, book(100, 10, 101, 10))
    assert order.queue_ahead == 10

    assert model.on_trade([order], trade(100, 4, Side.SELL), book(100, 10, 101, 10)) == []
    assert order.queue_ahead == 6
    assert order.traded_at_level == 4

    assert model.on_book([order], book(100, 10, 101, 10), book(100, 6, 101, 10)) == []
    assert order.queue_ahead == 6
    assert order.traded_at_level == 0

    assert model.on_book([order], book(100, 6, 101, 10), book(100, 2, 101, 10)) == []
    assert order.queue_ahead == 2

    assert model.on_trade([order], trade(100, 5, Side.SELL), book(100, 2, 101, 10)) == [(order, 3)]
    assert order.queue_ahead == 0
    assert order.qty == 5


def test_queue_s2_fills_are_capped_by_remaining_size():
    model = QueueAwareFill(alpha=0.5)
    order = buy(100, 3)

    model.on_new_order(order, book(99, 5, 101, 5))
    assert order.queue_ahead == 0

    assert model.on_trade([order], trade(100, 2, Side.SELL), book(99, 5, 101, 5)) == [(order, 2)]

    order.qty = 1
    assert model.on_trade([order], trade(100, 2, Side.SELL), book(99, 5, 101, 5)) == [(order, 1)]


def test_queue_s3_trade_through_the_level_fills_fully():
    model = QueueAwareFill(alpha=0.5)
    order = buy(100, 4)

    model.on_new_order(order, book(100, 10, 101, 10))

    assert model.on_trade([order], trade(99, 1, Side.SELL), book(100, 10, 101, 10)) == [(order, 4)]


def test_queue_s4_queue_is_learned_once_our_level_becomes_visible():
    model = QueueAwareFill(alpha=0.5)
    order = buy(100, 5)

    model.on_new_order(order, book(101, 5, 102, 5))
    assert order.queue_ahead is None

    assert model.on_trade([order], trade(100, 3, Side.SELL), book(101, 5, 102, 5)) == []
    assert model.on_book([order], book(101, 5, 102, 5), book(100, 7, 101, 5)) == []
    assert order.queue_ahead == 7


def test_queue_s5_market_crossing_our_price_fills_resting_buy_fully():
    model = QueueAwareFill(alpha=0.5)
    order = buy(100, 2)

    model.on_new_order(order, book(99, 5, 101, 5))
    assert order.queue_ahead == 0

    assert model.on_book([order], book(99, 5, 101, 5), book(99, 5, 100, 3)) == [(order, 2)]


def test_buy_aggressor_at_our_bid_price_neither_fills_nor_counts_level_volume():
    model = QueueAwareFill(alpha=0.5)
    order = buy(100, 5)
    model.on_new_order(order, book(100, 10, 101, 10))

    assert model.on_trade([order], trade(100, 4, Side.BUY), book(100, 10, 101, 10)) == []
    assert order.traded_at_level == 0
    assert order.queue_ahead == 10


def test_participation_scales_the_at_level_volume_that_reaches_the_queue():
    model = QueueAwareFill(alpha=0.5, participation=0.5)
    order = buy(100, 10)
    model.on_new_order(order, book(99, 5, 101, 5))

    assert model.on_trade([order], trade(100, 4, Side.SELL), book(99, 5, 101, 5)) == [(order, 2)]


def test_queue_sell_mirror_of_s1_drains_by_buy_trades_then_cancels_then_fills():
    model = QueueAwareFill(alpha=0.5)
    order = sell(101, 5)

    model.on_new_order(order, book(100, 10, 101, 10))
    assert order.queue_ahead == 10

    assert model.on_trade([order], trade(101, 4, Side.BUY), book(100, 10, 101, 10)) == []
    assert order.queue_ahead == 6
    assert order.traded_at_level == 4

    assert model.on_book([order], book(100, 10, 101, 10), book(100, 10, 101, 6)) == []
    assert order.queue_ahead == 6
    assert order.traded_at_level == 0

    assert model.on_book([order], book(100, 10, 101, 6), book(100, 10, 101, 2)) == []
    assert order.queue_ahead == 2

    assert model.on_trade([order], trade(101, 5, Side.BUY), book(100, 10, 101, 2)) == [(order, 3)]
    assert order.queue_ahead == 0


def test_factory_returns_the_model_for_each_name():
    assert isinstance(fill_model_from_config(fills_cfg("queue")), QueueAwareFill)
    assert isinstance(fill_model_from_config(fills_cfg("optimistic")), OptimisticFill)
    assert isinstance(fill_model_from_config(fills_cfg("trade_through")), TradeThroughFill)


def test_factory_rejects_unknown_model_and_lists_valid_names():
    with pytest.raises(ValueError, match="'bogus'.*optimistic, queue, trade_through"):
        fill_model_from_config(fills_cfg("bogus"))


def test_queue_model_rejects_alpha_outside_unit_interval():
    with pytest.raises(ValueError, match="alpha"):
        QueueAwareFill(alpha=1.5)


def fills_cfg(model: str) -> dict:
    return {"fills": {"model": model, "alpha": 0.5, "participation": 1.0}}
