"""Shared quoting machinery: fair value, online volatility, inventory, rounding, inventory bound.

Every concrete strategy only answers one question: given volatility ``sigma`` and the current
inventory, how far below and above fair value should the bid and the ask sit (in USD)? This
base class turns those two offsets into tick prices and applies the rules every strategy shares:

* fair value is the mid or the microprice, optionally shifted by ``ofi_beta`` times the
  normalised order-flow imbalance (a short-horizon signal, not an alpha claim);
* the bid is rounded down and the ask up to the tick grid, so rounding never makes a quote
  more aggressive than the model asked for;
* a quote that would cross the touch is pulled back one tick inside it (orders are post-only);
  ``bid_clamps`` and ``ask_clamps`` count how often that happens out of ``quote_updates``;
* a side is dropped when one more fill would take inventory beyond ``max_inventory_lots``.

Inventory is tracked from our own fills as the strategy receives them, so the strategy never
needs to see the exchange or the engine. That is what lets the same class run in the paper
runner.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from quotelab.strategies.signals import OfiTracker, RollingVol, microprice, mid_price
from quotelab.types import NS_PER_SECOND, BookEvent, Fill, InstrumentSpec, Quote, Quotes

FAIR_VALUE_KINDS = ("mid", "microprice")
ROUNDING_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class QuotingParams:
    """Settings shared by every strategy.

    ``sigma_fallback`` (USD per sqrt-second) is used until ``sigma_window_s`` of data has been
    seen; it normally comes from calibration on an earlier period. ``ofi_beta`` is in USD per
    unit of normalised OFI; zero switches the OFI adjustment off.
    """

    order_lots: int
    max_inventory_lots: int
    fair_value: str
    ofi_beta: float
    ofi_window_s: float
    sigma_window_s: float
    sigma_fallback: float


class QuotingStrategy:
    """Base strategy. Subclasses implement ``offsets(sigma)``."""

    def __init__(self, spec: InstrumentSpec, params: QuotingParams) -> None:
        if params.fair_value not in FAIR_VALUE_KINDS:
            raise ValueError(f"fair_value must be one of {FAIR_VALUE_KINDS}")
        if params.order_lots < 1 or params.max_inventory_lots < params.order_lots:
            raise ValueError("need order_lots >= 1 and max_inventory_lots >= order_lots")
        self.spec = spec
        self.params = params
        self.position = 0
        self.now_ns = 0
        self.book: BookEvent | None = None
        self.vol = RollingVol(params.sigma_window_s, params.sigma_fallback)
        self.ofi = OfiTracker(round(params.ofi_window_s * NS_PER_SECOND))
        self.quote_updates = 0
        self.bid_clamps = 0
        self.ask_clamps = 0

    def offsets(self, sigma: float) -> tuple[float, float]:
        """(bid offset, ask offset) below and above fair value, in USD."""
        raise NotImplementedError

    def on_book(self, ev: BookEvent) -> Quotes | None:
        self.book = ev
        self.now_ns = max(self.now_ns, ev.ts)
        self.vol.update(ev.ts, mid_price(self.spec, ev))
        if self.params.ofi_beta != 0.0:
            self.ofi.update(ev)
        return self.quotes()

    def on_trade(self, ev: object) -> Quotes | None:
        return None

    def on_fill(self, fill: Fill) -> Quotes | None:
        self.position += fill.side * fill.qty
        return self.quotes() if self.book is not None else None

    def on_timer(self, t_ns: int) -> Quotes | None:
        self.now_ns = max(self.now_ns, t_ns)
        return self.quotes() if self.book is not None else None

    def position_qty(self) -> float:
        """Inventory in base-asset units (for example BTC), the unit the pricing formulas use."""
        return self.spec.to_qty(self.position)

    def order_qty(self) -> float:
        return self.spec.to_qty(self.params.order_lots)

    def fair_value(self) -> float:
        if self.params.fair_value == "microprice":
            fair = microprice(self.spec, self.book)
        else:
            fair = mid_price(self.spec, self.book)
        return fair + self.params.ofi_beta * self.ofi.value()

    def quotes(self) -> Quotes:
        """Turn the model's offsets into post-only tick prices, then apply the inventory bound."""
        book = self.book
        fair = self.fair_value()
        bid_off, ask_off = self.offsets(self.vol.sigma())
        tick = self.spec.tick_size
        bid_px = math.floor((fair - bid_off) / tick + ROUNDING_EPS)
        ask_px = math.ceil((fair + ask_off) / tick - ROUNDING_EPS)
        self.quote_updates += 1
        if bid_px > book.ask_px - 1:
            bid_px = book.ask_px - 1
            self.bid_clamps += 1
        if ask_px < book.bid_px + 1:
            ask_px = book.bid_px + 1
            self.ask_clamps += 1
        size = self.params.order_lots
        limit = self.params.max_inventory_lots
        bid = Quote(bid_px, size) if self.position + size <= limit else None
        ask = Quote(ask_px, size) if self.position - size >= -limit else None
        return Quotes(bid, ask)
