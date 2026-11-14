import asyncio
import logging
import socket
from dataclasses import replace

import pytest

from tests.integration.ws_server import MockWsServer, wait_until
from tradesmart import (
    AccessToken,
    AuthenticationError,
    ClientConfig,
    NetworkError,
    RequestTimeoutError,
    SubscriptionLimitError,
    TradeSmartError,
)
from tradesmart.models.stream import TouchlineAck, TouchlineFeed, UnknownMessage
from tradesmart.stream.async_stream import AsyncTradeSmartStream, _reconnect_delay
from tradesmart.stream.state import StreamState


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


# --- brief tests -------------------------------------------------------------


async def test_connect_subscribe_receive():
    async with MockWsServer() as srv:
        async with AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv)) as s:
            s.__aiter__()  # start iterating before subscribing (messages queue from here)
            await s.subscribe_touchline(["NSE|22"])
            msg = await asyncio.wait_for(s.__anext__(), 1)
            assert isinstance(msg, TouchlineAck) and s.state is StreamState.CONNECTED
        assert srv.received[0] == {
            "t": "a",
            "uid": "TEST123",
            "actid": "TEST123",
            "source": "API",
            "accesstoken": "tok",
        }


async def test_auth_failure_no_reconnect():
    async with MockWsServer(auth_ok=False) as srv:
        stream = AsyncTradeSmartStream("TEST123", "bad", config=steady_cfg(srv))
        with pytest.raises(AuthenticationError):
            await stream.connect()
        await asyncio.sleep(0.1)
        assert srv.connections == 1
        assert stream.state is StreamState.CLOSED


@pytest.mark.parametrize("ok", ["OK", "ok", " Ok "])
async def test_auth_ack_accepts_ok_in_any_case(ok: str):
    # Observed live 2026-11-10: the connect ack carries s="OK" (the docs say "Ok").
    async with MockWsServer() as srv:
        srv.ok_status = ok
        async with AsyncTradeSmartStream("TEST123", "tok", config=steady_cfg(srv)) as s:
            assert s.state is StreamState.CONNECTED


@pytest.mark.parametrize("bad", ["Not_Ok", "NOT_OK", "", "Okay"])
async def test_auth_ack_other_status_is_rejected(bad: str):
    async with MockWsServer() as srv:
        srv.ok_status = bad
        stream = AsyncTradeSmartStream("TEST123", "tok", config=steady_cfg(srv))
        with pytest.raises(AuthenticationError):
            await stream.connect()


async def test_heartbeat_sent():
    async with MockWsServer() as srv:
        async with AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv)):
            # ~0.1s normally; polled rather than a fixed sleep so CPU load cannot flake it
            await wait_until(lambda: sum(m == {"t": "h"} for m in srv.received) >= 2, timeout=30)
        assert sum(m == {"t": "h"} for m in srv.received) >= 2


async def test_reconnect_restores_subscriptions_once():
    async with (
        MockWsServer() as srv,
        AsyncTradeSmartStream("TEST123", "tok", config=steady_cfg(srv)) as s,
    ):
        await s.subscribe_touchline(["NSE|22"])
        await s.subscribe_orders()
        await srv.drop_all()
        await wait_until(lambda: srv.connections == 2 and s.state is StreamState.CONNECTED)
        # CONNECTED means the restore frames were sent, not yet read by the server:
        # a later frame on the same connection arrives after them (in-order fence).
        await s.unsubscribe_positions()
        await wait_until(lambda: {"t": "up"} in srv.received_on(connection=2))
        after = srv.received_on(connection=2)
        assert after[0]["t"] == "a"
        assert {"t": "t", "k": "NSE|22"} in after
        assert {"t": "o", "actid": "TEST123"} in after
        assert sum(m.get("t") == "t" for m in after) == 1


async def test_reconnect_exhausted_raises_from_iterator():
    async with MockWsServer() as srv:
        s = AsyncTradeSmartStream(
            "TEST123", "tok", config=replace(cfg(srv), reconnect_max_attempts=1)
        )
        await s.connect()
    with pytest.raises(NetworkError):  # server gone
        await asyncio.wait_for(s.__anext__(), 2)
    assert s.state is StreamState.CLOSED


