from quotelab.engine.messages import CancelOrder, NewOrder, Rejected
from quotelab.engine.orders import OrderManager, WorkingOrder
from quotelab.types import NS_PER_MS, Fill, InstrumentSpec, Quote, Quotes, Side

SPEC = InstrumentSpec("X", tick_size=0.1, lot_size=0.001, min_notional=5.0)
MIN_REQUOTE_NS = 100 * NS_PER_MS
BID = Quote(400000, 100)
ASK = Quote(400010, 100)
MOVED_BID = Quote(399990, 100)


def make_manager() -> OrderManager:
    return OrderManager(SPEC, MIN_REQUOTE_NS)


def fill_of(order_id: int, side: Side, px: int, qty: int) -> Fill:
    return Fill(
        ts=0,
        symbol="X",
        order_id=order_id,
        side=side,
        px=px,
        qty=qty,
        fee=0.0,
        is_maker=True,
    )


def test_first_reconcile_sends_new_orders_for_both_sides():
    manager = make_manager()

    actions, wake = manager.reconcile(Quotes(BID, ASK), t_ns=0)

    assert actions == [NewOrder(1, Side.BUY, 400000, 100), NewOrder(2, Side.SELL, 400010, 100)]
    assert wake is None
    assert manager.orders_sent == 2


def test_unchanged_quotes_send_nothing():
    manager = make_manager()
    manager.reconcile(Quotes(BID, ASK), t_ns=0)

    assert manager.reconcile(Quotes(BID, ASK), t_ns=1) == ([], None)


def test_price_change_cancels_then_sends_new_order_once_min_requote_has_passed():
    manager = make_manager()
    manager.reconcile(Quotes(BID, ASK), t_ns=0)

    actions, wake = manager.reconcile(Quotes(MOVED_BID, ASK), t_ns=MIN_REQUOTE_NS // 2)

    assert actions == [CancelOrder(1)]
    assert wake == MIN_REQUOTE_NS
    assert manager.throttled == 1

    actions, wake = manager.reconcile(Quotes(MOVED_BID, ASK), t_ns=MIN_REQUOTE_NS)

    assert actions == [NewOrder(3, Side.BUY, 399990, 100)]
    assert wake is None
    assert manager.working(Side.BUY) == WorkingOrder(3, Side.BUY, 399990, 100)


def test_throttled_wake_time_is_reported_only_once():
    manager = make_manager()
    manager.reconcile(Quotes(BID, ASK), t_ns=0)

    _, first_wake = manager.reconcile(Quotes(MOVED_BID, ASK), t_ns=10 * NS_PER_MS)
    _, second_wake = manager.reconcile(Quotes(MOVED_BID, ASK), t_ns=20 * NS_PER_MS)

    assert first_wake == MIN_REQUOTE_NS
    assert second_wake is None
    assert manager.throttled == 1


def test_below_min_notional_quote_is_never_sent_and_counted_once_when_repeated():
    manager = make_manager()
    tiny = Quote(1000, 1)

    manager.reconcile(Quotes(tiny, None), t_ns=0)
    manager.reconcile(Quotes(tiny, None), t_ns=MIN_REQUOTE_NS)
    manager.reconcile(Quotes(Quote(1000, 2), None), t_ns=2 * MIN_REQUOTE_NS)

    assert manager.orders_sent == 0
    assert manager.working(Side.BUY) is None
    assert manager.local_rejects == 2


def test_quote_none_on_a_side_cancels_its_working_order():
    manager = make_manager()
    manager.reconcile(Quotes(BID, ASK), t_ns=0)

    actions, wake = manager.reconcile(Quotes(None, ASK), t_ns=1)

    assert actions == [CancelOrder(1)]
    assert wake is None
    assert manager.working(Side.BUY) is None
    assert manager.cancels_sent == 1


def test_partial_fill_keeps_order_working_and_full_fill_clears_it():
    manager = make_manager()
    manager.reconcile(Quotes(BID, ASK), t_ns=0)

    manager.on_fill(fill_of(1, Side.BUY, 400000, 40))
    manager.on_fill(fill_of(99, Side.SELL, 400010, 5))

    assert manager.working(Side.BUY) == WorkingOrder(1, Side.BUY, 400000, 100, filled=40)
    assert manager.reconcile(Quotes(BID, ASK), t_ns=1) == ([], None)

    manager.on_fill(fill_of(1, Side.BUY, 400000, 60))

    assert manager.working(Side.BUY) is None


def test_rejected_order_clears_the_working_order():
    manager = make_manager()
    manager.reconcile(Quotes(BID, ASK), t_ns=0)

    manager.on_rejected(Rejected(1, "post_only"))

    assert manager.working(Side.BUY) is None
    actions, _ = manager.reconcile(Quotes(BID, ASK), t_ns=MIN_REQUOTE_NS)
    assert actions == [NewOrder(3, Side.BUY, 400000, 100)]
