"""Scalar statistics over equity curves, daily PnL series and single backtest runs."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import polars as pl

from quotelab.engine.recorder import BacktestResult


def max_drawdown(equity: np.ndarray | Sequence[float]) -> float:
    """Largest peak-to-trough fall of an equity curve, as a non-negative number.

    Returns 0 for an empty, flat or rising curve.
    """
    curve = np.asarray(equity, dtype=float)
    if curve.size == 0:
        return 0.0
    running_peak = np.maximum.accumulate(curve)
    return float(np.max(running_peak - curve))


def bootstrap_mean_ci(
    values: Sequence[float] | np.ndarray,
    n_boot: int = 10_000,
    level: float = 0.95,
    seed: int = 0,
) -> tuple[float, float, float]:
    """(mean, lo, hi) of the mean of `values` by resampling whole blocks with replacement.

    Each value is one block (for example one day's PnL), so this is a block bootstrap by day.
    Percentile interval. Uses np.random.default_rng(seed) so it is deterministic.
    With fewer than 2 values return (mean, nan, nan) since an interval from one block is
    meaningless. Empty input returns (nan, nan, nan).
    """
    sample = np.asarray(values, dtype=float)
    if sample.size == 0:
        return (float("nan"), float("nan"), float("nan"))
    mean = float(sample.mean())
    if sample.size < 2:
        return (mean, float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, sample.size, size=(n_boot, sample.size))
    resampled_means = sample[draws].mean(axis=1)
    tail = (1.0 - level) / 2.0
    lo, hi = np.quantile(resampled_means, [tail, 1.0 - tail])
    return (mean, float(lo), float(hi))


def run_metrics(result: BacktestResult) -> dict[str, float]:
    """Scalar performance metrics of one run, all computed from the run's own tables.

    ``fill_rate`` is quote fills per order sent. It counts fills, not orders, so an order that
    fills twice counts twice and the value can exceed 1.
    """
    fills = result.fills
    maker = fills.filter(pl.col("is_maker"))
    positions = result.timeseries["position"]
    summary = result.summary
    pnl = float(summary["final_equity"])
    quote_fills = maker.height
    orders_sent = summary["orders_sent"]
    maker_volume = float((maker["px"] * maker["qty"]).sum())
    return {
        "pnl": pnl,
        "quote_fills": float(quote_fills),
        "fill_rate": quote_fills / orders_sent if orders_sent else 0.0,
        "maker_volume_usd": maker_volume,
        "pnl_per_volume_bps": pnl / maker_volume * 10_000 if maker_volume else float("nan"),
        "max_drawdown_usd": max_drawdown(result.timeseries["equity"].to_numpy()),
        "mean_abs_inventory": float(positions.abs().mean()) if positions.len() else 0.0,
        "max_abs_inventory": float(positions.abs().max()) if positions.len() else 0.0,
        "uptime_both": float(summary["uptime_both"]),
        "fees": float(summary["fees"]),
        "funding": float(summary["funding"]),
        "hedge_fills": float(fills.filter(~pl.col("is_maker")).height),
    }
