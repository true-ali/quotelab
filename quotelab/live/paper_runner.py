"""Paper trading on the live Binance feed: the backtest engine, stepped in real time.

One session has two phases, both timed in exchange time from the first message received:

1. Warm-up. ``warmup_minutes`` of book updates and trades are recorded without quoting and
   then calibrated with the same ``calibrate`` used for downloaded days. A calibration from a
   2024 day would be wrong at today's price level, and fitting on the minutes just before
   trading keeps the walk-forward rule: fit on one period, trade the next.
2. Live. For ``minutes`` more, every message is pushed into a ``Backtest`` built exactly as a
   backtest job builds one: the configured strategy, queue-aware fills with the default
   latencies, and the E6 risk controls switched on. Our orders exist only inside the simulated
   exchange. Nothing is ever sent to Binance.

Everything received is written to Parquet in the processed schemas, plus three feed columns
(``recv_ns``, ``seq`` and ``pushed_ts``), so ``paper-compare`` can replay the same hour through
``run_backtest`` and report every difference.

Time. The engine runs on exchange time (Binance's ``T``), like a backtest. Two live-only effects
are handled and counted rather than hidden:

* The book and trade streams are not merged in exchange-time order. A message stamped earlier
  than the engine's clock is pushed at the clock ("clamped"); ``pushed_ts`` records it.
* Exchange time only moves when a message arrives, so a silent feed would freeze the engine and
  the stale-data kill switch could never fire. A heartbeat task therefore advances the engine
  to the exchange time extrapolated from the last message with the local monotonic clock.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from quotelab.config import instrument_spec, resolve_path
from quotelab.data.schema import BOOK_SCHEMA, TRADE_SCHEMA
from quotelab.engine.backtest import Backtest
from quotelab.engine.config import EngineConfig, engine_config
from quotelab.engine.recorder import BacktestResult, write_run
from quotelab.experiments.definitions import RISK_ON
from quotelab.experiments.jobs import apply_overrides
from quotelab.experiments.worker import risk_layer
from quotelab.fills.base import FillModel
from quotelab.fills.factory import fill_model_from_config
from quotelab.live.binance_ws import FeedGap, FeedMessage, market_messages, stream_url
from quotelab.risk.base import RiskLayer
from quotelab.strategies.base import Strategy
from quotelab.strategies.calibration import (
    Calibration,
    calibrate,
    calibration_config,
    write_calibration,
)
from quotelab.strategies.factory import build_strategy
from quotelab.types import NS_PER_SECOND, BookEvent, InstrumentSpec, Side, TradeEvent

NS_PER_MINUTE = 60 * NS_PER_SECOND
FEED_SCHEMA: dict[str, pl.DataType] = {
    "recv_ns": pl.Int64(),
    "seq": pl.Int64(),
    "pushed_ts": pl.Int64(),
}
TRADE_FEED_SCHEMA: dict[str, pl.DataType] = {**FEED_SCHEMA, "trade_type": pl.String()}
DEADLINE_GRACE_S = 120
PROGRESS_EVERY_S = 60


@dataclass(frozen=True)
class LiveConfig:
    """The ``live`` section of the config. Durations in minutes and seconds as written there."""

    symbol: str
    warmup_minutes: float
    minutes: float
    heartbeat_s: float
    silence_reconnect_s: float
    backoff_initial_s: float
    backoff_max_s: float
    sessions_dir: Path
    reports_dir: Path


def live_config(cfg: dict[str, Any]) -> LiveConfig:
    raw = cfg["live"]
    return LiveConfig(
        symbol=raw["symbol"],
        warmup_minutes=float(raw["warmup_minutes"]),
        minutes=float(raw["minutes"]),
        heartbeat_s=float(raw["heartbeat_s"]),
        silence_reconnect_s=float(raw["silence_reconnect_s"]),
        backoff_initial_s=float(raw["backoff_initial_s"]),
        backoff_max_s=float(raw["backoff_max_s"]),
        sessions_dir=resolve_path(raw["sessions_dir"]),
        reports_dir=resolve_path(raw["reports_dir"]),
    )


def session_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """The baseline config with the E6 risk controls on: the setup a live rollout would use."""
    return apply_overrides(cfg, RISK_ON)


def trading_setup(
    cfg: dict[str, Any], spec: InstrumentSpec, cal: Calibration
) -> tuple[Strategy, FillModel, EngineConfig, RiskLayer]:
    """Strategy, fill model, engine settings and risk layer, built as a backtest job builds them.

    The paper session and its replay both call this, so any difference between them comes from
    the feed and not from the setup.
    """
    strategy = build_strategy(cfg["strategy"], spec, cal)
    order_lots = int(cfg["strategy"]["order_lots"][spec.symbol])
    risk = risk_layer(cfg, spec.symbol, order_lots)
    return strategy, fill_model_from_config(cfg), engine_config(cfg), risk


def book_event(spec: InstrumentSpec, row: dict[str, Any]) -> BookEvent:
    return BookEvent(
        row["ts"],
        spec.symbol,
        spec.to_ticks(row["bid_px"]),
        spec.to_lots(row["bid_qty"]),
        spec.to_ticks(row["ask_px"]),
        spec.to_lots(row["ask_qty"]),
    )


def trade_event(spec: InstrumentSpec, row: dict[str, Any]) -> TradeEvent:
    """A trade with the taker's side: ``is_buyer_maker`` means the taker sold (as the loader)."""
    aggressor = Side.SELL if row["is_buyer_maker"] else Side.BUY
    return TradeEvent(
        row["ts"], spec.symbol, spec.to_ticks(row["px"]), spec.to_lots(row["qty"]), aggressor
    )


