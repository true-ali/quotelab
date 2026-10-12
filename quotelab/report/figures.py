"""Figures for the experiment tables in ``quotelab.report.tables``.

Each public function draws one PNG from the frames that the matching table function returns. The
figures use the object-oriented matplotlib API only, so no global pyplot state is involved. Callers
draw and save inside ``styled()``, which applies the light theme and the font sizes of ``STYLE``.
"""

from contextlib import AbstractContextManager
from pathlib import Path

import matplotlib
import numpy as np
import polars as pl
from matplotlib.artist import Artist
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.ticker import NullLocator, StrMethodFormatter

DPI = 200
PANEL_WIDTH = 5.0
PANEL_HEIGHT = 4.0
MAX_LEGEND_COLUMNS = 5
MARKOUT_LEGEND_COLUMNS = 2
CALIBRATION_LEGEND_COLUMNS = 3
STRATEGY_LEGEND_COLUMNS = 3
TITLE_X = 0.01

BLUE = "#0072B2"
ORANGE = "#E69F00"
GREEN = "#009E73"
VERMILION = "#D55E00"
PURPLE = "#CC79A7"
SKY = "#56B4E9"
YELLOW = "#F0E442"
BLACK = "#000000"
PALETTE = (BLUE, ORANGE, GREEN, VERMILION, PURPLE, SKY, YELLOW, BLACK)
EVENT_COLORS = (VERMILION, GREEN, PURPLE, BLACK, SKY)
BACKGROUND = "#ffffff"
TEXT = "#222222"
AXIS_GREY = "#555555"
GRID = "#e5e5e5"
ZERO_LINE = "#888888"
ERROR_COLOR = "#333333"
ERROR_WIDTH = 1.2
SHADE = "#d9d9d9"
RISK_OFF_COLOR = ORANGE
RISK_ON_COLOR = BLUE

STYLE = {
    "font.size": 12,
    "axes.titlesize": 13,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "axes.labelsize": 12,
    "xtick.labelsize": 11,
    "ytick.labelsize": 11,
    "legend.fontsize": 11,
    "legend.frameon": False,
    "figure.titlesize": 15,
    "figure.titleweight": "bold",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.edgecolor": AXIS_GREY,
    "axes.labelcolor": TEXT,
    "xtick.color": AXIS_GREY,
    "ytick.color": AXIS_GREY,
    "text.color": TEXT,
    "axes.grid": True,
    "axes.grid.axis": "y",
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "axes.axisbelow": True,
    "figure.facecolor": BACKGROUND,
    "axes.facecolor": BACKGROUND,
    "savefig.facecolor": BACKGROUND,
    "errorbar.capsize": 4,
    "lines.linewidth": 2.0,
}

CI_LABEL = "95% block-bootstrap CI over days"
MEAN_CI_LABEL = f"mean, {CI_LABEL}"
FIT_LABEL = "fitted a*exp(-k*distance)"
HOLLOW_LABEL = "hollow: too few hits, not fitted"
PNL_LABEL = "mean daily PnL (USD)"
HOURLY_PNL_LABEL = "mean hourly PnL (USD)"
LATENCY_LABEL = "order latency (ms), data latency is half"
ADVERSE_LABEL = "adverse move at 5 s (bps)"
FEE_LABEL = "maker fee (bps, negative = rebate)"
VIOLENT_LABEL = "most violent hour"

LINE_WIDTH = 1.8
MARKER_SIZE = 5
TOTAL_MARKER_SIZE = 7
ZERO_WIDTH = 0.8
BAR_WIDTH = 0.38
HALF_BAR = BAR_WIDTH / 2
STACK_WIDTH = 0.6
BEFORE_FEES_ALPHA = 0.45
BAND_ALPHA = 0.2
SCATTER_SIZE = 12
CURVE_WIDTH = 1.0
CURVE_POINTS = 100
RERUN_SIZE = 120
RERUN_WIDTH = 1.6
SHADE_ALPHA = 0.7
EVENT_WIDTH = 1.0
EVENT_ALPHA = 0.85
DASHED = "--"
SE_Z = 1.96
LOG_BASE = 2
NS_PER_HOUR = 3_600_000_000_000
BUY_SIDE = 1
SELL_SIDE = -1
SIDE_LABELS = {BUY_SIDE: "buy fills", SELL_SIDE: "sell fills"}
SIDE_COLORS = {BUY_SIDE: BLUE, SELL_SIDE: VERMILION}
SENSITIVITY_PARAMETERS = ("skew_ticks_per_order", "size_multiplier", "max_inventory_orders")
LOG_PARAMETERS = ("skew_ticks_per_order", "size_multiplier")
STRATEGY_COMPONENTS = (
    ("attr_spread", "spread", BLUE),
    ("attr_inventory", "inventory", ORANGE),
    ("attr_fees", "fees", GREEN),
    ("attr_hedging", "hedging", VERMILION),
    ("attr_funding", "funding", PURPLE),
)
REGIMES = (("low", -HALF_BAR, SKY), ("high", HALF_BAR, VERMILION))


