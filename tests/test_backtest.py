"""Backtest loop: latency gating, cancel races, throttling, hedging, funding and determinism.

Every scenario uses the default engine timing: data latency 5 ms, order and cancel latency
10 ms, a 100 ms requote throttle. Times in the tests are milliseconds after ``T0``.
"""

import dataclasses
from dataclasses import dataclass, field

import pytest

from quotelab.config import instrument_spec, load_config
from quotelab.data.loader import events_from_frames
from quotelab.data.synthetic import generate, synthetic_config
from quotelab.engine.backtest import Backtest, run_backtest
from quotelab.engine.config import EngineConfig
from quotelab.fills.optimistic import OptimisticFill
from quotelab.fills.queue_aware import QueueAwareFill
from quotelab.risk.base import HedgeRequest, NoRisk
from quotelab.types import (
    NS_PER_MS,
    BookEvent,
    Fill,
    FundingEvent,
    InstrumentSpec,
    Quote,
    Quotes,
    Side,
    TradeEvent,
)

SPEC = InstrumentSpec("X", tick_size=0.1, lot_size=0.001, min_notional=5.0)
CFG = EngineConfig()
T0 = 1_700_000_000_000 * NS_PER_MS
BID = 400_000
ASK = 400_001
QTY = 10
NO_QUOTES = Quotes(None, None)


def at(ms: float) -> int:
    return T0 + round(ms * NS_PER_MS)


def book(ms: float) -> BookEvent:
    return BookEvent(at(ms), "X", BID, 50, ASK, 50)


def trade(ms: float, px: int) -> TradeEvent:
    return TradeEvent(at(ms), "X", px, 5, Side.SELL)


def bid_only(px: int) -> Quotes:
    return Quotes(Quote(px, QTY), None)


class Scripted:
    """Switches to ``plan[ts]`` when it sees the book stamped ``ts``; otherwise keeps quoting."""

    def __init__(self, plan: dict[int, Quotes]) -> None:
        self.plan = plan
        self.current = NO_QUOTES
        self.timer_calls: list[int] = []

    def on_book(self, ev: BookEvent) -> Quotes | None:
        self.current = self.plan.get(ev.ts, self.current)
        return self.current

    def on_trade(self, ev: TradeEvent) -> Quotes | None:
        return None

    def on_fill(self, fill: Fill) -> Quotes | None:
        return None

    def on_timer(self, t_ns: int) -> Quotes | None:
        self.timer_calls.append(t_ns)
        return None


class QuoteTouch:
    """Joins both sides of the touch with a fixed size on every book update."""

    def on_book(self, ev: BookEvent) -> Quotes | None:
        return Quotes(Quote(ev.bid_px, QTY), Quote(ev.ask_px, QTY))

    def on_trade(self, ev: TradeEvent) -> Quotes | None:
        return None

    def on_fill(self, fill: Fill) -> Quotes | None:
        return None

    def on_timer(self, t_ns: int) -> Quotes | None:
        return None


@dataclass
class RecordingRisk(NoRisk):
    """Records when the trader side sees things; optionally hedges any open position."""

    hedge_open_position: bool = False
    market_seen: list[tuple[int, int]] = field(default_factory=list)
    fills_seen: list[tuple[int, int]] = field(default_factory=list)

    def on_market(self, ev, t_ns):
        self.market_seen.append((ev.ts, t_ns))

    def on_fill(self, fill, portfolio, t_ns):
        self.fills_seen.append((fill.ts, t_ns))

    def hedge(self, portfolio, t_ns):
        if not self.hedge_open_position or portfolio.position == 0:
            return None
        side = Side.SELL if portfolio.position > 0 else Side.BUY
        return HedgeRequest(side, abs(portfolio.position))


def run(events, strategy, risk=None, cfg=CFG, fill_model=None) -> tuple[Backtest, object]:
    bt = Backtest(strategy, fill_model or OptimisticFill(), SPEC, cfg, risk or NoRisk())
    return bt, bt.run(events)


def one_fill_scenario() -> list:
    """Bid sent at 5 ms reaches the exchange at 15 ms; only the trade at 16 ms can fill it."""
    return [book(0), trade(14, BID), trade(16, BID), book(1000)]


def test_trader_sees_market_data_after_data_latency():
    risk = RecordingRisk()
    run([book(0), trade(1, ASK), book(2), book(1000)], Scripted({}), risk)
    assert risk.market_seen
    assert all(seen - ts == CFG.data_latency_ns for ts, seen in risk.market_seen)


def test_order_rests_only_after_order_latency():
    _, result = run(one_fill_scenario(), Scripted({at(0): bid_only(BID)}))
    assert result.fills["ts"].to_list() == [at(16)]
    assert result.fills["order_id"].to_list() == [1]


def test_own_fill_reaches_trader_after_data_latency():
    risk = RecordingRisk()
    run(one_fill_scenario(), Scripted({at(0): bid_only(BID)}), risk)
    assert risk.fills_seen == [(at(16), at(16) + CFG.data_latency_ns)]


