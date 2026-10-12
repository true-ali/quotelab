"""Run outputs: fills, sampled timeseries, funding and risk events as parquet, plus a JSON summary.

Every table has a fixed schema, so an empty run still writes files with the same columns and
dtypes as a non-empty one and can be read back the same way.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import polars as pl

from quotelab.risk.base import RiskEvent

FILLS_SCHEMA: dict[str, pl.DataType] = {
    "ts": pl.Int64,
    "order_id": pl.Int64,
    "side": pl.Int8,
    "px": pl.Float64,
    "qty": pl.Float64,
    "fee": pl.Float64,
    "is_maker": pl.Boolean,
    "kind": pl.String,
    "mid": pl.Float64,
    "position": pl.Float64,
}

TIMESERIES_SCHEMA: dict[str, pl.DataType] = {
    "ts": pl.Int64,
    "mid": pl.Float64,
    "bid": pl.Float64,
    "ask": pl.Float64,
    "position": pl.Float64,
    "cash": pl.Float64,
    "equity": pl.Float64,
    "fees_cum": pl.Float64,
    "funding_cum": pl.Float64,
    "n_fills": pl.Int64,
    "quoting_bid": pl.Boolean,
    "quoting_ask": pl.Boolean,
}

FUNDING_SCHEMA: dict[str, pl.DataType] = {
    "ts": pl.Int64,
    "rate": pl.Float64,
    "mark": pl.Float64,
    "position": pl.Float64,
    "payment": pl.Float64,
}

RISK_EVENTS_SCHEMA: dict[str, pl.DataType] = {
    "ts": pl.Int64,
    "kind": pl.String,
    "detail": pl.String,
}


@dataclass
class BacktestResult:
    """Everything one simulation run produces: four tables and a flat summary of scalars."""

    fills: pl.DataFrame
    timeseries: pl.DataFrame
    funding: pl.DataFrame
    risk_events: pl.DataFrame
    summary: dict


def frame(rows: list[dict], schema: dict[str, pl.DataType]) -> pl.DataFrame:
    """Build a table with the given schema. An empty row list yields an empty, typed table."""
    return pl.DataFrame(rows, schema=schema)


def risk_events_frame(events: list[RiskEvent]) -> pl.DataFrame:
    return frame([asdict(event) for event in events], RISK_EVENTS_SCHEMA)


def write_run(result: BacktestResult, out_dir: Path) -> None:
    """Write the run as four parquet tables and a deterministic summary.json."""
    out_dir.mkdir(parents=True, exist_ok=True)
    result.fills.write_parquet(out_dir / "fills.parquet")
    result.timeseries.write_parquet(out_dir / "timeseries.parquet")
    result.funding.write_parquet(out_dir / "funding.parquet")
    result.risk_events.write_parquet(out_dir / "risk_events.parquet")
    summary_text = json.dumps(result.summary, indent=2, sort_keys=True)
    (out_dir / "summary.json").write_text(summary_text + "\n")


def read_run(out_dir: Path) -> BacktestResult:
    """Load a run written by write_run. Parquet keeps dtypes, so the tables come back unchanged."""
    return BacktestResult(
        fills=pl.read_parquet(out_dir / "fills.parquet"),
        timeseries=pl.read_parquet(out_dir / "timeseries.parquet"),
        funding=pl.read_parquet(out_dir / "funding.parquet"),
        risk_events=pl.read_parquet(out_dir / "risk_events.parquet"),
        summary=json.loads((out_dir / "summary.json").read_text()),
    )
