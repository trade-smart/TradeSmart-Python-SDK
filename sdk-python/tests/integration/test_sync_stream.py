import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import replace

import pytest
from websockets.exceptions import ConnectionClosed

import tradesmart
import tradesmart.stream
from tests.integration.ws_server import MockWsServer, wait_until_sync
from tradesmart import (
    AuthenticationError,
    ClientConfig,
    NetworkError,
    RequestTimeoutError,
    RequestValidationError,
    SubscriptionLimitError,
    TradeSmartError,
)
from tradesmart.models.stream import (
    DepthFeed,
    OrderUpdate,
    PositionUpdate,
    TouchlineAck,
    TouchlineFeed,
    UnknownMessage,
    UnsubscribeAck,
)
from tradesmart.stream import AsyncTradeSmartStream, StreamState, TradeSmartStream


def cfg(srv: MockWsServer) -> ClientConfig:
    return ClientConfig(
        ws_url=srv.url,
        heartbeat_interval=0.05,
        reconnect_backoff_base=0.01,
        reconnect_backoff_cap=0.02,
        ws_auth_timeout=1.0,
    )


def steady_cfg(srv: MockWsServer) -> ClientConfig:
    """For tests asserting exact connection counts or no reconnect: a 0.4 s silence margin."""
    return replace(cfg(srv), heartbeat_interval=0.2)


def stopped(s: TradeSmartStream) -> bool:
    """The stream reached CLOSED and its threads are gone (all callbacks delivered)."""
    threads = (s._thread, s._callback_thread)
    return s.state is StreamState.CLOSED and not any(
        t is not None and t.is_alive() for t in threads
    )


# --- brief tests -------------------------------------------------------------


def test_sync_stream_callbacks(threaded_server):
    got, connected = [], threading.Event()
    s = TradeSmartStream(
        "TEST123",
        "tok",
        config=cfg(threaded_server),
        on_touchline=got.append,
        on_connect=connected.set,
    )
    s.start()
    s.subscribe_touchline(["NSE|22"])
    wait_until_sync(lambda: got)
    s.stop()
    assert connected.is_set() and isinstance(got[0], TouchlineAck) and s.state is StreamState.CLOSED
    assert stopped(s)


def test_callback_exception_does_not_kill_stream(threaded_server):
    errors = []

    def boom(_):
        raise RuntimeError("user bug")

    s = TradeSmartStream(
        "TEST123", "tok", config=cfg(threaded_server), on_touchline=boom, on_error=errors.append
    )
    s.start()
    s.subscribe_touchline(["NSE|22", "NSE|23"])
    wait_until_sync(lambda: len(errors) >= 2)
    assert s.state is StreamState.CONNECTED
    s.stop()
    assert stopped(s)


def test_start_auth_failure_raises(threaded_server_bad_auth):
    s = TradeSmartStream("TEST123", "bad", config=cfg(threaded_server_bad_auth))
    with pytest.raises(AuthenticationError):
        s.start()
    assert stopped(s)


# --- dispatch ------------------------------------------------------------------


def test_on_message_runs_before_type_specific_callback(threaded_server):
    calls = []
    s = TradeSmartStream(
        "TEST123",
        "tok",
        config=cfg(threaded_server),
        on_message=lambda m: calls.append(("message", type(m))),
        on_touchline=lambda m: calls.append(("touchline", type(m))),
        on_depth=lambda m: calls.append(("depth", type(m))),
        on_order_update=lambda m: calls.append(("order", type(m))),
        on_position=lambda m: calls.append(("position", type(m))),
    )
    s.start()
    try:
        s.subscribe_touchline(["NSE|22"])
        wait_until_sync(lambda: len(calls) >= 2)
        for msg in (
            {"t": "tf", "e": "NSE", "tk": "22", "lp": "101.00"},
            {"t": "df", "e": "NSE", "tk": "22", "bp1": "100.00"},
            {"t": "om", "norenordno": "1", "status": "OPEN"},
            {"t": "pm", "tsym": "ACC-EQ"},
            {"t": "uk"},
        ):
            threaded_server.run(threaded_server.send(msg))
        wait_until_sync(lambda: len(calls) >= 11)
    finally:
        s.stop()
    assert calls == [
        ("message", TouchlineAck),
        ("touchline", TouchlineAck),
        ("message", TouchlineFeed),
        ("touchline", TouchlineFeed),
        ("message", DepthFeed),
        ("depth", DepthFeed),
        ("message", OrderUpdate),
        ("order", OrderUpdate),
        ("message", PositionUpdate),
        ("position", PositionUpdate),
        ("message", UnsubscribeAck),
    ]
    assert s.snapshot("NSE|22")["lp"] == "101.00"


