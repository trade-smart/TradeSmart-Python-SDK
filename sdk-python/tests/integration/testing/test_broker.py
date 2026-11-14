"""``OfflineBroker``: real clients and streams wired to the offline broker (no network)."""

from __future__ import annotations

import asyncio
import gc
import os
import shutil
import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from tradesmart import (
    AsyncTradeSmart,
    AsyncTradeSmartStream,
    AuthenticationError,
    OrderType,
    Product,
    StreamState,
    TradeSmart,
    TradeSmartStream,
    TransactionType,
)
from tradesmart.auth import AccessToken, next_expiry
from tradesmart.enums import OrderStatus
from tradesmart.errors import APIError, OrderRejectedError
from tradesmart.models.base import IST
from tradesmart.models.stream import (
    OrderUpdate,
    PositionAck,
    StreamMessage,
    TouchlineAck,
    TouchlineFeed,
)
from tradesmart.testing import OfflineBroker, OfflineOrder, OfflineRequest, drills

WAIT = 30.0
STREAM_THREAD = "tradesmart-offline-stream"


def wait_for(predicate: Callable[[], bool], timeout: float = WAIT) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        time.sleep(0.005)


async def await_for(predicate: Callable[[], bool], timeout: float = WAIT) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(0.005)


def stream_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == STREAM_THREAD and t.is_alive()}


def port_of(ws_url: str) -> int:
    port = urlsplit(ws_url).port
    assert port is not None
    return port


