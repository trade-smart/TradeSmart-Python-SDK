"""REST review round 1: definitive 'not sent' failures, token checks, deep JSON, clean
exception context, rate-limit endpoint names and strict read arguments. Sync and async."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from tests.conftest import fixture
from tradesmart import (
    AccessToken,
    AsyncTradeSmart,
    AuthenticationError,
    ClientConfig,
    ConfigurationError,
    NetworkError,
    OrderRejectedError,
    OrderStatusUnknownError,
    RateLimitError,
    RequestTimeoutError,
    RequestValidationError,
    ResponseParseError,
    TradeSmart,
)
from tradesmart._transport.async_http import AsyncTransport
from tradesmart._transport.sync_http import SyncTransport
from tradesmart.endpoints.market import to_epoch
from tradesmart.endpoints.orders import place_order_call
from tradesmart.models.base import IST

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
ACCESS = "test-access-token"
OK_ORDER = {"stat": "Ok", "norenordno": "123"}


@pytest.fixture(autouse=True)
def _capture(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="tradesmart")


def _order_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage().split(" in ")[0]
        for r in caplog.records
        if r.name == "tradesmart.orders" and r.levelno == logging.INFO
    ]


class _Recorder:
    """MockTransport handler that records requests and answers with a fixed response."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond or (lambda r: httpx.Response(200, json=OK_ORDER))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)


def _sync(rec: _Recorder, token: Any = ACCESS, **kw: Any) -> TradeSmart:
    http = httpx.Client(transport=httpx.MockTransport(rec))
    return TradeSmart(
        "TEST123", access_token=token, config=kw.pop("config", FAST), http_client=http
    )


def _async(rec: _Recorder, token: Any = ACCESS, **kw: Any) -> AsyncTradeSmart:
    http = httpx.AsyncClient(transport=httpx.MockTransport(rec))
    return AsyncTradeSmart(
        "TEST123", access_token=token, config=kw.pop("config", FAST), http_client=http
    )


# -- I1: tokens are checked when they are set ------------------------------------------------

BAD_TOKENS = ["to\nk", "tok\r\nX-Evil: 1", "tök", "to k", "to\x00k", "tok\x7f", "to\tk"]


@pytest.mark.parametrize("token", BAD_TOKENS)
def test_bad_token_is_rejected_at_construction(token: str) -> None:
    with pytest.raises(ConfigurationError, match="access_token") as info:
        TradeSmart("TEST123", access_token=token)
    assert token not in str(info.value) and "Evil" not in str(info.value)


@pytest.mark.parametrize("token", BAD_TOKENS)
async def test_async_bad_token_is_rejected_at_construction(token: str) -> None:
    with pytest.raises(ConfigurationError, match="access_token"):
        AsyncTradeSmart("TEST123", access_token=token)


@pytest.mark.parametrize("token", BAD_TOKENS)
def test_bad_token_is_rejected_by_set_access_token(token: str) -> None:
    client = _sync(_Recorder())
    with pytest.raises(ConfigurationError, match="access_token"):
        client.set_access_token(token)


@pytest.mark.parametrize("token", BAD_TOKENS)
async def test_async_bad_token_is_rejected_by_set_access_token(token: str) -> None:
    client = _async(_Recorder())
    with pytest.raises(ConfigurationError, match="access_token"):
        client.set_access_token(token)


def test_bad_access_token_model_is_rejected() -> None:
    token = AccessToken.from_response({"stat": "Ok", "access_token": "a\nb"})
    with pytest.raises(ConfigurationError, match="access_token"):
        TradeSmart("TEST123", access_token=token)


def test_non_string_token_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="access_token"):
        TradeSmart("TEST123", access_token=12345)  # type: ignore[arg-type]


def test_token_read_from_a_file_has_its_newline_stripped() -> None:
    rec = _Recorder(lambda r: httpx.Response(200, json={"stat": "Ok"}))
    client = _sync(rec, token="  test-access-token\n")
    client.margins()
    assert rec.requests[0].headers["authorization"] == "Bearer test-access-token"


async def test_async_token_read_from_a_file_has_its_newline_stripped() -> None:
    rec = _Recorder(lambda r: httpx.Response(200, json={"stat": "Ok"}))
    client = _async(rec)
    client.set_access_token("test-access-token\r\n")
    await client.margins()
    assert rec.requests[0].headers["authorization"] == "Bearer test-access-token"


# -- I1: the request is built before it is marked as sent ------------------------------------

