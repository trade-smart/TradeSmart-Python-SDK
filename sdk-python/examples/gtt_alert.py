"""Buy the dip with a GTT (good till triggered) order (offline: no account needed).

A GTT is held on TradeSmart's server as an alert: when the live value it watches crosses the
trigger value, the server places the order described by the other fields. **A GTT whose
condition is already true when it is placed fires at once** and places a real order, so
``buy_below`` reads a fresh last price first and refuses when the LTP is already at or below the
trigger. Alert validity ``GTT`` keeps it for at most 365 days or the contract's expiry, whichever
is earlier.

- ``buy_below``: one ``LTP_B_O`` GTT (LTP less than the trigger) for a CNC LIMIT BUY; after an
  ambiguous outcome (``OrderStatusUnknownError``) it is never resent: ``pending_gtts()`` is read
  for its tag instead;
- ``move_trigger``: ``modify_gtt`` sends every field again, both validities from the GTT's own
  row (``modify_gtt`` has no defaults for them, so nothing changes by accident).

``main()`` runs it against ``tradesmart.testing.OfflineBroker``, an in-memory broker: no account,
keys, network or market hours (a GTT fires there only when ``broker.trigger_gtt()`` is called).
``buy_below(TradeSmart.from_env(), ...)`` would place a real GTT on your account.

Run:  python gtt_alert.py
Guide: docs/gtt.md.
"""

from __future__ import annotations

import sys
from decimal import Decimal

from tradesmart import (
    Gtt,
    GttAlertType,
    GttValidity,
    OrderStatusUnknownError,
    OrderType,
    Product,
    TradeSmart,
    TradeSmartError,
    TransactionType,
)
from tradesmart.testing import OfflineBroker, drills

EXCHANGE, TOKEN, SYMBOL = "NSE", "1660", "ITC-EQ"


def find_gtt_by_tag(client: TradeSmart, tag: str) -> str | None:
    """The alert id of the pending GTT carrying ``tag`` (``None`` when there is none)."""
    try:
        found = [g.alert_id for g in client.pending_gtts() if g.tag == tag and g.alert_id]
    except TradeSmartError as exc:
        print(f"could not read the pending GTTs ({exc}); check them before sending anything")
        return None
    if not found:
        print(f"no pending GTT with tag {tag}; check triggered_gtts() and the order book too")
        return None
    return found[0]


def buy_below(client: TradeSmart, *, trigger: Decimal, price: Decimal, tag: str) -> str | None:
    """Buy 1 share at ``price`` once the LTP falls below ``trigger``; the GTT's alert id."""
    try:
        last_price = client.quote(EXCHANGE, TOKEN).last_price
    except TradeSmartError as exc:
        print(f"no quote ({exc}); not placing the GTT")
        return None
    if last_price is None or last_price <= trigger:
        print(f"LTP {last_price} is not above {trigger}: the GTT would fire at once; not placed")
        return None
    try:
        return client.place_gtt(
            exchange=EXCHANGE,
            tradingsymbol=SYMBOL,
            alert_type=GttAlertType.LTP_BELOW,  # fires when the LTP is less than trigger_value
            trigger_value=trigger,
            quantity=1,
            transaction_type=TransactionType.BUY,
            product=Product.CNC,
            order_type=OrderType.LIMIT,
            price=price,
            tag=tag,  # required: the key to find the GTT again
            alert_validity=GttValidity.GTT,  # until it fires or is cancelled (365 days at most)
        )
    except OrderStatusUnknownError:
        # The GTT may or may not exist. Never resend: look for its tag.
        alert_id = find_gtt_by_tag(client, tag)
        print(f"outcome unknown; found by tag {tag}: {alert_id}")
        return alert_id
    except TradeSmartError as exc:
        print(f"GTT not placed: {type(exc).__name__}: {exc}")
        return None


