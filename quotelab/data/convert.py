"""Convert raw Binance CSV archives into the processed Parquet layout from ``quotelab.data.schema``.

Raw CSVs can be several GB unzipped, so each one is streamed batch by batch into a ``.part``
Parquet file. Timestamps are normalised to int64 nanoseconds. If the source is not in ascending
``ts`` order, a stable sort pass rewrites the file so downstream readers can rely on that order.
"""

from __future__ import annotations

import os
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from quotelab.data.schema import SCHEMAS, processed_path

RAW_TO_PROCESSED = {
    "bookTicker": "book",
    "trades": "trades",
    "fundingRate": "funding",
    "klines": "klines",
}

RAW_COLUMN_TYPES: dict[str, dict[str, pa.DataType]] = {
    "trades": {
        "id": pa.int64(),
        "price": pa.float64(),
        "qty": pa.float64(),
        "quote_qty": pa.float64(),
        "time": pa.int64(),
        "is_buyer_maker": pa.bool_(),
    },
    "bookTicker": {
        "update_id": pa.int64(),
        "best_bid_price": pa.float64(),
        "best_bid_qty": pa.float64(),
        "best_ask_price": pa.float64(),
        "best_ask_qty": pa.float64(),
        "transaction_time": pa.int64(),
        "event_time": pa.int64(),
    },
    "fundingRate": {
        "calc_time": pa.int64(),
        "funding_interval_hours": pa.int64(),
        "last_funding_rate": pa.float64(),
    },
    "klines": {
        "open_time": pa.int64(),
        "open": pa.float64(),
        "high": pa.float64(),
        "low": pa.float64(),
        "close": pa.float64(),
        "volume": pa.float64(),
        "close_time": pa.int64(),
        "quote_volume": pa.float64(),
        "count": pa.int64(),
        "taker_buy_volume": pa.float64(),
        "taker_buy_quote_volume": pa.float64(),
        "ignore": pa.float64(),
    },
}

FIELD_SOURCES: dict[str, dict[str, str]] = {
    "bookTicker": {
        "ts": "transaction_time",
        "bid_px": "best_bid_price",
        "bid_qty": "best_bid_qty",
        "ask_px": "best_ask_price",
        "ask_qty": "best_ask_qty",
    },
    "trades": {"ts": "time", "px": "price", "qty": "qty", "is_buyer_maker": "is_buyer_maker"},
    "fundingRate": {"ts": "calc_time", "rate": "last_funding_rate"},
    "klines": {
        "ts": "open_time",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    },
}

PERIOD_PATTERN = re.compile(r"(\d{4}-\d{2}(?:-\d{2})?)\.zip$")


@dataclass(frozen=True)
class ConvertResult:
    rows: int
    resorted: bool
    out_path: Path


def raw_kind_from_path(zip_path: Path) -> str:
    return zip_path.parent.name


def period_from_filename(name: str) -> str:
    match = PERIOD_PATTERN.search(name)
    if match is None:
        raise ValueError(f"no YYYY-MM or YYYY-MM-DD period in {name!r}")
    return match.group(1)


def convert_file(zip_path: Path, raw_kind: str, out_path: Path) -> ConvertResult:
    processed_kind = RAW_TO_PROCESSED[raw_kind]
    target = _target_schema(processed_kind)
    member = _csv_member(zip_path)
    skip_rows = 1 if _has_header(zip_path, member) else 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    part = out_path.with_suffix(".parquet.part")
    rows, in_order = _stream_to_part(zip_path, member, raw_kind, skip_rows, target, part)
    if in_order:
        os.replace(part, out_path)
        return ConvertResult(rows=rows, resorted=False, out_path=out_path)
    _sort_part(part, out_path)
    return ConvertResult(rows=rows, resorted=True, out_path=out_path)


def run_prepare(
    raw_dir: Path,
    processed_dir: Path,
    *,
    force: bool = False,
    out: Callable[[str], object] = print,
) -> list[ConvertResult]:
    """Convert every raw archive under ``raw_dir/{SYMBOL}/{kind}/``.

    Outputs that already exist are skipped unless ``force``; only conversions performed are
    returned.
    """
    results: list[ConvertResult] = []
    for zip_path in sorted(raw_dir.glob("*/*/*.zip")):
        raw_kind = raw_kind_from_path(zip_path)
        if raw_kind not in RAW_TO_PROCESSED:
            continue
        symbol = zip_path.parent.parent.name
        out_path = processed_path(
            processed_dir,
            symbol,
            RAW_TO_PROCESSED[raw_kind],
            period_from_filename(zip_path.name),
        )
        if out_path.exists() and not force:
            out(f"skip {out_path}")
            continue
        result = convert_file(zip_path, raw_kind, out_path)
        out(f"wrote {out_path} rows={result.rows} resorted={result.resorted}")
        results.append(result)
    return results


