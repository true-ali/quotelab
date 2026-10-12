"""Run every experiment on one dataset, in parallel, and write the tables the report reads.

Three phases, each spread over a process pool:

1. calibrate every (symbol, calibration day) of the walk-forward split;
2. measure the hourly realised variance of every test day, to pick the E6 window and the E5
   regimes;
3. run each distinct job once.

Outputs under ``runs/<dataset>/``: ``calibration/``, ``runs/<key>/``, ``market_hours.parquet``,
``jobs.parquet`` (experiment, variant and day for every job, many rows can share a key) and
``metrics.parquet`` (one row per key).

Workers are started with ``spawn`` and ``POLARS_MAX_THREADS=1``: each run is single-threaded
Python anyway, and letting every worker start a full polars thread pool would oversubscribe the
cores. Results do not depend on the number of workers.
"""

from __future__ import annotations

import multiprocessing
import os
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any

import polars as pl

from quotelab.config import resolve_path
from quotelab.data.synthetic import synthetic_config, synthetic_dates
from quotelab.experiments.definitions import daily_jobs, event_study_jobs
from quotelab.experiments.jobs import Job, job_key, walk_forward_pairs
from quotelab.experiments.worker import (
    HOUR_NS,
    Dataset,
    calibrate_day,
    market_hours_day,
    run_job,
)

DATASETS = ("synthetic", "real")
PROGRESS_LINES = 20


def dataset_for(cfg: dict[str, Any], name: str) -> Dataset:
    """The synthetic sessions or the downloaded real days, as configured."""
    if name == "synthetic":
        sc = synthetic_config(cfg)
        symbols, dates = (sc.symbol,), tuple(synthetic_dates(sc))
    elif name == "real":
        symbols, dates = tuple(cfg["data"]["symbols"]), tuple(sorted(cfg["data"]["dates"]))
    else:
        raise ValueError(f"dataset must be one of {DATASETS}, got {name!r}")
    return Dataset(
        name=name,
        symbols=symbols,
        dates=dates,
        processed_dir=resolve_path(cfg["data"]["processed_dir"]),
        out_dir=resolve_path(cfg["experiments"]["runs_dir"]) / name,
    )


def run_experiments(cfg: dict[str, Any], dataset: Dataset, workers: int) -> pl.DataFrame:
    """Calibrate, pick the event-study windows, run every distinct job; return the metrics."""
    pairs = calibrate_all(cfg, dataset, workers)
    hour_tasks = [(cfg, dataset, symbol, test) for symbol in dataset.symbols for _, test in pairs]
    market_hours = pl.concat(_parallel(market_hours_day, hour_tasks, workers, "hours"))
    market_hours.write_parquet(dataset.out_dir / "market_hours.parquet")

    jobs = all_jobs(cfg, dataset, pairs, market_hours)
    keys = [job_key(job, cfg) for job in jobs]
    _job_index(jobs, keys).write_parquet(dataset.out_dir / "jobs.parquet")
    first_job: dict[str, Job] = {}
    for job, key in zip(jobs, keys, strict=True):
        first_job.setdefault(key, job)
    run_tasks = [(cfg, dataset, job, key) for key, job in first_job.items()]
    rows = _parallel(run_job, run_tasks, workers, "run")
    metrics = pl.DataFrame(rows, infer_schema_length=None).sort("key")
    metrics.write_parquet(dataset.out_dir / "metrics.parquet")
    return metrics


def calibrate_all(cfg: dict[str, Any], dataset: Dataset, workers: int) -> list[tuple[str, str]]:
    """Fit and save the parameters for every calibration day; return the walk-forward pairs."""
    pairs = walk_forward_pairs(list(dataset.dates))
    if not pairs:
        raise ValueError("walk-forward needs at least two dates")
    dataset.out_dir.mkdir(parents=True, exist_ok=True)
    tasks = [(cfg, dataset, symbol, calib) for symbol in dataset.symbols for calib, _ in pairs]
    _parallel(calibrate_day, tasks, workers, "calibrate")
    return pairs


def all_jobs(
    cfg: dict[str, Any],
    dataset: Dataset,
    pairs: list[tuple[str, str]],
    market_hours: pl.DataFrame,
) -> list[Job]:
    """Full-day jobs for every symbol, plus the E6 jobs on each symbol's most violent hour."""
    calib_for_test = {test: calib for calib, test in pairs}
    padding = int(cfg["experiments"]["e6_padding_hours"]) * HOUR_NS
    jobs: list[Job] = []
    for symbol in dataset.symbols:
        jobs += daily_jobs(cfg, symbol, pairs)
        violent = most_violent_hour(market_hours, symbol)
        window = (violent["period_start"] - padding, violent["period_start"] + HOUR_NS + padding)
        test_date = violent["test_date"]
        jobs += event_study_jobs(symbol, calib_for_test[test_date], test_date, window)
    return jobs


def most_violent_hour(market_hours: pl.DataFrame, symbol: str) -> dict[str, Any]:
    """The test-day hour with the largest realised variance of the 1-second mid for ``symbol``.

    Choosing the window uses the test days' prices, which is fine for an event study: the
    window only decides what period to replay, it never feeds a strategy parameter.
    """
    rows = market_hours.filter(pl.col("symbol") == symbol).sort("realised_var", descending=True)
    return rows.row(0, named=True)


def _job_index(jobs: list[Job], keys: list[str]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "experiment": [job.experiment for job in jobs],
            "variant": [job.variant for job in jobs],
            "symbol": [job.symbol for job in jobs],
            "calib_date": [job.calib_date for job in jobs],
            "test_date": [job.test_date for job in jobs],
            "window_start": [job.window[0] if job.window else None for job in jobs],
            "window_end": [job.window[1] if job.window else None for job in jobs],
            "key": keys,
        },
        schema_overrides={"window_start": pl.Int64, "window_end": pl.Int64},
    )


def _parallel(func: Callable, tasks: list[tuple], workers: int, label: str) -> list:
    """``func(*task)`` for every task, in task order; in-process when ``workers`` is 1."""
    started = time.perf_counter()
    if workers <= 1:
        results = []
        for done, task in enumerate(tasks, start=1):
            results.append(func(*task))
            _progress(label, done, len(tasks), started)
        return results
    os.environ["POLARS_MAX_THREADS"] = "1"
    context = multiprocessing.get_context("spawn")
    results_by_index: dict[int, Any] = {}
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
        futures = {pool.submit(func, *task): index for index, task in enumerate(tasks)}
        for done, future in enumerate(as_completed(futures), start=1):
            results_by_index[futures[future]] = future.result()
            _progress(label, done, len(tasks), started)
    return [results_by_index[index] for index in range(len(tasks))]


def _progress(label: str, done: int, total: int, started: float) -> None:
    """Print about twenty progress lines per phase, plus the last one."""
    if done % max(1, total // PROGRESS_LINES) and done != total:
        return
    elapsed = time.perf_counter() - started
    print(f"{label}: {done}/{total} done, {elapsed:.0f}s elapsed", flush=True)
