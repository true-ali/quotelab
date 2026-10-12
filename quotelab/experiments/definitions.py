"""The variants of each experiment, as config overrides on top of the baseline.

The baseline is the config file as written: GLFT, queue-aware fills, default latencies, risk
controls off. Every experiment changes one thing relative to it. The grids come from the
``experiments`` section of the config so they are visible and editable in one place.

E5 reuses the E2 runs (it splits their hours by market volatility), and E7 is computed from
the baseline's maker volume, with real reruns at ``e7_rerun_maker_bps`` to check the formula.
"""

from __future__ import annotations

from typing import Any

from quotelab.experiments.jobs import Job, apply_overrides

Variant = tuple[str, dict[str, Any]]

RISK_ON = {"risk": {"kill_switches": True, "circuit_breakers": True, "size_ramp": True}}


def latency(order_ms: float) -> dict[str, Any]:
    """Order and cancel latency ``order_ms``, market data latency half of it.

    Tying data latency to order latency keeps E4 one-dimensional. The 10 ms point reproduces the
    baseline (data 5 ms, orders 10 ms).
    """
    return {
        "engine": {
            "data_latency_ms": order_ms / 2,
            "order_latency_ms": order_ms,
            "cancel_latency_ms": order_ms,
        }
    }


def realism_ladder(cfg: dict[str, Any]) -> list[Variant]:
    """E1: the same GLFT strategy under fill and latency assumptions of growing realism.

    The last step is the adverse-selection stress, a forward-looking assumption about the
    queue, not a filter on outcomes: no cancellations are credited as coming from ahead of us
    (``alpha`` 0) and only part of the volume traded at our price reaches our queue
    (``participation``). It never looks at what the mid did after a fill.
    """
    stress = cfg["experiments"]["e1_stress"]
    zero = latency(0)
    return [
        ("optimistic", apply_overrides(zero, {"fills": {"model": "optimistic"}})),
        ("trade_through", apply_overrides(zero, {"fills": {"model": "trade_through"}})),
        ("queue", apply_overrides(zero, {"fills": {"model": "queue"}})),
        ("queue_latency", {}),
        (
            "queue_latency_stress",
            {"fills": {"alpha": stress["alpha"], "participation": stress["participation"]}},
        ),
    ]


def strategy_comparison() -> list[Variant]:
    """E2: the four strategies, with A-S under both horizon choices."""
    return [
        ("fixed", {"strategy": {"name": "fixed"}}),
        ("skew", {"strategy": {"name": "skew"}}),
        ("as_rolling", {"strategy": {"name": "as", "as_horizon": "match_glft"}}),
        ("as_session", {"strategy": {"name": "as", "as_horizon": "session"}}),
        ("glft", {"strategy": {"name": "glft"}}),
    ]


def sensitivity(cfg: dict[str, Any], symbol: str) -> list[Variant]:
    """E3: GLFT risk aversion, order size and inventory limit, one at a time.

    Risk aversion is varied through the skew per order it implies (gamma is back-solved from
    it), which is in ticks and therefore comparable across symbols.
    """
    grid = cfg["experiments"]
    base_lots = int(cfg["strategy"]["order_lots"][symbol])
    variants: list[Variant] = []
    for ticks in grid["e3_skew_ticks_per_order"]:
        variants.append(
            (f"skew_ticks_{ticks:g}", {"strategy": {"glft_skew_ticks_per_order": ticks}})
        )
    for multiple in grid["e3_size_multipliers"]:
        lots = {"strategy": {"order_lots": {symbol: base_lots * int(multiple)}}}
        variants.append((f"size_x{multiple}", lots))
    for orders in grid["e3_max_inventory_orders"]:
        variants.append((f"max_inventory_{orders}", {"strategy": {"max_inventory_orders": orders}}))
    return variants


def latency_stress(cfg: dict[str, Any]) -> list[Variant]:
    """E4: order latency from the config grid, data latency half of it."""
    return [(f"latency_{ms:g}ms", latency(ms)) for ms in cfg["experiments"]["e4_latency_ms"]]


def fee_reruns(cfg: dict[str, Any]) -> list[Variant]:
    """E7: real reruns at selected maker fees, to check the analytic fee-tier formula."""
    return [
        (f"maker_{bps:g}bps", {"fees": {"maker_bps": bps}})
        for bps in cfg["experiments"]["e7_rerun_maker_bps"]
    ]


def daily_jobs(cfg: dict[str, Any], symbol: str, pairs: list[tuple[str, str]]) -> list[Job]:
    """Every full-day job for one symbol: E1 to E5 and the E7 reruns on each test day."""
    experiments = {
        "E1": realism_ladder(cfg),
        "E2": strategy_comparison(),
        "E3": sensitivity(cfg, symbol),
        "E4": latency_stress(cfg),
        "E5": strategy_comparison(),
        "E7": [("baseline", {}), *fee_reruns(cfg)],
    }
    return [
        Job(name, variant, symbol, calib_date, test_date, overrides)
        for calib_date, test_date in pairs
        for name, variants in experiments.items()
        for variant, overrides in variants
    ]


def event_study_jobs(
    symbol: str, calib_date: str, test_date: str, window: tuple[int, int]
) -> list[Job]:
    """E6: the most violent window, with every risk control off and then on."""
    return [
        Job("E6", "risk_off", symbol, calib_date, test_date, {}, window),
        Job("E6", "risk_on", symbol, calib_date, test_date, RISK_ON, window),
    ]
