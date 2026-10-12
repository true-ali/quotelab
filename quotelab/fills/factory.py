"""Build the fill model selected by the run configuration."""

from __future__ import annotations

from typing import Any

from quotelab.fills.base import FillModel
from quotelab.fills.optimistic import OptimisticFill
from quotelab.fills.queue_aware import QueueAwareFill
from quotelab.fills.trade_through import TradeThroughFill

MODEL_NAMES = ("optimistic", "queue", "trade_through")


def fill_model_from_config(cfg: dict[str, Any]) -> FillModel:
    """Build the model named by ``cfg["fills"]["model"]``. ``alpha`` and ``participation`` apply
    only to the queue model, and ``participation`` defaults to counting all at-level volume."""
    settings = cfg["fills"]
    name = settings["model"]
    if name == "queue":
        return QueueAwareFill(
            alpha=float(settings["alpha"]),
            participation=float(settings.get("participation", 1.0)),
        )
    if name == "optimistic":
        return OptimisticFill()
    if name == "trade_through":
        return TradeThroughFill()
    raise ValueError(f"unknown fill model {name!r}; valid: {', '.join(MODEL_NAMES)}")
