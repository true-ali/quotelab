"""One backtest job, config overrides, the walk-forward split and the run key.

A job is one strategy configuration replayed on one test day, with its parameters calibrated on
the day before. Several experiments often ask for the same run (the E2 GLFT row, the E1
queue-plus-latency row and the E4 10 ms row are all the baseline), so each job gets a key
derived from the configuration it would actually run with. Equal keys mean equal runs, and
the runner executes each key once.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

RUN_SECTIONS = ("instruments", "fees", "engine", "fills", "strategy", "calibration", "risk")
KEY_DIGEST_CHARS = 10


@dataclass
class Job:
    """One run: ``overrides`` are merged into the base config; ``window`` is (start_ns, end_ns)."""

    experiment: str
    variant: str
    symbol: str
    calib_date: str
    test_date: str
    overrides: dict[str, Any] = field(default_factory=dict)
    window: tuple[int, int] | None = None


def apply_overrides(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """A deep copy of ``base`` with ``overrides`` merged in, nested dicts merged key by key."""
    merged = copy.deepcopy(base)
    _merge_into(merged, overrides)
    return merged


def _merge_into(target: dict[str, Any], overrides: dict[str, Any]) -> None:
    for name, value in overrides.items():
        if isinstance(value, dict) and isinstance(target.get(name), dict):
            _merge_into(target[name], value)
        else:
            target[name] = copy.deepcopy(value)


def walk_forward_pairs(dates: list[str]) -> list[tuple[str, str]]:
    """(calibration day, test day) for each pair of consecutive days.

    Parameters are always fitted on the earlier day and evaluated on the later one, so no test
    day ever informs its own parameters.
    """
    ordered = sorted(dates)
    return list(zip(ordered[:-1], ordered[1:], strict=True))


def job_key(job: Job, base: dict[str, Any]) -> str:
    """``SYMBOL_TESTDATE_digest``, where the digest covers everything that changes the run.

    The digest hashes the effective config sections, not the overrides, so two jobs that reach
    the same config by different overrides share a key. Integers are hashed as floats so that
    ``5`` and ``5.0`` in a YAML file count as the same setting; the window is hashed as text
    because nanosecond timestamps do not survive the trip through a float.
    """
    effective = apply_overrides(base, job.overrides)
    material = {name: effective[name] for name in RUN_SECTIONS}
    material["run"] = {
        "symbol": job.symbol,
        "calib_date": job.calib_date,
        "test_date": job.test_date,
        "window": f"{job.window[0]}-{job.window[1]}" if job.window else None,
    }
    text = json.dumps(_normalise(material), sort_keys=True)
    digest = hashlib.sha1(text.encode()).hexdigest()[:KEY_DIGEST_CHARS]
    return f"{job.symbol}_{job.test_date}_{digest}"


def _normalise(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, dict):
        return {str(name): _normalise(item) for name, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normalise(item) for item in value]
    raise TypeError(f"cannot hash config value of type {type(value).__name__}")
