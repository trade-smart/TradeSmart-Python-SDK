"""The M2 (modify) and M9 (views) additions to the family dry-run rig (``uat_family_rig.py``).

``modify_views_rig(tmp_path)`` is ``family_rig`` (real session, ledger, guard, run on a fake
clock, ``UatLikeMarket``, one instrument per role) plus:

- a real ``TradeSmartStream`` on the broker whose order updates feed an ``OrderWatcher``, set as
  ``run.watcher`` (heartbeat 5 s: the broker's 0.5 s default reconnects a stream whose process is
  busy for a second, and updates sent meanwhile are lost; live streams use the 30 s default);
- UAT-like modify rules on that client only (the offline broker's surface does not grow): a product
  change is refused; a test may script the n-th modify call: ``"lie"`` (acknowledged, nothing
  changes), ``"stop"`` (the STOP file appears, then the call goes out), ``"interrupt"`` (the
  session is marked interrupted, as Ctrl-C between calls does) or ``"reject:<message>"``;
- ``install_async``: the same UAT-like place / cancel policy on an ``AsyncTradeSmart``.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.integration.uat_family_rig import MORNING, Rig, family_rig
from tests.integration.uat_like import ALGO_MKT, RMS, UatLikeMarket
from tests.live_orders.uat_matrix import views
from tradesmart import AsyncTradeSmart
from tradesmart.enums import OrderType, Product
from tradesmart.testing import drills

RUN_ID = "1113fam5"
PRODUCT_REFUSAL = "Rejected : ORA:Product change is not allowed in modify"
STEADY_HEARTBEAT = 5.0


@dataclass
class ModifyViewsRig:
    base: Rig
    watcher: views.OrderWatcher
    script: dict[int, str] = field(default_factory=dict)
    refuse_product: bool = True
    modify_calls: int = 0
    product_changes: list[str] = field(default_factory=list)

    @property
    def broker(self) -> Any:
        return self.base.broker

    @property
    def market(self) -> UatLikeMarket:
        return self.base.market

    @property
    def session(self) -> Any:
        return self.base.session

    @property
    def run(self) -> Any:
        return self.base.run

    @property
    def root(self) -> Path:
        return self.base.root

    def count(self, endpoint: str) -> int:
        return len(self.base.requests(endpoint))

    def count_for(self, endpoint: str, order_id: str) -> int:
        return sum(1 for r in self.base.requests(endpoint) if str(r.get("norenordno")) == order_id)

    def statuses(self) -> dict[str, str]:
        return {o.id: o.status for o in self.broker.orders}

    def records(self) -> list[dict[str, Any]]:
        return self.base.records()

    def ledger_lines(self) -> list[dict[str, Any]]:
        text = self.base.ledger_path().read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]


def _outside_band(rig: ModifyViewsRig, tradingsymbol: str, kwargs: dict[str, Any]) -> bool:
    """A modify price or trigger outside the instrument's circuit band (UAT acknowledges it and
    leaves the order unchanged, seen live 13 Nov 2026)."""
    inst = rig.market.instruments.get(tradingsymbol)
    if inst is None:
        return False
    for name in ("price", "trigger_price"):
        value = kwargs.get(name)
        if value is not None and not inst.lower <= Decimal(str(value)) <= inst.upper:
            return True
    return False


ACK_PREFIXES = ("ack-reject:", "ack-apply", "ack-unreadable")


def _acknowledge_only(
    rig: ModifyViewsRig, order_id: str, action: str, kwargs: dict[str, Any]
) -> None:
    """A modify acknowledged ``Ok`` of an order that is already final; the outcome shows only in
    the order history: ``ack-reject:<words>`` appends an entry carrying the refusal (the order
    stays CANCELED), ``ack-apply`` applies the new price (history entry, order changed),
    ``ack-unreadable`` leaves the history unreadable from now on."""
    state = rig.broker._state
    with state.lock:
        order = state._find(order_id)
        assert order is not None
        if action.startswith("ack-reject:"):
            words = action.removeprefix("ack-reject:")
            state._update(order, "Rejected", {"reject_reason": words})
        elif action == "ack-apply":
            state._update(order, "Replaced", {"price": Decimal(str(kwargs["price"]))})
    state._drain()
    if action == "ack-unreadable":
        client = rig.session.client

        def unreadable(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("order history unreadable")

        client.order_history = unreadable


def _modify_policy(rig: ModifyViewsRig) -> None:
    client = rig.session.client
    modify: Callable[..., str] = client.modify_order
    # One modify at a time through the rig: ``fail_next`` arms a refusal for the NEXT ModifyOrder
    # the broker receives, so arming it and sending this modify must not be split by another
    # case's modify (the matrix runs cases in a thread pool; the intermittent ``product`` step
    # ``record`` of 13-14 Nov 2026 was that race).
    lock = threading.Lock()

    def modified(order_id: str, **kwargs: Any) -> str:
        with lock:
            return _modify_once(order_id, kwargs)

    def _modify_once(order_id: str, kwargs: dict[str, Any]) -> str:
        rig.modify_calls += 1
        action = rig.script.get(rig.modify_calls, "")
        if action == "lie":
            return order_id  # acknowledged; the order does not change
        if action.startswith(ACK_PREFIXES):
            _acknowledge_only(rig, order_id, action, kwargs)
            return order_id
        if action == "stop":
            (rig.root / "STOP").write_text("", encoding="utf-8")
        if action == "interrupt":  # as a Ctrl-C between calls marks the session
            rig.session.interrupted = True
        order = next((o for o in rig.broker.orders if o.id == order_id), None)
        if order is not None and _outside_band(rig, order.symbol, kwargs):
            return order_id  # UAT: acknowledged ``Ok``, the order is not changed
        if action.startswith("reject:"):
            rig.broker.fail_next("ModifyOrder", drills.reject(action.removeprefix("reject:")))
        elif rig.refuse_product and order is not None and kwargs.get("product") is not None:
            wanted = Product(str(kwargs["product"])).value
            if wanted != order.product:
                rig.product_changes.append(order_id)
                rig.broker.fail_next("ModifyOrder", drills.reject(PRODUCT_REFUSAL))
        return modify(order_id, **kwargs)

    client.modify_order = modified


def install_async(market: UatLikeMarket, client: AsyncTradeSmart) -> AsyncTradeSmart:
    """``UatLikeMarket``'s policy on an ``AsyncTradeSmart`` of the same offline broker: the order
    is booked through the async client's own request, then decided as the sync policy does."""
    place: Callable[..., Awaitable[str]] = client.place_order
    cancel: Callable[[str], Awaitable[str]] = client.cancel_order

    async def placed(**kwargs: Any) -> str:
        order_type = OrderType(str(kwargs["order_type"]))
        tradingsymbol = str(kwargs["tradingsymbol"])
        if tradingsymbol in market.rms:
            market.broker.fail_next("PlaceOrder", drills.reject(RMS))
        elif order_type in (OrderType.MARKET, OrderType.SL_M) and market.market == "reject":
            market.broker.fail_next("PlaceOrder", drills.reject(ALGO_MKT))
        order_id = await place(**kwargs)  # raises on a refusal in the reply
        inst = market.instruments[tradingsymbol]
        if market._sticks(inst.exchange, tradingsymbol):
            return order_id
        reason = market._invalid(inst, kwargs, order_type)
        if reason is not None:
            market.broker.reject(order_id, reason)
            return order_id
        market.broker.accept(order_id)
        market._match(order_id, inst, kwargs, order_type)
        return order_id

    async def cancelled(order_id: str) -> str:
        market.cancels.append(order_id)
        order = next((o for o in market.broker.orders if o.id == order_id), None)
        if order is not None and market._sticks(order.exchange, order.symbol):
            return order_id
        return await cancel(order_id)

    client.place_order = placed  # type: ignore[method-assign]
    client.cancel_order = cancelled  # type: ignore[method-assign]
    return client


@contextmanager
def modify_views_rig(
    tmp_path: Path, *, now: datetime = MORNING, uat: bool = False
) -> Iterator[ModifyViewsRig]:
    with family_rig(tmp_path, now=now, run_id=RUN_ID, uat=uat) as base:
        client = base.session.client
        config = replace(client.config, heartbeat_interval=STEADY_HEARTBEAT)
        with views.order_stream(client, config=config) as watcher:
            base.run.watcher = watcher
            rig = ModifyViewsRig(base, watcher)
            _modify_policy(rig)
            try:
                yield rig
            finally:
                base.run.watcher = None
