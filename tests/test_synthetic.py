from dataclasses import replace

import numpy as np
import polars as pl
import pytest
from polars.testing import assert_frame_equal

from quotelab.config import load_config
from quotelab.data.schema import BOOK_SCHEMA, TRADE_SCHEMA, processed_path
from quotelab.data.synthetic import (
    SyntheticConfig,
    generate,
    hawkes_times,
    simulate,
    synthetic_config,
    write_synthetic,
)

START_NS = 1_704_067_200 * 1_000_000_000


@pytest.fixture(scope="module")
def default_config() -> SyntheticConfig:
    return synthetic_config(load_config())


@pytest.fixture(scope="module")
def short_frames(default_config: SyntheticConfig) -> tuple[pl.DataFrame, pl.DataFrame]:
    return generate(replace(default_config, duration_s=600.0))


@pytest.fixture(scope="module")
def hour_run(default_config: SyntheticConfig):
    return simulate(default_config)


def test_same_seed_gives_identical_frames(default_config, short_frames):
    book, trades = generate(replace(default_config, duration_s=600.0))
    assert_frame_equal(book, short_frames[0])
    assert_frame_equal(trades, short_frames[1])


def test_different_seed_changes_frames(default_config, short_frames):
    other = replace(default_config, duration_s=600.0, seed=default_config.seed + 1)
    other_book, _ = generate(other)
    assert not other_book.equals(short_frames[0])


def test_frames_match_processed_schema(short_frames):
    book, trades = short_frames
    assert list(book.schema.items()) == list(BOOK_SCHEMA.items())
    assert list(trades.schema.items()) == list(TRADE_SCHEMA.items())


def test_book_is_valid_and_time_ordered(hour_run):
    book = hour_run.book
    assert (book["ask_px"] > book["bid_px"]).all()
    assert (book["bid_qty"] > 0).all()
    assert (book["ask_qty"] > 0).all()
    assert book["ts"].is_sorted()
    assert book["ts"][0] == START_NS
    assert hour_run.trades["ts"].is_sorted()


def test_market_orders_sweep_one_tick_per_level_from_the_touch(hour_run, default_config):
    trades = hour_run.trades.with_columns(
        (pl.col("ts") - 1).alias("lookup_ts"),
        pl.int_range(pl.len()).over("ts").alias("level"),
    )
    before = trades.join_asof(
        hour_run.book.rename({"ts": "book_ts"}),
        left_on="lookup_ts",
        right_on="book_ts",
        strategy="backward",
    )
    assert before["bid_px"].null_count() == 0
    is_sell = pl.col("is_buyer_maker")
    touch_px = pl.when(is_sell).then(pl.col("bid_px")).otherwise(pl.col("ask_px"))
    touch_qty = pl.when(is_sell).then(pl.col("bid_qty")).otherwise(pl.col("ask_qty"))
    step = pl.when(is_sell).then(-1).otherwise(1) * pl.col("level") * default_config.tick_size
    on_the_ladder = before.select((pl.col("px") - (touch_px + step)).abs() < 1e-9)
    assert on_the_ladder.to_series().all()
    first = before.filter(pl.col("level") == 0)
    assert first.select(pl.col("qty") <= touch_qty).to_series().all()
    assert before["level"].max() >= 2


def test_trade_count_is_poisson_without_self_excitation(default_config):
    sc = replace(default_config, hawkes_alpha=0.0, duration_s=3600.0)
    _, trades = generate(sc)
    expected = sc.trade_base_rate_hz * sc.duration_s
    market_orders = trades["ts"].n_unique()
    assert abs(market_orders - expected) <= 5 * np.sqrt(expected)


def test_hawkes_mean_count_matches_branching_formula():
    mu, alpha, beta, horizon = 1.0, 0.5, 2.0, 20000.0
    count = len(hawkes_times(mu, alpha, beta, horizon, np.random.default_rng(0)))
    assert count == pytest.approx(mu * horizon / (1 - alpha / beta), rel=0.10)
    assert count > 1.2 * mu * horizon


@pytest.mark.parametrize(("alpha", "beta"), [(2.0, 2.0), (3.0, 2.0), (-0.1, 2.0), (0.5, 0.0)])
def test_hawkes_rejects_non_stationary_or_invalid_kernel(alpha, beta):
    with pytest.raises(ValueError):
        hawkes_times(1.0, alpha, beta, 10.0, np.random.default_rng(0))


@pytest.mark.parametrize(("fraction", "low", "high"), [(1.0, 0.95, 1.0), (0.0, 0.4, 0.6)])
def test_informed_fraction_controls_alignment_with_next_move(default_config, fraction, low, high):
    sc = replace(default_config, informed_fraction=fraction, duration_s=1800.0)
    run = simulate(sc)
    next_idx = np.searchsorted(run.move_times, run.trade_times, side="right")
    has_next_move = next_idx < len(run.move_times)
    next_dirs = run.move_dirs[next_idx[has_next_move]]
    orders = run.trades.unique("ts", keep="first", maintain_order=True)
    trade_dirs = np.where(orders["is_buyer_maker"].to_numpy(), -1, 1)[has_next_move]
    alignment = np.mean(trade_dirs == next_dirs)
    assert low < alignment <= high


def test_write_synthetic_writes_one_dated_session_per_day(default_config, tmp_path):
    sc = replace(default_config, duration_s=600.0, sessions=2)
    written = write_synthetic(sc, tmp_path)
    days = ["2024-01-01", "2024-01-02"]
    expected = [
        processed_path(tmp_path, sc.symbol, kind, d) for d in days for kind in ("book", "trades")
    ]
    assert written == expected
    book, trades = generate(sc)
    assert_frame_equal(pl.read_parquet(expected[0]), book)
    assert_frame_equal(pl.read_parquet(expected[1]), trades)
    second_book = pl.read_parquet(expected[2])
    assert second_book["ts"][0] == START_NS + 86_400 * 1_000_000_000
    assert not second_book["bid_qty"].head(100).equals(book["bid_qty"].head(100))