HEADER_CASES = [
    pytest.param("tök", id="non-ascii"),
    pytest.param("tok\r\nX-Evil: 1", id="crlf"),
    pytest.param("tok\x00", id="nul"),
]


@pytest.mark.parametrize("token", HEADER_CASES)
def test_unencodable_header_is_not_sent_for_an_order(token: str, caplog) -> None:
    rec = _Recorder()
    transport = SyncTransport(
        FAST, lambda: token, http_client=httpx.Client(transport=httpx.MockTransport(rec))
    )
    with pytest.raises(ConfigurationError, match="nothing was sent") as info:
        transport.execute(place_order_call("TEST123", **PLACE_KW))
    assert rec.requests == []
    assert "Evil" not in str(info.value) and token not in str(info.value)
    assert info.value.__context__ is None
    assert _order_lines(caplog) == ["PlaceOrder not_sent order_id=-"]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize("token", HEADER_CASES)
async def test_async_unencodable_header_is_not_sent_for_an_order(token: str, caplog) -> None:
    rec = _Recorder()
    transport = AsyncTransport(
        FAST, lambda: token, http_client=httpx.AsyncClient(transport=httpx.MockTransport(rec))
    )
    with pytest.raises(ConfigurationError, match="nothing was sent") as info:
        await transport.execute(place_order_call("TEST123", **PLACE_KW))
    assert rec.requests == []
    assert info.value.__context__ is None
    assert _order_lines(caplog) == ["PlaceOrder not_sent order_id=-"]


def test_unencodable_header_on_a_read_is_not_retried() -> None:
    calls: list[int] = []

    def token() -> str:
        calls.append(1)
        return "tök"

    rec = _Recorder()
    transport = SyncTransport(
        ClientConfig(max_retries=3, backoff_base=0.001, backoff_cap=0.001),
        token,
        http_client=httpx.Client(transport=httpx.MockTransport(rec)),
    )
    from tradesmart.endpoints.portfolio import limits_call

    with pytest.raises(ConfigurationError):
        transport.execute(limits_call("TEST123"))
    assert rec.requests == [] and len(calls) == 1


def test_lone_surrogate_in_the_payload_is_a_validation_error_not_unknown(caplog) -> None:
    rec = _Recorder()
    client = _sync(rec)
    with pytest.raises(RequestValidationError, match="remarks") as info:
        client.place_order(**PLACE_KW, tag="desk\ud800")
    assert rec.requests == []
    assert info.value.__context__ is None
    assert _order_lines(caplog) == ["PlaceOrder not_sent order_id=-"]


async def test_async_lone_surrogate_in_the_payload_is_a_validation_error(caplog) -> None:
    rec = _Recorder()
    client = _async(rec)
    with pytest.raises(RequestValidationError, match="remarks"):
        await client.place_order(**PLACE_KW, tag="desk\ud800")
    assert rec.requests == []
    assert _order_lines(caplog) == ["PlaceOrder not_sent order_id=-"]


def test_lone_surrogate_in_a_raw_request_is_a_validation_error() -> None:
    rec = _Recorder()
    with pytest.raises(RequestValidationError, match="stext"):
        _sync(rec).request("SearchScrip", {"stext": "\udcff"}, state_changing=False)
    assert rec.requests == []


# -- M1: deeply nested JSON ------------------------------------------------------------------

TOO_DEEP = "[" * 200_000  # json.loads raises RecursionError
# Python 3.12+ parses this (its JSON parser has its own C-stack limit) but redact() raises
# RecursionError; Python 3.11's parser already raises RecursionError (not valid JSON).
NESTED = "[" * 3000 + "]" * 3000
DEEP_NOT_OK = '{"stat":"Not_Ok","emsg":"bad","x":' + NESTED + "}"
DEEP_OK = '{"stat":"Ok","x":' + NESTED + "}"  # no order number: ack body is redacted


def _parses(text: str) -> bool:
    try:
        json.loads(text)
    except RecursionError:
        return False
    return True


def test_deeply_nested_ok_with_an_order_number_is_accepted() -> None:
    body = '{"stat":"Ok","norenordno":"9","x":' + NESTED + "}"
    client = _sync(_Recorder(lambda r: httpx.Response(200, text=body)))
    if not _parses(body):  # Python 3.11: not valid JSON, so the outcome is unknown
        with pytest.raises(OrderStatusUnknownError, match="not valid JSON"):
            client.place_order(**PLACE_KW)
        return
    assert client.place_order(**PLACE_KW) == "9"


