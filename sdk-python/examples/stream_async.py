"""asyncio WebSocket stream consumed with ``async for`` (read-only subscriptions).

Credentials: ``AsyncTradeSmartStream.from_env()`` -- ``TRADESMART_CLIENT_ID`` and
``TRADESMART_ACCESS_TOKEN`` when set, else the login saved by ``python -m tradesmart login``.

Run: ``python examples/stream_async.py "NSE|22" "BSE|508123"`` (keys are ``EXCH|TOKEN``).
The stream runs for ``TRADESMART_STREAM_SECONDS`` seconds (default 60), then closes.

Important: messages are queued for ``async for`` only once iteration has started, so the
consumer task is started *before* subscribing.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys

from tradesmart import (
    AsyncTradeSmartStream,
    AuthenticationError,
    DepthFeed,
    OrderUpdate,
    TouchlineAck,
    TouchlineFeed,
    TradeSmartError,
    UnknownMessage,
)


def build_stream() -> AsyncTradeSmartStream:
    """The stream for the saved login (or the environment); prints state changes."""
    return AsyncTradeSmartStream.from_env(
        on_state_change=lambda state: print(f"state: {state.value}"),
    )


async def consume(stream: AsyncTradeSmartStream) -> None:
    try:
        async for msg in stream:  # ends when the stream is closed
            if isinstance(msg, (TouchlineAck, TouchlineFeed)):
                print(f"{msg.key} last={msg.last_price}")
            elif isinstance(msg, DepthFeed):
                print(f"{msg.key} bid={msg.bp1} ask={msg.sp1}")
            elif isinstance(msg, OrderUpdate):
                print(f"order {msg.order_id} {msg.status}")
            elif isinstance(msg, UnknownMessage):
                print(f"unrecognised frame: {msg.raw}")
    except TradeSmartError as exc:  # terminal: reconnects exhausted or re-auth rejected
        print(f"stream stopped: {exc}", file=sys.stderr)


async def run(keys: list[str], seconds: float) -> int:
    try:
        stream = build_stream()
    except TradeSmartError as exc:  # no login, or it expired at 06:00 IST
        print(f"No usable login, nothing was sent: {exc}", file=sys.stderr)
        return 2
    try:
        async with stream:  # connect() + authenticate; close() on exit
            consumer = asyncio.create_task(consume(stream))
            await asyncio.sleep(0)  # let the consumer start iterating before subscribing
            await stream.subscribe_touchline(keys)
            await stream.subscribe_orders()
            with contextlib.suppress(asyncio.TimeoutError):  # run for ``seconds``
                await asyncio.wait_for(asyncio.shield(consumer), seconds)
            print(f"latest snapshot for {keys[0]}: {stream.snapshot(keys[0])}")
        await consumer  # close() ended the iteration
    except AuthenticationError as exc:
        print(f"WebSocket login rejected: {exc.message}", file=sys.stderr)
        return 1
    except TradeSmartError as exc:  # could not connect, no login acknowledgement, ...
        print(f"stream failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    keys = sys.argv[1:] or ["NSE|22"]
    seconds = float(os.environ.get("TRADESMART_STREAM_SECONDS", "60"))
    return asyncio.run(run(keys, seconds))


if __name__ == "__main__":
    sys.exit(main())
