"""Transport robustness (review 2026-11-10): unexpected exceptions, malformed list items,
clean exception chains and redirects. Sync and async clients behave the same."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tradesmart import (
    AsyncTradeSmart,
    ClientConfig,
    NetworkError,
    OrderStatusUnknownError,
    RateLimitError,
    ResponseParseError,
    TradeSmart,
    TradeSmartError,
)

PLACE_KW: dict[str, Any] = dict(
    exchange="NSE",
    tradingsymbol="INFY-EQ",
    quantity=1,
    transaction_type="B",
    product="C",
    order_type="LMT",
    price="1234.50",
)
FAST = ClientConfig(backoff_base=0.001, backoff_cap=0.001)
ACCEPTED_ON_500 = b'{"stat":"Ok","norenordno":"123"}'


def _orders_log(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "tradesmart.orders" and r.levelno == logging.INFO
    ]


@pytest.fixture(autouse=True)
def _capture(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="tradesmart")


class _Counter:
    """MockTransport handler that counts requests per host and path."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.seen: list[str] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(f"{request.url.host}{request.url.path}")
        return self._respond(request)


def _raise_runtime(request: httpx.Request) -> httpx.Response:
    raise RuntimeError("Event loop is closed https://secret.example/?token=abc")


def _raise_for_status(response: httpx.Response) -> None:
    response.raise_for_status()


async def _araise_for_status(response: httpx.Response) -> None:
    response.raise_for_status()


def _sync(handler: Any, **kwargs: Any) -> TradeSmart:
    http = httpx.Client(transport=httpx.MockTransport(handler), **kwargs)
    return TradeSmart("TEST123", access_token="test-access-token", config=FAST, http_client=http)


def _async(handler: Any, **kwargs: Any) -> AsyncTradeSmart:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
    return AsyncTradeSmart(
        "TEST123", access_token="test-access-token", config=FAST, http_client=http
    )


def _assert_clean_chain(exc: BaseException) -> None:
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True


# -- I1: any exception from the post is mapped ------------------------------------------


def test_order_response_hook_error_is_unknown_outcome(caplog: pytest.LogCaptureFixture) -> None:
    client = _sync(
        lambda r: httpx.Response(500, content=ACCEPTED_ON_500),
        event_hooks={"response": [_raise_for_status]},
    )
    with pytest.raises(OrderStatusUnknownError) as info:
        client.place_order(**PLACE_KW)
    assert "HTTPStatusError during the request; check orders() before retrying" in str(info.value)
    _assert_clean_chain(info.value)
    assert [m.split(" in ")[0] for m in _orders_log(caplog)] == ["PlaceOrder unknown order_id=-"]


async def test_async_order_response_hook_error_is_unknown_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _async(
        lambda r: httpx.Response(500, content=ACCEPTED_ON_500),
        event_hooks={"response": [_araise_for_status]},
    )
    with pytest.raises(OrderStatusUnknownError) as info:
        await client.place_order(**PLACE_KW)
    assert "HTTPStatusError during the request; check orders() before retrying" in str(info.value)
    _assert_clean_chain(info.value)
    assert [m.split(" in ")[0] for m in _orders_log(caplog)] == ["PlaceOrder unknown order_id=-"]


def test_order_transport_runtime_error_is_unknown_outcome(caplog: pytest.LogCaptureFixture) -> None:
    client = _sync(_raise_runtime)
    with pytest.raises(OrderStatusUnknownError) as info:
        client.place_order(**PLACE_KW)
    assert "RuntimeError during the request" in str(info.value)
    assert "secret.example" not in str(info.value) and "token=abc" not in str(info.value)
    _assert_clean_chain(info.value)
    assert _orders_log(caplog)[0].startswith("PlaceOrder unknown ")


async def test_async_order_transport_runtime_error_is_unknown_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _async(_raise_runtime)
    with pytest.raises(OrderStatusUnknownError) as info:
        await client.place_order(**PLACE_KW)
    assert "RuntimeError during the request" in str(info.value)
    _assert_clean_chain(info.value)
    assert _orders_log(caplog)[0].startswith("PlaceOrder unknown ")


def test_read_transport_runtime_error_is_network_error() -> None:
    client = _sync(_raise_runtime)
    with pytest.raises(NetworkError) as info:
        client.profile()
    assert str(info.value) == "UserDetails: unexpected error (RuntimeError)"
    _assert_clean_chain(info.value)


async def test_async_read_response_hook_error_is_network_error() -> None:
    client = _async(
        lambda r: httpx.Response(500, json={"stat": "Ok"}),
        event_hooks={"response": [_araise_for_status]},
    )
    with pytest.raises(NetworkError) as info:
        await client.profile()
    assert str(info.value) == "UserDetails: unexpected error (HTTPStatusError)"
    _assert_clean_chain(info.value)


# -- I2: list items that are not JSON objects ----------------------------------------------


