"""The report's sections: tables of formatted cells and paragraphs generated from the numbers.

Every number in a paragraph is formatted from a table when the report is built, never typed.
Where the wording depends on a number's sign ("a gain of", "a loss of"), a helper picks it, so
the text cannot contradict the tables when the data change.

``FEW_DAYS``: below this many test days per cell the introduction calls the bootstrap intervals
crude. Ten is a judgement call, not a statistical threshold.
``SAME_USD``: two dollar amounts closer than this are reported as equal (floating-point noise).
``PROSE_HORIZON_S``: the markout horizon quoted in the text; the tables show every horizon.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import polars as pl

from quotelab.report.document import Section, Table, fmt, fmt_ci, fmt_pct
from quotelab.report.tables import ReportTables

FEW_DAYS = 10
SAME_USD = 1e-6
PROSE_HORIZON_S = 5
NORMAL_95 = 1.96
NS_PER_MINUTE = 60_000_000_000
MILLION = 1e6
BILLION = 1e9
FIGURES = {
    "calibration": "figures/calibration.png",
    "e1": "figures/e1_realism_ladder.png",
    "e2": "figures/e2_strategies.png",
    "e3": "figures/e3_sensitivity.png",
    "e4": "figures/e4_latency.png",
    "e5": "figures/e5_regimes.png",
    "e6": "figures/e6_event_study.png",
    "e7": "figures/e7_fee_tiers.png",
    "markouts": "figures/markouts.png",
}
SIDE_NAMES = {1: "buy", -1: "sell"}

Row = dict[str, Any]
Column = tuple[str, Callable[[Row], str]]


def usd(value: float | None) -> str:
    """A dollar amount with its sign in front, e.g. ``-$1,129.03``."""
    if value is None or math.isnan(value):
        return "n/a"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def gain_or_loss(value: float) -> str:
    """``a gain of $X``, ``a loss of $X`` or ``no change``, so the word always follows the sign."""
    if math.isnan(value):
        return "an undefined change"
    if abs(value) < SAME_USD:
        return "no change"
    return f"a {'gain' if value > 0 else 'loss'} of {usd(abs(value))}"


def span(values: pl.Series, digits: int = 2) -> str:
    """``lo to hi`` of a column, or a single value when every entry is the same."""
    lo, hi = fmt(values.min(), digits), fmt(values.max(), digits)
    return lo if lo == hi else f"{lo} to {hi}"


def rows_of(frame: pl.DataFrame, **equal: Any) -> list[Row]:
    """Rows of ``frame`` whose columns equal the given values, as dicts, in frame order."""
    for column, value in equal.items():
        frame = frame.filter(pl.col(column) == value)
    return list(frame.iter_rows(named=True))


def one_row(frame: pl.DataFrame, **equal: Any) -> Row:
    """The single row matching ``equal``; anything else is a bug in the tables, so it raises."""
    rows = rows_of(frame, **equal)
    if len(rows) != 1:
        raise ValueError(f"expected one row for {equal}, found {len(rows)}")
    return rows[0]


def symbols_in(frame: pl.DataFrame) -> list[str]:
    return frame["symbol"].unique(maintain_order=True).to_list()


def market_events(t: ReportTables) -> float:
    """Market events replayed across every run of the dataset."""
    return float((t.run_cost["runs"] * t.run_cost["mean_events_per_run"]).sum())


def market_events_scaled(t: ReportTables) -> tuple[str, str]:
    """The market events replayed as a short number and its unit, ``("6.0", "billion")`` or
    ``("37.5", "million")``, so a small dataset does not read as 0.0 billion."""
    events = market_events(t)
    if events >= BILLION:
        return f"{events / BILLION:,.1f}", "billion"
    return f"{events / MILLION:,.1f}", "million"


def needs_rebate(t: ReportTables) -> bool:
    """True when every symbol's break-even maker fee is below zero, so only a rebate pays."""
    return bool((t.break_even["break_even_maker_bps"] < 0).all())


def rung_pnl(t: ReportTables, symbol: str, rung: str) -> str:
    """Mean daily PnL of one E1 rung for one symbol, as a signed dollar amount."""
    return usd(one_row(t.realism, symbol=symbol, variant=rung)["pnl_mean"])


def frame_table(title: str, frame: pl.DataFrame, columns: list[Column]) -> Table:
    """A ``Table`` with one row per frame row; each column is a header and a cell formatter."""
    rows = [[render(row) for _, render in columns] for row in frame.iter_rows(named=True)]
    return Table(title, [header for header, _ in columns], rows)


def num(name: str, digits: int = 2) -> Callable[[Row], str]:
    return lambda row: fmt(row[name], digits)


def ci(prefix: str, digits: int = 2) -> Callable[[Row], str]:
    """Cell ``mean [lo, hi]`` from the columns ``<prefix>_mean``, ``_lo`` and ``_hi``."""
    return lambda row: fmt_ci(
        row[f"{prefix}_mean"], row[f"{prefix}_lo"], row[f"{prefix}_hi"], digits
    )


def pct(name: str) -> Callable[[Row], str]:
    return lambda row: fmt_pct(row[name])


def text(name: str) -> Callable[[Row], str]:
    return lambda row: "none" if row[name] is None else str(row[name])


