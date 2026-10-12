"""Baseline strategies: a fixed symmetric spread, and the same spread with a linear skew."""

from __future__ import annotations

from quotelab.strategies.quoting import QuotingParams, QuotingStrategy
from quotelab.types import InstrumentSpec


class FixedSpread(QuotingStrategy):
    """Quote ``half_spread_ticks`` either side of fair value, whatever the inventory."""

    def __init__(self, spec: InstrumentSpec, params: QuotingParams, half_spread_ticks: float):
        super().__init__(spec, params)
        self.half_spread = half_spread_ticks * spec.tick_size

    def offsets(self, sigma: float) -> tuple[float, float]:
        return self.half_spread, self.half_spread


class InventorySkew(QuotingStrategy):
    """Fixed spread with both quotes shifted against inventory by ``skew_ticks_per_order``.

    Holding ``n`` orders' worth of inventory moves the bid down and the ask down by ``n`` times
    the skew when long (and up when short), so the side that reduces inventory becomes more
    likely to fill. This is the heuristic that A-S and GLFT derive from first principles.
    """

    def __init__(
        self,
        spec: InstrumentSpec,
        params: QuotingParams,
        half_spread_ticks: float,
        skew_ticks_per_order: float,
    ):
        super().__init__(spec, params)
        self.half_spread = half_spread_ticks * spec.tick_size
        self.skew_per_order = skew_ticks_per_order * spec.tick_size

    def offsets(self, sigma: float) -> tuple[float, float]:
        skew = self.skew_per_order * self.position / self.params.order_lots
        return self.half_spread + skew, self.half_spread - skew
