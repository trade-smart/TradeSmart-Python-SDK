"""One order's whole life -- DRY RUN by default: place (tagged) -> status -> modify -> cancel.

Without ``--live`` this script only validates the three requests locally and prints what it
*would* send (``PlaceOrder``, ``ModifyOrder``, ``CancelOrder``); nothing touches the network and
no login is needed.

With ``--live`` it first checks both prices against a live quote: it rounds them to the tick
size and refuses to send anything unless ``lc <= price < last price`` and
``lc <= new price < last price`` (a BUY below the market rests instead of filling). It then
places a REAL limit BUY order on your account, reads its status from the order book
(``orders()``), moves the price to ``--new-price`` and cancels the order. Whenever the order
exists and its last known status is not final, the script tries to cancel it on the way out
(also after a failed modify). It never places the order twice.

Credentials: ``TradeSmart.from_env()`` -- the login saved by ``python -m tradesmart login``, or
``TRADESMART_CLIENT_ID`` and ``TRADESMART_ACCESS_TOKEN``.

Dry run:  ``python examples/order_lifecycle.py``
Live:     ``python examples/order_lifecycle.py --symbol SBIN-EQ --price P --new-price Q --live``
          with ``P`` and ``Q`` below the current market price (and inside the day's band);
          other prices are refused before anything is sent.

Safety: the order carries a unique ``tag``. If placing it ends in ``OrderStatusUnknownError``
the script looks for that tag in the order book and never sends the order a second time.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import NamedTuple

from tradesmart import (
    Exchange,
    OrderRejectedError,
    OrderStatus,
    OrderStatusUnknownError,
    OrderType,
    Product,
    RequestValidationError,
    TradeSmart,
    TradeSmartError,
    TransactionType,
    round_to_tick,
)
from tradesmart.endpoints.orders import cancel_order_call, modify_order_call, place_order_call
from tradesmart.models.orders import Order

POLL_SECONDS = 1.0  # pause between status checks
POLL_ATTEMPTS = 10  # status checks before giving up waiting
TERMINAL = (OrderStatus.COMPLETE, OrderStatus.CANCELED, OrderStatus.REJECTED)
UNKNOWN = "status unknown - check orders()"


class Plan(NamedTuple):
    """The order this script places and the price it moves it to."""

    exchange: str
    tradingsymbol: str
    quantity: int
    product: Product
    price: str
    new_price: str
    tag: str


def find_by_tag(client: TradeSmart, tag: str) -> Order | None:
    """Today's order carrying ``tag``, or ``None``."""
    return next((order for order in client.orders() if order.tag == tag), None)


def order_row(client: TradeSmart, order_id: str) -> Order | None:
    """The order's row in today's order book (one row per order), or ``None``."""
    return next((order for order in client.orders() if order.order_id == order_id), None)


def wait_for(
    client: TradeSmart,
    order_id: str,
    done: Callable[[Order], bool],
    last: Order | None = None,
) -> Order | None:
    """Poll the order's row until ``done(row)`` or ``POLL_ATTEMPTS`` run out; the last row read.

    A failed read keeps the last row read (``last`` initially): ``None`` means the status is
    unknown.
    """
    for attempt in range(POLL_ATTEMPTS):
        if attempt:
            time.sleep(POLL_SECONDS)
        try:
            row = order_row(client, order_id)
        except TradeSmartError:
            row = None
        if row is not None:
            last = row
            if done(row):
                break
    return last


def _show(label: str, row: Order | None) -> None:
    if row is None:
        print(f"{label}: {UNKNOWN}")
        return
    reason = f" ({row.reject_reason})" if row.reject_reason else ""
    print(f"{label}: status={row.status} price={row.price}{reason}")


def _refuse(message: str) -> None:
    print(f"Refused, nothing was sent: {message}", file=sys.stderr)


