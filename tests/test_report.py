"""Report: the E7 fee formula, wording that follows the sign, escaping, and the README block."""

import math
from pathlib import Path

import polars as pl
import pytest

from quotelab.report.animation import busiest_window_start
from quotelab.report.dashboard import render_dashboard
from quotelab.report.document import Section, Table, fmt_ci, markdown_table
from quotelab.report.results import update_readme
from quotelab.report.sections import gain_or_loss, usd
from quotelab.report.tables import RunOutputs, break_even_maker_bps, fee_tier_table

FEE_CFG = {"fees": {"maker_bps": 2.0}, "experiments": {"e7_maker_bps": [2.0, 0.0]}}


def baseline_outputs() -> RunOutputs:
    jobs = pl.DataFrame(
        {
            "experiment": ["E7", "E7"],
            "variant": ["baseline", "baseline"],
            "symbol": ["BTCUSDT", "BTCUSDT"],
            "test_date": ["2024-01-16", "2024-01-17"],
            "pnl": [-10.0, -30.0],
            "maker_volume_usd": [100_000.0, 100_000.0],
        }
    )
    return RunOutputs(Path("."), jobs, pl.DataFrame())


def test_fee_tiers_add_back_the_fee_difference_on_maker_volume():
    tiers = fee_tier_table(baseline_outputs(), FEE_CFG)
    at_zero = tiers.filter(pl.col("maker_bps") == 0.0).row(0, named=True)
    assert at_zero["pnl_mean"] == pytest.approx((10.0 + -10.0) / 2)
    at_base = tiers.filter(pl.col("maker_bps") == 2.0).row(0, named=True)
    assert at_base["pnl_mean"] == pytest.approx(-20.0)


def test_break_even_fee_zeroes_total_pnl():
    row = break_even_maker_bps(baseline_outputs(), FEE_CFG).row(0, named=True)
    assert row["break_even_maker_bps"] == pytest.approx(2.0 - 40.0 / 200_000.0 * 10_000)


def test_wording_follows_the_sign():
    assert usd(-1129.03) == "-$1,129.03"
    assert gain_or_loss(-5.0) == "a loss of $5.00"
    assert gain_or_loss(5.0) == "a gain of $5.00"
    assert gain_or_loss(1e-9) == "no change"
    assert gain_or_loss(math.nan) == "an undefined change"


def test_one_day_has_no_interval_and_pipes_do_not_split_cells():
    assert fmt_ci(-3.0, math.nan, math.nan) == "-3.00"
    rendered = markdown_table(Table("t", ["Mean |position|"], [["1"]]))
    assert "Mean \\|position\\|" in rendered


def test_dashboard_escapes_text():
    section = Section("e1", "E1", ["<script>alert(1)</script> and `code`"])
    page = render_dashboard("QuoteLab", "sub", [], [section])
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page and "<code>code</code>" in page


def test_readme_block_replaces_only_its_own_dataset(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("intro\n")
    update_readme(readme, "synthetic", "old synthetic")
    update_readme(readme, "real", "real block")
    update_readme(readme, "synthetic", "new synthetic")
    text = readme.read_text()
    assert "old synthetic" not in text
    assert text.count("new synthetic") == 1 and "real block" in text
    assert text.startswith("intro\n")


def test_busiest_window_counts_only_quote_fills():
    fills = pl.DataFrame(
        {
            "ts": [1, 2, 11, 12, 13, 3, 4, 5, 6, 7],
            "kind": ["quote", "quote", "quote", "quote", "quote"] + ["hedge"] * 5,
        },
        schema={"ts": pl.Int64, "kind": pl.String},
    )
    assert busiest_window_start(fills, window_ns=10) == 10


def test_busiest_window_tie_goes_to_the_earlier_bucket():
    fills = pl.DataFrame(
        {"ts": [11, 12, 1, 2], "kind": ["quote"] * 4},
        schema={"ts": pl.Int64, "kind": pl.String},
    )
    assert busiest_window_start(fills, window_ns=10) == 0