def refuses_connections(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return True
    return False


def buy(client: TradeSmart, symbol: str = "ACC-EQ", quantity: int = 10, **extra: Any) -> str:
    settings: dict[str, Any] = {
        "exchange": "NSE",
        "tradingsymbol": symbol,
        "quantity": quantity,
        "transaction_type": TransactionType.BUY,
        "product": Product.MIS,
        "order_type": OrderType.LIMIT,
        "price": "101.50",
    }
    settings.update(extra)
    return client.place_order(**settings)


# --- the spec's example (design §3), verbatim


def run_my_strategy(client: TradeSmart) -> None:
    """A tiny strategy: buy one lot at the last price."""
    quote = client.quote("NFO", "43210")
    client.place_order(
        exchange="NFO",
        tradingsymbol="NIFTY25NOV24000CE",
        quantity=75,
        transaction_type=TransactionType.BUY,
        product=Product.NRML,
        order_type=OrderType.LIMIT,
        price=quote.last_price,
    )


def test_spec_example() -> None:
    with OfflineBroker() as broker:
        broker.add_instrument(
            "NFO", token="43210", symbol="NIFTY25NOV24000CE", lot_size=75, tick_size="0.05"
        )
        broker.set_quote("NFO", "43210", last_price="101.50")
        client = broker.client()  # real TradeSmart pointed at the offline broker, already logged in
        run_my_strategy(client)
        order = broker.orders[-1]  # what the strategy sent
        assert order.side == "B" and order.quantity == 75
        broker.fill(order.id, price="101.50")
        # beyond the spec example: the fill reached the order book and the position
        assert client.orders()[0].status is OrderStatus.COMPLETE
        assert client.positions()[0].net_quantity == 75


async def run_my_async_strategy(client: AsyncTradeSmart) -> None:
    quote = await client.quote("NFO", "43210")
    await client.place_order(
        exchange="NFO",
        tradingsymbol="NIFTY25NOV24000CE",
        quantity=75,
        transaction_type=TransactionType.BUY,
        product=Product.NRML,
        order_type=OrderType.LIMIT,
        price=quote.last_price,
    )


async def test_spec_example_async() -> None:
    with OfflineBroker() as broker:
        broker.add_instrument(
            "NFO", token="43210", symbol="NIFTY25NOV24000CE", lot_size=75, tick_size="0.05"
        )
        broker.set_quote("NFO", "43210", last_price="101.50")
        async with broker.async_client() as client:
            await run_my_async_strategy(client)
            order = broker.orders[-1]
            assert order.side == "B" and order.quantity == 75
            broker.fill(order.id, price="101.50")
            assert (await client.orders())[0].status is OrderStatus.COMPLETE


# --- review focus 1: closing


def test_exception_inside_with_still_closes() -> None:
    before = stream_threads()
    with pytest.raises(RuntimeError, match="strategy crashed"), OfflineBroker() as broker:
        client = broker.client()
        port = port_of(client.config.ws_url)
        cache = Path(str(client.config.instruments_dir))
        assert cache.is_dir() and stream_threads() - before
        raise RuntimeError("strategy crashed")
    assert stream_threads() == before  # the server thread has stopped
    assert refuses_connections(port)
    assert not cache.exists()  # the private script-master folder is gone
    broker.close()  # idempotent


def test_close_is_idempotent_and_safe_if_never_started() -> None:
    OfflineBroker().close()
    broker = OfflineBroker()
    broker.close()
    broker.close()


def test_a_closed_broker_builds_no_more_clients_or_streams() -> None:
    broker = OfflineBroker()
    broker.close()
    for factory in (broker.client, broker.async_client, broker.stream, broker.async_stream):
        with pytest.raises(RuntimeError, match="closed"):
            factory()


def test_server_starts_lazily_without_with() -> None:
    before = stream_threads()
    broker = OfflineBroker()
    try:
        assert stream_threads() == before  # nothing runs until it is needed
        broker.add_instrument("NSE", "22", "ACC-EQ")
        broker.set_quote("NSE", "22", last_price="101.50")
        ticks: list[StreamMessage] = []
        with broker.stream(on_touchline=ticks.append) as stream:
            stream.subscribe_touchline("NSE|22")
            wait_for(lambda: any(isinstance(m, TouchlineAck) for m in ticks))
    finally:
        broker.close()
    assert stream_threads() == before


# --- review focus 2: two brokers


def test_two_brokers_are_isolated() -> None:
    with OfflineBroker() as first, OfflineBroker("OTHER1") as second:
        one, two = first.client(), second.client()
        assert port_of(one.config.ws_url) != port_of(two.config.ws_url)
        assert one.config.instruments_dir != two.config.instruments_dir
        assert (one.client_id, two.client_id) == ("OFFLINE01", "OTHER1")
        first.add_instrument("NSE", "22", "ACC-EQ")
        first.set_quote("NSE", "22", last_price="101.50")
        buy(one)
        assert len(first.orders) == 1 and second.orders == []
        assert one.quote("NSE", "22").last_price == Decimal("101.50")
        with pytest.raises(APIError, match="no data"):
            two.quote("NSE", "22")
        assert {r.endpoint for r in second.requests} == {"GetQuotes"}
        with pytest.raises(ValueError, match="no order"):
            second.fill(first.orders[0].id, price="101.50")
        assert one.instruments("NSE")[0]["TradingSymbol"] == "ACC-EQ"
        assert two.instruments("NSE") == []


# --- review focus 3: events from the test thread reach a sync stream


def test_fill_from_test_thread_reaches_sync_stream_callback() -> None:
    updates: list[OrderUpdate] = []
    acks: list[PositionAck] = []
    threads: set[str] = set()

    def on_order_update(update: OrderUpdate) -> None:
        threads.add(threading.current_thread().name)
        updates.append(update)

    with OfflineBroker() as broker:
        client = broker.client()
        with broker.stream(on_order_update=on_order_update, on_position=acks.append) as stream:
            stream.subscribe_orders()
            stream.subscribe_positions()  # "o" has no ack: "pk" proves "o" was handled first
            wait_for(lambda: bool(acks))
            order_id = buy(client)
            broker.fill(order_id, price="101.50")  # on the test thread
            wait_for(lambda: any(u.status is OrderStatus.COMPLETE for u in updates))
        assert {u.order_id for u in updates} == {order_id}
        assert threading.current_thread().name not in threads  # delivered across threads


# --- the factories


def test_client_login_does_not_expire_during_a_test() -> None:
    # Not the real 06:00 IST expiry: a test that runs across 06:00 IST must not lose its login.
    with OfflineBroker("ABC123") as broker:
        before = datetime.now(IST)
        client = broker.client()
        token = client._creds.current_token()
        assert isinstance(token, AccessToken)
        assert token.expires_at >= before + timedelta(hours=24)
        assert not token.is_expired(next_expiry(before) + timedelta(minutes=1))
        assert token.access_token.get_secret_value().startswith("offline-token-")
        assert client.client_id == broker.client_id == "ABC123"
        assert client.profile() is not None  # no login step needed


def test_login_flow_against_the_offline_broker_returns_a_synthetic_token() -> None:
    with OfflineBroker() as broker:
        client = broker.client()
        assert client.login_url().startswith("https://offline.tradesmart.invalid/authorize")
        token = client.generate_session("offline-auth-code")
        assert token.access_token.get_secret_value().startswith("offline-token-")
        assert [r.endpoint for r in broker.requests] == ["GenAcsTok"]


def test_client_config_overrides_and_private_cache(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # the user's real cache would live here
    with OfflineBroker() as broker:
        broker.add_instrument("NSE", "22", "ACC-EQ")
        client = broker.client(max_retries=0, read_timeout=3.0)
        assert (client.config.max_retries, client.config.read_timeout) == (0, 3.0)
        assert client.config.base_url.startswith("https://offline.tradesmart.invalid/")
        path = client.download_instruments("NSE")
        assert path.parent == Path(str(client.config.instruments_dir))
        assert client.instruments("NSE")[0]["Token"] == "22"
        broker.fail_next("GetQuotes", drills.rate_limit())
        with pytest.raises(Exception, match="(?i)too many"):
            client.quote("NSE", "22")  # max_retries=0: the drill reaches the caller
    assert list(tmp_path.iterdir()) == []  # nothing written under HOME
    assert not path.exists()


async def test_async_client_uses_the_private_cache(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    with OfflineBroker() as broker:
        broker.add_instrument("NSE", "22", "ACC-EQ")
        async with broker.async_client(max_retries=0) as client:
            assert client.config.max_retries == 0
            rows = await client.instruments("NSE")
            path = await client.download_instruments("NSE")
        assert rows[0]["Token"] == "22"
        assert path.parent == Path(str(client.config.instruments_dir))
    assert list(tmp_path.iterdir()) == []
    assert not path.exists()


async def test_async_stream_receives_ticks() -> None:
    messages: list[StreamMessage] = []
    with OfflineBroker() as broker:
        broker.add_instrument("NSE", "22", "ACC-EQ")
        broker.set_quote("NSE", "22", last_price="101.50")
        stream = broker.async_stream(on_message=messages.append)
        assert isinstance(stream, AsyncTradeSmartStream)
        async with stream:
            await stream.subscribe_touchline("NSE|22")
            await await_for(lambda: any(isinstance(m, TouchlineAck) for m in messages))
            broker.tick("NSE", "22", last_price="102")
            await await_for(lambda: any(isinstance(m, TouchlineFeed) for m in messages))
    feed = next(m for m in messages if isinstance(m, TouchlineFeed))
    assert feed.last_price == Decimal("102")


def test_streams_from_an_offline_client_reach_the_offline_broker() -> None:
    # The documented route for stream settings: give them to client() and use from_client.
    with OfflineBroker() as broker:
        client = broker.client(heartbeat_interval=0.5)
        with TradeSmartStream.from_client(client) as stream:
            wait_for(lambda: stream.heartbeat_acks >= 1)
            assert stream.state is StreamState.CONNECTED


# --- state, orders and drills go to the broker's core


def test_setup_methods_feed_the_client() -> None:
    with OfflineBroker() as broker:
        client = broker.client()
        broker.add_instrument("NSE", "22", "ACC-EQ")
        broker.set_quote("NSE", "22", last_price="101.50")
        broker.set_candles("NSE", "22", [{"time": datetime(2026, 11, 10, 9, 15), "close": "100"}])
        broker.set_funds(cash="5000")
        broker.set_holdings([{"exch_tsym": [{"exch": "NSE", "tsym": "ACC-EQ", "token": "22"}]}])
        broker.set_positions([{"exch": "NSE", "tsym": "ACC-EQ", "prd": "I", "netqty": "5"}])
        broker.set_user(uname="Test User")
        broker.set_reply("GetOrderMargin", {"stat": "Ok", "cash": "1.00"})
        assert client.search_instruments("ACC")[0].tradingsymbol == "ACC-EQ"
        assert client.historical_data("NSE", "22")[0].intc == Decimal("100")
        assert client.margins().cash == Decimal("5000")
        assert len(client.holdings()) == 1
        assert client.positions()[0].net_quantity == 5
        assert client.profile().uname == "Test User"
        assert client.request("GetOrderMargin", {}, state_changing=False)["cash"] == "1.00"
        assert [r.endpoint for r in broker.requests][-1] == "GetOrderMargin"
        assert all(isinstance(r, OfflineRequest) for r in broker.requests)


def test_lifecycle_methods_drive_the_order_book() -> None:
    with OfflineBroker() as broker:
        client = broker.client()
        broker.default_place = "pending"
        first = buy(client)
        broker.accept(first)
        broker.fill(first, price="101.50", quantity=4)
        broker.cancel(first)
        second = buy(client)
        broker.reject(second, "RMS:Margin Exceeds")
        broker.default_place = "open"
        third = buy(client, order_type=OrderType.SL, trigger_price="101.00")
        broker.trigger(third)
        statuses = {o.id: (o.status, o.filled_quantity) for o in broker.orders}
        assert statuses == {
            first: ("CANCELED", 4),
            second: ("REJECTED", 0),
            third: ("OPEN", 0),
        }
        assert all(isinstance(o, OfflineOrder) for o in broker.orders)


def test_default_place_reject_uses_the_reject_message() -> None:
    with OfflineBroker() as broker:
        assert (broker.default_place, broker.default_reject_message) == (
            "open",
            "RMS:Rejected by OfflineBroker",
        )
        broker.default_place = "reject"
        broker.default_reject_message = "RMS:Blocked for the test"
        order_id = buy(broker.client())
        order = broker.orders[-1]
        assert (order.id, order.status, order.reject_reason) == (
            order_id,
            "REJECTED",
            "RMS:Blocked for the test",
        )


@pytest.mark.parametrize("bad", ["OPEN", "filled", "", None])
def test_default_place_rejects_unknown_values_at_once(bad: Any) -> None:
    broker = OfflineBroker()
    with pytest.raises(ValueError, match="'open', 'pending' or 'reject'"):
        broker.default_place = bad
    assert broker.default_place == "open"


def test_orders_and_requests_are_snapshots() -> None:
    with OfflineBroker() as broker:
        buy(broker.client())
        broker.orders.clear()
        broker.requests.clear()
        assert len(broker.orders) == 1 and len(broker.requests) == 1


def test_fail_next_arms_a_drill() -> None:
    with OfflineBroker() as broker:
        client = broker.client()
        broker.fail_next("PlaceOrder", drills.reject("RMS:Blocked"))
        with pytest.raises(OrderRejectedError, match="RMS:Blocked"):
            buy(client)
        assert broker.orders == []


# --- stream drills (by design: no overrides needed, nothing waits in real time)


def test_drop_stream_makes_the_client_reconnect_and_resubscribe_at_once() -> None:
    acks: list[Any] = []
    with OfflineBroker() as broker:
        broker.add_instrument("NSE", "22", "ACC-EQ")
        broker.set_quote("NSE", "22", last_price="101.50")
        with broker.stream(on_touchline=acks.append) as stream:
            stream.subscribe_touchline("NSE|22")
            wait_for(lambda: len(acks) == 1)
            started = time.monotonic()
            broker.drop_stream()
            wait_for(lambda: stream.reconnect_count == 1)
            # generous bound (load-proof); production backoff is 0.5 to 60 s
            assert time.monotonic() - started < 5
            wait_for(lambda: len([m for m in acks if isinstance(m, TouchlineAck)]) == 2)


def test_refuse_stream_login_refuses_the_next_connect_only() -> None:
    with OfflineBroker() as broker:
        broker.refuse_stream_login()
        with pytest.raises(AuthenticationError):
            broker.stream().start()
        with broker.stream() as stream:
            assert stream.state is StreamState.CONNECTED


def test_silence_stream_is_detected_in_about_a_second_then_resume_stream() -> None:
    with OfflineBroker() as broker:
        broker.silence_stream()
        with broker.stream() as stream:
            started = time.monotonic()
            wait_for(lambda: stream.reconnect_count >= 1)  # silence detected
            assert time.monotonic() - started < 4.0  # 2 x heartbeat_interval (0.5 s), with slack
            assert stream.heartbeat_acks == 0
            broker.resume_stream()
            wait_for(lambda: stream.heartbeat_acks >= 1)


async def test_async_stream_reconnects_at_once_after_drop_stream() -> None:
    with OfflineBroker() as broker:
        async with broker.async_stream() as stream:
            started = time.monotonic()
            broker.drop_stream()
            await await_for(lambda: stream.reconnect_count == 1)
            # generous bound (load-proof): production backoff is 0.5 to 60 s
            assert time.monotonic() - started < 5


# --- closing what the broker built


def test_close_stops_the_sync_streams_it_built() -> None:
    broker = OfflineBroker()
    stream = broker.stream()
    stream.start()
    idle = broker.stream()  # never started: closing it is a no-op
    broker.close()
    assert stream.state is StreamState.CLOSED
    assert idle.state is StreamState.DISCONNECTED


def test_close_can_be_retried_when_stopping_the_server_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = OfflineBroker()
    port = port_of(broker.client().config.ws_url)
    server = broker._server
    real_close = server.close

    def failing_close() -> None:
        raise RuntimeError("server did not stop")

    monkeypatch.setattr(server, "close", failing_close)
    with pytest.raises(RuntimeError, match="did not stop"):
        broker.close()
    with pytest.raises(RuntimeError, match="closed"):
        broker.client()  # refused although the first close() failed
    monkeypatch.setattr(server, "close", real_close)
    broker.close()  # retried, now succeeds
    assert refuses_connections(port)
    broker.close()  # and is idempotent afterwards


def test_a_close_failure_inside_with_keeps_the_users_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    broker = OfflineBroker()
    real_close = broker._server.close

    def failing_close() -> None:
        raise RuntimeError("server did not stop")

    monkeypatch.setattr(broker._server, "close", failing_close)
    with pytest.raises(ValueError, match="strategy bug"), broker:
        raise ValueError("strategy bug")
    (record,) = [r for r in caplog.records if r.exc_info]
    assert record.levelname == "WARNING" and record.name.startswith("tradesmart")
    assert "server did not stop" in str(record.exc_info[1])
    monkeypatch.setattr(broker._server, "close", real_close)
    broker.close()


def test_a_failed_folder_delete_logs_one_debug_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    broker = OfflineBroker()
    folder = Path(str(broker.client().config.instruments_dir))
    (folder / "a.txt").write_text("x", encoding="utf-8")
    (folder / "b.txt").write_text("x", encoding="utf-8")

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("file in use")

    with caplog.at_level("DEBUG", logger="tradesmart"):
        monkeypatch.setattr(os, "unlink", refuse)
        broker.close()  # no error: a leftover folder only costs disk space
        monkeypatch.undo()
    lines = [r for r in caplog.records if "could not delete" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelname == "DEBUG"
    assert lines[0].name.startswith("tradesmart")
    assert folder.exists()
    shutil.rmtree(folder)


# --- final review fixes


def symbols(rows: list[dict[str, str]]) -> set[str]:
    return {row["TradingSymbol"] for row in rows}


def test_an_instrument_added_later_reaches_the_script_master() -> None:
    with OfflineBroker() as broker:
        broker.add_instrument("NSE", token="22", symbol="ACC-EQ")
        client = broker.client()
        assert symbols(client.instruments("NSE")) == {"ACC-EQ"}  # the file is now cached
        broker.add_instrument("NSE", token="25", symbol="ADANIENT-EQ")
        both = {"ACC-EQ", "ADANIENT-EQ"}
        assert symbols(client.instruments("NSE")) == both  # the same client
        assert symbols(broker.client().instruments("NSE")) == both  # a new client


async def test_an_instrument_added_later_reaches_the_script_master_async() -> None:
    with OfflineBroker() as broker:
        broker.add_instrument("NSE", token="22", symbol="ACC-EQ")
        async with broker.async_client() as client:
            assert symbols(await client.instruments("NSE")) == {"ACC-EQ"}
            broker.add_instrument("NSE", token="25", symbol="ADANIENT-EQ")
            both = {"ACC-EQ", "ADANIENT-EQ"}
            assert symbols(await client.instruments("NSE")) == both
            async with broker.async_client() as fresh:
                assert symbols(await fresh.instruments("NSE")) == both


def test_an_option_added_later_reaches_option_greeks() -> None:
    expiry = datetime.now(IST).date() + timedelta(days=30)
    stem = f"NIFTY{expiry:%d}{expiry:%b}{expiry:%y}".upper()

    def add_call(token: str, strike: int) -> str:
        symbol = f"{stem}C{strike}"
        broker.add_instrument(
            "NFO", token, symbol, lot_size=75, instrument="OPTIDX", expiry=expiry,
            strike=strike, option_type="CE", underlying="NIFTY",
        )  # fmt: skip
        broker.set_quote("NFO", token, last_price="320.00")
        return symbol

    with OfflineBroker() as broker:
        broker.add_instrument(
            "NFO", "1001", f"{stem}F", lot_size=75, instrument="FUTIDX", expiry=expiry,
            underlying="NIFTY",
        )  # fmt: skip
        broker.set_quote("NFO", "1001", last_price="24050.00")
        client = broker.client()
        assert client.option_greeks("NFO", add_call("2001", 24000)).status == "ok"
        assert client.option_greeks("NFO", add_call("2002", 24100)).status == "ok"


def test_set_positions_accepts_product_names() -> None:
    with OfflineBroker() as broker:
        broker.add_instrument("NSE", token="22", symbol="ACC-EQ")
        broker.set_positions([{"exch": "NSE", "tsym": "ACC-EQ", "prd": "MIS", "netqty": "5"}])
        client = broker.client()
        order_id = buy(client, quantity=5, transaction_type=TransactionType.SELL)
        broker.fill(order_id, price="101.50")
        rows = client.positions()
        assert len(rows) == 1  # the fill landed on the MIS row, not a new one
        assert rows[0].product is Product.MIS and rows[0].net_quantity == 0


def test_set_positions_rejects_an_unknown_product() -> None:
    with OfflineBroker() as broker, pytest.raises(ValueError, match="unknown product 'XYZ'"):
        broker.set_positions([{"exch": "NSE", "tsym": "ACC-EQ", "prd": "XYZ"}])


@pytest.mark.parametrize("name", ["ws_url", "base_url", "authorize_url", "script_master_base_url"])
def test_client_refuses_the_urls_the_offline_broker_sets(name: str) -> None:
    with OfflineBroker() as broker:
        for factory in (broker.client, broker.async_client):
            with pytest.raises(ValueError, match=f"OfflineBroker sets {name} itself"):
                factory(**{name: "wss://example.invalid/"})


def _client_of_a_dropped_broker() -> TradeSmart:
    broker = OfflineBroker()
    broker.add_instrument("NSE", "22", "ACC-EQ")
    return broker.client()


def _connect_and_stop(stream: TradeSmartStream) -> None:
    with stream:
        wait_for(lambda: stream.state is StreamState.CONNECTED)


def test_a_client_keeps_its_broker_alive() -> None:
    before = stream_threads()
    client = _client_of_a_dropped_broker()
    gc.collect()
    folder = Path(str(client.config.instruments_dir))
    assert folder.is_dir()
    assert client.instruments("NSE")[0]["Token"] == "22"
    _connect_and_stop(TradeSmartStream.from_client(client))  # the stream server still runs
    del client
    gc.collect()
    wait_for(lambda: not folder.exists())  # the last client gone: the broker's cleanup ran
    wait_for(lambda: not (stream_threads() - before))


async def test_an_async_client_keeps_its_broker_alive() -> None:
    def make() -> AsyncTradeSmart:
        broker = OfflineBroker()
        broker.add_instrument("NSE", "22", "ACC-EQ")
        return broker.async_client()

    client = make()
    gc.collect()
    assert Path(str(client.config.instruments_dir)).is_dir()  # the broker's folder is still there
    async with client:
        assert (await client.instruments("NSE"))[0]["Token"] == "22"


def test_a_stream_keeps_its_broker_alive() -> None:
    def make() -> TradeSmartStream:
        return OfflineBroker().stream()

    stream = make()
    gc.collect()
    _connect_and_stop(stream)


async def test_an_async_stream_keeps_its_broker_alive() -> None:
    def make() -> AsyncTradeSmartStream:
        return OfflineBroker().async_stream()

    stream = make()
    gc.collect()
    async with stream:
        await await_for(lambda: stream.state is StreamState.CONNECTED)


def test_a_broker_never_closed_stops_its_server_when_collected() -> None:
    before = stream_threads()
    broker = OfflineBroker()
    port = port_of(broker.client().config.ws_url)  # starts the server
    assert stream_threads() - before
    del broker
    gc.collect()
    wait_for(lambda: not (stream_threads() - before))
    assert refuses_connections(port)
