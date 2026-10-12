"""Experiment tables computed from one dataset's run outputs.

Everything the figures, ``RESULTS.md`` and the dashboard show comes from these functions, which
only read what ``quotelab backtest`` wrote under ``runs/<dataset>/``. Nothing here re-runs a
backtest, so the report is cheap to regenerate.

Daily statistics treat each test day as one block. A confidence interval is the block bootstrap
of the mean over days (``bootstrap_mean_ci``), so it is only as good as the number of days.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from quotelab.analytics.queries import markout_summary
from quotelab.analytics.stats import bootstrap_mean_ci

SUMMARY_COLUMNS = (
    "pnl_before_fees",
    "quote_fills",
    "fill_rate",
    "maker_volume_usd",
    "attr_spread",
    "attr_inventory",
    "attr_fees",
    "attr_hedging",
    "attr_funding",
    "adverse_bps_5s",
    "markout_bps_5s",
    "mean_abs_inventory",
    "max_drawdown_usd",
    "uptime_both",
    "gamma",
)
SENSITIVITY_PREFIXES = {
    "skew_ticks_": "skew_ticks_per_order",
    "size_x": "size_multiplier",
    "max_inventory_": "max_inventory_orders",
}
BPS = 10_000


@dataclass(frozen=True)
class RunOutputs:
    """One dataset's experiment outputs: ``jobs`` has one row per job with its run metrics."""

    out_dir: Path
    jobs: pl.DataFrame
    market_hours: pl.DataFrame


def load_outputs(out_dir: Path) -> RunOutputs:
    """Join ``jobs.parquet`` with ``metrics.parquet`` and add the PnL before maker fees."""
    jobs = pl.read_parquet(out_dir / "jobs.parquet")
    metrics = pl.read_parquet(out_dir / "metrics.parquet").drop("symbol")
    joined = jobs.join(metrics, on="key", how="left", maintain_order="left").with_columns(
        (pl.col("pnl") - pl.col("attr_fees")).alias("pnl_before_fees")
    )
    return RunOutputs(out_dir, joined, pl.read_parquet(out_dir / "market_hours.parquet"))


def experiment_rows(outputs: RunOutputs, experiment: str) -> pl.DataFrame:
    return outputs.jobs.filter(pl.col("experiment") == experiment)


def daily_summary(rows: pl.DataFrame, value: str = "pnl") -> pl.DataFrame:
    """Per (variant, symbol): number of days, mean daily ``value`` with its bootstrap CI, and
    the mean of every ``SUMMARY_COLUMNS`` column. Rows keep the order variants were defined in.
    """
    present = [name for name in SUMMARY_COLUMNS if name in rows.columns]
    grouped = rows.group_by("variant", "symbol", maintain_order=True).agg(
        pl.col(value).alias("_values"),
        pl.len().alias("n_days"),
        *[pl.col(name).mean() for name in present],
    )
    intervals = [bootstrap_mean_ci(values.to_list()) for values in grouped["_values"]]
    return grouped.drop("_values").with_columns(
        pl.Series(f"{value}_mean", [mean for mean, _, _ in intervals]),
        pl.Series(f"{value}_lo", [lo for _, lo, _ in intervals]),
        pl.Series(f"{value}_hi", [hi for _, _, hi in intervals]),
    )


def realism_ladder_table(outputs: RunOutputs) -> pl.DataFrame:
    """E1: daily summary per rung, plus the change in mean daily PnL from the previous rung."""
    table = daily_summary(experiment_rows(outputs, "E1"))
    return table.with_columns(
        (pl.col("pnl_mean") - pl.col("pnl_mean").shift(1).over("symbol")).alias(
            "change_vs_previous"
        )
    )


def strategy_table(outputs: RunOutputs) -> pl.DataFrame:
    """E2: daily summary per strategy, plus how often the quote had to be clamped to the touch."""
    rows = experiment_rows(outputs, "E2")
    clamps = rows.group_by("variant", "symbol", maintain_order=True).agg(
        ((pl.col("bid_clamps") + pl.col("ask_clamps")).sum() / pl.col("quote_updates").sum()).alias(
            "clamp_share"
        )
    )
    return daily_summary(rows).join(clamps, on=["variant", "symbol"], maintain_order="left")


def sensitivity_table(outputs: RunOutputs) -> pl.DataFrame:
    """E3: daily summary per setting, with the varied parameter and its value split out."""
    table = daily_summary(experiment_rows(outputs, "E3"))
    parameters, values = [], []
    for variant in table["variant"]:
        prefix = next(p for p in SENSITIVITY_PREFIXES if variant.startswith(p))
        parameters.append(SENSITIVITY_PREFIXES[prefix])
        values.append(float(variant.removeprefix(prefix)))
    return table.with_columns(pl.Series("parameter", parameters), pl.Series("value", values)).sort(
        "parameter", "symbol", "value", maintain_order=True
    )


