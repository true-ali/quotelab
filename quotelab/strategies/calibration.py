"""Calibrate sigma, the fill intensity ``A * exp(-k * delta)`` and the OFI coefficient.

All three are fitted on one period (A) and used to run a later period (B), so a backtest never
uses parameters that saw its own data (walk-forward).

Volatility. The mid is sampled once per second (the last mid at or before each second).
``sigma`` is the root mean square of the one-second changes, in USD per sqrt-second. The mean
change over one second is negligible next to its spread, so this equals the standard deviation
in practice, and it matches what ``RollingVol`` computes online. When 1-minute klines are
available, the standard deviation of minute-close changes divided by sqrt(60) is a cross-check.

Fill intensity. A quote resting ``delta`` above the mid can only fill if a buyer's aggressive
order reaches that far. Our strategies requote on every book update, so the quote keeps its
distance from the current mid; the relevant rate is therefore "aggressive orders per second
whose deepest print is at least ``delta`` past the mid prevailing just before them". For each
``delta`` on a half-tick-offset grid:

    lambda(delta) = number of aggressive orders reaching delta / time span (seconds)

pooled over both sides (sells mirrored against the bid). An ordinary least-squares regression
of ``ln lambda`` on ``delta`` gives slope ``-k`` and intercept ``ln A``, with standard errors
and R^2. This is the brief's "log-linear regression of trade-arrival counts against distance
from mid".

A first-passage estimator (fix the mid at a start time and wait for a trade at ``mid + delta``)
was tried first and rejected: on BTCUSDT the mid diffuses about 100 ticks in a 10-second window,
so every level up to 20 ticks was reached by the mid wandering rather than by a sweep, and the
fitted curve was flat (R^2 = 0.26). It worked on the synthetic data only because the synthetic
mid moves about 36 times less.

Caveats (also in docs/MODEL.md): a hit means a taker reached the price, not that an order
queued there would have filled, so ``A`` is an optimistic touch intensity; and real flow has a
point mass at the touch (most takers stop at the best level), which a single exponential
cannot carry. The per-level table is kept in the output so the fit can be inspected.

OFI. The normalised order-flow imbalance over the trailing ``ofi_window_s`` (the same
definition as ``OfiTracker``) is regressed against the mid change over the next
``ofi_horizon_s`` on the one-second grid. The slope ``beta`` is USD per unit of normalised OFI.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from quotelab.types import NS_PER_SECOND, InstrumentSpec


@dataclass(frozen=True, slots=True)
class CalibrationConfig:
    max_delta_ticks: int
    min_hits: int
    ofi_window_s: float
    ofi_horizon_s: float


def calibration_config(cfg: dict[str, Any]) -> CalibrationConfig:
    raw = cfg["calibration"]
    return CalibrationConfig(
        max_delta_ticks=int(raw["max_delta_ticks"]),
        min_hits=int(raw["min_hits"]),
        ofi_window_s=float(cfg["strategy"]["ofi_window_s"]),
        ofi_horizon_s=float(raw["ofi_horizon_s"]),
    )


@dataclass(frozen=True, slots=True)
class LinearFit:
    """``y = intercept + slope * x`` by ordinary least squares, with standard errors."""

    intercept: float
    slope: float
    intercept_se: float
    slope_se: float
    r2: float
    n: int


@dataclass(frozen=True, slots=True)
class IntensityLevel:
    """Pooled excursion counts for one distance from the mid, with their rate per second."""

    delta_usd: float
    hits: int
    exposure_s: float
    rate: float


@dataclass(frozen=True, slots=True)
class Calibration:
    """Everything a strategy needs from period A, plus the fit diagnostics to report."""

    symbol: str
    period: str
    start_ts: int
    end_ts: int
    sigma: float
    sigma_klines: float | None
    a: float
    a_se: float
    k: float
    k_se: float
    intensity_r2: float
    intensity_levels_used: int
    levels: tuple[IntensityLevel, ...]
    ofi_beta: float
    ofi_beta_se: float
    ofi_r2: float


def fit_line(x: np.ndarray, y: np.ndarray) -> LinearFit:
    """Ordinary least squares of ``y`` on ``x`` with an intercept.

    Standard errors use the usual homoskedastic formula with ``n - 2`` degrees of freedom.
    """
    n = len(x)
    if n < 3:
        raise ValueError(f"need at least 3 points for a line with standard errors, got {n}")
    x_mean = x.mean()
    y_mean = y.mean()
    sxx = float(((x - x_mean) ** 2).sum())
    slope = float(((x - x_mean) * (y - y_mean)).sum() / sxx)
    intercept = float(y_mean - slope * x_mean)
    residuals = y - intercept - slope * x
    ss_res = float((residuals**2).sum())
    ss_tot = float(((y - y_mean) ** 2).sum())
    residual_var = ss_res / (n - 2)
    slope_se = math.sqrt(residual_var / sxx)
    intercept_se = math.sqrt(residual_var * (1.0 / n + x_mean**2 / sxx))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return LinearFit(intercept, slope, intercept_se, slope_se, r2, n)


def mid_grid(book: pl.DataFrame, step_ns: int) -> tuple[np.ndarray, np.ndarray]:
    """(grid timestamps, mid as of each one): the last mid at or before every grid point.

    The grid runs on whole multiples of ``step_ns`` from the first book update to the last.
    """
    ts = book["ts"].to_numpy()
    mids = ((book["bid_px"] + book["ask_px"]) / 2).to_numpy()
    first = -(-int(ts[0]) // step_ns) * step_ns
    grid = np.arange(first, int(ts[-1]) + 1, step_ns, dtype=np.int64)
    idx = np.searchsorted(ts, grid, side="right") - 1
    return grid, mids[idx]


def realised_sigma(grid_mids: np.ndarray, step_s: float) -> float:
    """Root mean square of the per-step mid changes, scaled to USD per sqrt-second."""
    changes = np.diff(grid_mids)
    return float(math.sqrt(np.mean(changes**2) / step_s))


def klines_sigma(klines: pl.DataFrame) -> float:
    """Cross-check: std of 1-minute close-to-close changes, in USD per sqrt-second."""
    closes = klines.sort("ts")["close"].to_numpy()
    return float(np.std(np.diff(closes), ddof=1) / math.sqrt(60.0))


def aggressive_order_excursions(
    spec: InstrumentSpec, book: pl.DataFrame, trades: pl.DataFrame
) -> np.ndarray:
    """How far past the prevailing mid each aggressive order reached, in half-ticks.

    Prints sharing a timestamp and aggressor side are one taker order sweeping several levels,
    so they are grouped and the deepest print is kept. The prevailing mid is the last book
    update strictly before the trade: the loader puts a trade before the book update stamped
    with the same time, because that update is the trade's result. The excursion is
    ``2 * side * (px - mid)`` in ticks, which is an exact integer (``bid + ask`` is the doubled
    mid), so comparing it with a level ``2 * delta`` never suffers float rounding.
    """
    book_ts = book["ts"].to_numpy()
    bid = np.rint(book["bid_px"].to_numpy() / spec.tick_size).astype(np.int64)
    ask = np.rint(book["ask_px"].to_numpy() / spec.tick_size).astype(np.int64)
    trade_ts = trades["ts"].to_numpy()
    px = np.rint(trades["px"].to_numpy() / spec.tick_size).astype(np.int64)
    buyer_took = ~trades["is_buyer_maker"].to_numpy()
    prevailing = np.searchsorted(book_ts, trade_ts, side="left") - 1
    has_book = prevailing >= 0
    doubled_mid = bid[prevailing] + ask[prevailing]
    excursion = np.where(buyer_took, 2 * px - doubled_mid, doubled_mid - 2 * px)
    orders = (
        pl.DataFrame(
            {
                "ts": trade_ts[has_book],
                "buy": buyer_took[has_book],
                "excursion": excursion[has_book],
            }
        )
        .group_by(["ts", "buy"])
        .agg(pl.col("excursion").max())
    )
    return orders["excursion"].to_numpy()


def fit_intensity(
    spec: InstrumentSpec,
    book: pl.DataFrame,
    trades: pl.DataFrame,
    cc: CalibrationConfig,
) -> tuple[LinearFit, tuple[IntensityLevel, ...]]:
    """Rate of aggressive orders reaching each distance from the mid, and its log-linear fit.

    Levels sit half a tick off the grid, ``delta_j = (j + 0.5)`` ticks, so on a one-tick market
    ``delta_0`` is the touch. The rate is per side per second: both sides are pooled and the
    count is divided by twice the time span of the book data.
    """
    excursions = aggressive_order_excursions(spec, book, trades)
    span_s = (int(book["ts"][-1]) - int(book["ts"][0])) / NS_PER_SECOND
    exposure_s = 2.0 * span_s
    levels2 = 2 * np.arange(cc.max_delta_ticks, dtype=np.int64) + 1
    hits = np.array([int(np.sum(excursions >= level)) for level in levels2], dtype=np.int64)
    delta_usd = levels2 * spec.tick_size / 2
    levels = tuple(
        IntensityLevel(float(d), int(h), exposure_s, float(h / exposure_s))
        for d, h in zip(delta_usd, hits, strict=True)
    )
    usable = hits >= cc.min_hits
    fit = fit_line(delta_usd[usable], np.log(hits[usable] / exposure_s))
    return fit, levels


def ofi_series(book: pl.DataFrame, spec: InstrumentSpec) -> pl.DataFrame:
    """Per-update OFI contribution in lots, same definition as ``signals.ofi_contribution``."""
    lots = book.select(
        "ts",
        (pl.col("bid_px") / spec.tick_size).round().cast(pl.Int64).alias("bp"),
        (pl.col("bid_qty") / spec.lot_size).round().cast(pl.Int64).alias("bq"),
        (pl.col("ask_px") / spec.tick_size).round().cast(pl.Int64).alias("ap"),
        (pl.col("ask_qty") / spec.lot_size).round().cast(pl.Int64).alias("aq"),
    )
    prev = {name: pl.col(name).shift(1) for name in ("bp", "bq", "ap", "aq")}
    bid_in = pl.when(pl.col("bp") >= prev["bp"]).then(pl.col("bq")).otherwise(0)
    bid_out = pl.when(pl.col("bp") <= prev["bp"]).then(prev["bq"]).otherwise(0)
    ask_in = pl.when(pl.col("ap") <= prev["ap"]).then(pl.col("aq")).otherwise(0)
    ask_out = pl.when(pl.col("ap") >= prev["ap"]).then(prev["aq"]).otherwise(0)
    return lots.select("ts", (bid_in - bid_out - ask_in + ask_out).alias("e")).slice(1)


def fit_ofi(book: pl.DataFrame, spec: InstrumentSpec, cc: CalibrationConfig) -> LinearFit:
    """Regress the next ``ofi_horizon_s`` mid change on trailing normalised OFI (1 s grid).

    A contribution stamped ``ts`` belongs to second bucket ``ceil(ts / 1s)``, so the bucket
    labelled ``t`` covers ``(t - 1s, t]``; summing the last ``ofi_window_s`` buckets gives the
    window ``(t - W, t]`` that ``OfiTracker`` uses online.
    """
    window = round(cc.ofi_window_s)
    horizon = round(cc.ofi_horizon_s)
    grid, mids = mid_grid(book, NS_PER_SECOND)
    buckets = (
        ofi_series(book, spec)
        .with_columns(((pl.col("ts") - 1) // NS_PER_SECOND + 1).alias("sec"))
        .group_by("sec")
        .agg(pl.col("e").sum().alias("net"), pl.col("e").abs().sum().alias("gross"))
    )
    seconds = pl.DataFrame({"sec": grid // NS_PER_SECOND, "mid": mids})
    joined = (
        seconds.join(buckets, on="sec", how="left")
        .fill_null(0)
        .with_columns(
            pl.col("net").rolling_sum(window, min_samples=window).alias("net_w"),
            pl.col("gross").rolling_sum(window, min_samples=window).alias("gross_w"),
            (pl.col("mid").shift(-horizon) - pl.col("mid")).alias("future_change"),
        )
        .drop_nulls()
        .filter(pl.col("gross_w") > 0)
    )
    ofi = (joined["net_w"] / joined["gross_w"]).to_numpy()
    return fit_line(ofi, joined["future_change"].to_numpy())


def calibrate(
    spec: InstrumentSpec,
    book: pl.DataFrame,
    trades: pl.DataFrame,
    cc: CalibrationConfig,
    period: str,
    klines: pl.DataFrame | None = None,
) -> Calibration:
    """Fit every parameter on one period of processed (real-unit) book and trade frames."""
    _, grid_mids = mid_grid(book, NS_PER_SECOND)
    intensity, levels = fit_intensity(spec, book, trades, cc)
    ofi = fit_ofi(book, spec, cc)
    a = math.exp(intensity.intercept)
    return Calibration(
        symbol=spec.symbol,
        period=period,
        start_ts=int(book["ts"][0]),
        end_ts=int(book["ts"][-1]),
        sigma=realised_sigma(grid_mids, 1.0),
        sigma_klines=klines_sigma(klines) if klines is not None and klines.height > 2 else None,
        a=a,
        a_se=a * intensity.intercept_se,
        k=-intensity.slope,
        k_se=intensity.slope_se,
        intensity_r2=intensity.r2,
        intensity_levels_used=intensity.n,
        levels=levels,
        ofi_beta=ofi.slope,
        ofi_beta_se=ofi.slope_se,
        ofi_r2=ofi.r2,
    )


def write_calibration(cal: Calibration, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(cal), indent=2))


def read_calibration(path: Path) -> Calibration:
    raw = json.loads(path.read_text())
    raw["levels"] = tuple(IntensityLevel(**level) for level in raw["levels"])
    return Calibration(**raw)
