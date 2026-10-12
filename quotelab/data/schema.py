"""Column layout of the processed Parquet files. This is the contract between
``convert`` (raw Binance CSV -> Parquet), ``synthetic`` (generated data) and ``loader``
(Parquet -> event stream).

Processed files store real units (floats), not ticks, so they do not depend on an
instrument spec. ``ts`` is always int64 nanoseconds since the epoch, sorted ascending.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

BOOK_SCHEMA: dict[str, pl.DataType] = {
    "ts": pl.Int64(),
    "bid_px": pl.Float64(),
    "bid_qty": pl.Float64(),
    "ask_px": pl.Float64(),
    "ask_qty": pl.Float64(),
}

TRADE_SCHEMA: dict[str, pl.DataType] = {
    "ts": pl.Int64(),
    "px": pl.Float64(),
    "qty": pl.Float64(),
    "is_buyer_maker": pl.Boolean(),
}

FUNDING_SCHEMA: dict[str, pl.DataType] = {
    "ts": pl.Int64(),
    "rate": pl.Float64(),
}

KLINE_SCHEMA: dict[str, pl.DataType] = {
    "ts": pl.Int64(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
}

SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "book": BOOK_SCHEMA,
    "trades": TRADE_SCHEMA,
    "funding": FUNDING_SCHEMA,
    "klines": KLINE_SCHEMA,
}


def processed_path(root: Path, symbol: str, kind: str, period: str) -> Path:
    """Canonical location of one processed file.

    Example: ``data/processed/BTCUSDT/book/2024-01-15.parquet``. ``period`` is a date
    (``YYYY-MM-DD``) for daily kinds and a month (``YYYY-MM``) for funding.
    """
    return root / symbol / kind / f"{period}.parquet"