def utc_time(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=UTC).strftime("%Y-%m-%d %H:%M")


def intro(t: ReportTables, cfg: dict[str, Any], dataset: str) -> list[str]:
    """What data the report covers, how many runs, how to read the intervals, and what it is not."""
    days = len(t.test_dates)
    paragraphs = [
        _data_paragraph(t, cfg, dataset),
        f"{t.n_jobs} experiment jobs ran as {t.n_runs} distinct backtests: jobs whose "
        "configuration is identical (the E1 `queue_latency` rung, the E2 `glft` strategy, the "
        "E4 baseline latency and the E7 baseline) share one run.",
    ]
    interval = (
        "Values in brackets are 95% block-bootstrap intervals of the mean over test days, "
        "resampling whole days."
    )
    if days < FEW_DAYS:
        interval += (
            f" With {days} test days per cell they are crude: read them as a rough range, not as "
            "a significance test. With one day there is no interval."
        )
    paragraphs.append(interval)
    paragraphs.append(
        "Everything here is a simulation with an estimated queue position, no market impact "
        "and a single venue. Nothing in it is evidence that a strategy would "
        "make money live, and none of it is investment advice."
    )
    return paragraphs


def _data_paragraph(t: ReportTables, cfg: dict[str, Any], dataset: str) -> str:
    days = len(t.test_dates)
    if dataset == "synthetic":
        sc = cfg["synthetic"]
        hours = float(sc["duration_s"]) / 3600
        return (
            f"Seeded synthetic order flow for `{sc['symbol']}`: {sc['sessions']} sessions of "
            f"{hours:g} hour(s) from seed {sc['seed']}. Each session runs with parameters "
            f"calibrated on the session before it, which leaves {days} test sessions. The "
            "generator's parameters are chosen, not fitted to a market, so these numbers test "
            "the pipeline and say nothing about real trading."
        )
    symbols = ", ".join(f"`{symbol}`" for symbol in t.symbols)
    return (
        f"Public Binance USD-M perpetual futures data for {symbols}: top of book (`bookTicker`) "
        f"and trade prints, no order-book depth. {days} test days from {t.test_dates[0]} to "
        f"{t.test_dates[-1]}; each test day runs with parameters calibrated on the day before "
        "it and on nothing later (walk-forward)."
    )


def calibration_section(t: ReportTables, cfg: dict[str, Any]) -> Section:
    """Fitted parameters per calibration day, with how well the intensity model fits."""
    paragraphs = [
        "`sigma` is the root mean square of one-second mid changes in USD per √s. `A` and `k` "
        "come from an ordinary least-squares fit of ln λ(δ) = ln A − kδ, where λ(δ) is the "
        "number of aggressive orders per second whose deepest print reached δ USD past the mid "
        f"just before them, both sides pooled; levels with fewer than "
        f"{cfg['calibration']['min_hits']} hits are left out of the fit. `ofi_beta` is the "
        "slope of the next second's mid change on the normalised order-flow imbalance of the "
        "trailing window.",
    ]
    paragraphs += [_calibration_paragraph(t, cfg, symbol) for symbol in symbols_in(t.calibration)]
    table = frame_table(
        "Calibration per day (each is used to run the following day)",
        t.calibration,
        [
            ("Symbol", text("symbol")),
            ("Calibrated on", text("date")),
            ("sigma (USD/√s)", num("sigma", 4)),
            ("sigma, klines", num("sigma_klines", 4)),
            ("A (1/s)", num("a", 3)),
            ("A s.e.", num("a_se", 3)),
            ("k (1/USD)", num("k", 3)),
            ("k s.e.", num("k_se", 3)),
            ("R²", num("intensity_r2", 3)),
            ("Levels", num("levels_used", 0)),
            ("Touch share of hits", pct("touch_hit_share")),
            ("OFI beta (USD)", num("ofi_beta", 4)),
            ("OFI R²", num("ofi_r2", 4)),
        ],
    )
    return Section("calibration", "Calibration", paragraphs, [table], FIGURES["calibration"])


def _calibration_paragraph(t: ReportTables, cfg: dict[str, Any], symbol: str) -> str:
    rows = t.calibration.filter(pl.col("symbol") == symbol)
    tick = float(cfg["instruments"][symbol]["tick_size"])
    k_per_tick = float(rows["k"].mean()) * tick
    touch = (
        t.intensity.filter(pl.col("symbol") == symbol)
        .sort("date", "delta_usd")
        .group_by("date", maintain_order=True)
        .first()
    )
    touch_ratio = touch["rate"] / (touch["a"] * (-touch["k"] * touch["delta_usd"]).exp())
    klines = rows["sigma_klines"].drop_nulls()
    cross_check = f" (1-minute klines: {span(klines, 4)})" if klines.len() else ""
    parts = [
        f"`{symbol}`, {rows.height} calibration days: sigma {span(rows['sigma'], 4)} USD/√s"
        f"{cross_check}. k averaged {fmt(k_per_tick, 3)} per tick of {tick:g} USD, so the fitted "
        f"hit rate falls by a factor of e every {fmt(1 / k_per_tick, 1)} ticks; R² "
        f"{span(rows['intensity_r2'], 3)} over {span(rows['levels_used'], 0)} distance levels.",
        f"The first level, the touch, carried {span(rows['touch_hit_share'] * 100, 1)}% of all "
        f"hits, and its measured rate was {span(touch_ratio, 1)} times the fitted curve there.",
    ]
    if bool((touch_ratio > 1).all()):
        parts.append(
            "The touch is under-fitted on every day: most takers stop at the best price, a point "
            "mass that a single exponential cannot carry, so `A` understates fills at the touch "
            "and overstates them a few ticks away."
        )
    parts.append(
        f"The OFI slope averaged {fmt(float(rows['ofi_beta'].mean()), 4)} USD per unit of "
        f"imbalance with R² at most {fmt_pct(float(rows['ofi_r2'].max()), 2)}: that is the most "
        "of the next second's mid variance the imbalance explained on any day."
    )
    return " ".join(parts)


