"""Core event and order types shared by every module.

Conventions (the whole codebase relies on these):

* Timestamps are integer nanoseconds since the Unix epoch (``int``).
* Prices are integer ticks and quantities are integer lots. Convert to
  quote-currency floats only at the edges with an ``InstrumentSpec``.
* ``Side.BUY == 1`` and ``Side.SELL == -1`` so a side can be used directly as a
  sign in PnL and inventory arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

NS_PER_MS = 1_000_000
NS_PER_SECOND = 1_000_000_000


class Side(IntEnum):
    """Order or aggressor side. The integer value is the sign of the position change."""

    BUY = 1
    SELL = -1


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Exchange trading rules for one symbol, used to convert ticks/lots to real units."""

    symbol: str
    tick_size: float
    lot_size: float
    min_notional: float

    def to_price(self, ticks: int) -> float:
        return ticks * self.tick_size

    def to_ticks(self, price: float) -> int:
        """Nearest tick. Callers that need a passive rounding direction must round themselves."""
        return round(price / self.tick_size)

    def to_qty(self, lots: int) -> float:
        return lots * self.lot_size

    def to_lots(self, qty: float) -> int:
        return round(qty / self.lot_size)

    def notional(self, ticks: int, lots: int) -> float:
        return self.to_price(ticks) * self.to_qty(lots)


@dataclass(frozen=True, slots=True)
class BookEvent:
    """Top-of-book snapshot (best bid/ask and displayed sizes) at exchange time ``ts``."""

    ts: int
    symbol: str
    bid_px: int
    bid_qty: int
    ask_px: int
    ask_qty: int


@dataclass(frozen=True, slots=True)
class TradeEvent:
    """A public trade print. ``aggressor`` is the taker's side: BUY means it lifted the ask."""

    ts: int
    symbol: str
    px: int
    qty: int
    aggressor: Side


@dataclass(frozen=True, slots=True)
class FundingEvent:
    """Perpetual funding settlement. Longs pay ``rate * notional`` when ``rate > 0``."""

    ts: int
    symbol: str
    rate: float


Event = BookEvent | TradeEvent | FundingEvent


@dataclass(frozen=True, slots=True)
class Quote:
    """One desired resting order: price in ticks, size in lots."""

    px: int
    qty: int


@dataclass(frozen=True, slots=True)
class Quotes:
    """The strategy's desired two-sided state. ``None`` on a side means "no order on that side"."""

    bid: Quote | None
    ask: Quote | None


@dataclass(frozen=True, slots=True)
class Fill:
    """An execution of one of our orders.

    ``fee`` is in quote currency and is positive when paid, negative for a rebate.
    """

    ts: int
    symbol: str
    order_id: int
    side: Side
    px: int
    qty: int
    fee: float
    is_maker: bool
