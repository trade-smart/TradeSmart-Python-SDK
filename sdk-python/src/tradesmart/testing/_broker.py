"""``OfflineBroker``: the public face of the offline broker.

It owns one ``BrokerState`` (what the test sets and what the strategy sends), the loopback
stream server and a private folder for the script master, and builds real clients and streams
wired to them.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import shutil
import sys
import tempfile
import threading
import weakref
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, TypeVar

import httpx

from tradesmart.async_client import AsyncTradeSmart
from tradesmart.auth import AccessToken
from tradesmart.client import TradeSmart
from tradesmart.config import ClientConfig
from tradesmart.enums import Exchange
from tradesmart.models.base import IST
from tradesmart.stream.async_stream import AsyncTradeSmartStream
from tradesmart.stream.sync_stream import TradeSmartStream
from tradesmart.testing._rest import AsyncOfflineTransport, OfflineTransport, offline_config
from tradesmart.testing._state import _DEFAULT_PLACE, BrokerState, OfflineOrder, OfflineRequest
from tradesmart.testing._ws import OfflineStreamServer
from tradesmart.testing.drills import Drill

__all__ = ["OfflineBroker"]

# Synthetic app credentials, so login_url() and generate_session() work against the offline broker.
_APP_KEY = "offline-app-key"
_SECRET_KEY = "offline-secret-key"  # noqa: S105 - synthetic, never a real key
# Settings that point the clients at the offline broker; an override would send calls elsewhere.
_OFFLINE_URLS = ("ws_url", "base_url", "authorize_url", "script_master_base_url")
_log = logging.getLogger("tradesmart.testing")
# How long the synthetic login of broker.client() and the streams stays valid.
_LOGIN_LIFETIME = timedelta(hours=24)
# Each client or stream built by a broker keeps it alive (weak key, strong value), so a helper
# that returns only the client still has a running stream server and its private folder.
_OWNERS: weakref.WeakKeyDictionary[object, OfflineBroker] = weakref.WeakKeyDictionary()
_OWNERS_LOCK = threading.Lock()
_Built = TypeVar("_Built")


def _stop_server(server: OfflineStreamServer) -> None:
    """Close the stream server of a broker that was never closed (from its finalizer)."""
    if threading.current_thread() is getattr(server, "_thread", None):
        return  # never join the server thread from itself
    try:
        server.close()
    except Exception:
        _log.debug("OfflineBroker could not stop the stream server of an unclosed broker")


def _delete_folder(path: str) -> None:
    """Delete the private script-master folder; a failure leaves it and logs one DEBUG line."""
    failed: list[str] = []

    def note(function: Any, name: str, error: Any) -> None:
        exc = error if isinstance(error, BaseException) else error[1]
        if not isinstance(exc, FileNotFoundError):
            failed.append(name)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=note)
    else:
        shutil.rmtree(path, onerror=note)
    if failed:
        _log.debug(
            "OfflineBroker could not delete %d item(s) of its script-master folder %s",
            len(failed),
            path,
        )


class OfflineBroker:
    """An offline TradeSmart server for testing strategy code with the real SDK clients.

    Use it as a context manager (``with OfflineBroker() as broker:``) or call ``close()``. The
    stream server starts on ``with`` or with the first client or stream; ``close()`` stops the
    sync streams it built and the server, and deletes the private script-master folder of the
    clients it built (close async streams yourself before the broker). Every client and stream
    it builds keeps it alive; a broker never closed is cleaned up once it and all of them are
    gone. Nothing leaves the process: REST runs in memory and the stream server listens on
    ``127.0.0.1`` only.
    """

    def __init__(self, client_id: str = "OFFLINE01") -> None:
        self._state = BrokerState(client_id)
        self._server = OfflineStreamServer(self._state)
        self._lock = threading.Lock()
        self._started = False
        self._closed = False  # no more clients or streams
        self._shut_down = False  # close() completed
        self._streams: weakref.WeakSet[TradeSmartStream] = weakref.WeakSet()
        # Offline script masters go here, never into the user's real cache folder.
        self._instruments_dir = tempfile.mkdtemp(prefix="tradesmart-offline-")
        # Removes the folder on close(), or when a broker that was never closed goes away.
        self._remove_dir = weakref.finalize(self, _delete_folder, self._instruments_dir)
        # Stops the stream server (thread and port) of a broker that goes away unclosed.
        self._stop_server = weakref.finalize(self, _stop_server, self._server)

    # --- lifecycle ---------------------------------------------------------------------------

    def __enter__(self) -> OfflineBroker:
        self._ws_url()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is None:
            self.close()
            return
        try:  # the exception raised inside ``with`` stays the one the caller sees
            self.close()
        except Exception:
            _log.warning("OfflineBroker.close() failed while handling another error", exc_info=True)

    def close(self) -> None:
        """Stop the sync streams built here and the stream server; delete the private folder.

        Idempotent. If stopping fails, the error is raised and a later ``close()`` tries again;
        no new client or stream is built meanwhile. Async streams cannot be stopped from here:
        close them before the broker.
        """
        with self._lock:
            if self._shut_down:
                return
            self._closed = True
            streams = list(self._streams)
        try:
            for stream in streams:
                stream.stop()
            self._server.close()
            self._stop_server.detach()
        finally:
            self._remove_dir()
        with self._lock:
            self._shut_down = True

    def __repr__(self) -> str:
        return f"OfflineBroker(client_id={self.client_id!r})"

    # --- real clients and streams wired to the offline broker ----------------------------------

    def client(self, **config_overrides: Any) -> TradeSmart:
        """A real ``TradeSmart`` wired to the offline broker and already logged in.

        ``config_overrides`` set ``ClientConfig`` fields (e.g. ``max_retries=0`` to see a read
        drill's error instead of the SDK's retry). The client keeps this broker alive.
        """
        return self._owned(
            TradeSmart(
                self.client_id,
                access_token=self._token(),
                app_key=_APP_KEY,
                secret_key=_SECRET_KEY,
                config=self._config(config_overrides),
                http_client=httpx.Client(transport=OfflineTransport(self._state)),
            )
        )

    def async_client(self, **config_overrides: Any) -> AsyncTradeSmart:
        """A real ``AsyncTradeSmart`` wired to the offline broker and already logged in."""
        return self._owned(
            AsyncTradeSmart(
                self.client_id,
                access_token=self._token(),
                app_key=_APP_KEY,
                secret_key=_SECRET_KEY,
                config=self._config(config_overrides),
                http_client=httpx.AsyncClient(transport=AsyncOfflineTransport(self._state)),
            )
        )

    def stream(self, **callbacks: Any) -> TradeSmartStream:
        """A real ``TradeSmartStream`` connected to the offline broker; ``close()`` stops it."""
        stream = TradeSmartStream(
            self.client_id, self._token(), config=self._config({}), **callbacks
        )
        with self._lock:
            self._streams.add(stream)
        return self._owned(stream)

    def async_stream(self, **callbacks: Any) -> AsyncTradeSmartStream:
        """A real ``AsyncTradeSmartStream`` connected to the broker (close it before the broker)."""
        return self._owned(
            AsyncTradeSmartStream(
                self.client_id, self._token(), config=self._config({}), **callbacks
            )
        )

    # --- state set by the test -----------------------------------------------------------------

    def add_instrument(
        self,
        exchange: str,
        token: str,
        symbol: str,
        *,
        lot_size: int = 1,
        tick_size: Any = "0.05",
        instrument: str = "EQ",
        expiry: date | None = None,
        strike: Any = None,
        option_type: str | None = None,
        underlying: str | None = None,
        **wire_extra: Any,
    ) -> None:
        """Add an instrument (served by search, chains, quotes and the script master).

        ``wire_extra`` sets more wire keys of the instrument, e.g. ``frzqty=1801``: the freeze
        quantity that ``security_info()`` replies carry (blank when not given) and that
        ``place_sliced_order`` slices by.
        """
        self._state.add_instrument(
            exchange,
            token,
            symbol,
            lot_size=lot_size,
            tick_size=tick_size,
            instrument=instrument,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            underlying=underlying,
            **wire_extra,
        )
        self._forget_script_master(exchange)

    def set_quote(self, exchange: str, token: str, **fields: Any) -> None:
        """Set quote fields (friendly names such as ``last_price``, or wire keys)."""
        self._state.set_quote(exchange, token, **fields)

    def set_candles(self, exchange: str, token: str, candles: Iterable[Mapping[str, Any]]) -> None:
        """Set the candles served by ``historical_data`` and ``daily_history``."""
        self._state.set_candles(exchange, token, candles)

    def set_funds(self, **wire: Any) -> None:
        """Set ``Limits`` fields (wire keys, e.g. ``cash="100000"``)."""
        self._state.set_funds(**wire)

    def set_holdings(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Set the ``Holdings`` records (wire keys)."""
        self._state.set_holdings(rows)

    def set_positions(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Set the starting positions (wire keys; at least ``exch``, ``tsym``, ``prd``)."""
        self._state.set_positions(rows)

    def set_user(self, **wire: Any) -> None:
        """Set ``UserDetails`` fields (wire keys)."""
        self._state.set_user(**wire)

    def set_reply(self, endpoint: str, body: dict[str, Any] | list[Any]) -> None:
        """Answer ``endpoint`` with this body (for endpoints the offline broker does not model)."""
        self._state.set_reply(endpoint, body)

    def tick(self, exchange: str, token: str, **fields: Any) -> None:
        """Update the quote and push it to the streams subscribed to the instrument."""
        self._state.tick(exchange, token, **fields)

    # --- orders, driven by the test ------------------------------------------------------------

    def accept(self, order_id: str) -> None:
        """PENDING -> OPEN (TRIGGER_PENDING for a stop order)."""
        self._state.accept(order_id)

    def reject(self, order_id: str, message: str) -> None:
        """Reject an open order with ``message`` as the reason."""
        self._state.reject(order_id, message)

    def fill(self, order_id: str, price: Any, quantity: int | None = None) -> None:
        """Fill ``quantity`` (default: all that is open) at ``price``; partials accumulate."""
        self._state.fill(order_id, price, quantity)

    def cancel(self, order_id: str) -> None:
        """Cancel an open order (as the exchange would)."""
        self._state.cancel(order_id)

    def trigger(self, order_id: str) -> None:
        """TRIGGER_PENDING -> OPEN."""
        self._state.trigger(order_id)

    def trigger_gtt(self, alert_id: str) -> str:
        """Fire a pending GTT: its order is placed (as ``default_place`` says, with
        ``add_ord_id`` = ``alert_id``) and the GTT moves to ``triggered_gtts()``. Returns the
        new order id. A GTT never fires on its own in the offline broker.
        """
        return self._state.trigger_gtt(alert_id)

    def fail_next(self, endpoint: str, drill: Drill) -> None:
        """Arm ``drill`` (from ``tradesmart.testing.drills``) for the next call of ``endpoint``."""
        self._state.fail_next(endpoint, drill)

    # --- read access and settings --------------------------------------------------------------

    @property
    def orders(self) -> list[OfflineOrder]:
        """The orders the strategy sent, oldest first (a new list; the orders are live)."""
        with self._state.lock:
            return list(self._state.orders)

    @property
    def requests(self) -> list[OfflineRequest]:
        """Every REST call received, oldest first (secrets masked)."""
        with self._state.lock:
            return list(self._state.requests)

    @property
    def client_id(self) -> str:
        """The client id of the offline broker account (``"OFFLINE01"`` unless given)."""
        return self._state.client_id

    @property
    def default_place(self) -> str:
        """How a new order is booked: ``"open"`` (default), ``"pending"`` or ``"reject"``."""
        with self._state.lock:
            return self._state.default_place

    @default_place.setter
    def default_place(self, value: str) -> None:
        if value not in _DEFAULT_PLACE:
            raise ValueError(f"default_place must be 'open', 'pending' or 'reject', not {value!r}")
        with self._state.lock:
            self._state.default_place = value

    @property
    def default_reject_message(self) -> str:
        """The reason given to orders rejected by ``default_place = "reject"``."""
        with self._state.lock:
            return self._state.default_reject_message

    @default_reject_message.setter
    def default_reject_message(self, value: str) -> None:
        with self._state.lock:
            self._state.default_reject_message = value

    # --- stream drills -------------------------------------------------------------------------

    def drop_stream(self) -> None:
        """Cut every stream connection; the real clients reconnect and resubscribe."""
        self._server.drop_all()

    def refuse_stream_login(self) -> None:
        """Answer the next stream connect with ``s="Not_Ok"`` (``AuthenticationError``)."""
        with self._state.lock:
            self._state.refuse_login_once = True

    def silence_stream(self) -> None:
        """Stop answering heartbeats, so the clients' silence detection fires."""
        with self._state.lock:
            self._state.silenced = True

    def resume_stream(self) -> None:
        """Answer heartbeats again."""
        with self._state.lock:
            self._state.silenced = False

    # --- helpers -------------------------------------------------------------------------------

    def _owned(self, built: _Built) -> _Built:
        """Keep this broker alive (server, folder) while ``built`` is in use."""
        with _OWNERS_LOCK:
            _OWNERS[built] = self
        return built

    def _ws_url(self) -> str:
        """The stream server's URL; starts the server on first use."""
        with self._lock:
            if self._closed:
                raise RuntimeError("OfflineBroker is closed; create a new one")
            if not self._started:
                self._server.start()
                self._started = True
        return self._server.url

    def _forget_script_master(self, exchange: str) -> None:
        """Delete the exchange's cached script master, so the next read gets the new list.

        The SDK reuses a file written since the last daily refresh without asking the server;
        all clients of this broker share the private folder.
        """
        try:
            code = Exchange(exchange).value
        except ValueError:
            return  # no script master is served for an unknown exchange
        cached = Path(self._instruments_dir) / f"{code}_symbols.txt"
        for path in (cached, cached.with_name(cached.name + ".checked")):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def _config(self, overrides: dict[str, Any]) -> ClientConfig:
        for name in _OFFLINE_URLS:
            if name in overrides:
                raise ValueError(
                    f"OfflineBroker sets {name} itself (it points the client at itself); "
                    "remove it from the overrides"
                )
        settings: dict[str, Any] = {"instruments_dir": self._instruments_dir, **overrides}
        return offline_config(self._ws_url(), **settings)

    def _token(self) -> AccessToken:
        """A synthetic login, valid for 24 hours.

        Not the real 06:00 IST expiry: the broker cannot move the clock, so that expiry would only
        make a test that runs across 06:00 IST fail.
        """
        issued = datetime.now(IST)
        token = AccessToken.from_response(
            {
                "stat": "Ok",
                "access_token": f"offline-token-{secrets.token_hex(16)}",
                "uid": self.client_id,
                "actid": self.client_id,
            },
            issued_at=issued,
        )
        return token.model_copy(update={"expires_at": issued + _LOGIN_LIFETIME})