@pytest.mark.parametrize(("trade_ms", "filled_ids"), [(110, [1]), (120, [])])
def test_order_can_fill_while_its_cancel_is_in_flight(trade_ms, filled_ids):
    """The requote at 105 ms cancels order 1; the cancel only reaches the exchange at 115 ms."""
    cfg = dataclasses.replace(CFG, min_requote_ns=0)
    plan = {at(0): bid_only(BID), at(100): bid_only(BID - 1)}
    events = [book(0), book(100), trade(trade_ms, BID), book(1000)]
    bt, result = run(events, Scripted(plan), cfg=cfg)
    assert result.fills["order_id"].to_list() == filled_ids
    assert result.summary["cancels_sent"] == 1
    assert bt.trader_portfolio.position == bt.exchange.portfolio.position


def test_post_only_order_that_would_cross_is_rejected():
    plan = {at(0): bid_only(ASK)}
    _, result = run([book(0), book(1000)], Scripted(plan))
    assert result.fills.height == 0
    assert result.summary["post_only_rejects"] >= 1
    assert result.summary["post_only_rejects"] == result.summary["orders_sent"]


def test_throttled_requote_is_sent_when_the_throttle_expires():
    """Order 1 is sent at 5 ms. The change seen at 25 ms is throttled until 105 ms, with no
    market event at that time, so only the scheduled wake can send order 2 (resting at 115 ms).
    Order 3 refills the bid after order 2 fills at 200 ms."""
    plan = {at(0): bid_only(BID), at(20): bid_only(BID - 1)}
    events = [book(0), book(20), trade(200, BID - 1), book(1000)]
    _, result = run(events, Scripted(plan))
    assert result.fills["order_id"].to_list() == [2]
    assert result.summary["throttled"] == 1
    assert result.summary["orders_sent"] == 3


def test_maker_fill_updates_cash_fees_and_equity():
    bt, result = run(one_fill_scenario(), Scripted({at(0): bid_only(BID)}))
    notional = 40_000.0 * 0.01
    fee = notional * 2.0 / 10_000
    summary = result.summary
    assert summary["final_position"] == pytest.approx(0.01)
    assert summary["fees"] == pytest.approx(fee)
    assert summary["final_cash"] == pytest.approx(-notional - fee)
    assert summary["final_equity"] == pytest.approx(-notional - fee + 0.01 * 40_000.05)
    assert bt.trader_portfolio.cash == pytest.approx(bt.exchange.portfolio.cash)


def test_hedge_takes_the_touch_once_and_pays_taker_fee():
    """Books at 25 and 30 ms reach the trader while the hedge is in flight; no second hedge."""
    risk = RecordingRisk(hedge_open_position=True)
    events = [book(0), trade(16, BID), book(25), book(30), book(1000)]
    _, result = run(events, Scripted({at(0): bid_only(BID)}), risk)
    hedges = result.fills.filter(result.fills["kind"] == "hedge")
    assert result.summary["hedges_sent"] == 1
    assert hedges["px"].to_list() == [pytest.approx(40_000.0)]
    assert hedges["fee"].to_list() == [pytest.approx(400.0 * 5.0 / 10_000)]
    assert hedges["is_maker"].to_list() == [False]
    assert hedges["ts"].to_list() == [at(21) + CFG.order_latency_ns]
    assert result.summary["final_position"] == pytest.approx(0.0)


def test_funding_is_charged_on_the_open_position_at_the_mid():
    rate = 0.0001
    events = one_fill_scenario()
    events.insert(3, FundingEvent(at(500), "X", rate))
    bt, result = run(events, Scripted({at(0): bid_only(BID)}))
    expected = 0.01 * 40_000.05 * rate
    assert result.funding["payment"].to_list() == [pytest.approx(expected)]
    assert result.summary["funding"] == pytest.approx(expected)
    assert bt.trader_portfolio.funding == pytest.approx(expected)


def test_timer_fires_every_second_and_samples_follow_the_grid():
    strategy = Scripted({})
    _, result = run([book(0), book(3000)], strategy)
    assert strategy.timer_calls == [at(1000), at(2000), at(3000)]
    assert result.timeseries["ts"].to_list() == [at(1000), at(2000), at(3000)]


def test_same_input_gives_identical_runs():
    cfg = load_config()
    sc = dataclasses.replace(synthetic_config(cfg), duration_s=120)
    spec = instrument_spec(cfg, sc.symbol)
    book_frame, trades_frame = generate(sc)

    def once():
        events = events_from_frames(spec, book_frame, trades_frame)
        return run_backtest(events, QuoteTouch(), QueueAwareFill(0.5, 1.0), spec, CFG)

    first, second = once(), once()
    timing = {"wall_time_s", "events_per_s"}
    assert first.fills.height > 0
    assert first.fills.equals(second.fills)
    assert first.timeseries.equals(second.timeseries)
    assert {k: v for k, v in first.summary.items() if k not in timing} == {
        k: v for k, v in second.summary.items() if k not in timing
    }