def realism_section(t: ReportTables, cfg: dict[str, Any]) -> Section:
    """E1: how much PnL each step towards realistic fills and latency removes."""
    engine, stress = cfg["engine"], cfg["experiments"]["e1_stress"]
    paragraphs = [
        "The same `glft` strategy, calibration and days under five fill and latency assumptions. "
        "`optimistic`: any touch of our price fills the whole order. "
        "`trade_through`: only a trade strictly through our price fills it. "
        "`queue`: the lots queued ahead of us, estimated from top-of-book sizes, must trade away "
        "first. These three have no latency. `queue_latency` adds "
        f"{engine['data_latency_ms']:g} ms market-data and {engine['order_latency_ms']:g} ms "
        "order latency and is the baseline of every other experiment. `queue_latency_stress` "
        f"is the adverse-selection stress: `alpha` = {float(stress['alpha']):g}, so no "
        "unexplained drop in size at our price counts as a cancellation ahead of us, and "
        f"`participation` = {float(stress['participation']):g}, so only that share of the "
        "volume traded at our price reaches our queue. The rungs are not nested bounds on "
        "fills: with latency a quote keeps its old price until the trader's update or cancel "
        "arrives, and it can fill in that window, so a latency rung can fill more often than "
        "`optimistic`.",
    ]
    paragraphs += [_realism_paragraph(t, symbol) for symbol in symbols_in(t.realism)]
    paragraphs.append(
        "What this can say: how much of the simulated PnL rests on each fill assumption, with "
        "everything else held fixed. What it cannot say: which rung is right. With top-of-book "
        "data the queue ahead of us is never observed, so `queue` is an estimate and the stress "
        "rung is an assumption about it, not a measurement. No rung models the market impact of "
        "our own orders, and the ladder only brackets reality if reality lies between the rungs, "
        "which this data cannot show."
    )
    table = frame_table(
        "E1 realism ladder (USD per day unless stated)",
        t.realism,
        [
            ("Rung", text("variant")),
            ("Symbol", text("symbol")),
            ("Days", num("n_days", 0)),
            ("Mean daily PnL [95% CI]", ci("pnl")),
            ("Change vs previous rung", num("change_vs_previous")),
            ("PnL before maker fees", num("pnl_before_fees")),
            ("Quote fills", num("quote_fills", 0)),
            (f"Adverse {PROSE_HORIZON_S} s (bps)", num(f"adverse_bps_{PROSE_HORIZON_S}s")),
        ],
    )
    return Section("e1", "E1 · Realism ladder", paragraphs, [table], FIGURES["e1"])


def _realism_paragraph(t: ReportTables, symbol: str) -> str:
    rows = rows_of(t.realism, symbol=symbol)
    first, last = rows[0], rows[-1]
    baseline = one_row(t.realism, symbol=symbol, variant="queue_latency")
    steps = ", ".join(f"`{r['variant']}` {usd(r['change_vs_previous'])}" for r in rows[1:])
    worst = min(rows[1:], key=lambda r: r["change_vs_previous"])
    parts = [
        f"`{symbol}`: mean daily PnL {usd(first['pnl_mean'])} under `{first['variant']}` and "
        f"{usd(last['pnl_mean'])} under `{last['variant']}`, a difference of "
        f"{usd(last['pnl_mean'] - first['pnl_mean'])} a day. Change at each step: {steps}.",
    ]
    if worst["change_vs_previous"] < -SAME_USD:
        parts.append(
            f"The largest single drop came at `{worst['variant']}` "
            f"({usd(worst['change_vs_previous'])})."
        )
    else:
        parts.append("No step lowered the mean daily PnL.")
    parts.append(
        f"Before maker fees the first and last rungs made {usd(first['pnl_before_fees'])} and "
        f"{usd(last['pnl_before_fees'])}; maker fees contributed {usd(baseline['attr_fees'])} a "
        f"day to the baseline on {usd(baseline['maker_volume_usd'])} of maker volume. Quote "
        f"fills went from {fmt(first['quote_fills'], 0)} to {fmt(last['quote_fills'], 0)} a day."
    )
    most = max(rows, key=lambda r: r["quote_fills"])
    if most["variant"] != first["variant"]:
        parts.append(
            f"The most quote fills came under `{most['variant']}` "
            f"({fmt(most['quote_fills'], 0)} a day), not under `{first['variant']}`."
        )
    return " ".join(parts)


