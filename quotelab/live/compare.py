"""Compare one paper session with a replay of its live window through the ordinary backtest.

The replay reads the session's own recording, calibration and config, builds the strategy, fill
model and risk layer with the same ``trading_setup`` as the paper session, and runs
``run_backtest`` on the recorded messages in exchange-time order. The simulated exchange, the
fees and the latencies are the same in both, so every difference comes from how the live feed
delivered the messages:

* order: the paper engine saw messages as they arrived, while the replay sorts them by exchange
  time and puts trades before book updates stamped with the same millisecond (the loader's rule);
* clamping: a message stamped earlier than the paper engine's clock was pushed at that clock,
  which happens when the two streams interleave out of order or after a heartbeat advance.

The feed statistics count both, and measure how late messages arrived against the constant data
latency the engine assumes. Every number in the written comparison is computed here.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from quotelab.analytics.attribution import COMPONENTS, run_attribution
from quotelab.analytics.queries import book_mids, markouts
from quotelab.analytics.stats import run_metrics
from quotelab.config import instrument_spec
from quotelab.data.loader import events_from_frames
from quotelab.data.schema import BOOK_SCHEMA, TRADE_SCHEMA
from quotelab.engine.backtest import run_backtest
from quotelab.engine.recorder import BacktestResult, read_run, write_run
from quotelab.live.paper_runner import NS_PER_MINUTE, LiveConfig, trading_setup
from quotelab.report import figures, persian
from quotelab.report.document import Section, Table, fmt, fmt_pct, markdown_document
from quotelab.report.results import README, update_readme
from quotelab.report.sections import usd, utc_time
from quotelab.strategies.calibration import Calibration, read_calibration
from quotelab.types import NS_PER_MS, NS_PER_SECOND

TITLE = "QuoteLab paper session vs replay"
FIGURE = "figures/paper_vs_replay.png"
MARKOUT_HORIZON_S = 5
LATENESS_QUANTILES = (0.5, 0.9, 0.99)
SAME_USD = 0.005
FILL_KEYS = ("ts", "side", "px", "qty")
FEED_COLUMNS = ("ts", "recv_ns", "seq", "pushed_ts")
METRIC_ROWS: tuple[tuple[str, str, int], ...] = (
    ("PnL (USD)", "pnl", 2),
    *((f"of which {name} (USD)", f"attr_{name}", 2) for name in COMPONENTS),
    ("Quote fills", "quote_fills", 0),
    ("Hedge fills", "hedge_fills", 0),
    ("Orders sent", "orders_sent", 0),
    ("Quote fills per order sent", "fill_rate", 3),
    ("Maker volume (USD)", "maker_volume_usd", 0),
    ("Max drawdown (USD)", "max_drawdown_usd", 2),
    ("Mean |position|", "mean_abs_inventory", 4),
    ("Final position", "final_position", 4),
    (f"Adverse move at {MARKOUT_HORIZON_S} s (bps)", "adverse_bps", 3),
    (f"Markout at {MARKOUT_HORIZON_S} s (bps)", "markout_bps", 3),
    ("Risk events", "risk_events", 0),
)


class NoSession(RuntimeError):
    """There is no paper session to compare, or the session never reached its live window."""


@dataclass(frozen=True)
class Session:
    """One recorded paper session as ``PaperSession.close`` wrote it."""

    path: Path
    meta: dict[str, Any]
    book: pl.DataFrame
    trades: pl.DataFrame
    calibration: Calibration

    @property
    def window(self) -> tuple[int, int]:
        return self.meta["live_start_ts"], self.meta["live_end_ts"]


def latest_session(sessions_dir: Path) -> Path:
    """The newest session directory. Names are UTC start times, so they sort by time."""
    found = sorted(path.parent for path in sessions_dir.glob("*/session.json"))
    if not found:
        raise NoSession(f"no paper session in {sessions_dir}; run `make paper` first")
    return found[-1]


def load_session(path: Path) -> Session:
    meta = json.loads((path / "session.json").read_text())
    if not meta["traded"]:
        raise NoSession(f"session {path.name} never reached its live window: {meta['stop_reason']}")
    return Session(
        path,
        meta,
        pl.read_parquet(path / "book.parquet"),
        pl.read_parquet(path / "trades.parquet"),
        read_calibration(path / "calibration.json"),
    )


def replay(session: Session) -> BacktestResult:
    """The backtest of the recorded live window, with the session's own config and calibration."""
    cfg = session.meta["config"]
    spec = instrument_spec(cfg, session.meta["symbol"])
    strategy, fill_model, engine_cfg, risk = trading_setup(cfg, spec, session.calibration)
    book = session.book.select(list(BOOK_SCHEMA))
    trades = session.trades.select(list(TRADE_SCHEMA))
    events = events_from_frames(spec, book, trades, window=session.window)
    return run_backtest(events, strategy, fill_model, spec, engine_cfg, risk)


