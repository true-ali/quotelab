"""DuckDB analytics over run outputs.

Each query registers polars frames as views and runs one SQL file from the sql directory.
Connections are in-memory and per call, so the polars frames are the only inputs and outputs.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import duckdb
import polars as pl

HORIZONS_S: tuple[int, ...] = (1, 5, 30, 60)


def read_sql(name: str) -> str:
    """Text of the analytics query ``name``, read from the ``sql`` directory next to this module."""
    return (Path(__file__).parent / "sql" / f"{name}.sql").read_text()


def book_mids(book: pl.DataFrame) -> pl.DataFrame:
    """ts and mid=(bid_px+ask_px)/2 from a processed book frame (real units), sorted by ts.

    The mid is computed once here so every markout is measured against the same reference price.
    """
    return book.select("ts", ((pl.col("bid_px") + pl.col("ask_px")) / 2).alias("mid")).sort("ts")


def markouts(
    fills: pl.DataFrame,
    mids: pl.DataFrame,
    symbol: str,
    horizons_s: Sequence[int] = HORIZONS_S,
) -> pl.DataFrame:
    """One row per (fill, horizon) with the mid after the horizon, from markouts.sql.

    Registers ``fills``, ``mids`` and ``horizons`` (a one-column frame ``h_s``) as views.
    """
    horizons = pl.DataFrame({"h_s": list(horizons_s)}, schema={"h_s": pl.Int64})
    return _run_query(
        "markouts",
        {"fills": fills, "mids": mids, "horizons": horizons},
        {"symbol": symbol},
    )


def markout_summary(markouts_df: pl.DataFrame) -> pl.DataFrame:
    """Counts, mean markouts, standard error and total USD per symbol, kind, side and horizon.

    Registers ``markouts_df`` as the ``markouts`` view that markout_summary.sql reads.
    """
    return _run_query("markout_summary", {"markouts": markouts_df}, None)


def period_pnl(timeseries: pl.DataFrame, period_ns: int) -> pl.DataFrame:
    """Equity at the end of each period and the pnl of that period, from period_pnl.sql.

    Registers ``timeseries`` as a view. Periods are ``ts // period_ns`` buckets.
    """
    return _run_query("period_pnl", {"timeseries": timeseries}, {"period_ns": period_ns})


def _run_query(
    name: str,
    views: dict[str, pl.DataFrame],
    params: dict[str, object] | None,
) -> pl.DataFrame:
    """Run one SQL file on a fresh in-memory DuckDB connection and return the result as polars.

    Three session settings, each for a measured reason:

    - ``asof_loop_join_threshold=0``: DuckDB estimates a registered polars frame at about one
      row, so it plans every ASOF join as a nested loop. On one synthetic day that made the
      markouts query take 4 s instead of 5 ms; it would never finish on a real day.
    - ``threads=1``: experiments already run one process per core, and a single-threaded
      query is fast enough once the join is planned correctly.
    - ``enable_progress_bar=false``: keeps the worker logs readable.
    """
    with duckdb.connect() as con:
        con.execute("SET asof_loop_join_threshold = 0")
        con.execute("SET threads = 1")
        con.execute("SET enable_progress_bar = false")
        for view_name, view_frame in views.items():
            con.register(view_name, view_frame)
        return con.execute(read_sql(name), params).pl()
