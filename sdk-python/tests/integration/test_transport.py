import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from tests.conftest import fixture
from tradesmart import AsyncTradeSmart, Exchange, TradeSmart
from tradesmart._client_core import build_raw_call
from tradesmart._transport.async_http import AsyncTransport
from tradesmart._transport.sync_http import SyncTransport
from tradesmart.config import ClientConfig
from tradesmart.endpoints.base import Call, EndpointSpec, parse_list
from tradesmart.errors import (
    ConfigurationError,
    NetworkError,
    OrderRejectedError,
    OrderStatusUnknownError,
    RateLimitError,
    ResponseParseError,
)
from tradesmart.models.base import TradeSmartModel

BASE = "https://v2api.tradesmartonline.in/NorenWClientAPIv2"
READ = EndpointSpec(name="Limits", parse=lambda b: b)
ORDER = EndpointSpec(
    name="PlaceOrder", parse=lambda b: b, state_changing=True, rate_group="place_order"
)


@respx.mock
def test_sends_jdata_and_bearer():
    route = respx.post(f"{BASE}/Limits").respond(200, json={"stat": "Ok"})
    t = SyncTransport(ClientConfig(), lambda: "test-access-token")
    assert t.execute(Call(READ, {"uid": "TEST123"})) == {"stat": "Ok"}
    req = route.calls.last.request
    assert req.content == b'jData={"uid":"TEST123"}'
    assert req.headers["Authorization"] == "Bearer test-access-token"
    assert "content-type" not in req.headers


@respx.mock
def test_unauthenticated_spec_has_no_auth_header():
    route = respx.post(f"{BASE}/GenAcsTok").respond(200, json={"stat": "Ok"})
    SyncTransport(ClientConfig(), lambda: pytest.fail("token requested")).execute(
        Call(
            EndpointSpec(
                name="GenAcsTok", parse=lambda b: b, authenticated=False, state_changing=True
            ),
            {},
        )
    )
    assert "Authorization" not in route.calls.last.request.headers


@respx.mock
def test_read_retries_on_502_then_succeeds():
    route = respx.post(f"{BASE}/Limits").mock(
        side_effect=[httpx.Response(502, text="bad"), httpx.Response(200, json={"stat": "Ok"})]
    )
    t = SyncTransport(ClientConfig(), lambda: "tok", sleep=lambda s: None)
    assert t.execute(Call(READ, {})) == {"stat": "Ok"} and route.call_count == 2


@respx.mock
def test_429_retry_after_header_drives_delay():
    sleeps: list[float] = []
    respx.post(f"{BASE}/Limits").mock(
        side_effect=[
            httpx.Response(429, text="slow", headers={"Retry-After": "3"}),
            httpx.Response(200, json={"stat": "Ok"}),
        ]
    )
    t = SyncTransport(ClientConfig(), lambda: "tok", sleep=sleeps.append)
    t.execute(Call(READ, {}))
    assert sleeps == [3.0]


@respx.mock
def test_429_exhausted_exposes_retry_after():
    respx.post(f"{BASE}/Limits").respond(429, text="slow", headers={"Retry-After": "2"})
    t = SyncTransport(ClientConfig(max_retries=0), lambda: "tok")
    with pytest.raises(RateLimitError) as info:
        t.execute(Call(READ, {}))
    assert info.value.retry_after == 2.0


@respx.mock
def test_state_changing_read_timeout_is_unknown_and_not_retried():
    route = respx.post(f"{BASE}/PlaceOrder").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(OrderStatusUnknownError):
        SyncTransport(ClientConfig(), lambda: "tok", sleep=lambda s: None).execute(Call(ORDER, {}))
    assert route.call_count == 1


@respx.mock
def test_state_changing_5xx_not_retried():
    route = respx.post(f"{BASE}/PlaceOrder").respond(503, text="down")
    with pytest.raises(OrderStatusUnknownError):
        SyncTransport(ClientConfig(), lambda: "tok", sleep=lambda s: None).execute(Call(ORDER, {}))
    assert route.call_count == 1


