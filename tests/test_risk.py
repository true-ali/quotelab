"""Risk controls: inventory limit, kill switches, circuit breakers, size ramp and hedging.

Unit scenarios drive the manager directly with scripted events. The trader sees each event at
its own exchange time, so no latency is involved. The integration test runs the full backtest
loop, where the trader sees data after the engine's latencies.
"""

import dataclasses
from typing import Any

from quotelab.config import load_config
from quotelab.engine.backtest import run_backtest
from quotelab.engine.config import EngineConfig
from quotelab.engine.portfolio import Portfolio
from quotelab.fills.optimistic import OptimisticFill
from quotelab.risk.base import HedgeRequest
from quotelab.risk.manager import RiskConfig, RiskManager, risk_config
from quotelab.types import (
    NS_PER_MS,
    NS_PER_SECOND,
    BookEvent,
    Fill,
    InstrumentSpec,
    Quote,
    Quotes,
    Side,
    TradeEvent,
)

SPEC = InstrumentSpec("X", tick_size=0.1, lot_size=0.001, min_notional=5.0)
ORDER_LOTS = 10
T0 = 1_700_000_000 * NS_PER_SECOND
BID = 400_000
ASK = 400_001
QTY = 10
QUOTES = Quotes(Quote(BID, QTY), Quote(ASK, QTY))
NO_QUOTES = Quotes(None, None)
ENGINE = EngineConfig()
HOLE_START_MS = 2000
HOLE_END_MS = 12000

BASE = RiskConfig(
    kill_switches=False,
    circuit_breakers=False,
    size_ramp=False,
    max_inventory_orders=10,
    kill_inventory_orders=15,
    max_drawdown_bps=100.0,
    stale_after_s=5.0,
    cooldown_s=1.0,
    vol_short_s=2.0,
    vol_long_s=20.0,
    vol_ratio=4.0,
    max_spread_bps=5.0,
    toxicity_window_s=5.0,
    toxicity_threshold=0.8,
    toxicity_min_updates=20,
    max_gap_s=2.0,
    ramp_levels=(1, 2, 4),
    ramp_step_hours=2 / 3600,
    hedge_threshold_orders=None,
    hedge_to_orders=0,
    flatten_on_kill=True,
)


def make_cfg(**overrides: Any) -> RiskConfig:
    """The base config with everything off and small windows, plus per-test overrides."""
    return dataclasses.replace(BASE, **overrides)


def sec(seconds: float) -> int:
    return T0 + round(seconds * NS_PER_SECOND)


def book(seconds: float, bid: int = BID, ask: int = ASK, bid_qty: int = 50) -> BookEvent:
    return BookEvent(sec(seconds), "X", bid, bid_qty, ask, 50)


def observe(risk: RiskManager, ev: BookEvent | TradeEvent) -> None:
    risk.on_market(ev, ev.ts)


def kinds(risk: RiskManager) -> list[str]:
    return [event.kind for event in risk.events]


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


def ms_book(ms: int, bid: int = BID, ask: int = ASK) -> BookEvent:
    return BookEvent(T0 + ms * NS_PER_MS, "X", bid, 50, ask, 50)


def books_with_a_hole() -> list[BookEvent]:
    """Books every 100 ms up to 2 s, a 10 s silence, then books one tick higher."""
    before = [ms_book(ms) for ms in range(0, HOLE_START_MS + 1, 100)]
    after = [ms_book(ms, BID + 10, ASK + 10) for ms in range(HOLE_END_MS, HOLE_END_MS + 1001, 100)]
    return before + after


def run_with_kill_switches(kill_switches: bool) -> tuple[RiskManager, Any]:
    risk = RiskManager(SPEC, make_cfg(kill_switches=kill_switches), ORDER_LOTS)
    result = run_backtest(books_with_a_hole(), QuoteTouch(), OptimisticFill(), SPEC, ENGINE, risk)
    return risk, result