async def test_subscription_limit_enforced_before_send():
    async with MockWsServer() as srv, AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv)) as s:
        with pytest.raises(SubscriptionLimitError):
            await s.subscribe_touchline([f"NSE|{i}" for i in range(1001)])
        assert not any(m.get("t") == "t" for m in srv.received)


async def test_unparseable_frame_does_not_kill_stream():
    async with MockWsServer() as srv, AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv)) as s:
        s.__aiter__()
        await srv.send({"t": "tf", "e": "NSE", "tk": "22", "ft": "inf"})
        await srv.send({"t": "tf", "e": "NSE", "tk": "22", "lp": "101.25"})
        bad = await asyncio.wait_for(s.__anext__(), 1)
        good = await asyncio.wait_for(s.__anext__(), 1)
        assert isinstance(bad, UnknownMessage) and bad.t == "tf"
        assert isinstance(good, TouchlineFeed) and str(good.last_price) == "101.25"
        assert s.state is StreamState.CONNECTED


_SUBSCRIPTION_CALLS = [
    ("subscribe_touchline", (["NSE|22"],)),
    ("unsubscribe_touchline", (["NSE|22"],)),
    ("subscribe_depth", (["NSE|22"],)),
    ("unsubscribe_depth", (["NSE|22"],)),
    ("subscribe_orders", ()),
    ("unsubscribe_orders", ()),
    ("subscribe_positions", ()),
    ("unsubscribe_positions", ()),
]


async def test_subscribe_after_close_raises():
    async with MockWsServer() as srv:
        s = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv))
        await s.connect()
        await s.close()
        for name, args in _SUBSCRIPTION_CALLS:
            with pytest.raises(TradeSmartError, match="stream is closed; create a new stream"):
                await asyncio.wait_for(getattr(s, name)(*args), 1)
        assert s.state is StreamState.CLOSED


async def test_subscribe_after_failed_auth_raises():
    async with MockWsServer(auth_ok=False) as srv:
        s = AsyncTradeSmartStream("TEST123", "bad", config=cfg(srv))
        with pytest.raises(AuthenticationError):
            await s.connect()
        with pytest.raises(TradeSmartError, match="stream is closed"):
            await s.subscribe_touchline(["NSE|22"])


# --- additional coverage ------------------------------------------------------


async def test_state_transitions_and_sync_on_message():
    states: list[StreamState] = []
    messages: list[object] = []
    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream(
            "TEST123",
            "tok",
            config=cfg(srv),
            on_message=messages.append,
            on_state_change=states.append,
        )
        async with stream:
            await stream.subscribe_touchline([("NSE", "22")])
            await wait_until(lambda: len(messages) == 1)
    assert isinstance(messages[0], TouchlineAck)
    assert states == [
        StreamState.CONNECTING,
        StreamState.AUTHENTICATING,
        StreamState.CONNECTED,
        StreamState.CLOSED,
    ]


async def test_async_callback_and_callback_errors_are_logged_and_swallowed(caplog):
    caplog.set_level(logging.ERROR, logger="tradesmart")
    seen: list[object] = []

    async def on_message(msg):
        seen.append(msg)
        raise ValueError("boom in callback")

    def on_state_change(state):
        raise RuntimeError("boom in state callback")

    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream(
            "TEST123",
            "tok",
            config=cfg(srv),
            on_message=on_message,
            on_state_change=on_state_change,
        )
        async with stream:
            stream.__aiter__()
            await stream.subscribe_touchline(["NSE|22", "NSE|23"])
            await wait_until(lambda: len(seen) == 2)
            assert stream.state is StreamState.CONNECTED
            first = await asyncio.wait_for(stream.__anext__(), 1)
            assert isinstance(first, TouchlineAck)
    assert "boom in callback" in caplog.text
    assert "boom in state callback" in caplog.text
    assert any(r.name == "tradesmart.stream" for r in caplog.records)


async def test_internal_messages_not_queued_and_snapshot_merges():
    async with MockWsServer() as srv, AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv)) as s:
        s.__aiter__()
        await s.subscribe_touchline(["NSE|22"])
        ack = await asyncio.wait_for(s.__anext__(), 1)
        assert isinstance(ack, TouchlineAck)
        await srv.send({"t": "ak", "uid": "TEST123", "s": "Ok"})
        await srv.send({"t": "hk", "ft": "1700000000"})
        await srv.send({"t": "tf", "e": "NSE", "tk": "22", "lp": "101.25", "v": "10"})
        feed = await asyncio.wait_for(s.__anext__(), 1)
        assert isinstance(feed, TouchlineFeed)
        snap = s.snapshot("NSE|22")
        assert snap is not None
        assert snap["lp"] == "101.25" and snap["v"] == "10" and snap["e"] == "NSE"
        assert "t" not in snap
        assert s.snapshot("NSE|99") is None


