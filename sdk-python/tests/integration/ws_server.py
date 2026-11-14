"""Local WebSocket server mimicking the Noren streaming protocol (tests only)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable, Container, Coroutine, Iterator
from typing import Any, TypeVar

import pytest
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

T = TypeVar("T")


class MockWsServer:
    """Async context manager serving on ``127.0.0.1`` with an OS-assigned port.

    Replies ``ak`` to ``t=a`` (``Ok`` unless ``auth_ok`` is false), ``hk`` to
    ``t=h`` and one ``tk`` per key to ``t=t``; other requests are only recorded.
    Request types listed in ``ignore`` are recorded but never answered.
    Connections whose 1-based number is in ``drop_after_auth`` are closed by the
    server right after it sends ``ak``.
    """

    def __init__(self, auth_ok: bool = True) -> None:
        self.auth_ok = auth_ok
        self.ignore: set[str] = set()
        self.drop_after_auth: Container[int] = ()
        self.ack_delay = 0.0
        self.ok_status = "Ok"  # the live server sends "OK"; the docs say "Ok"
        self.received: list[dict[str, Any]] = []
        self.user_agents: list[str] = []  # User-Agent of each handshake, in order
        self.connections = 0
        self.url = ""
        self._per_connection: list[list[dict[str, Any]]] = []
        self._active: set[ServerConnection] = set()
        self._server: Server | None = None

    async def __aenter__(self) -> MockWsServer:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/"
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def open_connections(self) -> int:
        return len(self._active)

    def received_on(self, connection: int) -> list[dict[str, Any]]:
        """Messages received on the ``connection``-th connection (1-based)."""
        return self._per_connection[connection - 1]

    async def send(self, msg: dict[str, Any]) -> None:
        """Send ``msg`` to every open connection."""
        for ws in list(self._active):
            await ws.send(json.dumps(msg))

    async def drop_all(self) -> None:
        """Close every open connection from the server side."""
        for ws in list(self._active):
            await ws.close()

    async def _handle(self, ws: ServerConnection) -> None:
        self.connections += 1
        self.user_agents.append(ws.request.headers.get("User-Agent", "") if ws.request else "")
        number = self.connections
        received: list[dict[str, Any]] = []
        self._per_connection.append(received)
        self._active.add(ws)
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.received.append(msg)
                received.append(msg)
                await self._reply(ws, msg)
                if msg.get("t") == "a" and number in self.drop_after_auth:
                    await ws.close()
        except ConnectionClosed:
            pass
        finally:
            self._active.discard(ws)

    async def _reply(self, ws: ServerConnection, msg: dict[str, Any]) -> None:
        t = msg.get("t")
        if t in self.ignore:
            return
        if t == "a":
            await asyncio.sleep(self.ack_delay)
            status = self.ok_status if self.auth_ok else "Not_Ok"
            await ws.send(json.dumps({"t": "ak", "uid": msg.get("uid"), "s": status}))
        elif t == "h":
            await ws.send(json.dumps({"t": "hk", "ft": str(int(time.time()))}))
        elif t == "t":
            for key in msg["k"].split("#"):
                exchange, token = key.split("|")
                await ws.send(json.dumps({"t": "tk", "e": exchange, "tk": token, "lp": "100.50"}))


async def wait_until(predicate: Callable[[], bool], timeout: float = 30) -> None:
    """Poll ``predicate`` until it is true; fail the test after ``timeout`` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(0.005)


def wait_until_sync(predicate: Callable[[], bool], timeout: float = 30.0) -> None:
    """Blocking twin of :func:`wait_until` for threaded (sync) tests."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        time.sleep(0.005)


class ThreadedMockWsServer(MockWsServer):
    """A :class:`MockWsServer` living on its own event-loop thread.

    Sync tests drive it with :meth:`run`, e.g. ``srv.run(srv.drop_all())``.
    """

    def __init__(self, auth_ok: bool = True) -> None:
        super().__init__(auth_ok=auth_ok)
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self.loop.run_forever, name="mock-ws-server", daemon=True
        )

    def run(self, coro: Coroutine[Any, Any, T], timeout: float = 5.0) -> T:
        """Run ``coro`` on the server loop and return its result."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self) -> None:
        self._thread.start()
        self.run(self.__aenter__())

    def stop(self) -> None:
        try:
            self.run(self.__aexit__(None, None, None))
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(5)
            self.loop.close()


def _threaded(auth_ok: bool) -> Iterator[ThreadedMockWsServer]:
    server = ThreadedMockWsServer(auth_ok=auth_ok)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def threaded_server() -> Iterator[ThreadedMockWsServer]:
    yield from _threaded(auth_ok=True)


@pytest.fixture
def threaded_server_bad_auth() -> Iterator[ThreadedMockWsServer]:
    yield from _threaded(auth_ok=False)
