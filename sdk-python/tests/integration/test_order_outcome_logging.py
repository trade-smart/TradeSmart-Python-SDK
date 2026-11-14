"""Order outcome logging: one INFO per state-changing call, one WARNING per unknown outcome."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx
import pytest
import respx

from tests.conftest import BASE, fixture
from tradesmart import (
    AccessToken,
    AsyncTradeSmart,
    AuthenticationError,
    ClientConfig,
    Exchange,
    OrderRejectedError,
    OrderStatusUnknownError,
    OrderType,
    Product,
    RateLimitError,
    RequestValidationError,
    TradeSmart,
    TradeSmartError,
    TransactionType,
)
from tradesmart._transport.async_http import AsyncTransport
from tradesmart._transport.sync_http import SyncTransport
from tradesmart.endpoints.orders import place_order_call
from tradesmart.models.base import IST

PLACE_KW: dict[str, Any] = dict(
    exchange=Exchange.NSE,
    tradingsymbol="INFY-EQ",
    quantity=1,
    transaction_type=TransactionType.BUY,
    product=Product.CNC,
    order_type=OrderType.LIMIT,
    price=1234.5,
    tag="test-tag-77",
)
SECRETS = (
    "test-access-token",
    "1234.5",
    "INFY-EQ",
    "test-tag-77",
    "test-secret",
    "test-app-key",
)


def _records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "tradesmart.orders" and r.levelno == level]


def _assert_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    text = " ".join(r.getMessage() for r in caplog.records)
    for secret in SECRETS:
        assert secret not in text


@pytest.fixture(autouse=True)
def _capture(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="tradesmart")


@respx.mock
def test_accepted_place_order_logs_one_info(client: TradeSmart, caplog) -> None:
    respx.post(f"{BASE}/PlaceOrder").respond(200, json=fixture("place_order_success"))
    client.place_order(**PLACE_KW)
    (info,) = _records(caplog, logging.INFO)
    message = info.getMessage()
    assert message.startswith("PlaceOrder accepted order_id=26110700000001 in ")
    assert message.endswith(" ms")
    assert _records(caplog, logging.WARNING) == []
    _assert_no_secrets(caplog)


@respx.mock
def test_rejected_cancel_logs_rejected_with_order_id(client: TradeSmart, caplog) -> None:
    respx.post(f"{BASE}/CancelOrder").respond(200, json=fixture("errors")["order_reject"])
    with pytest.raises(OrderRejectedError):
        client.cancel_order("123")
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("CancelOrder rejected order_id=123 in ")
    assert _records(caplog, logging.WARNING) == []
    _assert_no_secrets(caplog)


@respx.mock
def test_timeout_logs_unknown_and_exactly_one_warning(client: TradeSmart, caplog) -> None:
    respx.post(f"{BASE}/PlaceOrder").mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(OrderStatusUnknownError):
        client.place_order(**PLACE_KW)
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("PlaceOrder unknown order_id=- in ")
    (warning,) = _records(caplog, logging.WARNING)
    assert "PlaceOrder" in warning.getMessage()
    assert "check orders() before acting" in warning.getMessage()
    _assert_no_secrets(caplog)


@respx.mock
def test_unusable_ack_logs_exactly_one_warning(client: TradeSmart, caplog) -> None:
    respx.post(f"{BASE}/PlaceOrder").respond(200, json={"stat": "Ok"})
    with pytest.raises(OrderStatusUnknownError):
        client.place_order(**PLACE_KW)
    assert len(_records(caplog, logging.WARNING)) == 1
    assert _records(caplog, logging.INFO)[0].getMessage().startswith("PlaceOrder unknown")


@respx.mock
def test_ambiguous_body_logs_exactly_one_warning(client: TradeSmart, caplog) -> None:
    respx.post(f"{BASE}/ModifyOrder").respond(502, text="<html>bad gateway</html>")
    with pytest.raises(OrderStatusUnknownError):
        client.modify_order(
            "77",
            exchange=Exchange.NSE,
            tradingsymbol="INFY-EQ",
            quantity=1,
            price=1234.5,
            order_type=OrderType.LIMIT,
            product=Product.CNC,
        )
    assert len(_records(caplog, logging.WARNING)) == 1
    assert (
        _records(caplog, logging.INFO)[0]
        .getMessage()
        .startswith("ModifyOrder unknown order_id=77 in ")
    )
    _assert_no_secrets(caplog)


@respx.mock
def test_read_endpoints_are_not_logged_as_order_outcomes(client: TradeSmart, caplog) -> None:
    respx.post(f"{BASE}/OrderBook").respond(200, json=[])
    client.orders()
    assert [r for r in caplog.records if r.name == "tradesmart.orders"] == []


@respx.mock
def test_genacstok_unknown_is_not_an_order_warning(caplog) -> None:
    client = TradeSmart("TEST123", app_key="test-app-key", secret_key="test-secret")
    respx.post(f"{BASE}/GenAcsTok").mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(AuthenticationError):
        client.generate_session("test-auth-code")
    assert [r for r in caplog.records if r.name == "tradesmart.orders"] == []
    _assert_no_secrets(caplog)


@respx.mock
def test_raw_request_place_multi_order_is_logged(client: TradeSmart, caplog) -> None:
    respx.post(f"{BASE}/PlaceMultiOrder").respond(200, json={"stat": "Ok", "norenordno": "9"})
    client.request("PlaceMultiOrder", {"exch": "NSE"}, state_changing=True)
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("PlaceMultiOrder accepted order_id=9 in ")


@respx.mock
def test_raw_state_changing_read_is_not_an_order_outcome(client: TradeSmart, caplog) -> None:
    respx.post(f"{BASE}/OrderBook").respond(200, json=[])
    client.request("OrderBook", {}, state_changing=True, expect="list")
    assert [r for r in caplog.records if r.name == "tradesmart.orders"] == []


@respx.mock
async def test_async_raw_state_changing_read_is_not_an_order_outcome(
    async_client: AsyncTradeSmart, caplog
) -> None:
    respx.post(f"{BASE}/OrderBook").respond(200, json=[])
    await async_client.request("OrderBook", {}, state_changing=True, expect="list")
    assert [r for r in caplog.records if r.name == "tradesmart.orders"] == []


def test_client_side_rate_limit_refusal_logs_one_not_sent_and_no_warning(caplog) -> None:
    transport = SyncTransport(
        ClientConfig(rate_limit_mode="raise"),
        lambda: "test-access-token",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    limiter = transport._limiter.get("place_order")
    assert limiter is not None
    while limiter.try_acquire() == 0.0:
        pass  # exhaust the per-second window so the next order is refused
    with pytest.raises(RateLimitError):
        transport.execute(place_order_call("TEST123", **PLACE_KW))
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("PlaceOrder not_sent order_id=- in ")
    assert _records(caplog, logging.WARNING) == []
    _assert_no_secrets(caplog)


async def test_async_client_side_rate_limit_refusal_logs_not_sent(caplog) -> None:
    transport = AsyncTransport(
        ClientConfig(rate_limit_mode="raise"),
        lambda: "test-access-token",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    limiter = transport._limiter.get("place_order")
    assert limiter is not None
    while limiter.try_acquire() == 0.0:
        pass
    with pytest.raises(RateLimitError):
        await transport.execute(place_order_call("TEST123", **PLACE_KW))
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("PlaceOrder not_sent order_id=- in ")
    assert _records(caplog, logging.WARNING) == []


def _expired_token() -> AccessToken:
    return AccessToken.from_response(
        fixture("gen_acs_tok_success"), issued_at=datetime(2020, 1, 1, tzinfo=IST)
    )


@pytest.mark.parametrize("token", [None, "expired"])
def test_missing_or_expired_token_logs_not_sent(caplog, token: str | None) -> None:
    client = TradeSmart("TEST123", access_token=_expired_token() if token else None)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{BASE}/CancelOrder").respond(200, json={"stat": "Ok"})
        with pytest.raises(AuthenticationError):
            client.cancel_order("123")
    assert not route.called
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("CancelOrder not_sent order_id=123 in ")
    assert _records(caplog, logging.WARNING) == []


@pytest.mark.parametrize("token", [None, "expired"])
async def test_async_missing_or_expired_token_logs_not_sent(caplog, token: str | None) -> None:
    client = AsyncTradeSmart("TEST123", access_token=_expired_token() if token else None)
    with pytest.raises(AuthenticationError):
        await client.place_order(**PLACE_KW)
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("PlaceOrder not_sent order_id=- in ")


_CONNECT_PHASE = [
    pytest.param(httpx.ConnectError("refused"), id="connect-error"),
    pytest.param(httpx.ConnectTimeout("connect timed out"), id="connect-timeout"),
    pytest.param(httpx.PoolTimeout("pool full"), id="pool-timeout"),
]


@pytest.mark.parametrize("error", _CONNECT_PHASE)
@respx.mock
def test_connect_phase_failure_logs_not_sent(client: TradeSmart, caplog, error) -> None:
    respx.post(f"{BASE}/PlaceOrder").mock(side_effect=error)
    with pytest.raises(TradeSmartError) as info:
        client.place_order(**PLACE_KW)
    assert not isinstance(info.value, OrderStatusUnknownError)
    (info_line,) = _records(caplog, logging.INFO)
    assert info_line.getMessage().startswith("PlaceOrder not_sent order_id=- in ")
    assert _records(caplog, logging.WARNING) == []


@pytest.mark.parametrize("error", _CONNECT_PHASE)
@respx.mock
async def test_async_connect_phase_failure_logs_not_sent(
    async_client: AsyncTradeSmart, caplog, error
) -> None:
    respx.post(f"{BASE}/PlaceOrder").mock(side_effect=error)
    with pytest.raises(TradeSmartError):
        await async_client.place_order(**PLACE_KW)
    (info_line,) = _records(caplog, logging.INFO)
    assert info_line.getMessage().startswith("PlaceOrder not_sent order_id=- in ")


@respx.mock
def test_server_rate_limit_answer_is_rejected_not_not_sent(client: TradeSmart, caplog) -> None:
    respx.post(f"{BASE}/PlaceOrder").respond(
        429, json={"stat": "Not_Ok", "emsg": "Too many requests"}
    )
    with pytest.raises(RateLimitError):
        client.place_order(**PLACE_KW)
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("PlaceOrder rejected order_id=- in ")


@respx.mock
async def test_async_rejected_order_logs_rejected(async_client: AsyncTradeSmart, caplog) -> None:
    respx.post(f"{BASE}/CancelOrder").respond(200, json=fixture("errors")["order_reject"])
    with pytest.raises(OrderRejectedError):
        await async_client.cancel_order("123")
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("CancelOrder rejected order_id=123 in ")


def test_invalid_order_parameters_are_never_logged_as_rejected(client: TradeSmart, caplog) -> None:
    """Validation fails before an order call exists: no outcome line at all."""
    with pytest.raises(RequestValidationError):
        client.place_order(**{**PLACE_KW, "quantity": 0})
    assert _records(caplog, logging.INFO) == []


def test_unknown_outcome_is_logged_unknown_even_if_raised_before_sending(caplog) -> None:
    """Defence in depth: an OrderStatusUnknownError always logs `unknown` and its WARNING."""
    from tradesmart._transport.outcome import log_failure

    call = place_order_call("TEST123", **PLACE_KW)
    log_failure(call, OrderStatusUnknownError("PlaceOrder", "x"), 0.0, sent=False)
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("PlaceOrder unknown order_id=- in ")
    assert len(_records(caplog, logging.WARNING)) == 1


# Failures after the connection exists: the request may have been written, so never `not_sent`.
_AFTER_CONNECT = [
    pytest.param(httpx.WriteTimeout("write timed out"), id="write-timeout"),
    pytest.param(httpx.WriteError("write failed"), id="write-error"),
    pytest.param(httpx.ReadError("read failed"), id="read-error"),
    pytest.param(httpx.RemoteProtocolError("server disconnected"), id="remote-protocol-error"),
]


@pytest.mark.parametrize("error", _AFTER_CONNECT)
@respx.mock
def test_failure_after_connect_logs_unknown_not_not_sent(client: TradeSmart, caplog, error) -> None:
    respx.post(f"{BASE}/PlaceOrder").mock(side_effect=error)
    with pytest.raises(OrderStatusUnknownError):
        client.place_order(**PLACE_KW)
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("PlaceOrder unknown order_id=- in ")
    assert len(_records(caplog, logging.WARNING)) == 1


@pytest.mark.parametrize("error", _AFTER_CONNECT)
@respx.mock
async def test_async_failure_after_connect_logs_unknown_not_not_sent(
    async_client: AsyncTradeSmart, caplog, error
) -> None:
    respx.post(f"{BASE}/PlaceOrder").mock(side_effect=error)
    with pytest.raises(OrderStatusUnknownError):
        await async_client.place_order(**PLACE_KW)
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("PlaceOrder unknown order_id=- in ")
    assert len(_records(caplog, logging.WARNING)) == 1


@respx.mock
async def test_async_accepted_and_unknown(async_client: AsyncTradeSmart, caplog) -> None:
    route = respx.post(f"{BASE}/PlaceOrder")
    route.respond(200, json=fixture("place_order_success"))
    await async_client.place_order(**PLACE_KW)
    route.mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(OrderStatusUnknownError):
        await async_client.place_order(**PLACE_KW)
    messages = [r.getMessage() for r in _records(caplog, logging.INFO)]
    assert messages[0].startswith("PlaceOrder accepted order_id=26110700000001 in ")
    assert messages[1].startswith("PlaceOrder unknown order_id=- in ")
    assert len(_records(caplog, logging.WARNING)) == 1
    _assert_no_secrets(caplog)


class _HangingTransport(httpx.AsyncBaseTransport):
    """Receives the request, then never answers (the server got it; no reply yet)."""

    def __init__(self) -> None:
        self.received = 0
        self.arrived = asyncio.Event()  # created inside the test's running loop

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.received += 1
        self.arrived.set()
        await asyncio.sleep(30)
        return httpx.Response(200, json=fixture("place_order_success"))  # pragma: no cover


async def _cancel_after_send(client: AsyncTradeSmart, hanging: _HangingTransport) -> None:
    """Start an order, wait until the transport has the request, then cancel it."""
    task = asyncio.ensure_future(client.place_order(**PLACE_KW))
    await asyncio.wait_for(hanging.arrived.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_async_cancellation_after_send_warns_and_reraises(caplog) -> None:
    hanging = _HangingTransport()
    client = AsyncTradeSmart(
        "TEST123",
        access_token="test-access-token",
        http_client=httpx.AsyncClient(transport=hanging),
    )
    await _cancel_after_send(client, hanging)
    assert hanging.received == 1
    (warning,) = _records(caplog, logging.WARNING)
    assert warning.getMessage() == "PlaceOrder: outcome unknown - check orders()"
    assert warning.getMessage().isascii()
    _assert_no_secrets(caplog)


async def test_async_cancellation_reraises_cancelled_error_unchanged(caplog) -> None:
    hanging = _HangingTransport()
    client = AsyncTradeSmart(
        "TEST123",
        access_token="test-access-token",
        http_client=httpx.AsyncClient(transport=hanging),
    )
    await _cancel_after_send(client, hanging)
    assert len(_records(caplog, logging.WARNING)) == 1


async def test_async_cancellation_while_waiting_for_rate_limit_slot_is_silent(caplog) -> None:
    hanging = _HangingTransport()
    transport = AsyncTransport(
        ClientConfig(order_rate_limit_max_wait=60.0),
        lambda: "test-access-token",
        http_client=httpx.AsyncClient(transport=hanging),
    )
    limiter = transport._limiter.get("place_order")
    assert limiter is not None
    while limiter.try_acquire() == 0.0:
        pass  # exhaust the per-second window so the next order must wait
    call = place_order_call("TEST123", **PLACE_KW)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(transport.execute(call), 0.1)
    assert hanging.received == 0
    assert [r for r in caplog.records if r.name == "tradesmart.orders"] == []


def _interrupting_transport(exc: BaseException) -> SyncTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        raise exc

    return SyncTransport(
        ClientConfig(),
        lambda: "test-access-token",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
def test_sync_interrupt_while_sending_logs_unknown_and_reraises_unchanged(
    caplog, exc_type: type[BaseException]
) -> None:
    original = exc_type()
    transport = _interrupting_transport(original)
    with pytest.raises(exc_type) as raised:
        transport.execute(place_order_call("TEST123", **PLACE_KW))
    assert raised.value is original
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("PlaceOrder unknown order_id=- in ")
    (warning,) = _records(caplog, logging.WARNING)
    assert "outcome unknown" in warning.getMessage()
    _assert_no_secrets(caplog)


def test_sync_interrupt_before_sending_logs_nothing_new(caplog) -> None:
    original = KeyboardInterrupt()

    def provider() -> str:
        raise original

    transport = SyncTransport(
        ClientConfig(),
        provider,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    with pytest.raises(KeyboardInterrupt) as raised:
        transport.execute(place_order_call("TEST123", **PLACE_KW))
    assert raised.value is original
    assert [r for r in caplog.records if r.name == "tradesmart.orders"] == []


# --- GTT and iceberg write endpoints (spec 2026-11-12 §2) --------------------------------


GTT_ACK = {"request_time": "10:00:00 12-11-2026", "stat": "Oi created", "al_id": "26111200000010"}


@respx.mock
def test_request_place_gtt_accepted_logs_al_id(client: TradeSmart, caplog) -> None:
    respx.post(f"{BASE}/PlaceGTTOrder").respond(200, json=GTT_ACK)
    client.request("placegttorder", {"tsym": "INFY-EQ", "d": "1234.5"}, state_changing=False)
    (info,) = _records(caplog, logging.INFO)
    message = info.getMessage()
    assert message.startswith("PlaceGTTOrder accepted al_id=26111200000010 in ")
    assert "order_id" not in message
    assert _records(caplog, logging.WARNING) == []
    _assert_no_secrets(caplog)


@respx.mock
def test_request_cancel_gtt_rejected_names_the_payload_al_id(client: TradeSmart, caplog) -> None:
    respx.post(f"{BASE}/CancelGTTOrder").respond(
        200, json={"stat": "Not_Ok", "emsg": "Alert not found"}
    )
    with pytest.raises(OrderRejectedError):
        client.request("CancelGTTOrder", {"al_id": "26111200000099"}, state_changing=False)
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("CancelGTTOrder rejected al_id=26111200000099 in ")


@respx.mock
def test_request_place_gtt_without_al_id_logs_a_dash(client: TradeSmart, caplog) -> None:
    respx.post(f"{BASE}/PlaceGTTOrder").respond(200, json={"stat": "Ok"})
    with pytest.raises(OrderStatusUnknownError) as raised:
        client.request("PlaceGTTOrder", {}, state_changing=False)
    assert "pending_gtts()" in str(raised.value)
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("PlaceGTTOrder unknown al_id=- in ")


@pytest.mark.parametrize("endpoint", ["PlaceGTTOrder", "ModifyGTTOrder", "CancelGTTOrder"])
@respx.mock
def test_gtt_unknown_outcome_warning_points_to_pending_gtts(
    client: TradeSmart, caplog, endpoint: str
) -> None:
    respx.post(f"{BASE}/{endpoint}").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(OrderStatusUnknownError) as raised:
        client.request(endpoint, {"al_id": "26111200000010"}, state_changing=False)
    assert "pending_gtts()" in str(raised.value)
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith(f"{endpoint} unknown al_id=26111200000010 in ")
    (warning,) = _records(caplog, logging.WARNING)
    assert warning.getMessage() == (
        f"{endpoint}: order status unknown; check pending_gtts() before acting"
    )


@respx.mock
def test_iceberg_writes_are_order_outcomes(client: TradeSmart, caplog) -> None:
    respx.post(f"{BASE}/PlaceIceBergOrder").respond(200, json={"stat": "true", "aq_id": "7"})
    client.request("placeicebergorder", {}, state_changing=False)
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("PlaceIceBergOrder accepted order_id=")


@respx.mock
def test_iceberg_unknown_warning_still_points_to_orders(client: TradeSmart, caplog) -> None:
    respx.post(f"{BASE}/CancelIceBergOrder").respond(200, json={"stat": "True"})
    with pytest.raises(OrderStatusUnknownError):
        client.request("CancelIceBergOrder", {}, state_changing=False)
    (warning,) = _records(caplog, logging.WARNING)
    assert "check orders() before acting" in warning.getMessage()


async def test_async_gtt_cancellation_after_send_warns_pending_gtts(caplog) -> None:
    hanging = _HangingTransport()
    client = AsyncTradeSmart(
        "TEST123",
        access_token="test-access-token",
        http_client=httpx.AsyncClient(transport=hanging),
    )
    task = asyncio.ensure_future(client.request("placegttorder", {}, state_changing=False))
    await asyncio.wait_for(hanging.arrived.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (warning,) = _records(caplog, logging.WARNING)
    assert warning.getMessage() == "PlaceGTTOrder: outcome unknown - check pending_gtts()"


@respx.mock
async def test_async_place_gtt_accepted_logs_al_id(async_client: AsyncTradeSmart, caplog) -> None:
    respx.post(f"{BASE}/PlaceGTTOrder").respond(200, json=GTT_ACK)
    await async_client.request("placegttorder", {}, state_changing=False)
    (info,) = _records(caplog, logging.INFO)
    assert info.getMessage().startswith("PlaceGTTOrder accepted al_id=26111200000010 in ")
    await async_client.aclose()