def latency_table(outputs: RunOutputs) -> pl.DataFrame:
    """E4: daily summary per order latency, sorted by latency."""
    table = daily_summary(experiment_rows(outputs, "E4"))
    latency_ms = [float(v.removeprefix("latency_").removesuffix("ms")) for v in table["variant"]]
    return table.with_columns(pl.Series("latency_ms", latency_ms)).sort("symbol", "latency_ms")


def regime_labels(market_hours: pl.DataFrame) -> pl.DataFrame:
    """Each test-day hour labelled ``high`` or ``low`` volatility against its symbol's median.

    The label uses the hour's own realised variance, so it is a description of what happened,
    not something a strategy could have known in advance.
    """
    median = pl.col("realised_var").median().over("symbol")
    return market_hours.with_columns(
        median.alias("median_var"),
        pl.when(pl.col("realised_var") > median)
        .then(pl.lit("high"))
        .otherwise(pl.lit("low"))
        .alias("regime"),
    )


def hourly_pnl(outputs: RunOutputs, experiment: str) -> pl.DataFrame:
    """Hourly PnL of every run of ``experiment``, tagged with variant, symbol and test day."""
    frames = []
    for row in experiment_rows(outputs, experiment).iter_rows(named=True):
        hourly = pl.read_parquet(outputs.out_dir / "runs" / row["key"] / "hourly.parquet")
        frames.append(
            hourly.with_columns(
                pl.lit(row["variant"]).alias("variant"),
                pl.lit(row["symbol"]).alias("symbol"),
                pl.lit(row["test_date"]).alias("test_date"),
            )
        )
    return pl.concat(frames)


def regime_table(outputs: RunOutputs) -> pl.DataFrame:
    """E5: mean hourly PnL of each strategy in high- and low-volatility hours.

    The interval is a block bootstrap over days of each day's mean hourly PnL in that regime.
    """
    labels = regime_labels(outputs.market_hours).select(
        "symbol", "test_date", "period_start", "regime"
    )
    hours = hourly_pnl(outputs, "E5").join(labels, on=["symbol", "test_date", "period_start"])
    per_day = hours.group_by("variant", "symbol", "regime", "test_date", maintain_order=True).agg(
        pl.col("pnl").mean().alias("day_mean"), pl.len().alias("hours")
    )
    grouped = per_day.group_by("variant", "symbol", "regime", maintain_order=True).agg(
        pl.col("day_mean").alias("_values"),
        pl.len().alias("n_days"),
        pl.col("hours").sum().alias("n_hours"),
    )
    intervals = [bootstrap_mean_ci(values.to_list()) for values in grouped["_values"]]
    return (
        grouped.drop("_values")
        .with_columns(
            pl.Series("pnl_per_hour_mean", [m for m, _, _ in intervals]),
            pl.Series("pnl_per_hour_lo", [lo for _, lo, _ in intervals]),
            pl.Series("pnl_per_hour_hi", [hi for _, _, hi in intervals]),
        )
        .sort("symbol", "regime", maintain_order=True)
    )


def event_windows(outputs: RunOutputs) -> pl.DataFrame:
    """E6: per symbol, the replayed window and the most violent hour inside it."""
    windows = (
        experiment_rows(outputs, "E6")
        .group_by("symbol", maintain_order=True)
        .agg(pl.col("test_date", "window_start", "window_end").first())
    )
    violent = (
        outputs.market_hours.sort("realised_var", descending=True)
        .group_by("symbol", maintain_order=True)
        .first()
    )
    return windows.join(
        violent.select("symbol", "period_start", "realised_var"), on="symbol"
    ).rename({"period_start": "violent_hour_start"})


def event_study_table(outputs: RunOutputs) -> pl.DataFrame:
    """E6: risk controls off against on in the event window, with the risk events by kind."""
    rows = experiment_rows(outputs, "E6")
    counts = []
    for row in rows.iter_rows(named=True):
        events = pl.read_parquet(outputs.out_dir / "runs" / row["key"] / "risk_events.parquet")
        by_kind = events.group_by("kind").len().sort("kind")
        counts.append(", ".join(f"{k} {n}" for k, n in by_kind.iter_rows()) or "none")
    return rows.select(
        "symbol",
        "variant",
        "pnl",
        "max_drawdown_usd",
        "quote_fills",
        "mean_abs_inventory",
        "risk_events",
        "kill_kind",
        "drawdown_limit_usd",
    ).with_columns(pl.Series("events_by_kind", counts))


