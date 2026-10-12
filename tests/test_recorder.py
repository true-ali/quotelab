from pathlib import Path

from polars.testing import assert_frame_equal

from quotelab.engine.recorder import (
    FILLS_SCHEMA,
    FUNDING_SCHEMA,
    TIMESERIES_SCHEMA,
    BacktestResult,
    frame,
    read_run,
    risk_events_frame,
    write_run,
)
from quotelab.risk.base import RiskEvent


def sample_result() -> BacktestResult:
    return BacktestResult(
        fills=frame(
            [
                {
                    "ts": 2000,
                    "order_id": 1,
                    "side": 1,
                    "px": 39999.0,
                    "qty": 0.1,
                    "fee": 0.79998,
                    "is_maker": True,
                    "kind": "quote",
                    "mid": 40000.5,
                    "position": 0.1,
                }
            ],
            FILLS_SCHEMA,
        ),
        timeseries=frame(
            [
                {
                    "ts": 1000,
                    "mid": 40000.5,
                    "bid": 40000.0,
                    "ask": 40001.0,
                    "position": 0.1,
                    "cash": -3999.9,
                    "equity": 0.6,
                    "fees_cum": 0.79998,
                    "funding_cum": 0.0,
                    "n_fills": 1,
                    "quoting_bid": True,
                    "quoting_ask": False,
                }
            ],
            TIMESERIES_SCHEMA,
        ),
        funding=frame(
            [{"ts": 3000, "rate": 0.0001, "mark": 40000.5, "position": 0.1, "payment": 0.4}],
            FUNDING_SCHEMA,
        ),
        risk_events=risk_events_frame(
            [RiskEvent(ts=4000, kind="kill_switch", detail="drawdown 512.3 > 500")]
        ),
        summary={"fills": 1, "final_equity": 0.6, "label": "run"},
    )


def empty_result() -> BacktestResult:
    return BacktestResult(
        fills=frame([], FILLS_SCHEMA),
        timeseries=frame([], TIMESERIES_SCHEMA),
        funding=frame([], FUNDING_SCHEMA),
        risk_events=risk_events_frame([]),
        summary={},
    )


def assert_same_run(loaded: BacktestResult, original: BacktestResult) -> None:
    assert_frame_equal(loaded.fills, original.fills)
    assert_frame_equal(loaded.timeseries, original.timeseries)
    assert_frame_equal(loaded.funding, original.funding)
    assert_frame_equal(loaded.risk_events, original.risk_events)
    assert loaded.summary == original.summary


def test_run_with_one_row_per_table_round_trips_through_parquet_and_json(tmp_path: Path):
    original = sample_result()

    write_run(original, tmp_path / "run")

    assert_same_run(read_run(tmp_path / "run"), original)


def test_empty_run_round_trips_with_its_schema(tmp_path: Path):
    original = empty_result()

    write_run(original, tmp_path / "run")

    loaded = read_run(tmp_path / "run")
    assert loaded.fills.height == 0
    assert loaded.risk_events.height == 0
    assert_same_run(loaded, original)