def run_row(result: BacktestResult, mids: pl.DataFrame, symbol: str) -> dict[str, Any]:
    """The scalars compared side by side: performance, attribution, markouts and risk events.

    Both runs are marked against the same recorded mids, so markouts differ only by fills.
    """
    row: dict[str, Any] = run_metrics(result)
    row.update({f"attr_{name}": value for name, value in run_attribution(result).items()})
    marks = markouts(result.fills, mids, symbol, (MARKOUT_HORIZON_S,))
    quote_marks = marks.filter(pl.col("kind") == "quote")
    for column in ("adverse_bps", "markout_bps"):
        row[column] = float(quote_marks[column].mean()) if quote_marks.height else math.nan
    row["orders_sent"] = float(result.summary["orders_sent"])
    row["final_position"] = float(result.summary["final_position"])
    row["risk_events"] = float(result.risk_events.height)
    kills = result.risk_events.filter(pl.col("kind").str.starts_with("kill_"))
    row["first_kill"] = kills["kind"][0] if kills.height else "none"
    return row


def in_window(frame: pl.DataFrame, window: tuple[int, int]) -> pl.DataFrame:
    return frame.filter((pl.col("ts") >= window[0]) & (pl.col("ts") < window[1]))


def live_messages(session: Session) -> pl.DataFrame:
    """Every message of the live window, book and trades together, with their feed columns."""
    parts = [in_window(frame, session.window) for frame in (session.book, session.trades)]
    return pl.concat([part.select(FEED_COLUMNS) for part in parts])


def lateness_ms(session: Session) -> np.ndarray:
    """Local receive time minus exchange time of each live-window message, in milliseconds.

    It includes the offset between the local clock and Binance's, which is not measured.
    """
    messages = live_messages(session)
    return ((messages["recv_ns"] - messages["ts"]) / NS_PER_MS).to_numpy()


def same_ts_reversals(book: pl.DataFrame, trades: pl.DataFrame) -> int:
    """Trades that arrived after a book update stamped with the same exchange time.

    The replay puts a trade before a book update at the same time, so these are exactly the
    messages the two runs process in opposite orders without either being clamped.
    """
    books = book.select("ts", pl.col("seq").alias("book_seq"))
    later = trades.join(books, on="ts").filter(pl.col("book_seq") < pl.col("seq"))
    return later["seq"].n_unique()


def feed_stats(session: Session) -> dict[str, Any]:
    """How the live feed delivered the live window: volume, ordering, clamping, delay and gaps."""
    book = in_window(session.book, session.window)
    trades = in_window(session.trades, session.window)
    messages = live_messages(session)
    clamps_ms = (
        messages.filter(pl.col("pushed_ts") > pl.col("ts"))
        .select((pl.col("pushed_ts") - pl.col("ts")) / NS_PER_MS)
        .to_series()
    )
    arrivals = messages["recv_ns"].sort().to_numpy()
    late = lateness_ms(session)
    stats: dict[str, Any] = {
        "book_messages": book.height,
        "trade_messages": trades.height,
        "outside_window": session.book.height + session.trades.height - messages.height,
        "clamped": clamps_ms.len(),
        "clamped_share": clamps_ms.len() / messages.height if messages.height else math.nan,
        "clamp_max_ms": float(clamps_ms.max()) if clamps_ms.len() else 0.0,
        "reversals": same_ts_reversals(book, trades),
        "max_arrival_gap_s": float(np.diff(arrivals).max()) / NS_PER_SECOND
        if arrivals.size > 1
        else 0.0,
        "reconnects": len(session.meta["gaps"]),
        "non_market_trades": trades.filter(pl.col("trade_type") != "MARKET").height,
        "lateness_min_ms": float(late.min()) if late.size else math.nan,
        "lateness_max_ms": float(late.max()) if late.size else math.nan,
    }
    for q in LATENESS_QUANTILES:
        stats[f"lateness_p{round(q * 100)}_ms"] = (
            float(np.quantile(late, q)) if late.size else math.nan
        )
    return stats