def test_callback_errors_are_logged(threaded_server, caplog):
    def boom(_):
        raise RuntimeError("user bug")

    def bad_on_error(_):
        raise ValueError("on_error bug")

    s = TradeSmartStream(
        "TEST123", "tok", config=cfg(threaded_server), on_message=boom, on_error=bad_on_error
    )
    with caplog.at_level(logging.ERROR, logger="tradesmart.stream"):
        s.start()
        s.subscribe_touchline(["NSE|22"])
        wait_until_sync(
            lambda: any("on_error" in r.getMessage() for r in caplog.records), timeout=2
        )
        assert s.state is StreamState.CONNECTED
        s.stop()
    messages = [r.getMessage() for r in caplog.records if r.name == "tradesmart.stream"]
    assert any("on_message" in m for m in messages)
    assert stopped(s)


def test_reconnect_fires_disconnect_then_connect(threaded_server):
    events = []
    s = TradeSmartStream(
        "TEST123",
        "tok",
        config=steady_cfg(threaded_server),
        on_connect=lambda: events.append("connect"),
        on_disconnect=lambda: events.append("disconnect"),
    )
    s.start()
    wait_until_sync(lambda: events == ["connect"])  # on_connect runs on the callback thread
    threaded_server.run(threaded_server.drop_all())
    wait_until_sync(lambda: events.count("connect") == 2)
    assert s.state is StreamState.CONNECTED
    s.stop()
    assert events == ["connect", "disconnect", "connect", "disconnect"]


def test_terminal_error_goes_to_on_error_and_thread_ends(threaded_server):
    errors = []
    config = replace(cfg(threaded_server), reconnect_max_attempts=0)
    s = TradeSmartStream("TEST123", "tok", config=config, on_error=errors.append)
    s.start()
    threaded_server.run(threaded_server.drop_all())
    wait_until_sync(lambda: stopped(s))
    assert len(errors) == 1 and isinstance(errors[0], NetworkError)
    s.stop()  # still fine once the stream ended on its own


# --- sync subscribe API -------------------------------------------------------------


def test_subscribe_errors_are_raised_in_caller(threaded_server):
    s = TradeSmartStream(
        "TEST123", "tok", config=replace(cfg(threaded_server), heartbeat_interval=5.0)
    )
    s.start()
    try:
        with pytest.raises(SubscriptionLimitError):
            s.subscribe_depth([f"NSE|{i}" for i in range(1001)])
        with pytest.raises(RequestValidationError):
            s.subscribe_touchline(["not-a-key"])
        s.subscribe_orders()
        s.subscribe_positions()
        s.unsubscribe_touchline(["NSE|22"])
        s.unsubscribe_depth(["NSE|22"])
        s.unsubscribe_orders()
        s.unsubscribe_positions()
        expected = ["a", "o", "p", "u", "ud", "uo", "up"]  # nothing sent for rejected calls
        wait_until_sync(
            lambda: [m["t"] for m in threaded_server.received if m["t"] != "h"] == expected,
            timeout=30,
        )
        assert s.state is StreamState.CONNECTED
    finally:
        s.stop()


def test_subscribe_before_start_and_after_stop_raises(threaded_server):
    s = TradeSmartStream("TEST123", "tok", config=cfg(threaded_server))
    with pytest.raises(TradeSmartError, match="start"):
        s.subscribe_touchline(["NSE|22"])
    s.start()
    s.stop()
    with pytest.raises(TradeSmartError):
        s.subscribe_orders()


_SYNC_SUBSCRIPTION_CALLS = [
    ("subscribe_touchline", (["NSE|22"],)),
    ("unsubscribe_touchline", (["NSE|22"],)),
    ("subscribe_depth", (["NSE|22"],)),
    ("unsubscribe_depth", (["NSE|22"],)),
    ("subscribe_orders", ()),
    ("unsubscribe_orders", ()),
    ("subscribe_positions", ()),
    ("unsubscribe_positions", ()),
]


