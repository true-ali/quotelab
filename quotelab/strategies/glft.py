"""Gueant-Lehalle-Fernandez-Tapia (GLFT) infinite-horizon quotes with an inventory bound.

Closed-form approximation from Gueant (2017), "Optimal market making", eq. 4.6-4.7, with the
risk-aversion parameter of the objective equal to ``gamma`` and orders of size ``delta``:

    c1 = 1/(gamma*delta) * ln(1 + gamma*delta/k)
    c2 = sqrt( gamma*sigma^2 / (2*A*delta*k) * (1 + gamma*delta/k)^(k/(gamma*delta) + 1) )
    bid offset = c1 + (2q + delta)/2 * c2
    ask offset = c1 - (2q - delta)/2 * c2

With ``delta = 1`` this is the GLFT (2013) result. The approximation is asymptotic: it is most
accurate for small inventories and less accurate near the inventory bound, where the exact
solution of the paper's ODE system bends away from these straight lines. Outside the bound the
strategy stops quoting the side that would add exposure (handled in ``QuotingStrategy``).

Units: q and delta in base-asset units, sigma in USD per sqrt-second, A in fills per second,
k in 1/USD, gamma in 1/(USD * base unit). Offsets come out in USD.
"""

from __future__ import annotations

import math

from quotelab.strategies.quoting import QuotingParams, QuotingStrategy
from quotelab.types import InstrumentSpec


def glft_coefficients(
    gamma: float, sigma: float, a: float, k: float, delta: float
) -> tuple[float, float]:
    """(c1, c2): c1 is the inventory-free half spread, c2 the offset change per unit of q.

    ``(1 + x)^(1/x + 1)`` is evaluated as ``exp((1/x + 1) * log1p(x))`` so it stays accurate when
    ``x = gamma*delta/k`` is tiny (it tends to e).
    """
    x = gamma * delta / k
    c1 = math.log1p(x) / (gamma * delta)
    growth = math.exp((1.0 / x + 1.0) * math.log1p(x))
    c2 = math.sqrt(gamma * sigma**2 / (2.0 * a * delta * k) * growth)
    return c1, c2


def glft_offsets(q: float, delta: float, c1: float, c2: float) -> tuple[float, float]:
    """(bid offset, ask offset) in USD for inventory ``q``; see the module docstring."""
    bid = c1 + (2.0 * q + delta) / 2.0 * c2
    ask = c1 - (2.0 * q - delta) / 2.0 * c2
    return bid, ask


def gamma_from_skew(target_skew: float, sigma: float, a: float, k: float, delta: float) -> float:
    """Risk aversion whose GLFT skew per order, ``delta * c2``, equals ``target_skew`` (USD).

    gamma has awkward units, so it is easier to choose "how many USD should one order of
    inventory move my quotes" and back out gamma. ``c2`` increases with gamma, so bisection on
    ``log(gamma)`` finds the unique solution.
    """
    if target_skew <= 0:
        raise ValueError("target_skew must be positive")

    def skew(log_gamma: float) -> float:
        return delta * glft_coefficients(math.exp(log_gamma), sigma, a, k, delta)[1]

    lo, hi = -60.0, 60.0
    if not skew(lo) < target_skew < skew(hi):
        raise ValueError("target skew is outside the range reachable by gamma in [e^-60, e^60]")
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if skew(mid) < target_skew:
            lo = mid
        else:
            hi = mid
    return math.exp((lo + hi) / 2.0)


class Glft(QuotingStrategy):
    """GLFT quotes with live sigma and a calibrated intensity ``A * exp(-k * delta)``."""

    def __init__(
        self, spec: InstrumentSpec, params: QuotingParams, gamma: float, a: float, k: float
    ):
        super().__init__(spec, params)
        self.gamma = gamma
        self.a = a
        self.k = k

    def offsets(self, sigma: float) -> tuple[float, float]:
        delta = self.order_qty()
        c1, c2 = glft_coefficients(self.gamma, sigma, self.a, self.k, delta)
        return glft_offsets(self.position_qty(), delta, c1, c2)
