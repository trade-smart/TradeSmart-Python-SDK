"""Buy more than the exchange freeze limit with ``place_sliced_order`` (offline: no account needed).

``buy_in_slices`` sends one order as slices that each fit the freeze limit (NIFTY future: lot 65,
freeze quantity 1801, so at most 1755 = 27 lots per slice) and handles every outcome:

- every slice placed: the order ids;
- stopped by a definite failure (a rejection, the rate limit, ...): the slices placed before it,
  the error, and the quantity never sent; nothing more is sent;
- an ambiguous outcome (``OrderStatusUnknownError``): never resent; the order book is read for
  the slices' shared tag, a few times with a short pause (the book may lag a little).

``main()`` runs it against ``tradesmart.testing.OfflineBroker``, an in-memory broker: no account,
keys, network or market hours. ``buy_in_slices(TradeSmart.from_env(), ...)`` would place real
orders on your account.

Run:  python sliced_order.py
Guide: docs/orders.md ("Sliced orders").
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal

from tradesmart import (
    OrderStatusUnknownError,
    OrderType,
    Product,
    TradeSmart,
    TradeSmartError,
    TransactionType,
)
from tradesmart.testing import OfflineBroker, drills

EXCHANGE, TOKEN, SYMBOL = "NFO", "35001", "NIFTY26NOVFUT"
LOT_SIZE, FREEZE_QUANTITY = 65, 1801
LOOKUPS, LOOKUP_PAUSE_SECONDS = 3, 2.0  # after an unknown outcome: look for the tag 3 times


def find_by_tag(
    client: TradeSmart, tag: str, sleep: Callable[[float], None] = time.sleep
) -> list[str]:
    """The ids of today's orders carrying ``tag``, looked up a few times with a short pause.

    The order book may lag a little behind an order that was placed; an order not found yet is
    not proof that it was not placed.
    """
    for lookup in range(LOOKUPS):
        if lookup:
            sleep(LOOKUP_PAUSE_SECONDS)
        try:
            found = [o.order_id for o in client.orders() if o.tag == tag and o.order_id]
        except TradeSmartError as exc:
            print(f"could not read the order book ({exc})")
            continue
        if found:
            return [order_id for order_id in found if order_id is not None]
    print(
        f"no order with tag {tag} found yet; not found yet is not 'not placed': check the order "
        "book (or the TradeSmart app) again before sending anything"
    )
    return []


def buy_in_slices(
    client: TradeSmart,
    quantity: int,
    price: Decimal,
    tag: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Buy ``quantity`` units of the NIFTY future at ``price`` in slices; the ids placed."""
    try:
        result = client.place_sliced_order(
            exchange=EXCHANGE,
            tradingsymbol=SYMBOL,
            quantity=quantity,  # units, a whole number of lots (MCX too)
            transaction_type=TransactionType.BUY,
            product=Product.NRML,
            order_type=OrderType.LIMIT,
            price=price,
            tag=tag,  # every slice carries this tag
        )
    except OrderStatusUnknownError as exc:
        # One slice may or may not exist. Never resend: look for the tag in the order book
        # (a few times: the book may lag a little).
        partial = exc.sliced_result
        placed = len(partial.order_ids) if partial is not None else 0
        print(f"outcome unknown after {placed} slice(s); looking for tag {tag}")
        found = find_by_tag(client, tag, sleep)
        print(f"orders with tag {tag}: {found}")
        return found
    print("slices:", ", ".join(str(size) for size in result.slice_quantities))
    if not result.complete:
        print(
            f"stopped after {len(result.order_ids)} of {len(result.slice_quantities)} slices "
            f"({type(result.error).__name__}: {result.error}); "
            f"{result.unsent_quantity} not sent"
        )
    return list(result.order_ids)


def add_nifty_future(broker: OfflineBroker) -> None:
    """The NIFTY future on the offline broker, with its lot size and freeze quantity."""
    broker.add_instrument(
        EXCHANGE,
        token=TOKEN,
        symbol=SYMBOL,
        lot_size=LOT_SIZE,
        tick_size="0.10",
        instrument="FUTIDX",
        expiry=date.today() + timedelta(days=14),
        underlying="NIFTY",
        frzqty=FREEZE_QUANTITY,
    )


def main(argv: list[str] | None = None) -> int:
    """Three runs on the offline broker: every slice placed, a rejection of the first slice, and
    an unknown outcome of the first slice (it reached the broker; found by its tag)."""
    del argv  # no options
    try:
        with OfflineBroker() as broker:
            add_nifty_future(broker)
            client = broker.client()
            ids = buy_in_slices(client, 3965, Decimal("24000.00"), tag="slices-1")  # 61 lots
            print("placed:", ids)
            broker.fail_next("PlaceOrder", drills.reject("RMS:Margin Exceeds"))
            print("placed:", buy_in_slices(client, 3965, Decimal("24000.00"), tag="slices-2"))
            broker.fail_next("PlaceOrder", drills.timeout_after_send())
            print("placed:", buy_in_slices(client, 3965, Decimal("24000.00"), tag="slices-3"))
            client.close()
    except TradeSmartError as exc:
        print(f"SDK error {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
