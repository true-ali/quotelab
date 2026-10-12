import math

import pytest

from quotelab.analytics.stats import bootstrap_mean_ci, max_drawdown, run_metrics
from quotelab.engine.recorder import (
    FILLS_SCHEMA,
    FUNDING_SCHEMA,
    TIMESERIES_SCHEMA,
    BacktestResult,
    frame,
    risk_events_frame,
)

EQUITY = [0.0, 5.0, 3.0, 8.0, 2.0, 4.0]
POSITION = [0.0, 2.0, -1.0, 3.0, -2.0, 0.0]


def fill_row(order_id: int, side: int, px: float, qty: float, is_maker: bool) -> dict:
    return {
        "ts": 1000 * order_id,
        "order_id": order_id,
        "side": side,
        "px": px,
        "qty": qty,
        "fee": 0.0,
        "is_maker": is_maker,
        "kind": "quote" if is_maker else "hedge",
        "mid": px,
        "position": 0.0,
    }


def timeseries_row(ts: int, equity: float, position: float) -> dict:
    return {
        "ts": ts,
        "mid": 100.0,
        "bid": 99.5,
        "ask": 100.5,
        "position": position,
        "cash": 0.0,
        "equity": equity,
        "fees_cum": 0.0,
        "funding_cum": 0.0,
        "n_fills": 0,
        "quoting_bid": True,
        "quoting_ask": True,
    }


def sample_run() -> BacktestResult:
    return BacktestResult(
        fills=frame(
            [
                fill_row(1, side=1, px=100.0, qty=2.0, is_maker=True),
                fill_row(2, side=-1, px=101.0, qty=1.0, is_maker=True),
                fill_row(3, side=-1, px=101.5, qty=1.0, is_maker=False),
            ],
            FILLS_SCHEMA,
        ),
        timeseries=frame(
            [
                timeseries_row(ts, equity, position)
                for ts, (equity, position) in enumerate(zip(EQUITY, POSITION, strict=True))
            ],
            TIMESERIES_SCHEMA,
        ),
        funding=frame([], FUNDING_SCHEMA),
        risk_events=risk_events_frame([]),
        summary={
            "final_equity": 3.01,
            "orders_sent": 8,
            "uptime_both": 0.5,
            "fees": 0.25,
            "funding": -0.1,
        },
    )


def test_max_drawdown_is_largest_peak_to_trough_fall():
    assert max_drawdown([0, 5, 3, 8, 2, 4]) == 6


def test_max_drawdown_is_zero_for_rising_or_empty_curve():
    assert max_drawdown([1, 2, 3]) == 0
    assert max_drawdown([]) == 0


def test_bootstrap_is_deterministic_for_same_seed():
    values = [1.0, -2.0, 3.5, 0.5, 2.0]

    assert bootstrap_mean_ci(values, seed=7) == bootstrap_mean_ci(values, seed=7)


def test_bootstrap_interval_brackets_the_mean():
    mean, lo, hi = bootstrap_mean_ci([1.0, -2.0, 3.5, 0.5, 2.0])

    assert lo <= mean <= hi


def test_bootstrap_of_constant_series_has_zero_width_interval():
    assert bootstrap_mean_ci([2.5, 2.5, 2.5, 2.5]) == (2.5, 2.5, 2.5)


def test_bootstrap_with_fewer_than_two_values_has_nan_bounds():
    mean, lo, hi = bootstrap_mean_ci([4.0])

    assert mean == 4.0
    assert math.isnan(lo) and math.isnan(hi)
    assert all(math.isnan(x) for x in bootstrap_mean_ci([]))


def test_run_metrics_match_hand_computed_values():
    metrics = run_metrics(sample_run())

    assert metrics == pytest.approx(
        {
            "pnl": 3.01,
            "quote_fills": 2.0,
            "fill_rate": 0.25,
            "maker_volume_usd": 301.0,
            "pnl_per_volume_bps": 100.0,
            "max_drawdown_usd": 6.0,
            "mean_abs_inventory": 8 / 6,
            "max_abs_inventory": 3.0,
            "uptime_both": 0.5,
            "fees": 0.25,
            "funding": -0.1,
            "hedge_fills": 1.0,
        }
    )


def test_run_metrics_of_empty_run_use_neutral_values():
    empty = BacktestResult(
        fills=frame([], FILLS_SCHEMA),
        timeseries=frame([], TIMESERIES_SCHEMA),
        funding=frame([], FUNDING_SCHEMA),
        risk_events=risk_events_frame([]),
        summary={
            "final_equity": 0.0,
            "orders_sent": 0,
            "uptime_both": 0.0,
            "fees": 0.0,
            "funding": 0.0,
        },
    )

    metrics = run_metrics(empty)

    assert metrics["fill_rate"] == 0.0
    assert math.isnan(metrics["pnl_per_volume_bps"])
    assert metrics["mean_abs_inventory"] == 0.0
    assert metrics["max_abs_inventory"] == 0.0
    assert metrics["max_drawdown_usd"] == 0.0
