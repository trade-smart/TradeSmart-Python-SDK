"""Exit a position with an OCO (one cancels the other) order (offline: no account needed).

An OCO is held on TradeSmart's server with a target and a stop-loss, each with its own trigger
value and order. When the last price reaches one trigger, that side's order is sent and the other
side is cancelled. **A trigger that is already crossed when the OCO is placed may fire at once**
and place a real order, so ``exit_long`` reads a fresh last price first and refuses unless it lies
between the two triggers.

- ``exit_long``: one SELL OCO (target above the market, stop-loss below it) with LIMIT legs; after
  an ambiguous outcome (``OrderStatusUnknownError``) it is never resent: ``pending_gtts()`` is read
  for its tag instead;
- ``move_stop_loss``: ``modify_oco`` sends the whole OCO again (``validity`` and
  ``alert_validity`` have no defaults), read back from the pending row with ``stop_loss`` and
  ``target``.

``main()`` runs it against ``tradesmart.testing.OfflineBroker``, an in-memory broker: no account,
keys, network or market hours. The offline broker does not simulate OCO triggering.
``exit_long(TradeSmart.from_env(), ...)`` would place a real OCO on your account.

Run:  python oco_exit.py
Guide: docs/oco.md.
"""

from __future__ import annotations

import sys
from decimal import Decimal

from tradesmart import (
    OcoLeg,
    OrderStatusUnknownError,
    TradeSmart,
    TradeSmartError,
)
from tradesmart.testing import OfflineBroker, drills

EXCHANGE, TOKEN, SYMBOL = "NSE", "1660", "ITC-EQ"


def find_oco_by_tag(client: TradeSmart, tag: str) -> str | None:
    """The alert id of the pending OCO whose legs carry ``tag`` (``None`` when there is none)."""
    try:
        rows = [g for g in client.pending_gtts() if g.is_oco and g.alert_id]
    except TradeSmartError as exc:
        print(f"could not read the pending GTTs ({exc}); check them before sending anything")
        return None
    found = [
        g.alert_id
        for g in rows
        if tag in {(g.oco_first and g.oco_first.tag), (g.oco_second and g.oco_second.tag)}
    ]
    if not found:
        print(
            f"no pending OCO carries tag {tag} (the list may not show it): compare the pending "
            "OCOs with what you sent, and check triggered_gtts() and the order book too"
        )
        return None
    return found[0]


def exit_long(
    client: TradeSmart, *, quantity: int, target: Decimal, stop_loss: Decimal, tag: str
) -> str | None:
    """Sell ``quantity`` shares at the target or the stop-loss, whichever is reached first."""
    try:
        last_price = client.quote(EXCHANGE, TOKEN).last_price
    except TradeSmartError as exc:
        print(f"no quote ({exc}); not placing the OCO")
        return None
    if last_price is None or not stop_loss < last_price < target:
        print(f"LTP {last_price} is not between {stop_loss} and {target}: not placed")
        return None
    try:
        return client.place_oco(
            exchange=EXCHANGE,
            tradingsymbol=SYMBOL,
            transaction_type="SELL",  # a SELL OCO needs the stop-loss trigger below the target's
            quantity=quantity,
            product="CNC",
            target=OcoLeg(trigger_value=target, price=target),
            stop_loss=OcoLeg(trigger_value=stop_loss, price=stop_loss),
            tag=tag,
            alert_validity="GTT",  # until a side triggers or it is cancelled
        )
    except OrderStatusUnknownError:
        # The OCO may or may not exist. Never resend: look for its tag.
        alert_id = find_oco_by_tag(client, tag)
        print(f"outcome unknown; found by tag {tag}: {alert_id}")
        return alert_id
    except TradeSmartError as exc:
        print(f"OCO not placed: {type(exc).__name__}: {exc}")
        return None


def move_stop_loss(client: TradeSmart, alert_id: str, stop_loss: Decimal, tag: str) -> None:
    """Raise the stop-loss of a pending OCO; the rest is sent again from its own row."""
    row = next((g for g in client.pending_gtts() if g.alert_id == alert_id), None)
    if row is None or not row.is_oco or row.target is None or row.quantity is None:
        print("the OCO is not pending or its row is unreadable; not modifying")
        return
    leg = row.target.leg
    if leg.price is None:
        print("the target leg lacks a price; not modifying")
        return
    client.modify_oco(
        alert_id,
        exchange=EXCHANGE,
        tradingsymbol=SYMBOL,
        transaction_type="SELL",
        quantity=row.quantity,
        product="CNC",
        target=OcoLeg(trigger_value=row.target.trigger_value, price=leg.price),
        stop_loss=OcoLeg(trigger_value=stop_loss, price=stop_loss),
        tag=tag,
        validity="DAY",  # required on modify: no default
        alert_validity="GTT",  # required on modify: no default
    )


def main(argv: list[str] | None = None) -> int:
    """On the offline broker: place an OCO, move its stop-loss, read it back and cancel it; an
    unknown outcome found by its tag; an OCO refused because a trigger is already crossed."""
    del argv  # no options
    try:
        with OfflineBroker() as broker:
            broker.add_instrument(
                EXCHANGE, token=TOKEN, symbol=SYMBOL, lot_size=1, tick_size="0.05"
            )
            broker.set_quote(EXCHANGE, TOKEN, lp="400.00", ti="0.05")
            client = broker.client()
            alert_id = exit_long(
                client, quantity=10, target=Decimal("440"), stop_loss=Decimal("360"), tag="exit-1"
            )
            if alert_id is not None:
                move_stop_loss(client, alert_id, Decimal("370"), "exit-1")
                for row in client.pending_gtts():
                    if row.is_oco and row.stop_loss and row.target:
                        print(
                            f"pending OCO {row.alert_id}: stop-loss "
                            f"{row.stop_loss.trigger_value}, target {row.target.trigger_value}"
                        )
                print(f"cancelled {client.cancel_oco(alert_id)}")
            broker.fail_next("PlaceOCOOrder", drills.timeout_after_send())
            unknown = exit_long(
                client, quantity=10, target=Decimal("440"), stop_loss=Decimal("360"), tag="exit-2"
            )
            if unknown is not None:
                print(f"cancelled {client.cancel_oco(unknown)}")
            exit_long(
                client, quantity=10, target=Decimal("390"), stop_loss=Decimal("360"), tag="exit-3"
            )
            client.close()
    except TradeSmartError as exc:
        print(f"SDK error {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