def test_subscribe_after_stop_raises_closed_immediately(threaded_server):
    s = TradeSmartStream("TEST123", "tok", config=cfg(threaded_server))
    s.start()
    s.stop()
    for name, args in _SYNC_SUBSCRIPTION_CALLS:
        started = time.monotonic()
        with pytest.raises(TradeSmartError, match="stream is closed; create a new stream"):
            getattr(s, name)(*args)
        assert time.monotonic() - started < 1.0


def test_subscribe_after_terminal_error_raises_closed(threaded_server):
    errors = []
    config = replace(cfg(threaded_server), reconnect_max_attempts=1)
    s = TradeSmartStream("TEST123", "tok", config=config, on_error=errors.append)
    s.start()
    threaded_server.ignore = {"a"}  # reconnects never authenticate
    threaded_server.run(threaded_server.drop_all())
    wait_until_sync(lambda: s.state is StreamState.CLOSED, timeout=5)
    started = time.monotonic()
    with pytest.raises(TradeSmartError, match="stream is closed"):
        s.subscribe_orders()
    assert time.monotonic() - started < 1.0
    s.stop()


def test_subscribe_after_stop_requested_from_callback_raises(threaded_server):
    holder, raised = {}, []

    def on_touchline(_):
        holder["s"].stop()  # only requests the shutdown from the stream thread
        try:
            holder["s"].subscribe_orders()
        except TradeSmartError as exc:
            raised.append(exc)

    s = TradeSmartStream("TEST123", "tok", config=cfg(threaded_server), on_touchline=on_touchline)
    holder["s"] = s
    s.start()
    s.subscribe_touchline(["NSE|22"])
    wait_until_sync(lambda: stopped(s))
    assert len(raised) == 1 and "stream is closed" in str(raised[0])
    assert {"t": "o", "actid": "TEST123"} not in threaded_server.received


def test_subscribe_from_callback_does_not_deadlock(threaded_server):
    got = []
    holder = {}
    s = TradeSmartStream(
        "TEST123",
        "tok",
        config=cfg(threaded_server),
        on_connect=lambda: holder["s"].subscribe_touchline(["NSE|22"]),
        on_touchline=got.append,
    )
    holder["s"] = s
    s.start()
    wait_until_sync(lambda: got)
    s.stop()
    assert isinstance(got[0], TouchlineAck)


def test_subscribe_error_from_callback_goes_to_on_error(threaded_server):
    errors = []
    holder = {}
    s = TradeSmartStream(
        "TEST123",
        "tok",
        config=cfg(threaded_server),
        on_connect=lambda: holder["s"].subscribe_touchline(["bad"]),
        on_error=errors.append,
    )
    holder["s"] = s
    s.start()
    wait_until_sync(lambda: errors)
    s.stop()
    assert isinstance(errors[0], RequestValidationError)


# --- lifecycle ---------------------------------------------------------------------


def test_start_twice_is_noop_and_restart_after_stop_raises(threaded_server):
    s = TradeSmartStream("TEST123", "tok", config=steady_cfg(threaded_server))
    s.start()
    thread = s._thread
    s.start()
    assert s._thread is thread and threaded_server.connections == 1
    s.stop()
    s.stop()  # idempotent
    assert stopped(s)
    with pytest.raises(TradeSmartError, match="new TradeSmartStream"):
        s.start()


def test_stop_before_start_is_noop(threaded_server):
    s = TradeSmartStream("TEST123", "tok", config=cfg(threaded_server))
    s.stop()
    assert s.state is StreamState.DISCONNECTED


def test_stop_from_callback(threaded_server):
    holder = {}
    s = TradeSmartStream(
        "TEST123", "tok", config=cfg(threaded_server), on_touchline=lambda _: holder["s"].stop()
    )
    holder["s"] = s
    s.start()
    s.subscribe_touchline(["NSE|22"])
    wait_until_sync(lambda: stopped(s))


def test_start_timeout_stops_thread(threaded_server):
    threaded_server.ignore = {"a"}  # never acknowledge the connect
    config = replace(cfg(threaded_server), ws_auth_timeout=5.0)
    s = TradeSmartStream("TEST123", "tok", config=config)
    with pytest.raises(RequestTimeoutError):
        s.start(timeout=0.2)
    assert stopped(s)
    wait_until_sync(lambda: threaded_server.open_connections == 0)


def test_run_forever_returns_after_stop(threaded_server):
    s = TradeSmartStream("TEST123", "tok", config=cfg(threaded_server))
    runner = threading.Thread(target=s.run_forever)
    runner.start()
    wait_until_sync(lambda: s.state is StreamState.CONNECTED)
    s.stop()
    runner.join(2)
    assert not runner.is_alive() and stopped(s)


