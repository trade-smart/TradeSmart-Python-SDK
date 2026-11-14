"""Order-path refinements from the 2026-11-10 review (C3)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx
import pytest
import respx

from tests.conftest import BASE
from tests.integration.ws_server import MockWsServer, wait_until, wait_until_sync
from tradesmart import (
    AsyncTradeSmart,
    AsyncTradeSmartStream,
    ClientConfig,
    ConfigurationError,
    Exchange,
    OrderRejectedError,
    OrderStatusUnknownError,
    RateLimitError,
    ResponseParseError,
    ServerError,
    TradeSmart,
    TradeSmartStream,
)
from tradesmart._transport import ratelimit
from tradesmart.endpoints.base import parse_list
from tradesmart.endpoints.market import _parse_eod
from tradesmart.models.base import TradeSmartModel
from tradesmart.stream.protocol import format_keys

SCRIPT_URL = "https://v2api.tradesmartonline.in/NSE_symbols.txt.zip"
PLACE_KW: dict[str, Any] = dict(
    exchange="NSE",
    tradingsymbol="INFY-EQ",
    quantity=1,
    transaction_type="BUY",
    product="C",
    order_type="LMT",
    price=1,
)
ORDER_ACK = {"stat": "Ok", "norenordno": "26110700000001"}


# --- a. order rate limit never waits longer than order_rate_limit_max_wait -------------


def test_order_rate_limit_max_wait_default_and_validation() -> None:
    assert ClientConfig().order_rate_limit_max_wait == 1.0
    assert ClientConfig(order_rate_limit_max_wait=0).order_rate_limit_max_wait == 0
    with pytest.raises(ConfigurationError, match="order_rate_limit_max_wait"):
        ClientConfig(order_rate_limit_max_wait=-0.1)


@respx.mock
def test_order_rate_limit_wait_longer_than_cap_raises_and_sends_nothing() -> None:
    route = respx.post(f"{BASE}/PlaceOrder").respond(200, json=ORDER_ACK)
    client = TradeSmart(
        "TEST123", access_token="t", config=ClientConfig(order_rate_limit_max_wait=0)
    )
    # freeze the limiter clock: 11 calls must land in one 1 s window however slow the host is
    limiter = client._transport._limiter.get("place_order")
    assert limiter is not None
    limiter._clock = lambda: 1000.0
    for _ in range(10):
        client.place_order(**PLACE_KW)
    with pytest.raises(RateLimitError) as info:
        client.place_order(**PLACE_KW)
    assert info.value.retry_after is not None and info.value.retry_after > 0
    assert route.call_count == 10


@respx.mock
async def test_async_order_rate_limit_wait_longer_than_cap_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A frozen clock: on a slow machine the 10 orders can take longer than the 1 s window,
    # and the 11th would then need less than the 0.05 s cap (the test used to be flaky).
    class FrozenClockLimiter(ratelimit.RateLimiter):
        def __init__(self, limits: Any, **_: Any) -> None:
            super().__init__(limits, clock=lambda: 1000.0)

    monkeypatch.setattr(ratelimit, "RateLimiter", FrozenClockLimiter)
    route = respx.post(f"{BASE}/PlaceOrder").respond(200, json=ORDER_ACK)
    config = ClientConfig(order_rate_limit_max_wait=0.05)
    async with AsyncTradeSmart("TEST123", access_token="t", config=config) as client:
        for _ in range(10):
            await client.place_order(**PLACE_KW)
        with pytest.raises(RateLimitError):
            await client.place_order(**PLACE_KW)
    assert route.call_count == 10


# --- b. state-changing calls wait at most connect_timeout for a pool slot ---------------


@respx.mock
def test_order_pool_timeout_is_connect_timeout_reads_unchanged() -> None:
    order = respx.post(f"{BASE}/PlaceOrder").respond(200, json=ORDER_ACK)
    read = respx.post(f"{BASE}/Limits").respond(200, json={"stat": "Ok"})
    config = ClientConfig(connect_timeout=2.0, read_timeout=9.0)
    client = TradeSmart("TEST123", access_token="t", config=config)
    client.place_order(**PLACE_KW)
    client.margins()
    order_timeout = order.calls.last.request.extensions["timeout"]
    assert order_timeout["pool"] == 2.0 and order_timeout["read"] == 9.0
    assert order_timeout["connect"] == 2.0
    assert read.calls.last.request.extensions["timeout"]["pool"] == 9.0


@respx.mock
async def test_async_order_pool_timeout_is_connect_timeout() -> None:
    order = respx.post(f"{BASE}/CancelOrder").respond(200, json=ORDER_ACK)
    config = ClientConfig(connect_timeout=2.0, read_timeout=9.0)
    async with AsyncTradeSmart("TEST123", access_token="t", config=config) as client:
        await client.cancel_order("26110700000001")
    assert order.calls.last.request.extensions["timeout"]["pool"] == 2.0


@respx.mock
def test_order_pool_timeout_stays_definitive() -> None:
    route = respx.post(f"{BASE}/PlaceOrder").mock(side_effect=httpx.PoolTimeout("busy"))
    client = TradeSmart("TEST123", access_token="t")
    with pytest.raises(Exception) as info:
        client.place_order(**PLACE_KW)
    assert not isinstance(info.value, OrderStatusUnknownError)
    assert route.call_count == 1


# --- c. 5xx with JSON stat Not_Ok on a state-changing call is a rejection ---------------


@respx.mock
def test_order_5xx_json_not_ok_is_order_rejected_with_status() -> None:
    route = respx.post(f"{BASE}/ModifyOrder").respond(
        503, json={"stat": "Not_Ok", "emsg": "Service unavailable"}
    )
    client = TradeSmart("TEST123", access_token="t")
    with pytest.raises(OrderRejectedError) as info:
        client.modify_order(
            "1", exchange="NSE", tradingsymbol="INFY-EQ", quantity=1, price=1,
            order_type="LMT", product="C",
        )  # fmt: skip
    assert info.value.http_status == 503 and info.value.emsg == "Service unavailable"
    assert not isinstance(info.value, ServerError)
    assert route.call_count == 1


@respx.mock
def test_read_5xx_json_not_ok_still_server_error() -> None:
    respx.post(f"{BASE}/Limits").respond(500, json={"stat": "Not_Ok", "emsg": "boom"})
    client = TradeSmart("TEST123", access_token="t", config=ClientConfig(max_retries=0))
    with pytest.raises(ServerError):
        client.margins()


# --- e. every error body is redacted --------------------------------------------------

SECRET = "SECRET-XYZ-9"


class _Row(TradeSmartModel):
    pass


def test_parse_list_error_body_is_redacted() -> None:
    with pytest.raises(ResponseParseError) as info:
        parse_list(_Row, "OrderBook")({"stat": "Ok", "access_token": SECRET})
    assert SECRET not in info.value.body and "access_token" in info.value.body


@respx.mock
def test_values_parser_error_body_is_redacted() -> None:
    respx.post(f"{BASE}/SearchScrip").respond(200, json={"stat": "Ok", "susertoken": SECRET})
    client = TradeSmart("TEST123", access_token="t")
    with pytest.raises(ResponseParseError) as info:
        client.search_instruments("INFY")
    assert SECRET not in info.value.body and "susertoken" in info.value.body


@pytest.mark.parametrize(
    "body",
    [{"access_token": SECRET}, ['{"access_token":"' + SECRET + '"'], [f"Bearer {SECRET}"]],
    ids=["not-a-list", "bad-json-element", "non-object-element"],
)
def test_eod_parser_error_body_is_redacted(body: Any) -> None:
    with pytest.raises(ResponseParseError) as info:
        _parse_eod(body)
    assert SECRET not in info.value.body


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text=f"oops access_token={SECRET}"),
        httpx.Response(403, json={"stat": "Not_Ok", "access_token": SECRET}),
    ],
    ids=["text", "json"],
)
@respx.mock
def test_script_master_status_error_raw_is_redacted(tmp_path: Any, response: Any) -> None:
    respx.get(SCRIPT_URL).mock(return_value=response)
    client = TradeSmart("TEST123", access_token="t")
    with pytest.raises(Exception) as info:
        client.download_instruments("NSE", tmp_path)
    assert SECRET not in str(info.value.raw)  # type: ignore[attr-defined]
    assert "access_token" in str(info.value.raw)  # type: ignore[attr-defined]


# --- f. a single instrument is one instrument -----------------------------------------


def test_format_keys_accepts_a_single_instrument() -> None:
    assert format_keys("NSE|22") == ["NSE|22"]
    assert format_keys(("NSE", "22")) == ["NSE|22"]
    assert format_keys((Exchange.BSE, "508123")) == ["BSE|508123"]
    assert format_keys(("NSE|22", "NSE|23")) == ["NSE|22", "NSE|23"]
    assert format_keys([("NSE", "22")]) == ["NSE|22"]


def _ws_config(srv: MockWsServer) -> ClientConfig:
    return ClientConfig(ws_url=srv.url, heartbeat_interval=5, ws_auth_timeout=1.0)


@pytest.mark.parametrize("instrument", ["NSE|22", ("NSE", "22")], ids=["str", "tuple"])
def test_sync_stream_single_instrument(threaded_server: Any, instrument: Any) -> None:
    s = TradeSmartStream("TEST123", "tok", config=_ws_config(threaded_server))
    s.start()
    try:
        s.subscribe_touchline(instrument)
        s.subscribe_depth(instrument)
        s.unsubscribe_depth(instrument)
        s.unsubscribe_touchline(instrument)
        wait_until_sync(
            lambda: [m["t"] for m in threaded_server.received if m["t"] != "h"][-1:] == ["u"]
        )
    finally:
        s.stop()
    sent = {m["t"]: m["k"] for m in threaded_server.received if m["t"] in ("t", "d", "ud", "u")}
    assert sent == {"t": "NSE|22", "d": "NSE|22", "ud": "NSE|22", "u": "NSE|22"}


@pytest.mark.parametrize("instrument", ["NSE|22", ("NSE", "22")], ids=["str", "tuple"])
async def test_async_stream_single_instrument(instrument: Any) -> None:
    cfg = ClientConfig(heartbeat_interval=5, ws_auth_timeout=1.0)
    async with (
        MockWsServer() as srv,
        AsyncTradeSmartStream("TEST123", "tok", config=replace(cfg, ws_url=srv.url)) as s,
    ):
        await s.subscribe_touchline(instrument)
        await s.subscribe_depth(instrument)
        await s.unsubscribe_depth(instrument)
        await s.unsubscribe_touchline(instrument)
        await wait_until(lambda: any(m["t"] == "u" for m in srv.received))
    sent = {m["t"]: m["k"] for m in srv.received if m["t"] in ("t", "d", "ud", "u")}
    assert sent == {"t": "NSE|22", "d": "NSE|22", "ud": "NSE|22", "u": "NSE|22"}


# --- g. every state-changing method x ambiguous failure -> unknown, one request ---------

_METHODS: list[tuple[str, str, Callable[[Any], Any]]] = [
    ("place_order", "PlaceOrder", lambda c: c.place_order(**PLACE_KW)),
    (
        "modify_order",
        "ModifyOrder",
        lambda c: c.modify_order(
            "1",
            exchange="NSE",
            tradingsymbol="INFY-EQ",
            quantity=1,
            price=1,
            order_type="LMT",
            product="C",
        ),  # fmt: skip
    ),
    ("cancel_order", "CancelOrder", lambda c: c.cancel_order("1")),
    ("exit_order", "ExitSNOOrder", lambda c: c.exit_order("1", "B")),
]
_FAILURES: list[tuple[str, Callable[[], dict[str, Any]]]] = [
    ("read-timeout", lambda: {"side_effect": httpx.ReadTimeout("slow")}),
    ("remote-protocol", lambda: {"side_effect": httpx.RemoteProtocolError("disconnected")}),
    (
        "504-html",
        lambda: {"return_value": httpx.Response(504, text="<html>Gateway Timeout</html>")},
    ),
]
_MATRIX = [
    pytest.param(endpoint, call, mock, id=f"{name}-{failure}")
    for name, endpoint, call in _METHODS
    for failure, mock in _FAILURES
]


@pytest.mark.parametrize("endpoint,call,mock", _MATRIX)
@respx.mock
def test_state_changing_method_ambiguous_failure_is_unknown(endpoint, call, mock) -> None:
    route = respx.post(f"{BASE}/{endpoint}").mock(**mock())
    client = TradeSmart("TEST123", access_token="t", config=ClientConfig(backoff_base=0.001))
    with pytest.raises(OrderStatusUnknownError):
        call(client)
    assert route.call_count == 1 and len(respx.calls) == 1


@pytest.mark.parametrize("endpoint,call,mock", _MATRIX)
@respx.mock
async def test_async_state_changing_method_ambiguous_failure_is_unknown(
    endpoint, call, mock
) -> None:
    route = respx.post(f"{BASE}/{endpoint}").mock(**mock())
    config = ClientConfig(backoff_base=0.001)
    async with AsyncTradeSmart("TEST123", access_token="t", config=config) as client:
        with pytest.raises(OrderStatusUnknownError):
            await call(client)
    assert route.call_count == 1 and len(respx.calls) == 1


@respx.mock
def test_client_order_rate_follows_order_rate_limit_per_second() -> None:
    route = respx.post(f"{BASE}/PlaceOrder").respond(200, json=ORDER_ACK)
    config = ClientConfig(
        order_rate_limit_per_second=30, order_rate_limit_max_wait=0, algo_id="ALGO1"
    )
    client = TradeSmart("TEST123", access_token="t", config=config)
    limiter = client._transport._limiter.get("place_order")
    assert limiter is not None
    limiter._clock = lambda: 1000.0
    for _ in range(30):
        client.place_order(**PLACE_KW)
    with pytest.raises(RateLimitError):
        client.place_order(**PLACE_KW)
    assert route.call_count == 30