def strategy_section(t: ReportTables, cfg: dict[str, Any]) -> Section:
    """E2: the four strategies on the baseline fills, with their PnL attribution."""
    st = cfg["strategy"]
    paragraphs = [
        f"`fixed` quotes {float(st['fixed_half_spread_ticks']):g} tick(s) either side of fair "
        f"value whatever the inventory; `skew` adds a linear skew of "
        f"{float(st['skew_ticks_per_order']):g} tick(s) per order held; `glft` is the "
        "Guéant–Lehalle–Fernandez-Tapia closed form with γ back-solved from "
        f"{float(st['glft_skew_ticks_per_order']):g} tick(s) of skew per order held. "
        "`as_rolling` is Avellaneda–Stoikov with a constant horizon chosen so that it quotes "
        "like GLFT at the calibrated sigma; `as_session` takes the time left until 00:00 UTC as "
        "its horizon. Fair value is the "
        f"`{st['fair_value']}`{' plus the OFI signal' if st['use_ofi'] else ''} for all of them. "
        "PnL is split into spread (half-spread earned against the mid at each fill), inventory "
        "(mark-to-market of the position held), fees, hedging and funding; the five add up to "
        "the final equity of every run.",
    ]
    paragraphs += [_strategy_paragraph(t, symbol) for symbol in symbols_in(t.strategies)]
    table = frame_table(
        "E2 strategies (USD per day)",
        t.strategies,
        [
            ("Strategy", text("variant")),
            ("Symbol", text("symbol")),
            ("Mean daily PnL [95% CI]", ci("pnl")),
            ("Spread", num("attr_spread")),
            ("Inventory", num("attr_inventory")),
            ("Fees", num("attr_fees")),
            ("Hedging", num("attr_hedging")),
            ("Funding", num("attr_funding")),
            ("Quote fills", num("quote_fills", 0)),
            ("Clamped to touch", pct("clamp_share")),
            (f"Adverse {PROSE_HORIZON_S} s (bps)", num(f"adverse_bps_{PROSE_HORIZON_S}s")),
            ("Mean |position| (base units)", num("mean_abs_inventory", 4)),
        ],
    )
    return Section("e2", "E2 · Strategy comparison", paragraphs, [table], FIGURES["e2"])


def _strategy_paragraph(t: ReportTables, symbol: str) -> str:
    rows = sorted(rows_of(t.strategies, symbol=symbol), key=lambda r: r["pnl_mean"], reverse=True)
    best, worst = rows[0], rows[-1]
    glft = one_row(t.strategies, symbol=symbol, variant="glft")
    parts = [
        f"`{symbol}`: highest mean daily PnL `{best['variant']}` {usd(best['pnl_mean'])}, lowest "
        f"`{worst['variant']}` {usd(worst['pnl_mean'])}.",
        f"For `glft` the day splits into spread {usd(glft['attr_spread'])}, inventory "
        f"{usd(glft['attr_inventory'])}, fees {usd(glft['attr_fees'])}, hedging "
        f"{usd(glft['attr_hedging'])} and funding {usd(glft['attr_funding'])}.",
    ]
    if 0 < best["quote_fills"] < glft["quote_fills"]:
        parts.append(
            f"`{best['variant']}` ranks first with {fmt(best['quote_fills'], 0)} quote fills a "
            f"day against {fmt(glft['quote_fills'], 0)} for `glft`; per quote fill it made "
            f"{fmt(best['pnl_mean'] / best['quote_fills'], 4)} USD against "
            f"{fmt(glft['pnl_mean'] / glft['quote_fills'], 4)} USD for `glft`."
        )
    idle = [r["variant"] for r in rows if r["quote_fills"] == 0]
    if idle:
        names = ", ".join(f"`{name}`" for name in idle)
        parts.append(f"{names} had no quote fills on any test day.")
    if min(rows, key=lambda r: r["quote_fills"])["variant"] == "as_session":
        parts.append(
            "With the session horizon the A–S spread term γΔσ²τ is wide for most of the day, "
            "so `as_session` quotes far from the touch and trades least."
        )
    clamps = ", ".join(f"`{r['variant']}` {fmt_pct(r['clamp_share'])}" for r in rows)
    parts.append(
        "A quote that would cross the opposite touch is moved back to the touch (post-only "
        f"clamp); share of quote updates clamped: {clamps}."
    )
    return " ".join(parts)


