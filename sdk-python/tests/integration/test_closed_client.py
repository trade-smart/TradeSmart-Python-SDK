"""A closed client raises ``ConfigurationError`` before sending (never httpx's RuntimeError)."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
import respx

from tradesmart import AsyncTradeSmart, ConfigurationError, TradeSmart, TradeSmartError

CLOSED = "client is closed; create a new client"
ORDER = {
    "exchange": "NSE",
    "tradingsymbol": "SBIN-EQ",
    "quantity": 1,
    "transaction_type": "B",
    "product": "C",
    "order_type": "LMT",
    "price": "100.00",
}


def _orders_log(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "tradesmart.orders"]


def test_sync_calls_after_close_raise_before_sending(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.DEBUG, logger="tradesmart")
    client = TradeSmart("TEST123", access_token="test-access-token")
    client.close()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.route().respond(200, json={"stat": "Ok"})
        for call in (
            client.profile,
            client.orders,
            lambda: client.place_order(**ORDER),
            lambda: client.cancel_order("26110700000001"),
            lambda: client.request("Limits", {}, state_changing=False),
            lambda: client.instruments("NSE"),
            lambda: client.download_instruments("NSE", tmp_path, force=True),
        ):
            with pytest.raises(ConfigurationError, match=CLOSED) as info:
                call()
            assert isinstance(info.value, TradeSmartError)
    assert not router.calls
    # Nothing was sent: each order call logs `not_sent` (never `rejected`), with no WARNING.
    assert [r.getMessage().split(" in ")[0] for r in _orders_log(caplog)] == [
        "PlaceOrder not_sent order_id=-",
        "CancelOrder not_sent order_id=26110700000001",
    ]


def test_sync_with_block_closes_the_client() -> None:
    with TradeSmart("TEST123", access_token="test-access-token") as client:
        pass
    with pytest.raises(ConfigurationError, match=CLOSED):
        client.margins()


def test_sync_injected_http_client_closed_by_the_caller() -> None:
    http = httpx.Client()
    client = TradeSmart("TEST123", access_token="test-access-token", http_client=http)
    http.close()
    with pytest.raises(ConfigurationError, match=CLOSED):
        client.place_order(**ORDER)


def test_sync_close_marks_a_client_with_an_injected_http_client_closed() -> None:
    http = httpx.Client()
    client = TradeSmart("TEST123", access_token="test-access-token", http_client=http)
    client.close()
    assert not http.is_closed  # the caller owns it
    mock = respx.mock(assert_all_mocked=True, assert_all_called=False)
    with mock as router, pytest.raises(ConfigurationError, match=CLOSED):
        client.profile()
    assert not router.calls
    http.close()


async def test_async_calls_after_close_raise_before_sending(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.DEBUG, logger="tradesmart")
    client = AsyncTradeSmart("TEST123", access_token="test-access-token")
    await client.aclose()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.route().respond(200, json={"stat": "Ok"})
        for call in (
            client.profile,
            lambda: client.place_order(**ORDER),
            lambda: client.modify_order(
                "26110700000001",
                exchange="NSE",
                tradingsymbol="SBIN-EQ",
                quantity=1,
                price="101.00",
                order_type="LMT",
                product="C",
            ),
            lambda: client.request("PlaceOrder", {}, state_changing=True),
            lambda: client.instruments("NSE"),
            lambda: client.download_instruments("NSE", tmp_path, force=True),
        ):
            with pytest.raises(ConfigurationError, match=CLOSED):
                await call()
    assert not router.calls
    assert [r.getMessage().split(" in ")[0] for r in _orders_log(caplog)] == [
        "PlaceOrder not_sent order_id=-",
        "ModifyOrder not_sent order_id=26110700000001",
        "PlaceOrder not_sent order_id=-",
    ]


async def test_async_with_block_closes_the_client() -> None:
    async with AsyncTradeSmart("TEST123", access_token="test-access-token") as client:
        pass
    with pytest.raises(ConfigurationError, match=CLOSED):
        await client.positions()


async def test_async_injected_http_client_closed_by_the_caller() -> None:
    http = httpx.AsyncClient()
    client = AsyncTradeSmart("TEST123", access_token="test-access-token", http_client=http)
    await http.aclose()
    with pytest.raises(ConfigurationError, match=CLOSED):
        await client.place_order(**ORDER)