def test_run_forever_keyboard_interrupt_stops(threaded_server, monkeypatch):
    s = TradeSmartStream("TEST123", "tok", config=cfg(threaded_server))
    s.start()
    thread = s._thread
    real_join = thread.join
    interrupted = []

    def join(timeout=None):
        if not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt
        real_join(timeout)

    monkeypatch.setattr(thread, "join", join)
    s.run_forever()  # returns instead of propagating the interrupt
    assert interrupted and stopped(s)


# --- exports -----------------------------------------------------------------------


def test_exports():
    for name in ("TradeSmartStream", "AsyncTradeSmartStream", "StreamState"):
        assert name in tradesmart.stream.__all__
        assert name in tradesmart.__all__
    assert tradesmart.TradeSmartStream is TradeSmartStream
    assert tradesmart.AsyncTradeSmartStream is AsyncTradeSmartStream
    assert tradesmart.TouchlineAck is TouchlineAck and tradesmart.OrderUpdate is OrderUpdate


# --- fix round 1 -------------------------------------------------------------------


def test_subscribe_error_in_callback_is_logged_without_on_error(threaded_server, caplog):
    holder = {}
    s = TradeSmartStream(
        "TEST123",
        "tok",
        config=cfg(threaded_server),
        on_connect=lambda: holder["s"].subscribe_touchline(["bad"]),
    )
    holder["s"] = s
    with caplog.at_level(logging.ERROR, logger="tradesmart.stream"):
        s.start()
        wait_until_sync(
            lambda: any(
                r.name == "tradesmart.stream" and "on_connect callback raised" in r.getMessage()
                for r in caplog.records
            )
        )
        assert s.state is StreamState.CONNECTED
        s.stop()
    record = next(r for r in caplog.records if "on_connect" in r.getMessage())
    assert record.exc_info is not None and isinstance(record.exc_info[1], RequestValidationError)
    assert "tok" not in record.getMessage()


def test_failed_auth_never_fires_on_disconnect(threaded_server_bad_auth):
    events = []
    s = TradeSmartStream(
        "TEST123",
        "bad",
        config=cfg(threaded_server_bad_auth),
        on_connect=lambda: events.append("connect"),
        on_disconnect=lambda: events.append("disconnect"),
    )
    with pytest.raises(AuthenticationError):
        s.start()
    assert events == [] and stopped(s)


def test_reconnect_exhaustion_fires_one_disconnect(threaded_server):
    events = []
    config = replace(cfg(threaded_server), reconnect_max_attempts=0)
    s = TradeSmartStream(
        "TEST123",
        "tok",
        config=config,
        on_connect=lambda: events.append("connect"),
        on_disconnect=lambda: events.append("disconnect"),
        on_error=lambda e: events.append("error"),
    )
    s.start()
    threaded_server.run(threaded_server.drop_all())
    wait_until_sync(lambda: stopped(s))
    s.stop()
    assert events == ["connect", "disconnect", "error"]


def test_stop_fires_one_disconnect(threaded_server):
    events = []
    s = TradeSmartStream(
        "TEST123",
        "tok",
        config=cfg(threaded_server),
        on_connect=lambda: events.append("connect"),
        on_disconnect=lambda: events.append("disconnect"),
    )
    s.start()
    s.stop()
    s.stop()
    assert events == ["connect", "disconnect"]


def test_start_default_timeout_covers_connect(threaded_server, monkeypatch):
    # The connect can take up to open_timeout + ack timeout, i.e. 2 * ws_auth_timeout.
    threaded_server.ack_delay = 0.8
    config = replace(cfg(threaded_server), ws_auth_timeout=1.0)
    s = TradeSmartStream("TEST123", "tok", config=config)
    waits: list[float | None] = []
    real_result = s._ready.result

    def spy(timeout=None):
        waits.append(timeout)
        return real_result(timeout)

    monkeypatch.setattr(s._ready, "result", spy)
    s.start()  # no explicit timeout
    try:
        assert waits == [2 * 1.0 + 5]
        assert s.state is StreamState.CONNECTED
    finally:
        s.stop()