def event_study_series(outputs: RunOutputs) -> dict[tuple[str, str], dict[str, pl.DataFrame]]:
    """E6: the equity timeseries and risk events of each (symbol, variant) run, for the figure."""
    series = {}
    for row in experiment_rows(outputs, "E6").iter_rows(named=True):
        run = outputs.out_dir / "runs" / row["key"]
        series[(row["symbol"], row["variant"])] = {
            "timeseries": pl.read_parquet(run / "timeseries.parquet"),
            "risk_events": pl.read_parquet(run / "risk_events.parquet"),
        }
    return series


def fee_tier_table(outputs: RunOutputs, cfg: dict[str, Any]) -> pl.DataFrame:
    """E7: mean daily PnL at each maker fee in the grid, computed from the baseline runs.

    PnL(m) = PnL(base) + (base_bps - m) / 10000 * maker volume. This is exact when the fills do
    not depend on the fee, which holds here: no strategy reads the fee and the baseline has no
    risk layer. The reruns in ``fee_rerun_check`` test that claim.
    """
    base_bps = float(cfg["fees"]["maker_bps"])
    baseline = experiment_rows(outputs, "E7").filter(pl.col("variant") == "baseline")
    rows = []
    for symbol in baseline["symbol"].unique(maintain_order=True):
        days = baseline.filter(pl.col("symbol") == symbol)
        for maker_bps in cfg["experiments"]["e7_maker_bps"]:
            daily = days["pnl"] + (base_bps - maker_bps) / BPS * days["maker_volume_usd"]
            mean, lo, hi = bootstrap_mean_ci(daily.to_list())
            rows.append(
                {
                    "symbol": symbol,
                    "maker_bps": float(maker_bps),
                    "n_days": days.height,
                    "pnl_mean": mean,
                    "pnl_lo": lo,
                    "pnl_hi": hi,
                }
            )
    return pl.DataFrame(rows)


def break_even_maker_bps(outputs: RunOutputs, cfg: dict[str, Any]) -> pl.DataFrame:
    """E7: the maker fee at which the baseline's total PnL over all test days would be zero."""
    base_bps = float(cfg["fees"]["maker_bps"])
    return (
        experiment_rows(outputs, "E7")
        .filter(pl.col("variant") == "baseline")
        .group_by("symbol", maintain_order=True)
        .agg(
            pl.col("pnl").sum().alias("total_pnl"),
            pl.col("maker_volume_usd").sum().alias("total_maker_volume_usd"),
        )
        .with_columns(
            (base_bps + pl.col("total_pnl") / pl.col("total_maker_volume_usd") * BPS).alias(
                "break_even_maker_bps"
            )
        )
    )


def fee_rerun_check(outputs: RunOutputs, cfg: dict[str, Any]) -> pl.DataFrame:
    """E7: each real rerun at a different maker fee against the formula's prediction."""
    base_bps = float(cfg["fees"]["maker_bps"])
    e7 = experiment_rows(outputs, "E7")
    baseline = e7.filter(pl.col("variant") == "baseline").select(
        "symbol",
        "test_date",
        pl.col("pnl").alias("baseline_pnl"),
        pl.col("maker_volume_usd").alias("baseline_volume"),
        pl.col("quote_fills").alias("baseline_fills"),
    )
    reruns = e7.filter(pl.col("variant") != "baseline").with_columns(
        pl.col("variant")
        .str.strip_prefix("maker_")
        .str.strip_suffix("bps")
        .cast(pl.Float64)
        .alias("maker_bps")
    )
    return (
        reruns.join(baseline, on=["symbol", "test_date"])
        .with_columns(
            (
                pl.col("baseline_pnl")
                + (base_bps - pl.col("maker_bps")) / BPS * pl.col("baseline_volume")
            ).alias("predicted_pnl")
        )
        .select(
            "symbol",
            "test_date",
            "maker_bps",
            pl.col("pnl").alias("rerun_pnl"),
            "predicted_pnl",
            (pl.col("pnl") - pl.col("predicted_pnl")).abs().alias("abs_difference"),
            (pl.col("quote_fills") == pl.col("baseline_fills")).alias("same_fills"),
        )
    )


def calibration_table(outputs: RunOutputs) -> pl.DataFrame:
    """Every calibration day's fitted parameters and fit quality, read from the saved JSON.

    ``touch_hit_share`` is the share of all trade hits that landed at the first distance level.
    """
    rows = []
    for path in sorted((outputs.out_dir / "calibration").glob("*/*.json")):
        cal = json.loads(path.read_text())
        hits = [level["hits"] for level in cal["levels"]]
        rows.append(
            {
                "symbol": cal["symbol"],
                "date": cal["period"],
                "sigma": cal["sigma"],
                "sigma_klines": cal["sigma_klines"],
                "a": cal["a"],
                "a_se": cal["a_se"],
                "k": cal["k"],
                "k_se": cal["k_se"],
                "intensity_r2": cal["intensity_r2"],
                "levels_used": cal["intensity_levels_used"],
                "touch_hit_share": hits[0] / sum(hits) if hits and sum(hits) else None,
                "ofi_beta": cal["ofi_beta"],
                "ofi_beta_se": cal["ofi_beta_se"],
                "ofi_r2": cal["ofi_r2"],
            }
        )
    return pl.DataFrame(rows, infer_schema_length=None)


