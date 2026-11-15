# Testing your strategy offline

`tradesmart.testing.OfflineBroker` is a local TradeSmart server that runs inside your test. Your code
uses the real `TradeSmart` client and the real streams; only the server is local. You need no
account, no keys, no network, no static IP and no open market. The offline broker never decides a price or
a fill: your test does.

*Illustrative — a complete pytest test that runs as is (set a price, create the client, run your
code, fill the order):*

```python
from decimal import Decimal

from tradesmart import OrderStatus, OrderType, Product, TradeSmart, TransactionType
from tradesmart.testing import OfflineBroker


def my_strategy(client: TradeSmart) -> None:  # your code
    if client.quote("NSE", "22").last_price <= Decimal("100"):
        client.place_order(exchange="NSE", tradingsymbol="ACC-EQ", quantity=10,
                           transaction_type=TransactionType.BUY, product=Product.MIS,
                           order_type=OrderType.LIMIT, price="100")


def test_buys_on_the_dip():
    with OfflineBroker() as broker:
        broker.add_instrument("NSE", token="22", symbol="ACC-EQ")  # 1. set the market
        broker.set_quote("NSE", "22", last_price="99.50")
        client = broker.client()             # 2. a real client, already logged in
        my_strategy(client)                  # 3. run your code
        order = broker.orders[-1]            # 4. check what it sent
        assert (order.side, order.quantity) == ("B", 10)
        broker.fill(order.id, price="100")   # 5. fill it
        assert client.orders()[0].status is OrderStatus.COMPLETE
```

