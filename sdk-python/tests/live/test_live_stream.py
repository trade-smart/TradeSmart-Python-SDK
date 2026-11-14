"""Live, read-only: WebSocket stream (connect, authenticate, one touchline, close). Opt-in.

Only a touchline subscription on one MCX future is made. No tick within the wait (market
closed) skips the test; it never fails for that. One test holds an orders-only stream open for
150 s to check the server answers heartbeats (``hk``) so an idle stream is not reconnected.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

import pytest

from tests._environment import environment_stream
from tests.live._helpers import Contract
from tradesmart import (
    ConnectAck,
    StreamState,
    TouchlineAck,
    TouchlineFeed,
    TradeSmart,
    TradeSmartStream,
)
from tradesmart.models.stream import StreamMessage

pytestmark = [pytest.mark.live, pytest.mark.mcx]

TICK_WAIT_SECONDS = 20.0


def test_stream_connect_subscribe_first_tick(
    shared_client: TradeSmart, mcx_future: Contract, observe: Callable[..., object]
) -> None:
    key = f"{mcx_future.exchange}|{mcx_future.token}"
    ack_at: list[float] = []
    tick_at: list[float] = []
    connect_acks: list[str | None] = []
    errors: list[BaseException] = []
    first_tick = threading.Event()

    def on_message(message: StreamMessage) -> None:
        now = time.monotonic()
        if isinstance(message, ConnectAck):
            connect_acks.append(message.status)
        elif isinstance(message, TouchlineAck) and not ack_at:
            ack_at.append(now)
        elif isinstance(message, TouchlineFeed) and not tick_at:
            tick_at.append(now)
            first_tick.set()

    stream = TradeSmartStream.from_client(
        shared_client, on_message=on_message, on_error=errors.append
    )
    began = time.monotonic()
    stream.start()
    try:
        connected = time.monotonic()
        assert stream.state is StreamState.CONNECTED
        stream.subscribe_touchline(key)
        subscribed = time.monotonic()
        got_tick = first_tick.wait(TICK_WAIT_SECONDS)
    finally:
        stream.stop()
    observe(
        instrument=mcx_future.tradingsymbol,
        connect_seconds=round(connected - began, 3),
        connect_ack_status=connect_acks,
        touchline_ack_seconds=round(ack_at[0] - subscribed, 3) if ack_at else None,
        first_tick_seconds=round(tick_at[0] - subscribed, 3) if tick_at else None,
        waited_seconds=TICK_WAIT_SECONDS,
        got_tick=got_tick,
        errors=[type(e).__name__ for e in errors],
        final_state=str(stream.state),
    )
    assert not errors, f"stream errors: {[type(e).__name__ for e in errors]}"
    assert stream.state is not StreamState.CONNECTED  # closed cleanly
    if not got_tick:
        pytest.skip(f"market closed: no tick within {TICK_WAIT_SECONDS:.0f} s")


def test_stream_from_env_connects_and_closes(observe: Callable[..., object]) -> None:
    stream = environment_stream(os.environ)
    began = time.monotonic()
    stream.start()
    try:
        connect_seconds = time.monotonic() - began
        assert stream.state is StreamState.CONNECTED
    finally:
        stream.stop()
    observe(connect_seconds=round(connect_seconds, 3), final_state=str(stream.state))
    assert stream.state is not StreamState.CONNECTED


IDLE_HOLD_SECONDS = 150.0  # > 2 x the 60 s silence limit


def test_stream_orders_only_is_kept_alive_by_heartbeat_acks(
    shared_client: TradeSmart, observe: Callable[..., object]
) -> None:
    """An orders-only stream often carries no data: the server's ``hk`` replies to the SDK's
    heartbeats are its only frames, so without them it would reconnect every 60 s."""
    errors: list[BaseException] = []
    stream = TradeSmartStream.from_client(shared_client, on_error=errors.append)
    stream.start()
    try:
        stream.subscribe_orders()  # read-only subscription; no order is placed
        time.sleep(IDLE_HOLD_SECONDS)
        state_after_hold = stream.state
    finally:
        stream.stop()
    observe(
        held_seconds=IDLE_HOLD_SECONDS,
        heartbeat_acks=stream.heartbeat_acks,
        reconnect_count=stream.reconnect_count,
        state_after_hold=str(state_after_hold),
        errors=[type(e).__name__ for e in errors],
    )
    assert not errors, f"stream errors: {[type(e).__name__ for e in errors]}"
    assert state_after_hold is StreamState.CONNECTED
    assert stream.reconnect_count == 0
    assert stream.heartbeat_acks >= 1