def styled() -> AbstractContextManager:
    """The light theme and font sizes of ``STYLE``, for every figure drawn and saved inside its
    ``with`` block. Matplotlib reads rcParams when it creates artists and when it saves, so both
    the drawing and the saving must happen inside the block.
    """
    return matplotlib.rc_context(STYLE)


def _legend(ax: Axes) -> None:
    """A legend on one axes, only when it has a labelled artist. Used where panels differ."""
    if ax.get_legend_handles_labels()[0]:
        ax.legend()


def _shared_legend(fig: Figure, axes: np.ndarray, columns: int = MAX_LEGEND_COLUMNS) -> None:
    """One legend under the whole figure, gathering the labelled artists of every panel. Panels
    that show the same kinds of series repeat their labels, so each label is kept once, from the
    first panel that has it.
    """
    entries: dict[str, Artist] = {}
    for ax in axes.flat:
        handles, labels = ax.get_legend_handles_labels()
        for handle, label in zip(handles, labels, strict=True):
            entries.setdefault(label, handle)
    if entries:
        fig.legend(
            list(entries.values()),
            list(entries),
            loc="outside lower center",
            ncols=min(len(entries), columns),
            frameon=False,
        )


def _figure_title(fig: Figure, text: str) -> None:
    """A short figure title, left-aligned at the figure's left edge so it reads first."""
    fig.suptitle(text, x=TITLE_X, ha="left")


def _usd_axis(ax: Axes) -> None:
    """Thousands separators on a y axis in USD, so 12000 reads as 12,000."""
    ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,g}"))


def _figure(columns: int) -> tuple[Figure, np.ndarray]:
    """A row of panels read as ``axes[0][i]``. The theme comes from ``styled()``."""
    fig = Figure(figsize=(PANEL_WIDTH * columns, PANEL_HEIGHT), layout="constrained")
    return fig, fig.subplots(1, columns, squeeze=False)


def _symbol_figure(symbols: list[str]) -> tuple[Figure, np.ndarray]:
    """A figure with one panel per symbol, each titled with its symbol."""
    fig, axes = _figure(len(symbols))
    for i, symbol in enumerate(symbols):
        axes[0][i].set_title(symbol)
    return fig, axes