async def test_subscribe_before_connect_is_sent_after_auth():
    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv))
        await stream.subscribe_touchline(["NSE|22"])
        await stream.subscribe_positions()
        async with stream:
            await wait_until(lambda: len(srv.received) >= 3)
            assert srv.received[0]["t"] == "a"
            assert srv.received[1:3] == [
                {"t": "t", "k": "NSE|22"},
                {"t": "p", "uid": "TEST123"},
            ]


async def test_unsubscribe_sends_and_is_not_restored():
    async with (
        MockWsServer() as srv,
        AsyncTradeSmartStream("TEST123", "tok", config=steady_cfg(srv)) as s,
    ):
        await s.subscribe_depth(["NSE|22"])
        await s.subscribe_positions()
        await s.subscribe_orders()
        await s.unsubscribe_depth(["NSE|22"])
        await s.unsubscribe_positions()
        await s.unsubscribe_orders()
        await s.subscribe_touchline(["NSE|1"])
        await s.unsubscribe_touchline(["NSE|1"])
        await wait_until(lambda: any(m.get("t") == "u" for m in srv.received))
        sent = [m for m in srv.received if m.get("t") != "h"]
        assert sent[1:] == [
            {"t": "d", "k": "NSE|22"},
            {"t": "p", "uid": "TEST123"},
            {"t": "o", "actid": "TEST123"},
            {"t": "ud", "k": "NSE|22"},
            {"t": "up"},
            {"t": "uo"},
            {"t": "t", "k": "NSE|1"},
            {"t": "u", "k": "NSE|1"},
        ]
        await srv.drop_all()
        await wait_until(lambda: srv.connections == 2 and s.state is StreamState.CONNECTED)
        await s.unsubscribe_orders()  # in-order fence after any restore frames
        await wait_until(lambda: {"t": "uo"} in srv.received_on(2))
        restored = [m for m in srv.received_on(2) if m.get("t") != "h"]
        assert restored == [srv.received[0], {"t": "uo"}]


async def test_silent_socket_is_treated_as_dead_and_reconnected():
    states: list[StreamState] = []
    async with MockWsServer() as srv:
        srv.ignore = {"h"}
        stream = AsyncTradeSmartStream(
            "TEST123", "tok", config=cfg(srv), on_state_change=states.append
        )
        async with stream:
            await wait_until(lambda: srv.connections >= 2)
            await wait_until(lambda: stream.state is StreamState.CONNECTED)
    assert StreamState.RECONNECTING in states


async def test_reauth_failure_is_terminal_without_further_attempts():
    async with MockWsServer() as srv:
        s = AsyncTradeSmartStream("TEST123", "tok", config=steady_cfg(srv))
        await s.connect()
        srv.auth_ok = False
        await srv.drop_all()
        with pytest.raises(AuthenticationError):
            await asyncio.wait_for(s.__anext__(), 2)
        assert s.state is StreamState.CLOSED
        await asyncio.sleep(0.1)
        assert srv.connections == 2
        with pytest.raises(AuthenticationError):  # the terminal error is sticky
            await asyncio.wait_for(s.__anext__(), 1)
        await s.close()


async def test_auth_timeout_raises_request_timeout():
    async with MockWsServer() as srv:
        srv.ignore = {"a"}
        stream = AsyncTradeSmartStream(
            "TEST123", "tok", config=replace(cfg(srv), ws_auth_timeout=0.2)
        )
        with pytest.raises(RequestTimeoutError):
            await stream.connect()
        assert stream.state is StreamState.CLOSED


async def test_connect_failure_raises_network_error():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = ClientConfig(ws_url=f"ws://127.0.0.1:{port}/", ws_auth_timeout=0.3)
    stream = AsyncTradeSmartStream("TEST123", "tok", config=config)
    with pytest.raises(NetworkError):
        await stream.connect()
    assert stream.state is StreamState.CLOSED