def first_divergence(paper: pl.DataFrame, replayed: pl.DataFrame, start_ts: int) -> str:
    """One sentence on where the two fill lists first differ in time, side, price or size."""
    ours = paper.select(FILL_KEYS).rows()
    theirs = replayed.select(FILL_KEYS).rows()
    for i, (a, b) in enumerate(zip(ours, theirs, strict=False)):
        if a != b:
            return (
                f"The first {i:,} fills match exactly; fill {i + 1} differs: paper "
                f"{_describe_fill(a, start_ts)}, replay {_describe_fill(b, start_ts)}."
            )
    if len(ours) == len(theirs):
        return f"All {len(ours):,} fills match exactly in time, side, price and size."
    longer = "paper session" if len(ours) > len(theirs) else "replay"
    shared = min(len(ours), len(theirs))
    return (
        f"The first {shared:,} fills match exactly; the {longer} has "
        f"{abs(len(ours) - len(theirs)):,} more."
    )


def _describe_fill(fill: tuple, start_ts: int) -> str:
    ts, side, px, qty = fill
    word = "buy" if side > 0 else "sell"
    return f"{word} {qty:g} at {px:,.2f}, {(ts - start_ts) / NS_PER_SECOND:.3f} s into the window"


def fills_match(paper: pl.DataFrame, replayed: pl.DataFrame) -> bool:
    return paper.select(FILL_KEYS).rows() == replayed.select(FILL_KEYS).rows()


def metric_table(paper: dict[str, Any], replayed: dict[str, Any]) -> Table:
    rows = []
    for label, key, digits in METRIC_ROWS:
        cells = [fmt(paper[key], digits), fmt(replayed[key], digits)]
        rows.append([label, *cells, fmt(paper[key] - replayed[key], digits)])
    uptime_gap = (paper["uptime_both"] - replayed["uptime_both"]) * 100
    rows.append(
        [
            "Uptime, both sides quoted",
            fmt_pct(paper["uptime_both"]),
            fmt_pct(replayed["uptime_both"]),
            f"{fmt(uptime_gap, 1)} pp",
        ]
    )
    rows.append(["First kill switch", paper["first_kill"], replayed["first_kill"], ""])
    return Table(
        "Paper session and replay of the same window",
        ["", "Paper", "Replay", "Paper − replay"],
        rows,
    )


def verdict(
    paper: dict[str, Any], replayed: dict[str, Any], stats: dict[str, Any], same: bool
) -> str:
    """Why the two runs agree or differ, worded from the measured counts."""
    if same and abs(paper["pnl"] - replayed["pnl"]) < SAME_USD:
        return (
            "Paper and replay agree. That is expected rather than evidence of realism: both trade "
            "the same recorded messages against the same simulated exchange with the same "
            "constant latencies, so only the feed's delivery could separate them, and on this "
            f"window it changed no fill even though {stats['clamped']:,} messages were clamped "
            f"and {stats['reversals']:,} trades arrived after a book update with the same "
            "exchange time. The comparison says nothing about whether real orders would have "
            "filled; none were sent."
        )
    return (
        "They differ because the paper engine saw the feed as it arrived: "
        f"{stats['clamped']:,} messages ({fmt_pct(stats['clamped_share'], 2)}) came stamped "
        "earlier than the engine's clock and were pushed at the clock, and "
        f"{stats['reversals']:,} trades arrived after a book update with the same exchange time, "
        "which the replay processes in the opposite order. Either can shift one quote by a "
        "requote cycle, and once one fill differs the inventory, the skew and every later quote "
        "differ too, so the size of the gap says more about path dependence than about the feed."
    )


def comparison_sections(
    session: Session,
    paper: BacktestResult,
    replayed: BacktestResult,
    rows: dict[str, dict[str, Any]],
    stats: dict[str, Any],
) -> list[Section]:
    start, _ = session.window
    same = fills_match(paper.fills, replayed.fills)
    gap = rows["paper"]["pnl"] - rows["replay"]["pnl"]
    summary = [
        f"The paper session's PnL was {usd(rows['paper']['pnl'])} and the replay's "
        f"{usd(rows['replay']['pnl'])}, a difference of {usd(gap)}. "
        + first_divergence(paper.fills, replayed.fills, start),
        verdict(rows["paper"], rows["replay"], stats, same),
    ]
    compared = Section(
        "compare",
        "Paper vs replay",
        summary,
        [metric_table(rows["paper"], rows["replay"])],
        FIGURE,
    )
    return [compared, feed_section(session, stats), setup_section(session)]