def test_subscribe_timeout_message(threaded_server, monkeypatch):
    monkeypatch.setattr("tradesmart.stream.sync_stream._SUBMIT_TIMEOUT", 0.2)
    s = TradeSmartStream("TEST123", "tok", config=cfg(threaded_server))

    async def never_finishes(_instruments):
        await asyncio.sleep(30)

    monkeypatch.setattr(s._stream, "subscribe_touchline", never_finishes)
    s.start()
    try:
        with pytest.raises(RequestTimeoutError, match="may still take effect"):
            s.subscribe_touchline(["NSE|22"])
    finally:
        s.stop()


# --- callback thread (C4, C5) --------------------------------------------------------


def heartbeats(srv) -> int:
    return sum(m == {"t": "h"} for m in list(srv.received))


def test_slow_sync_callback_does_not_block_heartbeat(threaded_server):
    marks, events, in_callback = {}, [], threading.Event()

    def on_touchline(_):
        marks["start"] = heartbeats(threaded_server)
        in_callback.set()
        time.sleep(2)
        marks["end"] = heartbeats(threaded_server)

    config = replace(cfg(threaded_server), heartbeat_interval=0.5)
    s = TradeSmartStream(
        "TEST123",
        "tok",
        config=config,
        on_touchline=on_touchline,
        on_disconnect=lambda: events.append("disconnect"),
    )
    s.start()
    try:
        s.subscribe_touchline(["NSE|22"])
        assert in_callback.wait(2)
        time.sleep(3)
        assert s.state is StreamState.CONNECTED
        assert threaded_server.connections == 1 and events == []
        assert marks["end"] - marks["start"] >= 2  # heartbeats kept flowing during the callback
    finally:
        s.stop()


def test_stop_from_callback_thread(threaded_server):
    calls = []

    def on_order_update(_):
        calls.append(threading.current_thread().name)
        s.stop()  # only requests the shutdown; must not join its own thread
        calls.append("returned")

    s = TradeSmartStream(
        "TEST123", "tok", config=cfg(threaded_server), on_order_update=on_order_update
    )
    s.start()
    s.subscribe_orders()
    threaded_server.run(threaded_server.send({"t": "om", "norenordno": "1", "status": "OPEN"}))
    wait_until_sync(lambda: stopped(s), timeout=5)
    assert calls == ["tradesmart-callbacks", "returned"]


def test_backlog_survives_reconnect(threaded_server):
    got = []

    def on_order_update(msg):
        time.sleep(0.1)
        got.append(msg.order_id)

    s = TradeSmartStream(
        "TEST123", "tok", config=steady_cfg(threaded_server), on_order_update=on_order_update
    )
    s.start()
    try:
        for n in (1, 2, 3):
            threaded_server.run(
                threaded_server.send({"t": "om", "norenordno": str(n), "status": "OPEN"})
            )
        threaded_server.run(threaded_server.drop_all())
        wait_until_sync(
            lambda: threaded_server.connections == 2 and s.state is StreamState.CONNECTED
        )
        wait_until_sync(lambda: len(got) == 3)
        assert got == ["1", "2", "3"]
    finally:
        s.stop()


def test_callbacks_run_on_callback_thread(threaded_server):
    calls = []

    def record(name):
        return lambda *_: calls.append((name, threading.current_thread().name))

    s = TradeSmartStream(
        "TEST123",
        "tok",
        config=cfg(threaded_server),
        on_connect=record("connect"),
        on_touchline=record("touchline"),
        on_disconnect=record("disconnect"),
    )
    s.start()
    s.subscribe_touchline(["NSE|22"])
    wait_until_sync(lambda: any(name == "touchline" for name, _ in calls))
    s.stop()
    assert {name for name, _ in calls} == {"connect", "touchline", "disconnect"}
    assert {thread for _, thread in calls} == {"tradesmart-callbacks"}
    assert s._callback_thread.daemon and not s._callback_thread.is_alive()


def test_stop_does_not_hang_on_blocked_callback(threaded_server):
    release, entered = threading.Event(), threading.Event()

    def on_touchline(_):
        entered.set()
        release.wait(10)

    s = TradeSmartStream("TEST123", "tok", config=cfg(threaded_server), on_touchline=on_touchline)
    s.start()
    try:
        s.subscribe_touchline(["NSE|22"])
        assert entered.wait(2)
        started = time.monotonic()
        s.stop(timeout=1.0)
        assert time.monotonic() - started < 3
        assert s.state is StreamState.CLOSED and not s._thread.is_alive()
    finally:
        release.set()


