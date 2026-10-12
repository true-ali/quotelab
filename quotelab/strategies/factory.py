"""Build a configured strategy from the YAML ``strategy`` section and a calibration.

The risk aversion ``gamma`` is not set directly by default. It is back-solved so that one
order of inventory moves the GLFT quotes by ``glft_skew_ticks_per_order`` ticks at the
calibrated sigma (see ``glft.gamma_from_skew``). The same gamma is used for A-S, whose rolling
horizon is then chosen so its skew per order matches GLFT's (``as_horizon: match_glft``).
"""

from __future__ import annotations

from typing import Any

from quotelab.strategies.avellaneda_stoikov import AvellanedaStoikov
from quotelab.strategies.calibration import Calibration
from quotelab.strategies.fixed import FixedSpread, InventorySkew
from quotelab.strategies.glft import Glft, gamma_from_skew, glft_coefficients
from quotelab.strategies.quoting import QuotingParams, QuotingStrategy
from quotelab.types import InstrumentSpec

STRATEGY_NAMES = ("fixed", "skew", "as", "glft")
AS_HORIZONS = ("match_glft", "session")


def quoting_params(
    strategy_cfg: dict[str, Any], spec: InstrumentSpec, cal: Calibration
) -> QuotingParams:
    order_lots = int(strategy_cfg["order_lots"][spec.symbol])
    return QuotingParams(
        order_lots=order_lots,
        max_inventory_lots=int(strategy_cfg["max_inventory_orders"]) * order_lots,
        fair_value=strategy_cfg["fair_value"],
        ofi_beta=cal.ofi_beta if strategy_cfg["use_ofi"] else 0.0,
        ofi_window_s=float(strategy_cfg["ofi_window_s"]),
        sigma_window_s=float(strategy_cfg["sigma_window_s"]),
        sigma_fallback=cal.sigma,
    )


def default_gamma(strategy_cfg: dict[str, Any], spec: InstrumentSpec, cal: Calibration) -> float:
    delta = spec.to_qty(int(strategy_cfg["order_lots"][spec.symbol]))
    target = float(strategy_cfg["glft_skew_ticks_per_order"]) * spec.tick_size
    return gamma_from_skew(target, cal.sigma, cal.a, cal.k, delta)


def matched_as_horizon(gamma: float, cal: Calibration, delta: float) -> float:
    """Rolling A-S horizon (s) whose skew per unit inventory, gamma*sigma^2*tau, equals c2."""
    _, c2 = glft_coefficients(gamma, cal.sigma, cal.a, cal.k, delta)
    return c2 / (gamma * cal.sigma**2)


def build_strategy(
    strategy_cfg: dict[str, Any],
    spec: InstrumentSpec,
    cal: Calibration,
    name: str | None = None,
    gamma: float | None = None,
) -> QuotingStrategy:
    """Strategy ``name`` (default ``strategy_cfg["name"]``); ``gamma`` overrides the default."""
    name = name or strategy_cfg["name"]
    params = quoting_params(strategy_cfg, spec, cal)
    if name == "fixed":
        return FixedSpread(spec, params, float(strategy_cfg["fixed_half_spread_ticks"]))
    if name == "skew":
        return InventorySkew(
            spec,
            params,
            float(strategy_cfg["skew_half_spread_ticks"]),
            float(strategy_cfg["skew_ticks_per_order"]),
        )
    gamma = gamma if gamma is not None else default_gamma(strategy_cfg, spec, cal)
    if name == "glft":
        return Glft(spec, params, gamma, cal.a, cal.k)
    if name == "as":
        horizon = strategy_cfg["as_horizon"]
        if horizon not in AS_HORIZONS:
            raise ValueError(f"as_horizon must be one of {AS_HORIZONS}, got {horizon!r}")
        horizon_s = None
        if horizon == "match_glft":
            horizon_s = matched_as_horizon(gamma, cal, spec.to_qty(params.order_lots))
        return AvellanedaStoikov(spec, params, gamma, cal.k, horizon_s)
    raise ValueError(f"unknown strategy {name!r}; expected one of {STRATEGY_NAMES}")
