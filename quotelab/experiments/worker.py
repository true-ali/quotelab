"""Work done inside one worker process: calibrate a day, measure its hourly volatility, run a job.

Every function takes plain, picklable arguments and writes its own outputs, so the runner can
send them to a process pool and only collect small results back.
"""

from __future__ import annotations

import resource
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from quotelab.analytics.attribution import COMPONENTS, reconcile, run_attribution
from quotelab.analytics.queries import HORIZONS_S, book_mids, markouts, period_pnl
from quotelab.analytics.stats import run_metrics
from quotelab.config import instrument_spec
from quotelab.data.loader import events_from_frames, load_day
from quotelab.data.schema import processed_path
from quotelab.engine.backtest import run_backtest
from quotelab.engine.config import engine_config
from quotelab.engine.recorder import write_run
from quotelab.experiments.jobs import Job, apply_overrides
from quotelab.fills.factory import fill_model_from_config
from quotelab.risk.base import NoRisk, RiskLayer
from quotelab.risk.manager import RiskManager, risk_config
from quotelab.strategies.calibration import (
    calibrate,
    calibration_config,
    mid_grid,
    read_calibration,
    write_calibration,
)
from quotelab.strategies.factory import build_strategy
from quotelab.types import NS_PER_SECOND

HOUR_NS = 3600 * NS_PER_SECOND


@dataclass(frozen=True)
class Dataset:
    """Where one dataset's inputs live and where its experiment outputs go."""

    name: str
    symbols: tuple[str, ...]
    dates: tuple[str, ...]
    processed_dir: Path
    out_dir: Path


def calibration_path(dataset: Dataset, symbol: str, date: str) -> Path:
    return dataset.out_dir / "calibration" / symbol / f"{date}.json"


def run_dir(dataset: Dataset, key: str) -> Path:
    return dataset.out_dir / "runs" / key


def calibrate_day(cfg: dict[str, Any], dataset: Dataset, symbol: str, date: str) -> str:
    """Fit the strategy parameters on one day and write them as JSON; return the path."""
    spec = instrument_spec(cfg, symbol)
    book, trades, _ = load_day(dataset.processed_dir, spec, date)
    klines_file = processed_path(dataset.processed_dir, symbol, "klines", date)
    klines = pl.read_parquet(klines_file) if klines_file.is_file() else None
    cal = calibrate(spec, book, trades, calibration_config(cfg), date, klines)
    path = calibration_path(dataset, symbol, date)
    write_calibration(cal, path)
    return str(path)


