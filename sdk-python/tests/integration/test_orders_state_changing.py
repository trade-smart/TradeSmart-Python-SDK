import json
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError

from tests.conftest import BASE, fixture
from tradesmart import (
    AsyncTradeSmart,
    Exchange,
    OrderRejectedError,
    OrderStatusUnknownError,
    OrderType,
    Product,
    TradeSmart,
    TransactionType,
)

PLACE_KW: dict[str, Any] = dict(
    exchange=Exchange.NSE,
    tradingsymbol="INFY-EQ",
    quantity=1,
    transaction_type=TransactionType.BUY,
    product=Product.CNC,
    order_type=OrderType.LIMIT,
    price=1,
)


@respx.mock
def test_place_order_payload_and_order_id(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/PlaceOrder").respond(200, json=fixture("place_order_success"))
    order_id = client.place_order(**{**PLACE_KW, "price": 1500.5, "amo": True})
    body = json.loads(route.calls.last.request.content.decode()[6:])
    assert body["prc"] == "1500.5" and body["amo"] == "Yes" and "dscqty" not in body
    assert order_id == "26110700000001"


@respx.mock
def test_place_order_encodes_ampersand_symbol(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/PlaceOrder").respond(200, json=fixture("place_order_success"))
    client.place_order(**{**PLACE_KW, "tradingsymbol": "M&M-EQ"})
    assert b'"tsym":"M%26M-EQ"' in route.calls.last.request.content


@respx.mock
def test_place_order_timeout_raises_unknown(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/PlaceOrder").mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(OrderStatusUnknownError):
        client.place_order(**PLACE_KW)
    assert route.call_count == 1


@respx.mock
def test_place_order_5xx_not_retried(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/PlaceOrder").respond(503, text="<html>bad gateway</html>")
    with pytest.raises(OrderStatusUnknownError):
        client.place_order(**PLACE_KW)
    assert route.call_count == 1


@respx.mock
def test_place_order_5xx_json_not_ok_is_order_rejected(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/PlaceOrder").respond(
        500, json={"stat": "Not_Ok", "emsg": "Internal error"}
    )
    with pytest.raises(OrderRejectedError, match="Internal error") as info:
        client.place_order(**PLACE_KW)
    assert info.value.http_status == 500
    assert route.call_count == 1


@respx.mock
def test_reject_raises_order_rejected(client: TradeSmart) -> None:
    respx.post(f"{BASE}/CancelOrder").respond(200, json=fixture("errors")["order_reject"])
    with pytest.raises(OrderRejectedError, match="Order not found"):
        client.cancel_order("123")


@respx.mock
def test_cancel_order_live_ack_shape_returns_the_order_id(client: TradeSmart) -> None:
    """The live server acknowledges a cancel with ``stat: Ok`` and the order id in ``result``."""
    ack = fixture("cancel_order_ack_live_shape")
    respx.post(f"{BASE}/CancelOrder").respond(200, json=ack)
    assert client.cancel_order(ack["result"]) == ack["result"]


@respx.mock
async def test_async_cancel_order_live_ack_shape_returns_the_order_id(
    async_client: AsyncTradeSmart,
) -> None:
    ack = fixture("cancel_order_ack_live_shape")
    respx.post(f"{BASE}/CancelOrder").respond(200, json=ack)
    assert await async_client.cancel_order(ack["result"]) == ack["result"]


@respx.mock
def test_modify_cancel_exit_payloads(client: TradeSmart) -> None:
    m = respx.post(f"{BASE}/ModifyOrder").respond(200, json=fixture("place_order_success"))
    c = respx.post(f"{BASE}/CancelOrder").respond(200, json=fixture("cancel_order_success"))
    e = respx.post(f"{BASE}/ExitSNOOrder").respond(200, json=fixture("cancel_order_success"))
    order_id = client.modify_order(
        "1",
        exchange=Exchange.NSE,
        tradingsymbol="INFY-EQ",
        quantity=2,
        price=10,
        order_type=OrderType.LIMIT,
        product=Product.CNC,
    )
    assert order_id == "26110700000001"
    assert client.cancel_order("1") == "1"
    assert client.exit_order("1", Product.BO) == "1"
    assert json.loads(m.calls.last.request.content.decode()[6:]) == {
        "uid": "TEST123",
        "actid": "TEST123",
        "exch": "NSE",
        "norenordno": "1",
        "tsym": "INFY-EQ",
        "qty": "2",
        "prc": "10",
        "prctyp": "LMT",
        "prd": "C",
        "ordersource": "API",
    }
    assert c.calls.last.request.content == b'jData={"uid":"TEST123","norenordno":"1"}'
    assert e.calls.last.request.content == b'jData={"uid":"TEST123","norenordno":"1","prd":"B"}'


@respx.mock
def test_cancel_timeout_not_retried(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/CancelOrder").mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(OrderStatusUnknownError):
        client.cancel_order("1")
    assert route.call_count == 1


@respx.mock
async def test_async_place_order_returns_order_id(async_client: AsyncTradeSmart) -> None:
    respx.post(f"{BASE}/PlaceOrder").respond(200, json=fixture("place_order_success"))
    assert await async_client.place_order(**PLACE_KW) == "26110700000001"


@respx.mock
async def test_async_place_order_timeout_not_retried(async_client: AsyncTradeSmart) -> None:
    route = respx.post(f"{BASE}/PlaceOrder").mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(OrderStatusUnknownError):
        await async_client.place_order(**PLACE_KW)
    assert route.call_count == 1


NUMERIC_ACK = {"stat": "Ok", "norenordno": 12345, "request_time": "10:00:00 07-11-2026"}
BAD_ACK = {"stat": "Ok", "norenordno": {"bad": "shape"}, "request_time": "x"}


def _assert_unknown(exc: OrderStatusUnknownError, endpoint: str) -> None:
    assert exc.endpoint == endpoint
    assert "accepted by the server" in str(exc) and "orders()" in str(exc)
    assert exc.body is not None and "bad" in exc.body
    assert "TEST123" not in str(exc) and "INFY" not in str(exc)  # no request payload
    assert not isinstance(exc.__cause__, ValidationError)
    assert not isinstance(exc.__context__, ValidationError)
    assert exc.__cause__ is None and exc.__context__ is None


@respx.mock
def test_place_order_numeric_ack_is_coerced(client: TradeSmart) -> None:
    respx.post(f"{BASE}/PlaceOrder").respond(200, json=NUMERIC_ACK)
    assert client.place_order(**PLACE_KW) == "12345"


@respx.mock
def test_place_order_unparseable_ack_is_unknown_outcome(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/PlaceOrder").respond(200, json=BAD_ACK)
    with pytest.raises(OrderStatusUnknownError) as info:
        client.place_order(**PLACE_KW)
    assert route.call_count == 1
    _assert_unknown(info.value, "PlaceOrder")


@respx.mock
def test_cancel_unparseable_ack_is_unknown_outcome(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/CancelOrder").respond(200, json=BAD_ACK)
    with pytest.raises(OrderStatusUnknownError) as info:
        client.cancel_order("1")
    assert route.call_count == 1
    _assert_unknown(info.value, "CancelOrder")


@respx.mock
async def test_async_place_order_numeric_ack_is_coerced(async_client: AsyncTradeSmart) -> None:
    respx.post(f"{BASE}/PlaceOrder").respond(200, json=NUMERIC_ACK)
    assert await async_client.place_order(**PLACE_KW) == "12345"


@respx.mock
async def test_async_place_order_unparseable_ack_is_unknown_outcome(
    async_client: AsyncTradeSmart,
) -> None:
    route = respx.post(f"{BASE}/PlaceOrder").respond(200, json=BAD_ACK)
    with pytest.raises(OrderStatusUnknownError) as info:
        await async_client.place_order(**PLACE_KW)
    assert route.call_count == 1
    _assert_unknown(info.value, "PlaceOrder")


@respx.mock
async def test_async_cancel_unparseable_ack_is_unknown_outcome(
    async_client: AsyncTradeSmart,
) -> None:
    route = respx.post(f"{BASE}/CancelOrder").respond(200, json=BAD_ACK)
    with pytest.raises(OrderStatusUnknownError) as info:
        await async_client.cancel_order("1")
    assert route.call_count == 1
    _assert_unknown(info.value, "CancelOrder")


@respx.mock
def test_order_book_numeric_order_id_is_str(client: TradeSmart) -> None:
    respx.post(f"{BASE}/OrderBook").respond(200, json=[{"stat": "Ok", "norenordno": 777}])
    assert client.orders()[0].order_id == "777"


MODIFY_KW: dict[str, Any] = dict(
    exchange=Exchange.NSE,
    tradingsymbol="INFY-EQ",
    quantity=2,
    price=10,
    order_type=OrderType.LIMIT,
    product=Product.CNC,
)
NO_ORDER_NUMBER_ACK = {"stat": "Ok", "request_time": "10:00:00 09-11-2026"}


@respx.mock
def test_place_ack_without_order_id_is_unknown(client: TradeSmart) -> None:
    respx.post(f"{BASE}/PlaceOrder").respond(200, json=NO_ORDER_NUMBER_ACK)
    with pytest.raises(OrderStatusUnknownError):
        client.place_order(**PLACE_KW)


@respx.mock
def test_modify_ack_without_order_id_returns_callers_id(client: TradeSmart) -> None:
    # Spec A1 is PlaceOrder-only: a ModifyOrder ``stat: Ok`` ack is an acceptance.
    respx.post(f"{BASE}/ModifyOrder").respond(200, json=NO_ORDER_NUMBER_ACK)
    assert client.modify_order("1", **MODIFY_KW) == "1"


@respx.mock
def test_place_ack_with_blank_order_id_is_unknown(client: TradeSmart) -> None:
    respx.post(f"{BASE}/PlaceOrder").respond(200, json={"stat": "Ok", "norenordno": "  "})
    with pytest.raises(OrderStatusUnknownError):
        client.place_order(**PLACE_KW)


@respx.mock
def test_cancel_ack_without_order_id_is_ok(client: TradeSmart) -> None:
    respx.post(f"{BASE}/CancelOrder").respond(200, json={"stat": "Ok", "dmsg": "success"})
    assert client.cancel_order("1") == "1"


@respx.mock
def test_place_and_modify_send_ordersource_api(client: TradeSmart) -> None:
    p = respx.post(f"{BASE}/PlaceOrder").respond(200, json=fixture("place_order_success"))
    m = respx.post(f"{BASE}/ModifyOrder").respond(200, json=fixture("place_order_success"))
    client.place_order(**PLACE_KW)
    client.modify_order("1", **MODIFY_KW)
    for route in (p, m):
        body = json.loads(route.calls.last.request.content.decode()[6:])
        assert body["ordersource"] == "API"