def sensitivity_section(t: ReportTables, cfg: dict[str, Any]) -> Section:
    """E3: GLFT risk aversion, order size and inventory limit, one at a time."""
    grid, st = cfg["experiments"], cfg["strategy"]
    paragraphs = [
        "One GLFT parameter at a time, everything else at the baseline. Risk aversion is set "
        "through the skew it implies per order held, "
        f"{_list(grid['e3_skew_ticks_per_order'])} ticks (baseline "
        f"{float(st['glft_skew_ticks_per_order']):g}), and γ is back-solved from it. Order size "
        f"is the baseline order times {_list(grid['e3_size_multipliers'])}. The inventory limit "
        f"is {_list(grid['e3_max_inventory_orders'])} orders (baseline "
        f"{st['max_inventory_orders']}).",
    ]
    for symbol in symbols_in(t.sensitivity):
        paragraphs.append(_sensitivity_paragraph(t, symbol))
    table = frame_table(
        "E3 sensitivity (USD per day)",
        t.sensitivity,
        [
            ("Parameter", text("parameter")),
            ("Value", num("value", 2)),
            ("Symbol", text("symbol")),
            ("Mean daily PnL [95% CI]", ci("pnl")),
            ("Quote fills", num("quote_fills", 0)),
            ("Mean |position| (base units)", num("mean_abs_inventory", 4)),
            ("Max drawdown", num("max_drawdown_usd")),
            ("γ", num("gamma", 4)),
        ],
    )
    return Section("e3", "E3 · Parameter sensitivity", paragraphs, [table], FIGURES["e3"])


def _list(values: list[float]) -> str:
    return ", ".join(f"{float(value):g}" for value in values)


def _sensitivity_paragraph(t: ReportTables, symbol: str) -> str:
    frame = t.sensitivity.filter(pl.col("symbol") == symbol)
    parts = [f"`{symbol}`:"]
    skew = rows_of(frame, parameter="skew_ticks_per_order")
    if skew:
        best = max(skew, key=lambda r: r["pnl_mean"])
        worst = min(skew, key=lambda r: r["pnl_mean"])
        parts.append(
            f"across skews, mean daily PnL ranged from {usd(worst['pnl_mean'])} at "
            f"{worst['value']:g} ticks to {usd(best['pnl_mean'])} at {best['value']:g}."
        )
    size = rows_of(frame, parameter="size_multiplier")
    if size:
        small, large = size[0], size[-1]
        parts.append(
            f"Order size ×{small['value']:g} to ×{large['value']:g} moved it from "
            f"{usd(small['pnl_mean'])} to {usd(large['pnl_mean'])}. γ went from "
            f"{fmt(small['gamma'], 4)} to {fmt(large['gamma'], 4)}: it is back-solved for the "
            "same skew per order, so a larger order implies a different γ, and this row mixes "
            "a size change with a risk-aversion change."
        )
    limits = rows_of(frame, parameter="max_inventory_orders")
    if limits:
        pnls = [r["pnl_mean"] for r in limits]
        lo, hi = limits[0]["value"], limits[-1]["value"]
        if max(pnls) - min(pnls) < SAME_USD:
            parts.append(
                f"Every inventory limit from {lo:g} to {hi:g} orders gave the same result: even "
                "the smallest limit never bound on these days."
            )
        else:
            parts.append(
                f"Inventory limits from {lo:g} to {hi:g} orders gave mean daily PnL from "
                f"{usd(limits[0]['pnl_mean'])} to {usd(limits[-1]['pnl_mean'])}."
            )
    return " ".join(parts)


def latency_section(t: ReportTables, cfg: dict[str, Any]) -> Section:
    """E4: PnL and adverse selection as order latency grows."""
    engine = cfg["engine"]
    paragraphs = [
        f"Order and cancel latency over {_list(cfg['experiments']['e4_latency_ms'])} ms, with "
        "market-data latency half of the order latency. "
        f"{float(engine['order_latency_ms']):g} ms is the baseline.",
    ]
    for symbol in symbols_in(t.latency):
        rows = rows_of(t.latency, symbol=symbol)
        fast, slow = rows[0], rows[-1]
        adverse = f"adverse_bps_{PROSE_HORIZON_S}s"
        paragraphs.append(
            f"`{symbol}`: mean daily PnL {usd(fast['pnl_mean'])} at {fast['latency_ms']:g} ms "
            f"and {usd(slow['pnl_mean'])} at {slow['latency_ms']:g} ms, "
            f"{gain_or_loss(slow['pnl_mean'] - fast['pnl_mean'])} a day. The mid move "
            f"{PROSE_HORIZON_S} s after a fill went from {fmt(fast[adverse])} to "
            f"{fmt(slow[adverse])} bps and quote fills from {fmt(fast['quote_fills'], 0)} to "
            f"{fmt(slow['quote_fills'], 0)} a day."
        )
        worst = min(rows, key=lambda r: r["pnl_mean"])
        if worst is not slow:
            paragraphs[-1] += (
                f" PnL did not fall steadily with latency: the lowest mean was "
                f"{usd(worst['pnl_mean'])} at {worst['latency_ms']:g} ms."
            )
    table = frame_table(
        "E4 latency (USD per day)",
        t.latency,
        [
            ("Order latency (ms)", num("latency_ms", 0)),
            ("Symbol", text("symbol")),
            ("Mean daily PnL [95% CI]", ci("pnl")),
            ("Quote fills", num("quote_fills", 0)),
            (f"Adverse {PROSE_HORIZON_S} s (bps)", num(f"adverse_bps_{PROSE_HORIZON_S}s")),
            (f"Markout {PROSE_HORIZON_S} s (bps)", num(f"markout_bps_{PROSE_HORIZON_S}s")),
        ],
    )
    return Section("e4", "E4 · Latency", paragraphs, [table], FIGURES["e4"])


