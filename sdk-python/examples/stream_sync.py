"""Synchronous WebSocket stream with callbacks (read-only subscriptions).

Credentials: ``TradeSmartStream.from_env()`` -- ``TRADESMART_CLIENT_ID`` and
``TRADESMART_ACCESS_TOKEN`` when set, else the login saved by ``python -m tradesmart login``.

Run: ``python examples/stream_sync.py "NSE|22" "BSE|508123"`` (instrument keys are
``EXCH|TOKEN``). Press Ctrl+C to stop.

All callbacks run one at a time, in arrival order, on a dedicated callback thread
(``tradesmart-callbacks``), never on the thread that reads the socket: a slow callback
delays later callbacks but not reading, heartbeats or reconnects. Keep them short anyway.
Subscribing from inside a callback is allowed; it waits until the stream has processed the
request and raises in the callback if it fails.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence

from tradesmart import (
    DepthAck,
    DepthFeed,
    OrderUpdate,
    TouchlineAck,
    TouchlineFeed,
    TradeSmartError,
    TradeSmartStream,
)


def on_touchline(msg: TouchlineAck | TouchlineFeed) -> None:
    # TouchlineFeed carries only the fields that changed; unchanged ones are None.
    print(f"{msg.key} last={msg.last_price} volume={msg.volume}")


def on_depth(msg: DepthAck | DepthFeed) -> None:
    print(f"{msg.key} bid={msg.bp1}x{msg.bq1} ask={msg.sp1}x{msg.sq1}")


def on_order_update(msg: OrderUpdate) -> None:
    print(f"order {msg.order_id} {msg.status} filled={msg.filled_quantity}")


errors: list[BaseException] = []  # main() exits with 1 if any error was reported


def on_error(exc: BaseException) -> None:
    errors.append(exc)
    print(f"stream error: {exc}", file=sys.stderr)


def build_stream() -> TradeSmartStream:
    """The stream for the saved login (or the environment), with this script's callbacks."""
    return TradeSmartStream.from_env(
        on_touchline=on_touchline,
        on_depth=on_depth,
        on_order_update=on_order_update,
        on_connect=lambda: print("connected"),
        on_disconnect=lambda: print("disconnected (reconnecting unless stopped)"),
        on_error=on_error,  # also gets a terminal error, e.g. reconnects exhausted
    )


def main(argv: Sequence[str] | None = None) -> int:
    keys = list(argv if argv is not None else sys.argv[1:]) or ["NSE|22"]
    logging.basicConfig(level=logging.INFO)  # SDK logs are redacted; tokens never appear
    try:
        stream = build_stream()
    except TradeSmartError as exc:  # no login, or it expired at 06:00 IST
        print(f"No usable login, nothing was sent: {exc}", file=sys.stderr)
        return 2
    try:
        stream.start()  # returns once CONNECTED; raises AuthenticationError on a bad token
        stream.subscribe_touchline(keys)
        stream.subscribe_depth(keys[:1])
        stream.subscribe_orders()
    except TradeSmartError as exc:  # login rejected, network failure, timeout, ...
        print(f"stream failed: {exc}", file=sys.stderr)
        stream.stop()
        return 1
    stream.run_forever()  # blocks until stopped; Ctrl+C stops the stream
    print(f"latest snapshot for {keys[0]}: {stream.snapshot(keys[0])}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