def feed_section(session: Session, stats: dict[str, Any]) -> Section:
    assumed = session.meta["config"]["engine"]["data_latency_ms"]
    paragraphs = [
        f"In the live window the feed delivered {stats['book_messages']:,} book updates and "
        f"{stats['trade_messages']:,} trades, with {stats['reconnects']} reconnects; the longest "
        f"wait between two arrivals was {fmt(stats['max_arrival_gap_s'], 2)} s. "
        f"{stats['non_market_trades']:,} trades had a type other than `MARKET`; they are "
        "treated like any other trade, as the downloaded trade files do not mark the type.",
        f"Messages arrived a median {fmt(stats['lateness_p50_ms'], 1)} ms after their exchange "
        f"time (90th percentile {fmt(stats['lateness_p90_ms'], 1)} ms, 99th "
        f"{fmt(stats['lateness_p99_ms'], 1)} ms, maximum {fmt(stats['lateness_max_ms'], 1)} ms). "
        f"The engine assumes a constant {assumed} ms in both runs, so this delay is not a cause "
        "of the difference above; it measures how far the backtest's assumption is from this "
        "machine's connection. The level includes the unmeasured offset between the local "
        "clock and Binance's"
        + (
            f", and the smallest value ({fmt(stats['lateness_min_ms'], 1)} ms) is below zero, "
            "which only a clock offset can explain."
            if stats["lateness_min_ms"] < 0
            else "."
        ),
    ]
    table = Table(
        "Feed during the live window",
        ["Measure", "Value"],
        [
            ["Book updates", f"{stats['book_messages']:,}"],
            ["Trades", f"{stats['trade_messages']:,}"],
            ["Messages outside the live window (warm-up)", f"{stats['outside_window']:,}"],
            ["Reconnects", str(stats["reconnects"])],
            ["Clamped messages", f"{stats['clamped']:,} ({fmt_pct(stats['clamped_share'], 2)})"],
            ["Largest clamp (ms)", fmt(stats["clamp_max_ms"], 0)],
            ["Trades after a same-time book update", f"{stats['reversals']:,}"],
            ["Longest wait between arrivals (s)", fmt(stats["max_arrival_gap_s"], 3)],
            ["Delay p50 / p90 / p99 (ms)", _lateness_cells(stats)],
            [
                "Delay min / max (ms)",
                f"{fmt(stats['lateness_min_ms'], 1)} / {fmt(stats['lateness_max_ms'], 1)}",
            ],
            ["Assumed data latency (ms)", str(assumed)],
        ],
    )
    return Section("feed", "The live feed", paragraphs, [table])


def _lateness_cells(stats: dict[str, Any]) -> str:
    return " / ".join(fmt(stats[f"lateness_p{round(q * 100)}_ms"], 1) for q in LATENESS_QUANTILES)


def setup_section(session: Session) -> Section:
    meta = session.meta
    cfg = meta["config"]
    cal = session.calibration
    engine = cfg["engine"]
    paragraphs = [
        f"The strategy `{cfg['strategy']['name']}` was calibrated on the "
        f"{meta['warmup_ns'] / NS_PER_MINUTE:g} minutes of feed recorded just before the live "
        "window, then traded it with the same code a backtest job uses: "
        f"`{cfg['fills']['model']}` fills (alpha {cfg['fills']['alpha']}), data latency "
        f"{engine['data_latency_ms']} ms, order and cancel latency {engine['order_latency_ms']} "
        f"and {engine['cancel_latency_ms']} ms, and the E6 risk controls on (kill switches, "
        f"circuit breakers, size ramp). Fees: maker {cfg['fees']['maker_bps']} bps and taker "
        f"{cfg['fees']['taker_bps']} bps, config values that were not checked against Binance's "
        "current fee schedule.",
        "Nothing was sent to Binance. The feed is the public read-only market stream, and every "
        "order existed only inside the simulated exchange.",
    ]
    table = Table(
        "Calibration on the warm-up",
        ["Parameter", "Value", "Std. error"],
        [
            ["sigma (USD/√s)", fmt(cal.sigma, 4), ""],
            ["A (fills per second at the mid)", fmt(cal.a, 3), fmt(cal.a_se, 3)],
            ["k (per USD)", fmt(cal.k, 3), fmt(cal.k_se, 3)],
            ["Intensity fit R²", fmt(cal.intensity_r2, 3), ""],
            ["Levels in the fit", str(cal.intensity_levels_used), ""],
            ["OFI beta (USD)", fmt(cal.ofi_beta, 4), fmt(cal.ofi_beta_se, 4)],
        ],
    )
    return Section("setup", "Setup", paragraphs, [table])


