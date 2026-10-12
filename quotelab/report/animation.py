"""An animated GIF of the baseline backtest run: a few minutes of the market's best bid and ask
with the simulated fills, the position and the equity, drawn only from the files one backtest
wrote.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
from matplotlib.animation import FuncAnimation, PillowWriter
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from quotelab.report import figures
from quotelab.report.figures import (
    BLUE,
    GREEN,
    ORANGE,
    PURPLE,
    VERMILION,
)
from quotelab.report.tables import RunOutputs

WINDOW_S = 60
FRAME_STEP_S = 1
FPS = 12
HOLD_FRAMES = 12
GIF_DPI = 125
FIGSIZE = (8.0, 6.0)
HEIGHT_RATIOS = (3, 1, 1)
PRICE_LINE_WIDTH = 1.8
FILL_MARKER_SIZE = 8
NS_PER_S = 1_000_000_000
MARGIN = 0.1
FLAT_PAD = 0.5
BASELINE_EXPERIMENT = "E1"
BASELINE_VARIANT = "queue_latency"
BUY_SIDE = 1
SELL_SIDE = -1
QUOTE_KIND = "quote"

_Track = tuple[Line2D, pl.Series, pl.Series]


def baseline_run_dir(outputs: RunOutputs) -> tuple[str, str, Path]:
    """The baseline run of the first symbol on its first test day, as (symbol, test date, run
    directory). Sorting by symbol and date makes the pick the same on every run.
    """
    baseline = outputs.jobs.filter(
        (pl.col("experiment") == BASELINE_EXPERIMENT) & (pl.col("variant") == BASELINE_VARIANT)
    ).sort("symbol", "test_date")
    row = baseline.row(0, named=True)
    return row["symbol"], row["test_date"], outputs.out_dir / "runs" / row["key"]


def busiest_window_start(fills: pl.DataFrame, window_ns: int) -> int:
    """Start, in nanoseconds, of the window with the most quote fills. Windows are aligned to
    multiples of ``window_ns`` so the choice is deterministic, and a tie goes to the earliest
    window. Picking the busiest window keeps the animation on a stretch where the simulator
    trades rather than idles.
    """
    quotes = fills.filter(pl.col("kind") == QUOTE_KIND)
    if quotes.is_empty():
        raise ValueError("no quote fills to animate")
    counts = quotes.group_by((pl.col("ts") // window_ns * window_ns).alias("bucket")).agg(
        pl.len().alias("n")
    )
    return int(counts.filter(pl.col("n") == pl.col("n").max())["bucket"].min())


def _padded(lo: float, hi: float) -> tuple[float, float]:
    """Axis limits around a data range, with a share of the range added on both sides. A constant
    range, such as a flat position of 0, has no width to scale, so it gets a fixed half-height.
    """
    if hi > lo:
        pad = MARGIN * (hi - lo)
        return lo - pad, hi + pad
    return lo - FLAT_PAD, hi + FLAT_PAD


def _in_window(frame: pl.DataFrame, start: int, window_ns: int) -> pl.DataFrame:
    """The rows whose ``ts`` falls in ``[start, start + window_ns)``, in time order, with a
    ``seconds`` column counting from ``start``.
    """
    in_window = (pl.col("ts") >= start) & (pl.col("ts") < start + window_ns)
    return (
        frame.filter(in_window)
        .sort("ts")
        .with_columns(((pl.col("ts") - start) / NS_PER_S).alias("seconds"))
    )


def _track(ax: Axes, x: pl.Series, y: pl.Series, **style: Any) -> _Track:
    """An empty line on ``ax`` with the full data it reveals over time. The data is kept outside
    the line so each frame can show the points up to its time without recomputing them.
    """
    (line,) = ax.plot([], [], **style)
    return line, x, y


def _reveal(tracks: list[_Track], t: int) -> None:
    """Show each line's points at or before ``t`` seconds into the window."""
    for line, x, y in tracks:
        visible = x <= t
        line.set_data(x.filter(visible).to_numpy(), y.filter(visible).to_numpy())


def _price_range(series: pl.DataFrame, quotes: pl.DataFrame) -> tuple[float, float]:
    """The lowest and highest price in the window, across the market quotes and our fills. Polars
    skips nulls here, so a missing value cannot turn an axis limit into NaN.
    """
    low = min(series["bid"].min(), series["ask"].min(), quotes["px"].min())
    high = max(series["bid"].max(), series["ask"].max(), quotes["px"].max())
    return low, high


