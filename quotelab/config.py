"""Load the YAML run configuration and build typed objects from it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from quotelab.types import InstrumentSpec

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "default.yaml"


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    with open(path or DEFAULT_CONFIG) as fh:
        return yaml.safe_load(fh)


def instrument_spec(cfg: dict[str, Any], symbol: str) -> InstrumentSpec:
    try:
        raw = cfg["instruments"][symbol]
    except KeyError as exc:
        raise KeyError(f"no instrument spec for {symbol!r} in config") from exc
    return InstrumentSpec(
        symbol=symbol,
        tick_size=float(raw["tick_size"]),
        lot_size=float(raw["lot_size"]),
        min_notional=float(raw["min_notional"]),
    )


def resolve_path(path: str | Path) -> Path:
    """Relative paths in the config are relative to the repo root, not the working directory."""
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p
