from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from quotelab.data.loader import events_from_frames, load_day, load_events
from quotelab.data.schema import BOOK_SCHEMA, FUNDING_SCHEMA, TRADE_SCHEMA, processed_path
from quotelab.types import NS_PER_SECOND, BookEvent, FundingEvent, InstrumentSpec, Side, TradeEvent

SPEC = InstrumentSpec("BTCUSDT", 0.1, 0.001, 100.0)
SYMBOL = SPEC.symbol


def book_frame(*rows: tuple[int, float, float, float, float]) -> pl.DataFrame:
    return pl.DataFrame(list(rows), schema=BOOK_SCHEMA, orient="row")


def trade_frame(*rows: tuple[int, float, float, bool]) -> pl.DataFrame:
    return pl.DataFrame(list(rows), schema=TRADE_SCHEMA, orient="row")


def funding_frame(*rows: tuple[int, float]) -> pl.DataFrame:
    return pl.DataFrame(list(rows), schema=FUNDING_SCHEMA, orient="row")


def midnight_ns(date: str) -> int:
    return int(datetime.fromisoformat(date).replace(tzinfo=UTC).timestamp()) * NS_PER_SECOND


def write_processed(root: Path, kind: str, period: str, frame: pl.DataFrame) -> None:
    path = processed_path(root, SYMBOL, kind, period)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path)


def test_real_units_convert_to_integer_ticks_and_lots_despite_float_noise():
    book = book_frame((1, 40000.1, 1.234, 40000.100000000006, 0.5))

    assert list(events_from_frames(SPEC, book, trade_frame())) == [
        BookEvent(1, SYMBOL, 400001, 1234, 400001, 500)
    ]


def test_events_are_ordered_by_ts_then_trade_book_and_input_order():
    book = book_frame(
        (10, 100.0, 1.0, 101.0, 1.0),
        (20, 200.0, 1.0, 201.0, 1.0),
        (10, 300.0, 1.0, 301.0, 1.0),
    )
    trades = trade_frame(
        (10, 100.5, 1.0, False),
        (15, 150.0, 2.0, True),
        (20, 200.5, 3.0, False),
    )

    assert list(events_from_frames(SPEC, book, trades)) == [
        TradeEvent(10, SYMBOL, 1005, 1000, Side.BUY),
        BookEvent(10, SYMBOL, 1000, 1000, 1010, 1000),
        BookEvent(10, SYMBOL, 3000, 1000, 3010, 1000),
        TradeEvent(15, SYMBOL, 1500, 2000, Side.SELL),
        TradeEvent(20, SYMBOL, 2005, 3000, Side.BUY),
        BookEvent(20, SYMBOL, 2000, 1000, 2010, 1000),
    ]


def test_buyer_maker_trade_is_a_sell_and_otherwise_a_buy():
    trades = trade_frame((1, 100.0, 1.0, True), (2, 100.0, 1.0, False))

    sides = [event.aggressor for event in events_from_frames(SPEC, book_frame(), trades)]

    assert sides == [Side.SELL, Side.BUY]


def test_funding_is_emitted_at_its_ts_and_last_among_events_sharing_a_ts():
    book = book_frame((10, 100.0, 1.0, 101.0, 1.0))
    trades = trade_frame((10, 100.5, 1.0, False))
    funding = funding_frame((10, 0.0001), (5, -0.0002))

    assert list(events_from_frames(SPEC, book, trades, funding)) == [
        FundingEvent(5, SYMBOL, -0.0002),
        TradeEvent(10, SYMBOL, 1005, 1000, Side.BUY),
        BookEvent(10, SYMBOL, 1000, 1000, 1010, 1000),
        FundingEvent(10, SYMBOL, 0.0001),
    ]


