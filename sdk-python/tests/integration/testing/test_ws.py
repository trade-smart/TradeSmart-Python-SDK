"""The offline broker's stream server, driven through the real sync and async streams (loopback).

Every wait is on a condition. To prove that something did NOT arrive, a test sends a later request
whose reply (``pk``) is queued behind anything the earlier action could have pushed on the same
connection; once that reply is in, the absence is certain.
"""

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from websockets.asyncio.client import ClientConnection, connect

from tradesmart import (
    AsyncTradeSmartStream,
    AuthenticationError,
    ClientConfig,
    StreamState,
    TradeSmartStream,
)
from tradesmart.enums import OrderStatus
from tradesmart.models.stream import (
    ConnectAck,
    DepthAck,
    DepthFeed,
    HeartbeatAck,
    OrderUpdate,
    PositionAck,
    PositionUpdate,
    StreamMessage,
    TouchlineAck,
    TouchlineFeed,
    UnknownMessage,
    UnsubscribeAck,
)
from tradesmart.stream.protocol import parse_message
from tradesmart.testing._state import BrokerState
from tradesmart.testing._ws import OfflineStreamServer

TOKEN = "offline-token-ws-test"
WAIT = 30.0
ORDER = {
    "uid": "OFFLINE01",
    "actid": "OFFLINE01",
    "exch": "NSE",
    "tsym": "ACC-EQ",
    "qty": "10",
    "prc": "101.50",
    "prd": "I",
    "trantype": "B",
    "prctyp": "LMT",
    "ret": "DAY",
}


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


def cfg(server: OfflineStreamServer, **overrides: Any) -> ClientConfig:
    settings: dict[str, Any] = {
        "ws_url": server.url,
        "reconnect_backoff_base": 0.01,
        "reconnect_backoff_cap": 0.02,
    }
    settings.update(overrides)
    return ClientConfig(**settings)


class Recorder:
    """Collects what a stream delivers; the fixture checks every message parsed."""

    def __init__(self) -> None:
        self.messages: list[StreamMessage] = []
        self.errors: list[BaseException] = []

    def of(self, kind: Any) -> list[Any]:
        return [m for m in list(self.messages) if isinstance(m, kind)]

    def wait(self, kind: Any, count: int = 1) -> list[Any]:
        wait_for(lambda: len(self.of(kind)) >= count)
        return self.of(kind)

    async def await_(self, kind: Any, count: int = 1) -> list[Any]:
        await await_for(lambda: len(self.of(kind)) >= count)
        return self.of(kind)

    def stream(self, server: OfflineStreamServer, **overrides: Any) -> TradeSmartStream:
        return TradeSmartStream(
            "OFFLINE01",
            TOKEN,
            config=cfg(server, **overrides),
            on_message=self.messages.append,
            on_error=self.errors.append,
        )

    def async_stream(self, server: OfflineStreamServer, **overrides: Any) -> AsyncTradeSmartStream:
        return AsyncTradeSmartStream(
            "OFFLINE01", TOKEN, config=cfg(server, **overrides), on_message=self.messages.append
        )

    def barrier(self, stream: TradeSmartStream) -> None:
        """Wait until everything pushed before this call has been delivered (see module doc)."""
        acks = len(self.of(PositionAck))
        stream.subscribe_positions()
        self.wait(PositionAck, acks + 1)


@pytest.fixture
def state() -> BrokerState:
    broker = BrokerState()
    broker.add_instrument("NSE", "22", "ACC-EQ")
    broker.set_quote("NSE", "22", last_price="101.50", bid="101.45", ask="101.55")
    return broker


@pytest.fixture
def server(state: BrokerState) -> Iterator[OfflineStreamServer]:
    server = OfflineStreamServer(state)
    server.start()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def rec() -> Iterator[Recorder]:
    recorder = Recorder()
    yield recorder
    assert not recorder.of(UnknownMessage), "every pushed message parses with the SDK models"


# --- server basics -------------------------------------------------------------------------


def test_url_is_loopback_on_the_noren_path(server: OfflineStreamServer) -> None:
    match = re.fullmatch(r"ws://127\.0\.0\.1:(\d+)/NorenWSAPI/", server.url)
    assert match is not None and int(match.group(1)) > 0


def test_two_servers_listen_on_different_ports(state: BrokerState) -> None:
    first, second = OfflineStreamServer(state), OfflineStreamServer(BrokerState())
    first.start()
    second.start()
    try:
        assert first.url != second.url
    finally:
        first.close()
        second.close()