def regime_section(t: ReportTables) -> Section:
    """E5: hourly PnL of each strategy in calm and volatile hours."""
    paragraphs = [
        "Each test-day hour is labelled `high` when the sum of its squared one-second mid "
        "changes is above its symbol's median over all test hours, otherwise `low`. The label "
        "uses the hour's own data, so this describes where PnL came from; it is not a regime "
        "signal a strategy could have traded on. The strategies are the E2 runs.",
    ]
    for symbol in symbols_in(t.regimes):
        paragraphs.append(_regime_paragraph(t, symbol))
    table = frame_table(
        "E5 volatility regimes (USD per hour)",
        t.regimes,
        [
            ("Strategy", text("variant")),
            ("Symbol", text("symbol")),
            ("Regime", text("regime")),
            ("Days", num("n_days", 0)),
            ("Hours", num("n_hours", 0)),
            ("Mean hourly PnL [95% CI]", ci("pnl_per_hour")),
        ],
    )
    return Section("e5", "E5 · Volatility regimes", paragraphs, [table], FIGURES["e5"])


def _regime_paragraph(t: ReportTables, symbol: str) -> str:
    frame = t.regimes.filter(pl.col("symbol") == symbol)
    variants = frame["variant"].unique(maintain_order=True).to_list()
    changes, pieces = {}, []
    for variant in variants:
        low = one_row(frame, variant=variant, regime="low")["pnl_per_hour_mean"]
        high = one_row(frame, variant=variant, regime="high")["pnl_per_hour_mean"]
        changes[variant] = high - low
        pieces.append(f"`{variant}` {usd(low)} in low and {usd(high)} in high hours")
    hours = one_row(frame, variant=variants[0], regime="high")["n_hours"]
    parts = [f"`{symbol}` ({hours} high-volatility hours), mean PnL per hour: {'; '.join(pieces)}."]
    worst = min(changes, key=changes.__getitem__)
    if changes[worst] < -SAME_USD:
        parts.append(
            f"The largest fall from calm to volatile hours was `{worst}`, "
            f"{usd(changes[worst])} an hour."
        )
    return " ".join(parts)


def event_section(t: ReportTables, cfg: dict[str, Any]) -> Section:
    """E6: the most violent window replayed with every risk control off and then on."""
    rc, grid, st = cfg["risk"], cfg["experiments"], cfg["strategy"]
    paragraphs = [
        "Each symbol's most violent hour over all test days (largest sum of squared one-second "
        f"mid changes) is replayed with {grid['e6_padding_hours']} hour(s) either side, once "
        "with every risk control off and once with all of them on. Kill switches: position "
        f"beyond {rc['kill_inventory_orders']} orders, drawdown, and no book update for "
        f"{rc['stale_after_s']} s. Circuit breakers, each pausing quotes for "
        f"{rc['cooldown_s']} s: {rc['vol_short_s']} s volatility above {float(rc['vol_ratio']):g}"
        f" times the {rc['vol_long_s']} s volatility, spread above "
        f"{float(rc['max_spread_bps']):g} bps, |OFI| at least "
        f"{float(rc['toxicity_threshold']):g}, and a gap of more than {float(rc['max_gap_s']):g} "
        f"s in market data. Size ramp: {_list(rc['ramp_levels'])} times the order size, one "
        f"step up per {float(rc['ramp_step_hours']):g} hours without a breaker trip. A kill "
        "stops quoting for the rest of the run"
        f"{' and flattens the position with a taker order' if rc['flatten_on_kill'] else ''}.",
        "The drawdown kill fires when equity falls more than `max_drawdown_bps` "
        f"({float(rc['max_drawdown_bps']):g} bps) of the largest allowed position's notional "
        f"below its peak: {float(rc['max_drawdown_bps']):g} / 10,000 × "
        f"{st['max_inventory_orders']} orders × order size × the first mid of the run. "
        f"{_limits(t)}. The {float(rc['max_drawdown_bps']):g} bps is a round number chosen for "
        "this project, not calibrated, and the risk-on result depends on it directly.",
    ]
    paragraphs += [_event_paragraph(t, symbol) for symbol in symbols_in(t.events)]
    paragraphs.append(_kill_inventory_paragraph(t, cfg))
    windows = frame_table(
        "E6 windows",
        t.windows.with_columns(
            pl.col("window_start").map_elements(utc_time, return_dtype=pl.String),
            pl.col("window_end").map_elements(utc_time, return_dtype=pl.String),
            pl.col("violent_hour_start").map_elements(utc_time, return_dtype=pl.String),
        ),
        [
            ("Symbol", text("symbol")),
            ("Test day", text("test_date")),
            ("Window start (UTC)", text("window_start")),
            ("Window end (UTC)", text("window_end")),
            ("Most violent hour (UTC)", text("violent_hour_start")),
            ("Realised variance (USD²)", num("realised_var")),
        ],
    )
    events = frame_table(
        "E6 risk controls off and on (USD over the window)",
        t.events,
        [
            ("Symbol", text("symbol")),
            ("Controls", text("variant")),
            ("PnL", num("pnl")),
            ("Max drawdown", num("max_drawdown_usd")),
            ("Quote fills", num("quote_fills", 0)),
            ("Mean |position| (base units)", num("mean_abs_inventory", 4)),
            ("Risk events", num("risk_events", 0)),
            ("Kill", text("kill_kind")),
            ("Drawdown limit", num("drawdown_limit_usd")),
            ("Events by kind", text("events_by_kind")),
        ],
    )
    return Section("e6", "E6 · Event study", paragraphs, [windows, events], FIGURES["e6"])