def hourly_variance(book: pl.DataFrame) -> pl.DataFrame:
    """Realised variance of the 1-second mid in each UTC hour: (period_start, realised_var).

    Each squared one-second change is assigned to the hour in which it ends.
    """
    grid, mids = mid_grid(book, NS_PER_SECOND)
    return (
        pl.DataFrame({"ts": grid[1:], "sq": np.diff(mids) ** 2})
        .group_by((pl.col("ts") // HOUR_NS * HOUR_NS).alias("period_start"))
        .agg(pl.col("sq").sum().alias("realised_var"))
        .sort("period_start")
    )


def market_hours_day(cfg: dict[str, Any], dataset: Dataset, symbol: str, date: str) -> pl.DataFrame:
    """Hourly realised variance of one test day, tagged with symbol and date."""
    spec = instrument_spec(cfg, symbol)
    book, _, _ = load_day(dataset.processed_dir, spec, date)
    return hourly_variance(book).with_columns(
        pl.lit(symbol).alias("symbol"), pl.lit(date).alias("test_date")
    )


def risk_layer(cfg: dict[str, Any], symbol: str, order_lots: int) -> RiskLayer:
    """The risk manager when any control is switched on, otherwise no risk layer at all.

    With every control off the manager would only re-apply the inventory bound that the
    strategy already enforces, so skipping it changes nothing but the run time.
    """
    rc = risk_config(cfg)
    controls_on = rc.kill_switches or rc.circuit_breakers or rc.size_ramp
    if controls_on or rc.hedge_threshold_orders is not None:
        return RiskManager(instrument_spec(cfg, symbol), rc, order_lots)
    return NoRisk()


def run_job(cfg: dict[str, Any], dataset: Dataset, job: Job, key: str) -> dict[str, Any]:
    """Run one job end to end, write its outputs under ``runs/<key>`` and return its metrics row.

    Raises ``AttributionMismatch`` if the PnL components miss the final equity by a cent.
    """
    started = time.perf_counter()
    job_cfg = apply_overrides(cfg, job.overrides)
    spec = instrument_spec(job_cfg, job.symbol)
    cal = read_calibration(calibration_path(dataset, job.symbol, job.calib_date))
    strategy = build_strategy(job_cfg["strategy"], spec, cal)
    order_lots = int(job_cfg["strategy"]["order_lots"][job.symbol])
    risk = risk_layer(job_cfg, job.symbol, order_lots)
    book, trades, funding = load_day(dataset.processed_dir, spec, job.test_date)
    events = events_from_frames(spec, book, trades, funding, window=job.window)
    result = run_backtest(
        events, strategy, fill_model_from_config(job_cfg), spec, engine_config(job_cfg), risk
    )
    result.summary.update(
        key=key,
        calib_date=job.calib_date,
        test_date=job.test_date,
        sigma_calibrated=cal.sigma,
        gamma=getattr(strategy, "gamma", None),
        as_horizon_s=getattr(strategy, "horizon_s", None),
        quote_updates=strategy.quote_updates,
        bid_clamps=strategy.bid_clamps,
        ask_clamps=strategy.ask_clamps,
        drawdown_limit_usd=getattr(risk, "drawdown_limit_usd", None),
    )
    gap = reconcile(result)
    out = run_dir(dataset, key)
    write_run(result, out)
    marks = markouts(result.fills, book_mids(book), job.symbol)
    marks.write_parquet(out / "markouts.parquet")
    period_pnl(result.timeseries, HOUR_NS).write_parquet(out / "hourly.parquet")
    return metrics_row(result, marks, gap, key, started)


def metrics_row(result, marks: pl.DataFrame, gap: float, key: str, started: float) -> dict:
    """Flat scalars for the metrics table: performance, attribution, markouts and run cost."""
    summary = result.summary
    parts = run_attribution(result)
    row: dict[str, Any] = {"key": key, "symbol": summary["symbol"]}
    row.update(run_metrics(result))
    row.update({f"attr_{name}": parts[name] for name in COMPONENTS})
    row["attr_gap"] = gap
    quote_marks = marks.filter(pl.col("kind") == "quote")
    for horizon in HORIZONS_S:
        at_horizon = quote_marks.filter(pl.col("horizon_s") == horizon)
        row[f"adverse_bps_{horizon}s"] = _mean_or_nan(at_horizon, "adverse_bps")
        row[f"markout_bps_{horizon}s"] = _mean_or_nan(at_horizon, "markout_bps")
    for name in (
        "market_events",
        "orders_sent",
        "throttled",
        "post_only_rejects",
        "quote_updates",
        "bid_clamps",
        "ask_clamps",
        "sigma_calibrated",
        "gamma",
        "as_horizon_s",
        "drawdown_limit_usd",
        "final_position",
    ):
        row[name] = summary[name]
    row["risk_events"] = result.risk_events.height
    row["kill_kind"] = _first_kill(result.risk_events)
    row["sim_wall_s"] = summary["wall_time_s"]
    row["job_wall_s"] = time.perf_counter() - started
    row["worker_peak_rss_mb"] = _peak_rss_mb()
    return row


def _mean_or_nan(frame: pl.DataFrame, column: str) -> float:
    return float(frame[column].mean()) if frame.height else float("nan")


def _first_kill(risk_events: pl.DataFrame) -> str | None:
    kills = risk_events.filter(pl.col("kind").str.starts_with("kill_"))
    return kills["kind"][0] if kills.height else None


def _peak_rss_mb() -> float:
    """Peak resident memory of this worker process so far, in MB.

    ``ru_maxrss`` is in bytes on macOS and in kilobytes on Linux. A pool worker runs many jobs,
    so this is the peak over every job it has run, an upper bound for the current one.
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    bytes_per_unit = 1 if sys.platform == "darwin" else 1024
    return peak * bytes_per_unit / 1_000_000
