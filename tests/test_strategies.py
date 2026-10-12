"""Quoting maths and rules checked against closed forms and hand-worked examples.

Covers the GLFT offsets and gamma back-solve, the Avellaneda-Stoikov offsets and horizons, the
tick rounding, crossing clamps, inventory bound and sigma fallback in ``QuotingStrategy``, the
strategy factory, and one calibration run on the seeded synthetic data. All offline and
deterministic.
"""

import math

import pytest

from quotelab.config import instrument_spec, load_config
from quotelab.data.synthetic import generate, synthetic_config
from quotelab.strategies.avellaneda_stoikov import (
    AvellanedaStoikov,
    as_offsets,
    seconds_to_utc_midnight,
)
from quotelab.strategies.calibration import Calibration, calibrate, calibration_config
from quotelab.strategies.factory import STRATEGY_NAMES, build_strategy, matched_as_horizon
from quotelab.strategies.fixed import FixedSpread, InventorySkew
from quotelab.strategies.glft import Glft, gamma_from_skew, glft_coefficients, glft_offsets
from quotelab.strategies.quoting import QuotingParams, QuotingStrategy
from quotelab.types import BookEvent, InstrumentSpec

SPEC = InstrumentSpec("X", tick_size=0.1, lot_size=0.001, min_notional=5.0)
DAY_START_NS = 1_705_276_800 * 10**9
GAMMA = 0.1
SIGMA = 1.0
FILL_A = 1.1
FILL_K = 1.2
DELTA = 0.003


def make_params(
    order_lots: int = 10, max_inventory_lots: int = 30, sigma_fallback: float = 3.0
) -> QuotingParams:
    return QuotingParams(
        order_lots=order_lots,
        max_inventory_lots=max_inventory_lots,
        fair_value="mid",
        ofi_beta=0.0,
        ofi_window_s=5.0,
        sigma_window_s=300.0,
        sigma_fallback=sigma_fallback,
    )


def make_book(bid_px: int, ask_px: int, ts: int = 0) -> BookEvent:
    return BookEvent(ts=ts, symbol="X", bid_px=bid_px, bid_qty=100, ask_px=ask_px, ask_qty=100)


def make_calibration() -> Calibration:
    return Calibration(
        symbol="BTCUSDT",
        period="test",
        start_ts=0,
        end_ts=3_600 * 10**9,
        sigma=3.0,
        sigma_klines=None,
        a=1.1,
        a_se=0.05,
        k=1.2,
        k_se=0.05,
        intensity_r2=0.95,
        intensity_levels_used=5,
        levels=(),
        ofi_beta=0.5,
        ofi_beta_se=0.1,
        ofi_r2=0.1,
    )


class FixedOffsets(QuotingStrategy):
    def __init__(
        self, spec: InstrumentSpec, params: QuotingParams, bid_off: float, ask_off: float
    ) -> None:
        super().__init__(spec, params)
        self.fixed = (bid_off, ask_off)

    def offsets(self, sigma: float) -> tuple[float, float]:
        return self.fixed


def test_total_spread_at_zero_inventory_grows_with_sigma():
    spreads = []
    for sigma in (0.5, 1.0, 2.0):
        coefficients = glft_coefficients(GAMMA, sigma, FILL_A, FILL_K, DELTA)
        bid_off, ask_off = glft_offsets(0.0, DELTA, *coefficients)
        spreads.append(bid_off + ask_off)
    assert spreads[0] < spreads[1] < spreads[2]


def test_higher_gamma_raises_c2_and_lowers_c1():
    """Higher risk aversion trades a little spread for much stronger inventory skew."""
    coefficients = [
        glft_coefficients(gamma, SIGMA, FILL_A, FILL_K, DELTA) for gamma in (0.01, 0.1, 1.0, 10.0)
    ]
    c1s = [c1 for c1, _ in coefficients]
    c2s = [c2 for _, c2 in coefficients]
    assert c2s[0] < c2s[1] < c2s[2] < c2s[3]
    assert c1s[0] > c1s[1] > c1s[2] > c1s[3]


def test_offsets_are_symmetric_at_zero_inventory():
    bid_off, ask_off = glft_offsets(
        0.0, DELTA, *glft_coefficients(GAMMA, SIGMA, FILL_A, FILL_K, DELTA)
    )
    assert bid_off == pytest.approx(ask_off, rel=1e-12)


def test_inventory_skews_both_quotes_toward_flattening():
    c1, c2 = glft_coefficients(GAMMA, SIGMA, FILL_A, FILL_K, DELTA)
    long_bid, long_ask = glft_offsets(2 * DELTA, DELTA, c1, c2)
    short_bid, short_ask = glft_offsets(-2 * DELTA, DELTA, c1, c2)
    assert long_bid > long_ask
    assert short_bid < short_ask


def test_delta_one_matches_glft_2013_closed_form():
    gamma, sigma, a, k = 0.5, 2.0, 1.1, 1.2
    c1, c2 = glft_coefficients(gamma, sigma, a, k, 1.0)
    half = (1 / gamma) * math.log(1 + gamma / k)
    scale = math.sqrt(sigma**2 * gamma / (2 * k * a) * (1 + gamma / k) ** (1 + k / gamma))
    assert c2 == pytest.approx(scale, rel=1e-12)
    for q in (-2.0, 0.0, 3.0):
        bid_off, ask_off = glft_offsets(q, 1.0, c1, c2)
        assert bid_off == pytest.approx(half + (2 * q + 1) / 2 * scale, rel=1e-12)
        assert ask_off == pytest.approx(half - (2 * q - 1) / 2 * scale, rel=1e-12)