def _price_tracks(ax: Axes, series: pl.DataFrame, quotes: pl.DataFrame) -> list[_Track]:
    """Market bid and ask as steps, with our buy and sell fills as markers. The price range covers
    the whole window, so the axis does not rescale while the animation plays, and prices are
    printed in full rather than as an offset from a large number.
    """
    ax.set_ylabel("price (USD)")
    ax.ticklabel_format(axis="y", useOffset=False, style="plain")
    ax.set_ylim(*_padded(*_price_range(series, quotes)))
    seconds = series["seconds"]
    buys = quotes.filter(pl.col("side") == BUY_SIDE)
    sells = quotes.filter(pl.col("side") == SELL_SIDE)
    tracks = [
        _track(
            ax,
            seconds,
            series["bid"],
            drawstyle="steps-post",
            color=BLUE,
            linewidth=PRICE_LINE_WIDTH,
            label="best bid",
        ),
        _track(
            ax,
            seconds,
            series["ask"],
            drawstyle="steps-post",
            color=ORANGE,
            linewidth=PRICE_LINE_WIDTH,
            label="best ask",
        ),
        _track(
            ax,
            buys["seconds"],
            buys["px"],
            linestyle="none",
            marker="^",
            markersize=FILL_MARKER_SIZE,
            color=GREEN,
            label="our buys",
        ),
        _track(
            ax,
            sells["seconds"],
            sells["px"],
            linestyle="none",
            marker="v",
            markersize=FILL_MARKER_SIZE,
            color=VERMILION,
            label="our sells",
        ),
    ]
    ax.legend(loc="lower left", bbox_to_anchor=(0.0, 1.0), ncols=len(tracks))
    return tracks


def _position_tracks(ax: Axes, series: pl.DataFrame) -> list[_Track]:
    """Our inventory in base units as steps, on an axis fitted to the window's range."""
    position = series["position"]
    ax.set_ylabel("position")
    ax.set_ylim(*_padded(position.min(), position.max()))
    return [_track(ax, series["seconds"], position, drawstyle="steps-post", color=PURPLE)]


def _equity_tracks(ax: Axes, series: pl.DataFrame) -> list[_Track]:
    """Equity change since the window opened, which is the window's PnL, so every window starts
    from zero."""
    change = series["equity"] - series["equity"].item(0)
    ax.set_ylabel("PnL (USD)")
    ax.set_ylim(*_padded(change.min(), change.max()))
    return [_track(ax, series["seconds"], change, color=BLUE)]


def replay_animation(timeseries: pl.DataFrame, fills: pl.DataFrame, title: str, out: Path) -> Path:
    """Save an animated GIF of the busiest window of one backtest run at ``out``. ``timeseries``
    and ``fills`` are that run's own files. The figure is built on the object-oriented API with an
    explicit Agg canvas, so no pyplot state is involved.
    """
    window_ns = WINDOW_S * NS_PER_S
    start = busiest_window_start(fills, window_ns)
    series = _in_window(timeseries, start, window_ns)
    quotes = _in_window(fills.filter(pl.col("kind") == QUOTE_KIND), start, window_ns)
    with figures.styled():
        fig = Figure(figsize=FIGSIZE, layout="constrained")
        FigureCanvasAgg(fig)
        fig.suptitle(title, x=figures.TITLE_X, ha="left")
        axes = fig.subplots(
            len(HEIGHT_RATIOS), 1, sharex=True, gridspec_kw={"height_ratios": HEIGHT_RATIOS}
        )
        for ax in axes:
            ax.set_xlim(0, WINDOW_S)
        axes[-1].set_xlabel("seconds into the window")
        fig.align_ylabels(axes)
        tracks = [
            *_price_tracks(axes[0], series, quotes),
            *_position_tracks(axes[1], series),
            *_equity_tracks(axes[2], series),
        ]
        frames = list(range(FRAME_STEP_S, WINDOW_S + 1, FRAME_STEP_S)) + [WINDOW_S] * HOLD_FRAMES
        out.parent.mkdir(parents=True, exist_ok=True)
        animation = FuncAnimation(fig, lambda t: _reveal(tracks, t), frames=frames, blit=False)
        animation.save(out, writer=PillowWriter(fps=FPS), dpi=GIF_DPI)
    return out
