"""Trader-side order management: turns desired two-sided quotes into new orders and cancels.

At most one working order rests per side. A replaced order moves to the cancelling set until the
exchange confirms the cancel, so its late fills are still attributed to it.
"""

from __future__ import annotations

from dataclasses import dataclass

from quotelab.engine.messages import CancelDone, CancelOrder, HedgeOrder, NewOrder, Rejected
from quotelab.types import Fill, InstrumentSpec, Quote, Quotes, Side


@dataclass(slots=True)
class WorkingOrder:
    """An order we sent that is not yet fully filled. ``qty`` is the original size."""

    order_id: int
    side: Side
    px: int
    qty: int
    filled: int = 0


def _matches(order: WorkingOrder, wanted: Quote | None) -> bool:
    """True when the wanted quote is the same price and original size as the order."""
    return wanted is not None and wanted.px == order.px and wanted.qty == order.qty


class OrderManager:
    """Keeps at most one working order per side and throttles how often a side is requoted."""

    def __init__(self, spec: InstrumentSpec, min_requote_ns: int) -> None:
        self.spec = spec
        self.min_requote_ns = min_requote_ns
        self.orders_sent: int = 0
        self.cancels_sent: int = 0
        self.local_rejects: int = 0
        self.throttled: int = 0
        self.hedges_sent: int = 0
        self._next_id: int = 1
        self._working: dict[Side, WorkingOrder] = {}
        self._cancelling: dict[int, WorkingOrder] = {}
        self._last_sent_ts: dict[Side, int] = {}
        self._pending_wake: dict[Side, int] = {}
        self._last_rejected: dict[Side, Quote] = {}

    def working(self, side: Side) -> WorkingOrder | None:
        return self._working.get(side)

    def reconcile(
        self, quotes: Quotes, t_ns: int
    ) -> tuple[list[NewOrder | CancelOrder], int | None]:
        """Turn the desired quotes into exchange actions, BUY side first.

        Returns the actions and the earliest throttle wake time that this call reports for the
        first time, so the caller can schedule a requote when the throttle expires.
        """
        actions: list[NewOrder | CancelOrder] = []
        wakes: list[int] = []
        for side, wanted in ((Side.BUY, quotes.bid), (Side.SELL, quotes.ask)):
            self._cancel_if_changed(side, wanted, actions)
            wake = self._send_if_allowed(side, wanted, t_ns, actions)
            if wake is not None:
                wakes.append(wake)
        return actions, min(wakes, default=None)

    def on_fill(self, fill: Fill) -> None:
        """Count a fill against our order. Fills for ids we do not track (hedges) are ignored."""
        order = self._locate(fill.order_id)
        if order is None:
            return
        order.filled += fill.qty
        if order.filled >= order.qty:
            self._forget(order)

    def on_rejected(self, msg: Rejected) -> None:
        """Drop the working order the exchange refused. Cancelling orders are not affected."""
        for side, order in self._working.items():
            if order.order_id == msg.order_id:
                del self._working[side]
                return

    def on_cancel_done(self, msg: CancelDone) -> None:
        self._cancelling.pop(msg.order_id, None)

    def new_hedge(self, side: Side, qty: int) -> HedgeOrder:
        self.hedges_sent += 1
        return HedgeOrder(self._take_id(), side, qty)

    def _cancel_if_changed(
        self, side: Side, wanted: Quote | None, actions: list[NewOrder | CancelOrder]
    ) -> None:
        """Cancel the working order when the wanted quote moved or was withdrawn.

        The comparison uses the original size, so a partially filled order that still matches
        the wanted quote keeps working.
        """
        current = self._working.get(side)
        if current is None or _matches(current, wanted):
            return
        actions.append(CancelOrder(current.order_id))
        self.cancels_sent += 1
        self._cancelling[current.order_id] = current
        del self._working[side]

    def _send_if_allowed(
        self,
        side: Side,
        wanted: Quote | None,
        t_ns: int,
        actions: list[NewOrder | CancelOrder],
    ) -> int | None:
        """Send a new order for an empty side when the quote is valid and not throttled.

        Returns a throttle wake time only the first time it is reported for that wake time.
        """
        if wanted is None or side in self._working:
            return None
        if not self._is_valid(wanted):
            self._count_local_reject(side, wanted)
            return None
        last = self._last_sent_ts.get(side)
        if last is not None and t_ns - last < self.min_requote_ns:
            return self._report_throttle(side, last + self.min_requote_ns)
        actions.append(self._send(side, wanted, t_ns))
        return None

    def _is_valid(self, wanted: Quote) -> bool:
        """Local checks against the venue minimums: a positive size and the minimum notional."""
        notional = self.spec.notional(wanted.px, wanted.qty)
        return wanted.qty >= 1 and notional >= self.spec.min_notional

    def _count_local_reject(self, side: Side, wanted: Quote) -> None:
        """Count an invalid quote once per run of identical repeats, not once per reconcile."""
        if self._last_rejected.get(side) == wanted:
            return
        self._last_rejected[side] = wanted
        self.local_rejects += 1

    def _report_throttle(self, side: Side, wake: int) -> int | None:
        """Count a throttled side once per wake time, and report that wake time once."""
        if self._pending_wake.get(side) == wake:
            return None
        self._pending_wake[side] = wake
        self.throttled += 1
        return wake

    def _send(self, side: Side, wanted: Quote, t_ns: int) -> NewOrder:
        order_id = self._take_id()
        self._working[side] = WorkingOrder(order_id, side, wanted.px, wanted.qty)
        self._last_sent_ts[side] = t_ns
        self._pending_wake.pop(side, None)
        self.orders_sent += 1
        return NewOrder(order_id, side, wanted.px, wanted.qty)

    def _take_id(self) -> int:
        order_id = self._next_id
        self._next_id += 1
        return order_id

    def _locate(self, order_id: int) -> WorkingOrder | None:
        for order in self._working.values():
            if order.order_id == order_id:
                return order
        return self._cancelling.get(order_id)

    def _forget(self, order: WorkingOrder) -> None:
        self._cancelling.pop(order.order_id, None)
        if self._working.get(order.side) is order:
            del self._working[order.side]