def test_gamma_from_skew_round_trips_and_rejects_non_positive_targets():
    gamma = gamma_from_skew(0.1, 3.0, FILL_A, FILL_K, DELTA)
    _, c2 = glft_coefficients(gamma, 3.0, FILL_A, FILL_K, DELTA)
    assert DELTA * c2 == pytest.approx(0.1, rel=1e-9)
    with pytest.raises(ValueError):
        gamma_from_skew(0.0, 3.0, FILL_A, FILL_K, DELTA)


def test_matched_as_horizon_quotes_like_glft_at_calibrated_sigma():
    """The matched rolling horizon makes A-S and GLFT quote identically at the calibrated sigma."""
    cal = make_calibration()
    gamma = gamma_from_skew(0.1, cal.sigma, cal.a, cal.k, DELTA)
    tau = matched_as_horizon(gamma, cal, DELTA)
    glft_c1_c2 = glft_coefficients(gamma, cal.sigma, cal.a, cal.k, DELTA)
    for q in (-0.009, 0.0, 0.006):
        as_bid, as_ask = as_offsets(q, DELTA, gamma, cal.sigma, cal.k, tau)
        glft_bid, glft_ask = glft_offsets(q, DELTA, *glft_c1_c2)
        assert (as_bid, as_ask) == pytest.approx((glft_bid, glft_ask), rel=1e-9)


def test_utc_midnight_countdown_and_session_versus_rolling_horizon():
    assert seconds_to_utc_midnight(DAY_START_NS) == 86_400.0
    assert seconds_to_utc_midnight(DAY_START_NS + 86_399 * 10**9) == 1.0
    t = DAY_START_NS + 3_600 * 10**9
    session = AvellanedaStoikov(SPEC, make_params(), GAMMA, FILL_K, horizon_s=None)
    session.on_timer(t)
    assert session.tau() == seconds_to_utc_midnight(t)
    rolling = AvellanedaStoikov(SPEC, make_params(), GAMMA, FILL_K, horizon_s=10.0)
    assert rolling.tau() == 10.0


def test_quotes_round_bid_down_and_ask_up_to_ticks():
    strategy = FixedSpread(SPEC, make_params(), half_spread_ticks=2.5)
    quotes = strategy.on_book(make_book(1000, 1010))
    assert quotes.bid.px == 1002
    assert quotes.ask.px == 1008


def test_crossing_quotes_are_clamped_one_tick_inside_the_touch():
    book = make_book(1000, 1001)
    params = make_params(order_lots=1, max_inventory_lots=10)
    bid_side = FixedOffsets(SPEC, params, bid_off=-1.0, ask_off=0.05)
    quotes = bid_side.on_book(book)
    assert quotes.bid.px == 1000
    assert bid_side.bid_clamps == 1
    assert bid_side.ask_clamps == 0
    assert bid_side.quote_updates == 1
    ask_side = FixedOffsets(SPEC, params, bid_off=0.05, ask_off=-1.0)
    quotes = ask_side.on_book(book)
    assert quotes.ask.px == 1001
    assert ask_side.ask_clamps == 1
    assert ask_side.bid_clamps == 0


def test_side_is_dropped_when_one_more_fill_breaches_the_inventory_bound():
    strategy = FixedSpread(SPEC, make_params(order_lots=10, max_inventory_lots=30), 0.5)
    book = make_book(1000, 1010)
    strategy.position = 21
    quotes = strategy.on_book(book)
    assert quotes.bid is None
    assert quotes.ask is not None
    strategy.position = 20
    quotes = strategy.on_book(book)
    assert quotes.bid is not None
    assert quotes.ask is not None
    strategy.position = -21
    quotes = strategy.on_book(book)
    assert quotes.ask is None
    assert quotes.bid is not None


def test_sigma_is_the_fallback_until_the_volatility_window_is_warm():
    strategy = FixedSpread(SPEC, make_params(sigma_fallback=2.5), 0.5)
    for ts, bid_px in ((0, 1000), (1_000_000_000, 1001), (2_000_000_000, 1003)):
        strategy.on_book(make_book(bid_px, bid_px + 10, ts=ts))
    assert not strategy.vol.is_warm()
    assert strategy.vol.sigma() == 2.5


def test_factory_builds_each_named_strategy_and_rejects_bad_input():
    cfg = load_config()
    spec = instrument_spec(cfg, "BTCUSDT")
    cal = make_calibration()
    expected = {"fixed": FixedSpread, "skew": InventorySkew, "as": AvellanedaStoikov, "glft": Glft}
    assert set(expected) == set(STRATEGY_NAMES)
    for name, cls in expected.items():
        assert type(build_strategy(cfg["strategy"], spec, cal, name=name)) is cls
    with pytest.raises(ValueError):
        build_strategy(cfg["strategy"], spec, cal, name="nope")
    bogus_horizon = {**cfg["strategy"], "as_horizon": "bogus"}
    with pytest.raises(ValueError):
        build_strategy(bogus_horizon, spec, cal, name="as")


def test_calibration_recovers_sane_parameters_from_synthetic_data():
    """The synthetic generator sweeps one tick per emptied level, so the fill intensity decays
    exponentially with distance and the log-linear fit is near perfect. Real data fits worse,
    which is a finding and not tested here."""
    cfg = load_config()
    sc = synthetic_config(cfg)
    book, trades = generate(sc)
    spec = instrument_spec(cfg, sc.symbol)
    cal = calibrate(spec, book, trades, calibration_config(cfg), "synthetic")
    assert cal.k > 0
    assert cal.a > 0
    assert cal.sigma > 0
    assert cal.intensity_r2 > 0.9
    assert cal.intensity_levels_used >= 3
