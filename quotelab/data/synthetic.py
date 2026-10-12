"""Seeded synthetic top-of-book and trade generator.

Produces book and trade frames with the same columns and dtypes as processed real data, so
tests and the offline demo run the whole pipeline without network access. The model runs in
integer ticks and lots; floats appear only when rows are written.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from quotelab.config import instrument_spec
from quotelab.data.schema import BOOK_SCHEMA, TRADE_SCHEMA, processed_path
from quotelab.types import NS_PER_SECOND, Side

MOVE = 0
REFRESH = 1
TRADE = 2
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class SyntheticConfig:
    symbol: str
    seed: int
    start: str
    duration_s: float
    start_px: float
    spread_ticks: int
    move_rate_hz: float
    refresh_rate_hz: float
    trade_base_rate_hz: float
    hawkes_alpha: float
    hawkes_beta: float
    informed_fraction: float
    mean_trade_lots: float
    mean_level_lots: float
    sessions: int
    tick_size: float
    lot_size: float


@dataclass(frozen=True)
class Simulation:
    """Generated frames plus the exogenous move and trade times behind them.

    The move times and directions are exposed so informed flow can be verified against the
    real drivers, without changing the signature of generate.
    """

    book: pl.DataFrame
    trades: pl.DataFrame
    move_times: np.ndarray
    move_dirs: np.ndarray
    trade_times: np.ndarray


def synthetic_config(cfg: dict[str, Any]) -> SyntheticConfig:
    raw = cfg["synthetic"]
    spec = instrument_spec(cfg, raw["symbol"])
    return SyntheticConfig(**raw, tick_size=spec.tick_size, lot_size=spec.lot_size)


def start_datetime(start: str) -> datetime:
    parsed = datetime.fromisoformat(start)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def start_ns(start: str) -> int:
    """Exact integer nanoseconds since the epoch. Going through a float timestamp would lose
    precision, because 1.7e18 ns is beyond float64's exact integer range."""
    return (start_datetime(start) - EPOCH) // timedelta(microseconds=1) * 1000


def real_value(units: int, unit_size: float) -> float:
    """Ticks or lots to real units. Rounding to 10 decimals makes the conversion invert exactly."""
    return round(units * unit_size, 10)


def poisson_times(rate_hz: float, horizon_s: float, rng: np.random.Generator) -> np.ndarray:
    """Arrival times of a homogeneous Poisson process on [0, horizon_s).

    Draws the count and then places that many points uniformly. Sorted, this has the same law
    as cumulative exponential gaps and needs only two vectorized draws.
    """
    count = rng.poisson(rate_hz * horizon_s)
    return np.sort(rng.uniform(0.0, horizon_s, size=count))


def hawkes_times(
    mu: float, alpha: float, beta: float, horizon_s: float, rng: np.random.Generator
) -> np.ndarray:
    """Event times of a self-exciting process with intensity mu + sum alpha*exp(-beta*(t - t_i)).

    Simulated by Ogata thinning. Between events the excitation S(t) = sum alpha*exp(-beta*(t - t_i))
    only decays, so mu + S at the current time bounds the intensity until the next candidate.
    Each candidate is accepted with probability intensity / bound. S is updated recursively,
    so every step is O(1) instead of summing over all past events.

    The branching ratio alpha/beta must be below 1 for a stationary process. The expected count
    over horizon T is mu*T/(1 - alpha/beta). With alpha = 0 every candidate is accepted, which is
    a Poisson process.
    """
    if mu < 0:
        raise ValueError(f"mu must be >= 0, got {mu}")
    if alpha < 0:
        raise ValueError(f"alpha must be >= 0, got {alpha}")
    if beta <= 0:
        raise ValueError(f"beta must be > 0, got {beta}")
    if alpha / beta >= 1:
        raise ValueError(f"alpha/beta must be < 1 for a stationary process, got {alpha / beta}")
    times: list[float] = []
    t = 0.0
    excitation = 0.0
    while True:
        bound = mu + excitation
        if bound <= 0.0:
            break
        gap = rng.exponential(1.0 / bound)
        t += gap
        if t >= horizon_s:
            break
        excitation *= math.exp(-beta * gap)
        if rng.uniform() * bound < mu + excitation:
            times.append(t)
            excitation += alpha
    return np.array(times, dtype=float)


