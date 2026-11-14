"""Stream fixes from the 2026-11-10 review: async hooks, bounded close, expiry before
reconnect, heartbeat-ack counter and non-iterable instrument arguments."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import re
import threading
import time
import warnings
from typing import Any

import pytest

from tests.integration.ws_server import MockWsServer, wait_until, wait_until_sync
from tradesmart import (
    AccessToken,
    AuthenticationError,
    ClientConfig,
    RequestValidationError,
)
from tradesmart.stream import AsyncTradeSmartStream, StreamState, TradeSmartStream


def cfg(url: str, **overrides: Any) -> ClientConfig:
    values: dict[str, Any] = {
        "ws_url": url,
        "heartbeat_interval": 0.2,
        "reconnect_backoff_base": 0.01,
        "reconnect_backoff_cap": 0.02,
        "ws_auth_timeout": 1.0,
    }
    values.update(overrides)
    return ClientConfig(**values)


def fresh_token() -> AccessToken:
    """An unexpired synthetic token (valid until the next 06:00 IST)."""
    return AccessToken.from_response({"stat": "Ok", "access_token": "synthetic-token"})


# --- a peer that never answers the close frame -----------------------------------------

_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class NoCloseReplyServer:
    """Speaks just enough WebSocket to authenticate and answer heartbeats, but never
    answers a close frame and never closes the TCP connection itself."""

    def __init__(self) -> None:
        self.url = ""
        self.close_frames = 0
        self._writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.base_events.Server | None = None

    async def __aenter__(self) -> NoCloseReplyServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/"
        return self

    async def __aexit__(self, *exc: object) -> None:
        for writer in self._writers:
            writer.transport.abort()
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @staticmethod
    def _frame(msg: dict[str, Any]) -> bytes:
        data = json.dumps(msg).encode()
        assert len(data) < 126
        return bytes([0x81, len(data)]) + data

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            match = re.search(rb"(?i)sec-websocket-key:\s*(\S+)", request)
            assert match is not None
            # RFC 6455 handshake hash (protocol-mandated, not used for security)
            digest = hashlib.sha1(match.group(1) + _WS_GUID, usedforsecurity=False).digest()
            accept = base64.b64encode(digest)
            writer.write(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            while True:
                head = await reader.readexactly(2)
                opcode, length = head[0] & 0x0F, head[1] & 0x7F
                if length == 126:
                    length = int.from_bytes(await reader.readexactly(2), "big")
                elif length == 127:
                    length = int.from_bytes(await reader.readexactly(8), "big")
                mask = await reader.readexactly(4)  # client frames are always masked
                body = await reader.readexactly(length)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
                if opcode == 0x8:
                    self.close_frames += 1  # deliberately never answered
                elif opcode == 0x1:
                    t = json.loads(payload).get("t")
                    if t == "a":
                        writer.write(self._frame({"t": "ak", "s": "Ok"}))
                    elif t == "h":
                        writer.write(self._frame({"t": "hk"}))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


# --- Minor 1: async hooks on the async stream; coroutine callbacks rejected by the sync one


async def test_async_on_state_change_coroutine_is_run():
    seen: list[tuple[StreamState, int]] = []

    async def on_state(state: StreamState) -> None:
        await asyncio.sleep(0)
        seen.append((state, stream.reconnect_count))

    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream(
            "TEST123", "tok", config=cfg(srv.url), on_state_change=on_state
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)  # "coroutine ... never awaited"
            async with stream:
                await wait_until(lambda: (StreamState.CONNECTED, 0) in seen)
                await srv.drop_all()
                await wait_until(lambda: (StreamState.CONNECTED, 1) in seen)
    states = [state for state, _ in seen]
    assert states == [
        StreamState.CONNECTING,
        StreamState.AUTHENTICATING,
        StreamState.CONNECTED,
        StreamState.RECONNECTING,
        StreamState.CONNECTED,
        StreamState.CLOSED,
    ]


async def test_async_on_state_change_catch_up_after_reconnect():
    caught_up = asyncio.Event()

    async def catch_up() -> None:  # stands in for `await client.orders()`
        await asyncio.sleep(0.01)
        caught_up.set()

    async def on_state(state: StreamState) -> None:
        if state is StreamState.CONNECTED and stream.reconnect_count:
            await catch_up()

    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream(
            "TEST123", "tok", config=cfg(srv.url), on_state_change=on_state
        )
        async with stream:
            assert not caught_up.is_set()
            await srv.drop_all()
            await asyncio.wait_for(caught_up.wait(), 2)


async def test_async_on_state_change_coroutine_error_is_logged(caplog):
    caplog.set_level(logging.ERROR, logger="tradesmart.stream")

    async def on_state(state: StreamState) -> None:
        raise ValueError("boom in async on_state_change")

    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream(
            "TEST123", "tok", config=cfg(srv.url), on_state_change=on_state
        )
        async with stream:
            await wait_until(lambda: "on_state_change callback raised" in caplog.text)
            assert stream.state is StreamState.CONNECTED  # the stream carries on


async def test_async_close_waits_for_the_closed_state_coroutine():
    seen: list[StreamState] = []

    async def on_state(state: StreamState) -> None:
        await asyncio.sleep(0.05)
        seen.append(state)

    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream(
            "TEST123", "tok", config=cfg(srv.url), on_state_change=on_state
        )
        await stream.connect()
        await stream.close()
        assert seen[-1] is StreamState.CLOSED


async def test_async_close_from_the_state_coroutine_does_not_deadlock():
    async def on_state(state: StreamState) -> None:
        if state is StreamState.CONNECTED:
            await stream.close()

    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream(
            "TEST123", "tok", config=cfg(srv.url), on_state_change=on_state
        )
        await stream.connect()
        await wait_until(lambda: stream.state is StreamState.CLOSED)
        await asyncio.wait_for(stream.close(), 2)


# --- fix round 1 (task review I2): close() never hangs on an async on_state_change ------


@pytest.fixture
def short_close_timeout(monkeypatch):
    from tradesmart.stream import async_stream

    monkeypatch.setattr(async_stream, "CLOSE_TIMEOUT", 0.3)
    return 0.3


@pytest.mark.parametrize(
    "closer",
    [
        lambda s: asyncio.gather(s.close()),
        lambda s: asyncio.shield(s.close()),
        lambda s: asyncio.ensure_future(s.close()),
        lambda s: asyncio.wait_for(s.close(), 5),
    ],
    ids=["gather", "shield", "task", "wait_for"],
)
async def test_async_close_from_a_child_task_of_the_state_callback_returns(closer):
    returned: list[float] = []

    async def on_state(state: StreamState) -> None:
        if state is StreamState.CONNECTED:
            began = time.monotonic()
            await closer(stream)
            returned.append(time.monotonic() - began)

    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream(
            "TEST123", "tok", config=cfg(srv.url), on_state_change=on_state
        )
        await stream.connect()
        await wait_until(lambda: bool(returned), timeout=30)
    assert stream.state is StreamState.CLOSED
    assert returned[0] < 1.0  # no wait on the callback that is itself closing


async def test_async_close_cancels_a_state_callback_that_never_ends(short_close_timeout, caplog):
    caplog.set_level(logging.WARNING, logger="tradesmart.stream")
    cancelled = asyncio.Event()

    async def on_state(state: StreamState) -> None:
        if state is StreamState.CONNECTED:
            try:
                await asyncio.sleep(3600)  # e.g. long-lived work started on connect
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream(
            "TEST123", "tok", config=cfg(srv.url), on_state_change=on_state
        )
        await stream.connect()
        began = time.monotonic()
        await stream.close()
        elapsed = time.monotonic() - began
        await asyncio.wait_for(cancelled.wait(), 1)
    assert elapsed < short_close_timeout + 1.5, f"close() took {elapsed:.2f}s"
    warnings_ = [r for r in caplog.records if "on_state_change" in r.getMessage()]
    assert len(warnings_) == 1 and warnings_[0].levelno == logging.WARNING
    assert "cancelled" in warnings_[0].getMessage()


async def test_async_close_from_a_task_the_callback_returned_does_not_wait(caplog):
    # re-review m1: on_state_change is a plain function returning its own task
    caplog.set_level(logging.WARNING, logger="tradesmart.stream")
    finished: list[float] = []

    async def handler(state: StreamState) -> None:
        if state is StreamState.CONNECTED:
            began = time.monotonic()
            await stream.close()
            finished.append(time.monotonic() - began)

    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream(
            "TEST123",
            "tok",
            config=cfg(srv.url),
            on_state_change=lambda s: asyncio.ensure_future(handler(s)),
        )
        await stream.connect()
        await wait_until(lambda: bool(finished), timeout=30)
    assert finished[0] < 1.0
    assert "on_state_change" not in caplog.text  # nothing was cancelled


async def test_async_close_from_a_worker_still_bounds_the_state_callbacks(
    short_close_timeout, caplog
):
    # re-review m2: the CONNECTED callback starts a worker and keeps running; the worker
    # closes the stream. close() returns at once, but the callback is still cancelled
    # after the bound and the CLOSED notification runs.
    caplog.set_level(logging.WARNING, logger="tradesmart.stream")
    closed_by_worker = asyncio.Event()
    cancelled = asyncio.Event()
    seen: list[StreamState] = []
    workers: list[asyncio.Future[None]] = []

    async def worker() -> None:
        await stream.close()
        closed_by_worker.set()

    async def on_state(state: StreamState) -> None:
        seen.append(state)
        if state is StreamState.CONNECTED:
            workers.append(asyncio.ensure_future(worker()))
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream(
            "TEST123", "tok", config=cfg(srv.url), on_state_change=on_state
        )
        await stream.connect()
        await asyncio.wait_for(closed_by_worker.wait(), 1)
        began = time.monotonic()
        await asyncio.wait_for(cancelled.wait(), short_close_timeout + 1.5)
        assert time.monotonic() - began < short_close_timeout + 1.5
    assert StreamState.CLOSED in seen
    assert sum("on_state_change" in r.getMessage() for r in caplog.records) == 1


async def test_async_concurrent_closes_log_the_cancel_warning_once(short_close_timeout, caplog):
    caplog.set_level(logging.WARNING, logger="tradesmart.stream")

    async def on_state(state: StreamState) -> None:
        if state is StreamState.CONNECTED:
            await asyncio.sleep(3600)

    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream(
            "TEST123", "tok", config=cfg(srv.url), on_state_change=on_state
        )
        await stream.connect()
        await asyncio.wait_for(asyncio.gather(stream.close(), stream.close()), 2)
    assert sum("on_state_change" in r.getMessage() for r in caplog.records) == 1


# --- fix round 1 (task review Minor 2): sync callbacks that return an awaitable ---------


def test_sync_callback_returning_a_coroutine_is_reported_once_and_closed(threaded_server, caplog):
    caplog.set_level(logging.ERROR, logger="tradesmart.stream")

    async def handler(message: object) -> None:
        pass

    created: list[Any] = []

    def on_message(message: object) -> Any:  # a plain function returning a coroutine
        coro = handler(message)
        created.append(coro)
        return coro

    errors: list[BaseException] = []
    stream = TradeSmartStream(
        "TEST123",
        "tok",
        config=cfg(threaded_server.url),
        on_message=on_message,
        on_error=errors.append,
    )
    with stream:
        stream.subscribe_touchline(["NSE|22", "NSE|23", "NSE|24"])
        wait_until_sync(lambda: len(created) == 3)
    # every coroutine was closed (an unclosed, never-started one still has a frame) ...
    assert all(coro.cr_frame is None for coro in created)
    # ... and the mistake is reported once, not on every message
    assert len(errors) == 1 and isinstance(errors[0], TypeError)
    assert "on_message" in str(errors[0]) and "awaitable" in str(errors[0])
    logged = [r for r in caplog.records if r.exc_info and "awaitable" in str(r.exc_info[1])]
    assert len(logged) == 1 and logged[0].levelno == logging.ERROR


def test_sync_on_error_returning_a_coroutine_is_logged(threaded_server, caplog):
    caplog.set_level(logging.ERROR, logger="tradesmart.stream")

    async def report(exc: BaseException) -> None:
        pass

    def on_message(message: object) -> None:
        raise ValueError("boom")

    stream = TradeSmartStream(
        "TEST123",
        "tok",
        config=cfg(threaded_server.url, heartbeat_interval=5.0),
        on_message=on_message,
        on_error=lambda exc: report(exc),
    )
    with stream:
        stream.subscribe_touchline("NSE|22")
        wait_until_sync(
            lambda: "on_error callback returned an awaitable" in caplog.text, timeout=30
        )


_SYNC_CALLBACKS = (
    "on_touchline",
    "on_depth",
    "on_order_update",
    "on_position",
    "on_connect",
    "on_disconnect",
    "on_error",
    "on_message",
    "on_reconnect",
)


@pytest.mark.parametrize("name", _SYNC_CALLBACKS)
def test_sync_stream_rejects_coroutine_callbacks(name: str):
    async def callback(*args: object) -> None:
        pass

    with pytest.raises(TypeError, match=name):
        TradeSmartStream("TEST123", "tok", **{name: callback})


def test_sync_stream_rejects_async_callable_objects():
    class Handler:
        async def __call__(self, message: object) -> None:
            pass

    with pytest.raises(TypeError, match="on_message"):
        TradeSmartStream("TEST123", "tok", on_message=Handler())


def test_sync_stream_accepts_plain_callbacks():
    TradeSmartStream("TEST123", "tok", **{name: (lambda *a: None) for name in _SYNC_CALLBACKS})


# --- Minor 2: close is bounded when the peer ignores the close frame -------------------


async def test_async_close_is_bounded_when_the_peer_ignores_the_close_frame():
    async with NoCloseReplyServer() as srv:
        stream = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv.url, heartbeat_interval=30))
        await stream.connect()
        began = time.monotonic()
        await stream.close()
        elapsed = time.monotonic() - began
    assert srv.close_frames == 1
    assert stream.state is StreamState.CLOSED
    assert elapsed < 5.0, f"close() took {elapsed:.1f}s"  # nominal 2 s; 10 s unbounded


def test_sync_stop_does_not_time_out_when_the_peer_ignores_the_close_frame(caplog):
    caplog.set_level(logging.WARNING, logger="tradesmart.stream")
    loop = asyncio.new_event_loop()
    server = NoCloseReplyServer()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        asyncio.run_coroutine_threadsafe(server.__aenter__(), loop).result(5)
        stream = TradeSmartStream("TEST123", "tok", config=cfg(server.url, heartbeat_interval=30))
        stream.start()
        disconnected = threading.Event()
        stream._on_disconnect = disconnected.set
        began = time.monotonic()
        stream.stop()
        elapsed = time.monotonic() - began
        assert "did not stop" not in caplog.text
        assert stream._thread is not None and not stream._thread.is_alive()
        assert disconnected.is_set()  # on_disconnect ran before stop() returned
        assert elapsed < 5.5, f"stop() took {elapsed:.1f}s"  # the warning check above is the key
        assert server.close_frames == 1
    finally:
        asyncio.run_coroutine_threadsafe(server.__aexit__(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


def test_sync_stop_default_timeout_covers_the_bounded_close():
    from tradesmart.stream import async_stream

    default = inspect.signature(TradeSmartStream.stop).parameters["timeout"].default
    assert default > async_stream.CLOSE_TIMEOUT + 1


# --- Minor 3: an expired token ends the stream before a reconnect attempt ---------------


async def test_async_expired_token_before_reconnect_is_terminal(monkeypatch):
    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream("TEST123", fresh_token(), config=cfg(srv.url))
        async with stream:
            stream.__aiter__()
            monkeypatch.setattr(AccessToken, "is_expired", lambda self, now=None: True)  # 06:00 IST
            await srv.drop_all()
            with pytest.raises(AuthenticationError, match="expired"):
                await asyncio.wait_for(stream.__anext__(), 2)
            assert stream.state is StreamState.CLOSED
        assert srv.connections == 1  # no reconnect attempt was made


async def test_async_expired_token_reconnects_when_the_check_is_off(monkeypatch):
    async with MockWsServer() as srv:
        config = cfg(srv.url, check_token_expiry=False)
        stream = AsyncTradeSmartStream("TEST123", fresh_token(), config=config)
        async with stream:
            monkeypatch.setattr(AccessToken, "is_expired", lambda self, now=None: True)
            await srv.drop_all()
            await wait_until(lambda: srv.connections == 2 and stream.state is StreamState.CONNECTED)


def test_sync_expired_token_before_reconnect_goes_to_on_error(threaded_server, monkeypatch):
    errors: list[BaseException] = []
    stream = TradeSmartStream(
        "TEST123", fresh_token(), config=cfg(threaded_server.url), on_error=errors.append
    )
    stream.start()
    try:
        monkeypatch.setattr(AccessToken, "is_expired", lambda self, now=None: True)
        threaded_server.run(threaded_server.drop_all())
        wait_until_sync(lambda: len(errors) == 1)
        assert isinstance(errors[0], AuthenticationError)
        assert stream.state is StreamState.CLOSED
        assert threaded_server.connections == 1
    finally:
        stream.stop()


# --- Minor 4: heartbeat acks are counted -------------------------------------------------


async def test_async_heartbeat_acks_are_counted():
    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream(
            "TEST123", "tok", config=cfg(srv.url, heartbeat_interval=0.5)
        )
        assert stream.heartbeat_acks == 0
        async with stream:
            await wait_until(lambda: stream.heartbeat_acks >= 2, timeout=30)
        # <=: close() may stop reading after the server recorded an "h" but before its "hk"
        sent = sum(1 for m in srv.received if m["t"] == "h")
        assert 2 <= stream.heartbeat_acks <= sent
    with pytest.raises(AttributeError):
        stream.heartbeat_acks = 5  # type: ignore[misc]  # read-only


async def test_async_heartbeat_acks_stay_zero_when_the_server_does_not_answer():
    async with MockWsServer() as srv:
        srv.ignore.add("h")
        stream = AsyncTradeSmartStream(
            "TEST123", "tok", config=cfg(srv.url, heartbeat_interval=0.05)
        )
        async with stream:
            await wait_until(lambda: any(m["t"] == "h" for m in srv.received))
            assert stream.heartbeat_acks == 0


def test_sync_heartbeat_acks_are_counted(threaded_server):
    stream = TradeSmartStream(
        "TEST123", "tok", config=cfg(threaded_server.url, heartbeat_interval=0.5)
    )
    assert stream.heartbeat_acks == 0
    with stream:
        wait_until_sync(lambda: stream.heartbeat_acks >= 2)
    with pytest.raises(AttributeError):
        stream.heartbeat_acks = 5  # type: ignore[misc]  # read-only


# --- Minor 5: non-iterable instrument arguments ------------------------------------------

_NOT_ITERABLE = "expected 'EXCH|TOKEN', (exchange, token) or a list of them"


@pytest.mark.parametrize("bad", [22, None])
@pytest.mark.parametrize(
    "method",
    ["subscribe_touchline", "unsubscribe_touchline", "subscribe_depth", "unsubscribe_depth"],
)
async def test_async_non_iterable_instruments_raise_request_validation_error(method, bad):
    stream = AsyncTradeSmartStream("TEST123", "tok")
    with pytest.raises(RequestValidationError, match=re.escape(_NOT_ITERABLE)):
        await getattr(stream, method)(bad)


@pytest.mark.parametrize("bad", [22, None])
@pytest.mark.parametrize(
    "method",
    ["subscribe_touchline", "unsubscribe_touchline", "subscribe_depth", "unsubscribe_depth"],
)
def test_sync_non_iterable_instruments_raise_request_validation_error(threaded_server, method, bad):
    stream = TradeSmartStream("TEST123", "tok", config=cfg(threaded_server.url))
    with stream, pytest.raises(RequestValidationError, match=re.escape(_NOT_ITERABLE)):
        getattr(stream, method)(bad)