async def test_close_is_idempotent_and_ends_iteration():
    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv))
        await stream.connect()
        stream.__aiter__()
        await stream.subscribe_touchline(["NSE|22"])
        await wait_until(lambda: any(m.get("t") == "t" for m in srv.received))
        await asyncio.sleep(0.02)  # let the ack arrive before closing
        await stream.close()
        await stream.close()
        assert stream.state is StreamState.CLOSED
        received = [msg async for msg in stream]  # queued messages drain, then stop
        assert [type(m) for m in received] == [TouchlineAck]
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        with pytest.raises(TradeSmartError, match="stream is closed"):
            await stream.subscribe_orders()  # single-use: nothing sent after close
        assert {"t": "o", "actid": "TEST123"} not in srv.received


async def test_close_from_async_callback():
    async with MockWsServer() as srv:
        holder: list[AsyncTradeSmartStream] = []

        async def on_message(msg):
            await holder[0].close()

        stream = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv), on_message=on_message)
        holder.append(stream)
        await stream.connect()
        stream.__aiter__()
        await stream.subscribe_touchline(["NSE|22"])
        await wait_until(lambda: stream.state is StreamState.CLOSED)
        assert [type(m) for m in [m async for m in stream]] == [TouchlineAck]


async def test_connect_twice_is_rejected():
    async with MockWsServer() as srv:
        async with AsyncTradeSmartStream("TEST123", "tok", config=steady_cfg(srv)) as s:
            with pytest.raises(TradeSmartError, match="once per stream"):
                await s.connect()
        assert srv.connections == 1


async def test_reusing_a_closed_stream_raises_a_tradesmart_error():
    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream("TEST123", "tok", config=steady_cfg(srv))
        async with stream:
            pass
        with pytest.raises(TradeSmartError, match="once per stream"):
            await stream.connect()
        assert srv.connections == 1


async def test_access_token_object_and_token_never_logged(caplog):
    caplog.set_level(logging.DEBUG, logger="tradesmart")
    token = AccessToken.from_response({"stat": "Ok", "access_token": "test-access-token"})
    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream("TEST123", token, config=cfg(srv))
        async with stream:
            stream.__aiter__()
            await stream.subscribe_touchline(["NSE|22"])
            await asyncio.wait_for(stream.__anext__(), 1)
            assert "test-access-token" not in repr(stream)
        assert srv.received[0]["accesstoken"] == "test-access-token"
        srv.auth_ok = False
        with pytest.raises(AuthenticationError) as excinfo:
            await AsyncTradeSmartStream("TEST123", token, config=cfg(srv)).connect()
    assert "test-access-token" not in str(excinfo.value)
    assert "test-access-token" not in caplog.text


# --- fix round 1 ---------------------------------------------------------------


async def test_drop_during_replay_counts_as_failed_attempt():
    async with MockWsServer() as srv:
        config = replace(cfg(srv), reconnect_max_attempts=5)
        async with AsyncTradeSmartStream("TEST123", "tok", config=config) as s:
            await s.subscribe_touchline(["NSE|22"])
            await s.subscribe_orders()
            srv.drop_after_auth = {2}  # connection 2 accepts auth, then closes at once
            await srv.drop_all()
            await wait_until(lambda: srv.connections >= 3 and s.state is StreamState.CONNECTED)
            await s.unsubscribe_positions()  # in-order fence after the restore frames
            await wait_until(lambda: {"t": "up"} in srv.received_on(3))
            assert {"t": "t", "k": "NSE|22"} in srv.received_on(3)
            assert s.state is StreamState.CONNECTED


async def test_accept_then_drop_exhausts_attempts():
    async with MockWsServer() as srv:
        srv.drop_after_auth = range(1, 1000)  # every connection: auth Ok, then dropped
        s = AsyncTradeSmartStream(
            "TEST123", "tok", config=replace(steady_cfg(srv), reconnect_max_attempts=3)
        )
        await s.connect()
        with pytest.raises(NetworkError, match="reconnect attempts exhausted"):
            await asyncio.wait_for(s.__anext__(), 2)
        assert s.state is StreamState.CLOSED
        assert srv.connections == 4  # initial + 3 attempts, counter never reset