@respx.mock
def test_connect_error_maps_to_network_error():
    respx.post(f"{BASE}/Limits").mock(side_effect=httpx.ConnectError("dns"))
    with pytest.raises(NetworkError):
        SyncTransport(ClientConfig(max_retries=0), lambda: "tok").execute(Call(READ, {}))


@respx.mock
def test_state_changing_connect_error_is_network_error_not_unknown():
    respx.post(f"{BASE}/PlaceOrder").mock(side_effect=httpx.ConnectError("dns"))
    with pytest.raises(NetworkError):
        SyncTransport(ClientConfig(), lambda: "tok").execute(Call(ORDER, {}))


def test_parse_list_rejects_non_list():
    class Row(TradeSmartModel):
        pass

    with pytest.raises(ResponseParseError):
        parse_list(Row, "X")({"stat": "Ok"})


@respx.mock
async def test_async_transport_same_contract():
    respx.post(f"{BASE}/Limits").respond(200, json={"stat": "Ok"})
    t = AsyncTransport(ClientConfig(), lambda: "tok")
    assert await t.execute(Call(READ, {})) == {"stat": "Ok"}
    await t.aclose()


@respx.mock
async def test_async_transport_rate_limit_waits_use_the_injected_sleep():
    respx.post(f"{BASE}/GetQuotes").respond(200, json={"stat": "Ok"})
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    t = AsyncTransport(ClientConfig(), lambda: "tok", sleep=fake_sleep)
    limiter = t._limiter.get("quotes")
    assert limiter is not None
    now = [0.0]
    limiter._clock = lambda: now[0]
    limiter._limits = ((1, 1.0),)
    quotes = EndpointSpec(name="GetQuotes", parse=lambda b: b, rate_group="quotes")
    await t.execute(Call(quotes, {}))
    # The fake clock only moves in fake_sleep: a real sleep would wait forever.
    await asyncio.wait_for(t.execute(Call(quotes, {})), timeout=2.0)
    assert slept == [pytest.approx(1.0)]
    await t.aclose()


@respx.mock
async def test_async_state_changing_timeout_unknown_not_retried():
    route = respx.post(f"{BASE}/PlaceOrder").mock(side_effect=httpx.ReadTimeout("slow"))
    t = AsyncTransport(ClientConfig(), lambda: "tok")
    with pytest.raises(OrderStatusUnknownError):
        await t.execute(Call(ORDER, {}))
    assert route.call_count == 1
    await t.aclose()


# --- ambiguous state-changing responses (final review C1) ----------------------

_AMBIGUOUS = [  # builders: a fresh response/exception per test
    pytest.param(
        lambda: {"return_value": httpx.Response(504, text="<html>Gateway Timeout</html>")},
        id="504-html",
    ),
    pytest.param(lambda: {"return_value": httpx.Response(200, text="")}, id="empty-200"),
    pytest.param(lambda: {"side_effect": httpx.DecodingError("bad gzip")}, id="decoding-error"),
    pytest.param(lambda: {"side_effect": httpx.TooManyRedirects("loop")}, id="too-many-redirects"),
]


@pytest.mark.parametrize("mock", _AMBIGUOUS)
@respx.mock
def test_state_changing_ambiguous_response_is_unknown(mock):
    route = respx.post(f"{BASE}/PlaceOrder").mock(**mock())
    with pytest.raises(OrderStatusUnknownError) as info:
        SyncTransport(ClientConfig(), lambda: "tok", sleep=lambda s: None).execute(Call(ORDER, {}))
    assert route.call_count == 1
    assert info.value.endpoint == "PlaceOrder"
    assert info.value.__cause__ is None and info.value.__suppress_context__  # no raw body/headers


