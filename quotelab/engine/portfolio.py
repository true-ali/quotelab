"""Position and cash bookkeeping in quote currency (USDT)."""

from __future__ import annotations

from dataclasses import dataclass

from quotelab.types import Fill, InstrumentSpec


def fee_for(notional: float, fee_bps: float) -> float:
    """Fee in quote currency. Positive is paid; a negative ``fee_bps`` is a rebate."""
    return notional * fee_bps / 10_000


@dataclass(slots=True)
class Portfolio:
    """Signed position in lots plus cash. Equity = cash + position marked at a price.

    The run starts flat with zero cash, so equity is the run's total PnL.
    """

    spec: InstrumentSpec
    position: int = 0
    cash: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    fill_count: int = 0

    def apply_fill(self, fill: Fill) -> None:
        notional = self.spec.notional(fill.px, fill.qty)
        self.position += int(fill.side) * fill.qty
        self.cash -= int(fill.side) * notional + fill.fee
        self.fees += fill.fee
        self.fill_count += 1

    def funding_payment(self, rate: float, mark: float) -> float:
        """Perpetual funding owed now: longs pay ``rate * position notional`` when ``rate > 0``.

        Positive means we pay, negative means we receive.
        """
        return self.position_qty() * mark * rate

    def pay_funding(self, payment: float) -> None:
        self.cash -= payment
        self.funding += payment

    def position_qty(self) -> float:
        return self.spec.to_qty(self.position)

    def equity(self, mark: float) -> float:
        return self.cash + self.position_qty() * mark
