"""Stream reconnect awareness: on_reconnect, reconnect_count, `with`, heartbeat supervision."""

import asyncio
import contextlib
import logging
import threading

import pytest

import tradesmart.stream.async_stream as async_stream_module
from tests.integration.ws_server import MockWsServer, wait_until, wait_until_sync
from tradesmart import ClientConfig, NetworkError, TradeSmartError
from tradesmart.stream import AsyncTradeSmartStream, StreamState, TradeSmartStream


def cfg(srv: MockWsServer) -> ClientConfig:
    return ClientConfig(
        ws_url=srv.url,
        heartbeat_interval=0.2,  # margin: exact reconnect counts are asserted
        reconnect_backoff_base=0.01,
        reconnect_backoff_cap=0.02,
        ws_auth_timeout=1.0,
    )


def _crashing_heartbeat() -> dict[str, str]:
    raise RuntimeError("heartbeat bug")


def test_on_reconnect_fires_after_on_connect_only_on_reconnect(threaded_server):
    events = []
    s = TradeSmartStream(
        "TEST123",
        "tok",
        config=cfg(threaded_server),
        on_connect=lambda: events.append(("connect", threading.current_thread().name)),
        on_reconnect=lambda: events.append(("reconnect", threading.current_thread().name)),
    )
    s.start()
    wait_until_sync(lambda: len(events) == 1)
    assert events == [("connect", "tradesmart-callbacks")]  # first connect: no on_reconnect
    threaded_server.run(threaded_server.drop_all())
    wait_until_sync(lambda: len(events) == 3)
    s.stop()
    assert [name for name, _ in events] == ["connect", "connect", "reconnect"]
    assert {thread for _, thread in events} == {"tradesmart-callbacks"}


def test_on_reconnect_error_goes_to_on_error(threaded_server):
    errors, reconnected = [], threading.Event()

    def boom() -> None:
        reconnected.set()
        raise ValueError("reconnect handler failed")

    s = TradeSmartStream(
        "TEST123", "tok", config=cfg(threaded_server), on_reconnect=boom, on_error=errors.append
    )
    s.start()
    threaded_server.run(threaded_server.drop_all())
    assert reconnected.wait(2)
    wait_until_sync(lambda: len(errors) == 1)
    assert s.state is StreamState.CONNECTED
    s.stop()


async def test_async_reconnect_count():
    async with MockWsServer() as srv:
        s = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv))
        async with s:
            assert s.reconnect_count == 0
            await srv.drop_all()
            await wait_until(lambda: srv.connections == 2 and s.state is StreamState.CONNECTED)
            assert s.reconnect_count == 1


async def test_async_reconnect_count_is_visible_in_connected_state_callback():
    seen = []
    async with MockWsServer() as srv:
        s = AsyncTradeSmartStream(
            "TEST123",
            "tok",
            config=cfg(srv),
            on_state_change=lambda state: seen.append((state, s.reconnect_count)),
        )
        async with s:
            await srv.drop_all()
            await wait_until(lambda: srv.connections == 2 and s.state is StreamState.CONNECTED)
    connected = [count for state, count in seen if state is StreamState.CONNECTED]
    assert connected == [0, 1]


def test_sync_reconnect_count(threaded_server):
    reconnected = threading.Event()
    s = TradeSmartStream(
        "TEST123", "tok", config=cfg(threaded_server), on_reconnect=reconnected.set
    )
    assert s.reconnect_count == 0
    with s:
        assert s.reconnect_count == 0
        threaded_server.run(threaded_server.drop_all())
        assert reconnected.wait(2)
        assert s.reconnect_count == 1
    with pytest.raises(AttributeError):
        s.reconnect_count = 5  # type: ignore[misc]  # read-only


def test_with_statement_starts_and_stops(threaded_server):
    s = TradeSmartStream("TEST123", "tok", config=cfg(threaded_server))
    with s as entered:
        assert entered is s
        assert s.state is StreamState.CONNECTED
    assert s.state is StreamState.CLOSED
    assert s._thread is not None and not s._thread.is_alive()


async def test_async_crashing_heartbeat_terminates_stream(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="tradesmart.stream")
    monkeypatch.setattr(async_stream_module, "heartbeat_msg", _crashing_heartbeat)
    async with MockWsServer() as srv:
        s = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv))
        await s.connect()
        s.__aiter__()
        with pytest.raises(NetworkError, match="heartbeat"):
            await asyncio.wait_for(s.__anext__(), 2)
        assert s.state is StreamState.CLOSED
        await wait_until(lambda: srv.open_connections == 0)
        await s.close()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("heartbeat" in r.getMessage() for r in errors)
    assert any(r.exc_info and isinstance(r.exc_info[1], RuntimeError) for r in errors)


