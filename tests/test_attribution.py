"""PnL attribution: the five components must add up to the engine's final equity to the cent.

The run below is built so that every component is non-zero: an optimistic fill model gives
many quote fills, a low hedge threshold forces taker hedges, and injected funding rows charge
funding on whatever position is open at each funding time.
"""

import copy
from dataclasses import replace

import polars as pl
import pytest

from quotelab.analytics.attribution import (
    COMPONENTS,
    AttributionMismatch,
    reconcile,
    run_attribution,
)
from quotelab.config import instrument_spec, load_config
from quotelab.data.loader import events_from_frames
from quotelab.data.synthetic import generate, synthetic_config
from quotelab.engine.backtest import run_backtest
from quotelab.engine.config import engine_config
from quotelab.fills.optimistic import OptimisticFill
from quotelab.risk.manager import RiskManager, risk_config
from quotelab.strategies.fixed import FixedSpread
from quotelab.strategies.quoting import QuotingParams
from quotelab.types import NS_PER_SECOND

DURATION_S = 1200
FUNDING_EVERY_S = 300
FUNDING_RATE = 0.0001


@pytest.fixture(scope="module")
def hedged_run():
    cfg = copy.deepcopy(load_config())
    cfg["risk"]["hedge_threshold_orders"] = 2
    sc = replace(synthetic_config(cfg), duration_s=DURATION_S)
    spec = instrument_spec(cfg, sc.symbol)
    book, trades = generate(sc)
    start = int(book["ts"][0])
    funding = pl.DataFrame(
        {
            "ts": [start + n * FUNDING_EVERY_S * NS_PER_SECOND for n in range(1, 4)],
            "rate": [FUNDING_RATE] * 3,
        }
    )
    order_lots = int(cfg["strategy"]["order_lots"][sc.symbol])
    params = QuotingParams(
        order_lots=order_lots,
        max_inventory_lots=10 * order_lots,
        fair_value="mid",
        ofi_beta=0.0,
        ofi_window_s=5.0,
        sigma_window_s=300.0,
        sigma_fallback=0.1,
    )
    return run_backtest(
        events_from_frames(spec, book, trades, funding),
        FixedSpread(spec, params, half_spread_ticks=0.5),
        OptimisticFill(),
        spec,
        engine_config(cfg),
        RiskManager(spec, risk_config(cfg), order_lots),
    )


def test_components_reconcile_to_final_equity_with_hedges_and_funding(hedged_run):
    parts = run_attribution(hedged_run)
    assert abs(reconcile(hedged_run)) < 0.01
    assert parts["total"] == pytest.approx(sum(parts[name] for name in COMPONENTS))
    assert hedged_run.summary["hedge_fills"] > 0
    assert hedged_run.funding.height == 3
    for name in COMPONENTS:
        assert parts[name] != 0.0, name


def test_reconcile_raises_when_equity_disagrees(hedged_run):
    summary = dict(hedged_run.summary, final_equity=hedged_run.summary["final_equity"] + 0.02)
    with pytest.raises(AttributionMismatch):
        reconcile(replace(hedged_run, summary=summary))