def test_inventory_limit_drops_the_side_that_adds_exposure_with_all_flags_off():
    risk = RiskManager(SPEC, make_cfg(), ORDER_LOTS)
    portfolio = Portfolio(SPEC)
    observe(risk, book(0))
    observe(risk, TradeEvent(sec(0.5), "X", BID, 5, Side.SELL))

    portfolio.position = 90
    assert risk.filter_quotes(QUOTES, portfolio, sec(1)) == QUOTES
    portfolio.position = 100
    assert risk.filter_quotes(QUOTES, portfolio, sec(1)) == Quotes(None, Quote(ASK, QTY))
    portfolio.position = -100
    assert risk.filter_quotes(QUOTES, portfolio, sec(1.5)) == Quotes(Quote(BID, QTY), None)
    assert risk.events == []


def test_size_ramp_steps_up_after_clean_time_and_a_breaker_resets_it():
    cfg = make_cfg(size_ramp=True, circuit_breakers=True)
    risk = RiskManager(SPEC, cfg, ORDER_LOTS)
    portfolio = Portfolio(SPEC)
    observe(risk, book(0))
    observe(risk, book(1))
    risk.on_timer(portfolio, sec(1))
    observe(risk, book(2))
    risk.on_timer(portfolio, sec(2))
    assert kinds(risk) == ["ramp_up"]
    assert risk.filter_quotes(QUOTES, portfolio, sec(2)) == Quotes(Quote(BID, 20), Quote(ASK, 20))

    observe(risk, book(3, ask=BID + 300))
    assert kinds(risk) == ["ramp_up", "cb_spread"]
    assert risk.filter_quotes(QUOTES, portfolio, sec(3.5)) == NO_QUOTES
    assert risk.filter_quotes(QUOTES, portfolio, sec(4)) == QUOTES


def test_kill_drawdown_latches_and_requests_a_full_flatten():
    risk = RiskManager(SPEC, make_cfg(kill_switches=True), ORDER_LOTS)
    portfolio = Portfolio(SPEC)
    portfolio.position = 100
    portfolio.cash = -4000.0
    observe(risk, book(0))
    risk.filter_quotes(QUOTES, portfolio, sec(0))
    observe(risk, book(1, bid=395_000, ask=395_001))
    risk.on_timer(portfolio, sec(1))
    assert kinds(risk) == ["kill_drawdown"]
    assert risk.filter_quotes(QUOTES, portfolio, sec(2)) == NO_QUOTES
    assert risk.hedge(portfolio, sec(2)) == HedgeRequest(Side.SELL, 100)


def test_kill_inventory_when_a_fill_pushes_position_past_the_kill_limit():
    risk = RiskManager(SPEC, make_cfg(kill_switches=True), ORDER_LOTS)
    portfolio = Portfolio(SPEC)
    observe(risk, book(0))
    fill = Fill(sec(0), "X", 1, Side.BUY, BID, 151, 0.0, True)
    portfolio.apply_fill(fill)
    risk.on_fill(fill, portfolio, sec(0))
    assert kinds(risk) == ["kill_inventory"]
    assert risk.killed
    assert risk.filter_quotes(QUOTES, portfolio, sec(0)) == NO_QUOTES


def test_kill_stale_fires_only_after_no_book_for_stale_after_s():
    risk = RiskManager(SPEC, make_cfg(kill_switches=True, stale_after_s=5.0), ORDER_LOTS)
    portfolio = Portfolio(SPEC)
    observe(risk, book(0))
    risk.on_timer(portfolio, sec(4))
    assert risk.events == []
    risk.on_timer(portfolio, sec(6))
    assert kinds(risk) == ["kill_stale"]
    assert risk.kill_reason == "kill_stale"


def test_cb_gap_pauses_quoting_until_the_cooldown_ends():
    risk = RiskManager(SPEC, make_cfg(circuit_breakers=True, cooldown_s=1.0), ORDER_LOTS)
    portfolio = Portfolio(SPEC)
    observe(risk, book(0))
    observe(risk, book(3.2))
    assert kinds(risk) == ["cb_gap"]
    assert risk.events[0].detail == "gap 3.200s > 2.0s"
    assert risk.filter_quotes(QUOTES, portfolio, sec(3.7)) == NO_QUOTES
    assert risk.filter_quotes(QUOTES, portfolio, sec(4.2)) == QUOTES


