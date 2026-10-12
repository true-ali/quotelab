"""Paper trading without a network: message parsing, reconnects, the stale-data kill through the
heartbeat, clamping, and agreement with the replay that ``paper-compare`` runs."""

import asyncio
import dataclasses
import json

import polars as pl
import pytest

from quotelab.config import load_config
from quotelab.data.schema import BOOK_SCHEMA, TRADE_SCHEMA
from quotelab.data.synthetic import generate, synthetic_config
from quotelab.engine.recorder import read_run
from quotelab.live.binance_ws import FeedGap, FeedMessage, market_messages, parse_message
from quotelab.live.compare import feed_stats, load_session, write_comparison
from quotelab.live.paper_runner import NS_PER_MINUTE, LiveConfig, PaperSession
from quotelab.types import NS_PER_MS, NS_PER_SECOND

WARMUP_MINUTES = 10
LIVE_MINUTES = 5
ARRIVAL_DELAY_NS = 5 * NS_PER_MS
BOOK_RAW = json.dumps(
    {
        "stream": "btcusdt@bookTicker",
        "data": {
            "e": "bookTicker",
            "u": 1,
            "s": "BTCUSDT",
            "b": "62000.10",
            "B": "1.234",
            "a": "62000.20",
            "A": "0.500",
            "T": 1760000000123,
            "E": 1760000000125,
        },
    }
)
TRADE_RAW = json.dumps(
    {
        "stream": "btcusdt@trade",
        "data": {
            "e": "trade",
            "E": 1760000000130,
            "T": 1760000000128,
            "s": "BTCUSDT",
            "t": 7,
            "p": "62000.20",
            "q": "0.010",
            "X": "MARKET",
            "m": False,
        },
    }
)


def test_parse_book_and_trade_messages():
    book = parse_message(BOOK_RAW, recv_ns=5)
    assert book.kind == "book" and book.recv_ns == 5
    assert book.row == {
        "ts": 1760000000123 * NS_PER_MS,
        "bid_px": 62000.1,
        "bid_qty": 1.234,
        "ask_px": 62000.2,
        "ask_qty": 0.5,
    }
    trade = parse_message(TRADE_RAW, recv_ns=6)
    assert trade.row["ts"] == 1760000000128 * NS_PER_MS
    assert trade.row["is_buyer_maker"] is False and trade.trade_type == "MARKET"
    assert parse_message(json.dumps({"result": None, "id": 1}), recv_ns=7) is None


class FakeSocket:
    """A connection that delivers ``messages`` and then goes silent."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = list(messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def recv(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        await asyncio.sleep(3600)


def test_silent_and_refused_connections_are_reported_and_retried():
    attempts = []

    def connect(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise OSError("connection refused")
        if len(attempts) == 2:
            return FakeSocket([])
        return FakeSocket([BOOK_RAW])

    async def first_three():
        stream = market_messages("wss://fake", 0.05, 0.0, 0.0, connect=connect)
        items = [await anext(stream) for _ in range(3)]
        await stream.aclose()
        return items

    refused, silent, message = asyncio.run(first_three())
    assert isinstance(refused, FeedGap) and "refused" in refused.reason
    assert isinstance(silent, FeedGap) and silent.reason.startswith("TimeoutError")
    assert isinstance(message, FeedMessage) and message.kind == "book"
    assert len(attempts) == 3


@pytest.fixture(scope="module")
def feed() -> list[FeedMessage]:
    """Synthetic frames as a live feed in replay order (trades first at equal times), each
    arriving a constant 5 ms after its exchange time, running a second past the live window."""
    cfg = load_config()
    duration_s = (WARMUP_MINUTES + LIVE_MINUTES) * 60 + 1
    book, trades = generate(dataclasses.replace(synthetic_config(cfg), duration_s=duration_s))
    tagged = [
        (row["ts"], 0, i, FeedMessage("trade", row, row["ts"] + ARRIVAL_DELAY_NS, "MARKET"))
        for i, row in enumerate(trades.select(list(TRADE_SCHEMA)).iter_rows(named=True))
    ]
    tagged += [
        (row["ts"], 1, i, FeedMessage("book", row, row["ts"] + ARRIVAL_DELAY_NS))
        for i, row in enumerate(book.select(list(BOOK_SCHEMA)).iter_rows(named=True))
    ]
    return [message for *_, message in sorted(tagged, key=lambda item: item[:3])]


def new_session(path) -> PaperSession:
    cfg = load_config()
    return PaperSession(
        cfg,
        cfg["synthetic"]["symbol"],
        path,
        WARMUP_MINUTES * NS_PER_MINUTE,
        LIVE_MINUTES * NS_PER_MINUTE,
        NS_PER_SECOND,
    )


def live_config(tmp_path, sessions_dir) -> LiveConfig:
    return LiveConfig(
        "SYNTHUSDT",
        WARMUP_MINUTES,
        LIVE_MINUTES,
        1.0,
        10.0,
        1.0,
        30.0,
        sessions_dir,
        tmp_path / "reports",
    )


def test_in_order_feed_matches_its_replay_exactly(feed, tmp_path):
    session = new_session(tmp_path / "sessions" / "20240101T0000Z")
    for message in feed:
        session.on_message(message, message.recv_ns)
        if session.done:
            break
    paper = session.close()
    assert session.stop_reason == "completed"
    assert paper.fills.height > 0
    readme = tmp_path / "README.md"
    readme.write_text("intro\n")
    document = write_comparison(live_config(tmp_path, tmp_path / "sessions"), readme=readme)
    replayed = read_run(session.out_dir / "replay")
    assert paper.fills.equals(replayed.fills)
    assert paper.timeseries.equals(replayed.timeseries)
    assert paper.risk_events.equals(replayed.risk_events)
    assert "Paper and replay agree" in document.read_text()
    assert "quotelab:results:paper:start" in readme.read_text()


def test_late_message_is_clamped_and_counted(feed, tmp_path):
    session = new_session(tmp_path / "late")
    live_start = feed[0].row["ts"] + WARMUP_MINUTES * NS_PER_MINUTE
    first_live = next(i for i, m in enumerate(feed) if m.row["ts"] >= live_start)
    held = next(
        i for i in range(first_live + 1, len(feed)) if feed[i].row["ts"] > feed[i - 1].row["ts"]
    )
    reordered = [*feed[: held - 1], feed[held], feed[held - 1], *feed[held + 1 :]]
    for message in reordered:
        session.on_message(message, message.recv_ns)
        if session.done:
            break
    session.close()
    stats = feed_stats(load_session(session.out_dir))
    assert stats["clamped"] == 1
    assert stats["clamp_max_ms"] == (feed[held].row["ts"] - feed[held - 1].row["ts"]) / NS_PER_MS


def test_heartbeat_through_a_silent_feed_trips_the_stale_kill(feed, tmp_path):
    session = new_session(tmp_path / "stale")
    live_start = feed[0].row["ts"] + WARMUP_MINUTES * NS_PER_MINUTE
    for message in feed:
        session.on_message(message, message.recv_ns)
        if message.row["ts"] >= live_start + NS_PER_MINUTE:
            break
    last_mono = session.last_message[1]
    for second in range(1, 11):
        session.heartbeat(last_mono + second * NS_PER_SECOND)
    paper = session.close()
    kills = paper.risk_events.filter(pl.col("kind") == "kill_stale")
    assert kills.height == 1
