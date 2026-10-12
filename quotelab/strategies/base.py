"""The one strategy interface used by both the backtest engine and the paper runner.

A strategy only ever receives events. It never sees the clock, the simulated exchange or
the fill model, which is what lets the same class run unchanged in ``engine/`` and ``live/``.
Each callback returns the desired quotes, or ``None`` to leave current orders unchanged.
"""

from __future__ import annotations

from typing import Protocol

from quotelab.types import BookEvent, Fill, Quotes, TradeEvent


class Strategy(Protocol):
    def on_book(self, ev: BookEvent) -> Quotes | None: ...

    def on_trade(self, ev: TradeEvent) -> Quotes | None: ...

    def on_fill(self, fill: Fill) -> Quotes | None: ...

    def on_timer(self, t_ns: int) -> Quotes | None: ...