def test_batch_rows_does_not_change_the_event_sequence():
    book = book_frame(
        (10, 100.0, 1.0, 101.0, 1.0),
        (20, 200.0, 1.0, 201.0, 1.0),
        (10, 300.0, 1.0, 301.0, 1.0),
    )
    trades = trade_frame((10, 100.5, 1.0, False), (15, 150.0, 2.0, True), (20, 200.5, 3.0, False))
    funding = funding_frame((10, 0.0001))

    default = list(events_from_frames(SPEC, book, trades, funding))
    batched = list(events_from_frames(SPEC, book, trades, funding, batch_rows=2))

    assert len(default) == 7
    assert batched == default


def test_window_keeps_events_in_start_inclusive_end_exclusive_range():
    book = book_frame((10, 100.0, 1.0, 101.0, 1.0), (20, 200.0, 1.0, 201.0, 1.0))
    trades = trade_frame((15, 150.0, 2.0, True), (20, 200.5, 3.0, False))
    funding = funding_frame((10, 0.0001))

    events = list(events_from_frames(SPEC, book, trades, funding, window=(10, 20)))

    assert [(type(ev).__name__, ev.ts) for ev in events] == [
        ("BookEvent", 10),
        ("FundingEvent", 10),
        ("TradeEvent", 15),
    ]


def test_missing_column_raises_naming_it():
    book = book_frame().drop("ask_qty")

    with pytest.raises(ValueError, match="ask_qty"):
        list(events_from_frames(SPEC, book, trade_frame()))


def test_negative_quantity_after_lot_conversion_raises():
    trades = trade_frame((1, 100.0, -1.0, False))

    with pytest.raises(ValueError, match="negative quantity"):
        list(events_from_frames(SPEC, book_frame(), trades))


def test_load_events_yields_only_the_requested_days_funding_in_date_order(tmp_path):
    day_15 = midnight_ns("2024-01-15")
    day_16 = midnight_ns("2024-01-16")
    funding_15 = day_15 + 8 * 3600 * NS_PER_SECOND
    for date, start in [("2024-01-15", day_15), ("2024-01-16", day_16)]:
        write_processed(tmp_path, "book", date, book_frame((start + 1, 100.0, 1.0, 101.0, 1.0)))
        write_processed(tmp_path, "trades", date, trade_frame((start + 2, 100.5, 1.0, False)))
    write_processed(
        tmp_path, "funding", "2024-01", funding_frame((funding_15, 0.0001), (day_16, 0.0002))
    )

    book, trades, funding = load_day(tmp_path, SPEC, "2024-01-15")
    only_15 = list(load_events(tmp_path, SPEC, ["2024-01-15"]))
    both = list(load_events(tmp_path, SPEC, ["2024-01-16", "2024-01-15"]))

    assert (book.height, trades.height, funding.height) == (1, 1, 1)
    assert [e for e in only_15 if isinstance(e, FundingEvent)] == [
        FundingEvent(funding_15, SYMBOL, 0.0001)
    ]
    assert [e for e in both if isinstance(e, FundingEvent)] == [
        FundingEvent(funding_15, SYMBOL, 0.0001),
        FundingEvent(day_16, SYMBOL, 0.0002),
    ]
    assert len(both) == 6
    assert [e.ts for e in both] == sorted(e.ts for e in both)


def test_load_events_without_a_funding_file_emits_no_funding(tmp_path):
    start = midnight_ns("2024-01-15")
    write_processed(tmp_path, "book", "2024-01-15", book_frame((start + 1, 100.0, 1.0, 101.0, 1.0)))
    write_processed(tmp_path, "trades", "2024-01-15", trade_frame((start + 2, 100.5, 1.0, False)))

    events = list(load_events(tmp_path, SPEC, ["2024-01-15"]))

    assert [type(e) for e in events] == [BookEvent, TradeEvent]


def test_missing_book_file_points_to_quotelab_prepare(tmp_path):
    with pytest.raises(FileNotFoundError, match="quotelab prepare"):
        list(load_events(tmp_path, SPEC, ["2024-01-15"]))