def test_cb_spread_trips_on_a_wide_book():
    risk = RiskManager(SPEC, make_cfg(circuit_breakers=True), ORDER_LOTS)
    observe(risk, book(0, ask=BID + 300))
    assert kinds(risk) == ["cb_spread"]


def test_cb_vol_trips_when_short_term_vol_jumps_above_long_term():
    """Per-second alternation keeps the sampled mid moving. A 2 s / 20 s pair can reach a
    short-to-long ratio of at most sqrt(10), so vol_ratio is lowered to 2 for this test."""
    cfg = make_cfg(circuit_breakers=True, vol_ratio=2.0, toxicity_threshold=2.0)
    risk = RiskManager(SPEC, cfg, ORDER_LOTS)
    for tick in range(250):
        bid = BID + (tick // 10) % 2
        observe(risk, book(tick / 10, bid=bid, ask=bid + 1))
    assert risk.events == []
    for second in (25, 26):
        bid = BID + 1000 * (second - 24)
        observe(risk, book(second, bid=bid, ask=bid + 1))
    assert kinds(risk) == ["cb_vol"]


def test_cb_toxicity_trips_on_one_sided_order_flow():
    risk = RiskManager(SPEC, make_cfg(circuit_breakers=True), ORDER_LOTS)
    for tick in range(25):
        observe(risk, BookEvent(sec(tick / 10), "X", BID, 50 + 10 * tick, ASK, 50))
    assert kinds(risk) == ["cb_toxicity"]


def test_with_all_flags_off_gaps_wide_spreads_and_drawdowns_are_ignored():
    risk = RiskManager(SPEC, make_cfg(), ORDER_LOTS)
    portfolio = Portfolio(SPEC)
    portfolio.position = 50
    portfolio.cash = -2000.0
    observe(risk, book(0))
    observe(risk, book(10, ask=BID + 300))
    risk.on_timer(portfolio, sec(10))
    portfolio.cash = -5000.0
    assert risk.filter_quotes(QUOTES, portfolio, sec(10)) == QUOTES
    assert risk.events == []
    assert not risk.killed
    assert risk.pause_until == 0


def test_hedge_reduces_a_position_above_the_threshold_to_the_target():
    risk = RiskManager(SPEC, make_cfg(hedge_threshold_orders=5, hedge_to_orders=2), ORDER_LOTS)
    portfolio = Portfolio(SPEC)
    portfolio.position = 50
    assert risk.hedge(portfolio, sec(0)) is None
    portfolio.position = 60
    assert risk.hedge(portfolio, sec(0)) == HedgeRequest(Side.SELL, 40)
    assert kinds(risk) == ["hedge"]


def test_risk_config_reads_the_default_yaml():
    cfg = risk_config(load_config())
    assert cfg.kill_switches is False
    assert cfg.circuit_breakers is False
    assert cfg.max_inventory_orders == 10
    assert cfg.ramp_levels == (1, 2, 4)
    assert cfg.hedge_threshold_orders is None
    assert cfg.flatten_on_kill is True


def test_kill_stale_stops_quoting_in_a_full_backtest_run():
    killed_risk, killed = run_with_kill_switches(True)
    free_risk, free = run_with_kill_switches(False)
    assert kinds(killed_risk) == ["kill_stale"]
    assert kinds(free_risk) == []
    assert "kill_stale" in killed.risk_events["kind"].to_list()

    kill_ts = killed_risk.events[0].ts
    timeseries = killed.timeseries
    before_kill = timeseries.filter(timeseries["ts"] < kill_ts)
    after_kill = timeseries.filter(timeseries["ts"] > kill_ts + NS_PER_SECOND)
    assert before_kill["quoting_bid"].any()
    assert after_kill.height > 0
    assert not after_kill["quoting_bid"].any()
    assert not after_kill["quoting_ask"].any()
    assert killed.summary["orders_sent"] < free.summary["orders_sent"]
