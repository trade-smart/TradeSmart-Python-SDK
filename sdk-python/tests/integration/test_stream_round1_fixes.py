"""Stream fixes from review round 1: sync stop() ends callbacks, a CancelledError from
on_message does not kill delivery, snapshot() key validation, no duplicate subscribe during a
reconnect, and the sync example's exit code after a terminal error."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.integration.ws_server import MockWsServer, wait_until, wait_until_sync
from tradesmart import ClientConfig, NetworkError, RequestValidationError
from tradesmart.models.stream import OrderUpdate
from tradesmart.stream import AsyncTradeSmartStream, StreamState, TradeSmartStream

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"


def cfg(url: str, **overrides: Any) -> ClientConfig:
    values: dict[str, Any] = {
        "ws_url": url,
        "heartbeat_interval": 2.0,  # a 4 s silence margin: no reconnect on a busy machine
        "reconnect_backoff_base": 0.01,
        "reconnect_backoff_cap": 0.02,
        "ws_auth_timeout": 1.0,
    }
    values.update(overrides)
    return ClientConfig(**values)


def stopped(s: TradeSmartStream) -> bool:
    threads = (s._thread, s._callback_thread)
    return s.state is StreamState.CLOSED and not any(
        t is not None and t.is_alive() for t in threads
    )


def om(n: int) -> dict[str, str]:
    return {"t": "om", "norenordno": str(n), "status": "OPEN"}


# --- I1: sync stop() delivers no further messages --------------------------------------


def test_sync_stop_from_callback_skips_the_buffered_backlog(threaded_server, caplog):
    caplog.set_level(logging.DEBUG, logger="tradesmart.stream")
    got: list[str] = []
    backlog_sent = threading.Event()

    def on_order_update(msg: OrderUpdate) -> None:
        got.append(msg.order_id)
        if len(got) == 1:
            assert backlog_sent.wait(5)  # slow first callback: the rest pile up meanwhile
        else:
            s.stop()

    s = TradeSmartStream(
        "TEST123", "tok", config=cfg(threaded_server.url), on_order_update=on_order_update
    )
    s.start()
    s.subscribe_orders()
    for n in range(1, 51):
        threaded_server.run(threaded_server.send(om(n)))
    # the other 49 wait in the stream while the first callback blocks
    wait_until_sync(lambda: len(s._stream._iter_buffer) == 49, timeout=30)
    backlog_sent.set()
    wait_until_sync(lambda: stopped(s), timeout=30)
    assert got == ["1", "2"]
    skipped = [r for r in caplog.records if "not delivered" in r.getMessage()]
    assert len(skipped) == 1 and skipped[0].levelno == logging.DEBUG
    assert "48" in skipped[0].getMessage()


def test_sync_stop_from_another_thread_skips_queued_messages(threaded_server):
    got: list[str] = []
    entered, release = threading.Event(), threading.Event()

    def on_order_update(msg: OrderUpdate) -> None:
        got.append(msg.order_id)
        entered.set()
        release.wait(5)

    disconnects: list[str] = []
    s = TradeSmartStream(
        "TEST123",
        "tok",
        config=cfg(threaded_server.url),
        on_order_update=on_order_update,
        on_disconnect=lambda: disconnects.append("x"),
    )
    s.start()
    s.subscribe_orders()
    for n in range(1, 21):
        threaded_server.run(threaded_server.send(om(n)))
    assert entered.wait(5)
    wait_until_sync(lambda: len(s._stream._iter_buffer) == 19, timeout=30)  # the rest wait
    timer = threading.Timer(0.2, release.set)
    timer.start()
    try:
        s.stop(timeout=5)
    finally:
        timer.join()
    assert stopped(s)
    assert got == ["1"]
    assert disconnects == ["x"]  # lifecycle callbacks still run after stop()


def test_sync_terminal_error_still_delivers_backlog_before_on_error(threaded_server):
    got: list[str] = []
    errors: list[BaseException] = []

    def on_order_update(msg: OrderUpdate) -> None:
        time.sleep(0.05)
        got.append(msg.order_id)

    config = cfg(threaded_server.url, reconnect_max_attempts=0)
    s = TradeSmartStream(
        "TEST123", "tok", config=config, on_order_update=on_order_update, on_error=errors.append
    )
    s.start()
    for n in range(1, 6):
        threaded_server.run(threaded_server.send(om(n)))
    threaded_server.run(threaded_server.drop_all())
    wait_until_sync(lambda: stopped(s), timeout=30)
    assert got == ["1", "2", "3", "4", "5"]
    assert len(errors) == 1 and isinstance(errors[0], NetworkError)


# --- I2: CancelledError raised by on_message ---------------------------------------------


async def _cancelled_task() -> asyncio.Task[None]:
    task = asyncio.ensure_future(asyncio.sleep(10))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return task


async def test_cancelled_error_in_on_message_is_logged_and_delivery_continues(caplog):
    seen: list[str] = []

    async def on_message(msg: Any) -> None:
        if isinstance(msg, OrderUpdate):
            seen.append(msg.order_id)
            if msg.order_id == "1":
                await await_cancelled()

    async def await_cancelled() -> None:
        await await_target  # a task cancelled elsewhere: raises CancelledError here

    await_target = await _cancelled_task()
    iterated: list[str] = []
    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv.url), on_message=on_message)
        async with stream:

            async def consume() -> None:
                async for msg in stream:
                    if isinstance(msg, OrderUpdate):
                        iterated.append(msg.order_id)

            consumer = asyncio.ensure_future(consume())
            await asyncio.sleep(0)
            await stream.subscribe_orders()
            for n in (1, 2, 3):
                await srv.send(om(n))
            await wait_until(lambda: seen == ["1", "2", "3"] and len(iterated) == 3, timeout=30)
            assert stream.state is StreamState.CONNECTED
            assert not stream._dispatcher.done()
        await consumer
    assert iterated == ["1", "2", "3"]
    assert any(
        "on_message callback raised" in r.getMessage() and r.exc_info for r in caplog.records
    )


async def test_close_still_cancels_a_running_on_message():
    entered = asyncio.Event()
    outcome: list[str] = []

    async def on_message(msg: Any) -> None:
        if isinstance(msg, OrderUpdate):
            entered.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                outcome.append("cancelled")
                raise

    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv.url), on_message=on_message)
        await stream.connect()
        await stream.subscribe_orders()
        await srv.send(om(1))
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(stream.close(), 2)
    assert outcome == ["cancelled"]
    assert stream._dispatcher.done()


async def test_cancelling_the_dispatcher_itself_is_not_swallowed():
    """E.g. asyncio.run() cancelling every task at exit: the dispatcher must end."""
    entered = asyncio.Event()

    async def on_message(msg: Any) -> None:
        if isinstance(msg, OrderUpdate):
            entered.set()
            await asyncio.sleep(10)

    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv.url), on_message=on_message)
        await stream.connect()
        try:
            await stream.subscribe_orders()
            await srv.send(om(1))
            await asyncio.wait_for(entered.wait(), 2)
            dispatcher = stream._dispatcher
            dispatcher.cancel()
            await asyncio.wait_for(asyncio.gather(dispatcher, return_exceptions=True), 2)
            assert dispatcher.cancelled()
        finally:
            await stream.close()


async def test_close_from_on_message_does_not_cancel_that_call():
    outcome: list[str] = []

    async def on_message(msg: Any) -> None:
        if isinstance(msg, OrderUpdate) and not outcome:
            await stream.close()
            outcome.append("returned")  # close() did not cancel the call it was made from

    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv.url), on_message=on_message)
        await stream.connect()
        await stream.subscribe_orders()
        await srv.send(om(1))
        await srv.send(om(2))
        await wait_until(lambda: stream.state is StreamState.CLOSED, timeout=30)
        await asyncio.wait_for(asyncio.gather(stream._dispatcher), 5)
    assert outcome == ["returned"]


# --- M1: snapshot() with several keys -----------------------------------------------------


@pytest.mark.parametrize("key", [["NSE|22", "NSE|23"], ("NSE|22", "NSE|23"), []])
def test_snapshot_with_not_exactly_one_instrument_raises_validation_error(key):
    stream = AsyncTradeSmartStream("TEST123", "tok")
    with pytest.raises(RequestValidationError):
        stream.snapshot(key)  # type: ignore[arg-type]


def test_snapshot_accepts_an_exchange_token_tuple():
    stream = AsyncTradeSmartStream("TEST123", "tok")
    stream._snapshots.update({"t": "tk", "e": "NSE", "tk": "22", "lp": "1.00"})
    assert stream.snapshot(("nse", "22")) is not None  # type: ignore[arg-type]


# --- M2: a subscribe made while the registry is being replayed is sent once ----------------


async def test_subscribe_during_replay_is_sent_exactly_once():
    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv.url))
        ws = await stream._open_and_authenticate(initial=True)
        lock = stream._send_lock
        await lock.acquire()  # holds the replay back until the subscribe is waiting too
        activate = asyncio.ensure_future(stream._activate(ws))
        await asyncio.sleep(0.01)
        subscribe = asyncio.ensure_future(stream.subscribe_touchline(["NSE|22"]))
        await asyncio.sleep(0.01)
        lock.release()
        await asyncio.wait_for(asyncio.gather(activate, subscribe), 2)
        assert stream.state is StreamState.CONNECTED
        await wait_until(lambda: any(m.get("t") == "t" for m in srv.received_on(1)), timeout=30)
        await asyncio.sleep(0.05)  # a duplicate would arrive meanwhile
        sent = [m for m in srv.received_on(1) if m.get("t") == "t"]
        await stream.close()
    assert sent == [{"t": "t", "k": "NSE|22"}]


# --- M3: examples/stream_sync.py exits 1 after a terminal stream error -------------------


def _load_example(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_r1_example_{name[:-3]}", EXAMPLES_DIR / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeStream:
    """Stands in for TradeSmartStream: run_forever() ends with a terminal error."""

    def __init__(self, callbacks: dict[str, Any]) -> None:
        self.callbacks = callbacks

    @classmethod
    def from_env(cls, **callbacks: Any) -> _FakeStream:
        return cls(callbacks)

    def start(self) -> None:
        pass

    def subscribe_touchline(self, keys: Any) -> None:
        pass

    def subscribe_depth(self, keys: Any) -> None:
        pass

    def subscribe_orders(self) -> None:
        pass

    def run_forever(self) -> None:
        self.callbacks["on_error"](NetworkError("reconnect attempts exhausted"))

    def snapshot(self, key: str) -> None:
        return None


def test_stream_sync_example_exits_1_after_a_terminal_error(monkeypatch, capsys):
    module = _load_example("stream_sync.py")
    monkeypatch.setattr(module, "TradeSmartStream", _FakeStream)
    assert module.main(["NSE|22"]) == 1
    assert "reconnect attempts exhausted" in capsys.readouterr().err


def test_stream_sync_example_still_exits_0_without_an_error(monkeypatch):
    module = _load_example("stream_sync.py")
    monkeypatch.setattr(_FakeStream, "run_forever", lambda self: None)
    monkeypatch.setattr(module, "TradeSmartStream", _FakeStream)
    assert module.main(["NSE|22"]) == 0
