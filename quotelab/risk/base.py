"""Interface between the backtest loop and the risk layer.

The risk layer sits on the trader side, between the strategy and the order manager. It sees
market data and fills when the trader sees them (after data latency), can veto or shrink the
strategy's quotes, and can request a taker hedge. ``NoRisk`` is the pass-through used when risk
controls are switched off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from quotelab.engine.portfolio import Portfolio
from quotelab.types import BookEvent, Fill, Quotes, Side, TradeEvent


@dataclass(frozen=True, slots=True)
class RiskEvent:
    """A logged risk decision, e.g. ``("kill_switch", "drawdown 512.3 > 500")``."""

    ts: int
    kind: str
    detail: str


@dataclass(frozen=True, slots=True)
class HedgeRequest:
    side: Side
    qty: int


class RiskLayer(Protocol):
    events: list[RiskEvent]

    def on_market(self, ev: BookEvent | TradeEvent, t_ns: int) -> None: ...

    def on_fill(self, fill: Fill, portfolio: Portfolio, t_ns: int) -> None: ...

    def on_timer(self, portfolio: Portfolio, t_ns: int) -> None: ...

    def filter_quotes(self, quotes: Quotes, portfolio: Portfolio, t_ns: int) -> Quotes:
        """The quotes actually sent: may drop a side, shrink sizes, or return no quotes."""
        ...

    def hedge(self, portfolio: Portfolio, t_ns: int) -> HedgeRequest | None: ...


@dataclass
class NoRisk:
    events: list[RiskEvent] = field(default_factory=list)

    def on_market(self, ev: BookEvent | TradeEvent, t_ns: int) -> None:
        return None

    def on_fill(self, fill: Fill, portfolio: Portfolio, t_ns: int) -> None:
        return None

    def on_timer(self, portfolio: Portfolio, t_ns: int) -> None:
        return None

    def filter_quotes(self, quotes: Quotes, portfolio: Portfolio, t_ns: int) -> Quotes:
        return quotes

    def hedge(self, portfolio: Portfolio, t_ns: int) -> HedgeRequest | None:
        return None