def test_sync_crashing_heartbeat_reaches_on_error(threaded_server, monkeypatch):
    monkeypatch.setattr(async_stream_module, "heartbeat_msg", _crashing_heartbeat)
    errors = []
    s = TradeSmartStream("TEST123", "tok", config=cfg(threaded_server), on_error=errors.append)
    s.start()
    wait_until_sync(lambda: s.state is StreamState.CLOSED and len(errors) == 1)
    assert isinstance(errors[0], TradeSmartError)
    s.stop()


def test_with_statement_stops_when_the_body_raises(threaded_server):
    s = TradeSmartStream("TEST123", "tok", config=cfg(threaded_server))
    with pytest.raises(ValueError, match="body failed"), s:
        assert s.state is StreamState.CONNECTED
        raise ValueError("body failed")
    assert s.state is StreamState.CLOSED
    assert s._thread is not None and not s._thread.is_alive()


async def test_async_with_statement_closes_when_the_body_raises():
    async with MockWsServer() as srv:
        s = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv))
        with pytest.raises(ValueError, match="body failed"):
            async with s:
                assert s.state is StreamState.CONNECTED
                raise ValueError("body failed")
        assert s.state is StreamState.CLOSED
        await wait_until(lambda: srv.open_connections == 0)


async def test_async_reconnect_count_reaches_two_after_two_drops():
    async with MockWsServer() as srv:
        s = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv))
        async with s:
            for expected in (1, 2):
                await srv.drop_all()
                await wait_until(
                    lambda n=expected: srv.connections == n + 1 and s.state is StreamState.CONNECTED
                )
                assert s.reconnect_count == expected


def test_on_reconnect_fires_once_per_reconnect(threaded_server):
    connects, reconnects = [], []
    s = TradeSmartStream(
        "TEST123",
        "tok",
        config=cfg(threaded_server),
        on_connect=lambda: connects.append(1),
        on_reconnect=lambda: reconnects.append(s._stream.reconnect_count),
    )
    with s:
        wait_until_sync(lambda: len(connects) == 1)
        for expected in (1, 2):
            threaded_server.run(threaded_server.drop_all())
            wait_until_sync(lambda n=expected: len(reconnects) == n)
    assert reconnects == [1, 2]
    assert len(connects) == 3


async def test_heartbeat_failure_stops_a_reader_that_lost_its_cancellation(monkeypatch):
    """On Python 3.11 ``asyncio.wait_for`` can swallow a cancellation of the reader."""
    monkeypatch.setattr(async_stream_module, "heartbeat_msg", _crashing_heartbeat)
    s = AsyncTradeSmartStream("TEST123", "tok", config=ClientConfig(heartbeat_interval=0.05))

    async def reader_losing_its_cancellation() -> None:
        for _ in range(300):  # bounded, so a regression fails instead of hanging
            if s._closing:  # what the real read loop checks
                return
            with contextlib.suppress(asyncio.CancelledError):  # lost, as wait_for may do
                await asyncio.sleep(0.01)

    s._reader = asyncio.ensure_future(reader_losing_its_cancellation())
    await asyncio.wait_for(s._send_heartbeats(), 2)
    assert s._reader.done()
    assert s.state is StreamState.CLOSED


async def test_heartbeat_failure_while_closing_logs_no_error(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="tradesmart.stream")
    monkeypatch.setattr(async_stream_module, "heartbeat_msg", _crashing_heartbeat)
    s = AsyncTradeSmartStream("TEST123", "tok", config=ClientConfig(heartbeat_interval=0.05))
    s._closing = True  # a close() (or a terminal error) is already in progress
    await asyncio.wait_for(s._send_heartbeats(), 2)
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


async def test_reconnect_loop_stops_once_closing(monkeypatch):
    """close() must never wait on a reconnect loop: each iteration checks ``_closing``."""
    s = AsyncTradeSmartStream("TEST123", "tok", config=ClientConfig(reconnect_max_attempts=None))
    monkeypatch.setattr(async_stream_module, "_reconnect_delay", lambda *args: 0.0)
    attempts = []

    async def failing_open(*, initial: bool):
        attempts.append(initial)
        s._closing = True  # close() requested while this attempt was failing
        raise NetworkError("WebSocket connection failed: OSError")

    monkeypatch.setattr(s, "_open_and_authenticate", failing_open)
    with pytest.raises(async_stream_module._StreamClosed):
        await asyncio.wait_for(s._reconnect(), 1)
    assert attempts == [False]  # no further attempt after closing


async def test_reconnect_does_not_start_when_already_closing(monkeypatch):
    s = AsyncTradeSmartStream("TEST123", "tok")
    s._closing = True

    async def unexpected_open(*, initial: bool):  # pragma: no cover - must not run
        raise AssertionError("no attempt expected")

    monkeypatch.setattr(s, "_open_and_authenticate", unexpected_open)
    with pytest.raises(async_stream_module._StreamClosed):
        await asyncio.wait_for(s._reconnect(), 1)
