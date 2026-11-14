"""The flat client API: same methods on ``TradeSmart`` and ``AsyncTradeSmart`` (spec B1, B4)."""

from __future__ import annotations

import inspect
import json
import threading
from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Any

import pytest
import respx

from tests.conftest import BASE
from tests.integration.ws_server import wait_until_sync
from tradesmart import (
    AsyncTradeSmart,
    ClientConfig,
    Exchange,
    OrderStatusUnknownError,
    OrderType,
    Product,
    TradeSmart,
    TransactionType,
)
from tradesmart.models.base import IST
from tradesmart.models.stream import OrderUpdate
from tradesmart.stream import TradeSmartStream

AnyClient = TradeSmart | AsyncTradeSmart

FLAT_METHODS = [
    "login_url",
    "generate_session",
    "set_access_token",
    "profile",
    "margins",
    "positions",
    "holdings",
    "place_order",
    "modify_order",
    "cancel_order",
    "exit_order",
    "orders",
    "order_history",
    "trades",
    "order_margin",
    "search_instruments",
    "quote",
    "security_info",
    "option_chain",
    "historical_data",
    "daily_history",
    "instruments",
    "download_instruments",
    "request",
]

PLACE_KW: dict[str, Any] = dict(
    exchange=Exchange.NSE,
    tradingsymbol="INFY-EQ",
    quantity=1,
    transaction_type=TransactionType.BUY,
    product=Product.CNC,
    order_type=OrderType.LIMIT,
    price=1,
)


@pytest.fixture(params=["sync", "async"])
async def any_client(request: pytest.FixtureRequest) -> AsyncIterator[AnyClient]:
    """Both clients, each closed after the test (no leaked httpx connection pools)."""
    kwargs: dict[str, Any] = {"access_token": "test-access-token", "config": ClientConfig()}
    if request.param == "sync":
        client = TradeSmart("TEST123", **kwargs)
        yield client
        client.close()
    else:
        aclient = AsyncTradeSmart("TEST123", **kwargs)
        yield aclient
        await aclient.aclose()