def _limits(t: ReportTables) -> str:
    on = t.events.filter(pl.col("variant") == "risk_on")
    limits = [
        f"{usd(r['drawdown_limit_usd'])} for `{r['symbol']}`" for r in on.iter_rows(named=True)
    ]
    return "That gave " + " and ".join(limits)


def _event_paragraph(t: ReportTables, symbol: str) -> str:
    off = one_row(t.events, symbol=symbol, variant="risk_off")
    on = one_row(t.events, symbol=symbol, variant="risk_on")
    window = one_row(t.windows, symbol=symbol)
    parts = [
        f"`{symbol}` on {window['test_date']}: PnL {usd(off['pnl'])} with controls off (max "
        f"drawdown {usd(off['max_drawdown_usd'])}) and {usd(on['pnl'])} with controls on (max "
        f"drawdown {usd(on['max_drawdown_usd'])}), {gain_or_loss(on['pnl'] - off['pnl'])} for "
        "the controls.",
    ]
    replayed = t.event_series[(symbol, "risk_on")]["timeseries"]["ts"]
    start, end = int(replayed.min()), int(replayed.max())
    parts.append(f"Market data in the window ran from {utc_time(start)} to {utc_time(end)} UTC.")
    kill = _first_kill(t, symbol)
    if kill is None:
        parts.append("No kill switch fired.")
    else:
        minutes = (kill["ts"] - start) / NS_PER_MINUTE
        parts.append(
            f"`{kill['kind']}` fired {fmt(minutes, 0)} minutes after the first replayed update "
            f"({kill['detail']})."
        )
    parts.append(f"Risk events with controls on: {on['events_by_kind']}.")
    return " ".join(parts)


def _first_kill(t: ReportTables, symbol: str) -> Row | None:
    events = t.event_series[(symbol, "risk_on")]["risk_events"]
    kills = events.filter(pl.col("kind").str.starts_with("kill")).sort("ts")
    return None if kills.is_empty() else kills.row(0, named=True)


def _kill_inventory_paragraph(t: ReportTables, cfg: dict[str, Any]) -> str:
    fired = sum(
        series["risk_events"].filter(pl.col("kind") == "kill_inventory").height
        for (_, variant), series in t.event_series.items()
        if variant == "risk_on"
    )
    return (
        f"`kill_inventory` is set at {cfg['risk']['kill_inventory_orders']} orders, above the "
        f"inventory limit of {cfg['strategy']['max_inventory_orders']} orders, and that limit "
        "already stops quoting any side that would take the position past it. The switch is "
        f"therefore a backstop against a fault in the limit; it fired {fired} time(s) here."
    )


def fee_section(t: ReportTables, cfg: dict[str, Any]) -> Section:
    """E7: PnL across maker fee tiers, the break-even fee and the rerun check of the formula."""
    fees = cfg["fees"]
    reruns = _list(cfg["experiments"]["e7_rerun_maker_bps"])
    paragraphs = [
        "PnL at maker fee m is computed from each baseline run as PnL(m) = PnL(base) + "
        "(base − m) / 10,000 × maker volume. It is exact when the fills do not depend on the "
        "fee, which holds here: no strategy reads the fee and the baseline has no risk layer. "
        f"Real reruns at {reruns} bps test that claim. The baseline fees, maker "
        f"{float(fees['maker_bps']):g} bps and taker {float(fees['taker_bps']):g} bps, are "
        "config values that were not checked against Binance's current fee schedule.",
    ]
    for symbol in symbols_in(t.break_even):
        paragraphs.append(_fee_paragraph(t, symbol))
    paragraphs.append(_rerun_paragraph(t))
    tiers = frame_table(
        "E7 fee tiers (USD per day)",
        t.fee_tiers,
        [
            ("Symbol", text("symbol")),
            ("Maker fee (bps)", num("maker_bps")),
            ("Days", num("n_days", 0)),
            ("Mean daily PnL [95% CI]", ci("pnl")),
        ],
    )
    break_even = frame_table(
        "E7 break-even maker fee over all test days",
        t.break_even,
        [
            ("Symbol", text("symbol")),
            ("Total PnL", num("total_pnl")),
            ("Total maker volume", num("total_maker_volume_usd", 0)),
            ("Break-even maker fee (bps)", num("break_even_maker_bps", 3)),
        ],
    )
    check = frame_table(
        "E7 reruns against the formula (USD per day)",
        t.fee_reruns,
        [
            ("Symbol", text("symbol")),
            ("Test day", text("test_date")),
            ("Maker fee (bps)", num("maker_bps")),
            ("Rerun PnL", num("rerun_pnl")),
            ("Formula PnL", num("predicted_pnl")),
            ("|Difference|", lambda row: f"{row['abs_difference']:.1e}"),
            ("Same fills", text("same_fills")),
        ],
    )
    return Section("e7", "E7 · Fee tiers", paragraphs, [tiers, break_even, check], FIGURES["e7"])