def intro(session: Session) -> list[str]:
    start, end = session.window
    meta = session.meta
    return [
        f"One paper session on Binance's public `{meta['symbol']}` USD-M feed. Live window "
        f"{utc_time(start)} to {utc_time(end)} UTC (exchange time), after a "
        f"{meta['warmup_ns'] / NS_PER_MINUTE:g}-minute warm-up. The session stopped because: "
        f"{meta['stop_reason']}.",
        "The replay runs the recorded live window through `run_backtest` with the session's own "
        "config and calibration, so the two differ only in how the feed delivered the messages. "
        "This is a simulation: paper fills come from the same queue-aware fill model as every "
        "backtest, not from an exchange.",
    ]


def readme_block(
    session: Session,
    base: str,
    rows: dict[str, dict[str, Any]],
    stats: dict[str, Any],
    divergence: str,
) -> str:
    """The README's summary of the latest paper session; ``base`` is the report directory
    relative to the README."""
    start, end = session.window
    paper, replayed = rows["paper"], rows["replay"]
    assumed = session.meta["config"]["engine"]["data_latency_ms"]
    return "\n".join(
        [
            "### QuoteLab paper session",
            "",
            f"- `{session.meta['symbol']}`, live window {utc_time(start)} to {utc_time(end)} UTC: "
            f"paper PnL {usd(paper['pnl'])} with {paper['quote_fills']:.0f} quote fills, replay "
            f"{usd(replayed['pnl'])} with {replayed['quote_fills']:.0f}. {divergence}",
            f"- Feed: {stats['book_messages'] + stats['trade_messages']:,} messages, "
            f"{stats['reconnects']} reconnects, {fmt_pct(stats['clamped_share'], 2)} clamped, "
            f"median delay {fmt(stats['lateness_p50_ms'], 1)} ms against the {assumed} ms the "
            "engine assumes.",
            "",
            f"![Paper session vs replay]({base}/{FIGURE})",
            "",
            f"Full comparison: [{base}/COMPARISON.md]({base}/COMPARISON.md).",
        ]
    )


def write_comparison(
    live: LiveConfig, session_path: Path | None = None, readme: Path = README
) -> Path:
    """Replay a session, write ``COMPARISON.md`` and its figure, update the README block.

    The replay's run outputs are written next to the paper run in the session directory. The
    Persian README next to ``readme`` gets its block too, when that file exists.
    """
    path = session_path or latest_session(live.sessions_dir)
    session = load_session(path)
    paper = read_run(path / "paper")
    replayed = replay(session)
    write_run(replayed, path / "replay")
    mids = book_mids(session.book)
    symbol = session.meta["symbol"]
    rows = {"paper": run_row(paper, mids, symbol), "replay": run_row(replayed, mids, symbol)}
    stats = feed_stats(session)
    out = live.reports_dir / path.name
    assumed = float(session.meta["config"]["engine"]["data_latency_ms"])
    figures.paper_comparison(
        paper.timeseries,
        replayed.timeseries,
        session.window[0],
        lateness_ms(session),
        assumed,
        out / FIGURE,
    )
    sections = comparison_sections(session, paper, replayed, rows, stats)
    document = out / "COMPARISON.md"
    document.write_text(markdown_document(TITLE, intro(session), sections))
    divergence = first_divergence(paper.fills, replayed.fills, session.window[0])
    base = Path(os.path.relpath(out, readme.parent)).as_posix()
    update_readme(readme, "paper", readme_block(session, base, rows, stats, divergence))
    readme_fa = readme.with_name(persian.README_FA.name)
    if readme_fa.exists():
        start, end = session.window
        window = (utc_time(start), utc_time(end))
        block = persian.paper_block(symbol, window, rows, stats, assumed, base, FIGURE)
        update_readme(readme_fa, "paper", block)
    return document
