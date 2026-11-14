"""Asyncio WebSocket stream with heartbeat, reconnect and subscription restore."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from tradesmart._client_core import check_expiry, env_kwargs
from tradesmart._logging import get_logger
from tradesmart.auth import AccessToken
from tradesmart.config import ClientConfig, header_user_agent
from tradesmart.errors import (
    AuthenticationError,
    NetworkError,
    RequestTimeoutError,
    TradeSmartError,
    TransportError,
)
from tradesmart.models.stream import (
    ConnectAck,
    DepthAck,
    DepthFeed,
    HeartbeatAck,
    OrderUpdate,
    PositionUpdate,
    StreamMessage,
    TouchlineAck,
    TouchlineFeed,
)
from tradesmart.stream.delivery import DeliveryBuffer, StreamEnded
from tradesmart.stream.protocol import (
    Instruments,
    _invalid,
    connect_msg,
    depth_msg,
    format_keys,
    heartbeat_msg,
    order_updates_msg,
    parse_message,
    position_updates_msg,
    touchline_msg,
    unsubscribe_depth_msg,
    unsubscribe_orders_msg,
    unsubscribe_positions_msg,
    unsubscribe_touchline_msg,
)
from tradesmart.stream.state import SnapshotCache, StreamState, SubscriptionRegistry

if TYPE_CHECKING:
    from tradesmart.async_client import AsyncTradeSmart
    from tradesmart.client import TradeSmart

_log = get_logger("stream")

MessageCallback = Callable[[StreamMessage], None | Awaitable[None]]
StateCallback = Callable[[StreamState], None | Awaitable[None]]

#: Seconds a closing handshake may take before the socket is aborted (a peer that never
#: answers the close frame), and that ``close()`` gives running async ``on_state_change``
#: calls before cancelling them; ``TradeSmartStream.stop()``'s default wait covers it.
CLOSE_TIMEOUT = 2.0

# The stream whose async on_state_change runs in the current task (or the task it came from).
_STATE_CALLBACK_OF: contextvars.ContextVar[AsyncTradeSmartStream | None] = contextvars.ContextVar(
    "tradesmart_state_callback_of", default=None
)

_SNAPSHOT_TYPES = (TouchlineAck, TouchlineFeed, DepthAck, DepthFeed)
_OPEN_ERRORS = (OSError, asyncio.TimeoutError, WebSocketException)


class _StreamClosed(Exception):
    """close() was called while a connection was being established."""


class _NoFrameLogger(logging.LoggerAdapter):  # type: ignore[type-arg]
    """Hides websockets' DEBUG frame dumps: the connect frame carries the token."""

    def isEnabledFor(self, level: int) -> bool:
        return level > logging.DEBUG and self.logger.isEnabledFor(level)


def env_identity(config: ClientConfig) -> tuple[str, str | AccessToken]:
    """Client id and token for a stream's ``from_env()`` (``TRADESMART_APP_KEY`` is ignored).

    A stream cannot log in, so without a usable token this raises ``ConfigurationError``
    (``AuthenticationError`` for an expired saved login) naming ``python -m tradesmart login``.
    """
    kwargs, _ = env_kwargs(config, allow_app_key=False)
    token: str | AccessToken = kwargs["access_token"]  # never None without an app key
    return kwargs["client_id"], token


def client_identity(client: TradeSmart | AsyncTradeSmart) -> tuple[str, str | AccessToken]:
    """Client id and current token of a REST client (no network; raises if missing/expired)."""
    return client.client_id, client._creds.current_token()


def _reconnect_delay(
    attempt: int, base: float, cap: float, rand: Callable[[], float] = random.random
) -> float:
    """Delay before 0-based reconnect ``attempt``: capped exponential with jitter."""
    ceiling = min(cap, base * 2.0 ** min(attempt, 1000))  # exponent bound avoids OverflowError
    return ceiling * (0.5 + rand() / 2)