def test_subscribe_from_callback_waits_and_raises_in_callback(threaded_server):
    outcome = []

    def on_connect():
        # The loop is free while callbacks run: subscribe waits for the loop and
        # raises its error right here instead of being scheduled in the background.
        try:
            s.subscribe_touchline(["bad"])
        except RequestValidationError as exc:
            outcome.append(exc)
        s.subscribe_orders()
        outcome.append("subscribed")

    s = TradeSmartStream(
        "TEST123",
        "tok",
        config=replace(cfg(threaded_server), heartbeat_interval=5.0),
        on_connect=on_connect,
    )
    s.start()
    try:
        wait_until_sync(lambda: len(outcome) == 2, timeout=30)
        assert isinstance(outcome[0], RequestValidationError) and outcome[1] == "subscribed"
        wait_until_sync(
            lambda: {"t": "o", "actid": "TEST123"} in threaded_server.received_on(1), timeout=30
        )
    finally:
        s.stop()


# --- fix round 1 (task 8) ----------------------------------------------------------


def test_run_forever_returns_only_after_queued_callbacks(threaded_server):
    got, errors = [], []

    def on_order_update(msg):
        time.sleep(0.3)  # slow: the backlog is still queued when the stream ends
        got.append(msg.order_id)

    config = replace(cfg(threaded_server), reconnect_max_attempts=0)
    s = TradeSmartStream(
        "TEST123", "tok", config=config, on_order_update=on_order_update, on_error=errors.append
    )
    s.start()
    for n in (1, 2, 3):
        threaded_server.run(
            threaded_server.send({"t": "om", "norenordno": str(n), "status": "OPEN"})
        )
    dropper = threading.Timer(0.1, lambda: threaded_server.run(threaded_server.drop_all()))
    dropper.start()
    try:
        s.run_forever()  # the terminal error ends the stream while callbacks lag behind
    finally:
        dropper.join()
    assert got == ["1", "2", "3"]
    assert len(errors) == 1 and isinstance(errors[0], NetworkError)
    assert stopped(s)


def test_callback_base_exception_closes_stream_and_is_reported(threaded_server, caplog):
    errors = []

    def on_touchline(_):
        raise SystemExit("callback called sys.exit()")

    s = TradeSmartStream(
        "TEST123",
        "tok",
        config=cfg(threaded_server),
        on_touchline=on_touchline,
        on_error=errors.append,
    )
    with caplog.at_level(logging.ERROR, logger="tradesmart.stream"):
        s.start()
        s.subscribe_touchline(["NSE|22", "NSE|23"])  # a second message is queued behind
        # The stream may already be closed by the SystemExit above: that is the point of the test.
        with contextlib.suppress(ConnectionClosed):
            threaded_server.run(threaded_server.send({"t": "om", "norenordno": "1"}))
        wait_until_sync(lambda: stopped(s), timeout=5)
    assert len(errors) == 1 and isinstance(errors[0], SystemExit)
    assert any("callback thread stopped" in r.getMessage() and r.exc_info for r in caplog.records)
    with pytest.raises(TradeSmartError, match="stream is closed"):
        s.subscribe_orders()
    s.stop()  # still fine


def test_order_update_with_numeric_text_fields_reaches_callback(threaded_server):
    got = []
    s = TradeSmartStream("TEST123", "tok", config=cfg(threaded_server), on_order_update=got.append)
    s.start()
    try:
        s.subscribe_orders()
        frame = {"t": "om", "norenordno": "77", "status": "OPEN", "tm": 1731234567, "pcode": 1}
        threaded_server.run(threaded_server.send(frame))
        wait_until_sync(lambda: got)
    finally:
        s.stop()
    assert isinstance(got[0], OrderUpdate) and got[0].order_id == "77" and got[0].tm == "1731234567"


def test_order_update_with_bad_order_id_reaches_on_message_as_unknown(threaded_server):
    messages, updates = [], []
    s = TradeSmartStream(
        "TEST123",
        "tok",
        config=cfg(threaded_server),
        on_message=messages.append,
        on_order_update=updates.append,
    )
    s.start()
    try:
        s.subscribe_orders()
        frame = {"t": "om", "norenordno": True, "status": "OPEN"}
        threaded_server.run(threaded_server.send(frame))
        wait_until_sync(lambda: any(m.t == "om" for m in messages))
    finally:
        s.stop()
    om = [m for m in messages if m.t == "om"]
    assert isinstance(om[0], UnknownMessage) and om[0].raw["norenordno"] is True
    assert updates == []
