"""Test a strategy with the offline broker: no account, keys, network or market hours.

``buy_the_dip`` below stands in for your own strategy. Each test sets the market on an
``OfflineBroker``, runs the strategy with a real ``TradeSmart`` client wired to it, and checks
what the strategy sent. The tests use plain ``assert``, so any test runner can collect them.

Run:  pytest offline_strategy_test.py      or      python offline_strategy_test.py
Guide: docs/testing.md ("Testing your strategy offline").
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from tradesmart import (
    APIError,
    OrderStatus,
    OrderStatusUnknownError,
    OrderType,
    Product,
    TradeSmart,
    TradeSmartError,
    TransactionType,
)
from tradesmart.testing import OfflineBroker, drills

EXCHANGE, TOKEN, SYMBOL = "NSE", "22", "ACC-EQ"


# --- the strategy under test (replace with your own) -------------------------------------------


def buy_the_dip(client: TradeSmart, limit: Decimal, quantity: int, tag: str) -> str | None:
    """Buy ``quantity`` at ``limit`` when the last price is at or below it; return the order id.

    After an unknown outcome it looks for its order by ``tag`` and never sends it again.
    """
    quote = client.quote(EXCHANGE, TOKEN)
    if quote.last_price is None or quote.last_price > limit:
        return None
    try:
        return client.place_order(
            exchange=EXCHANGE,
            tradingsymbol=SYMBOL,
            quantity=quantity,
            transaction_type=TransactionType.BUY,
            product=Product.MIS,
            order_type=OrderType.LIMIT,
            price=limit,
            tag=tag,
        )
    except OrderStatusUnknownError:
        mine = [order for order in client.orders() if order.tag == tag]
        return mine[0].order_id if mine else None


# --- helpers -----------------------------------------------------------------------------------


def market(broker: OfflineBroker, last_price: str) -> None:
    """One instrument with a last price."""
    broker.add_instrument(EXCHANGE, token=TOKEN, symbol=SYMBOL, tick_size="0.05")
    broker.set_quote(EXCHANGE, TOKEN, last_price=last_price)


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait for something a stream delivers on its own thread."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the stream")
        time.sleep(0.01)


# --- the tests ---------------------------------------------------------------------------------


def test_buys_when_the_price_dips() -> None:
    with OfflineBroker() as broker:
        market(broker, last_price="99.50")
        client = broker.client()
        order_id = buy_the_dip(client, limit=Decimal("100"), quantity=10, tag="dip-1")
        order = broker.orders[-1]  # what the strategy sent
        assert order.id == order_id
        assert (order.side, order.quantity, order.price) == ("B", 10, Decimal("100"))
        broker.fill(order.id, price="100")  # the test decides the fill
        assert client.orders()[0].status is OrderStatus.COMPLETE
        assert client.positions()[0].net_quantity == 10


def test_does_nothing_above_the_limit() -> None:
    with OfflineBroker() as broker:
        market(broker, last_price="101.00")
        assert buy_the_dip(broker.client(), Decimal("100"), 10, tag="dip-2") is None
        assert broker.orders == []


def test_a_lost_reply_is_found_by_tag_and_never_resent() -> None:
    with OfflineBroker() as broker:
        market(broker, last_price="99.50")
        # The order IS placed, but the reply is lost: the client raises OrderStatusUnknownError.
        broker.fail_next("PlaceOrder", drills.timeout_after_send())
        order_id = buy_the_dip(broker.client(), Decimal("100"), 10, tag="dip-3")
        assert [order.id for order in broker.orders] == [order_id]
        assert [r.endpoint for r in broker.requests].count("PlaceOrder") == 1


def test_a_rejection_reaches_the_strategy() -> None:
    with OfflineBroker() as broker:
        market(broker, last_price="99.50")
        broker.fail_next("PlaceOrder", drills.reject("RMS:Margin Exceeds"))
        try:
            buy_the_dip(broker.client(), Decimal("100"), 10, tag="dip-4")
        except APIError as exc:
            assert "Margin Exceeds" in str(exc)
        else:
            raise AssertionError("the rejection was not raised")
        assert broker.orders == []


def test_a_failed_read_is_retried_by_the_sdk() -> None:
    with OfflineBroker() as broker:
        market(broker, last_price="99.50")
        broker.fail_next("GetQuotes", drills.server_error(503))
        assert buy_the_dip(broker.client(), Decimal("100"), 10, tag="dip-5") is not None
        # With max_retries=0 the same failure reaches the strategy.
        broker.fail_next("GetQuotes", drills.server_error(503))
        try:
            buy_the_dip(broker.client(max_retries=0), Decimal("100"), 10, tag="dip-6")
        except TradeSmartError:
            pass
        else:
            raise AssertionError("the read failure was not raised")


def test_order_updates_arrive_on_the_stream() -> None:
    updates: list[Any] = []
    acks: list[Any] = []
    with OfflineBroker() as broker:
        market(broker, last_price="99.50")
        with broker.stream(on_order_update=updates.append, on_position=acks.append) as stream:
            stream.subscribe_orders()
            stream.subscribe_positions()  # the order subscription has no reply; this one has
            wait_until(lambda: bool(acks))
            order_id = buy_the_dip(broker.client(), Decimal("100"), 10, tag="dip-7")
            assert order_id is not None
            broker.fill(order_id, price="100")
            wait_until(lambda: any(u.status is OrderStatus.COMPLETE for u in updates))


TESTS = [value for name, value in sorted(globals().items()) if name.startswith("test_")]


def main() -> int:
    """Run every test without a test runner; 0 when all pass."""
    failed = 0
    for test in TESTS:
        try:
            test()
        except TradeSmartError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: unexpected SDK error {type(exc).__name__}: {exc}")
        except Exception as exc:  # a failed assert, or a broker driven the wrong way
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {test.__name__}")
    print(f"{len(TESTS) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