def draw_aggressor_sides(
    trade_times: np.ndarray,
    move_times: np.ndarray,
    move_dirs: np.ndarray,
    informed_fraction: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """Aggressor side (+1 BUY, -1 SELL) for each trade.

    An informed trade takes the direction of the next exogenous move after it. That plants
    adverse selection for later markout analysis. Uninformed trades, and informed trades with no
    later move, flip a fair coin. The trailing 0 appended to the directions marks "no next move".
    """
    count = len(trade_times)
    coin_sides = rng.choice([1, -1], size=count)
    is_informed = rng.random(count) < informed_fraction
    padded_dirs = np.append(move_dirs, 0)
    next_dirs = padded_dirs[np.searchsorted(move_times, trade_times, side="right")]
    follows_move = is_informed & (next_dirs != 0)
    return np.where(follows_move, next_dirs, coin_sides)


def draw_lots(mean_lots: float, rng: np.random.Generator) -> int:
    """Geometric on {1, 2, ...} with the given mean, so a level is never empty."""
    return int(rng.geometric(1.0 / mean_lots))


class _TopOfBook:
    """Integer top-of-book state. The ask is always the bid plus the configured spread."""

    def __init__(self, sc: SyntheticConfig, rng: np.random.Generator) -> None:
        self._sc = sc
        self._rng = rng
        self.bid_px = round(sc.start_px / sc.tick_size)
        self.bid_qty = 0
        self.ask_qty = 0
        self.redraw_sizes()

    @property
    def ask_px(self) -> int:
        return self.bid_px + self._sc.spread_ticks

    def redraw_sizes(self) -> None:
        self.bid_qty = draw_lots(self._sc.mean_level_lots, self._rng)
        self.ask_qty = draw_lots(self._sc.mean_level_lots, self._rng)

    def shift(self, sign: int) -> None:
        self.bid_px += sign

    def execute(self, side: int, lots: int) -> list[tuple[int, int]]:
        """Fill a market order level by level; return one (price in ticks, lots) per level.

        A BUY lifts the ask and a SELL hits the bid. Each level fills up to its displayed size.
        When a level is emptied, the touch steps one tick in the aggressor's direction with
        freshly drawn sizes and the rest of the order continues there, so a large order sweeps
        several ticks. That depth profile is what the fill-intensity calibration measures.
        """
        prints: list[tuple[int, int]] = []
        remaining = lots
        while remaining > 0:
            px, filled = self._take_touch(side, remaining)
            prints.append((px, filled))
            remaining -= filled
        return prints

    def _take_touch(self, side: int, lots: int) -> tuple[int, int]:
        is_buy = side == Side.BUY
        px = self.ask_px if is_buy else self.bid_px
        displayed = self.ask_qty if is_buy else self.bid_qty
        filled = min(lots, displayed)
        left = displayed - filled
        if is_buy:
            self.ask_qty = left
        else:
            self.bid_qty = left
        if left == 0:
            self.shift(side)
            self.redraw_sizes()
        return px, filled

    def row(self, ts: int) -> tuple[int, float, float, float, float]:
        sc = self._sc
        return (
            ts,
            real_value(self.bid_px, sc.tick_size),
            real_value(self.bid_qty, sc.lot_size),
            real_value(self.ask_px, sc.tick_size),
            real_value(self.ask_qty, sc.lot_size),
        )


def merged_events(
    move_times: np.ndarray, refresh_times: np.ndarray, trade_times: np.ndarray
) -> list[tuple[float, int, int]]:
    """Every exogenous and trade event as (time, kind, index), in time order."""
    tagged = [(t, MOVE, i) for i, t in enumerate(move_times.tolist())]
    tagged += [(t, REFRESH, i) for i, t in enumerate(refresh_times.tolist())]
    tagged += [(t, TRADE, i) for i, t in enumerate(trade_times.tolist())]
    return sorted(tagged)


def simulate(sc: SyntheticConfig) -> Simulation:
    """Run the whole model from one seeded RNG and return the frames with their drivers.

    The output depends only on the config because every draw comes from one default_rng(seed)
    in a fixed order: exogenous move times and directions, refresh times, Hawkes trade times,
    aggressor sides, trade sizes. The event loop then draws level sizes whenever the touch
    changes. Events from all sources are processed in time order. A market order emits one trade
    row per level it reaches, then the post-trade book row, all at the same ts.
    """
    rng = np.random.default_rng(sc.seed)
    move_times = poisson_times(sc.move_rate_hz, sc.duration_s, rng)
    move_dirs = rng.choice([1, -1], size=len(move_times))
    refresh_times = poisson_times(sc.refresh_rate_hz, sc.duration_s, rng)
    trade_times = hawkes_times(
        sc.trade_base_rate_hz, sc.hawkes_alpha, sc.hawkes_beta, sc.duration_s, rng
    )
    trade_sides = draw_aggressor_sides(
        trade_times, move_times, move_dirs, sc.informed_fraction, rng
    ).tolist()
    trade_lots = rng.geometric(1.0 / sc.mean_trade_lots, size=len(trade_times)).tolist()
    dirs = move_dirs.tolist()

    book = _TopOfBook(sc, rng)
    origin = start_ns(sc.start)
    book_rows = [book.row(origin)]
    trade_rows: list[tuple[int, float, float, bool]] = []
    for t, kind, index in merged_events(move_times, refresh_times, trade_times):
        ts = origin + round(t * NS_PER_SECOND)
        if kind == MOVE:
            book.shift(dirs[index])
            book.redraw_sizes()
        elif kind == REFRESH:
            book.redraw_sizes()
        else:
            side = trade_sides[index]
            for px, lots in book.execute(side, trade_lots[index]):
                trade_rows.append(
                    (
                        ts,
                        real_value(px, sc.tick_size),
                        real_value(lots, sc.lot_size),
                        side == Side.SELL,
                    )
                )
        book_rows.append(book.row(ts))
    return Simulation(
        book=pl.DataFrame(book_rows, schema=BOOK_SCHEMA, orient="row"),
        trades=pl.DataFrame(trade_rows, schema=TRADE_SCHEMA, orient="row"),
        move_times=move_times,
        move_dirs=move_dirs,
        trade_times=trade_times,
    )


def generate(sc: SyntheticConfig) -> tuple[pl.DataFrame, pl.DataFrame]:
    run = simulate(sc)
    return run.book, run.trades


def session_config(sc: SyntheticConfig, index: int) -> SyntheticConfig:
    """Session ``index``: same model, seed ``seed + index``, starting ``index`` days later.

    Each session stands in for one trading day, so synthetic data goes through the same
    walk-forward (calibrate on one day, test on the next) and by-day bootstrap as real data.
    The model is stationary, so this checks the plumbing, not out-of-sample generalisation.
    """
    start = start_datetime(sc.start) + timedelta(days=index)
    return replace(sc, seed=sc.seed + index, start=start.isoformat())


def synthetic_dates(sc: SyntheticConfig) -> list[str]:
    """The ``YYYY-MM-DD`` label of every session, in order."""
    return [
        start_datetime(session_config(sc, i).start).date().isoformat() for i in range(sc.sessions)
    ]


def write_synthetic(sc: SyntheticConfig, processed_dir: Path) -> list[Path]:
    """Write book and trades Parquet files for every session; return the paths written."""
    written: list[Path] = []
    for index, day in enumerate(synthetic_dates(sc)):
        book, trades = generate(session_config(sc, index))
        for kind, frame in (("book", book), ("trades", trades)):
            path = processed_path(processed_dir, sc.symbol, kind, day)
            path.parent.mkdir(parents=True, exist_ok=True)
            frame.write_parquet(path)
            written.append(path)
    return written
