"""Thread-backed synchronous wrapper around :class:`AsyncTradeSmartStream`."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import functools
import inspect
import queue
import threading
import time
from collections.abc import Callable, Coroutine
from types import TracebackType
from typing import TYPE_CHECKING, Any, TypeVar

from tradesmart._logging import get_logger
from tradesmart.auth import AccessToken
from tradesmart.config import ClientConfig
from tradesmart.errors import RequestTimeoutError, TradeSmartError
from tradesmart.models.stream import (
    DepthAck,
    DepthFeed,
    OrderUpdate,
    PositionAck,
    PositionUpdate,
    StreamMessage,
    TouchlineAck,
    TouchlineFeed,
)
from tradesmart.stream.async_stream import AsyncTradeSmartStream, client_identity, env_identity
from tradesmart.stream.protocol import Instruments, as_instrument_list
from tradesmart.stream.state import StreamState

if TYPE_CHECKING:
    from tradesmart.async_client import AsyncTradeSmart
    from tradesmart.client import TradeSmart

_log = get_logger("stream")

_M = TypeVar("_M", bound=StreamMessage)
Callback = Callable[[_M], None] | None
SignalCallback = Callable[[], None] | None

_SUBMIT_TIMEOUT = 10.0  # seconds a sync subscribe/unsubscribe waits for the loop thread

_Job = Callable[[], None] | None  # work for the callback thread; None ends it


def _set_done(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


def _is_async(callback: object) -> bool:
    return inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
        getattr(callback, "__call__", None)  # noqa: B004 - an object with ``async def __call__``
    )


def _discard_awaitable(result: object) -> bool:
    """Close ``result`` if it is an awaitable (no "never awaited" warning); True if it was."""
    if not inspect.isawaitable(result):
        return False
    close = getattr(result, "close", None)
    if callable(close):
        close()
    return True


def _reject_async_callbacks(**callbacks: object) -> None:
    """Sync callbacks run on a plain thread: an ``async def`` one would never be awaited."""
    for name, callback in callbacks.items():
        if callback is not None and _is_async(callback):
            raise TypeError(
                f"{name} must be a plain function: TradeSmartStream calls it on a thread and "
                "cannot await it; use AsyncTradeSmartStream for async callbacks"
            )


class TradeSmartStream:
    """Synchronous, callback-based client for the TradeSmart WebSocket stream.

    ``start()`` runs an :class:`AsyncTradeSmartStream` on a daemon thread
    (``tradesmart-stream``) with its own event loop and returns once it is
    ``CONNECTED``. Every callback runs on a second daemon thread,
    ``tradesmart-callbacks``, one at a time and in arrival order; for each
    message ``on_message`` runs first, then the type-specific one
    (``on_touchline``, ``on_depth``, ``on_order_update`` or ``on_position``).
    A slow callback only delays later callbacks: reading, heartbeats and
    reconnects carry on. While callbacks lag behind by
    ``ClientConfig.stream_backlog_limit`` messages or more, touchline/depth
    updates for the same instrument are merged (latest value per field wins).
    A merged market-data entry keeps its original position, so it may already
    contain ticks that arrived after later messages for other instruments
    (for the same instrument each field's values are delivered in arrival
    order). Order and position
    updates are never merged, dropped or reordered relative to each other.

    ``on_connect`` fires on every transition to ``CONNECTED`` (including after a
    reconnect); it runs on the callback thread, so possibly just after
    ``start()`` returns. ``on_reconnect`` (no arguments) runs right after that
    ``on_connect`` each time the stream is CONNECTED again after a reconnect (never
    on the first connect): updates sent during the outage are not replayed, so
    call ``orders()`` and ``positions()`` there. ``on_disconnect`` fires once each time the stream
    leaves ``CONNECTED`` (connection lost, reconnects exhausted, or ``stop()``),
    so it always pairs with a preceding ``on_connect`` and never fires after a
    failed initial connect. Both are queued as soon as the state changes, so
    they can run before the messages from the previous connection have been
    delivered (those still arrive afterwards). A terminal error (e.g.
    reconnects exhausted) is passed to ``on_error`` after the messages
    received before it. An exception raised by a callback is logged and passed
    to ``on_error`` without stopping the stream. A ``BaseException`` (e.g.
    ``sys.exit()``) raised by a callback ends the callback thread: it is
    logged, passed to ``on_error``, and the stream is closed.

    Inside a callback ``subscribe_*``/``unsubscribe_*`` work as anywhere else
    (they wait for the stream thread and raise their errors in the callback);
    ``stop()`` requests the shutdown and returns without waiting for it. After
    ``stop()`` no further message reaches the callbacks (see ``stop()``).

    Every callback must be a plain function: an ``async def`` callback raises
    ``TypeError`` here (use :class:`AsyncTradeSmartStream` for async code).

    ``with stream:`` calls ``start()`` on entry and ``stop()`` on exit.

    A stream is single-use: after ``stop()`` (or a terminal error) create a new one.
    """

    def __init__(
        self,
        client_id: str,
        access_token: str | AccessToken,
        *,
        config: ClientConfig | None = None,
        on_touchline: Callback[TouchlineAck | TouchlineFeed] = None,
        on_depth: Callback[DepthAck | DepthFeed] = None,
        on_order_update: Callback[OrderUpdate] = None,
        on_position: Callback[PositionAck | PositionUpdate] = None,
        on_connect: SignalCallback = None,
        on_disconnect: SignalCallback = None,
        on_error: Callable[[BaseException], None] | None = None,
        on_message: Callback[StreamMessage] = None,
        on_reconnect: SignalCallback = None,
    ) -> None:
        _reject_async_callbacks(
            on_touchline=on_touchline,
            on_depth=on_depth,
            on_order_update=on_order_update,
            on_position=on_position,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
            on_error=on_error,
            on_message=on_message,
            on_reconnect=on_reconnect,
        )
        self._client_id = client_id
        # No on_message here: messages are consumed (and dispatched) by iterating.
        self._stream = AsyncTradeSmartStream(
            client_id, access_token, config=config, on_state_change=self._on_state_change
        )
        self._on_touchline = on_touchline
        self._on_depth = on_depth
        self._on_order_update = on_order_update
        self._on_position = on_position
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._on_error = on_error
        self._on_message = on_message
        self._on_reconnect = on_reconnect
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._callback_thread: threading.Thread | None = None
        self._jobs: queue.SimpleQueue[_Job] = queue.SimpleQueue()
        self._ready: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main_task: asyncio.Task[None] | None = None
        self._shutdown: asyncio.Future[None] | None = None  # set once stopping/CLOSED
        self._stop_requested = False
        self._callbacks_dead = False  # the callback thread died (BaseException in a callback)
        self._skipped = 0  # messages not passed to callbacks after stop() (logged at the end)
        self._skipped_updates = 0  # ... of which order/position updates
        self._was_connected = False  # on_connect queued and no on_disconnect since
        self._connected_before = False  # a later CONNECTED is a reconnect
        self._background: set[asyncio.Task[Any]] = set()  # strong refs for scheduled tasks
        self._awaitable_reported: set[str] = set()  # callbacks already reported as async

    @classmethod
    def from_env(
        cls,
        *,
        config: ClientConfig | None = None,
        on_touchline: Callback[TouchlineAck | TouchlineFeed] = None,
        on_depth: Callback[DepthAck | DepthFeed] = None,
        on_order_update: Callback[OrderUpdate] = None,
        on_position: Callback[PositionAck | PositionUpdate] = None,
        on_connect: SignalCallback = None,
        on_disconnect: SignalCallback = None,
        on_error: Callable[[BaseException], None] | None = None,
        on_message: Callback[StreamMessage] = None,
        on_reconnect: SignalCallback = None,
    ) -> TradeSmartStream:
        """Build a stream from the ``TRADESMART_*`` variables or the saved login.

        Same precedence as ``TradeSmart.from_env()``, but ``TRADESMART_APP_KEY`` is ignored: a
        stream cannot log in. Without a usable token this raises ``ConfigurationError``
        (``AuthenticationError`` for an expired saved login) naming ``python -m tradesmart login``.
        """
        config = config if config is not None else ClientConfig()
        client_id, token = env_identity(config)
        return cls(
            client_id,
            token,
            config=config,
            on_touchline=on_touchline,
            on_depth=on_depth,
            on_order_update=on_order_update,
            on_position=on_position,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
            on_error=on_error,
            on_message=on_message,
            on_reconnect=on_reconnect,
        )

    @classmethod
    def from_client(
        cls,
        client: TradeSmart | AsyncTradeSmart,
        *,
        config: ClientConfig | None = None,
        on_touchline: Callback[TouchlineAck | TouchlineFeed] = None,
        on_depth: Callback[DepthAck | DepthFeed] = None,
        on_order_update: Callback[OrderUpdate] = None,
        on_position: Callback[PositionAck | PositionUpdate] = None,
        on_connect: SignalCallback = None,
        on_disconnect: SignalCallback = None,
        on_error: Callable[[BaseException], None] | None = None,
        on_message: Callback[StreamMessage] = None,
        on_reconnect: SignalCallback = None,
    ) -> TradeSmartStream:
        """Build a stream with the client id and current token of a REST client (sync or async).

        Uses ``client.config`` unless ``config`` is given. Nothing is sent; raises
        ``AuthenticationError`` if the client has no token or its token has expired.
        """
        client_id, token = client_identity(client)
        return cls(
            client_id,
            token,
            config=config if config is not None else client.config,
            on_touchline=on_touchline,
            on_depth=on_depth,
            on_order_update=on_order_update,
            on_position=on_position,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
            on_error=on_error,
            on_message=on_message,
            on_reconnect=on_reconnect,
        )

    def __repr__(self) -> str:
        return f"TradeSmartStream(client_id={self._client_id!r}, state={self.state.value})"

    @property
    def state(self) -> StreamState:
        return self._stream.state

    @property
    def reconnect_count(self) -> int:
        """Successful reconnects so far (0 until the first one).

        Already incremented when ``on_reconnect`` runs. Updates sent while disconnected are
        not replayed by the server: after a reconnect, call ``orders()`` and ``positions()``.
        """
        return self._stream.reconnect_count

    @property
    def heartbeat_acks(self) -> int:
        """Heartbeat replies (``hk``) received so far, over all connections (read-only)."""
        return self._stream.heartbeat_acks

    def snapshot(self, key: str) -> dict[str, Any] | None:
        """Latest merged touchline/depth fields for ``EXCH|TOKEN`` (``None`` if unseen).

        The key is normalised like a subscription (``"nse|22"`` finds ``"NSE|22"``).
        """
        return self._stream.snapshot(key)

    # -- lifecycle -------------------------------------------------------------

    def start(self, timeout: float | None = None) -> None:
        """Connect on a background thread; return once ``CONNECTED``.

        Re-raises the connect error (e.g. ``AuthenticationError``) here, or raises
        ``RequestTimeoutError`` (and stops the stream) if not connected within
        ``timeout`` seconds (default ``2 * ws_auth_timeout + 5``: socket open plus
        connect acknowledgement). A no-op while the stream is already running.
        """
        if timeout is None:
            timeout = 2 * self._stream._config.ws_auth_timeout + 5
        with self._lifecycle_lock:
            if self._thread is not None:
                if self._thread.is_alive():
                    return
                raise TradeSmartError("stream already stopped; create a new TradeSmartStream")
            self._callback_thread = threading.Thread(
                target=self._callback_main, name="tradesmart-callbacks", daemon=True
            )
            self._thread = threading.Thread(
                target=self._thread_main, name="tradesmart-stream", daemon=True
            )
            self._callback_thread.start()
            self._thread.start()
        try:
            self._ready.result(timeout)
        except concurrent.futures.TimeoutError:
            self.stop()
            raise RequestTimeoutError(f"Stream did not connect within {timeout}s") from None
        except BaseException:  # connect error, or Ctrl+C while connecting
            self.stop()
            raise

    def stop(self, timeout: float = 5.0) -> None:
        """Close the stream and wait up to ``timeout`` s for its threads. Idempotent.

        From the moment ``stop()`` is called no further message reaches the
        callbacks: messages still waiting are discarded (their count is logged at
        DEBUG). A message whose callbacks are running finishes them, and
        ``on_disconnect`` still fires. Waits for the stream thread, then for the
        callback thread. A callback still running when ``timeout`` expires (e.g.
        one that blocks) is not interrupted and may complete after ``stop()``
        returns. From a callback it only requests the shutdown.
        The closing handshake itself takes at most ``CLOSE_TIMEOUT`` (2 s, a peer
        that ignores the close frame is then aborted), well inside the default.
        """
        thread = self._thread
        if thread is None:
            return
        self._stop_requested = True  # set before reading _loop; _main checks it after setting it
        loop = self._loop
        if loop is not None:
            # RuntimeError: the loop is already closed, i.e. the thread has finished
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._request_close)
        callbacks = self._callback_thread
        if threading.current_thread() in (thread, callbacks):
            return  # joining ourselves would deadlock; the shutdown completes on its own
        deadline = time.monotonic() + timeout
        thread.join(timeout)
        if thread.is_alive():
            _log.warning("stream thread did not stop within %.1fs", timeout)
            return
        if callbacks is not None:
            callbacks.join(max(0.0, deadline - time.monotonic()))
            if callbacks.is_alive():
                _log.warning(
                    "a stream callback is still running after %.1fs; "
                    "remaining callbacks may run after stop() returns",
                    timeout,
                )

    def __enter__(self) -> TradeSmartStream:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def run_forever(self) -> None:
        """Start if needed and block until the stream is ``CLOSED`` and every queued
        callback (including ``on_error`` for a terminal error) has run.

        ``KeyboardInterrupt`` (Ctrl+C) stops the stream and returns.
        """
        if self._in_callback_thread():
            raise TradeSmartError("run_forever() cannot be called from a stream callback")
        self.start()
        try:
            for thread in (self._thread, self._callback_thread):
                while thread is not None and thread.is_alive():
                    thread.join(0.5)  # short waits keep Ctrl+C responsive on Windows
        except KeyboardInterrupt:
            self.stop()

    # -- subscriptions ---------------------------------------------------------

    def subscribe_touchline(self, instruments: Instruments) -> None:
        self._submit(self._stream.subscribe_touchline, as_instrument_list(instruments))

    def unsubscribe_touchline(self, instruments: Instruments) -> None:
        self._submit(self._stream.unsubscribe_touchline, as_instrument_list(instruments))

    def subscribe_depth(self, instruments: Instruments) -> None:
        self._submit(self._stream.subscribe_depth, as_instrument_list(instruments))

    def unsubscribe_depth(self, instruments: Instruments) -> None:
        self._submit(self._stream.unsubscribe_depth, as_instrument_list(instruments))

    def subscribe_orders(self) -> None:
        self._submit(self._stream.subscribe_orders)

    def unsubscribe_orders(self) -> None:
        self._submit(self._stream.unsubscribe_orders)

    def subscribe_positions(self) -> None:
        self._submit(self._stream.subscribe_positions)

    def unsubscribe_positions(self) -> None:
        self._submit(self._stream.unsubscribe_positions)

    # -- internals: threads ----------------------------------------------------

    def _in_callback_thread(self) -> bool:
        thread = self._callback_thread
        return thread is not None and threading.current_thread() is thread

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:  # never let the thread die with an unhandled error
            if not self._ready.done():
                self._ready.set_exception(exc)
            else:
                _log.exception("stream thread failed")
        finally:
            self._jobs.put(None)  # the callback thread ends after the queued callbacks

    def _callback_main(self) -> None:
        """Runs every user callback, one job at a time, in the order queued."""
        while True:
            job = self._jobs.get()
            if job is None:
                if self._skipped:
                    _log.debug(
                        "stream stopped: %d received messages were not delivered to the "
                        "callbacks (%d order/position updates)",
                        self._skipped,
                        self._skipped_updates,
                    )
                return
            try:
                job()
            except BaseException as exc:  # e.g. sys.exit() in a callback (Exception is caught)
                self._callbacks_failed(exc)
                return

    def _callbacks_failed(self, exc: BaseException) -> None:
        """A callback raised a BaseException: no more callbacks can run, so stop the stream."""
        _log.exception("stream callback thread stopped by %s; closing", type(exc).__name__)
        self._callbacks_dead = True  # _hand_over no longer waits for this thread
        self._stop_requested = True  # set before reading _loop, as in stop()
        loop = self._loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):  # the loop has already closed
                loop.call_soon_threadsafe(self._request_close)
        try:  # still on the callback thread, so on_error keeps its usual thread
            self._report_error(exc)
        except BaseException:
            _log.exception("on_error callback raised")

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._main_task = asyncio.current_task()
        self._shutdown = self._loop.create_future()
        stream = self._stream
        if self._stop_requested:  # stop() ran before the loop existed
            await stream.close()
            self._ready.set_exception(TradeSmartError("stream stopped before it connected"))
            return
        messages = stream.__aiter__()  # queue messages from the start, before any subscribe
        try:
            await stream.connect()
        except BaseException as exc:  # incl. cancellation by stop() during connect
            if isinstance(exc, Exception):
                self._ready.set_exception(exc)
            else:
                self._ready.set_exception(TradeSmartError("stream stopped before it connected"))
            return
        if stream.state is not StreamState.CONNECTED:  # stop() raced the handshake
            self._ready.set_exception(TradeSmartError("stream stopped before it connected"))
            return
        self._ready.set_result(None)
        try:
            async for message in messages:
                await self._hand_over(message)
        except TradeSmartError as exc:  # terminal error (already logged by the stream)
            self._jobs.put(functools.partial(self._report_error, exc))
        finally:
            await stream.close()

    async def _hand_over(self, message: StreamMessage) -> None:
        """Queue ``message`` for the callback thread and wait until it was handled.

        The wait does not block the loop; it keeps a backlog in the stream's
        bounded, merging buffer instead of an unbounded queue. Once a shutdown
        has started, the remaining messages are queued without waiting.
        """
        handled: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._jobs.put(functools.partial(self._run_message, message, handled))
        shutdown = self._shutdown
        if shutdown is not None and not shutdown.done() and not self._callbacks_dead:
            await asyncio.wait({handled, shutdown}, return_when=asyncio.FIRST_COMPLETED)

    def _run_message(self, message: StreamMessage, handled: asyncio.Future[None]) -> None:
        """Runs on the callback thread. After ``stop()`` the message is only counted."""
        try:
            if self._stop_requested:
                self._skipped += 1
                if isinstance(message, (OrderUpdate, PositionUpdate)):
                    self._skipped_updates += 1
            else:
                self._dispatch(message)
        finally:
            loop = self._loop
            if loop is not None:
                with contextlib.suppress(RuntimeError):  # the loop has already closed
                    loop.call_soon_threadsafe(_set_done, handled)

    def _begin_shutdown(self) -> None:
        """Runs on the loop thread: stop waiting for the callbacks to keep up."""
        if self._shutdown is not None:
            _set_done(self._shutdown)

    def _request_close(self) -> None:
        """Runs on the loop thread: close the stream (aborting a pending connect)."""
        self._begin_shutdown()
        main = self._main_task
        if main is None or main.done():
            # _main always leaves the stream CLOSED; a task spawned now would
            # outlive asyncio.run's task cleanup and be destroyed while pending.
            return
        if not self._ready.done():
            main.cancel()
        self._spawn(self._stream.close())

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _submit(self, method: Callable[..., Coroutine[Any, Any, None]], *args: Any) -> None:
        """Run a stream coroutine method on the loop thread and wait for it.

        Also safe from a callback: callbacks run on their own thread, so the loop is free.
        """
        if self._stop_requested or self._stream.state is StreamState.CLOSED:
            raise TradeSmartError("stream is closed; create a new stream")
        loop = self._loop
        if loop is None or self._thread is None or not self._thread.is_alive():
            raise TradeSmartError("stream is not running; call start() first")
        coro = method(*args)
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:  # the loop closed in the meantime
            coro.close()
            raise TradeSmartError("stream is not running; call start() first") from None
        try:
            future.result(_SUBMIT_TIMEOUT)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise RequestTimeoutError(
                f"stream did not confirm the request within {_SUBMIT_TIMEOUT}s; "
                "it may still take effect and will be restored on reconnect"
            ) from None
        except concurrent.futures.CancelledError:
            raise TradeSmartError("stream stopped before the request was processed") from None

    # -- internals: callbacks (queued on the loop thread, run on the callback thread)

    def _on_state_change(self, state: StreamState) -> None:
        """Runs on the loop thread."""
        if state is StreamState.CONNECTED:
            self._was_connected = True
            self._jobs.put(functools.partial(self._call, "on_connect", self._on_connect))
            if self._connected_before:
                self._jobs.put(functools.partial(self._call, "on_reconnect", self._on_reconnect))
            self._connected_before = True
        elif state in (StreamState.RECONNECTING, StreamState.CLOSED) and self._was_connected:
            self._was_connected = False  # one on_disconnect per disconnection episode
            self._jobs.put(functools.partial(self._call, "on_disconnect", self._on_disconnect))
        if state is StreamState.CLOSED:
            self._begin_shutdown()

    def _dispatch(self, message: StreamMessage) -> None:
        self._call("on_message", self._on_message, message)
        if isinstance(message, (TouchlineAck, TouchlineFeed)):
            self._call("on_touchline", self._on_touchline, message)
        elif isinstance(message, (DepthAck, DepthFeed)):
            self._call("on_depth", self._on_depth, message)
        elif isinstance(message, OrderUpdate):
            self._call("on_order_update", self._on_order_update, message)
        elif isinstance(message, (PositionAck, PositionUpdate)):
            self._call("on_position", self._on_position, message)

    def _call(self, name: str, callback: Callable[..., None] | None, *args: Any) -> None:
        if callback is None:
            return
        try:
            self._reject_awaitable(name, callback(*args))
        except Exception as exc:
            _log.exception("%s callback raised", name)
            self._report_error(exc)

    def _report_error(self, exc: BaseException) -> None:
        if self._on_error is None:
            return
        try:
            self._reject_awaitable("on_error", self._on_error(exc))
        except Exception:
            _log.exception("on_error callback raised")

    def _reject_awaitable(self, name: str, result: object) -> None:
        """A callback that returned an awaitable (e.g. ``lambda m: handler(m)`` with an async
        ``handler``) cannot be awaited on this thread: it is closed, and the first time per
        callback a ``TypeError`` is raised (so it is logged and reported once, not per message)."""
        if not _discard_awaitable(result) or name in self._awaitable_reported:
            return
        self._awaitable_reported.add(name)
        raise TypeError(
            f"{name} callback returned an awaitable; TradeSmartStream callbacks must be plain "
            "functions (use AsyncTradeSmartStream for async callbacks)"
        )
