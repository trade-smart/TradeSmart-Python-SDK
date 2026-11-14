# Streaming (WebSocket)

Live prices, market depth, order updates and position updates over TradeSmart's WebSocket, with
the `tradesmart` Python SDK. Two stream classes share one implementation:

- **`TradeSmartStream`** — for ordinary (blocking) programs. It runs in background threads and
  calls your functions (callbacks).
- **`AsyncTradeSmartStream`** — for `asyncio` programs, read with `async for`.

Both reconnect by themselves, re-send your subscriptions after a reconnect, and never let a slow
callback stall reading the socket. Code blocks on this page are *illustrative*; subscribing only
reads data, nothing on your account changes.

## Credentials for the stream

A stream logs in with your **Client ID** and **access token**. Three ways to give it them:

- **`from_env()`** (on either class) — the same lookup as `TradeSmart.from_env()`:
  `TRADESMART_CLIENT_ID` + `TRADESMART_ACCESS_TOKEN` when set, else the login saved by
  `python -m tradesmart login` (see [Getting started](getting-started.md#3-log-in-once-a-day)).
  `TRADESMART_APP_KEY` is ignored: a stream cannot log in by itself. Without a usable login it
  raises `ConfigurationError` (`AuthenticationError` if the saved login has expired) naming
  `python -m tradesmart login`.
- **`from_client(client)`** — the client id, current token and `config` of a `TradeSmart` or
  `AsyncTradeSmart` you already have (pass `config` to override). Nothing is sent; it raises
  `AuthenticationError` if the client has no token or its token has expired.
- **The constructor** — `TradeSmartStream(client_id, access_token, ...)` with a token you
  obtained yourself (`str`, or the `AccessToken` returned by `generate_session()`).

`from_env()` and `from_client()` take the same `config` and callback keywords as the
constructor. An `AccessToken` is checked against its 06:00 IST expiry before connecting (an
expired one raises `AuthenticationError`; off with `ClientConfig.check_token_expiry=False`). A
plain `str` token is not checked locally; the server refuses an expired one with
`AuthenticationError`.

## Instruments

Subscriptions take instrument keys `"EXCH|TOKEN"` (`"NSE|22"`) or `(exchange, token)` tuples,
as a list or a single one. The exchange may be in any case (`"nse|22"` is sent as `"NSE|22"`);
an invalid key, or an argument that is not a key or a list of keys (`22`, `None`), raises
`RequestValidationError`. `snapshot(key)` normalises its key the same way. Find tokens with `client.search_instruments()` or the script master
(see [Market data](market-data.md)). At most **1,000** touchline + depth subscriptions per
connection; more raises `SubscriptionLimitError` before anything is sent.

## `TradeSmartStream` (callbacks)

*Illustrative:*

```python
from tradesmart import TradeSmartStream


def on_touchline(msg):
    print(msg.key, msg.last_price, msg.volume)  # unchanged fields are None in updates


def on_order_update(msg):
    print("order", msg.order_id, msg.status, msg.filled_quantity, msg.reject_reason or "")


with TradeSmartStream.from_env(
    on_touchline=on_touchline,
    on_order_update=on_order_update,
    on_connect=lambda: print("connected"),
    on_disconnect=lambda: print("disconnected"),
    on_error=lambda exc: print("error:", exc),
) as stream:
    stream.subscribe_touchline(["NSE|22", ("BSE", "500112")])
    stream.subscribe_orders()
    stream.run_forever()  # blocks; Ctrl+C stops the stream
```

**Starting and stopping**

- `with stream:` calls `start()` on entry and `stop()` on exit. Without `with`, call them
  yourself.
- `start()` returns once connected. It raises the connect error, or `RequestTimeoutError` after
  `2 × ws_auth_timeout + 5` seconds (25 s by default).
- `run_forever()` blocks until the stream is closed and every queued callback has run.
- `stop()` ends delivery at once: no further message reaches your callbacks, even if some were
  still waiting (a message whose callbacks are already running finishes them). `on_disconnect`
  still fires. Called from a callback, `stop()` only requests the shutdown.
- A stream is single-use: after `stop()` create a new one.

**Callbacks**

- `on_touchline` (prices), `on_depth` (5-level depth), `on_order_update`, `on_position`,
  `on_message` (every message, called first), `on_connect`, `on_disconnect`, `on_error` and
  `on_reconnect`.
- **All callbacks run one at a time, in arrival order, on one dedicated thread**
  (`tradesmart-callbacks`), never on the thread that reads the socket. A slow callback delays
  the next callbacks but not reading, heartbeats or reconnects. Keep callbacks short: update
  state or put work on a queue.
- Callbacks must be plain functions. An `async def` raises `TypeError` when the stream is
  created (use `AsyncTradeSmartStream` for async code). A callback that returns a coroutine
  anyway (e.g. `lambda m: handler(m)`) has it closed and is reported once to `on_error` as a
  `TypeError`.
- An exception in a callback is logged and passed to `on_error`; the stream keeps running.
- `on_connect` fires on every (re)connect and may run just after `start()` returns.
  `on_disconnect` fires each time a connection is lost. Order updates from the dropped
  connection can still arrive after it: do not treat it as "no more fills".

**Subscribing**

`subscribe_*` / `unsubscribe_*` wait up to 10 seconds for the stream to process the request
(also from a callback). On a timeout they raise `RequestTimeoutError`; the subscription may
still take effect and is restored on reconnect.

### After a reconnect: catch up

Order and position updates sent while the stream was down are **not replayed**. `on_reconnect`
(no arguments) runs right after `on_connect` each time the stream is connected again after a
reconnect (never on the first connect). Refresh your state there:

*Illustrative:*

```python
from tradesmart import TradeSmart, TradeSmartStream

client = TradeSmart.from_env()


def catch_up():
    orders, positions = client.orders(), client.positions()
    print(f"reconciled {len(orders)} orders, {len(positions)} positions")


with client, TradeSmartStream.from_client(client, on_reconnect=catch_up) as stream:
    stream.subscribe_orders()
    stream.run_forever()
```

`stream.reconnect_count` counts the successful reconnects; it is already incremented when
`on_reconnect` runs.

## `AsyncTradeSmartStream` (`async for`)

*Illustrative:*

```python
import asyncio

from tradesmart import AsyncTradeSmartStream, OrderUpdate, TouchlineFeed


async def consume(stream):
    async for msg in stream:  # ends when the stream is closed
        if isinstance(msg, TouchlineFeed):
            print(msg.key, msg.last_price)
        elif isinstance(msg, OrderUpdate):
            print("order", msg.order_id, msg.status)


async def main():
    async with AsyncTradeSmartStream.from_env() as stream:
        consumer = asyncio.create_task(consume(stream))
        await asyncio.sleep(0)  # start iterating BEFORE subscribing
        await stream.subscribe_touchline(["NSE|22"])
        await stream.subscribe_orders()
        await asyncio.sleep(60)  # receive for one minute
    await consumer  # leaving the block closed the stream and ended the loop


asyncio.run(main())
```

- **Start iterating before you subscribe**: messages are queued for `async for` only once
  iteration has started (before that they reach only `on_message` and the snapshot cache).
- `async with` calls `connect()` and `close()`. `connect()` raises `NetworkError`,
  `RequestTimeoutError` or `AuthenticationError`; it does not retry the first connect.
- A terminal error (reconnects exhausted, login refused) is raised by the `async for` loop,
  after the messages received before it.

**Callbacks**

- `on_message(msg)` and `on_state_change(state)` are optional, each a plain or `async`
  function. A plain one blocks the event loop while it runs; prefer `async`.
- An exception in `on_message` is logged and delivery continues. This includes a
  `CancelledError` raised inside it (e.g. it awaited a task cancelled elsewhere).
- After `close()`, `on_message` is not called again. A call in progress is cancelled (unless
  `close()` was called from it), and messages it has not received yet are discarded (one
  WARNING). They still reach `async for`.
- An `async` `on_state_change` runs as its own task; its exceptions are logged. `close()` gives
  the ones still running (including the call for `CLOSED`) up to 2 seconds, then cancels them
  with one WARNING. Called from such a callback (or a task it started or returned), `close()`
  returns without waiting; the callbacks still get the same 2 seconds.

### After a reconnect: catch up (async)

`on_state_change` reports `StreamState.CONNECTED` after every (re)connect, and
`stream.reconnect_count` is already incremented when it does. Refresh your state there:

*Illustrative:*

```python
import asyncio

from tradesmart import AsyncTradeSmart, AsyncTradeSmartStream, StreamState


async def main():
    async with AsyncTradeSmart.from_env() as client:

        async def on_state_change(state):
            if state is StreamState.CONNECTED and stream.reconnect_count:
                orders, positions = await client.orders(), await client.positions()
                print(f"reconciled {len(orders)} orders, {len(positions)} positions")

        stream = AsyncTradeSmartStream.from_client(client, on_state_change=on_state_change)
        async with stream:
            await stream.subscribe_orders()
            await asyncio.sleep(3600)


asyncio.run(main())
```

A catch-up still running when the stream closes gets up to 2 seconds to finish and is then
cancelled, so keep it short or let it be cancelled safely.

## Messages

| Message | Delivered for | Useful fields |
|---|---|---|
| `TouchlineAck` / `TouchlineFeed` | touchline subscribe / update | `key`, `last_price`, `percent_change`, `volume`, `open`/`high`/`low`/`close`, `bp1`/`sp1` |
| `DepthAck` / `DepthFeed` | depth subscribe / update | five levels `bp1..5`, `bq1..5`, `sp1..5`, `sq1..5`, `uc`/`lc` |
| `OrderUpdate` | every order event | `order_id`, `status`, `reporttype`, `price`, `quantity`, `filled_quantity`, `average_price`, `reject_reason`, `tag` |
| `PositionAck` / `PositionUpdate` | position subscribe / update | `net_quantity`, `realized_pnl`, `product`, buy/sell averages |
| `UnsubscribeAck`, `UnknownMessage` | unsubscribes; anything unreadable (`raw` keeps it) | |

Update messages (`TouchlineFeed`, `DepthFeed`) carry only the fields that changed; the others
are `None`. `stream.snapshot("NSE|22")` returns the latest merged values for one instrument
(wire-keyed dict: `"lp"`, `"bp1"`, …) or `None`. TradeSmart's "price not set" value
`42949672.95` becomes `None` on price fields; the original stays in `.raw`.

## Connection behaviour

| Topic | Behaviour |
|---|---|
| Heartbeat | Sent every `heartbeat_interval` (30 s). No frame at all for 60 s → the connection is treated as dead and the stream reconnects. `stream.heartbeat_acks` counts the server's replies (`hk`). |
| Reconnect | Exponential backoff with jitter (1 s base, 60 s cap), at most `reconnect_max_attempts` (20; `None` = unlimited). Exhausted → terminal `NetworkError`. |
| Restore | After every re-login all subscriptions are sent again, once. |
| Login refused | `AuthenticationError`, terminal and never retried (e.g. an expired token). Log in again and create a new stream. |
| Token expiry | With an `AccessToken`, the 06:00 IST expiry is checked before connecting and before every reconnect attempt. An expired token ends the stream with `AuthenticationError` instead of retrying. A connected stream is not ended at 06:00 by the SDK. |
| Close | The closing handshake waits at most 2 s; a server that does not answer is then disconnected. |
| States | `stream.state`: `DISCONNECTED → CONNECTING → AUTHENTICATING → CONNECTED ⇄ RECONNECTING → CLOSED`. |
| Slow consumer | When 1,000 or more messages wait for your code (`stream_backlog_limit`), price and depth updates for the same instrument are merged, latest value wins. Each field's values still arrive in order. Order and position updates are never merged or dropped. A WARNING marks the start and end of merging. |

The stream never places, modifies or cancels orders; it only reads. Settings are in
[Configuration](configuration.md).
