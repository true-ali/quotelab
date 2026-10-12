"""Trader-side risk controls: kill switches, circuit breakers, a size ramp and inventory limits.

The manager sees what the trader sees (delayed market data and our own fills) and filters the
quotes the strategy wants to send. Each control is switched on or off in the config. The hard
inventory limit always applies, whatever the flags say.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from quotelab.engine.portfolio import Portfolio
from quotelab.risk.base import HedgeRequest, RiskEvent
from quotelab.strategies.signals import OfiTracker, RollingVol, mid_price
from quotelab.types import (
    NS_PER_SECOND,
    BookEvent,
    Fill,
    InstrumentSpec,
    Quote,
    Quotes,
    Side,
    TradeEvent,
)

NO_QUOTES = Quotes(None, None)


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Settings for every risk control. Times are in seconds or hours, as written in the YAML."""

    kill_switches: bool
    circuit_breakers: bool
    size_ramp: bool
    max_inventory_orders: int
    kill_inventory_orders: int
    max_drawdown_bps: float
    stale_after_s: float
    cooldown_s: float
    vol_short_s: float
    vol_long_s: float
    vol_ratio: float
    max_spread_bps: float
    toxicity_window_s: float
    toxicity_threshold: float
    toxicity_min_updates: int
    max_gap_s: float
    ramp_levels: tuple[int, ...]
    ramp_step_hours: float
    hedge_threshold_orders: int | None
    hedge_to_orders: int
    flatten_on_kill: bool


def risk_config(cfg: dict[str, Any]) -> RiskConfig:
    """Build the typed risk settings from the YAML ``risk`` section.

    The hard inventory limit is read from ``strategy.max_inventory_orders`` so the strategy and
    the risk layer can never disagree about it. The ramp levels become a tuple so the config
    stays hashable and cannot change mid-run.
    """
    raw = cfg["risk"]
    hedge_threshold = raw["hedge_threshold_orders"]
    return RiskConfig(
        kill_switches=bool(raw["kill_switches"]),
        circuit_breakers=bool(raw["circuit_breakers"]),
        size_ramp=bool(raw["size_ramp"]),
        max_inventory_orders=int(cfg["strategy"]["max_inventory_orders"]),
        kill_inventory_orders=int(raw["kill_inventory_orders"]),
        max_drawdown_bps=float(raw["max_drawdown_bps"]),
        stale_after_s=float(raw["stale_after_s"]),
        cooldown_s=float(raw["cooldown_s"]),
        vol_short_s=float(raw["vol_short_s"]),
        vol_long_s=float(raw["vol_long_s"]),
        vol_ratio=float(raw["vol_ratio"]),
        max_spread_bps=float(raw["max_spread_bps"]),
        toxicity_window_s=float(raw["toxicity_window_s"]),
        toxicity_threshold=float(raw["toxicity_threshold"]),
        toxicity_min_updates=int(raw["toxicity_min_updates"]),
        max_gap_s=float(raw["max_gap_s"]),
        ramp_levels=tuple(int(level) for level in raw["ramp_levels"]),
        ramp_step_hours=float(raw["ramp_step_hours"]),
        hedge_threshold_orders=None if hedge_threshold is None else int(hedge_threshold),
        hedge_to_orders=int(raw["hedge_to_orders"]),
        flatten_on_kill=bool(raw["flatten_on_kill"]),
    )


