"""Load processed Parquet files into one timestamp-ordered stream of engine events.

Ordering rule, the foundation of the no-look-ahead guarantee: events are sorted by ``ts``
ascending. At an equal ``ts`` the order is trade, then book, then funding. Within the same kind
and timestamp the original row order is kept. Binance stamps a trade and the top-of-book update
it causes with the same time; the trade is the cause, so it must be seen first. Funding comes
last at a shared timestamp so it is applied after that instant's market update.

Processed frames hold real units (floats). Prices become integer ticks and quantities become
integer lots through an ``InstrumentSpec``, as vectorised polars expressions rather than a
Python loop over rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from quotelab.data.schema import BOOK_SCHEMA, FUNDING_SCHEMA, TRADE_SCHEMA, processed_path
from quotelab.types import (
    NS_PER_SECOND,
    BookEvent,
    Event,
    FundingEvent,
    InstrumentSpec,
    Side,
    TradeEvent,
)

NS_PER_DAY = 86_400 * NS_PER_SECOND

_TRADE_KIND = 0
_BOOK_KIND = 1
_FUNDING_KIND = 2
_CHUNK_NS = 3600 * NS_PER_SECOND
_EVENT_COLUMNS = (
    "ts",
    "kind",
    "bid_px",
    "bid_qty",
    "ask_px",
    "ask_qty",
    "px",
    "qty",
    "aggressor_sign",
    "rate",
)


def book_frame_to_ticks(spec: InstrumentSpec, book: pl.DataFrame) -> pl.DataFrame:
    """Top-of-book frame with prices in integer ticks and sizes in integer lots."""
    frame = book.select(
        "ts",
        _integer_units("bid_px", spec.tick_size),
        _integer_units("bid_qty", spec.lot_size),
        _integer_units("ask_px", spec.tick_size),
        _integer_units("ask_qty", spec.lot_size),
    )
    _require_non_negative_qty(frame, ["bid_qty", "ask_qty"], "book")
    return frame


def trades_frame_to_ticks(spec: InstrumentSpec, trades: pl.DataFrame) -> pl.DataFrame:
    """Trade frame with integer tick price, integer lot quantity and the taker's side sign.

    Binance ``is_buyer_maker`` means the buyer rested and the seller hit the bid, so the taker
    sold. That maps to ``Side.SELL``; otherwise the taker bought.
    """
    frame = trades.select(
        "ts",
        _integer_units("px", spec.tick_size),
        _integer_units("qty", spec.lot_size),
        pl.when(pl.col("is_buyer_maker"))
        .then(pl.lit(Side.SELL.value, dtype=pl.Int8))
        .otherwise(pl.lit(Side.BUY.value, dtype=pl.Int8))
        .alias("aggressor_sign"),
    )
    _require_non_negative_qty(frame, ["qty"], "trades")
    return frame


def events_from_frames(
    spec: InstrumentSpec,
    book: pl.DataFrame,
    trades: pl.DataFrame,
    funding: pl.DataFrame | None = None,
    batch_rows: int = 100_000,
    window: tuple[int, int] | None = None,
) -> Iterator[Event]:
    """Yield the merged, ordered events for one symbol.

    Validation and the polars merge run eagerly when this is called, so a malformed frame fails
    at the call site. Only the per-event Python objects are produced lazily, one batch of
    ``batch_rows`` merged rows at a time, which keeps memory bounded on multi-million-row days.
    ``window = (start_ns, end_ns)`` keeps only events with ``start_ns <= ts < end_ns``.
    """
    if batch_rows < 1:
        raise ValueError(f"batch_rows must be positive, got {batch_rows}")
    _require_columns(book, BOOK_SCHEMA, "book")
    _require_columns(trades, TRADE_SCHEMA, "trades")
    if funding is not None:
        _require_columns(funding, FUNDING_SCHEMA, "funding")
    parts = _tagged_parts(
        book_frame_to_ticks(spec, book),
        trades_frame_to_ticks(spec, trades),
        funding if funding is not None else pl.DataFrame(schema=FUNDING_SCHEMA),
    )
    if window is not None:
        in_window = (pl.col("ts") >= window[0]) & (pl.col("ts") < window[1])
        parts = [part.filter(in_window) for part in parts]
    return _iter_events(parts, spec.symbol, batch_rows)


def load_day(
    processed_dir: Path, spec: InstrumentSpec, date: str
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame | None]:
    """Book, trades and funding for one UTC day (``YYYY-MM-DD``).

    Book and trades are daily files read whole. Funding lives in a monthly file, so its rows are
    cut to this day's UTC window. A missing monthly file yields ``None``, meaning no funding
    events, rather than an error.
    """
    book = _read_processed(processed_dir, spec.symbol, "book", date)
    trades = _read_processed(processed_dir, spec.symbol, "trades", date)
    return book, trades, _read_funding_for_day(processed_dir, spec.symbol, date)


def load_events(
    processed_dir: Path,
    spec: InstrumentSpec,
    dates: list[str],
    batch_rows: int = 100_000,
    window: tuple[int, int] | None = None,
) -> Iterator[Event]:
    """Yield the events for each date in chronological order, optionally cut to ``window``.

    Dates are sorted as strings, which is chronological for ``YYYY-MM-DD``. This is a generator,
    so a day's files are read only when iteration reaches that day.
    """
    for date in sorted(dates):
        book, trades, funding = load_day(processed_dir, spec, date)
        yield from events_from_frames(spec, book, trades, funding, batch_rows, window)


def _integer_units(column: str, unit_size: float) -> pl.Expr:
    """Round a real-unit column to whole multiples of ``unit_size`` (a tick or a lot).

    Dividing and then rounding to nearest absorbs float noise, so 40000.100000000006 / 0.1 still
    lands on 400001. ``round`` defaults to half-to-even, matching the Python ``round`` used by
    ``InstrumentSpec.to_ticks``, so both conversion paths agree on exact halves.
    """
    return (pl.col(column) / unit_size).round().cast(pl.Int64).alias(column)


def _require_columns(frame: pl.DataFrame, schema: dict[str, pl.DataType], label: str) -> None:
    missing = sorted(set(schema) - set(frame.columns))
    if missing:
        raise ValueError(f"{label} frame is missing required columns: {', '.join(missing)}")


def _require_non_negative_qty(frame: pl.DataFrame, qty_columns: list[str], label: str) -> None:
    negative_rows = frame.filter(pl.any_horizontal(pl.col(qty_columns) < 0)).height
    if negative_rows:
        raise ValueError(
            f"{label} has {negative_rows} rows with a negative quantity after lot conversion"
        )


def _tagged_parts(
    book_ticks: pl.DataFrame, trades_ticks: pl.DataFrame, funding: pl.DataFrame
) -> list[pl.DataFrame]:
    """Each kind's frame with its ``kind`` code and ``row``, its position in the input.

    ``row`` is taken before any filtering, so it preserves input order for ties at one ``ts``.
    """
    return [
        _with_kind_and_row(book_ticks, _BOOK_KIND),
        _with_kind_and_row(trades_ticks, _TRADE_KIND),
        _with_kind_and_row(funding.select("ts", "rate"), _FUNDING_KIND),
    ]


def _with_kind_and_row(frame: pl.DataFrame, kind: int) -> pl.DataFrame:
    return frame.with_row_index("row").with_columns(pl.lit(kind, dtype=pl.Int8).alias("kind"))


def _merged_chunk(parts: list[pl.DataFrame], start: int, end: int) -> pl.DataFrame:
    """All kinds with ``start <= ts < end`` in one frame sorted by (ts, kind, row).

    Diagonal concat fills each kind's absent columns with nulls, which lets one sort cover
    every kind. The columns are put in ``_EVENT_COLUMNS`` order for ``_event_from_row``.
    """
    in_chunk = (pl.col("ts") >= start) & (pl.col("ts") < end)
    chunk = [part.filter(in_chunk) for part in parts]
    return pl.concat(chunk, how="diagonal").sort(["ts", "kind", "row"]).select(_EVENT_COLUMNS)


def _iter_events(parts: list[pl.DataFrame], symbol: str, batch_rows: int) -> Iterator[Event]:
    """Merge and emit one hour of events at a time.

    Hours partition the timeline, so emitting sorted hours one after another gives the same
    sequence as sorting the whole day, while the merged frame held in memory stays one hour
    long instead of a full day of ~24 million rows.
    """
    non_empty = [part for part in parts if part.height > 0]
    if not non_empty:
        return
    first = min(part["ts"].min() for part in non_empty)
    last = max(part["ts"].max() for part in non_empty)
    for start in range(first, last + 1, _CHUNK_NS):
        merged = _merged_chunk(parts, start, start + _CHUNK_NS)
        for batch in merged.iter_slices(n_rows=batch_rows):
            for row in batch.iter_rows():
                yield _event_from_row(row, symbol)


def _event_from_row(row: tuple, symbol: str) -> Event:
    """Build the event for one merged row laid out as ``_EVENT_COLUMNS``."""
    ts, kind, bid_px, bid_qty, ask_px, ask_qty, px, qty, aggressor_sign, rate = row
    if kind == _BOOK_KIND:
        return BookEvent(ts, symbol, bid_px, bid_qty, ask_px, ask_qty)
    if kind == _TRADE_KIND:
        return TradeEvent(ts, symbol, px, qty, Side(aggressor_sign))
    return FundingEvent(ts, symbol, rate)


def _read_processed(processed_dir: Path, symbol: str, kind: str, date: str) -> pl.DataFrame:
    path = processed_path(processed_dir, symbol, kind, date)
    if not path.is_file():
        raise FileNotFoundError(
            f"processed {kind} file not found: {path}; run `quotelab prepare` to create it"
        )
    return pl.read_parquet(path)


def _read_funding_for_day(processed_dir: Path, symbol: str, date: str) -> pl.DataFrame | None:
    path = processed_path(processed_dir, symbol, "funding", date[:7])
    if not path.is_file():
        return None
    start = _day_start_ns(date)
    funding = pl.read_parquet(path)
    return funding.filter((pl.col("ts") >= start) & (pl.col("ts") < start + NS_PER_DAY))


def _day_start_ns(date: str) -> int:
    """Nanoseconds at 00:00:00 UTC on ``date``.

    The datetime is pinned to UTC before ``timestamp()``, which would otherwise use local time.
    Whole seconds are multiplied up, since a float timestamp times 1e9 is not exact.
    """
    midnight_utc = datetime.fromisoformat(date).replace(tzinfo=UTC)
    return int(midnight_utc.timestamp()) * NS_PER_SECOND
