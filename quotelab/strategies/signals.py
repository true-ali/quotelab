"""Online market signals computed from delivered top-of-book events only.

These are signals, not alpha claims: microprice and order-flow imbalance (OFI) are well-known,
public, short-horizon indicators of where the mid is likely to tick next. Every tracker here is
updated with events as the trader receives them, so nothing can look ahead.

Units: prices in quote currency (USD), volatility in USD per square-root second, OFI in lots.
"""

from __future__ import annotations

import math
from collections import deque

from quotelab.types import NS_PER_SECOND, BookEvent, InstrumentSpec


def mid_price(spec: InstrumentSpec, book: BookEvent) -> float:
    return (spec.to_price(book.bid_px) + spec.to_price(book.ask_px)) / 2


def microprice(spec: InstrumentSpec, book: BookEvent) -> float:
    """Size-weighted mid: ``(bid * ask_qty + ask * bid_qty) / (bid_qty + ask_qty)``.

    A large bid queue against a thin ask pulls the value toward the ask, because the ask is the
    level more likely to be consumed next. Falls back to the mid when both sizes are zero.
    """
    total = book.bid_qty + book.ask_qty
    if total == 0:
        return mid_price(spec, book)
    bid = spec.to_price(book.bid_px)
    ask = spec.to_price(book.ask_px)
    return (bid * book.ask_qty + ask * book.bid_qty) / total


def ofi_contribution(prev: BookEvent, book: BookEvent) -> int:
    """Order-flow imbalance of one top-of-book update, in lots (Cont, Kukanov, Stoikov 2014).

    Positive values mean buying pressure: the bid grew or moved up, or the ask shrank or moved
    up. Each side contributes the size added at the new level minus the size removed at the old
    one, depending on which way the price moved.
    """
    bid_in = book.bid_qty if book.bid_px >= prev.bid_px else 0
    bid_out = prev.bid_qty if book.bid_px <= prev.bid_px else 0
    ask_in = book.ask_qty if book.ask_px <= prev.ask_px else 0
    ask_out = prev.ask_qty if book.ask_px >= prev.ask_px else 0
    return bid_in - bid_out - ask_in + ask_out


class OfiTracker:
    """Normalised OFI over a trailing time window: ``sum(e) / sum(|e|)``, in [-1, 1].

    Normalising by gross flow makes the value comparable across symbols and across quiet and
    busy periods. It is 0 when there has been no flow in the window.
    """

    def __init__(self, window_ns: int) -> None:
        if window_ns <= 0:
            raise ValueError(f"window_ns must be positive, got {window_ns}")
        self.window_ns = window_ns
        self._prev: BookEvent | None = None
        self._items: deque[tuple[int, int]] = deque()
        self._net = 0
        self._gross = 0

    def update(self, book: BookEvent) -> None:
        if self._prev is not None:
            e = ofi_contribution(self._prev, book)
            if e != 0:
                self._items.append((book.ts, e))
                self._net += e
                self._gross += abs(e)
        self._prev = book
        self._expire(book.ts)

    def value(self) -> float:
        return self._net / self._gross if self._gross > 0 else 0.0

    def n_updates(self) -> int:
        """Number of non-zero contributions in the window; a few updates make ``value`` noisy."""
        return len(self._items)

    def _expire(self, now_ns: int) -> None:
        """Drop contributions at or before ``now - window`` so the window is (now - W, now]."""
        cutoff = now_ns - self.window_ns
        while self._items and self._items[0][0] <= cutoff:
            _, e = self._items.popleft()
            self._net -= e
            self._gross -= abs(e)


class RollingVol:
    """Realised volatility of the mid in USD per sqrt(second) over a trailing window.

    The mid is sampled on a fixed time grid (``sample_ns``, default one second). The mid as of
    a grid point is the last mid seen before it. A move spanning several grid steps (a quiet
    gap) is counted as one squared change over that many seconds, so a gap neither inflates nor
    deflates the per-second variance. ``sigma`` returns ``fallback`` until the window has filled,
    which is how a calibrated value bridges the warm-up without looking ahead.
    """

    def __init__(self, window_s: float, fallback: float, sample_ns: int = NS_PER_SECOND) -> None:
        if window_s <= 0 or sample_ns <= 0:
            raise ValueError("window_s and sample_ns must be positive")
        self.window_s = window_s
        self.fallback = fallback
        self.sample_ns = sample_ns
        self._grid_ts: int | None = None
        self._grid_mid = 0.0
        self._mid = 0.0
        self._moves: deque[tuple[float, float]] = deque()
        self._sum_sq = 0.0
        self._sum_s = 0.0

    def update(self, ts: int, mid: float) -> None:
        if self._grid_ts is None:
            self._grid_ts = ts - ts % self.sample_ns
            self._grid_mid = mid
            self._mid = mid
            return
        if ts >= self._grid_ts + self.sample_ns:
            steps = (ts - self._grid_ts) // self.sample_ns
            self._add((self._mid - self._grid_mid) ** 2, steps * self.sample_ns / NS_PER_SECOND)
            self._grid_ts += steps * self.sample_ns
            self._grid_mid = self._mid
        self._mid = mid

    def is_warm(self) -> bool:
        return self._sum_s >= self.window_s

    def sigma(self) -> float:
        if not self.is_warm():
            return self.fallback
        return math.sqrt(max(self._sum_sq, 0.0) / self._sum_s)

    def _add(self, squared_move: float, seconds: float) -> None:
        """Append one sampled move and drop the oldest while the rest still cover the window."""
        self._moves.append((squared_move, seconds))
        self._sum_sq += squared_move
        self._sum_s += seconds
        while self._moves and self._sum_s - self._moves[0][1] >= self.window_s:
            old_sq, old_s = self._moves.popleft()
            self._sum_sq -= old_sq
            self._sum_s -= old_s