class RiskManager:
    """Filters the strategy's quotes and requests hedges according to the risk controls.

    Kill switches latch. Once one trips (stale data, inventory or drawdown past its limit) the
    manager stops quoting for the rest of the run and, if ``flatten_on_kill`` is set, asks for a
    taker hedge that flattens the position. A kill needs a human to reset it, so nothing here
    clears it.

    Circuit breakers are temporary. A tripped breaker pauses both sides for ``cooldown_s`` and
    quoting then resumes. The size ramp is a staged rollout: quotes start at the first level,
    step up through ``ramp_levels`` after each ``ramp_step_hours`` of clean operation, and drop
    back to the first level on any breaker trip. The hard inventory limit stops quoting the side
    that would add exposure beyond ``max_inventory_orders``.
    """

    def __init__(self, spec: InstrumentSpec, cfg: RiskConfig, order_lots: int) -> None:
        if cfg.size_ramp and not cfg.ramp_levels:
            raise ValueError("ramp_levels must not be empty when size_ramp is on")
        self.spec = spec
        self.cfg = cfg
        self.events: list[RiskEvent] = []
        self.killed = False
        self.kill_reason: str | None = None
        self.pause_until = 0
        self.ramp_index = 0
        self.max_inventory_lots = cfg.max_inventory_orders * order_lots
        self.kill_inventory_lots = cfg.kill_inventory_orders * order_lots
        self.hedge_threshold_lots = (
            None if cfg.hedge_threshold_orders is None else cfg.hedge_threshold_orders * order_lots
        )
        self.hedge_to_lots = cfg.hedge_to_orders * order_lots
        self._max_gap_ns = _seconds_to_ns(cfg.max_gap_s)
        self._stale_ns = _seconds_to_ns(cfg.stale_after_s)
        self._cooldown_ns = _seconds_to_ns(cfg.cooldown_s)
        self._ramp_step_ns = _seconds_to_ns(cfg.ramp_step_hours * 3600)
        self._last_book: BookEvent | None = None
        self._last_book_seen_at: int | None = None
        self._last_market_ts: int | None = None
        self._peak_equity: float | None = None
        self.drawdown_limit_usd: float | None = None
        self._ramp_started_at: int | None = None
        self._logged_breakers: set[str] = set()
        self._short_vol = RollingVol(cfg.vol_short_s, fallback=0.0)
        self._long_vol = RollingVol(cfg.vol_long_s, fallback=0.0)
        self._ofi = OfiTracker(_seconds_to_ns(cfg.toxicity_window_s))

    def on_market(self, ev: BookEvent | TradeEvent, t_ns: int) -> None:
        """Feed one market event that the trader sees at ``t_ns``."""
        if self.cfg.circuit_breakers:
            self._check_gap(ev.ts, t_ns)
        self._last_market_ts = ev.ts
        if isinstance(ev, BookEvent):
            self._on_book(ev, t_ns)

    def on_fill(self, fill: Fill, portfolio: Portfolio, t_ns: int) -> None:
        """A fill can push inventory past the kill limit, so the check runs right away."""
        if abs(portfolio.position) > self.kill_inventory_lots:
            detail = f"position {portfolio.position} > {self.kill_inventory_lots} lots"
            self._kill("kill_inventory", detail, t_ns)
        self._check_drawdown(portfolio, t_ns)

    def on_timer(self, portfolio: Portfolio, t_ns: int) -> None:
        """Periodic checks: stale market data, drawdown and the size ramp step."""
        seen_at = self._last_book_seen_at
        if seen_at is not None and t_ns - seen_at > self._stale_ns:
            silent_s = (t_ns - seen_at) / NS_PER_SECOND
            detail = f"no book for {silent_s:.1f}s > {self.cfg.stale_after_s}s"
            self._kill("kill_stale", detail, t_ns)
        self._check_drawdown(portfolio, t_ns)
        self._maybe_ramp_up(t_ns)

    def filter_quotes(self, quotes: Quotes, portfolio: Portfolio, t_ns: int) -> Quotes:
        """Return the quotes that may be sent now.

        Nothing is sent while killed or paused. Otherwise the quotes are scaled by the ramp level
        and any side that would breach the inventory limit is dropped.
        """
        self._check_drawdown(portfolio, t_ns)
        if self.killed or t_ns < self.pause_until:
            return NO_QUOTES
        multiplier = self._size_multiplier()
        bid = _scaled(quotes.bid, multiplier)
        ask = _scaled(quotes.ask, multiplier)
        position = portfolio.position
        if bid is not None and position + bid.qty > self.max_inventory_lots:
            bid = None
        if ask is not None and position - ask.qty < -self.max_inventory_lots:
            ask = None
        return Quotes(bid, ask)

    def hedge(self, portfolio: Portfolio, t_ns: int) -> HedgeRequest | None:
        """Taker hedge to request now.

        After a kill with ``flatten_on_kill`` this flattens the whole position. Otherwise a
        position above the hedge threshold is reduced to ``hedge_to_orders``.
        """
        position = portfolio.position
        if self.killed and self.cfg.flatten_on_kill and position != 0:
            return HedgeRequest(_reducing_side(position), abs(position))
        if self.hedge_threshold_lots is None or abs(position) <= self.hedge_threshold_lots:
            return None
        qty = abs(position) - self.hedge_to_lots
        detail = f"position {position} > {self.hedge_threshold_lots} lots, hedge {qty} lots"
        self.events.append(RiskEvent(t_ns, "hedge", detail))
        return HedgeRequest(_reducing_side(position), qty)

    def _on_book(self, book: BookEvent, t_ns: int) -> None:
        """Remember the book, set the one-time limits and run the breakers on the new mid."""
        mid = mid_price(self.spec, book)
        self._last_book = book
        self._last_book_seen_at = t_ns
        if self.drawdown_limit_usd is None:
            max_qty = self.spec.to_qty(self.max_inventory_lots)
            self.drawdown_limit_usd = self.cfg.max_drawdown_bps / 10_000 * max_qty * mid
        if self._ramp_started_at is None:
            self._ramp_started_at = t_ns
        if self.cfg.circuit_breakers:
            self._short_vol.update(book.ts, mid)
            self._long_vol.update(book.ts, mid)
            self._ofi.update(book)
            self._check_breakers(book, mid, t_ns)

    def _check_gap(self, ts: int, t_ns: int) -> None:
        """Trip when no market event arrived for longer than ``max_gap_s`` of exchange time."""
        if self._last_market_ts is None:
            return
        gap_ns = ts - self._last_market_ts
        if gap_ns > self._max_gap_ns:
            detail = f"gap {gap_ns / NS_PER_SECOND:.3f}s > {self.cfg.max_gap_s}s"
            self._trip("cb_gap", detail, t_ns)

    def _check_breakers(self, book: BookEvent, mid: float, t_ns: int) -> None:
        """Spread, volatility-regime and order-flow toxicity checks on a fresh book."""
        spread_usd = self.spec.to_price(book.ask_px) - self.spec.to_price(book.bid_px)
        spread_bps = spread_usd / mid * 10_000
        if spread_bps > self.cfg.max_spread_bps:
            detail = f"spread {spread_bps:.2f} bps > {self.cfg.max_spread_bps:.2f} bps"
            self._trip("cb_spread", detail, t_ns)
        if self._vol_spiked():
            short_sigma = self._short_vol.sigma()
            long_sigma = self._long_vol.sigma()
            detail = (
                f"short vol {short_sigma:.4f} > {self.cfg.vol_ratio:.2f} x "
                f"long vol {long_sigma:.4f}"
            )
            self._trip("cb_vol", detail, t_ns)
        toxicity = abs(self._ofi.value())
        enough_updates = self._ofi.n_updates() >= self.cfg.toxicity_min_updates
        if enough_updates and toxicity >= self.cfg.toxicity_threshold:
            detail = f"|ofi| {toxicity:.3f} >= {self.cfg.toxicity_threshold:.3f}"
            self._trip("cb_toxicity", detail, t_ns)

    def _vol_spiked(self) -> bool:
        """True when both windows are full and short-term vol exceeds ``vol_ratio`` x long-term."""
        if not (self._short_vol.is_warm() and self._long_vol.is_warm()):
            return False
        long_sigma = self._long_vol.sigma()
        return long_sigma > 0 and self._short_vol.sigma() > self.cfg.vol_ratio * long_sigma

    def _maybe_ramp_up(self, t_ns: int) -> None:
        """Step up one ramp level once ``ramp_step_hours`` have passed since the last change."""
        last_level = len(self.cfg.ramp_levels) - 1
        if not self.cfg.size_ramp or self.killed or self.ramp_index >= last_level:
            return
        started = self._ramp_started_at
        if started is None or t_ns - started < self._ramp_step_ns:
            return
        self.ramp_index += 1
        self._ramp_started_at = t_ns
        level = self.cfg.ramp_levels[self.ramp_index]
        self.events.append(RiskEvent(t_ns, "ramp_up", f"{level}x"))

    def _size_multiplier(self) -> int:
        if not self.cfg.size_ramp:
            return 1
        return self.cfg.ramp_levels[self.ramp_index]

    def _trip(self, kind: str, detail: str, t_ns: int) -> None:
        """Pause both sides for ``cooldown_s`` and reset the ramp.

        Each kind is logged once per pause, so a condition that stays true does not flood the log.
        A trip after an earlier pause has ended starts a new pause and is logged again.
        """
        if not self.cfg.circuit_breakers:
            return
        if t_ns >= self.pause_until:
            self._logged_breakers.clear()
        self.pause_until = max(self.pause_until, t_ns + self._cooldown_ns)
        if kind not in self._logged_breakers:
            self.events.append(RiskEvent(t_ns, kind, detail))
            self._logged_breakers.add(kind)
        self.ramp_index = 0
        self._ramp_started_at = t_ns

    def _kill(self, kind: str, detail: str, t_ns: int) -> None:
        """Latch the kill switch. Only the first kill is recorded; the run stays killed."""
        if not self.cfg.kill_switches or self.killed:
            return
        self.killed = True
        self.kill_reason = kind
        self.events.append(RiskEvent(t_ns, kind, detail))

    def _check_drawdown(self, portfolio: Portfolio, t_ns: int) -> None:
        """Kill when equity falls more than the drawdown limit below its running peak.

        The peak starts at the first equity seen, so the limit is measured from there.
        """
        book = self._last_book
        limit = self.drawdown_limit_usd
        if book is None or limit is None:
            return
        equity = portfolio.equity(mid_price(self.spec, book))
        if self._peak_equity is None or equity > self._peak_equity:
            self._peak_equity = equity
        drawdown = self._peak_equity - equity
        if drawdown > limit:
            self._kill("kill_drawdown", f"drawdown {drawdown:.2f} > {limit:.2f} USD", t_ns)


def _seconds_to_ns(seconds: float) -> int:
    """Whole nanoseconds for a duration given in seconds."""
    return round(seconds * NS_PER_SECOND)


def _scaled(quote: Quote | None, multiplier: int) -> Quote | None:
    """The quote with its size multiplied by the ramp level, or ``None`` when there is no quote."""
    if quote is None:
        return None
    return Quote(quote.px, quote.qty * multiplier)


def _reducing_side(position: int) -> Side:
    """The side whose trade moves a non-zero position toward flat."""
    return Side.SELL if position > 0 else Side.BUY
