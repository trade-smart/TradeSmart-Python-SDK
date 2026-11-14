"""Loopback WebSocket stream server for the offline broker, on its own thread and event loop.

It speaks the documented stream protocol (``05-websocket.md``): ``a`` -> ``ak``, ``t`` / ``d`` ->
one ``tk`` / ``dk`` per key with the current quote, ``u`` -> ``uk`` (same ``k``), ``ud`` -> ``udk``,
``o`` -> nothing (order updates have no acknowledgement), ``uo`` -> ``uok``, ``p`` -> ``pk``,
``up`` -> ``upk``, ``h`` -> ``hk`` unless the state is silenced. State events (``tf``, ``df``,
``om``, ``pm``) reach only the connections subscribed to that instrument, to orders or to
positions. Everything sent to one connection goes through one queue, so it arrives in order.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from collections.abc import Coroutine
from datetime import datetime
from typing import Any, TypeVar

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from tradesmart.models.base import IST
from tradesmart.stream.async_stream import _NoFrameLogger
from tradesmart.testing import _replies
from tradesmart.testing._state import BrokerState

_T = TypeVar("_T")
_TIMEOUT = 5.0  # seconds start(), drop_all() and close() wait for the server thread
# websockets' DEBUG frame dumps would show the connect frame's access token.
_WS_LOGGER = _NoFrameLogger(logging.getLogger("tradesmart.testing.websockets"), {})


def _run(loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, _T]) -> _T:
    """Run ``coro`` on the server loop and wait for its result (from another thread)."""
    return asyncio.run_coroutine_threadsafe(coro, loop).result(_TIMEOUT)


def _keys(message: dict[str, Any]) -> list[str]:
    return [key for key in str(message.get("k", "")).split("#") if key]


class _Connection:
    """One client connection: its subscriptions and its ordered outgoing queue."""

    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        self.touchline: set[str] = set()
        self.depth: set[str] = set()
        self.orders = False
        self.positions = False
        self._outbox: asyncio.Queue[str] = asyncio.Queue()

    def send(self, message: dict[str, Any]) -> None:
        self._outbox.put_nowait(json.dumps(message))

    def wants(self, event: dict[str, Any]) -> bool:
        kind = event.get("t")
        key = f"{event.get('e')}|{event.get('tk')}"
        return (
            (kind == "om" and self.orders)
            or (kind == "pm" and self.positions)
            or (kind == "tf" and key in self.touchline)
            or (kind == "df" and key in self.depth)
        )

    async def write(self) -> None:
        with contextlib.suppress(ConnectionClosed):
            while True:
                await self.ws.send(await self._outbox.get())


class OfflineStreamServer:
    """The broker's stream endpoint on ``127.0.0.1`` (free port), served from a daemon thread."""

    def __init__(self, state: BrokerState) -> None:
        self._state = state
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: Server | None = None
        self._connections: set[_Connection] = set()
        self._port = 0
        self._closed = False
        self._close_lock = threading.Lock()

    @property
    def url(self) -> str:
        """``ws://127.0.0.1:<port>/NorenWSAPI/`` (after ``start()``)."""
        if not self._port:
            raise RuntimeError("OfflineStreamServer is not started")
        return f"ws://127.0.0.1:{self._port}/NorenWSAPI/"

    def start(self) -> None:
        """Listen on a free loopback port; returns once the server accepts connections."""
        if self._thread is not None:
            raise RuntimeError("OfflineStreamServer can only be started once")
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._thread = threading.Thread(
            target=loop.run_forever, name="tradesmart-offline-stream", daemon=True
        )
        self._thread.start()
        try:
            server = self._server = _run(loop, self._listen())
        except BaseException:
            self.close()
            raise
        self._port = int(next(iter(server.sockets)).getsockname()[1])
        self._state.add_listener(self._on_event)

    def drop_all(self) -> None:
        """Cut every open connection at once (no closing handshake); clients reconnect."""
        loop = self._loop
        if loop is not None and not self._closed:
            _run(loop, self._abort_all())

    def close(self) -> None:
        """Stop the server and its thread. Idempotent; safe if never started."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        try:
            server = self._server
            if server is not None:
                _run(loop, self._shutdown(server))
        finally:
            with contextlib.suppress(RuntimeError):  # the loop has already stopped
                loop.call_soon_threadsafe(loop.stop)
            thread.join(_TIMEOUT)
            if not thread.is_alive():
                loop.close()

    # --- server thread ---------------------------------------------------------------------

    async def _listen(self) -> Server:
        return await serve(self._handle, "127.0.0.1", 0, ping_interval=None, logger=_WS_LOGGER)

    async def _abort_all(self) -> None:
        # An abort needs nothing from the client, whose loop may be the one blocked on us.
        for connection in list(self._connections):
            connection.ws.transport.abort()

    async def _shutdown(self, server: Server) -> None:
        await self._abort_all()
        server.close()
        await server.wait_closed()

    def _on_event(self, event: dict[str, Any]) -> None:
        """State listener (any thread): hand the event to the server loop, never block."""
        loop = self._loop
        if loop is None or self._closed:
            return
        with contextlib.suppress(RuntimeError):  # closed meanwhile
            loop.call_soon_threadsafe(self._push, event)

    def _push(self, event: dict[str, Any]) -> None:
        for connection in self._connections:
            if connection.wants(event):
                connection.send(event)

    async def _handle(self, ws: ServerConnection) -> None:
        connection = _Connection(ws)
        writer = asyncio.ensure_future(connection.write())
        self._connections.add(connection)
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except ValueError:  # not JSON: ignored
                    continue
                if isinstance(message, dict):
                    self._reply(connection, message)
        except ConnectionClosed:
            pass
        finally:
            self._connections.discard(connection)
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    def _reply(self, connection: _Connection, message: dict[str, Any]) -> None:
        kind = message.get("t")
        state = self._state
        if kind == "a":
            with state.lock:
                refused, state.refuse_login_once = state.refuse_login_once, False
            connection.send(_replies.connect_ack(str(message.get("uid", "")), ok=not refused))
        elif kind == "h":
            with state.lock:
                silenced = state.silenced
            if not silenced:
                connection.send(_replies.heartbeat_ack(datetime.now(IST)))
        elif kind in ("t", "d"):
            keys = _keys(message)
            (connection.touchline if kind == "t" else connection.depth).update(keys)
            ack = _replies.touchline_ack if kind == "t" else _replies.depth_ack
            for key in keys:
                exchange, _, token = key.partition("|")
                with state.lock:
                    instrument = state.instruments.get((exchange, token))
                    instrument = dict(instrument) if instrument is not None else None
                    fields = dict(state.quotes.get((exchange, token), {}))
                connection.send(ack(exchange, token, instrument, fields, datetime.now(IST)))
        elif kind == "u":
            connection.touchline.difference_update(_keys(message))
            connection.send({"t": "uk", "k": str(message.get("k", ""))})
        elif kind == "ud":
            connection.depth.difference_update(_keys(message))
            connection.send({"t": "udk"})
        elif kind == "o":
            connection.orders = True  # documented: no acknowledgement
        elif kind == "uo":
            connection.orders = False
            connection.send({"t": "uok"})
        elif kind == "p":
            connection.positions = True
            connection.send({"t": "pk", "uid": str(message.get("uid", ""))})
        elif kind == "up":
            connection.positions = False
            connection.send({"t": "upk"})