def test_order_book_with_a_non_object_item_is_a_parse_error() -> None:
    client = _sync(lambda r: httpx.Response(200, json=["x"]))
    with pytest.raises(ResponseParseError) as info:
        client.orders()
    assert info.value.message == "Could not parse OrderBook response (expected a JSON object)"
    assert info.value.body == '"x"'


async def test_async_order_book_with_a_non_object_item_is_a_parse_error() -> None:
    client = _async(lambda r: httpx.Response(200, json=["x"]))
    with pytest.raises(ResponseParseError, match=r"expected a JSON object"):
        await client.orders()


def test_search_with_a_non_object_value_is_a_parse_error() -> None:
    client = _sync(lambda r: httpx.Response(200, json={"stat": "Ok", "values": [1]}))
    with pytest.raises(ResponseParseError) as info:
        client.search_instruments("INFY")
    assert info.value.message == "Could not parse SearchScrip response (expected a JSON object)"
    assert isinstance(info.value, TradeSmartError)


async def test_async_search_with_a_non_object_value_is_a_parse_error() -> None:
    client = _async(lambda r: httpx.Response(200, json={"stat": "Ok", "values": [1]}))
    expected = r"SearchScrip response \(expected a JSON object\)"
    with pytest.raises(ResponseParseError, match=expected):
        await client.search_instruments("INFY")


# -- M1: no exception chain that can hold the raw body or the request headers --------------


def _read_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


def _connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


def test_order_timeout_has_no_cause_holding_the_request() -> None:
    with pytest.raises(OrderStatusUnknownError) as info:
        _sync(_read_timeout).place_order(**PLACE_KW)
    _assert_clean_chain(info.value)


async def test_async_order_timeout_has_no_cause_holding_the_request() -> None:
    with pytest.raises(OrderStatusUnknownError) as info:
        await _async(_read_timeout).place_order(**PLACE_KW)
    _assert_clean_chain(info.value)


def test_read_connect_error_has_no_cause_holding_the_request() -> None:
    with pytest.raises(NetworkError) as info:
        _sync(_connect_error).profile()
    _assert_clean_chain(info.value)


async def test_async_read_connect_error_has_no_cause_holding_the_request() -> None:
    with pytest.raises(NetworkError) as info:
        await _async(_connect_error).profile()
    _assert_clean_chain(info.value)


def test_order_non_json_body_has_no_cause_holding_the_body() -> None:
    body = b"<html>Authorization: Bearer abc.def</html>"
    with pytest.raises(OrderStatusUnknownError) as info:
        _sync(lambda r: httpx.Response(502, content=body)).place_order(**PLACE_KW)
    _assert_clean_chain(info.value)
    assert "abc.def" not in (info.value.body or "")


async def test_async_order_non_json_body_has_no_cause_holding_the_body() -> None:
    body = b"<html>Authorization: Bearer abc.def</html>"
    with pytest.raises(OrderStatusUnknownError) as info:
        await _async(lambda r: httpx.Response(502, content=body)).place_order(**PLACE_KW)
    _assert_clean_chain(info.value)
    assert "abc.def" not in (info.value.body or "")


# -- BaseExceptions (not Exception) are never caught by the catch-all -----------------------


def _raiser(exc: BaseException) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        raise exc

    return respond


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("call", ["order", "read"])
def test_keyboard_interrupt_and_system_exit_pass_through(
    exc_type: type[BaseException], call: str
) -> None:
    original = exc_type()
    client = _sync(_raiser(original))
    with pytest.raises(exc_type) as info:
        client.place_order(**PLACE_KW) if call == "order" else client.profile()
    assert info.value is original


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("call", ["order", "read"])
async def test_async_keyboard_interrupt_and_system_exit_pass_through(
    exc_type: type[BaseException], call: str
) -> None:
    original = exc_type()
    client = _async(_raiser(original))
    with pytest.raises(exc_type) as info:
        await (client.place_order(**PLACE_KW) if call == "order" else client.profile())
    assert info.value is original


# -- reads: hook/transport errors are not retried; an SDK error passes through unchanged ----


class _HookError(TradeSmartError):
    """An SDK error raised by the caller's own event hook or transport."""


def _counting_raiser(exc: BaseException) -> _Counter:
    return _Counter(_raiser(exc))


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(lambda: _HookError("from my hook"), id="TradeSmartError"),
        pytest.param(
            lambda: RateLimitError("hook rate limit", endpoint="Hook", http_status=None),
            id="RateLimitError",
        ),
        pytest.param(lambda: NetworkError("hook network"), id="NetworkError"),
    ],
)
def test_read_sdk_error_from_a_transport_passes_through_unretried(error: Any) -> None:
    original = error()
    counter = _counting_raiser(original)
    with pytest.raises(TradeSmartError) as info:
        _sync(counter).profile()
    assert info.value is original
    assert len(counter.seen) == 1


async def test_async_read_sdk_error_from_a_response_hook_passes_through_unretried() -> None:
    original = RateLimitError("hook rate limit", endpoint="Hook", http_status=None)
    counter = _Counter(lambda r: httpx.Response(200, json={"stat": "Ok"}))

    async def hook(response: httpx.Response) -> None:
        raise original

    client = _async(counter, event_hooks={"response": [hook]})
    with pytest.raises(RateLimitError) as info:
        await client.profile()
    assert info.value is original
    assert len(counter.seen) == 1