def to_nanoseconds(values: pa.Array) -> pa.Array:
    """Scale by magnitude: below 1e14 is milliseconds, below 1e17 microseconds, else nanoseconds.

    The factor is chosen per row and multiplied once, so the checked multiply raises instead of
    silently wrapping when a value cannot be represented in int64 nanoseconds.
    """
    factor = pc.if_else(
        pc.less(values, 10**14),
        1_000_000,
        pc.if_else(pc.less(values, 10**17), 1_000, 1),
    )
    return pc.multiply_checked(values, factor)


def _target_schema(processed_kind: str) -> pa.Schema:
    return pl.DataFrame(schema=SCHEMAS[processed_kind]).to_arrow().schema


def _csv_member(zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path) as archive:
        members = [name for name in archive.namelist() if name.endswith(".csv")]
    if len(members) != 1:
        raise ValueError(f"{zip_path} must contain exactly one CSV, found {members}")
    return members[0]


def _has_header(zip_path: Path, member: str) -> bool:
    """Binance headers start with a column name, while data rows start with a number."""
    with zipfile.ZipFile(zip_path) as archive, archive.open(member) as stream:
        first_field = stream.readline().decode().split(",", 1)[0].strip()
    return not _is_number(first_field)


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _csv_options(raw_kind: str, skip_rows: int) -> tuple[pacsv.ReadOptions, pacsv.ConvertOptions]:
    column_types = RAW_COLUMN_TYPES[raw_kind]
    included = list(FIELD_SOURCES[raw_kind].values())
    read_options = pacsv.ReadOptions(column_names=list(column_types), skip_rows=skip_rows)
    convert_options = pacsv.ConvertOptions(
        include_columns=included,
        column_types={name: column_types[name] for name in included},
    )
    return read_options, convert_options


def _stream_to_part(
    zip_path: Path,
    member: str,
    raw_kind: str,
    skip_rows: int,
    target: pa.Schema,
    part: Path,
) -> tuple[int, bool]:
    rows = 0
    in_order = True
    previous_ts: int | None = None
    read_options, convert_options = _csv_options(raw_kind, skip_rows)
    with (
        zipfile.ZipFile(zip_path) as archive,
        archive.open(member) as stream,
        pq.ParquetWriter(part, target) as writer,
    ):
        for raw_batch in pacsv.open_csv(
            stream, read_options=read_options, convert_options=convert_options
        ):
            batch = _to_processed_batch(raw_batch, raw_kind, target)
            ts = batch.column("ts").to_numpy()
            in_order = in_order and continues_order(ts, previous_ts)
            if ts.size:
                previous_ts = int(ts[-1])
            rows += batch.num_rows
            writer.write_batch(batch)
    return rows, in_order


def _to_processed_batch(raw: pa.RecordBatch, raw_kind: str, target: pa.Schema) -> pa.RecordBatch:
    sources = FIELD_SOURCES[raw_kind]
    columns = []
    for field in target:
        column = raw.column(sources[field.name])
        if field.name == "ts":
            column = to_nanoseconds(column)
        columns.append(column.cast(field.type))
    return pa.RecordBatch.from_arrays(columns, schema=target)


def continues_order(ts: np.ndarray, previous_ts: int | None) -> bool:
    if ts.size == 0:
        return True
    starts_after_previous = previous_ts is None or ts[0] >= previous_ts
    return bool(starts_after_previous and np.all(np.diff(ts) >= 0))


def _sort_part(part: Path, out_path: Path) -> None:
    """Stable sort so rows with equal timestamps keep their original file order."""
    sorted_part = out_path.with_suffix(".parquet.sorted.part")
    pl.scan_parquet(part).sort("ts", maintain_order=True).sink_parquet(sorted_part)
    part.unlink()
    os.replace(sorted_part, out_path)