def move_trigger(client: TradeSmart, gtt: Gtt, trigger: Decimal) -> str | None:
    """Move a pending GTT's trigger; every other field is sent again from its own row."""
    needed = {
        "alert_id": gtt.alert_id,
        "exchange": gtt.exchange,
        "tradingsymbol": gtt.tradingsymbol,
        "alert_type": gtt.alert_type,
        "quantity": gtt.quantity,
        "transaction_type": gtt.transaction_type,
        "product": gtt.product,
        "order_type": gtt.order_type,
        "tag": gtt.tag,
        "validity": gtt.validity,
        "alert_validity": gtt.alert_validity,
    }
    if (
        gtt.alert_id is None
        or gtt.exchange is None
        or gtt.tradingsymbol is None
        or gtt.alert_type is None
        or gtt.quantity is None
        or gtt.transaction_type is None
        or gtt.product is None
        or gtt.order_type is None
        or gtt.tag is None
        or gtt.validity is None
        or gtt.alert_validity is None
    ):
        # Never guess a field: a wrong one would change the GTT. Pass the values you placed with.
        missing = [name for name, value in needed.items() if value is None]
        print(f"the GTT row lacks {', '.join(missing)}; not modifying")
        return None
    if gtt.order_type in (OrderType.SL, OrderType.SL_M) and gtt.trigger_price is None:
        # A stop-loss order needs its trigger_price on every modify: never guess it.
        print("the stop-loss GTT row lacks trigger_price; not modifying")
        return None
    return client.modify_gtt(
        gtt.alert_id,
        exchange=gtt.exchange,
        tradingsymbol=gtt.tradingsymbol,
        alert_type=gtt.alert_type,  # cannot change: send the original
        trigger_value=trigger,
        quantity=gtt.quantity,
        transaction_type=gtt.transaction_type,
        product=gtt.product,
        order_type=gtt.order_type,
        price=gtt.price,
        trigger_price=gtt.trigger_price,  # the stop-loss trigger of an SL order (not the GTT's)
        tag=gtt.tag,
        validity=gtt.validity,  # required on modify: no default
        alert_validity=gtt.alert_validity,  # required on modify: no default
    )


def add_itc(broker: OfflineBroker) -> None:
    """ITC on the offline broker, with a last price of 400."""
    broker.add_instrument(EXCHANGE, token=TOKEN, symbol=SYMBOL, lot_size=1, tick_size="0.05")
    broker.set_quote(EXCHANGE, TOKEN, lp="400.00", ti="0.05")


def main(argv: list[str] | None = None) -> int:
    """On the offline broker: place, list, modify and fire a GTT; an unknown outcome found by its
    tag and cancelled; a GTT refused because it would fire at once."""
    del argv  # no options
    try:
        with OfflineBroker() as broker:
            add_itc(broker)
            client = broker.client()
            alert_id = buy_below(client, trigger=Decimal("380"), price=Decimal("380"), tag="dip-1")
            for gtt in client.pending_gtts():
                print(f"pending GTT {gtt.alert_id}: {gtt.alert_type} {gtt.trigger_value}")
                move_trigger(client, gtt, Decimal("370"))
            if alert_id is not None:
                order_id = broker.trigger_gtt(alert_id)  # the offline broker fires only on request
                fired = [o for o in client.orders() if o.parent_id == alert_id]
                print(f"fired: order {order_id} (parent_id {fired[0].parent_id})")
            broker.fail_next("PlaceGTTOrder", drills.timeout_after_send())
            unknown = buy_below(client, trigger=Decimal("380"), price=Decimal("380"), tag="dip-2")
            if unknown is not None:
                print(f"cancelled {client.cancel_gtt(unknown)}")
            broker.set_quote(EXCHANGE, TOKEN, lp="375.00")
            buy_below(client, trigger=Decimal("380"), price=Decimal("380"), tag="dip-3")
            client.close()
    except TradeSmartError as exc:
        print(f"SDK error {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