def intensity_levels(outputs: RunOutputs, cfg: dict[str, Any]) -> pl.DataFrame:
    """The measured fill intensity at each distance from mid, with the day's fitted A and k.

    One row per (symbol, calibration day, distance level): delta_usd, hits, rate (hits per
    second of exposure), whether the level had enough hits to enter the fit (``fitted``), and
    the a and k fitted on that day, for the calibration figure.
    """
    min_hits = int(cfg["calibration"]["min_hits"])
    rows = []
    for path in sorted((outputs.out_dir / "calibration").glob("*/*.json")):
        cal = json.loads(path.read_text())
        for level in cal["levels"]:
            rows.append(
                {
                    "symbol": cal["symbol"],
                    "date": cal["period"],
                    "delta_usd": level["delta_usd"],
                    "hits": level["hits"],
                    "rate": level["rate"],
                    "fitted": level["hits"] >= min_hits,
                    "a": cal["a"],
                    "k": cal["k"],
                }
            )
    return pl.DataFrame(rows, infer_schema_length=None)


def baseline_markouts(outputs: RunOutputs) -> pl.DataFrame:
    """Markout summary of the baseline's quote fills over all test days, by symbol, side and
    horizon, from the DuckDB query ``markout_summary.sql``."""
    baseline = experiment_rows(outputs, "E7").filter(pl.col("variant") == "baseline")
    frames = [
        pl.read_parquet(outputs.out_dir / "runs" / key / "markouts.parquet")
        for key in baseline["key"]
    ]
    return markout_summary(pl.concat(frames)).filter(pl.col("kind") == "quote")


def run_cost_table(outputs: RunOutputs) -> pl.DataFrame:
    """Per symbol: distinct runs, events replayed, simulation speed and worker peak memory."""
    distinct = outputs.jobs.unique("key", keep="first", maintain_order=True)
    return distinct.group_by("symbol", maintain_order=True).agg(
        pl.len().alias("runs"),
        pl.col("market_events").mean().alias("mean_events_per_run"),
        pl.col("sim_wall_s").mean().alias("mean_sim_wall_s"),
        (pl.col("market_events").sum() / pl.col("sim_wall_s").sum()).alias("events_per_s"),
        pl.col("job_wall_s").sum().alias("total_job_wall_s"),
        pl.col("worker_peak_rss_mb").max().alias("max_worker_peak_rss_mb"),
    )


@dataclass(frozen=True)
class ReportTables:
    """Every table the report shows, computed once per dataset and shared by all its outputs."""

    symbols: list[str]
    test_dates: list[str]
    n_jobs: int
    n_runs: int
    calibration: pl.DataFrame
    intensity: pl.DataFrame
    realism: pl.DataFrame
    strategies: pl.DataFrame
    sensitivity: pl.DataFrame
    latency: pl.DataFrame
    regimes: pl.DataFrame
    windows: pl.DataFrame
    events: pl.DataFrame
    event_series: dict[tuple[str, str], dict[str, pl.DataFrame]]
    fee_tiers: pl.DataFrame
    break_even: pl.DataFrame
    fee_reruns: pl.DataFrame
    markouts: pl.DataFrame
    run_cost: pl.DataFrame


def report_tables(outputs: RunOutputs, cfg: dict[str, Any]) -> ReportTables:
    """Compute every report table from one dataset's outputs."""
    jobs = outputs.jobs
    return ReportTables(
        symbols=jobs["symbol"].unique(maintain_order=True).to_list(),
        test_dates=sorted(jobs["test_date"].unique().to_list()),
        n_jobs=jobs.height,
        n_runs=jobs["key"].n_unique(),
        calibration=calibration_table(outputs),
        intensity=intensity_levels(outputs, cfg),
        realism=realism_ladder_table(outputs),
        strategies=strategy_table(outputs),
        sensitivity=sensitivity_table(outputs),
        latency=latency_table(outputs),
        regimes=regime_table(outputs),
        windows=event_windows(outputs),
        events=event_study_table(outputs),
        event_series=event_study_series(outputs),
        fee_tiers=fee_tier_table(outputs, cfg),
        break_even=break_even_maker_bps(outputs, cfg),
        fee_reruns=fee_rerun_check(outputs, cfg),
        markouts=baseline_markouts(outputs),
        run_cost=run_cost_table(outputs),
    )