def _save(fig: Figure, path: Path) -> Path:
    """Write the figure as a PNG, creating the parent directory first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI)
    return path


def _symbols(frame: pl.DataFrame) -> list[str]:
    """Symbols in the order they first appear in the frame."""
    return frame["symbol"].unique(maintain_order=True).to_list()


def _for_symbol(frame: pl.DataFrame, symbol: str) -> pl.DataFrame:
    """The rows of one symbol, keeping their order."""
    return frame.filter(pl.col("symbol") == symbol)


def _array(frame: pl.DataFrame, column: str) -> np.ndarray:
    """A column as float64, with nulls as NaN."""
    return frame[column].cast(pl.Float64).to_numpy()


def _color(index: int) -> str:
    """The palette colour of the ``index``-th series, cycling past the last colour."""
    return PALETTE[index % len(PALETTE)]


def _yerr(mean: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """Distances from the mean down to ``lo`` and up to ``hi`` as a 2 x n array of non-negative
    values. A NaN bound (one day, so no interval) gives an error of 0.
    """
    below = np.nan_to_num(np.abs(mean - lo), nan=0.0)
    above = np.nan_to_num(np.abs(hi - mean), nan=0.0)
    return np.vstack([below, above])


def _ci_band(
    ax: Axes,
    x: np.ndarray,
    mean: np.ndarray,
    lo: np.ndarray,
    hi: np.ndarray,
    color: str,
    label: str,
) -> None:
    """Translucent CI band with its legend label. A NaN bound is replaced by the mean, so the band
    collapses there.
    """
    band_lo = np.where(np.isnan(lo), mean, lo)
    band_hi = np.where(np.isnan(hi), mean, hi)
    ax.fill_between(x, band_lo, band_hi, color=color, alpha=BAND_ALPHA, linewidth=0, label=label)


def _line(ax: Axes, x: np.ndarray, y: np.ndarray, color: str, label: str) -> None:
    """A line with round markers, the common style for the per-symbol series."""
    ax.plot(
        x, y, marker="o", color=color, linewidth=LINE_WIDTH, markersize=MARKER_SIZE, label=label
    )


def _numeric_ticks(ax: Axes, values: np.ndarray) -> None:
    """Ticks at exactly the distinct parameter values, labelled with those values."""
    positions = np.unique(values.astype(np.float64))
    ax.set_xticks(positions, labels=[f"{position:g}" for position in positions])
    ax.xaxis.set_minor_locator(NullLocator())


def _category_ticks(ax: Axes, names: list[str]) -> None:
    """One tick per variant with horizontal labels. Underscores become line breaks, so a long name
    such as ``queue_latency_stress`` stacks onto several lines instead of running into its
    neighbours.
    """
    ax.set_xticks(np.arange(len(names)), labels=[name.replace("_", "\n") for name in names])


def _by_variant(rows: pl.DataFrame, variants: list[str], regime: str, column: str) -> np.ndarray:
    """The values of one regime, ordered like ``variants``; a variant without a row gives NaN."""
    picked = rows.filter(pl.col("regime") == regime)
    values = dict(zip(picked["variant"].to_list(), _array(picked, column), strict=True))
    return np.array([values.get(variant, np.nan) for variant in variants])


def _stack_components(ax: Axes, rows: pl.DataFrame, x: np.ndarray) -> None:
    """Stack the attribution components of each variant: positive parts above zero, negative
    parts below zero, each component in its own colour.
    """
    positive_bottom = np.zeros(len(x))
    negative_bottom = np.zeros(len(x))
    for column, label, color in STRATEGY_COMPONENTS:
        values = np.nan_to_num(_array(rows, column), nan=0.0)
        positive = np.maximum(values, 0.0)
        negative = np.minimum(values, 0.0)
        ax.bar(x, positive, STACK_WIDTH, bottom=positive_bottom, color=color, label=label)
        ax.bar(x, negative, STACK_WIDTH, bottom=negative_bottom, color=color)
        positive_bottom += positive
        negative_bottom += negative


def _hours_since(times: np.ndarray | int, start: int) -> np.ndarray:
    """Nanosecond timestamps as hours after ``start``."""
    return (np.asarray(times) - start) / NS_PER_HOUR


def _event_colors(frames: list[pl.DataFrame]) -> dict[str, str]:
    """One colour per risk event kind across all the panels, so a kind has the same colour in
    every panel and the shared legend matches each of them.
    """
    kinds = dict.fromkeys(kind for frame in frames for kind in frame["kind"].to_list())
    return {kind: EVENT_COLORS[i % len(EVENT_COLORS)] for i, kind in enumerate(kinds)}


def _draw_risk_events(ax: Axes, events: pl.DataFrame, start: int, colors: dict[str, str]) -> None:
    """A dashed vertical line at each risk event, coloured by kind. Each kind gets one legend
    entry, so a long run with hundreds of events keeps a short legend.
    """
    labelled: set[str] = set()
    hours = _hours_since(events["ts"].to_numpy(), start)
    for hour, kind in zip(hours, events["kind"].to_list(), strict=True):
        ax.axvline(
            hour,
            color=colors[kind],
            linestyle=DASHED,
            linewidth=EVENT_WIDTH,
            alpha=EVENT_ALPHA,
            label=None if kind in labelled else kind,
        )
        labelled.add(kind)


def e1_realism_ladder(table: pl.DataFrame, path: Path) -> Path:
    """Mean daily PnL of each realism rung with its CI, beside the same PnL before maker fees.
    The gap between each pair is the maker-fee cost that rung adds.
    """
    symbols = _symbols(table)
    fig, axes = _symbol_figure(symbols)
    for i, symbol in enumerate(symbols):
        ax = axes[0][i]
        rows = _for_symbol(table, symbol)
        variants = rows["variant"].to_list()
        x = np.arange(len(variants))
        mean = _array(rows, "pnl_mean")
        yerr = _yerr(mean, _array(rows, "pnl_lo"), _array(rows, "pnl_hi"))
        ax.bar(
            x - HALF_BAR,
            mean,
            BAR_WIDTH,
            yerr=yerr,
            error_kw={"ecolor": ERROR_COLOR, "elinewidth": ERROR_WIDTH},
            color=BLUE,
            label=f"PnL: {MEAN_CI_LABEL}",
        )
        ax.bar(
            x + HALF_BAR,
            _array(rows, "pnl_before_fees"),
            BAR_WIDTH,
            color=BLUE,
            alpha=BEFORE_FEES_ALPHA,
            label="PnL before maker fees",
        )
        ax.axhline(0, color=ZERO_LINE, linewidth=ZERO_WIDTH)
        _category_ticks(ax, variants)
        ax.set_ylabel(PNL_LABEL)
        _usd_axis(ax)
    _figure_title(fig, "E1 realism ladder: mean daily PnL by fill model (USD)")
    _shared_legend(fig, axes)
    return _save(fig, path)


def e2_strategies(table: pl.DataFrame, path: Path) -> Path:
    """Each strategy's mean daily PnL split into five attribution components, stacked above and
    below zero, with the total PnL as a black diamond and its CI. The stack shows which
    component drives or drags each strategy.
    """
    symbols = _symbols(table)
    fig, axes = _symbol_figure(symbols)
    for i, symbol in enumerate(symbols):
        ax = axes[0][i]
        rows = _for_symbol(table, symbol)
        variants = rows["variant"].to_list()
        x = np.arange(len(variants))
        _stack_components(ax, rows, x)
        mean = _array(rows, "pnl_mean")
        yerr = _yerr(mean, _array(rows, "pnl_lo"), _array(rows, "pnl_hi"))
        ax.errorbar(
            x,
            mean,
            yerr=yerr,
            fmt="D",
            color=BLACK,
            ecolor=ERROR_COLOR,
            elinewidth=ERROR_WIDTH,
            markersize=TOTAL_MARKER_SIZE,
            label=f"total PnL: {MEAN_CI_LABEL}",
        )
        ax.axhline(0, color=ZERO_LINE, linewidth=ZERO_WIDTH)
        _category_ticks(ax, variants)
        ax.set_ylabel(PNL_LABEL)
        _usd_axis(ax)
    _figure_title(fig, "E2 strategies: mean daily PnL by component (USD)")
    _shared_legend(fig, axes, STRATEGY_LEGEND_COLUMNS)
    return _save(fig, path)


def e3_sensitivity(table: pl.DataFrame, path: Path) -> Path:
    """Mean daily PnL against each swept setting: one panel per parameter, one line per symbol,
    CI as a band. Skew and size are multiplicative, so they use a log base-2 axis.
    """
    present = table["parameter"].unique().to_list()
    parameters = [name for name in SENSITIVITY_PARAMETERS if name in present]
    fig, axes = _figure(len(parameters))
    for i, parameter in enumerate(parameters):
        ax = axes[0][i]
        rows = table.filter(pl.col("parameter") == parameter)
        if parameter in LOG_PARAMETERS:
            ax.set_xscale("log", base=LOG_BASE)
        for j, symbol in enumerate(_symbols(rows)):
            sub = _for_symbol(rows, symbol).sort("value")
            x = _array(sub, "value")
            mean = _array(sub, "pnl_mean")
            _ci_band(
                ax, x, mean, _array(sub, "pnl_lo"), _array(sub, "pnl_hi"), _color(j), MEAN_CI_LABEL
            )
            _line(ax, x, mean, _color(j), symbol)
        _numeric_ticks(ax, _array(rows, "value"))
        ax.set_xlabel(parameter)
        ax.set_ylabel(PNL_LABEL)
        _usd_axis(ax)
    _figure_title(fig, "E3 sensitivity: mean daily PnL by setting (USD)")
    _shared_legend(fig, axes)
    return _save(fig, path)


def e4_latency(table: pl.DataFrame, path: Path) -> Path:
    """Mean daily PnL (with its CI band) and the adverse move at 5 s, both against order latency.
    Latency is on a log axis, since the sweep spans two orders of magnitude.
    """
    fig, axes = _figure(2)
    pnl_ax = axes[0][0]
    adverse_ax = axes[0][1]
    pnl_ax.set_title("PnL")
    adverse_ax.set_title("adverse move")
    for ax in (pnl_ax, adverse_ax):
        ax.set_xscale("log")
        _numeric_ticks(ax, _array(table, "latency_ms"))
        ax.set_xlabel(LATENCY_LABEL)
    for j, symbol in enumerate(_symbols(table)):
        sub = _for_symbol(table, symbol).sort("latency_ms")
        x = _array(sub, "latency_ms")
        mean = _array(sub, "pnl_mean")
        _ci_band(
            pnl_ax,
            x,
            mean,
            _array(sub, "pnl_lo"),
            _array(sub, "pnl_hi"),
            _color(j),
            MEAN_CI_LABEL,
        )
        _line(pnl_ax, x, mean, _color(j), symbol)
        _line(adverse_ax, x, _array(sub, "adverse_bps_5s"), _color(j), symbol)
    pnl_ax.set_ylabel(PNL_LABEL)
    _usd_axis(pnl_ax)
    adverse_ax.set_ylabel(ADVERSE_LABEL)
    _figure_title(fig, "E4 latency: PnL and adverse move against order latency")
    _shared_legend(fig, axes)
    return _save(fig, path)


def e5_regimes(table: pl.DataFrame, path: Path) -> Path:
    """Mean hourly PnL of each strategy in low- and high-volatility hours, as grouped bars with
    CI. Comparing the pair per strategy shows which strategies lose money when volatility rises.
    """
    symbols = _symbols(table)
    fig, axes = _symbol_figure(symbols)
    for i, symbol in enumerate(symbols):
        ax = axes[0][i]
        rows = _for_symbol(table, symbol)
        variants = rows["variant"].unique(maintain_order=True).to_list()
        x = np.arange(len(variants))
        for regime, offset, color in REGIMES:
            mean = _by_variant(rows, variants, regime, "pnl_per_hour_mean")
            yerr = _yerr(
                mean,
                _by_variant(rows, variants, regime, "pnl_per_hour_lo"),
                _by_variant(rows, variants, regime, "pnl_per_hour_hi"),
            )
            ax.bar(
                x + offset,
                mean,
                BAR_WIDTH,
                yerr=yerr,
                error_kw={"ecolor": ERROR_COLOR, "elinewidth": ERROR_WIDTH},
                color=color,
                label=f"{regime} volatility: {MEAN_CI_LABEL}",
            )
        ax.axhline(0, color=ZERO_LINE, linewidth=ZERO_WIDTH)
        _category_ticks(ax, variants)
        ax.set_ylabel(HOURLY_PNL_LABEL)
        _usd_axis(ax)
    _figure_title(fig, "E5 volatility regimes: mean hourly PnL (USD)")
    _shared_legend(fig, axes)
    return _save(fig, path)


def e6_event_study(
    series: dict[tuple[str, str], dict[str, pl.DataFrame]], windows: pl.DataFrame, path: Path
) -> Path:
    """Equity of the risk-controls-off and risk-controls-on runs through the replayed window, with
    the most violent hour shaded and each risk-on risk event marked. It shows whether the risk
    layer cut losses during the stress hour and which controls fired when.
    """
    symbols = _symbols(windows)
    risk_events = {symbol: series[(symbol, "risk_on")]["risk_events"] for symbol in symbols}
    colors = _event_colors(list(risk_events.values()))
    fig, axes = _symbol_figure(symbols)
    for i, symbol in enumerate(symbols):
        ax = axes[0][i]
        window = _for_symbol(windows, symbol).row(0, named=True)
        start = window["window_start"]
        for variant, color in (("risk_off", RISK_OFF_COLOR), ("risk_on", RISK_ON_COLOR)):
            timeseries = series[(symbol, variant)]["timeseries"].sort("ts")
            ax.plot(
                _hours_since(timeseries["ts"].to_numpy(), start),
                _array(timeseries, "equity"),
                color=color,
                linewidth=LINE_WIDTH,
                label=variant,
            )
        violent_start = window["violent_hour_start"]
        ax.axvspan(
            _hours_since(violent_start, start),
            _hours_since(violent_start + NS_PER_HOUR, start),
            color=SHADE,
            alpha=SHADE_ALPHA,
            label=VIOLENT_LABEL,
        )
        _draw_risk_events(ax, risk_events[symbol], start, colors)
        ax.set_xlabel("hours since window start")
        ax.set_ylabel("equity (USD)")
        _usd_axis(ax)
    _figure_title(fig, "E6 event study: equity with risk controls off and on (USD)")
    _shared_legend(fig, axes)
    return _save(fig, path)


def e7_fee_tiers(fee_table: pl.DataFrame, rerun_check: pl.DataFrame, path: Path) -> Path:
    """Mean daily PnL at each maker fee in the grid, from the baseline volume formula, with CI, and
    the real reruns at those fees as hollow circles. Agreement between the line and the circles
    checks that the formula is exact. The fee axis is inverted so rebates sit on the right.
    """
    symbols = _symbols(fee_table)
    fig, axes = _symbol_figure(symbols)
    for i, symbol in enumerate(symbols):
        ax = axes[0][i]
        formula = _for_symbol(fee_table, symbol).sort("maker_bps")
        x = _array(formula, "maker_bps")
        mean = _array(formula, "pnl_mean")
        _ci_band(
            ax,
            x,
            mean,
            _array(formula, "pnl_lo"),
            _array(formula, "pnl_hi"),
            BLUE,
            MEAN_CI_LABEL,
        )
        _line(ax, x, mean, BLUE, "formula from baseline volume")
        reruns = (
            _for_symbol(rerun_check, symbol)
            .group_by("maker_bps")
            .agg(pl.col("rerun_pnl").mean())
            .sort("maker_bps")
        )
        ax.scatter(
            _array(reruns, "maker_bps"),
            _array(reruns, "rerun_pnl"),
            s=RERUN_SIZE,
            facecolors="none",
            edgecolors=VERMILION,
            linewidths=RERUN_WIDTH,
            label="real rerun",
        )
        ax.axhline(0, color=ZERO_LINE, linewidth=ZERO_WIDTH)
        ax.invert_xaxis()
        _numeric_ticks(ax, x)
        ax.set_xlabel(FEE_LABEL)
        ax.set_ylabel(PNL_LABEL)
        _usd_axis(ax)
    _figure_title(fig, "E7 maker fee tiers: mean daily PnL by maker fee (USD)")
    _shared_legend(fig, axes)
    return _save(fig, path)


def markouts(table: pl.DataFrame, path: Path) -> Path:
    """Baseline quote fills: mean markout at each horizon (solid, with error bars) and the mid move
    in the adverse direction (dashed), for buy and sell fills. A markout that stays negative while
    the mid moves against the fill shows the adverse selection the quotes pay.
    """
    symbols = _symbols(table)
    fig, axes = _symbol_figure(symbols)
    for i, symbol in enumerate(symbols):
        ax = axes[0][i]
        rows = _for_symbol(table, symbol)
        for side, name in SIDE_LABELS.items():
            sub = rows.filter(pl.col("side") == side).sort("horizon_s")
            x = _array(sub, "horizon_s")
            color = SIDE_COLORS[side]
            se = np.nan_to_num(_array(sub, "se_markout_bps"), nan=0.0)
            ax.errorbar(
                x,
                _array(sub, "avg_markout_bps"),
                yerr=SE_Z * se,
                fmt="o-",
                color=color,
                ecolor=ERROR_COLOR,
                elinewidth=ERROR_WIDTH,
                linewidth=LINE_WIDTH,
                markersize=MARKER_SIZE,
                label=f"{name}: markout, ±{SE_Z:g} SE",
            )
            ax.plot(
                x,
                _array(sub, "avg_adverse_bps"),
                linestyle=DASHED,
                color=color,
                linewidth=LINE_WIDTH,
                label=f"{name}: mid move (adverse)",
            )
        ax.set_xscale("log")
        _numeric_ticks(ax, _array(rows, "horizon_s"))
        ax.axhline(0, color=ZERO_LINE, linewidth=ZERO_WIDTH)
        ax.set_xlabel("horizon (s)")
        ax.set_ylabel("bps")
    _figure_title(fig, "Markouts of the baseline quote fills (bps)")
    _shared_legend(fig, axes, MARKOUT_LEGEND_COLUMNS)
    return _save(fig, path)


def intensity_fit(levels: pl.DataFrame, path: Path) -> Path:
    """Measured hit rate at each distance from mid for every calibration day, on a log axis, with
    the fitted a*exp(-k*distance) curve of each day. Levels left out of the fit are hollow. The
    curves should follow the points; the slope k says how fast hits fall off with distance.
    """
    symbols = _symbols(levels)
    fig, axes = _symbol_figure(symbols)
    for i, symbol in enumerate(symbols):
        ax = axes[0][i]
        rows = _for_symbol(levels, symbol).filter(pl.col("rate") > 0)
        grid = np.linspace(
            _array(rows, "delta_usd").min(), _array(rows, "delta_usd").max(), CURVE_POINTS
        )
        for j, date in enumerate(rows["date"].unique(maintain_order=True).to_list()):
            day = rows.filter(pl.col("date") == date)
            color = _color(j)
            fitted = day.filter(pl.col("fitted"))
            left_out = day.filter(~pl.col("fitted"))
            ax.scatter(
                _array(fitted, "delta_usd"),
                _array(fitted, "rate"),
                s=SCATTER_SIZE,
                color=color,
                label=date,
            )
            ax.scatter(
                _array(left_out, "delta_usd"),
                _array(left_out, "rate"),
                s=SCATTER_SIZE,
                facecolors="none",
                edgecolors=color,
            )
            ax.plot(
                grid,
                day["a"].first() * np.exp(-day["k"].first() * grid),
                color=color,
                linewidth=CURVE_WIDTH,
                label=FIT_LABEL,
            )
        ax.set_yscale("log")
        ax.set_xlabel("distance from mid (USD)")
        ax.set_ylabel("aggressive orders per second")
    axes[0][0].scatter([], [], facecolors="none", edgecolors=TEXT, label=HOLLOW_LABEL)
    _figure_title(fig, "Fill-intensity calibration: hits per second by distance from mid")
    _shared_legend(fig, axes, CALIBRATION_LEGEND_COLUMNS)
    return _save(fig, path)


NS_PER_MINUTE = 60_000_000_000
MINUTES_LABEL = "minutes since the live window opened"
LATENCY_BINS = 60
LATENCY_SHOWN_QUANTILE = 0.99


def paper_comparison(
    paper: pl.DataFrame,
    replay: pl.DataFrame,
    start_ts: int,
    lateness_ms: np.ndarray,
    assumed_ms: float,
    path: Path,
) -> Path:
    """Left: equity of the paper session and of its replay over the same live window. Right:
    how late messages arrived (local receive time minus exchange time) against the constant
    data latency the engine assumes. The slowest 1% are left out so a reconnect cannot squeeze
    the rest of the histogram into one bar.
    """
    fig, axes = _figure(2)
    equity, lateness = axes[0]
    runs = ((paper, "paper (live feed)", BLUE), (replay, "replay (backtest)", ORANGE))
    for frame, label, color in runs:
        minutes = (frame["ts"].to_numpy() - start_ts) / NS_PER_MINUTE
        equity.plot(
            minutes, _array(frame, "equity"), color=color, linewidth=LINE_WIDTH, label=label
        )
    equity.axhline(0, color=ZERO_LINE, linewidth=ZERO_WIDTH)
    equity.set_xlabel(MINUTES_LABEL)
    equity.set_ylabel("equity (USD)")
    _usd_axis(equity)
    equity.set_title("Paper session vs replay")
    _legend(equity)
    shown = lateness_ms[lateness_ms <= np.quantile(lateness_ms, LATENCY_SHOWN_QUANTILE)]
    lateness.hist(shown, bins=LATENCY_BINS, color=SKY)
    lateness.axvline(
        assumed_ms,
        color=VERMILION,
        linestyle=DASHED,
        linewidth=LINE_WIDTH,
        label=f"assumed data latency ({assumed_ms:g} ms)",
    )
    lateness.set_xlabel("receive time minus exchange time (ms)")
    lateness.set_ylabel("messages")
    lateness.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
    lateness.set_title("Feed delay, fastest 99% of messages")
    _legend(lateness)
    return _save(fig, path)