async def test_attempt_counter_resets_after_stable_connection():
    async with MockWsServer() as srv:
        config = replace(steady_cfg(srv), reconnect_max_attempts=1)
        async with AsyncTradeSmartStream("TEST123", "tok", config=config) as s:
            for expected in (2, 3):  # one attempt allowed per outage, twice in a row
                await asyncio.sleep(0.45)  # stay CONNECTED >= 2 x heartbeat_interval (0.4s)
                await srv.drop_all()
                await wait_until(
                    lambda n=expected: srv.connections == n and s.state is StreamState.CONNECTED
                )


async def test_messages_not_queued_until_iteration_starts():
    seen: list[object] = []
    feeds = [{"t": "tf", "e": "NSE", "tk": "22", "lp": f"{100 + i}.00"} for i in range(20)]
    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv), on_message=seen.append)
        async with stream:
            for feed in feeds:
                await srv.send(feed)
            await wait_until(lambda: len(seen) == 20)
            assert len(stream._iter_buffer) == 0  # callback-only consumer: nothing retained
            assert stream.snapshot("NSE|22")["lp"] == "119.00"
            stream.__aiter__()
            for feed in feeds:
                await srv.send(feed)
            got = [await asyncio.wait_for(stream.__anext__(), 1) for _ in feeds]
            assert all(isinstance(m, TouchlineFeed) for m in got)
            assert [m.last_price for m in got] == [m.last_price for m in seen[:20]]
            assert len(seen) == 40


@pytest.mark.parametrize("blocked_on", ["ack", "send_lock"])
async def test_cancelled_connect_releases_socket(blocked_on):
    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv))
        if blocked_on == "ack":
            srv.ignore = {"a"}
        else:  # ak arrives, then _activate waits for the send lock
            await stream._send_lock.acquire()
        task = asyncio.create_task(stream.connect())
        await wait_until(lambda: any(m.get("t") == "a" for m in srv.received))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if blocked_on == "send_lock":
            stream._send_lock.release()
        await wait_until(lambda: srv.open_connections == 0)
        assert stream.state is StreamState.CLOSED


async def test_close_during_terminal_shutdown_returns_only_when_closed():
    observed: list[StreamState] = []
    pending: list[asyncio.Task] = []
    async with MockWsServer() as srv:
        s = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv))

        async def close_and_observe():
            await s.close()
            observed.append(s.state)

        class CloseWhenStopping(logging.Handler):
            def emit(self, record):
                if record.getMessage().startswith("Stream stopped"):
                    pending.append(asyncio.get_running_loop().create_task(close_and_observe()))

        hook = CloseWhenStopping()
        logger = logging.getLogger("tradesmart.stream")
        logger.addHandler(hook)
        try:
            await s.connect()
            srv.auth_ok = False
            await srv.drop_all()  # re-auth fails -> terminal shutdown starts
            await wait_until(lambda: bool(pending))
            await asyncio.wait_for(pending[0], 2)
        finally:
            logger.removeHandler(hook)
    assert observed == [StreamState.CLOSED]


def test_reconnect_delay_is_bounded_exponential_with_jitter():
    for attempt in range(8):
        ceiling = min(60.0, 1.0 * 2**attempt)
        assert _reconnect_delay(attempt, 1.0, 60.0, lambda: 0.0) == ceiling * 0.5
        assert _reconnect_delay(attempt, 1.0, 60.0, lambda: 1.0) == ceiling
    assert _reconnect_delay(30, 1.0, 60.0, lambda: 1.0) == 60.0
    assert _reconnect_delay(5000, 1.0, 60.0, lambda: 1.0) == 60.0  # unlimited attempts: no overflow


# --- delivery buffer / dispatcher (C4, C5) -----------------------------------------


async def test_slow_callback_does_not_block_reader():
    states: list[StreamState] = []

    async def slow(_msg):
        await asyncio.sleep(0.5)

    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream(
            "TEST123", "tok", config=steady_cfg(srv), on_message=slow, on_state_change=states.append
        )
        async with stream:
            for i in range(20):
                await srv.send({"t": "tf", "e": "NSE", "tk": "22", "lp": f"{100 + i}.00"})
            await srv.send({"t": "hk", "ft": "1700000000"})
            await wait_until(
                lambda: (stream.snapshot("NSE|22") or {}).get("lp") == "119.00", timeout=0.3
            )
            await asyncio.sleep(0.6)  # three heartbeat intervals while callbacks lag
            assert stream.state is StreamState.CONNECTED and srv.connections == 1
    assert StreamState.RECONNECTING not in states