@pytest.mark.parametrize("body", [TOO_DEEP, DEEP_OK], ids=["unparseable", "parseable"])
def test_deeply_nested_order_reply_is_unknown(body: str, caplog) -> None:
    client = _sync(_Recorder(lambda r: httpx.Response(200, text=body)))
    with pytest.raises(OrderStatusUnknownError):
        client.place_order(**PLACE_KW)
    assert _order_lines(caplog) == ["PlaceOrder unknown order_id=-"]


@pytest.mark.parametrize("body", [TOO_DEEP, DEEP_OK], ids=["unparseable", "parseable"])
async def test_async_deeply_nested_order_reply_is_unknown(body: str, caplog) -> None:
    client = _async(_Recorder(lambda r: httpx.Response(200, text=body)))
    with pytest.raises(OrderStatusUnknownError):
        await client.place_order(**PLACE_KW)
    assert _order_lines(caplog) == ["PlaceOrder unknown order_id=-"]


class _Interrupt(BaseException):
    """A non-Exception error (like KeyboardInterrupt) raised while sending."""


async def test_async_base_exception_after_send_logs_unknown_and_reraises(caplog) -> None:
    def interrupt(request: httpx.Request) -> httpx.Response:
        raise _Interrupt

    client = _async(_Recorder(interrupt))
    with pytest.raises(_Interrupt):
        await client.place_order(**PLACE_KW)
    assert _order_lines(caplog) == ["PlaceOrder unknown order_id=-"]


def test_deeply_nested_not_ok_is_still_a_rejection() -> None:
    client = _sync(_Recorder(lambda r: httpx.Response(200, text=DEEP_NOT_OK)))
    if not _parses(DEEP_NOT_OK):  # Python 3.11: not valid JSON, so the outcome is unknown
        with pytest.raises(OrderStatusUnknownError, match="not valid JSON"):
            client.place_order(**PLACE_KW)
        return
    with pytest.raises(OrderRejectedError, match="bad") as info:
        client.place_order(**PLACE_KW)
    assert info.value.raw == "<response nested too deeply to show>"


@pytest.mark.parametrize("body", [TOO_DEEP, '{"stat":"Ok","values":' + NESTED + "}"])
def test_deeply_nested_read_reply_is_a_parse_error(body: str) -> None:
    client = _sync(_Recorder(lambda r: httpx.Response(200, text=body)))
    with pytest.raises(ResponseParseError) as info:
        client.orders() if body is TOO_DEEP else client.search_instruments("INFY")
    assert info.value.__context__ is None


async def test_async_deeply_nested_read_reply_is_a_parse_error() -> None:
    client = _async(_Recorder(lambda r: httpx.Response(200, text=TOO_DEEP)))
    with pytest.raises(ResponseParseError):
        await client.orders()


# -- M2: a blank token is no token -----------------------------------------------------------


@pytest.mark.parametrize("token", ["", "   ", "\n"])
def test_blank_token_raises_authentication_error_and_sends_nothing(token: str, caplog) -> None:
    rec = _Recorder()
    client = _sync(rec, token=token)
    with pytest.raises(AuthenticationError, match="No access token set"):
        client.margins()
    with pytest.raises(AuthenticationError):
        client.place_order(**PLACE_KW)
    assert rec.requests == []
    assert _order_lines(caplog) == ["PlaceOrder not_sent order_id=-"]


async def test_async_blank_token_set_later_sends_nothing() -> None:
    rec = _Recorder()
    client = _async(rec)
    client.set_access_token("")
    with pytest.raises(AuthenticationError, match="No access token set"):
        await client.margins()
    assert rec.requests == []


def test_blank_access_token_model_is_no_token() -> None:
    rec = _Recorder()
    token = AccessToken.from_response({"stat": "Ok", "access_token": ""})
    with pytest.raises(AuthenticationError, match="No access token set"):
        _sync(rec, token=token).margins()
    assert rec.requests == []


# -- M4: no httpx error (with the request headers) in __context__ ----------------------------


def _read_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


def _runtime(request: httpx.Request) -> httpx.Response:
    raise RuntimeError("boom")


@pytest.mark.parametrize(
    "handler,order_error,read_error",
    [
        (_read_timeout, OrderStatusUnknownError, RequestTimeoutError),
        (_runtime, OrderStatusUnknownError, NetworkError),
    ],
)
def test_transport_errors_carry_no_context(handler: Any, order_error: Any, read_error: Any) -> None:
    client = _sync(_Recorder(handler))
    with pytest.raises(order_error) as order_info:
        client.place_order(**PLACE_KW)
    with pytest.raises(read_error) as read_info:
        client.margins()
    for info in (order_info, read_info):
        assert info.value.__context__ is None and info.value.__cause__ is None