def test_server_thread_is_daemon_and_close_is_idempotent(
    state: BrokerState, caplog: pytest.LogCaptureFixture
) -> None:
    OfflineStreamServer(state).close()  # never started: nothing to do
    before = set(threading.enumerate())
    server = OfflineStreamServer(state)
    server.start()
    started = set(threading.enumerate()) - before
    assert started and all(thread.daemon for thread in started)
    with TradeSmartStream("OFFLINE01", TOKEN, config=cfg(server, reconnect_max_attempts=0)):
        server.close()  # with a client still connected
        server.close()
    assert not any(thread.is_alive() for thread in started)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tradesmart.testing"):
        state.tick("NSE", "22", last_price="105")  # the listener is now a no-op
    assert not [r for r in caplog.records if r.name.startswith("tradesmart.testing")]


async def test_close_does_not_wait_for_a_client_on_the_calling_thread(state: BrokerState) -> None:
    # The async stream lives on this thread's loop, which close() blocks: no closing handshake.
    server = OfflineStreamServer(state)
    server.start()
    stream = AsyncTradeSmartStream("OFFLINE01", TOKEN, config=cfg(server, reconnect_max_attempts=0))
    await stream.connect()
    server.close()
    await stream.close()


async def test_server_never_logs_the_access_token(
    server: OfflineStreamServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    quiet_client = logging.getLogger("tests.offline_ws.raw_client")
    quiet_client.setLevel(logging.WARNING)  # only the server's logging is under test
    async with connect(server.url, logger=quiet_client) as ws:
        await ws.send(json.dumps({"t": "a", "uid": "OFFLINE01", "accesstoken": TOKEN}))
        assert json.loads(await ws.recv())["t"] == "ak"
    assert TOKEN not in caplog.text


# --- raw protocol: every reply and push parses with the SDK models ---------------------------


async def _receive_until(ws: ClientConnection, last: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while not frames or frames[-1]["t"] != last:
        frames.append(json.loads(await asyncio.wait_for(ws.recv(), WAIT)))
    return frames


@pytest.fixture
async def raw(server: OfflineStreamServer) -> AsyncIterator[ClientConnection]:
    async with connect(server.url) as ws:
        yield ws


async def test_every_request_is_answered_as_documented(raw: ClientConnection) -> None:
    for request in (
        {"t": "a", "uid": "OFFLINE01", "actid": "OFFLINE01", "source": "API", "accesstoken": TOKEN},
        {"t": "t", "k": "NSE|22#NSE|23"},
        {"t": "d", "k": "NSE|22"},
        {"t": "o", "actid": "OFFLINE01"},  # documented: no acknowledgement
        {"t": "p", "uid": "OFFLINE01"},
        {"t": "h"},
        {"t": "u", "k": "NSE|22#NSE|23"},
        {"t": "ud", "k": "NSE|22"},
        {"t": "uo"},
        {"t": "up"},
    ):
        await raw.send(json.dumps(request))
    frames = await _receive_until(raw, "upk")
    expected = ["ak", "tk", "tk", "dk", "pk", "hk", "uk", "udk", "uok", "upk"]
    assert [f["t"] for f in frames] == expected
    ak, tk22, tk23, dk, pk, hk, uk, udk, uok, upk = [parse_message(f) for f in frames]
    assert isinstance(ak, ConnectAck) and ak.status == "OK" and ak.user_id == "OFFLINE01"
    assert isinstance(tk22, TouchlineAck) and tk22.tradingsymbol == "ACC-EQ"
    assert tk22.last_price == Decimal("101.50") and tk22.bp1 == Decimal("101.45")
    assert isinstance(tk23, TouchlineAck) and tk23.key == "NSE|23"  # no quote set: still acked
    assert isinstance(dk, DepthAck) and dk.sp1 == Decimal("101.55")
    assert isinstance(pk, PositionAck) and pk.user_id == "OFFLINE01"
    assert isinstance(hk, HeartbeatAck) and hk.ft is not None
    assert isinstance(uk, UnsubscribeAck) and uk.k == "NSE|22#NSE|23"
    assert all(isinstance(m, UnsubscribeAck) for m in (udk, uok, upk))


async def test_every_pushed_event_parses_with_the_sdk_models(
    raw: ClientConnection, state: BrokerState
) -> None:
    await raw.send(json.dumps({"t": "a", "uid": "OFFLINE01", "accesstoken": TOKEN}))
    for request in ({"t": "t", "k": "NSE|22"}, {"t": "d", "k": "NSE|22"}, {"t": "o"}):
        await raw.send(json.dumps(request))
    await raw.send(json.dumps({"t": "p", "uid": "OFFLINE01"}))
    await _receive_until(raw, "pk")  # all four subscriptions are registered
    state.tick("NSE", "22", last_price="102.00", volume=500)
    order = state.place(ORDER)
    state.fill(order.id, price="101.50")
    frames = await _receive_until(raw, "pm")
    assert [f["t"] for f in frames] == ["tf", "df", "om", "om", "om", "pm"]
    messages = [parse_message(f) for f in frames]
    assert not [m for m in messages if isinstance(m, UnknownMessage)]
    tf, df, *oms, pm = messages
    assert isinstance(tf, TouchlineFeed) and tf.last_price == Decimal("102.00")
    assert isinstance(df, DepthFeed) and df.volume == 500
    assert all(isinstance(m, OrderUpdate) and m.order_id == order.id for m in oms)
    assert isinstance(pm, PositionUpdate) and pm.net_quantity == 10


async def test_frames_that_are_not_json_objects_are_ignored(raw: ClientConnection) -> None:
    for frame in ("not json", "[1, 2]", json.dumps({"t": "h"})):
        await raw.send(frame)
    assert json.loads(await asyncio.wait_for(raw.recv(), WAIT))["t"] == "hk"


async def test_refused_login_is_answered_not_ok_once(
    server: OfflineStreamServer, state: BrokerState
) -> None:
    state.refuse_login_once = True
    for expected in ("Not_Ok", "OK"):
        async with connect(server.url) as ws:
            await ws.send(json.dumps({"t": "a", "uid": "OFFLINE01", "accesstoken": TOKEN}))
            assert json.loads(await ws.recv()) == {"t": "ak", "uid": "OFFLINE01", "s": expected}
    assert state.refuse_login_once is False


# --- sync stream -----------------------------------------------------------------------------


def test_touchline_ack_carries_the_quote_then_a_tick_arrives_as_a_feed(
    server: OfflineStreamServer, state: BrokerState, rec: Recorder
) -> None:
    with rec.stream(server) as stream:
        stream.subscribe_touchline("NSE|22")
        (ack,) = rec.wait(TouchlineAck)
        assert ack.key == "NSE|22" and ack.tradingsymbol == "ACC-EQ"
        assert ack.last_price == Decimal("101.50")
        state.tick("NSE", "22", last_price="102.25")
        (feed,) = rec.wait(TouchlineFeed)
        assert feed.key == "NSE|22" and feed.last_price == Decimal("102.25")
        rec.barrier(stream)
        assert not rec.of((DepthAck, DepthFeed))  # depth was not subscribed
    assert not rec.errors


def test_depth_ack_carries_the_quote_then_a_tick_arrives_as_a_depth_feed(
    server: OfflineStreamServer, state: BrokerState, rec: Recorder
) -> None:
    with rec.stream(server) as stream:
        stream.subscribe_depth("NSE|22")
        (ack,) = rec.wait(DepthAck)
        assert ack.bp1 == Decimal("101.45") and ack.sp1 == Decimal("101.55")
        state.tick("NSE", "22", bid="101.50")
        (feed,) = rec.wait(DepthFeed)
        assert feed.key == "NSE|22" and feed.bp1 == Decimal("101.50")
        rec.barrier(stream)
        assert not rec.of((TouchlineAck, TouchlineFeed))  # touchline was not subscribed


def test_order_subscription_receives_om_after_a_fill(
    server: OfflineStreamServer, state: BrokerState, rec: Recorder
) -> None:
    with rec.stream(server) as stream:
        stream.subscribe_orders()
        rec.barrier(stream)  # the order subscription is registered
        order = state.place(ORDER)
        state.fill(order.id, price="101.50")
        wait_for(lambda: any(m.status is OrderStatus.COMPLETE for m in rec.of(OrderUpdate)))
        updates = rec.of(OrderUpdate)
        assert [m.order_id for m in updates] == [order.id] * len(updates)
        assert updates[-1].filled_quantity == 10
        (position,) = rec.wait(PositionUpdate)
        assert position.net_quantity == 10


def test_updates_reach_only_matching_subscriptions(
    server: OfflineStreamServer, state: BrokerState, rec: Recorder
) -> None:
    with rec.stream(server) as stream:
        stream.subscribe_touchline("NSE|23")
        rec.barrier(stream)  # positions only; no orders, no NSE|22
        state.tick("NSE", "22", last_price="103")
        state.fill(state.place(ORDER).id, price="101.50")
        rec.wait(PositionUpdate)  # pushed after the tick and the om messages
        assert not rec.of((TouchlineFeed, DepthFeed, OrderUpdate))


def test_unsubscribed_updates_stop(
    server: OfflineStreamServer, state: BrokerState, rec: Recorder
) -> None:
    with rec.stream(server) as stream:
        stream.subscribe_touchline("NSE|22")
        stream.subscribe_depth("NSE|22")
        stream.subscribe_orders()
        stream.unsubscribe_touchline("NSE|22")
        stream.unsubscribe_depth("NSE|22")
        stream.unsubscribe_orders()
        rec.wait(UnsubscribeAck, 3)
        rec.barrier(stream)
        state.tick("NSE", "22", last_price="104")
        state.fill(state.place(ORDER).id, price="101.50")
        rec.wait(PositionUpdate)
        assert not rec.of((TouchlineFeed, DepthFeed, OrderUpdate))


def test_drop_all_makes_the_client_reconnect_and_resubscribe(
    server: OfflineStreamServer, state: BrokerState, rec: Recorder
) -> None:
    with rec.stream(server) as stream:
        stream.subscribe_touchline("NSE|22")
        rec.wait(TouchlineAck)
        server.drop_all()
        wait_for(lambda: stream.reconnect_count == 1)
        rec.wait(TouchlineAck, 2)  # the restored subscription was acknowledged again
        state.tick("NSE", "22", last_price="106")
        (feed,) = rec.wait(TouchlineFeed)
        assert feed.last_price == Decimal("106")
        assert stream.reconnect_count == 1 and stream.state is StreamState.CONNECTED
    assert not rec.errors


def test_refused_login_raises_authentication_error_once(
    server: OfflineStreamServer, state: BrokerState, rec: Recorder
) -> None:
    state.refuse_login_once = True
    with pytest.raises(AuthenticationError):
        rec.stream(server).start()
    with rec.stream(server) as stream:  # refused once only
        assert stream.state is StreamState.CONNECTED


def test_heartbeats_are_answered(server: OfflineStreamServer, rec: Recorder) -> None:
    with rec.stream(server, heartbeat_interval=0.5) as stream:
        wait_for(lambda: stream.heartbeat_acks >= 2)


def test_silenced_server_makes_the_client_detect_silence_and_reconnect(
    server: OfflineStreamServer, state: BrokerState, rec: Recorder
) -> None:
    state.silenced = True
    with rec.stream(server, heartbeat_interval=0.1) as stream:  # silent after 0.2 s
        wait_for(lambda: stream.reconnect_count >= 1)
        assert stream.heartbeat_acks == 0
        state.silenced = False
        wait_for(lambda: stream.heartbeat_acks >= 1)
        assert stream.state is StreamState.CONNECTED


# --- async stream ----------------------------------------------------------------------------


async def test_async_stream_touchline_and_depth(
    server: OfflineStreamServer, state: BrokerState, rec: Recorder
) -> None:
    async with rec.async_stream(server) as stream:
        await stream.subscribe_touchline("NSE|22")
        await stream.subscribe_depth("NSE|22")
        await rec.await_(TouchlineAck)
        await rec.await_(DepthAck)
        state.tick("NSE", "22", last_price="107")
        (tf,) = await rec.await_(TouchlineFeed)
        (df,) = await rec.await_(DepthFeed)
        assert tf.last_price == Decimal("107") and df.last_price == Decimal("107")


async def test_async_stream_receives_order_and_position_updates(
    server: OfflineStreamServer, state: BrokerState, rec: Recorder
) -> None:
    async with rec.async_stream(server) as stream:
        await stream.subscribe_orders()
        await stream.subscribe_positions()
        await rec.await_(PositionAck)  # sent after "o" on the same connection
        order = state.place(ORDER)
        state.fill(order.id, price="101.50")
        await await_for(lambda: any(m.status is OrderStatus.COMPLETE for m in rec.of(OrderUpdate)))
        (position,) = await rec.await_(PositionUpdate)
        assert position.net_quantity == 10


async def test_async_refused_login_on_reconnect_is_terminal(
    server: OfflineStreamServer, state: BrokerState
) -> None:
    stream = AsyncTradeSmartStream("OFFLINE01", TOKEN, config=cfg(server))
    await stream.connect()
    try:
        state.refuse_login_once = True
        server.drop_all()  # returns at once although this loop cannot answer a close handshake
        with pytest.raises(AuthenticationError):
            async for _ in stream:
                pass
        assert stream.state is StreamState.CLOSED
    finally:
        await stream.close()
