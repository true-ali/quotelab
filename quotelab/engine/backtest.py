"""Event-driven backtest loop with separate exchange and trader clocks.

Two views of the world run side by side:

* The exchange side (``SimExchange``) sees market events at their exchange timestamp, matches
  our resting orders with the fill model and holds the true portfolio.
* The trader side (strategy, risk layer, order manager) sees market data and our own fills only
  after ``data_latency_ns``, and its orders reach the exchange only after ``order_latency_ns``
  (cancels after ``cancel_latency_ns``).

Every latency is constant, so each internal queue is first-in first-out and a ``deque`` is
enough; the loop always processes the earliest pending item. When several items share a
timestamp the order is: market event, then arrivals at the exchange, then deliveries to the
trader, then timers. Putting the market first is the conservative choice: an order arriving at
the same nanosecond as a trade misses it, and a cancel arriving with a trade is too late.

The loop can be driven two ways. ``run`` replays a finished list of events. The paper runner
instead calls ``push`` for each live message as it arrives, ``advance_to`` to move the clock
through a silent feed, and ``finish`` at the end; ``run`` is exactly those calls in sequence,
so a backtest and a paper session step through the same code.
"""

from __future__ import annotations

import dataclasses
import heapq
import time
from collections import deque
from collections.abc import Iterable

from quotelab.engine.config import EngineConfig
from quotelab.engine.exchange import SimExchange
from quotelab.engine.messages import CancelDone, CancelOrder, FundingPaid, NewOrder, Rejected
from quotelab.engine.orders import OrderManager
from quotelab.engine.portfolio import Portfolio
from quotelab.engine.recorder import (
    FILLS_SCHEMA,
    FUNDING_SCHEMA,
    TIMESERIES_SCHEMA,
    BacktestResult,
    frame,
    risk_events_frame,
)
from quotelab.fills.base import FillModel
from quotelab.risk.base import NoRisk, RiskLayer
from quotelab.strategies.base import Strategy
from quotelab.types import (
    BookEvent,
    Event,
    Fill,
    FundingEvent,
    InstrumentSpec,
    Quotes,
    Side,
    TradeEvent,
)

NEVER = 2**63 - 1
NO_QUOTES = Quotes(bid=None, ask=None)


def run_backtest(
    events: Iterable[Event],
    strategy: Strategy,
    fill_model: FillModel,
    spec: InstrumentSpec,
    cfg: EngineConfig,
    risk: RiskLayer | None = None,
) -> BacktestResult:
    """Replay ``events`` (sorted by exchange time) through one strategy and return the run."""
    return Backtest(strategy, fill_model, spec, cfg, risk or NoRisk()).run(events)


