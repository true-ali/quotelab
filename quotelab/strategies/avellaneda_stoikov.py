"""Avellaneda-Stoikov (2008) quotes, with a session horizon and a rolling horizon.

With time to horizon ``tau = T - t`` and orders of size ``delta``:

    reservation price  r = s - q * gamma * sigma^2 * tau
    total spread         = gamma * delta * sigma^2 * tau + (2/(gamma*delta)) * ln(1 + gamma*delta/k)

With ``delta = 1`` this is the paper's formula. The ``delta`` scaling is this project's
extension to orders of more than one unit (the same scaling Gueant uses for GLFT): a fill moves
inventory by ``delta`` and earns ``delta`` times the half spread, so ``gamma`` appears as
``gamma * delta`` in the spread terms. It is an assumption of this repo, not taken from the
A-S paper.

Crypto perpetuals never close, so ``T`` is a modelling choice:

* ``session``: ``T`` is the next 00:00 UTC. The skew is huge right after midnight and fades to
  zero just before it, which shows why the textbook finite horizon fits a 24/7 market badly.
* rolling: ``tau`` is a constant. This repo sets it so the A-S skew per order equals the GLFT
  skew per order at the calibrated sigma, which makes the two models comparable: they then
  quote identically at that sigma and differ only in how they react when sigma moves (the A-S
  skew scales with sigma^2, the GLFT skew with sigma).
"""

from __future__ import annotations

import math

from quotelab.strategies.quoting import QuotingParams, QuotingStrategy
from quotelab.types import NS_PER_SECOND, InstrumentSpec

SECONDS_PER_DAY = 86_400


def as_offsets(
    q: float, delta: float, gamma: float, sigma: float, k: float, tau: float
) -> tuple[float, float]:
    """(bid offset, ask offset) in USD around the mid: half spread plus or minus the skew."""
    skew = q * gamma * sigma**2 * tau
    half = gamma * delta * sigma**2 * tau / 2.0 + math.log1p(gamma * delta / k) / (gamma * delta)
    return half + skew, half - skew


def seconds_to_utc_midnight(t_ns: int) -> float:
    """Time left until the next 00:00 UTC; at exactly midnight a full day is left."""
    day_ns = SECONDS_PER_DAY * NS_PER_SECOND
    return (day_ns - t_ns % day_ns) / NS_PER_SECOND


class AvellanedaStoikov(QuotingStrategy):
    """A-S quotes. ``horizon_s=None`` selects the session horizon, a number the rolling one."""

    def __init__(
        self,
        spec: InstrumentSpec,
        params: QuotingParams,
        gamma: float,
        k: float,
        horizon_s: float | None,
    ):
        super().__init__(spec, params)
        self.gamma = gamma
        self.k = k
        self.horizon_s = horizon_s

    def tau(self) -> float:
        if self.horizon_s is None:
            return seconds_to_utc_midnight(self.now_ns)
        return self.horizon_s

    def offsets(self, sigma: float) -> tuple[float, float]:
        return as_offsets(
            self.position_qty(), self.order_qty(), self.gamma, sigma, self.k, self.tau()
        )