def test_read_unexpected_error_is_a_non_retried_network_error() -> None:
    counter = _counting_raiser(RuntimeError("boom"))
    with pytest.raises(NetworkError, match=r"UserDetails: unexpected error \(RuntimeError\)"):
        _sync(counter).profile()
    assert len(counter.seen) == 1


async def test_async_read_response_hook_error_is_a_non_retried_network_error() -> None:
    counter = _Counter(lambda r: httpx.Response(404, json={"stat": "Ok"}))
    client = _async(counter, event_hooks={"response": [_araise_for_status]})
    with pytest.raises(NetworkError, match=r"unexpected error \(HTTPStatusError\)"):
        await client.profile()
    assert len(counter.seen) == 1


def test_order_sdk_error_from_a_hook_is_still_unknown() -> None:
    counter = _counting_raiser(_HookError("from my hook"))
    with pytest.raises(OrderStatusUnknownError, match="_HookError during the request"):
        _sync(counter).place_order(**PLACE_KW)
    assert len(counter.seen) == 1


async def test_async_order_sdk_error_from_a_hook_is_still_unknown() -> None:
    counter = _counting_raiser(_HookError("from my hook"))
    with pytest.raises(OrderStatusUnknownError, match="_HookError during the request"):
        await _async(counter).place_order(**PLACE_KW)
    assert len(counter.seen) == 1


# -- spec M2: a state-changing raw request never gets a parse error for an Ok body ----------


def test_state_changing_list_request_with_an_ok_object_is_unknown() -> None:
    client = _sync(lambda r: httpx.Response(200, json={"stat": "Ok", "norenordno": "1"}))
    with pytest.raises(OrderStatusUnknownError):
        client.request("PlaceMultiOrder", {}, state_changing=True, expect="list")


async def test_async_state_changing_list_request_with_an_ok_object_is_unknown() -> None:
    client = _async(lambda r: httpx.Response(200, json={"stat": "Ok"}))
    with pytest.raises(OrderStatusUnknownError):
        await client.request("SomeEndpoint", {}, state_changing=True, expect="list")


# -- spec I1: redirects are never followed ---------------------------------------------------


def _redirect(status: int) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "other.example":
            return httpx.Response(200, json={"stat": "Ok", "norenordno": "999"})
        return httpx.Response(
            status, headers={"Location": f"https://other.example{request.url.path}"}
        )

    return respond


@pytest.mark.parametrize("status", [307, 302])
@pytest.mark.parametrize("follow", [False, True])
def test_order_redirect_is_unknown_and_never_followed(status: int, follow: bool) -> None:
    counter = _Counter(_redirect(status))
    client = _sync(counter, follow_redirects=follow)
    with pytest.raises(OrderStatusUnknownError) as info:
        client.place_order(**PLACE_KW)
    assert info.value.http_status == status
    assert counter.seen == ["v2api.tradesmartonline.in/NorenWClientAPIv2/PlaceOrder"]


@pytest.mark.parametrize("status", [307, 302])
@pytest.mark.parametrize("follow", [False, True])
async def test_async_order_redirect_is_unknown_and_never_followed(
    status: int, follow: bool
) -> None:
    counter = _Counter(_redirect(status))
    client = _async(counter, follow_redirects=follow)
    with pytest.raises(OrderStatusUnknownError) as info:
        await client.place_order(**PLACE_KW)
    assert info.value.http_status == status
    assert counter.seen == ["v2api.tradesmartonline.in/NorenWClientAPIv2/PlaceOrder"]


@pytest.mark.parametrize("status", [307, 302])
@pytest.mark.parametrize("follow", [False, True])
def test_read_redirect_is_a_parse_error_and_never_followed(status: int, follow: bool) -> None:
    counter = _Counter(_redirect(status))
    client = _sync(counter, follow_redirects=follow)
    with pytest.raises(ResponseParseError) as info:
        client.profile()
    assert info.value.http_status == status
    assert counter.seen == ["v2api.tradesmartonline.in/NorenWClientAPIv2/UserDetails"]


@pytest.mark.parametrize("status", [307, 302])
@pytest.mark.parametrize("follow", [False, True])
async def test_async_read_redirect_is_a_parse_error_and_never_followed(
    status: int, follow: bool
) -> None:
    counter = _Counter(_redirect(status))
    client = _async(counter, follow_redirects=follow)
    with pytest.raises(ResponseParseError) as info:
        await client.profile()
    assert info.value.http_status == status
    assert counter.seen == ["v2api.tradesmartonline.in/NorenWClientAPIv2/UserDetails"]


def test_sdk_built_http_clients_do_not_follow_redirects() -> None:
    assert TradeSmart("TEST123", access_token="t")._transport.http_client.follow_redirects is False
    client = AsyncTradeSmart("TEST123", access_token="t")
    assert client._transport.http_client.follow_redirects is False