@pytest.mark.parametrize("mock", _AMBIGUOUS)
@respx.mock
async def test_async_state_changing_ambiguous_response_is_unknown(mock):
    route = respx.post(f"{BASE}/PlaceOrder").mock(**mock())
    t = AsyncTransport(ClientConfig(), lambda: "tok")
    with pytest.raises(OrderStatusUnknownError) as info:
        await t.execute(Call(ORDER, {}))
    assert route.call_count == 1
    assert info.value.endpoint == "PlaceOrder"
    assert info.value.__cause__ is None and info.value.__suppress_context__  # no raw body/headers
    await t.aclose()


@respx.mock
def test_state_changing_504_html_keeps_status_and_body():
    respx.post(f"{BASE}/PlaceOrder").respond(504, text="<html>Gateway Timeout</html>")
    with pytest.raises(OrderStatusUnknownError) as info:
        SyncTransport(ClientConfig(), lambda: "tok").execute(Call(ORDER, {}))
    assert info.value.http_status == 504 and info.value.body == "<html>Gateway Timeout</html>"


@respx.mock
def test_state_changing_5xx_json_not_ok_is_definitive_order_rejected():
    route = respx.post(f"{BASE}/PlaceOrder").respond(
        503, json={"stat": "Not_Ok", "emsg": "Service unavailable"}
    )
    with pytest.raises(OrderRejectedError):
        SyncTransport(ClientConfig(), lambda: "tok", sleep=lambda s: None).execute(Call(ORDER, {}))
    assert route.call_count == 1


@respx.mock
async def test_async_state_changing_5xx_json_not_ok_is_definitive_order_rejected():
    route = respx.post(f"{BASE}/PlaceOrder").respond(
        503, json={"stat": "Not_Ok", "emsg": "Service unavailable"}
    )
    t = AsyncTransport(ClientConfig(), lambda: "tok")
    with pytest.raises(OrderRejectedError):
        await t.execute(Call(ORDER, {}))
    assert route.call_count == 1
    await t.aclose()


@pytest.mark.parametrize(
    "exc",
    [httpx.DecodingError("bad gzip"), httpx.TooManyRedirects("loop")],
    ids=["decoding", "redirects"],
)
@respx.mock
def test_read_request_error_is_network_error(exc):
    respx.post(f"{BASE}/Limits").mock(side_effect=exc)
    with pytest.raises(NetworkError):
        SyncTransport(ClientConfig(max_retries=0), lambda: "tok").execute(Call(READ, {}))


@pytest.mark.parametrize(
    "exc",
    [httpx.DecodingError("bad gzip"), httpx.TooManyRedirects("loop")],
    ids=["decoding", "redirects"],
)
@respx.mock
async def test_async_read_request_error_is_network_error(exc):
    respx.post(f"{BASE}/Limits").mock(side_effect=exc)
    t = AsyncTransport(ClientConfig(max_retries=0), lambda: "tok")
    with pytest.raises(NetworkError):
        await t.execute(Call(READ, {}))
    await t.aclose()


@respx.mock
def test_raw_request_place_order_timeout_not_retried(client):
    route = respx.post(f"{BASE}/PlaceOrder").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(OrderStatusUnknownError):
        client.request("PlaceOrder", {"tsym": "TEST-EQ"}, state_changing=False)
    assert route.call_count == 1


CFG = ClientConfig(keepalive_expiry=30, max_connections=4, max_keepalive_connections=2)


def _assert_limits(pool: Any) -> None:
    assert pool._max_connections == 4
    assert pool._max_keepalive_connections == 2
    assert pool._keepalive_expiry == 30


def test_created_client_uses_pool_limits() -> None:
    client = TradeSmart("TEST123", access_token="t", config=CFG)
    _assert_limits(client._transport.http_client._transport._pool)
    client.close()


async def test_created_async_client_uses_pool_limits() -> None:
    client = AsyncTradeSmart("TEST123", access_token="t", config=CFG)
    _assert_limits(client._transport.http_client._transport._pool)
    await client.aclose()


