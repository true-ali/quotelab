"""Read-only client for the Binance USD-M public market streams: best bid/offer and trades.

It connects to the public combined-stream endpoint, which needs no API key and has no way to
place, change or cancel an order. Nothing in this package talks to a trading endpoint.

Two streams per symbol are used, chosen to match the historical files the backtests read:

* ``<symbol>@bookTicker``: every change of the best bid or ask, like the daily ``bookTicker``
  file. ``T`` is the transaction time, the column ``convert`` uses for ``ts``.
* ``<symbol>@trade``: every individual trade, like the daily ``trades`` file (not the
  aggregated ``aggTrade`` stream). ``T`` is the trade time and ``m`` is "buyer is maker".

Messages become rows in the processed schemas of ``quotelab.data.schema`` (real units, ``ts``
in nanoseconds), so a recording can be read by the same loader, calibration and analytics code
as a downloaded day.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import websockets

from quotelab.types import NS_PER_MS

PUBLIC_STREAM_URL = "wss://fstream.binance.com/stream"


@dataclass(frozen=True, slots=True)
class FeedMessage:
    """One parsed market message.

    ``kind`` is ``"book"`` or ``"trade"``; ``row`` follows ``BOOK_SCHEMA`` or ``TRADE_SCHEMA``
    with ``ts`` the exchange time. ``recv_ns`` is the local wall clock when it arrived.
    ``trade_type`` is Binance's ``X`` field for trades (for example ``MARKET``) and empty for
    book updates.
    """

    kind: str
    row: dict[str, Any]
    recv_ns: int
    trade_type: str = ""


@dataclass(frozen=True, slots=True)
class FeedGap:
    """The connection dropped at ``at_ns`` (local wall clock) for ``reason``; a retry follows."""

    at_ns: int
    reason: str


def stream_url(symbol: str) -> str:
    name = symbol.lower()
    return f"{PUBLIC_STREAM_URL}?streams={name}@bookTicker/{name}@trade"


def parse_message(raw: str | bytes, recv_ns: int) -> FeedMessage | None:
    """Turn one combined-stream message into a ``FeedMessage``; other messages give None.

    Binance sends prices and sizes as decimal strings and times in milliseconds.
    """
    data = json.loads(raw).get("data", {})
    event = data.get("e")
    if event == "bookTicker":
        row = {
            "ts": int(data["T"]) * NS_PER_MS,
            "bid_px": float(data["b"]),
            "bid_qty": float(data["B"]),
            "ask_px": float(data["a"]),
            "ask_qty": float(data["A"]),
        }
        return FeedMessage("book", row, recv_ns)
    if event == "trade":
        row = {
            "ts": int(data["T"]) * NS_PER_MS,
            "px": float(data["p"]),
            "qty": float(data["q"]),
            "is_buyer_maker": bool(data["m"]),
        }
        return FeedMessage("trade", row, recv_ns, str(data.get("X", "")))
    return None


def backoff_delays(initial_s: float, max_s: float) -> Callable[[], float]:
    """A function returning the wait before each successive reconnect: doubling, capped.

    Doubling spaces out retries when the venue is down for a while without waiting long after a
    single dropped connection.
    """
    delay = initial_s

    def next_delay() -> float:
        nonlocal delay
        current = delay
        delay = min(delay * 2, max_s)
        return current

    return next_delay


async def market_messages(
    url: str,
    silence_s: float,
    backoff_initial_s: float,
    backoff_max_s: float,
    connect: Callable[[str], Any] = websockets.connect,
) -> AsyncIterator[FeedMessage | FeedGap]:
    """Yield parsed messages forever, reconnecting whenever the connection fails.

    A connection counts as failed when it closes, errors, or delivers nothing for
    ``silence_s`` seconds (a socket can stay open while the stream behind it has stopped).
    Each failure yields a ``FeedGap`` so the caller can log it. The backoff resets after a
    connection delivers a message. ``connect`` is a parameter so tests can use a fake server.
    """
    next_delay = backoff_delays(backoff_initial_s, backoff_max_s)
    while True:
        try:
            async with connect(url) as ws:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), silence_s)
                    message = parse_message(raw, time.time_ns())
                    if message is not None:
                        next_delay = backoff_delays(backoff_initial_s, backoff_max_s)
                        yield message
        except (websockets.exceptions.WebSocketException, OSError, TimeoutError) as error:
            yield FeedGap(time.time_ns(), f"{type(error).__name__}: {error}".strip(": "))
        await asyncio.sleep(next_delay())
