"""Place an order -- DRY RUN by default.

Without ``--i-understand-this-places-a-real-order`` this script only validates the order
locally and prints the ``PlaceOrder`` request it *would* send; nothing touches the network.
With the flag it places a REAL order on your account.

Credentials (live only): ``TradeSmart.from_env()`` -- the login saved by
``python -m tradesmart login``, or ``TRADESMART_CLIENT_ID`` and ``TRADESMART_ACCESS_TOKEN``.
The dry run uses ``TRADESMART_CLIENT_ID`` when set (else a placeholder) and needs no login.

Dry run:   ``python examples/place_order.py --symbol SBIN-EQ --price 500``
Real order: ``python examples/place_order.py --symbol SBIN-EQ --price 500 \\
              --i-understand-this-places-a-real-order``

A successful response is an OMS acknowledgement, not an execution: the script then reads the
order's row in today's order book (``orders()``, one row per order) until its status leaves
``PENDING`` (about ``POLL_ATTEMPTS`` seconds at most) and prints it. When the row cannot be read it
says the status is unknown: check ``orders()``.

Every order carries a unique ``tag`` (``--tag``, else generated). If placing it ends in
``OrderStatusUnknownError``, the script looks for that tag in the order book and never sends the
order a second time.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from collections.abc import Callable, Sequence
from enum import Enum
from typing import TypeVar

from tradesmart import (
    ClientConfig,
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
)
from tradesmart.endpoints.orders import place_order_call
from tradesmart.models.orders import Order

LIVE_FLAG = "--i-understand-this-places-a-real-order"
POLL_ATTEMPTS = 10  # order-book reads after the acknowledgement
POLL_SECONDS = 1.0
_DEMO_SYMBOL = "SBIN-EQ"  # dry-run default only; a real order needs --symbol explicitly
_DEMO_PRICE = "500"  # dry-run default for LIMIT / SL only
_LIMIT_TYPES = (OrderType.LIMIT, OrderType.SL)

E = TypeVar("E", bound=Enum)


def _word(enum: type[E]) -> Callable[[str], E]:
    """argparse type: the SDK enum parses words (``BUY``, ``MIS``) and wire codes, any case."""

    def parse(text: str) -> E:
        try:
            return enum(text)
        except ValueError:
            words = "/".join(m.name.replace("_", "-") for m in enum)
            raise argparse.ArgumentTypeError(f"{text!r}: use {words} (or the wire code)") from None

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Place an order (dry run by default).")
    parser.add_argument("--exchange", choices=[e.value for e in Exchange], default="NSE")
    parser.add_argument("--symbol", help="trading symbol, e.g. SBIN-EQ")
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument(
        "--side", type=_word(TransactionType), default=TransactionType.BUY, help="BUY or SELL"
    )
    parser.add_argument(
        "--product",
        type=_word(Product),
        default=Product.CNC,
        help="CNC, MIS, NRML, CO, BO or MTF",
    )
    parser.add_argument(
        "--order-type",
        type=_word(OrderType),
        default=OrderType.LIMIT,
        help="LIMIT or SL (TradeSmart refuses MARKET for API orders; SL-M unconfirmed)",
    )
    parser.add_argument("--price", help="limit price (required for LIMIT and SL)")
    parser.add_argument("--trigger-price", help="required for SL and SL-M")
    parser.add_argument("--tag", help="unique order tag (default: generated)")
    parser.add_argument(LIVE_FLAG, dest="live", action="store_true", help="send a REAL order")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.live and args.symbol is None:
        parser.error("--symbol must be given explicitly when placing a real order")
    if args.live and args.order_type in _LIMIT_TYPES and args.price is None:
        parser.error("--price must be given explicitly when placing a real limit order")

    price = args.price
    if price is None and not args.live and args.order_type in _LIMIT_TYPES:
        price = _DEMO_PRICE
    client_id = os.environ.get("TRADESMART_CLIENT_ID") or "YOUR_CLIENT_ID"  # dry run only
    tag = args.tag or f"po-{secrets.token_hex(3)}"  # short and unique per run
    order = {
        "exchange": args.exchange,
        "tradingsymbol": args.symbol or _DEMO_SYMBOL,
        "quantity": args.quantity,
        "transaction_type": args.side,
        "product": args.product,
        "order_type": args.order_type,
        "price": price,
        "trigger_price": args.trigger_price,
        "tag": tag,
    }

    try:
        call = place_order_call(client_id, **order)  # same validation as place_order()
    except RequestValidationError as exc:
        print(f"Order rejected locally, nothing sent: {exc}", file=sys.stderr)
        return 2

    if not args.live:
        print("DRY RUN -- nothing was sent. Request that would be sent:")
        print(f"POST {ClientConfig().base_url}/{call.spec.name}")
        print(json.dumps(call.payload, indent=2))
        print("(sent as the body jData=<compact JSON>, with &, + and % percent-encoded)")
        print(f"Re-run with {LIVE_FLAG} to place this as a REAL order.")
        return 0

    try:
        client = TradeSmart.from_env()  # environment variables, else the saved login
    except TradeSmartError as exc:  # not logged in, or the saved login expired at 06:00 IST
        print(f"Not logged in, nothing was sent: {exc}", file=sys.stderr)
        return 2

    with client:
        order_id: str | None
        try:
            order_id = client.place_order(**order)
        except OrderRejectedError as exc:
            print(f"Rejected by the OMS: {exc.emsg}", file=sys.stderr)
            return 1
        except OrderStatusUnknownError as exc:
            # The request may or may not have reached the exchange. Never resend blindly.
            print(
                f"Outcome unknown ({exc.message}). Looking for tag {tag!r} in orders() ...",
                file=sys.stderr,
            )
            order_id = _find_by_tag(client, tag)
            if order_id is None:
                return 1
            print(f"Found it: order id {order_id} (tag {tag})")
        except TradeSmartError as exc:  # e.g. connection refused: provably not sent
            print(f"Order not placed: {exc}", file=sys.stderr)
            return 1
        else:
            print(f"Acknowledged by the OMS: order id {order_id} (tag {tag})")
        # Confirm the real state: an acknowledgement is not a fill. The order book row is
        # the authority for the current status (the order of order_history() is undocumented).
        row = _wait_for_status(client, order_id)
        if row is None:
            print("  status unknown", file=sys.stderr)
        else:
            print(f"  status={row.status} reject_reason={row.reject_reason or '-'}")
    return 0


def _wait_for_status(client: TradeSmart, order_id: str) -> Order | None:
    """The order's row once its status leaves PENDING (or the last row read); None if unread."""
    last: Order | None = None
    for attempt in range(POLL_ATTEMPTS):
        if attempt:
            time.sleep(POLL_SECONDS)
        try:
            row = next((o for o in client.orders() if o.order_id == order_id), None)
        except TradeSmartError as exc:
            print(f"Could not read the order book ({exc}); check orders().", file=sys.stderr)
            continue
        if row is not None:
            last = row
            if row.status != OrderStatus.PENDING:
                break
    return last


def _find_by_tag(client: TradeSmart, tag: str) -> str | None:
    """The id of today's order carrying ``tag`` (the order book may lag a little), or None."""
    for attempt in range(3):
        if attempt:
            time.sleep(POLL_SECONDS)
        try:
            found = next((o for o in client.orders() if o.tag == tag and o.order_id), None)
        except TradeSmartError as exc:
            print(f"Could not read the order book ({exc}).", file=sys.stderr)
            continue
        if found is not None:
            return found.order_id
    print(
        f"No order with tag {tag!r} in the order book yet. Do not place it again until you "
        "have checked the order book (or the TradeSmart app) once more.",
        file=sys.stderr,
    )
    return None


if __name__ == "__main__":
    sys.exit(main())