def checked_plan(client: TradeSmart, plan: Plan) -> Plan | None:
    """``plan`` with both prices on the tick grid, or ``None`` if they are not safely below
    the market (``lc <= price < last price`` and ``lc <= new price < last price``)."""
    instrument = next(
        (
            m
            for m in client.search_instruments(plan.tradingsymbol)
            if m.exchange == plan.exchange and m.tradingsymbol == plan.tradingsymbol
        ),
        None,
    )
    if instrument is None or not instrument.token:
        _refuse(f"{plan.tradingsymbol} not found on {plan.exchange}")
        return None
    quote = client.quote(plan.exchange, instrument.token)
    tick = client.security_info(plan.exchange, instrument.token).tick_size
    last = quote.last_price
    if last is None or tick is None:
        _refuse("no last price or tick size yet (market closed?)")
        return None
    try:
        price = round_to_tick(plan.price, tick)
        new_price = round_to_tick(plan.new_price, tick)
    except ValueError as exc:
        _refuse(str(exc))
        return None
    for label, value in (("--price", price), ("--new-price", new_price)):
        if value >= last:
            _refuse(f"{label} {value} is not below the last price {last}: it could fill")
            return None
        if quote.lc is not None and value < quote.lc:
            _refuse(f"{label} {value} is below the day's lower price limit {quote.lc}")
            return None
    print(f"Market check: last price {last}, lower limit {quote.lc}, tick {tick}")
    return plan._replace(price=str(price), new_price=str(new_price))


def _dry_run(plan: Plan, client_id: str) -> int:
    calls = [
        place_order_call(
            client_id,
            exchange=plan.exchange,
            tradingsymbol=plan.tradingsymbol,
            quantity=plan.quantity,
            transaction_type=TransactionType.BUY,
            product=plan.product,
            order_type=OrderType.LIMIT,
            price=plan.price,
            tag=plan.tag,
        ),
        modify_order_call(
            client_id,
            order_id="ORDER_ID",
            exchange=plan.exchange,
            tradingsymbol=plan.tradingsymbol,
            quantity=plan.quantity,
            price=plan.new_price,
            order_type=OrderType.LIMIT,
            product=plan.product,
        ),
        cancel_order_call(client_id, "ORDER_ID"),
    ]
    print("DRY RUN -- nothing was sent. The requests a live run sends, in order:")
    for call in calls:
        print(f"\n{call.spec.name}")
        print(json.dumps(call.payload, indent=2))
    print(
        "\nORDER_ID is the id PlaceOrder returns. A live run (--live) first checks both prices "
        "against the market and rounds them to the tick size."
    )
    return 0


def _place(client: TradeSmart, plan: Plan) -> str | None:
    """Place once; after an unknown outcome look the order up by its tag, never resend."""
    try:
        return client.place_order(
            exchange=plan.exchange,
            tradingsymbol=plan.tradingsymbol,
            quantity=plan.quantity,
            transaction_type=TransactionType.BUY,
            product=plan.product,
            order_type=OrderType.LIMIT,
            price=plan.price,
            tag=plan.tag,
        )
    except OrderRejectedError as exc:
        print(f"Rejected by the OMS: {exc.emsg}", file=sys.stderr)
        return None
    except OrderStatusUnknownError:
        print(f"Outcome unknown; looking for tag {plan.tag!r} in orders() ...", file=sys.stderr)
    for attempt in range(3):  # the order book may lag a little
        if attempt:
            time.sleep(POLL_SECONDS)
        found = find_by_tag(client, plan.tag)
        if found is not None and found.order_id:
            print(f"Found it: order id {found.order_id} (status {found.status})")
            return found.order_id
    print(
        f"No order with tag {plan.tag!r} in the order book yet. Do not place it again until "
        "you have checked the order book (or the TradeSmart app) once more.",
        file=sys.stderr,
    )
    return None