async def call(client: AnyClient, method: str, *args: Any, **kwargs: Any) -> Any:
    """Call ``client.<method>`` and await the result when the client is async."""
    result = getattr(client, method)(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


@pytest.mark.parametrize("cls", [TradeSmart, AsyncTradeSmart])
@pytest.mark.parametrize("method", FLAT_METHODS)
def test_flat_method_exists(cls: type, method: str) -> None:
    assert callable(getattr(cls, method, None))


@respx.mock
async def test_place_order_returns_order_id(any_client: AnyClient) -> None:
    respx.post(f"{BASE}/PlaceOrder").respond(
        200, json={"stat": "Ok", "norenordno": "26110900000001"}
    )
    assert await call(any_client, "place_order", **PLACE_KW) == "26110900000001"


@respx.mock
async def test_modify_order_returns_order_id(any_client: AnyClient) -> None:
    respx.post(f"{BASE}/ModifyOrder").respond(
        200, json={"stat": "Ok", "norenordno": "26110900000001"}
    )
    order_id = await call(
        any_client,
        "modify_order",
        "26110900000001",
        exchange="NSE",
        tradingsymbol="INFY-EQ",
        quantity=2,
        price=1,
        order_type="LIMIT",
        product="CNC",
    )
    assert order_id == "26110900000001"


MODIFY_KW: dict[str, Any] = dict(
    exchange="NSE",
    tradingsymbol="INFY-EQ",
    quantity=2,
    price=1,
    order_type="LIMIT",
    product="CNC",
)


@pytest.mark.parametrize(
    "ack, expected",
    [
        ({"stat": "Ok", "norenordno": "26110900000009"}, "26110900000009"),
        ({"stat": "Ok", "result": "26110900000007"}, "26110900000007"),
        ({"stat": "Ok", "request_time": "10:00:00 09-11-2026"}, "26110900000001"),
        ({"stat": "Ok", "norenordno": " ", "result": ""}, "26110900000001"),
    ],
    ids=["norenordno", "result", "bare-ok", "blank-fields"],
)
@respx.mock
async def test_modify_order_ok_ack_is_acceptance(
    any_client: AnyClient, ack: dict[str, Any], expected: str
) -> None:
    # Spec A1 (amended): a ModifyOrder ``stat: Ok`` ack is a definitive acceptance.
    route = respx.post(f"{BASE}/ModifyOrder").respond(200, json=ack)
    order_id = await call(any_client, "modify_order", "26110900000001", **MODIFY_KW)
    assert order_id == expected
    assert route.call_count == 1


@respx.mock
async def test_modify_order_unparseable_ack_is_unknown(any_client: AnyClient) -> None:
    route = respx.post(f"{BASE}/ModifyOrder").respond(
        200, json={"stat": "Ok", "norenordno": {"bad": "shape"}}
    )
    with pytest.raises(OrderStatusUnknownError):
        await call(any_client, "modify_order", "26110900000001", **MODIFY_KW)
    assert route.call_count == 1


@respx.mock
async def test_place_order_ack_without_order_id_is_unknown(any_client: AnyClient) -> None:
    respx.post(f"{BASE}/PlaceOrder").respond(200, json={"stat": "Ok", "result": "26110900000007"})
    with pytest.raises(OrderStatusUnknownError):
        await call(any_client, "place_order", **PLACE_KW)


@respx.mock
async def test_cancel_and_exit_return_given_id(any_client: AnyClient) -> None:
    ack = {"stat": "Ok", "dmsg": "success"}
    respx.post(f"{BASE}/CancelOrder").respond(200, json=ack)
    exit_route = respx.post(f"{BASE}/ExitSNOOrder").respond(200, json=ack)
    assert await call(any_client, "cancel_order", "123") == "123"
    assert await call(any_client, "exit_order", "123", "BO") == "123"
    body = json.loads(exit_route.calls.last.request.content.decode()[6:])
    assert body["norenordno"] == "123" and body["prd"] == "B"


@respx.mock
async def test_daily_history_builds_symbol(any_client: AnyClient) -> None:
    route = respx.post(f"{BASE}/EODChartData").respond(200, json=[])
    rows = await call(
        any_client, "daily_history", "NSE", "SBIN-EQ", date(2026, 11, 1), date(2026, 11, 7)
    )
    assert rows == []
    body = json.loads(route.calls.last.request.content.decode()[6:])
    assert body == {
        "sym": "NSE:SBIN-EQ",
        "from": str(int(datetime(2026, 11, 1, tzinfo=IST).timestamp())),
        "to": str(int(datetime(2026, 11, 7, tzinfo=IST).timestamp())),
    }


@respx.mock
async def test_historical_data_takes_from_and_to_dates(any_client: AnyClient) -> None:
    route = respx.post(f"{BASE}/TPSeries").respond(200, json=[])
    await call(
        any_client,
        "historical_data",
        "NSE",
        "22",
        from_date=date(2026, 11, 2),
        to_date=1762300800,
        interval=5,
    )
    body = json.loads(route.calls.last.request.content.decode()[6:])
    assert body["st"] == str(int(datetime(2026, 11, 2, tzinfo=IST).timestamp()))
    assert body["et"] == "1762300800" and body["intrv"] == "5"


async def test_no_namespaces(any_client: AnyClient) -> None:
    assert not hasattr(any_client, "orders") or callable(any_client.orders)
    assert not hasattr(any_client, "market")
    for name in ("auth", "user", "portfolio"):
        assert not hasattr(any_client, name)


def test_eod_symbol_not_exported() -> None:
    import tradesmart

    assert "eod_symbol" not in tradesmart.__all__
    assert not hasattr(tradesmart, "eod_symbol")


def test_stream_on_order_update(threaded_server: Any) -> None:
    got: list[OrderUpdate] = []
    connected = threading.Event()
    stream = TradeSmartStream(
        "TEST123",
        "tok",
        config=ClientConfig(ws_url=threaded_server.url, ws_auth_timeout=1.0),
        on_order_update=got.append,
        on_connect=connected.set,
    )
    stream.start()
    try:
        threaded_server.run(
            threaded_server.send({"t": "om", "norenordno": "26110900000001", "status": "OPEN"})
        )
        wait_until_sync(lambda: got)
    finally:
        stream.stop()
    assert connected.is_set()
    assert isinstance(got[0], OrderUpdate) and got[0].order_id == "26110900000001"