class Backtest:
    """One backtest run. Create a new instance per run; it is not reusable."""

    def __init__(
        self,
        strategy: Strategy,
        fill_model: FillModel,
        spec: InstrumentSpec,
        cfg: EngineConfig,
        risk: RiskLayer,
    ) -> None:
        self.strategy = strategy
        self.spec = spec
        self.cfg = cfg
        self.risk = risk
        self.exchange = SimExchange(spec, fill_model, cfg.maker_fee_bps, cfg.taker_fee_bps)
        self.orders = OrderManager(spec, cfg.min_requote_ns)
        self.trader_portfolio = Portfolio(spec)
        self.desired = NO_QUOTES
        self.hedge_order_id: int | None = None
        self.order_queue: deque = deque()
        self.cancel_queue: deque = deque()
        self.trader_queue: deque = deque()
        self.wakes: list[int] = []
        self.next_timer = NEVER
        self.next_sample = NEVER
        self.samples: list[dict] = []
        self.market_events = 0
        self.start_ts: int | None = None
        self.end_ts = 0
        self.clock = 0
        self.wall_start = 0.0

    def run(self, events: Iterable[Event]) -> BacktestResult:
        for ev in events:
            self.push(ev)
        if self.start_ts is None:
            raise ValueError("no events to backtest")
        return self.finish()

    def push(self, ev: Event) -> None:
        """Process everything due before ``ev``, then ``ev`` itself.

        Market events must arrive in time order: one stamped before the clock raises, because
        processing it would let the past change what has already happened.
        """
        if self.start_ts is None:
            self._start(ev.ts)
        if ev.ts < self.clock:
            raise ValueError(f"event at {ev.ts} is before the engine clock {self.clock}")
        self._drain_before(ev.ts)
        if ev.ts >= self.next_sample:
            self._record_samples_until(ev.ts)
        self._on_market(ev)
        self.end_ts = ev.ts
        self.clock = ev.ts

    def advance_to(self, t_ns: int) -> None:
        """Process everything due before ``t_ns`` without a market event: timers, wakes and
        in-flight messages. Does nothing before the first event or when ``t_ns`` is not ahead.
        """
        if self.start_ts is None or t_ns <= self.clock:
            return
        self._drain_before(t_ns)
        self.clock = t_ns

    def finish(self) -> BacktestResult:
        """Process everything due up to the clock, take the last samples and return the run."""
        if self.start_ts is None:
            raise ValueError("no events to backtest")
        self._drain_before(self.clock + 1)
        self._record_samples_until(self.clock)
        wall_s = time.perf_counter() - self.wall_start
        return self._result(self.start_ts, self.clock, wall_s)

    def _start(self, ts: int) -> None:
        self.wall_start = time.perf_counter()
        self.start_ts = ts
        self.clock = ts
        self.next_timer = ts + self.cfg.timer_ns
        self.next_sample = ts

    def _drain_before(self, limit: int) -> None:
        """Process the internal queues and timers in time order until the next item is at or
        after ``limit``. At equal times: exchange arrivals, deliveries, wakes, then timers.
        """
        while True:
            exchange_ts = min(_head_ts(self.order_queue), _head_ts(self.cancel_queue))
            trader_ts = _head_ts(self.trader_queue)
            wake_ts = self.wakes[0] if self.wakes else NEVER
            now = min(exchange_ts, trader_ts, self.next_timer, wake_ts)
            if now >= limit:
                return
            if now >= self.next_sample:
                self._record_samples_until(now)
            if exchange_ts == now:
                self._on_exchange_arrival(now)
            elif trader_ts == now:
                self._on_trader_delivery(*self.trader_queue.popleft())
            elif wake_ts == now:
                heapq.heappop(self.wakes)
                self._act(now)
            else:
                self._on_timer(now)

    def _deliver(self, at_ns: int, message: object) -> None:
        self.trader_queue.append((at_ns + self.cfg.data_latency_ns, message))

    def _on_market(self, ev: Event) -> None:
        """Exchange side: match our orders, update the book, then publish to the trader."""
        self.market_events += 1
        if type(ev) is BookEvent:
            fills = self.exchange.on_book(ev)
        elif type(ev) is TradeEvent:
            fills = self.exchange.on_trade(ev)
        else:
            self._on_funding(ev)
            return
        for fill in fills:
            self._deliver(ev.ts, fill)
        self._deliver(ev.ts, ev)

    def _on_funding(self, ev: FundingEvent) -> None:
        paid = self.exchange.on_funding(ev)
        if paid is not None:
            self._deliver(ev.ts, paid)

    def _on_exchange_arrival(self, now: int) -> None:
        """Apply the earliest order, hedge or cancel that reaches the exchange at ``now``."""
        if _head_ts(self.order_queue) == now:
            message = self.order_queue.popleft()[1]
        else:
            message = self.cancel_queue.popleft()[1]
        if type(message) is NewOrder:
            rejected = self.exchange.place(message, now)
            if rejected is not None:
                self._deliver(now, rejected)
        elif type(message) is CancelOrder:
            self._deliver(now, self.exchange.cancel(message))
        else:
            self._deliver(now, self.exchange.hedge(message, now))

    def _on_trader_delivery(self, now: int, message: object) -> None:
        """Trader side: update its own view, ask the strategy for quotes, then act."""
        kind = type(message)
        quotes: Quotes | None = None
        if kind is BookEvent:
            self.risk.on_market(message, now)
            quotes = self.strategy.on_book(message)
        elif kind is TradeEvent:
            self.risk.on_market(message, now)
            quotes = self.strategy.on_trade(message)
        elif kind is Fill:
            quotes = self._on_own_fill(message, now)
        elif kind is Rejected:
            self.orders.on_rejected(message)
            if message.order_id == self.hedge_order_id:
                self.hedge_order_id = None
        elif kind is CancelDone:
            self.orders.on_cancel_done(message)
        elif kind is FundingPaid:
            self.trader_portfolio.pay_funding(message.payment)
        if quotes is not None:
            self.desired = quotes
        self._act(now)

    def _on_own_fill(self, fill: Fill, now: int) -> Quotes | None:
        self.trader_portfolio.apply_fill(fill)
        self.orders.on_fill(fill)
        if fill.order_id == self.hedge_order_id:
            self.hedge_order_id = None
        self.risk.on_fill(fill, self.trader_portfolio, now)
        return self.strategy.on_fill(fill)

    def _on_timer(self, now: int) -> None:
        self.next_timer += self.cfg.timer_ns
        self.risk.on_timer(self.trader_portfolio, now)
        quotes = self.strategy.on_timer(now)
        if quotes is not None:
            self.desired = quotes
        self._act(now)

    def _act(self, now: int) -> None:
        """Send the order changes needed to reach the risk-filtered quotes, then hedge."""
        allowed = self.risk.filter_quotes(self.desired, self.trader_portfolio, now)
        actions, wake = self.orders.reconcile(allowed, now)
        for action in actions:
            if type(action) is CancelOrder:
                self.cancel_queue.append((now + self.cfg.cancel_latency_ns, action))
            else:
                self.order_queue.append((now + self.cfg.order_latency_ns, action))
        if wake is not None:
            heapq.heappush(self.wakes, wake)
        if self.hedge_order_id is None:
            self._maybe_hedge(now)

    def _maybe_hedge(self, now: int) -> None:
        request = self.risk.hedge(self.trader_portfolio, now)
        if request is None or request.qty < 1:
            return
        hedge = self.orders.new_hedge(request.side, request.qty)
        self.hedge_order_id = hedge.order_id
        self.order_queue.append((now + self.cfg.order_latency_ns, hedge))

    def _record_samples_until(self, now: int) -> None:
        """Snapshot the exchange-side truth on the fixed sampling grid up to ``now``."""
        while self.next_sample <= now:
            if self.exchange.book is not None:
                self.samples.append(self._sample(self.next_sample))
            self.next_sample += self.cfg.sample_ns

    def _sample(self, ts: int) -> dict:
        book = self.exchange.book
        portfolio = self.exchange.portfolio
        mid = self.exchange.mid()
        return {
            "ts": ts,
            "mid": mid,
            "bid": self.spec.to_price(book.bid_px),
            "ask": self.spec.to_price(book.ask_px),
            "position": portfolio.position_qty(),
            "cash": portfolio.cash,
            "equity": portfolio.equity(mid),
            "fees_cum": portfolio.fees,
            "funding_cum": portfolio.funding,
            "n_fills": portfolio.fill_count,
            "quoting_bid": self.exchange.has_order(Side.BUY),
            "quoting_ask": self.exchange.has_order(Side.SELL),
        }

    def _result(self, start_ts: int, end_ts: int, wall_s: float) -> BacktestResult:
        timeseries = frame(self.samples, TIMESERIES_SCHEMA)
        fills = frame(self.exchange.fill_rows, FILLS_SCHEMA)
        return BacktestResult(
            fills=fills,
            timeseries=timeseries,
            funding=frame(self.exchange.funding_rows, FUNDING_SCHEMA),
            risk_events=risk_events_frame(self.risk.events),
            summary=self._summary(start_ts, end_ts, wall_s, timeseries, fills),
        )

    def _summary(self, start_ts, end_ts, wall_s, timeseries, fills) -> dict:
        portfolio = self.exchange.portfolio
        mid = self.exchange.mid()
        orders = self.orders
        return {
            "symbol": self.spec.symbol,
            "engine": dataclasses.asdict(self.cfg),
            "start_ts": start_ts,
            "end_ts": end_ts,
            "market_events": self.market_events,
            "wall_time_s": wall_s,
            "events_per_s": self.market_events / wall_s if wall_s > 0 else 0.0,
            "orders_sent": orders.orders_sent,
            "cancels_sent": orders.cancels_sent,
            "hedges_sent": orders.hedges_sent,
            "local_rejects": orders.local_rejects,
            "throttled": orders.throttled,
            "post_only_rejects": self.exchange.post_only_rejects,
            "quote_fills": int(fills.filter(fills["is_maker"]).height),
            "hedge_fills": int(fills.filter(~fills["is_maker"]).height),
            "final_mid": mid,
            "final_position": portfolio.position_qty(),
            "final_cash": portfolio.cash,
            "final_equity": portfolio.equity(mid) if mid is not None else portfolio.cash,
            "fees": portfolio.fees,
            "funding": portfolio.funding,
            "uptime_bid": _fraction_true(timeseries, "quoting_bid"),
            "uptime_ask": _fraction_true(timeseries, "quoting_ask"),
            "uptime_both": _fraction_both(timeseries),
        }


def _head_ts(queue: deque) -> int:
    return queue[0][0] if queue else NEVER


def _fraction_true(timeseries, column: str) -> float:
    """Share of 1-second samples where the flag was set (uptime at sampling resolution)."""
    if timeseries.height == 0:
        return 0.0
    return float(timeseries[column].mean())


def _fraction_both(timeseries) -> float:
    if timeseries.height == 0:
        return 0.0
    return float((timeseries["quoting_bid"] & timeseries["quoting_ask"]).mean())
