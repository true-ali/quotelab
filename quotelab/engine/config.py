"""Engine timing and fee settings. The YAML uses milliseconds and bps; the engine uses ns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from quotelab.types import NS_PER_MS


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """All frictions of the simulated venue.

    * ``data_latency_ns``: exchange event -> strategy sees it (market data and our fills).
    * ``order_latency_ns``: strategy decision -> new order or hedge reaches the exchange.
    * ``cancel_latency_ns``: strategy decision -> cancel reaches the exchange. The order can
      still fill while the cancel is in flight.
    * ``min_requote_ns``: minimum time between two new orders on the same side (throttle).
    * ``timer_ns``: period of ``on_timer`` calls. ``sample_ns``: period of the recorded series.
    """

    data_latency_ns: int = 5 * NS_PER_MS
    order_latency_ns: int = 10 * NS_PER_MS
    cancel_latency_ns: int = 10 * NS_PER_MS
    min_requote_ns: int = 100 * NS_PER_MS
    timer_ns: int = 1000 * NS_PER_MS
    sample_ns: int = 1000 * NS_PER_MS
    maker_fee_bps: float = 2.0
    taker_fee_bps: float = 5.0


def ms_to_ns(ms: float) -> int:
    return round(ms * NS_PER_MS)


def engine_config(cfg: dict[str, Any]) -> EngineConfig:
    eng = cfg["engine"]
    fees = cfg["fees"]
    return EngineConfig(
        data_latency_ns=ms_to_ns(eng["data_latency_ms"]),
        order_latency_ns=ms_to_ns(eng["order_latency_ms"]),
        cancel_latency_ns=ms_to_ns(eng["cancel_latency_ms"]),
        min_requote_ns=ms_to_ns(eng["min_requote_ms"]),
        timer_ns=ms_to_ns(eng["timer_ms"]),
        sample_ns=ms_to_ns(eng["sample_ms"]),
        maker_fee_bps=float(fees["maker_bps"]),
        taker_fee_bps=float(fees["taker_bps"]),
    )