class AsyncTradeSmartStream:
    """Asyncio client for the TradeSmart (Noren) WebSocket stream.

    Use ``async with stream:`` and ``async for msg in stream:`` and/or the
    ``on_message`` callback. One stream owns exactly one connection.

    The reader task never waits for your code: it only reads frames, updates the
    snapshot cache (so ``snapshot()`` is always the latest) and hands messages to
    a separate dispatcher task, which awaits ``on_message`` and then feeds
    ``async for``, one message at a time and in arrival order. A slow consumer
    therefore cannot stall reading, heartbeats or reconnect detection. While it
    lags behind by ``ClientConfig.stream_backlog_limit`` messages or more,
    touchline/depth updates for the same instrument are merged (latest value per
    field wins; no field change is lost). A merged entry keeps its original
    position, so it may already contain ticks that arrived after later
    messages for other instruments; for the same instrument each field's
    values are delivered in arrival order. Acks, order and position updates
    are never merged, dropped or reordered relative to each other. A blocking
    (non-async) ``on_message`` still blocks the event loop: keep it short or
    make it ``async``.

    Messages are queued for ``async for`` only once iteration has started
    (``__aiter__``/``__anext__`` called); before that they go only to
    ``on_message`` and the snapshot cache, so a callback-only consumer retains
    nothing. If you consume via ``async for``, start iterating before
    subscribing. A terminal error is always raised by the iterator, after the
    messages received before it.

    Call ``close()`` only after you have what you need: it does not wait for a
    lagging ``on_message``. Messages ``on_message`` has not received yet are
    discarded (they still reach ``async for`` if you iterate) and counted in
    one WARNING, which says how many of them were order/position updates.

    ``on_state_change`` may be plain or ``async``. An ``async`` one runs as its
    own task (so it can ``await`` REST calls, e.g. to catch up after a
    reconnect) and its exceptions are logged. ``close()`` gives the ones still
    running (including the one for CLOSED) up to ``CLOSE_TIMEOUT`` (2 s) to
    finish and cancels the rest with one WARNING, so long-lived work started
    there never blocks ``close()``.
    The closing handshake is bounded: a peer that never answers the close
    frame is aborted after ``CLOSE_TIMEOUT`` (2 s).
    """

    def __init__(
        self,
        client_id: str,
        access_token: str | AccessToken,
        *,
        config: ClientConfig | None = None,
        on_message: MessageCallback | None = None,
        on_state_change: StateCallback | None = None,
    ) -> None:
        self._client_id = client_id
        # Kept for the 06:00 IST expiry check in connect(); None for a plain string token.
        self._access_token = access_token if isinstance(access_token, AccessToken) else None
        self._token = (
            access_token.access_token
            if isinstance(access_token, AccessToken)
            else SecretStr(access_token)
        )
        self._config = config if config is not None else ClientConfig()
        self._on_message = on_message
        self._on_state_change = on_state_change
        self._state = StreamState.DISCONNECTED
        self._registry = SubscriptionRegistry()
        self._snapshots = SnapshotCache()
        limit = self._config.stream_backlog_limit
        self._buffer = DeliveryBuffer(limit)  # reader -> dispatcher
        self._iter_buffer = DeliveryBuffer(limit)  # dispatcher -> async for
        # Created lazily, on first use inside the running loop.
        self._lock_obj: asyncio.Lock | None = None
        self._closed_obj: asyncio.Event | None = None
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._dispatcher: asyncio.Task[None] | None = None
        self._closing = False  # stop requested (close() or terminal error)
        self._user_closed = False  # close() called: no further on_message calls
        self._undelivered = 0  # buffered messages on_message never got (logged on close)
        self._undelivered_updates = 0  # ... of which order/position updates
        self._ended = False  # CLOSED reached and the delivery buffer ended
        self._error: TradeSmartError | None = None
        self._iterating = False
        self._attempts = 0  # reconnect attempts since the last stable connection
        self._reconnects = 0  # successful reconnects
        self._heartbeat_acks = 0  # "hk" replies received
        self._connected_at = 0.0
        self._state_tasks: set[asyncio.Future[Any]] = set()  # running async on_state_change calls
        self._state_tasks_cancelled = False  # close() cancelled some (warned once)
        # Bounds and cancels them when close() was called from a state callback.
        self._state_reaper: asyncio.Future[None] | None = None

    @classmethod
    def from_env(
        cls,
        *,
        config: ClientConfig | None = None,
        on_message: MessageCallback | None = None,
        on_state_change: StateCallback | None = None,
    ) -> AsyncTradeSmartStream:
        """Build a stream from the ``TRADESMART_*`` variables or the saved login.

        Same precedence as ``AsyncTradeSmart.from_env()``, but ``TRADESMART_APP_KEY`` is ignored:
        a stream cannot log in. Without a usable token this raises ``ConfigurationError``
        (``AuthenticationError`` for an expired saved login) naming ``python -m tradesmart login``.
        """
        config = config if config is not None else ClientConfig()
        client_id, token = env_identity(config)
        return cls(
            client_id,
            token,
            config=config,
            on_message=on_message,
            on_state_change=on_state_change,
        )

    @classmethod
    def from_client(
        cls,
        client: TradeSmart | AsyncTradeSmart,
        *,
        config: ClientConfig | None = None,
        on_message: MessageCallback | None = None,
        on_state_change: StateCallback | None = None,
    ) -> AsyncTradeSmartStream:
        """Build a stream with the client id and current token of a REST client (sync or async).

        Uses ``client.config`` unless ``config`` is given. Nothing is sent; raises
        ``AuthenticationError`` if the client has no token or its token has expired.
        """
        client_id, token = client_identity(client)
        return cls(
            client_id,
            token,
            config=config if config is not None else client.config,
            on_message=on_message,
            on_state_change=on_state_change,
        )

    def __repr__(self) -> str:
        return f"AsyncTradeSmartStream(client_id={self._client_id!r}, state={self._state.value})"

    @property
    def state(self) -> StreamState:
        return self._state

    @property
    def reconnect_count(self) -> int:
        """Successful reconnects so far (0 until the first one).

        Already incremented when ``on_state_change`` reports the CONNECTED that follows a
        reconnect. Updates sent while disconnected are not replayed by the server: after a
        reconnect, call ``orders()`` and ``positions()`` to catch up.
        """
        return self._reconnects

    @property
    def heartbeat_acks(self) -> int:
        """Heartbeat replies (``hk``) received so far, over all connections (read-only).

        The SDK sends a heartbeat every ``ClientConfig.heartbeat_interval`` seconds; a
        growing count shows the server answers them, which keeps an idle stream alive.
        """
        return self._heartbeat_acks

    @property
    def _send_lock(self) -> asyncio.Lock:
        if self._lock_obj is None:
            self._lock_obj = asyncio.Lock()
        return self._lock_obj

    @property
    def _closed(self) -> asyncio.Event:
        if self._closed_obj is None:
            self._closed_obj = asyncio.Event()
        return self._closed_obj

    def snapshot(self, key: str) -> dict[str, Any] | None:
        """Latest merged touchline/depth fields for ``EXCH|TOKEN`` (``None`` if unseen).

        The key is normalised like a subscription (``"nse|22"`` finds ``"NSE|22"``); an
        invalid key raises ``RequestValidationError``.
        """
        if isinstance(key, str):  # fast path: the key as the server reports it
            exact = self._snapshots.get(key)
            if exact is not None:
                return exact
        keys = format_keys(key)
        if len(keys) != 1:
            raise _invalid(0, "expected one instrument")
        return self._snapshots.get(keys[0])

    # -- lifecycle -------------------------------------------------------------

    async def connect(self) -> None:
        """Open the socket and authenticate; raises on failure (no reconnect).

        An expired ``AccessToken`` raises ``AuthenticationError`` before anything is sent
        (unless ``ClientConfig.check_token_expiry`` is False).
        """
        if self._state is not StreamState.DISCONNECTED:
            raise TradeSmartError("connect() can only be called once per stream")
        try:
            if self._access_token is not None:
                check_expiry(self._access_token, self._config)
            ws = await self._open_and_authenticate(initial=True)
            await self._activate(ws)
        except _StreamClosed:
            return
        except BaseException:
            self._finish()
            raise
        self._dispatcher = asyncio.create_task(self._deliver())
        self._reader = asyncio.create_task(self._run())
        self._heartbeat = asyncio.create_task(self._send_heartbeats())

    async def close(self) -> None:
        """Stop the stream: cancel tasks, close the socket, end iteration. Idempotent.

        Returns once the state is CLOSED, also when a shutdown (another ``close()``
        or a terminal error) is already in progress. Safe to await from callbacks.
        ``on_message`` is not called again afterwards (a call in progress elsewhere
        is cancelled); messages already received still reach ``async for``.
        ``async`` ``on_state_change`` calls still running (including the one for
        CLOSED) get up to ``CLOSE_TIMEOUT`` (2 s) to finish; the rest are
        cancelled (one WARNING). Called from such a callback, or from a task it
        started or returned, ``close()`` returns without waiting (it would wait
        for itself) and the same bounded wait and cancel run in the background.
        """
        current = asyncio.current_task()
        self._user_closed = True
        if self._closing:
            if current is not self._reader:  # the reader itself must not wait on itself
                await self._closed.wait()
            await self._wait_state_tasks()
            return
        self._closing = True
        try:
            tasks = [
                t
                for t in (self._reader, self._heartbeat, self._dispatcher)
                if t is not None and t is not current
            ]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._drop_socket()
        finally:
            self._finish()
        await self._wait_state_tasks()

    async def _wait_state_tasks(self) -> None:
        """Give the running async ``on_state_change`` calls ``CLOSE_TIMEOUT``, then cancel them.

        Inside one of them (or a task it started) close() must not wait, as it would wait
        for itself: the same bounded wait and cancel then run in a separate task.
        """
        if _STATE_CALLBACK_OF.get() is not self:
            await self._reap_state_tasks()
        elif self._state_reaper is None:
            # Created outside the marked context so it is not mistaken for a callback task.
            self._state_reaper = contextvars.Context().run(
                asyncio.ensure_future, self._reap_state_tasks()
            )

    async def _reap_state_tasks(self) -> None:
        pending = {t for t in self._state_tasks if not t.done()}
        if not pending:
            return
        _, pending = await asyncio.wait(pending, timeout=CLOSE_TIMEOUT)
        pending = {t for t in pending if not t.done()}
        if not pending:
            return
        for task in pending:
            task.cancel()
        if not self._state_tasks_cancelled:
            self._state_tasks_cancelled = True  # one WARNING, also with concurrent close() calls
            _log.warning(
                "%d async on_state_change callback(s) still running %.1fs after close(); cancelled",
                len(pending),
                CLOSE_TIMEOUT,
            )

    async def __aenter__(self) -> AsyncTradeSmartStream:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def __aiter__(self) -> AsyncTradeSmartStream:
        self._iterating = True
        return self

    async def __anext__(self) -> StreamMessage:
        self._iterating = True
        try:
            return await self._iter_buffer.get()
        except StreamEnded:  # stays ended: later calls end the same way
            pass
        if self._error is not None:  # raised outside the except: keeps its own __cause__
            raise self._error
        raise StopAsyncIteration

    # -- subscriptions ---------------------------------------------------------
    #
    # Each raises ``TradeSmartError`` once the stream is CLOSED (it is single-use). The
    # registry is changed and the request sent under the send lock, so a replay running in
    # ``_activate`` either already contains the change or the request is sent: never both.

    def _ensure_open(self) -> None:
        if self._state is StreamState.CLOSED:
            raise TradeSmartError("stream is closed; create a new stream")

    async def subscribe_touchline(self, instruments: Instruments) -> None:
        self._ensure_open()
        keys = format_keys(instruments)
        if keys:
            async with self._send_lock:
                self._registry.ensure_capacity(touchline_add=keys)
                self._registry.add_touchline(keys)
                await self._send_locked(touchline_msg(keys))

    async def unsubscribe_touchline(self, instruments: Instruments) -> None:
        self._ensure_open()
        keys = format_keys(instruments)
        if keys:
            async with self._send_lock:
                self._registry.remove_touchline(keys)
                await self._send_locked(unsubscribe_touchline_msg(keys))

    async def subscribe_depth(self, instruments: Instruments) -> None:
        self._ensure_open()
        keys = format_keys(instruments)
        if keys:
            async with self._send_lock:
                self._registry.ensure_capacity(depth_add=keys)
                self._registry.add_depth(keys)
                await self._send_locked(depth_msg(keys))

    async def unsubscribe_depth(self, instruments: Instruments) -> None:
        self._ensure_open()
        keys = format_keys(instruments)
        if keys:
            async with self._send_lock:
                self._registry.remove_depth(keys)
                await self._send_locked(unsubscribe_depth_msg(keys))

    async def subscribe_orders(self) -> None:
        self._ensure_open()
        async with self._send_lock:
            self._registry.orders = True
            await self._send_locked(order_updates_msg(self._client_id))

    async def unsubscribe_orders(self) -> None:
        self._ensure_open()
        async with self._send_lock:
            self._registry.orders = False
            await self._send_locked(unsubscribe_orders_msg())

    async def subscribe_positions(self) -> None:
        self._ensure_open()
        async with self._send_lock:
            self._registry.positions = True
            await self._send_locked(position_updates_msg(self._client_id))

    async def unsubscribe_positions(self) -> None:
        self._ensure_open()
        async with self._send_lock:
            self._registry.positions = False
            await self._send_locked(unsubscribe_positions_msg())

    # -- internals: connection -------------------------------------------------

    def _set_state(self, state: StreamState) -> None:
        if self._state is state or self._state is StreamState.CLOSED:  # CLOSED is terminal
            return
        self._state = state
        _log.debug("stream state -> %s", state.value)
        if self._on_state_change is not None:
            # The callback, its task and every task it starts (gather/shield/wait_for, a
            # task it returns) run in a context marked with this stream, so a close() from
            # there does not wait for the state callbacks (it would wait for itself).
            context = contextvars.copy_context()
            context.run(_STATE_CALLBACK_OF.set, self)
            try:
                result = context.run(self._on_state_change, state)
                if inspect.isawaitable(result):
                    task = context.run(asyncio.ensure_future, result)
                    self._state_tasks.add(task)
                    task.add_done_callback(self._state_task_done)
            except Exception:
                _log.exception("on_state_change callback raised")

    def _state_task_done(self, task: asyncio.Future[Any]) -> None:
        self._state_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _log.error("on_state_change callback raised", exc_info=task.exception())

    async def _open_and_authenticate(self, *, initial: bool) -> ClientConnection:
        """Open a socket and wait for ``ak``; raises TransportError or AuthenticationError."""
        timeout = self._config.ws_auth_timeout
        if initial:
            self._set_state(StreamState.CONNECTING)
        try:
            ws = await connect(
                self._config.ws_url,
                open_timeout=timeout,
                ping_interval=None,  # liveness is the protocol heartbeat below
                close_timeout=CLOSE_TIMEOUT,  # a peer ignoring the close frame is aborted
                user_agent_header=header_user_agent(self._config),
                logger=_NoFrameLogger(get_logger("stream.websockets"), {}),
            )
        except _OPEN_ERRORS as exc:
            raise NetworkError(f"WebSocket connection failed: {type(exc).__name__}") from exc
        if initial:
            self._set_state(StreamState.AUTHENTICATING)
        try:
            await ws.send(json.dumps(connect_msg(self._client_id, self._token.get_secret_value())))
            ack = await asyncio.wait_for(self._receive_ack(ws), timeout)
        except TimeoutError as exc:
            await ws.close()
            raise RequestTimeoutError(f"No connect acknowledgement within {timeout}s") from exc
        except ConnectionClosed as exc:
            raise NetworkError("WebSocket closed during authentication") from exc
        except BaseException:
            ws.transport.abort()  # immediate, also when cancelled
            raise
        # The docs say s="Ok"; the live server sends "OK" (observed 2026-11-10): accept any case.
        if (ack.status or "").strip().casefold() != "ok":
            await ws.close()
            raise AuthenticationError(
                f"WebSocket authentication rejected (s={ack.status})",
                endpoint="WebSocket",
                http_status=None,
                raw=ack.raw,
            )
        return ws

    async def _receive_ack(self, ws: ClientConnection) -> ConnectAck:
        while True:
            message = parse_message(await ws.recv())
            if isinstance(message, ConnectAck):
                return message
            self._dispatch(message)

    async def _activate(self, ws: ClientConnection, *, reconnect: bool = False) -> None:
        """Replay the registry exactly once and go CONNECTED (atomically w.r.t. sends).

        On any failure, including cancellation, ``ws`` is aborted before re-raising.
        """
        try:
            async with self._send_lock:
                if self._closing:
                    raise _StreamClosed
                for msg in self._registry.restore_messages(self._client_id):
                    await ws.send(json.dumps(msg))
                if self._closing:
                    raise _StreamClosed
                self._ws = ws
                self._connected_at = asyncio.get_running_loop().time()
                if reconnect:
                    self._reconnects += 1
                self._set_state(StreamState.CONNECTED)
        except BaseException as exc:
            if self._ws is not ws:
                ws.transport.abort()
            if isinstance(exc, ConnectionClosed):
                raise NetworkError("WebSocket closed while restoring subscriptions") from exc
            raise

    async def _drop_socket(self, *, abort: bool = False) -> None:
        ws, self._ws = self._ws, None
        if ws is None:
            return
        if abort:
            ws.transport.abort()
        await ws.close()

    def _finish(self, error: TradeSmartError | None = None) -> None:
        if error is not None and self._error is None:
            self._error = error
        self._closing = True
        self._set_state(StreamState.CLOSED)
        if not self._ended:
            self._ended = True
            self._buffer.end()  # a running dispatcher drains it, then ends iteration
            dispatcher = self._dispatcher
            if dispatcher is None or dispatcher.done():
                self._end_delivery()
            self._closed.set()

    # -- internals: tasks ------------------------------------------------------

    async def _run(self) -> None:
        """Reader/supervisor: read until the socket dies, then reconnect."""
        stable = 2 * self._config.heartbeat_interval
        loop = asyncio.get_running_loop()
        try:
            while True:
                dead = await self._read()
                if self._closing:
                    return
                if loop.time() - self._connected_at >= stable:
                    self._attempts = 0  # only a connection that held resets the budget
                self._set_state(StreamState.RECONNECTING)
                await self._drop_socket(abort=dead)
                await self._reconnect()
        except _StreamClosed:
            return
        except TradeSmartError as exc:
            await self._terminate(exc)
        except Exception as exc:  # defensive: never leave iteration hanging
            _log.exception("stream reader failed")
            await self._terminate(NetworkError(f"Stream failed: {type(exc).__name__}"))

    async def _read(self) -> bool:
        """Dispatch frames until the connection ends; True if it went silent (dead)."""
        ws = self._ws
        silence = 2 * self._config.heartbeat_interval
        while ws is not None and not self._closing:
            try:
                raw = await asyncio.wait_for(ws.recv(), silence)
            except TimeoutError:
                _log.warning("No WebSocket message for %.1fs; reconnecting", silence)
                return True
            except ConnectionClosed:
                if not self._closing:
                    _log.warning("WebSocket connection lost; reconnecting")
                return False
            self._dispatch(parse_message(raw))
        return False

    async def _reconnect(self) -> None:
        """Reconnect, re-auth and restore; ``ak`` Not_Ok (AuthenticationError) is terminal.

        Before each attempt an expired ``AccessToken`` raises ``AuthenticationError`` (terminal,
        nothing is sent), as in ``connect()``.
        """
        limit = self._config.reconnect_max_attempts
        while limit is None or self._attempts < limit:
            if self._closing:  # close() or a terminal error: never keep reconnecting
                raise _StreamClosed
            await asyncio.sleep(
                _reconnect_delay(
                    self._attempts,
                    self._config.reconnect_backoff_base,
                    self._config.reconnect_backoff_cap,
                )
            )
            if self._access_token is not None:
                check_expiry(self._access_token, self._config)
            self._attempts += 1
            try:
                ws = await self._open_and_authenticate(initial=False)
                await self._activate(ws, reconnect=True)
            except TransportError as exc:
                _log.warning("Reconnect attempt %d failed: %s", self._attempts, exc)
                continue
            return
        raise NetworkError("reconnect attempts exhausted")

    async def _terminate(self, error: TradeSmartError) -> None:
        _log.error("Stream stopped: %s", error)
        self._closing = True
        try:
            heartbeat = self._heartbeat
            if heartbeat is not None and heartbeat is not asyncio.current_task():
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            await self._drop_socket()
        finally:
            self._finish(error)

    async def _send_heartbeats(self) -> None:
        """Heartbeat task; an unexpected failure terminates the stream (never degrades silently)."""
        try:
            while True:
                await asyncio.sleep(self._config.heartbeat_interval)
                await self._send_if_connected(heartbeat_msg())
        except Exception as exc:
            if self._closing:  # a shutdown is already under way: not an error
                _log.debug("stream heartbeat stopped while closing: %s", type(exc).__name__)
                return
            _log.exception("stream heartbeat failed")
            error = NetworkError(f"Stream heartbeat failed: {type(exc).__name__}")
            error.__cause__ = exc
            # Set before cancelling: on Python 3.11 asyncio.wait_for can lose the
            # cancellation, and the read loop then still stops on _closing.
            self._closing = True
            reader = self._reader
            if reader is not None:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            await self._terminate(error)

    async def _send_if_connected(self, msg: dict[str, str]) -> None:
        """Send ``msg`` if CONNECTED; otherwise the registry restores it later."""
        async with self._send_lock:
            await self._send_locked(msg)

    async def _send_locked(self, msg: dict[str, str]) -> None:
        """``_send_if_connected`` for a caller that already holds the send lock."""
        ws = self._ws
        if self._state is not StreamState.CONNECTED or ws is None:
            return
        try:
            await ws.send(json.dumps(msg))
        except ConnectionClosed:
            _log.debug("send skipped: connection closed")

    # -- internals: delivery ---------------------------------------------------

    def _dispatch(self, message: StreamMessage) -> None:
        """Reader side: update the snapshot and hand off; never waits for user code."""
        if isinstance(message, HeartbeatAck):
            self._heartbeat_acks += 1
            return
        if isinstance(message, ConnectAck):
            return
        if isinstance(message, _SNAPSHOT_TYPES):
            self._snapshots.update(message.raw)
        if self._on_message is not None or self._iterating:  # else nobody consumes it
            self._buffer.put(message)

    async def _deliver(self) -> None:
        """Dispatcher: call ``on_message``, then feed iteration, in arrival order."""
        message: StreamMessage | None = None
        try:
            while True:
                message = await self._buffer.get()
                if self._user_closed:
                    self._count_undelivered(message)
                else:
                    await self._call_on_message(message)
                self._to_iterator(message)
                message = None
        except StreamEnded:
            pass
        finally:
            if message is not None:  # cancelled during on_message: the iterator still gets it
                self._to_iterator(message)
            self._end_delivery()

    async def _call_on_message(self, message: StreamMessage) -> None:
        """Call ``on_message``; its exceptions are logged, never end the dispatcher.

        A ``CancelledError`` raised by the callback itself (e.g. it awaited a task cancelled
        elsewhere) is logged like any other error; only a cancellation of the dispatcher
        (``close()``, or the event loop shutting down) propagates.
        """
        if self._on_message is None:
            return
        try:
            result = self._on_message(message)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            # Task.cancelling() tells a cancellation of the dispatcher itself apart from a
            # CancelledError the callback raised.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            _log.exception("on_message callback raised")
        except Exception:
            _log.exception("on_message callback raised")

    def _to_iterator(self, message: StreamMessage) -> None:
        if self._iterating:  # callback-only consumers must not accumulate a backlog
            self._iter_buffer.put(message)

    def _count_undelivered(self, message: StreamMessage) -> None:
        """``message`` will never reach ``on_message`` (the stream was closed)."""
        if self._on_message is None:
            return
        self._undelivered += 1
        if isinstance(message, (OrderUpdate, PositionUpdate)):
            self._undelivered_updates += 1

    def _end_delivery(self) -> None:
        """Move what the dispatcher did not deliver to the iterator, then end it."""
        for message in self._buffer.drain():
            self._count_undelivered(message)
            self._to_iterator(message)
        self._iter_buffer.end()
        if self._undelivered:
            _log.warning(
                "Stream closed with %d undelivered messages for on_message "
                "(%d order/position updates); they were discarded",
                self._undelivered,
                self._undelivered_updates,
            )
            self._undelivered = self._undelivered_updates = 0  # one warning per close
