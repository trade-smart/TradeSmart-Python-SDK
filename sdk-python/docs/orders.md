# Orders

Placing, modifying, cancelling and exiting orders with the `tradesmart` Python SDK; how to read
an order's status; and how to recover safely when the network fails at the wrong moment. New to
the SDK? Start with [Your first order in 10 minutes](getting-started.md).

Every code block on this page is *illustrative*: it uses real SDK calls with placeholder values,
and **running one that places, modifies or cancels an order changes your real account**.
`client` is a `TradeSmart` (or, with `await`, an `AsyncTradeSmart`).

## The four order calls

| Call | What it does | Returns |
|---|---|---|
| `client.place_order(...)` | New order (`PlaceOrder`) | the order id |
| `client.modify_order(order_id, ...)` | Change price, quantity, type or trigger of an open order (`ModifyOrder`) | the order id |
| `client.cancel_order(order_id)` | Cancel an open order (`CancelOrder`) | the order id you passed |
| `client.exit_order(order_id, product)` | Exit a cover (`CO`) or bracket (`BO`) order (`ExitSNOOrder`) | the order id you passed |

An order above the exchange's freeze quantity can be sent in slices with
`client.place_sliced_order(...)` ([Sliced orders](#sliced-orders)).
A GTT (good till triggered) order waits on TradeSmart's server until a price or other condition
is met and then places the order: see [GTT orders](gtt.md) (`place_gtt`, `modify_gtt`,
`cancel_gtt`, `pending_gtts`, `triggered_gtts`, `gtt_alert_types`).

**An acknowledgement is not an execution.** Each call returns once TradeSmart's order system
has *accepted the request*. Whether the order is open, filled, cancelled or rejected by the
exchange is confirmed with `client.order_history(order_id)`, `client.orders()` or live order
updates on the [stream](streaming.md).

### Place

*Illustrative — places a real order:*

```python
order_id = client.place_order(
    exchange="NSE",
    tradingsymbol="SBIN-EQ",
    quantity=1,
    transaction_type="BUY",   # or "SELL"
    product="CNC",            # CNC delivery, MIS intraday, NRML derivatives carry-forward
    order_type="LIMIT",       # LIMIT or SL (MARKET is refused for API orders, see below)
    price="500.05",           # str, int, float or Decimal
    validity="DAY",           # default; IOC, EOS where the exchange allows them
    tag="mystrat-0001",       # your own short, unique label (see Recovery)
)
```

All parameters are keyword-only. Optional: `trigger_price` (SL / SL-M), `disclosed_quantity`,
`amo`, `tag`, `book_loss_price`, `book_profit_price`, `trail_price` (cover / bracket orders),
`market_protection` and `algo_id` (see [Algo ID](#algo-id)).

Enum parameters accept the enum member (`TransactionType.BUY`), the common word in any case
(`"BUY"`, `"LIMIT"`, `"SL-M"`, `"MIS"`) or TradeSmart's code (`"B"`, `"LMT"`, `"SL-MKT"`, `"I"`).
Exchanges are `Exchange.NSE` or the code in any case. Put prices on the tick grid with
`round_to_tick(price, tick_size)`; the tick size is in `security_info()`, `quote()` and
`search_instruments()` (`tick_size`).

**MCX quantity.** `quantity` is in the exchange's trade units on every exchange, MCX included,
and is sent as given: a whole multiple of the lot size, exactly as on NFO and BFO. Two lots of
CRUDEOIL (lot 100 barrels) are `quantity=200`; one lot of GOLD (lot 1 kg) is `quantity=1`. A
quantity that is not a multiple of the lot size is rejected by the exchange (for example
`Quantity 1 is not a multiple of lot size 100.00` for CRUDEOIL). The lot size is the script
master's `LotSize` (`instruments()`) or `security_info().lot_size`.

The value of an MCX position is price × quantity × the **price factor** `security_info().prcftr`,
because some contracts are quoted for a smaller amount than they trade in: GOLD trades in kg but
is quoted per 10 g (`prcqqty` 10, `prcunt` `GRMS`), so its `prcftr` is 100 and one lot at a price
P is worth P × 1 × 100; CRUDEOIL has `prcftr` 1 (lot 100, value P × 100). The SDK does not use
`prcftr`; it is there for your own value and margin arithmetic.

### Modify

*Illustrative — changes a real order:*

```python
client.modify_order(
    order_id,
    exchange="NSE",
    tradingsymbol="SBIN-EQ",
    quantity=1,
    price="501.00",
    order_type="LIMIT",
    product="CNC",
)
```

`exchange`, `tradingsymbol`, `quantity`, `price`, `order_type` and `product` are required
(TradeSmart's ModifyOrder needs them again). TradeSmart documents `LIMIT` and `SL` as the order
types for a modification; a `MARKET` / `SL-M` modification (with `price=None`) is passed to the
server, which decides. Optional: `trigger_price`, `validity`, `tag`, `book_loss_price`,
`book_profit_price`, `trail_price` and `algo_id` (see [Algo ID](#algo-id)).

### Cancel and exit

*Illustrative — cancels a real order:*

```python
client.cancel_order(order_id)
client.exit_order(bracket_order_id, "BO")  # cover / bracket orders only
```

A successful reply means TradeSmart **accepted the request**, not that the order is cancelled
or placed. Checks can still refuse it a moment later. Seen on the live server: a new order is
acknowledged with an order id and then shows as `REJECTED` with a `rejreason`; cancelling an order
that is no longer open is acknowledged on NSE/NFO and then refused ("order is not open to
cancel"), while MCX refuses it in the reply. Always confirm the final status in `client.orders()`.

> **MCX:** MCX orders through the API are not yet enabled for TradeSmart (its exchange approval
> is pending). Until then every MCX API order is rejected with a message like
> "CTCLID not assigned" (`OrderRejectedError`, or `REJECTED` with that `rejreason`).

### Cover and bracket orders

- Cover order: `product="CO"` and `book_loss_price` (the stop-loss leg).
- Bracket order: `product="BO"` with `book_loss_price` **and** `book_profit_price`; optional
  `trail_price`.
- Leave them with `client.exit_order(order_id, "CO")` or `"BO"`.

### After-market orders (AMO)

Pass `amo=True` to place an after-market order (sent as `amo: "Yes"`; the default sends
`"No"`). TradeSmart's API documentation does not list the AMO time windows; ask your
TradeSmart relationship manager or support which sessions accept them.

### Margin before you order

`client.order_margin(...)` takes the same order fields (`exchange`, `tradingsymbol`, `quantity`,
`price`, `product`, `transaction_type`, `order_type`, optional `trigger_price`, `order_id`, `sno_number`,
`book_loss_price`) and returns an `OrderMargin` (`cash`, `marginused`, `ordermargin`,
`remarks`). It is a read: nothing is placed.

## Validation before sending

These mistakes raise `RequestValidationError` **before anything is sent**:

| Rule | Applies to |
|---|---|
| `quantity >= 1` and a real `int` (`True` / `False` are rejected); prices, trigger and leg prices `>= 0` | place, modify, margin |
| Prices must be numbers or numeric strings (`"500.05"`); anything else is rejected | place, modify, margin, option chain (`strike_price`) |
| `LIMIT` and `SL` need `price > 0` | place, modify |
| `MARKET` / `SL-M` without a price are sent with price `"0"` | place, modify |
| `SL` / `SL-M` need `trigger_price > 0`; `trigger_price` is **only** allowed on those two | place, modify |
| Cover order (`CO`) needs `book_loss_price` | place |
| Bracket order (`BO`) needs `book_loss_price` **and** `book_profit_price` | place |
| `book_profit_price` only on bracket orders; `book_loss_price` / `trail_price` only on `CO` / `BO` | place, modify |
| `MTF` is not allowed on `MCX` | place |
| `disclosed_quantity <= quantity` (and `>= 0`) | place |
| `exchange`, `product`, `transaction_type`, `order_type`, `validity` must be valid values | place, modify, margin |
| `order_id` must be a non-empty `str` (an `int` is rejected) | modify, cancel, exit |
| `order_id` must be a `str` (an `int` is rejected) | `order_history()` |
| `exit_order` product must be `CO` or `BO` | exit |

**`MARKET` orders are refused for API orders.** TradeSmart does not accept `MARKET` orders placed
through the API: `place_order` raises `OrderRejectedError` with a message such as
`ALGO_CHK: MKT Order type not allowed for API order`, and nothing is placed. To buy or sell at the
market, send a `LIMIT` order at a price that trades now (for a buy, at or above the last price or
the best offer; for a sell, at or below the last price or the best bid), rounded to the tick with
`round_to_tick()`. Stop-loss market (`SL-M`) orders are not confirmed either way yet; use `SL`
(stop-loss limit) with a limit price. The SDK itself does not block `MARKET` or `SL-M`, so code
keeps working unchanged if TradeSmart allows them later. Symbols containing `&`, `+` or `%` (e.g. `M&M-EQ`) are encoded for you — pass them
unescaped.

**See exactly what would be sent** without sending it — useful in tests and code reviews:

*Illustrative (nothing is sent):*

```python
from tradesmart.endpoints.orders import place_order_call

call = place_order_call(
    "YOUR_CLIENT_ID",
    exchange="NSE",
    tradingsymbol="SBIN-EQ",
    quantity=1,
    transaction_type="BUY",
    product="CNC",
    order_type="LIMIT",
    price="500",
    tag="mystrat-0001",
)
print(call.spec.name)  # PlaceOrder
print(call.payload)  # {'uid': 'YOUR_CLIENT_ID', 'trantype': 'B', 'prctyp': 'LMT', ...}
```

`modify_order_call`, `cancel_order_call`, `exit_sno_order_call` and `order_margin_call` work
the same way.

## Order status lifecycle

Every order has a `status` (`OrderStatus`):

| Status | Meaning (TradeSmart's definition) | Final? |
|---|---|---|
| `PENDING` | Order accepted; awaiting further exchange events | no |
| `OPEN` | Open / working at the exchange | no |
| `TRIGGER_PENDING` | Waiting for its trigger (stop-loss orders) | no |
| `COMPLETE` | Fully executed | **yes** |
| `CANCELED` | Cancelled | **yes** |
| `REJECTED` | Rejected by risk checks or the exchange (`reject_reason` says why) | **yes** |

Typical paths, as an illustration only: TradeSmart does not specify the transitions, and the
stop-loss path in particular is an assumption (an order may skip a state, and a stop-loss order
may go from `TRIGGER_PENDING` straight to `COMPLETE`, `CANCELED` or `REJECTED`). Code should
rely only on the final states below, not on a particular sequence.

```
LIMIT / MARKET:   PENDING ─► OPEN ─► COMPLETE
                                └──► CANCELED
SL / SL-M:        PENDING ─► TRIGGER_PENDING ─► OPEN ─► COMPLETE
Refused:          PENDING ─► REJECTED
```

Treat only `COMPLETE`, `CANCELED` and `REJECTED` as final. A partly executed order is not
final yet: read `filled_quantity` (and `average_price`) instead of assuming all-or-nothing.
`status` is the `OrderStatus` member when TradeSmart sends a known value and the raw text
otherwise, so `order.status == OrderStatus.OPEN` and `order.status == "OPEN"` both work.

### Reading `order_history()`

`client.order_history(order_id)` returns one `OrderHistoryEntry` per state change of that order
(an empty list when TradeSmart does not know the id, for example in the first moment after
placing). TradeSmart does not document the order of the entries; in its example the newest
entry is listed first. Each entry has the full order fields (`status`, `price`, `quantity`,
`filled_quantity`, `average_price`, `reject_reason`, `exchange_order_id`, `tag`, …) plus
TradeSmart's `rpt` report type (e.g. `NewAck`, `Rejected`).

For the **current** status, read the order's row in the day's order book, `client.orders()`:
it has exactly one row per order, so nothing depends on the history's order. It is also the
quickest way to check many orders at once.

*Illustrative (read-only):*

```python
import time

from tradesmart import OrderStatus

FINAL = (OrderStatus.COMPLETE, OrderStatus.CANCELED, OrderStatus.REJECTED)


def wait_until_final(client, order_id, seconds=30):
    """The order's order-book row once it is final, else the last row seen (None: unknown)."""
    row = None
    for _ in range(seconds):
        row = next((o for o in client.orders() if o.order_id == order_id), row)
        if row is not None and row.status in FINAL:
            break
        time.sleep(1)
    return row
```

For a running strategy prefer **live order updates** (`subscribe_orders()` on the
[stream](streaming.md)) to polling.

## Recovery: tag every order, never resend blindly

The SDK **never retries** a `PlaceOrder`, `PlaceMultiOrder`, `ModifyOrder`, `CancelOrder`,
`ExitSNOOrder`, GTT (`PlaceGTTOrder`, `ModifyGTTOrder`, `CancelGTTOrder`) or iceberg
(`PlaceIceBergOrder`, `ModifyIceBergOrder`, `CancelIceBergOrder`) request. A GTT is found again by
its tag in `pending_gtts()` ([GTT orders](gtt.md#recovery-find-a-gtt-by-its-tag-never-resend-blindly)). When an order request may have reached TradeSmart but no definitive
answer came back — a read timeout, a connection dropped after sending, a gateway error page, an
answer that cannot be read — the call raises **`OrderStatusUnknownError`**. The order may exist.

A failure that provably happened *before* sending (cannot connect, connect timeout, no free
connection) raises `NetworkError` / `RequestTimeoutError` instead: nothing was sent, so trying
again is safe.

**Tag every order.** Pass a short `tag` that is unique per order (a strategy prefix plus a
counter or a few random characters). It is sent as TradeSmart's `remarks` field and comes back
as `Order.tag`, `OrderHistoryEntry.tag` and `OrderUpdate.tag`. Keep it short: TradeSmart does
not document a maximum length.

*Illustrative — places a real order:*

```python
import secrets
import time

from tradesmart import OrderRejectedError, OrderStatusUnknownError


def find_by_tag(client, tag):
    """Today's orders carrying `tag` (normally none or one)."""
    return [order for order in client.orders() if order.tag == tag]


tag = "mystrat-" + secrets.token_hex(3)
try:
    order_id = client.place_order(
        exchange="NSE", tradingsymbol="SBIN-EQ", quantity=1, transaction_type="BUY",
        product="CNC", order_type="LIMIT", price="500.05", tag=tag,
    )
except OrderRejectedError as exc:
    print("Rejected:", exc.emsg)  # definitive: nothing to reconcile
    order_id = None
except OrderStatusUnknownError:
    order_id = None
    for _ in range(5):  # the order book can lag a little
        matches = find_by_tag(client, tag)
        if matches:
            order_id = matches[0].order_id  # it exists: follow it, do NOT place it again
            break
        time.sleep(2)
    if order_id is None:
        # Still not visible. Do not resend automatically: alert a human, or decide only
        # after checking the order book again later.
        print("Outcome unknown; tag", tag, "not found yet")
```

Rules that keep you out of duplicate orders:

1. Never wrap an order call in your own retry loop.
2. After `OrderStatusUnknownError`, look for the tag **before** deciding anything. Found: the
   order exists — follow it. Not found: look again a little later; place a new order only when
   you are sure the first one does not exist (and give it a **new** tag).
3. The same applies to modify and cancel: after an unknown outcome, read `order_history()` to see
   whether the change happened before sending it again.
4. After a stream reconnect, call `orders()` and `positions()`: updates sent while the stream
   was down are not replayed.

## Rate limits

TradeSmart documents `PlaceOrder` (and `PlaceMultiOrder`) at 30 per second and 600 per minute, but
most client accounts are limited to **10 orders per second**; a higher rate needs
an exchange-approved strategy with an algo ID, which TradeSmart checks on its side. The SDK
enforces 10 per second by default, per client instance, so use one client per account and process.
If your account is approved for more, set `ClientConfig(order_rate_limit_per_second=..., algo_id=...)`
(1 to 30): a rate above 10 without `algo_id` raises `ConfigurationError` when the config is built.

Production also enforces a per-minute cap: it refused an order once 200 had been received in the
current minute (message "Order Recieved 202 in a current minute exceeds Limit 200 for user"), so
the SDK defaults `order_rate_limit_per_minute` to **200** per rolling 60 seconds (integer 1 to 600,
never below the per-second rate). With 200 orders already in the minute, `place_order` waits when the
oldest slot frees within `order_rate_limit_max_wait`, otherwise (and always in `"raise"` mode) it
raises `RateLimitError` (`retry_after` is the seconds until a slot frees; the message names the
window, for example `client-side rate limit for place_order (200 per minute)`) and nothing is sent.
Raise the setting only if TradeSmart confirms a higher limit for your account.
According to TradeSmart, modify, cancel and the other order endpoints have no rate limit.

- In the default `"wait"` mode an order waits for a free slot for at most
  `ClientConfig.order_rate_limit_max_wait` (**1 second** by default). If it would have to wait
  longer it raises `RateLimitError` (with `retry_after`) and is **not sent**, so a stale order
  is never placed late.
- `ClientConfig(rate_limit_mode="raise")` raises `RateLimitError` at once instead of waiting.

See [Configuration](configuration.md#rate-limits-and-retries).

### Algo ID

TradeSmart's `PlaceOrder` and `ModifyOrder` take an optional `algo_id` ("Algo ID. Required when
placing more than 10 orders per second"). Set it once for the client, or per call.

*Illustrative — places real orders:*

```python
from tradesmart import ClientConfig, TradeSmart

config = ClientConfig(order_rate_limit_per_second=20, algo_id="<your algo ID>")
client = TradeSmart.from_env(config=config)

client.place_order(**order)                      # sends algo_id from the config
client.place_order(**order, algo_id="<other>")   # this call only: overrides the config
```

- `ClientConfig.algo_id` (default `None`) is sent as `algo_id` on every `place_order`,
  `modify_order` and every slice of `place_sliced_order`; the per-call `algo_id=` of those three
  methods overrides it for that call. With neither set, no `algo_id` is sent.
- It must be a non-blank string: anything else raises `ConfigurationError` (config) or
  `RequestValidationError` (per call, nothing sent).
- It is sent after the other order fields and on no other endpoint (`cancel_order`,
  `exit_order`, `order_margin` and reads never carry it). `request()` sends your payload as
  given and adds nothing.
- An algo ID is not a secret: it can appear in DEBUG payload logs (`log_payloads=True`) like
  any other order field, but not in the order outcome lines.

## Sliced orders

An exchange refuses an order above its **freeze quantity** (`security_info().frzqty`, in units).
`client.place_sliced_order(...)` sends a larger order as **slices** that each fit the limit, one
after another, as ordinary `PlaceOrder` calls (TradeSmart has no slicing endpoint). It takes every
`place_order` argument, plus `max_slice_quantity` (optional, a smaller slice size) and
`max_slices` (default 25), and returns a `SlicedOrderResult`. *Illustrative — places real
orders:*

```python
result = client.place_sliced_order(
    exchange="NFO",
    tradingsymbol="NIFTY26NOVFUT",
    quantity=3965,            # units: 61 lots of 65
    transaction_type="BUY",
    product="NRML",
    order_type="LIMIT",
    price="24000",
    tag="big-1",              # every slice carries this tag
)
print(result.slice_quantities, result.order_ids, result.complete)  # (1755, 1755, 455) ...
```

**Slice size.** The SDK reads the lot size from the script master and the freeze quantity from
`security_info()`, and sends slices of the largest whole number of lots that fits the freeze
quantity (a slice may equal it): as many full slices as fit, then the remainder. `quantity` is
in units on every exchange, MCX included (as for `place_order`, see **MCX quantity** in
[Place](#place)), and must be a whole number of lots. An order within one slice goes out as
**one** order.

| Contract | Lot | Freeze quantity | Largest slice | Example |
|---|---|---|---|---|
| NIFTY option or future (NFO) | 65 | 1801 | 1755 (27 lots) | 3965 → 1755, 1755, 455 |
| SENSEX option (BFO) | 20 | 1000 | 1000 (50 lots) | 2000 → 1000, 1000 |
| SENSEX future (BFO) | 20 | 500 | 500 (25 lots) | 600 → 500, 100 |
| CRUDEOIL future (MCX) | 100 | 2500 | 2500 (25 lots) | 6000 (60 lots) → 2500, 2500, 1000 |
| GOLD future (MCX) | 1 | 1 | 1 (1 lot) | 3 → 1, 1, 1 |

The values above were read on 12 November 2026; the SDK reads the freeze quantity again on
every call, so a change by the exchange needs no code change. `max_slice_quantity` lowers the
slice size (never raises it above the freeze quantity) and is required when the contract has no
freeze quantity (blank, 0 or negative).

The first call per exchange per day downloads that exchange's script master (as
`option_greeks()` does) and parses it once, which adds a moment before the first slice; call
`client.download_instruments("NFO")` (and the other exchanges you trade) at start-up to do the
download before your first order.

**Refused before anything is sent.** No order request is made when any of these applies:

- `ValueError`: a quantity that is not a whole number of lots (MCX included), or not an integer of
  at least 1; more slices than `max_slices` (the message gives the number needed: pass a larger
  `max_slices` if you mean it); a `disclosed_quantity` larger than the **smallest** slice; a
  contract that is not in the script master (or has no lot size there); a lot size in
  `security_info()` (`ls`) that differs from the script master's (stale data: refresh with
  `download_instruments(exchange, force=True)`); a freeze quantity below one lot; no freeze
  quantity and no `max_slice_quantity`; a `max_slice_quantity` that is not a whole number of lots.
- `ConfigurationError`: more slices than `order_rate_limit_per_second` while
  `rate_limit_mode="raise"`, or while `order_rate_limit_max_wait` is below 1.0: the order rate
  limit would stop such an order part-way every time (see **Rate limit** below); more slices than
  `order_rate_limit_per_minute` (default 200).
- `RateLimitError` (raised, nothing was sent): the current minute has no room for all the slices
  because other orders were sent in the last 60 seconds, and `rate_limit_mode="raise"` or the wait
  (`exc.retry_after` seconds) is above `order_rate_limit_max_wait`.
- The error of a failed `security_info()` read (for example `NetworkError`, or
  `ResponseParseError` for a freeze quantity that is not an integer).
- The `RequestValidationError` of `place_order`'s own checks, run once before the first slice.

**Stop at the first failure, then report.** Slices are never retried and never sent all at once:

- A definite failure (a rejection, the client-side rate limit, a validation error) stops the
  sending and is **returned, not raised**: `SlicedOrderResult(complete=False, error=...)` lists
  the slices placed before it in `order_ids`, and `unsent_quantity` is what was never sent.
  Always check `result.complete`. The up-front minute-room refusal (above) is the exception: it
  raises `RateLimitError` (nothing sent, `retry_after`); a rate refusal after sending has begun
  is returned in `result.error`, not raised.
- An ambiguous outcome raises `OrderStatusUnknownError` with the partial result in
  `exc.sliced_result`: the slices placed before it are in `order_ids`; the uncertain slice is not,
  and is counted in `unsent_quantity`. Look for the tag in `orders()` (every slice carries the
  same `tag`) before sending anything again.
- An interruption while slices are being sent (Ctrl-C, `SystemExit`, a task cancellation, or a
  timeout around the call such as `asyncio.wait_for`) is re-raised unchanged, and nothing more
  is sent. The SDK first adds a note to that exception listing the order ids already placed and
  the unsent quantity (the slice in flight may have been sent), attaches the partial result as
  `exc.sliced_result` when the exception accepts it, and logs a WARNING summary on
  `tradesmart.orders`. With `asyncio.wait_for` the `TimeoutError` you catch is raised by asyncio;
  the noted `CancelledError` is its `__cause__`. Look for the tag in `orders()` as above.
- There is no group modify or cancel: modify or cancel each slice by its order id.

The summary line on the `tradesmart.orders` logger is INFO when every slice was placed and
WARNING when the call stopped, ended unknown or was interrupted.

**Rate limit.** Slices are paced by the order rate limit ([Rate limits](#rate-limits): 10 orders
per second by default, 200 per minute). With the default settings (`"wait"` mode,
`order_rate_limit_max_wait` 1 second) a 25-slice order goes out in about 2 seconds. Settings
that cannot wait out the 1-second window, `rate_limit_mode="raise"` or an
`order_rate_limit_max_wait` below 1.0, would stop every order of more than
`order_rate_limit_per_second` slices part-way, so `place_sliced_order` refuses such an order up
front with `ConfigurationError` and sends nothing. The per-minute window is checked up front too:
when other orders were sent in the last minute and there is no room for every slice, the call
raises `RateLimitError` (`exc.retry_after` is how long until they fit) before sending anything,
unless the room frees within `order_rate_limit_max_wait`. Only a race with another thread sending
orders at the same moment can still stop an order part-way: that slice is not sent, and the call
returns with `result.error` set to the `RateLimitError` (never raised), as above.

## Order logging

Each order call writes one INFO line to the `tradesmart.orders` logger — for `PlaceOrder`,
`PlaceMultiOrder`, `ModifyOrder`, `CancelOrder`, `ExitSNOOrder`, the GTT writes (`PlaceGTTOrder`,
`ModifyGTTOrder`, `CancelGTTOrder`; the alert id as `al_id=`, and the WARNING says to check
`pending_gtts()`) and the iceberg writes sent through `request()` only:

```
PlaceOrder accepted order_id=26110700000001 in 42 ms
```

The result is one of four words; the order id is `-` when not known:

| Word | Meaning |
|---|---|
| `accepted` | TradeSmart acknowledged the request (not a fill; check `orders()`) |
| `rejected` | TradeSmart answered with an error (`OrderRejectedError`, `AuthenticationError`, a server 429, ...) |
| `unknown` | The outcome is unknown (`OrderStatusUnknownError`); also one WARNING ("check orders() before acting"; for a GTT write "check pending_gtts() before acting") |
| `not_sent` | The request never reached TradeSmart: no or an expired token, the SDK's own rate limit, a closed client, or the connection could not be opened |

The time includes any wait for the client-side rate limit. Invalid parameters
(`RequestValidationError`) are refused before an order call starts, so they log no line. A low-level `client.request(..., state_changing=True)`
to any other endpoint is never retried either, but writes no outcome line. Prices, payloads and
tokens are never logged. Turn the
lines on with `logging.basicConfig(level=logging.INFO)`; see
[Configuration](configuration.md#logging).

## Multi-leg and basket orders

`PlaceMultiOrder` exists in the API, but TradeSmart has not documented its request and response
format, so the SDK has no typed method for it yet (2-leg / 3-leg orders are not exposed). It can
be reached through the low-level `client.request("PlaceMultiOrder", {...},
state_changing=True)`, which applies the same never-retry rule, rate limit and error handling.

## Async

`AsyncTradeSmart` has the same methods with the same parameters; `await` them:
`order_id = await client.place_order(...)`. If you cancel an awaiting order task (e.g. with
`asyncio.wait_for`) after the request may already have been sent, the SDK logs a WARNING
("PlaceOrder: outcome unknown - check orders()"): treat it like `OrderStatusUnknownError`.