async def test_backlog_survives_reconnect():
    seen: list[object] = []

    async def slow(msg):
        await asyncio.sleep(0.1)
        seen.append(msg)

    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream("TEST123", "tok", config=steady_cfg(srv), on_message=slow)
        async with stream as s:
            s.__aiter__()
            for n in (1, 2, 3):
                await srv.send({"t": "om", "norenordno": str(n), "status": "OPEN"})
            await srv.drop_all()
            await wait_until(lambda: srv.connections == 2 and s.state is StreamState.CONNECTED)
            got = [await asyncio.wait_for(s.__anext__(), 2) for _ in range(3)]
            assert [m.order_id for m in got] == ["1", "2", "3"]
            await wait_until(lambda: len(seen) == 3)
            assert [m.order_id for m in seen] == ["1", "2", "3"]


async def test_iterator_merges_feeds_when_consumer_lags():
    feeds = [{"t": "tf", "e": "NSE", "tk": "22", "lp": f"{100 + i}.00"} for i in range(10)]
    async with MockWsServer() as srv:
        config = replace(cfg(srv), stream_backlog_limit=2)
        async with AsyncTradeSmartStream("TEST123", "tok", config=config) as s:
            s.__aiter__()
            await srv.send({"t": "om", "norenordno": "1", "status": "OPEN"})
            await srv.send({"t": "om", "norenordno": "2", "status": "OPEN"})
            for feed in feeds:
                await srv.send(feed)
            await wait_until(lambda: (s.snapshot("NSE|22") or {}).get("lp") == "109.00")
            await asyncio.sleep(0.05)  # let the dispatcher hand everything to the iterator
            got = [await asyncio.wait_for(s.__anext__(), 1) for _ in range(3)]
            assert [type(m).__name__ for m in got] == [
                "OrderUpdate",
                "OrderUpdate",
                "TouchlineFeed",
            ]
            assert str(got[2].last_price) == "109.00"


async def test_terminal_error_raised_after_buffered_messages():
    async with MockWsServer() as srv:
        config = replace(cfg(srv), reconnect_max_attempts=0)
        s = AsyncTradeSmartStream("TEST123", "tok", config=config)
        await s.connect()
        s.__aiter__()
        await srv.send({"t": "om", "norenordno": "1", "status": "OPEN"})
        await srv.drop_all()
        await wait_until(lambda: s.state is StreamState.CLOSED)
        first = await asyncio.wait_for(s.__anext__(), 1)
        assert first.order_id == "1"
        with pytest.raises(NetworkError):
            await asyncio.wait_for(s.__anext__(), 1)


# --- fix round 1 (task 8) ----------------------------------------------------------


async def test_close_counts_undelivered_callback_backlog_in_one_warning(caplog):
    caplog.set_level(logging.WARNING, logger="tradesmart.stream")
    started = asyncio.Event()

    async def slow(_msg):
        started.set()
        await asyncio.sleep(5)

    async with MockWsServer() as srv:
        s = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv), on_message=slow)
        await s.connect()
        for n in (1, 2, 3):
            await srv.send({"t": "om", "norenordno": str(n), "status": "OPEN"})
        await srv.send({"t": "pm", "tsym": "ACC-EQ"})
        await srv.send({"t": "tf", "e": "NSE", "tk": "22", "lp": "101.00"})
        await asyncio.wait_for(started.wait(), 1)  # om 1 is in on_message
        await wait_until(lambda: s.snapshot("NSE|22") is not None)  # the rest is buffered
        await asyncio.wait_for(s.close(), 1)
        await s.close()  # idempotent: no second warning
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "undelivered" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "4 undelivered" in warnings[0] and "3 order/position" in warnings[0]


async def test_close_without_backlog_logs_no_undelivered_warning(caplog):
    caplog.set_level(logging.WARNING, logger="tradesmart.stream")
    seen: list[object] = []
    async with MockWsServer() as srv:
        s = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv), on_message=seen.append)
        await s.connect()
        await srv.send({"t": "om", "norenordno": "1", "status": "OPEN"})
        await wait_until(lambda: len(seen) == 1)
        await s.close()
    assert not any("undelivered" in r.getMessage() for r in caplog.records)
