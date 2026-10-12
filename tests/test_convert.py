import zipfile
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from polars.testing import assert_frame_equal

from quotelab.data.convert import (
    continues_order,
    convert_file,
    period_from_filename,
    raw_kind_from_path,
    run_prepare,
)
from quotelab.data.schema import (
    BOOK_SCHEMA,
    FUNDING_SCHEMA,
    KLINE_SCHEMA,
    TRADE_SCHEMA,
    processed_path,
)
from quotelab.types import NS_PER_MS

TRADES_HEADER = "id,price,qty,quote_qty,time,is_buyer_maker"
TRADES_ROWS = [
    "1,40000.1,0.5,20000.05,1705276800000,true",
    "2,40000.2,0.25,10000.05,1705276800500,false",
]


def write_archive(zip_path: Path, lines: list[str]) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(f"{zip_path.stem}.csv", "\n".join(lines) + "\n")
    return zip_path


def test_trades_with_and_without_header_convert_identically(tmp_path):
    with_header = convert_file(
        write_archive(tmp_path / "with.zip", [TRADES_HEADER, *TRADES_ROWS]),
        "trades",
        tmp_path / "with.parquet",
    )
    without_header = convert_file(
        write_archive(tmp_path / "without.zip", TRADES_ROWS),
        "trades",
        tmp_path / "without.parquet",
    )

    left = pl.read_parquet(with_header.out_path)
    right = pl.read_parquet(without_header.out_path)

    assert_frame_equal(left, right)
    assert list(left.schema.items()) == list(TRADE_SCHEMA.items())
    assert left["ts"].to_list() == [1705276800000 * NS_PER_MS, 1705276800500 * NS_PER_MS]
    assert left["is_buyer_maker"].to_list() == [True, False]
    assert with_header.rows == 2
    assert with_header.resorted is False


def test_out_of_order_book_rows_are_resorted_and_columns_mapped(tmp_path):
    header = (
        "update_id,best_bid_price,best_bid_qty,best_ask_price,best_ask_qty,"
        "transaction_time,event_time"
    )
    rows = [
        "10,40000.0,1.5,40000.1,2.0,1705276800200,1705276800201",
        "11,40000.2,1.0,40000.3,3.0,1705276800100,1705276800101",
        "12,40000.4,0.5,40000.5,4.0,1705276800300,1705276800301",
    ]
    result = convert_file(
        write_archive(tmp_path / "book.zip", [header, *rows]),
        "bookTicker",
        tmp_path / "book.parquet",
    )

    frame = pl.read_parquet(result.out_path)

    assert result.resorted is True
    assert result.rows == 3
    assert list(frame.schema.items()) == list(BOOK_SCHEMA.items())
    assert frame["ts"].to_list() == [
        1705276800100 * NS_PER_MS,
        1705276800200 * NS_PER_MS,
        1705276800300 * NS_PER_MS,
    ]
    assert frame["bid_px"].to_list() == [40000.2, 40000.0, 40000.4]
    assert frame["ask_qty"].to_list() == [3.0, 2.0, 4.0]
    assert sorted(path.name for path in tmp_path.glob("book*")) == ["book.parquet", "book.zip"]


def test_funding_columns_are_mapped(tmp_path):
    header = "calc_time,funding_interval_hours,last_funding_rate"
    rows = ["1704038400000,8,0.0001", "1704067200000,8,-0.00005"]
    result = convert_file(
        write_archive(tmp_path / "funding.zip", [header, *rows]),
        "fundingRate",
        tmp_path / "funding.parquet",
    )

    frame = pl.read_parquet(result.out_path)

    assert list(frame.schema.items()) == list(FUNDING_SCHEMA.items())
    assert frame["ts"].to_list() == [1704038400000 * NS_PER_MS, 1704067200000 * NS_PER_MS]
    assert frame["rate"].to_list() == [0.0001, -0.00005]


def test_klines_columns_are_mapped(tmp_path):
    header = (
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
        "taker_buy_volume,taker_buy_quote_volume,ignore"
    )
    row = (
        "1705276800000,40000.0,40100.0,39900.0,40050.0,123.5,"
        "1705276859999,4940000.0,100,60.0,2400000.0,0"
    )
    result = convert_file(
        write_archive(tmp_path / "klines.zip", [header, row]),
        "klines",
        tmp_path / "klines.parquet",
    )

    frame = pl.read_parquet(result.out_path)

    assert list(frame.schema.items()) == list(KLINE_SCHEMA.items())
    assert frame.row(0) == (1705276800000 * NS_PER_MS, 40000.0, 40100.0, 39900.0, 40050.0, 123.5)


def test_run_prepare_writes_processed_layout_and_skips_existing(tmp_path):
    raw = tmp_path / "raw"
    processed = tmp_path / "processed"
    write_archive(
        raw / "BTCUSDT" / "trades" / "BTCUSDT-trades-2024-01-15.zip",
        [TRADES_HEADER, *TRADES_ROWS],
    )
    expected = processed_path(processed, "BTCUSDT", "trades", "2024-01-15")

    first = run_prepare(raw, processed, out=lambda _: None)
    second = run_prepare(raw, processed, out=lambda _: None)

    assert [result.out_path for result in first] == [expected]
    assert expected.exists()
    assert second == []
    assert not list(processed.rglob("*.part"))


@pytest.mark.parametrize(
    ("name", "period"),
    [
        ("BTCUSDT-trades-2024-01-15.zip", "2024-01-15"),
        ("BTCUSDT-1m-2024-01-15.zip", "2024-01-15"),
        ("BTCUSDT-fundingRate-2024-01.zip", "2024-01"),
    ],
)
def test_period_is_parsed_from_file_name(name, period):
    assert period_from_filename(name) == period


def test_raw_kind_is_the_parent_directory(tmp_path):
    assert raw_kind_from_path(tmp_path / "BTCUSDT" / "bookTicker" / "x.zip") == "bookTicker"


def test_order_check_spans_batch_boundaries():
    assert continues_order(np.array([5, 6]), previous_ts=5)
    assert not continues_order(np.array([4, 6]), previous_ts=5)
    assert not continues_order(np.array([5, 4]), previous_ts=None)
    assert continues_order(np.array([], dtype=np.int64), previous_ts=9)