def _fee_paragraph(t: ReportTables, symbol: str) -> str:
    row = one_row(t.break_even, symbol=symbol)
    tiers = rows_of(t.fee_tiers, symbol=symbol)
    lowest = min(tiers, key=lambda r: r["maker_bps"])
    be = row["break_even_maker_bps"]
    rebate = f", a rebate of {fmt(-be, 3)} bps" if be < 0 else ""
    return (
        f"`{symbol}`: over {lowest['n_days']} test days the baseline would break even at a maker "
        f"fee of {fmt(be, 3)} bps{rebate}. At {lowest['maker_bps']:g} bps, the lowest fee in the "
        f"grid, its mean daily PnL would be {usd(lowest['pnl_mean'])}."
    )


def _rerun_paragraph(t: ReportTables) -> str:
    check = t.fee_reruns
    same = int(check["same_fills"].sum())
    return (
        f"{check.height} rerun(s): the largest absolute difference between a rerun's PnL and "
        f"the formula was {float(check['abs_difference'].max()):.1e} USD, and {same} of "
        f"{check.height} had exactly the baseline's number of quote fills."
    )


def markout_section(t: ReportTables) -> Section:
    """Markouts of the baseline's quote fills, by symbol, side and horizon."""
    paragraphs = [
        "For each fill of the baseline, the markout is side × (mid after h seconds − fill "
        "price) / fill price, in bps: positive means the fill was worth more than its price "
        "against the later mid, and it includes the half-spread captured. The mid move is "
        "side × (mid after h − mid at fill) / fill price: it removes the half-spread and is "
        "the adverse-selection measure, negative when the mid moved against the fill. "
        f"Intervals in the figure are ±{NORMAL_95} standard errors across fills, which "
        "ignores the correlation between nearby fills and is therefore too narrow.",
    ]
    for symbol in symbols_in(t.markouts):
        pieces = []
        for side, name in SIDE_NAMES.items():
            rows = rows_of(t.markouts, symbol=symbol, side=side, horizon_s=PROSE_HORIZON_S)
            if rows:
                r = rows[0]
                pieces.append(
                    f"{name} fills markout {fmt(r['avg_markout_bps'])} bps and mid move "
                    f"{fmt(r['avg_adverse_bps'])} bps ({fmt(r['n_fills'], 0)} fills)"
                )
        if pieces:
            paragraphs.append(f"`{symbol}` at {PROSE_HORIZON_S} s: {'; '.join(pieces)}.")
    table = frame_table(
        "Markouts of baseline quote fills, all test days",
        t.markouts.with_columns(
            pl.col("side").replace_strict(SIDE_NAMES, return_dtype=pl.String).alias("side_name")
        ),
        [
            ("Symbol", text("symbol")),
            ("Side", text("side_name")),
            ("Horizon (s)", num("horizon_s", 0)),
            ("Fills", num("n_fills", 0)),
            ("Markout (bps)", num("avg_markout_bps", 3)),
            ("s.e. (bps)", num("se_markout_bps", 3)),
            ("Mid move (bps)", num("avg_adverse_bps", 3)),
            ("Total markout (USD)", num("total_markout_usd")),
        ],
    )
    return Section("markouts", "Markouts", paragraphs, [table], FIGURES["markouts"])


def run_cost_section(t: ReportTables) -> Section:
    """How long the backtests took and how much memory a worker needed."""
    paragraphs = []
    for row in t.run_cost.iter_rows(named=True):
        paragraphs.append(
            f"`{row['symbol']}`: {row['runs']} distinct runs replaying "
            f"{fmt(row['mean_events_per_run'], 0)} market events each on average, at "
            f"{fmt(row['events_per_s'], 0)} events per wall-clock second of simulation; the "
            f"largest worker peak resident memory was {fmt(row['max_worker_peak_rss_mb'], 0)} MB."
        )
    paragraphs.append(
        "Times are wall-clock on the machine that produced this report and vary with the "
        "machine and its load; they are here to size a rerun, not as a benchmark."
    )
    table = frame_table(
        "Run cost",
        t.run_cost,
        [
            ("Symbol", text("symbol")),
            ("Runs", num("runs", 0)),
            ("Mean events per run", num("mean_events_per_run", 0)),
            ("Mean simulation time (s)", num("mean_sim_wall_s", 1)),
            ("Events per second", num("events_per_s", 0)),
            ("Total job time (s)", num("total_job_wall_s", 0)),
            ("Max worker peak RSS (MB)", num("max_worker_peak_rss_mb", 0)),
        ],
    )
    return Section("cost", "Run cost", paragraphs, [table])


def all_sections(t: ReportTables, cfg: dict[str, Any]) -> list[Section]:
    """The report's sections in reading order."""
    return [
        calibration_section(t, cfg),
        realism_section(t, cfg),
        strategy_section(t, cfg),
        sensitivity_section(t, cfg),
        latency_section(t, cfg),
        regime_section(t),
        event_section(t, cfg),
        fee_section(t, cfg),
        markout_section(t),
        run_cost_section(t),
    ]