Most tests need nothing more. The sections below are for looking things up. A longer example
with failure drills and the stream:
[`examples/offline_strategy_test.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/offline_strategy_test.py).

## Set-up

`tradesmart.testing` comes with the SDK; there is nothing else to install. `import tradesmart`
does not load it. It works in any test runner or in a plain script. Use `with OfflineBroker() as
broker:`, or call `broker.close()`: that stops its stream server and deletes its files at once.
A client or stream the broker built keeps it alive, so a helper may return only
`broker.client()`; that broker is cleaned up once it and all its clients and streams are gone.

For the optional pytest fixtures `offline_broker`, `offline_client` and `async_offline_client`, add this
line to your **root** `conftest.py` (pytest does not accept it in a nested `conftest.py`). Each
fixture is new for each test and closed after it.

*Illustrative:*

```python
pytest_plugins = ["tradesmart.testing.pytest_plugin"]
```

## Setting the market and the account

| Method | What it sets |
|---|---|
| `add_instrument(exchange, token, symbol, lot_size=1, tick_size="0.05", ...)` | An instrument. Search, `instruments()`, the script master, option chains and Greeks all use it. Options also take `instrument`, `expiry`, `strike`, `option_type`, `underlying`. |
| `set_quote(exchange, token, **fields)` | Quote fields: `last_price`, `open`, `high`, `low`, `close`, `volume`, `oi`, `change_percent`, `bid`, `ask`, `bid_qty`, `ask_qty` (or the API's own field names). |
| `tick(exchange, token, **fields)` | Like `set_quote`, and also pushes the change to subscribed streams. |
| `set_candles(exchange, token, candles)` | Candles for `historical_data()` and `daily_history()`: dicts with `time` (a `datetime`, naive is IST; a `date` for daily candles), `open`, `high`, `low`, `close`, `volume`, `oi`. They come back newest first, as the API sends them, and only those in the requested range (both ends included; daily candles by day). |
| `set_funds(**fields)`, `set_user(**fields)` | `margins()` and `profile()`, with the API's field names (for example `cash="100000"`). |
| `set_holdings(rows)`, `set_positions(rows)` | `holdings()` and the starting `positions()`; a position row needs at least `exch`, `tsym` and `prd` (a product name such as `"MIS"`, or the API's code). |
| `set_reply(endpoint, body)` | A fixed reply for one API endpoint, for anything the offline broker does not model. |

`quote()` of an instrument with no `set_quote` raises `APIError` "no data". Quote fields you do
not set are 0 (`0.00` for prices), so set every field your code reads (`close` too, if it
measures a change). An unknown field name or a value that is not a number raises `ValueError`.

*Illustrative:*

```python
broker.add_instrument("NFO", token="43210", symbol="NIFTY26NOV24000CE", lot_size=65)
broker.set_quote("NFO", "43210", last_price="101.50", bid="101.45", ask="101.55")
broker.set_funds(cash="100000")
```

The script master the offline broker serves goes to a private temporary folder (the client's
`ClientConfig.instruments_dir`), deleted on `broker.close()`. Your real script-master cache in
`~/.cache/tradesmart` is never touched. Adding an instrument refreshes that folder, so every client sees
instruments added later (unless you gave the client an `instruments_dir` of your own). `broker.client()` sets the offline broker's own addresses (`ws_url`, `base_url`
and the like) and refuses them as overrides.

## Driving an order

A new order is `OPEN` (a stop-loss order is `TRIGGER_PENDING`). Your test then moves it:

| Method | Effect |
|---|---|
| `accept(order_id)` | `PENDING` → `OPEN` (`TRIGGER_PENDING` for a stop-loss order) |
| `fill(order_id, price, quantity=None)` | Fills all that is open, or `quantity`; partial fills add up; `COMPLETE` when fully filled |
| `trigger(order_id)` | `TRIGGER_PENDING` → `OPEN` |
| `cancel(order_id)` | Cancelled, as by the exchange |
| `reject(order_id, message)` | Rejected with `message` as the reason |

Set `broker.default_place = "pending"` to keep new orders `PENDING` until `accept()`, or
`"reject"` to reject every new order with `broker.default_reject_message`.

*Illustrative:*

```python
broker.fill(order.id, price="100", quantity=4)  # partial: still OPEN
broker.fill(order.id, price="101")              # the rest: COMPLETE, average 100.60
broker.trigger(stop_order.id)                   # TRIGGER_PENDING -> OPEN
```

Every change updates the order book and the order history, and pushes an order update to
subscribed streams. A fill also adds a trade-book row and updates the position (net quantity and
average price; funds and margin do not change), and pushes a position update. Your code's own
`modify_order()` and `cancel_order()` change the book too. `exit_order()` cancels a cover or
bracket order (product `CO` / `BO`) and refuses any other order. On an order that is no longer
open they are refused with `OrderRejectedError` ("order is not open to modify" / "... to cancel"
/ "... to exit").

To check what your code sent, read `broker.orders` (each has `id`, `exchange`, `symbol`, `side`
`"B"`/`"S"`, `quantity`, `price`, `trigger_price`, `order_type`, `product`, `status`,
`filled_quantity`, `average_price`, `reject_reason`, `payload`, `history`) and
`broker.requests` (every API call: `endpoint` and `payload`, with secrets masked).

*Illustrative:*

```python
sent = [r.payload for r in broker.requests if r.endpoint == "PlaceOrder"]
assert len(sent) == 1 and sent[0]["remarks"] == "dip-1"  # the order's tag
```

### GTT orders

The offline broker keeps GTTs as well: your code's `place_gtt()`, `modify_gtt()` and `cancel_gtt()` change
`pending_gtts()`, and `gtt_alert_types()` answers `["ATP", "LTP"]`. A GTT never fires on its own
(the offline broker watches no prices): `broker.trigger_gtt(alert_id)` fires it, places its order as a new
order would be placed (with `Order.parent_id` = the alert id), moves the GTT to
`triggered_gtts()` and returns the new order id. Drills work on the GTT endpoints too, e.g.
`broker.fail_next("PlaceGTTOrder", drills.timeout_after_send())`. See [GTT orders](gtt.md).

*Illustrative:*

```python
order_id = broker.trigger_gtt(alert_id)
assert [o.parent_id for o in client.orders() if o.order_id == order_id] == [alert_id]
```

## Failure drills

`broker.fail_next(endpoint, drill)` makes the next call of that API endpoint fail. Each drill
fires once; several drills on one endpoint fire in turn. Nothing waits for a real timeout.

*Illustrative:*

```python
from tradesmart.testing import drills

broker.fail_next("PlaceOrder", drills.timeout_after_send())
```

| Drill | What happens | An order call raises | A read raises (`max_retries=0`) |
|---|---|---|---|
| `drills.reject(message)` | The server answers `Not_Ok` with `message` | `OrderRejectedError` | `APIError` |
| `drills.timeout_after_send()` | The call takes effect (an order **is** placed) but the reply is lost | `OrderStatusUnknownError` | `RequestTimeoutError` |
| `drills.connect_error()` | The connection fails; nothing is sent | `NetworkError` | `NetworkError` |
| `drills.rate_limit(retry_after=None)` | HTTP 429 | `RateLimitError` | `RateLimitError` |
| `drills.server_error(status=503)` | An HTTP server error with a gateway page (not JSON): the order may have been placed | `OrderStatusUnknownError` | `ServerError` |
| `drills.server_error(status=503, json=True)` | An HTTP server error with a JSON `Not_Ok` body | `OrderRejectedError` | `ServerError` |
| `drills.bad_body()` | The reply is not JSON | `OrderStatusUnknownError` | `ResponseParseError` |
| `drills.session_expired()` | `Not_Ok` "Session Expired" | `AuthenticationError` | `AuthenticationError` |

**Reads are retried, orders never.** The offline broker's clients keep the SDK's normal retries for reads,
with waits of about 1 ms. So a network, timeout, rate-limit or server-error drill on a read is
usually absorbed by a retry, and your code gets the normal reply. To see the error, use
`broker.client(max_retries=0)`. Order calls are never retried. The SDK's own order rate limit
(10 orders per second by default, 200 per minute) still applies: a burst waits as it would in production,
for at most `order_rate_limit_max_wait` (1 s); a longer wait, as past 200 orders in a minute,
raises `RateLimitError` and the order is not sent. For a replay or soak test, raise that limit,
for example `broker.client(order_rate_limit_max_wait=60)`.

## The stream

`broker.stream(**callbacks)` and `broker.async_stream(**callbacks)` give real
`TradeSmartStream` / `AsyncTradeSmartStream` objects, connected to the offline broker on `127.0.0.1`.
They reconnect after about 1 ms and send a heartbeat every 0.5 s, so no reconnect waits in real
time.
`TradeSmartStream.from_client(broker.client())` works too; close that stream yourself.

*Illustrative:*

```python
with broker.stream(on_touchline=ticks.append) as stream:
    stream.subscribe_touchline("NSE|22")  # wait for its reply in ticks first
    broker.tick("NSE", "22", last_price="99.90")
```

An order subscription has no reply (the API documents none). To know it is active, subscribe to
something with a reply after it, for example `subscribe_positions()`, and wait for
`on_position`. The example file does this.

| Stream drill | What happens |
|---|---|
| `broker.drop_stream()` | Cuts every stream connection; the real client reconnects and subscribes again |
| `broker.refuse_stream_login()` | The next stream login is refused (`AuthenticationError`) |
| `broker.silence_stream()` / `broker.resume_stream()` | Stops / restarts answering heartbeats; the client notices the silence in about 1 s and reconnects |

`broker.close()` stops the sync streams from `broker.stream()`. Close an async stream before the
broker (`async with` inside `with OfflineBroker()`).

## Async code

`broker.async_client()` is a real `AsyncTradeSmart`. The `async_offline_client` fixture is a plain
fixture: run async tests with the async plugin you already use.

*Illustrative:*

```python
async with broker.async_client() as client:
    await my_async_strategy(client)
```

## Login code

`broker.client()` is already logged in for 24 hours (not until 06:00 IST like a real login, so
a test that runs across 06:00 IST keeps working). To test your own login code, `client.login_url()` and `client.generate_session(code)` work against the
offline broker and give a made-up token.

## What the offline broker does not model

- **The market.** It never fills, prices or rejects an order by itself. There is no margin, RMS,
  funds accounting or exchange rule. Your test decides.
- **Your mistakes.** It does not repeat the SDK's own checks; an invalid call fails in the real
  client first, as in production.
- **`PlaceMultiOrder`, `GetOrderMargin` (`order_margin()`) and other endpoints it does not
  model.** They answer `Not_Ok` "OfflineBroker does not model …; set a reply with
  `broker.set_reply(...)`". Set the reply you want to test.
- **Anything between tests.** Each `OfflineBroker` starts empty.

*Illustrative:*

```python
broker.set_reply("GetOrderMargin", {"stat": "Ok", "cash": "100000.00", "marginused": "1598.00"})
```

## Assumed shapes

The offline broker's replies use the API's documented field names and the reply shapes the SDK is tested
against. The details below are not confirmed by TradeSmart yet. The offline broker assumes them, and this
list will change when TradeSmart confirms them.

- Order history report types: `NewAck`, `New`, `Replaced`, `Fill`, `Canceled`, `Rejected`.
- A new order sends two order updates: `NewAck`, then `New`. `trigger()` sends `New`.
- An accepted stop-loss order is `TRIGGER_PENDING`. A partly filled order modified to `SL-LMT`
  is `TRIGGER_PENDING` too.
- Order history is newest first (as seen live). The trade book is in fill order, oldest first.
- `exit_order()` (`ExitSNOOrder`) cancels the whole cover or bracket order. The offline broker models no
  stop-loss or target legs, so your test fills any square-off itself. Its error texts ("Order not
  found to Exit", "order is not a cover or bracket order", "order is not open to exit") are the
  offline broker's own.
- The body of an HTTP 429 (`rate_limit`) and of `server_error(json=True)` is a JSON `Not_Ok`;
  TradeSmart does not document these bodies. A 5xx without JSON is `OrderStatusUnknownError` on
  an order.
- Average prices have 2 decimals.
- Modifying an unknown order: "Order not found to Modify". Modifying to a quantity not above the
  filled quantity: "quantity must be more than the filled quantity N".
- Cancelling or modifying a closed order is refused at once. (Live, NSE first acknowledged such
  a cancel and then refused it in the order book.)
- An instrument with no `set_quote` answers `quote()` with the "no data" error.
- Holdings are a list with one record per row you set (missing amounts are zero); the API's
  shape for a non-empty holdings list is not confirmed.

## Offline broker or sandbox

The offline broker runs inside your test, answers at once and fails on demand, so it suits fast tests and
failure drills that a real server cannot produce on cue. Its replies are the SDK's understanding
of the API, not TradeSmart's systems. A sandbox, a test environment run by TradeSmart, checks
your orders end to end; TradeSmart's API documentation describes no public one. If TradeSmart
gives you access to a test environment, point `ClientConfig`'s four URLs at it to confirm end to
end, and keep the offline broker for fast tests and drills.