def test_injected_client_untouched() -> None:
    own = httpx.Client(limits=httpx.Limits(max_connections=7, max_keepalive_connections=3))
    client = TradeSmart("TEST123", access_token="t", config=CFG, http_client=own)
    assert client._transport.http_client is own
    assert own._transport._pool._max_connections == 7  # type: ignore[attr-defined]
    assert own._transport._pool._max_keepalive_connections == 3  # type: ignore[attr-defined]
    own.close()


@respx.mock
def test_no_json_reparse_when_debug_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from tradesmart._transport import sync_http

    calls = []
    real = json.loads

    def counting(*a: Any, **k: Any) -> Any:
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(sync_http, "json", SimpleNamespace(loads=counting))
    respx.post(f"{BASE}/GetQuotes").respond(200, json=fixture("get_quotes_success"))
    client = TradeSmart("TEST123", access_token="t")
    with caplog.at_level(logging.INFO, logger="tradesmart"):
        client.quote(Exchange.NSE, "TCS-EQ")
    assert calls == []


@respx.mock
def test_stale_connection_order_unknown_read_retried() -> None:
    order = respx.post(f"{BASE}/PlaceOrder").mock(
        side_effect=httpx.RemoteProtocolError("Server disconnected")
    )
    client = TradeSmart("TEST123", access_token="t", config=ClientConfig(backoff_base=0.001))
    with pytest.raises(OrderStatusUnknownError):
        client.place_order(
            exchange="NSE",
            tradingsymbol="TCS-EQ",
            transaction_type="BUY",
            quantity=1,
            product="I",
            order_type="MKT",
        )
    assert order.call_count == 1
    quotes = respx.post(f"{BASE}/GetQuotes").mock(
        side_effect=[
            httpx.RemoteProtocolError("Server disconnected"),
            httpx.RemoteProtocolError("Server disconnected"),
            httpx.Response(200, json=fixture("get_quotes_success")),
        ]
    )
    assert client.quote(Exchange.NSE, "TCS-EQ").last_price is not None
    assert quotes.call_count == 3


# --- request() endpoint-name normalisation (review 2026-11-10 C1) ---------------


@pytest.mark.parametrize("name", ["/PlaceOrder", "placeorder", " PlaceOrder ", "PLACEORDER"])
@respx.mock
def test_raw_request_normalises_order_endpoint_name(client, name):
    route = respx.post(f"{BASE}/PlaceOrder").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(OrderStatusUnknownError):
        client.request(name, {"tsym": "TEST-EQ"}, state_changing=False)
    assert route.call_count == 1
    assert len(respx.calls) == 1
    assert respx.calls.last.request.url == f"{BASE}/PlaceOrder"


@pytest.mark.parametrize("name", ["/PlaceOrder", "placeorder", " PlaceOrder ", "PLACEORDER"])
@respx.mock
async def test_async_raw_request_normalises_order_endpoint_name(async_client, name):
    route = respx.post(f"{BASE}/PlaceOrder").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(OrderStatusUnknownError):
        await async_client.request(name, {"tsym": "TEST-EQ"}, state_changing=False)
    assert route.call_count == 1
    assert len(respx.calls) == 1
    await async_client.aclose()


@pytest.mark.parametrize("name", ["Place/Order", "", "../x", "Place Order", "PlaceOrder\n1"])
@respx.mock
def test_raw_request_rejects_invalid_endpoint_name(client, name):
    with pytest.raises(ConfigurationError) as info:
        client.request(name, {}, state_changing=False)
    assert "letters" in str(info.value)
    assert len(respx.calls) == 0


def test_raw_request_lowercase_quotes_uses_rate_group():
    call = build_raw_call("TEST123", "getquotes", {}, state_changing=False, expect="object")
    assert call.spec.name == "GetQuotes" and call.spec.rate_group == "quotes"
    assert call.spec.state_changing is False
    unknown = build_raw_call("TEST123", "/SomeNewList/", {}, state_changing=False, expect="list")
    assert unknown.spec.name == "SomeNewList" and unknown.spec.rate_group is None