class PaperSession:
    """The state of one paper session. Synchronous, so a test can drive it message by message.

    ``on_message`` records every message and, in the live phase, pushes it into the engine.
    ``heartbeat`` moves the engine's clock through silence. ``close`` finishes the engine and
    writes every output; it is safe to call after an error or an interrupt.
    """

    def __init__(
        self,
        cfg: dict[str, Any],
        symbol: str,
        out_dir: Path,
        warmup_ns: int,
        live_ns: int,
        heartbeat_ns: int,
    ) -> None:
        self.cfg = session_config(cfg)
        self.spec = instrument_spec(self.cfg, symbol)
        self.out_dir = out_dir
        self.warmup_ns = warmup_ns
        self.live_ns = live_ns
        self.heartbeat_ns = heartbeat_ns
        self.book_columns: dict[str, list] = {name: [] for name in [*BOOK_SCHEMA, *FEED_SCHEMA]}
        self.trade_columns: dict[str, list] = {
            name: [] for name in [*TRADE_SCHEMA, *TRADE_FEED_SCHEMA]
        }
        self.gaps: list[dict[str, Any]] = []
        self.messages = 0
        self.live_start_ts: int | None = None
        self.live_end_ts: int | None = None
        self.engine: Backtest | None = None
        self.calibration: Calibration | None = None
        self.last_message: tuple[int, int] | None = None
        self.done = False
        self.stop_reason = "running"

    def on_message(self, message: FeedMessage, mono_ns: int) -> None:
        """Record one message and, once warm-up is over, push it into the engine.

        Windows are in exchange time: warm-up is ``[first, first + warmup)`` and live is the
        ``live`` duration after it. The first message at or past the live end stops the session
        and is not recorded, so the recording and the replay window contain the same messages.
        """
        ts = message.row["ts"]
        if self.live_start_ts is None:
            self.live_start_ts = ts + self.warmup_ns
            self.live_end_ts = self.live_start_ts + self.live_ns
        if ts >= self.live_end_ts:
            self.done = True
            self.stop_reason = "completed"
            return
        pushed_ts = None
        if ts >= self.live_start_ts:
            if self.engine is None:
                self.engine = self._start_engine()
            pushed_ts = self._push(message)
        self._record(message, pushed_ts)
        latest = ts if self.last_message is None else max(ts, self.last_message[0])
        self.last_message = (latest, mono_ns)

    def on_gap(self, gap: FeedGap) -> None:
        self.gaps.append({"at_ns": gap.at_ns, "reason": gap.reason})

    def heartbeat(self, mono_ns: int) -> None:
        """Advance the engine to the exchange time estimated from the last message.

        The estimate is the newest exchange time seen plus the monotonic time since it arrived,
        minus one heartbeat. While messages keep flowing the target stays behind the engine's
        clock and nothing happens; the lag keeps ordinary latency jitter from pushing the clock
        past messages still in flight. Through a silence the clock moves on, timers fire, and
        after ``stale_after_s`` the risk manager's stale-data kill switch trips.
        """
        if self.engine is None or self.last_message is None:
            return
        latest_ts, at_mono = self.last_message
        target = latest_ts + (mono_ns - at_mono) - self.heartbeat_ns
        self.engine.advance_to(min(target, self.live_end_ts))

    def progress(self) -> str:
        phase = "warm-up" if self.engine is None else "live"
        line = f"[paper] {phase}: {self.messages:,} messages, {len(self.gaps)} reconnects"
        if self.engine is not None:
            portfolio = self.engine.exchange.portfolio
            mid = self.engine.exchange.mid()
            equity = portfolio.equity(mid) if mid is not None else portfolio.cash
            line += f", {portfolio.fill_count} fills, equity {equity:,.2f} USD"
        return line

    def close(self) -> BacktestResult | None:
        """Write the recording, the session metadata and, if trading started, the paper run."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        _sorted_frame(self.book_columns, {**BOOK_SCHEMA, **FEED_SCHEMA}).write_parquet(
            self.out_dir / "book.parquet"
        )
        _sorted_frame(self.trade_columns, {**TRADE_SCHEMA, **TRADE_FEED_SCHEMA}).write_parquet(
            self.out_dir / "trades.parquet"
        )
        result = None
        if self.engine is not None:
            result = self.engine.finish()
            write_run(result, self.out_dir / "paper")
        if self.stop_reason == "running":
            self.stop_reason = "stopped before the live window ended"
        meta = {
            "symbol": self.spec.symbol,
            "live_start_ts": self.live_start_ts,
            "live_end_ts": self.live_end_ts,
            "warmup_ns": self.warmup_ns,
            "live_ns": self.live_ns,
            "messages": self.messages,
            "gaps": self.gaps,
            "stop_reason": self.stop_reason,
            "traded": result is not None,
            "config": self.cfg,
        }
        (self.out_dir / "session.json").write_text(json.dumps(meta, indent=2) + "\n")
        return result

    def _start_engine(self) -> Backtest:
        """Calibrate on the warm-up recording and build the engine the live phase runs on."""
        book = _sorted_frame(self.book_columns, {**BOOK_SCHEMA, **FEED_SCHEMA})
        trades = _sorted_frame(self.trade_columns, {**TRADE_SCHEMA, **TRADE_FEED_SCHEMA})
        cal = calibrate(self.spec, book, trades, calibration_config(self.cfg), "warmup")
        write_calibration(cal, self.out_dir / "calibration.json")
        self.calibration = cal
        strategy, fill_model, engine_cfg, risk = trading_setup(self.cfg, self.spec, cal)
        return Backtest(strategy, fill_model, self.spec, engine_cfg, risk)

    def _push(self, message: FeedMessage) -> int:
        """Push one live message into the engine, clamped to its clock; return the time used."""
        if message.kind == "book":
            event = book_event(self.spec, message.row)
        else:
            event = trade_event(self.spec, message.row)
        if event.ts < self.engine.clock:
            event = dataclasses.replace(event, ts=self.engine.clock)
        self.engine.push(event)
        return event.ts

    def _record(self, message: FeedMessage, pushed_ts: int | None) -> None:
        columns = self.book_columns if message.kind == "book" else self.trade_columns
        for name, value in message.row.items():
            columns[name].append(value)
        columns["recv_ns"].append(message.recv_ns)
        columns["seq"].append(self.messages)
        columns["pushed_ts"].append(pushed_ts)
        if message.kind == "trade":
            columns["trade_type"].append(message.trade_type)
        self.messages += 1


def _sorted_frame(columns: dict[str, list], schema: dict[str, pl.DataType]) -> pl.DataFrame:
    """The recorded columns as a frame sorted by exchange time, arrival order kept for ties."""
    return pl.DataFrame(columns, schema=schema).sort("ts", maintain_order=True)


async def run_session(session: PaperSession, live: LiveConfig, url: str | None = None) -> None:
    """Feed the live stream into ``session`` until its live window ends or the deadline passes.

    The deadline is the warm-up plus live duration plus a grace period on the local clock, so a
    feed that never comes back still ends the session. ``close`` is left to the caller.
    """
    url = url or stream_url(session.spec.symbol)
    messages = market_messages(
        url, live.silence_reconnect_s, live.backoff_initial_s, live.backoff_max_s
    )
    watchdog = asyncio.create_task(_heartbeats(session, live.heartbeat_s))
    total_s = (live.warmup_minutes + live.minutes) * 60 + DEADLINE_GRACE_S
    deadline = asyncio.get_running_loop().time() + total_s
    try:
        async with asyncio.timeout_at(deadline):
            async for item in messages:
                if isinstance(item, FeedGap):
                    session.on_gap(item)
                    print(f"[paper] connection lost ({item.reason}); reconnecting", flush=True)
                    continue
                session.on_message(item, time.monotonic_ns())
                if session.done:
                    break
    except TimeoutError:
        session.stop_reason = "local deadline reached before the live window ended"
    finally:
        watchdog.cancel()
        await messages.aclose()


async def _heartbeats(session: PaperSession, interval_s: float) -> None:
    """Every ``interval_s``, let the session move its clock; print progress once a minute."""
    beats_per_print = max(1, round(PROGRESS_EVERY_S / interval_s))
    beat = 0
    while True:
        await asyncio.sleep(interval_s)
        session.heartbeat(time.monotonic_ns())
        beat += 1
        if beat % beats_per_print == 0:
            print(session.progress(), flush=True)


def new_session(cfg: dict[str, Any], live: LiveConfig, out_dir: Path) -> PaperSession:
    return PaperSession(
        cfg,
        live.symbol,
        out_dir,
        warmup_ns=round(live.warmup_minutes * NS_PER_MINUTE),
        live_ns=round(live.minutes * NS_PER_MINUTE),
        heartbeat_ns=round(live.heartbeat_s * NS_PER_SECOND),
    )