@pytest.mark.parametrize(
    "handler,order_error,read_error",
    [
        (_read_timeout, OrderStatusUnknownError, RequestTimeoutError),
        (_runtime, OrderStatusUnknownError, NetworkError),
    ],
)
async def test_async_transport_errors_carry_no_context(
    handler: Any, order_error: Any, read_error: Any
) -> None:
    client = _async(_Recorder(handler))
    with pytest.raises(order_error) as order_info:
        await client.place_order(**PLACE_KW)
    with pytest.raises(read_error) as read_info:
        await client.margins()
    for info in (order_info, read_info):
        assert info.value.__context__ is None and info.value.__cause__ is None


# -- M5: a client-side RateLimitError names the endpoint -------------------------------------

RAISE = ClientConfig(rate_limit_mode="raise")


def _exhaust(client: Any, group: str) -> None:
    """Fill the group's per-second window (no timing race with a slow machine)."""
    limiter = client._transport._limiter.get(group)
    while limiter.try_acquire() == 0.0:
        pass


def test_client_side_rate_limit_names_the_endpoint() -> None:
    client = _sync(_Recorder(), config=RAISE)
    _exhaust(client, "place_order")
    with pytest.raises(RateLimitError) as info:
        client.place_order(**PLACE_KW)
    assert info.value.endpoint == "PlaceOrder"
    assert "place_order" in str(info.value)


async def test_async_client_side_rate_limit_names_the_endpoint() -> None:
    quote = fixture("get_quotes_success")
    client = _async(_Recorder(lambda r: httpx.Response(200, json=quote)), config=RAISE)
    await client.quote("NSE", "22")  # goes through
    _exhaust(client, "quotes")
    with pytest.raises(RateLimitError) as info:
        await client.quote("NSE", "22")
    assert info.value.endpoint == "GetQuotes"
    assert "quotes" in str(info.value)


# -- M3: time inputs and ids -----------------------------------------------------------------


@pytest.mark.parametrize("value", ["2024-01-01", 1.9, True, None, 1.0, b"1"])
def test_to_epoch_accepts_only_datetime_date_or_int(value: Any) -> None:
    with pytest.raises(RequestValidationError, match="time"):
        to_epoch(value)


def test_to_epoch_still_accepts_valid_inputs() -> None:
    assert to_epoch(5) == "5"
    assert to_epoch(date(2025, 6, 20)) == "1750357800"
    assert to_epoch(datetime(2025, 6, 19, 18, 30, tzinfo=UTC)) == "1750357800"
    assert to_epoch(datetime(2025, 6, 20, tzinfo=IST)) == "1750357800"


def test_historical_data_with_a_text_date_is_a_validation_error() -> None:
    rec = _Recorder()
    with pytest.raises(RequestValidationError):
        _sync(rec).historical_data("NSE", "22", from_date="2024-01-01")  # type: ignore[arg-type]
    with pytest.raises(RequestValidationError):
        _sync(rec).daily_history("NSE", "SBIN-EQ", 1.5, 2)  # type: ignore[arg-type]
    assert rec.requests == []


async def test_async_historical_data_with_a_bool_is_a_validation_error() -> None:
    rec = _Recorder()
    with pytest.raises(RequestValidationError):
        await _async(rec).historical_data("NSE", "22", from_date=True)
    assert rec.requests == []


def _read_calls(client: Any) -> list[tuple[str, Callable[[], Any]]]:
    return [
        ("norenordno", lambda: client.order_history(12345)),
        ("token", lambda: client.quote("NSE", 2885)),
        ("token", lambda: client.security_info("NSE", 2885)),
        ("stext", lambda: client.search_instruments(None)),
        ("tsym", lambda: client.daily_history("NSE", 123, 1, 2)),
    ]


def test_read_ids_must_be_strings() -> None:
    rec = _Recorder()
    for field, call in _read_calls(_sync(rec)):
        with pytest.raises(RequestValidationError, match=field):
            call()
    assert rec.requests == []


async def test_async_read_ids_must_be_strings() -> None:
    rec = _Recorder()
    for field, call in _read_calls(_async(rec)):
        with pytest.raises(RequestValidationError, match=field):
            await call()
    assert rec.requests == []
