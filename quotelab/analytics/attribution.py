"""PnL attribution that sums exactly to the run's total PnL.

Notation for fill i: side ``s_i`` (+1 buy, -1 sell), price ``p_i``, size ``x_i`` (base units),
exchange mid at the fill ``m_i``, fee ``f_i``. ``m_end`` is the mid at the end of the run.
Funding payments ``F_j`` are positive when we pay.

    spread    = sum over quote fills of  s_i * (m_i - p_i) * x_i
    hedging   = sum over hedge fills of  s_i * (m_i - p_i) * x_i - f_i
    fees      = - sum over quote fills of f_i            (positive when it is a rebate)
    funding   = - sum over j of F_j
    inventory = sum over all fills of    s_i * x_i * (m_end - m_i)

Adding the terms, every ``m_i`` cancels and what is left is
``sum s_i * x_i * (m_end - p_i) - sum f_i - sum F_j``. That is cash plus the final position
marked at ``m_end``, i.e. the run's final equity, because the run starts flat with zero cash.
So the identity holds exactly, not approximately, and ``reconcile`` checks it to the cent.
"""

from __future__ import annotations

import polars as pl

from quotelab.engine.recorder import BacktestResult

COMPONENTS = ("spread", "inventory", "fees", "hedging", "funding")
CENT = 0.01


class AttributionMismatch(Exception):
    """The attribution components do not add up to the engine's final equity."""


def attribution(fills: pl.DataFrame, funding: pl.DataFrame, final_mid: float) -> dict[str, float]:
    """The five components and their ``total`` in USD for one run's fills and funding rows."""
    edge = pl.col("side") * (pl.col("mid") - pl.col("px")) * pl.col("qty")
    carry = pl.col("side") * pl.col("qty") * (final_mid - pl.col("mid"))
    quote = fills.filter(pl.col("kind") == "quote")
    hedge = fills.filter(pl.col("kind") == "hedge")
    parts = {
        "spread": float(quote.select(edge.sum()).item()),
        "inventory": float(fills.select(carry.sum()).item()),
        "fees": -float(quote["fee"].sum()),
        "hedging": float(hedge.select((edge - pl.col("fee")).sum()).item()),
        "funding": -float(funding["payment"].sum()),
    }
    parts["total"] = sum(parts[name] for name in COMPONENTS)
    return parts


def run_attribution(result: BacktestResult) -> dict[str, float]:
    """Attribution of a finished run, marked at the run's final exchange mid."""
    final_mid = result.summary["final_mid"]
    if final_mid is None:
        final_mid = 0.0
    return attribution(result.fills, result.funding, final_mid)


def reconcile(result: BacktestResult) -> float:
    """Attribution total minus the engine's final equity. Raises if they differ by a cent or more.

    The two numbers come from different code paths (the fills table here, the exchange's
    portfolio in the engine), so agreement checks both the bookkeeping and the identity.
    """
    gap = run_attribution(result)["total"] - float(result.summary["final_equity"])
    if abs(gap) >= CENT:
        raise AttributionMismatch(f"attribution misses final equity by {gap:.6f} USD")
    return gap