def cancel_unless_final(client: TradeSmart, order_id: str, row: Order | None) -> Order | None:
    """Try to cancel unless the last known status is final; the row read afterwards."""
    if row is not None and row.status in TERMINAL:
        return row
    try:
        client.cancel_order(order_id)
    except TradeSmartError as exc:  # rejected (e.g. already filled) or outcome unknown
        print(f"Cancel of {order_id} failed: {exc}. Check orders().", file=sys.stderr)
        return wait_for(client, order_id, lambda r: r.status in TERMINAL, row)
    row = wait_for(client, order_id, lambda r: r.status in TERMINAL, row)
    _show("After cancel", row)
    return row


def _manage(client: TradeSmart, plan: Plan, order_id: str) -> int:
    """Status, modify and cancel an order that was placed (so nothing here is "nothing sent")."""
    row: Order | None = None
    try:
        row = wait_for(client, order_id, lambda r: r.status != OrderStatus.PENDING)
        _show("Status", row)
        if row is None or row.status in TERMINAL:
            return 1  # final: nothing to do; unknown: the finally tries to cancel
        client.modify_order(  # never resent: an unknown outcome propagates
            order_id,
            exchange=plan.exchange,
            tradingsymbol=plan.tradingsymbol,
            quantity=plan.quantity,
            price=plan.new_price,
            order_type=OrderType.LIMIT,
            product=plan.product,
        )
        new_price = Decimal(plan.new_price)
        row = wait_for(
            client, order_id, lambda r: r.price == new_price or r.status in TERMINAL, row
        )
        _show("After modify", row)
    finally:
        row = cancel_unless_final(client, order_id, row)
    return 0 if row is not None and row.status == OrderStatus.CANCELED else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Place, modify and cancel one limit order.")
    parser.add_argument("--exchange", choices=[e.value for e in Exchange], default="NSE")
    parser.add_argument("--symbol", help="trading symbol, e.g. SBIN-EQ (required with --live)")
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--product", choices=["CNC", "MIS", "NRML"], default="CNC")
    parser.add_argument("--price", help="limit BUY price, below the market (checked live)")
    parser.add_argument("--new-price", help="the price to modify the order to (below the market)")
    parser.add_argument("--tag", help="unique order tag (default: generated)")
    parser.add_argument("--live", action="store_true", help="send a REAL order")
    args = parser.parse_args(argv)
    if args.live and None in (args.symbol, args.price, args.new_price):
        parser.error("--live needs --symbol, --price and --new-price given explicitly")
    plan = Plan(
        exchange=args.exchange,
        tradingsymbol=args.symbol or "SBIN-EQ",
        quantity=args.quantity,
        product=Product[args.product],
        price=args.price or "500",
        new_price=args.new_price or "501",
        tag=args.tag or f"lc-{secrets.token_hex(4)}",  # short and unique per run
    )

    try:
        if not args.live:
            return _dry_run(plan, os.environ.get("TRADESMART_CLIENT_ID") or "YOUR_CLIENT_ID")
        client = TradeSmart.from_env()
    except RequestValidationError as exc:
        print(f"Rejected locally, nothing sent: {exc}", file=sys.stderr)
        return 2
    except TradeSmartError as exc:  # not logged in, or the saved login expired at 06:00 IST
        print(f"Not logged in, nothing was sent: {exc}", file=sys.stderr)
        return 2
    with client:
        try:
            checked = checked_plan(client, plan)
            if checked is None:
                return 2
            order_id = _place(client, checked)  # a local rejection here sent nothing
        except RequestValidationError as exc:
            print(f"Rejected locally, nothing sent: {exc}", file=sys.stderr)
            return 2
        except TradeSmartError as exc:  # network failure, timeout, ...
            print(f"Stopped: {exc}. Check orders() before doing anything else.", file=sys.stderr)
            return 1
        if order_id is None:
            return 1
        print(f"Placed: order id {order_id} (an acknowledgement, not a fill)")
        try:
            return _manage(client, checked, order_id)
        except TradeSmartError as exc:  # the order exists: never "nothing sent" from here on
            print(f"Stopped: {exc}. Check orders() before doing anything else.", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
