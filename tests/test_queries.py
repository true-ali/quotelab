import math

import polars as pl
import pytest

from quotelab.analytics.queries import book_mids, markout_summary, markouts, period_pnl
from quotelab.data.schema import BOOK_SCHEMA
from quotelab.engine.recorder import FILLS_SCHEMA, TIMESERIES_SCHEMA, frame

SEC = 1_000_000_000
T0 = 10 * SEC
SYMBOL = "BTCUSDT"

MARKOUTS_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String,
    "ts": pl.Int64,
    "order_id": pl.Int64,
    "kind": pl.String,
    "side": pl.Int8,
    "px": pl.Float64,
    "qty": pl.Float64,
    "mid_at_fill": pl.Float64,
    "horizon_s": pl.Int64,
    "mid_after": pl.Float64,
    "markout_usd": pl.Float64,
    "markout_bps": pl.Float64,
    "adverse_bps": pl.Float64,
}


def mids_from(points: list[tuple[int, float]]) -> pl.DataFrame:
    book = frame(
        [
            {"ts": ts, "bid_px": mid - 0.5, "bid_qty": 1.0, "ask_px": mid + 0.5, "ask_qty": 1.0}
            for ts, mid in points
        ],
        BOOK_SCHEMA,
    )
    return book_mids(book)


def fill_row(ts: int, side: int, px: float, mid: float, order_id: int = 1) -> dict:
    return {
        "ts": ts,
        "order_id": order_id,
        "side": side,
        "px": px,
        "qty": 2.0,
        "fee": 0.0,
        "is_maker": True,
        "kind": "quote",
        "mid": mid,
        "position": 0.0,
    }


def fill_markout(row: dict, mids: pl.DataFrame, horizon_s: int = 1) -> dict:
    fills = frame([row], FILLS_SCHEMA)
    return markouts(fills, mids, SYMBOL, (horizon_s,)).row(0, named=True)


def test_buy_and_mirrored_sell_markouts_have_opposite_sign_when_mid_rises():
    mids = mids_from([(T0, 100.5), (T0 + SEC, 101.0)])

    buy = fill_markout(fill_row(T0, 1, 100.0, 100.5), mids)
    sell = fill_markout(fill_row(T0, -1, 100.0, 100.5, order_id=2), mids)

    assert buy["markout_usd"] == pytest.approx(2.0)
    assert buy["markout_bps"] == pytest.approx(100.0)
    assert buy["adverse_bps"] == pytest.approx(50.0)
    assert sell["markout_usd"] == pytest.approx(-2.0)
    assert sell["markout_bps"] == pytest.approx(-100.0)
    assert sell["adverse_bps"] == pytest.approx(-50.0)


def test_mid_after_is_last_mid_at_or_before_target():
    mids = mids_from([(T0 + SEC // 2, 100.5), (T0 + 3 * SEC // 2, 110.0)])

    row = fill_markout(fill_row(T0, 1, 100.0, 100.0), mids, horizon_s=1)

    assert row["mid_after"] == pytest.approx(100.5)


def test_targets_after_last_mid_are_dropped():
    mids = mids_from([(T0, 100.5), (T0 + 3 * SEC // 2, 101.0)])
    fills = frame([fill_row(T0, 1, 100.0, 100.5)], FILLS_SCHEMA)

    result = markouts(fills, mids, SYMBOL, (1, 5))

    assert result["horizon_s"].to_list() == [1]


def test_markout_summary_counts_and_averages_by_group():
    rows = [
        {
            "symbol": SYMBOL,
            "ts": T0,
            "order_id": 1,
            "kind": "quote",
            "side": side,
            "px": 100.0,
            "qty": 1.0,
            "mid_at_fill": 100.0,
            "horizon_s": 1,
            "mid_after": 100.0,
            "markout_usd": usd,
            "markout_bps": bps,
            "adverse_bps": adverse,
        }
        for side, bps, adverse, usd in [
            (1, 10.0, 5.0, 1.0),
            (1, 20.0, 5.0, 2.0),
            (1, 30.0, 8.0, 3.0),
            (-1, 4.0, 1.0, -0.5),
        ]
    ]

    summary = markout_summary(frame(rows, MARKOUTS_SCHEMA))

    buy = summary.filter(pl.col("side") == 1).row(0, named=True)
    sell = summary.filter(pl.col("side") == -1).row(0, named=True)
    assert buy["n_fills"] == 3
    assert buy["avg_markout_bps"] == pytest.approx(20.0)
    assert buy["avg_adverse_bps"] == pytest.approx(6.0)
    assert buy["se_markout_bps"] == pytest.approx(10.0 / math.sqrt(3))
    assert buy["total_markout_usd"] == pytest.approx(6.0)
    assert sell["n_fills"] == 1
    assert sell["se_markout_bps"] is None


def test_period_pnl_telescopes_to_final_equity():
    samples = [
        (200_000_000, 1.0),
        (800_000_000, 2.0),
        (1_500_000_000, 5.0),
        (2_100_000_000, 4.0),
    ]
    timeseries = frame(
        [
            {
                "ts": ts,
                "mid": 100.0,
                "bid": 99.5,
                "ask": 100.5,
                "position": 0.0,
                "cash": 0.0,
                "equity": equity,
                "fees_cum": 0.0,
                "funding_cum": 0.0,
                "n_fills": 0,
                "quoting_bid": True,
                "quoting_ask": True,
            }
            for ts, equity in samples
        ],
        TIMESERIES_SCHEMA,
    )

    pnl = period_pnl(timeseries, SEC)

    assert pnl["period_start"].to_list() == [0, SEC, 2 * SEC]
    assert pnl["pnl"].to_list() == pytest.approx([2.0, 3.0, -1.0])
    assert pnl["pnl"].sum() == pytest.approx(4.0)
    assert pnl["pnl"][0] == pytest.approx(pnl["equity_end"][0])
