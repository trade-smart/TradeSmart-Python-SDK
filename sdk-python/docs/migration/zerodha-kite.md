# Moving from Zerodha Kite Connect to TradeSmart

This guide maps the Kite Connect Python client (`pykiteconnect` v4: `KiteConnect` and
`KiteTicker`) to `tradesmart`, TradeSmart's official Python SDK. The method and parameter
vocabulary is deliberately familiar (`place_order`, `tradingsymbol`, `transaction_type`,
`order_type`, `product`, `validity`, `order_id`, …), but the SDK is **not** a drop-in
replacement: symbols, return types and error handling differ. Full SDK reference:
[SDK reference](../reference.md).

In the tables, `kite` is a `KiteConnect` instance and `client` a `tradesmart.TradeSmart`
instance. Every method shown on `TradeSmart` exists with the same parameters on
`AsyncTradeSmart` as a coroutine.

## Install and log in

1. Get API access from TradeSmart (App Key, Secret Key, your Client ID), with the Redirect URL
   `http://127.0.0.1:8080/callback` and your static IP registered —
   [Getting started, step 1](../getting-started.md#1-get-api-access).
2. Install the SDK (Python 3.11+):

   ```bash
   pip install tradesmart
   ```

3. Log in once per trading day (tokens are valid until 06:00 IST); the token is saved for your
   scripts:

   ```bash
   python -m tradesmart login
   ```

4. In your code, `client = TradeSmart.from_env()` picks the saved login up — no keys or tokens
   in the code.

## Behaviour you must know before porting

| Topic | Kite Connect | TradeSmart |
|---|---|---|
| Return values | Plain dicts and lists | Typed, frozen pydantic models (`Order`, `Position`, `Quote`, …): use `order.status`, not `order["status"]`. The wire payload is in `.raw`; `.model_dump()` gives a dict. |
| Errors | Exceptions mapped from the API's error type | Exceptions too, all derived from `TradeSmartError` (table below). Catch `TradeSmartError`, not only `APIError`, to include network failures. |
| Order calls | Return the `order_id` string | Return the order id once TradeSmart's order system has **accepted** the request. That is an acknowledgement, not a fill: confirm with `orders()`, `order_history()` or the order stream. |
| Positions | `{"net": [...], "day": [...]}` | A **flat list** of `Position`; each row carries net fields (`net_quantity`, `netavgprc`) and day fields (`daybuyqty`, `daysellqty`, …) together. |
| Instrument identity | `tradingsymbol` (`"INFY"`, `"NSE:INFY"` for quotes) or integer `instrument_token` | `tradingsymbol` with series (`"INFY-EQ"`) or a **string** token (`"22"`), always with the exchange. See [Symbols](#symbols). |

Also new in TradeSmart:

- **Unknown outcome.** A timeout or unreadable answer on `place_order`, `modify_order`,
  `cancel_order` or `exit_order` raises `OrderStatusUnknownError`. The order may exist:
  **check `orders()` first, never resend blindly.** The SDK never retries state-changing calls.
- **Automatic retries for reads only** (network errors, timeouts, HTTP 429, 5xx; 2 retries by
  default, `ClientConfig(max_retries=0)` disables them).
- **Client-side rate limits** per client instance: `place_order` 10/s (configurable up to 30 with an exchange-approved algo ID: `order_rate_limit_per_second` and `algo_id`) and 200/min (`order_rate_limit_per_minute`); `quote` and
  `historical_data` 100/s and 1,000/min each. By default a call waits for a free slot;
  `ClientConfig(rate_limit_mode="raise")` raises `RateLimitError` instead. Two client
  instances do not share a limit, so use one client per account and process.
- **Prices are `Decimal`** in every model; do not mix them with `float` arithmetic. Price
  inputs accept `str`, `int`, `float` or `Decimal`.
- **Enum fields hold wire codes.** `transaction_type`, `order_type`, `product` and `validity`
  are enum members whose values are TradeSmart's codes, so `trade.transaction_type == "BUY"` is
  **False** (the value is `"B"`); compare with `TransactionType.BUY`. `status` values are plain
  words (`order.status == OrderStatus.COMPLETE`).
- **Static IP.** API calls must come from the static IP registered with TradeSmart.

## Login flow

| Step | Kite Connect | TradeSmart |
|---|---|---|
| App credentials | `api_key`, `api_secret` | `app_key`, `secret_key`, plus your TradeSmart **Client ID** and the registered redirect URL |
| Login URL | `kite.login_url()` | `client.login_url()` (needs `app_key` on the client) |
| Value on the redirect | `request_token` | `code` (single use) |
| Exchange for a token | `kite.generate_session(request_token, api_secret)` → dict with `access_token`; also sets it on the client | `client.generate_session(code)` → `AccessToken`; also stored on the client. The secret key is given to the constructor, not here. Never retried; any failure raises `AuthenticationError` — restart the login. |
| Reuse a saved token | `kite.set_access_token(access_token)` | `client.set_access_token(token)` (`str` or `AccessToken`) |
| Daily login for your own account | — (your own script) | `python -m tradesmart login`, then `TradeSmart.from_env()` |

- A TradeSmart token is valid until the **next 06:00 IST** (`AccessToken.expires_at`,
  `is_expired()`); an expired `AccessToken` is refused locally before any request is sent.
- TradeSmart has no token-refresh or logout endpoint.
- The token exchange needs the secret key, so run it server-side.

## Client construction

| Kite Connect | TradeSmart |
|---|---|
| `KiteConnect(api_key, access_token=None, …)` | `TradeSmart(client_id, *, access_token=None, app_key=None, secret_key=None, config=None, http_client=None)` |
| `root=` | `ClientConfig(base_url=...)` |
| `timeout=` (default 7 s) | `ClientConfig(connect_timeout=5.0, read_timeout=10.0)` (defaults) |
| `debug=True` | `logging.getLogger("tradesmart").setLevel(logging.DEBUG)` |
| `proxies=`, `disable_ssl=` | Pass your own `httpx.Client` as `http_client=` |
| `pool=` | `ClientConfig(max_connections=…, max_keepalive_connections=…, keepalive_expiry=…)` |

Also available: `TradeSmart.from_env()` (reads `TRADESMART_CLIENT_ID`,
`TRADESMART_ACCESS_TOKEN`, `TRADESMART_APP_KEY`, `TRADESMART_SECRET_KEY`, else the login saved by
`python -m tradesmart login`);
`with TradeSmart(...) as client:` or `client.close()` to close the connection pool; and
`AsyncTradeSmart` for asyncio with the same methods as coroutines.

## Methods

| Kite Connect | TradeSmart | Notes |
|---|---|---|
| `place_order(variety, exchange, tradingsymbol, transaction_type, quantity, product, order_type, …)` | `place_order(*, exchange, tradingsymbol, quantity, transaction_type, product, order_type, price=None, trigger_price=None, validity="DAY", disclosed_quantity=None, amo=False, tag=None, book_loss_price=None, book_profit_price=None, trail_price=None, market_protection=None, algo_id=None)` | Keyword-only; no `variety` (see constants). Returns the order id. |
| — | `place_sliced_order(*, <the place_order arguments>, max_slice_quantity=None, max_slices=25)` | An order above the exchange's freeze quantity, sent as slices one after another; returns a `SlicedOrderResult`. See the [Orders guide](../orders.md#sliced-orders). |
| `modify_order(variety, order_id, parent_order_id=None, quantity=None, price=None, …)` | `modify_order(order_id, *, exchange, tradingsymbol, quantity, price, order_type, product, trigger_price=None, validity=None, tag=None, book_loss_price=None, book_profit_price=None, trail_price=None, algo_id=None)` | **Send the full order**: `exchange`, `tradingsymbol`, `quantity`, `price` (`None` for market types), `order_type`, `product` are required. No `disclosed_quantity` or `market_protection` on modify. |
| `cancel_order(variety, order_id, parent_order_id=None)` | `cancel_order(order_id)` | Returns `order_id`. To exit a cover or bracket order use `exit_order(order_id, product)` with `product` `"CO"` or `"BO"`. |
| `orders()` | `orders()` | `list[Order]`; `[]` when empty. |
| `order_history(order_id)` | `order_history(order_id)` | `list[OrderHistoryEntry]`. |
| `trades()` | `trades()` | `list[Trade]`. |
| `positions()` | `positions()` | Flat `list[Position]` (no `net` / `day` split). |
| `holdings()` | `holdings(product="CNC")` | `list[Holding]`. |
| `margins(segment=None)` | `margins()` | One `Limits` object (`cash`, `payin`, `marginused`, `span`, `expo`, `collateral`, `unmtom`, …). No segment argument. |
| `order_margins(params)` | `order_margin(*, exchange, tradingsymbol, quantity, price, product, transaction_type, order_type, trigger_price=None, …)` | **One order per call**, keyword arguments, returns `OrderMargin`. |
| `quote(*instruments)` with `"NSE:INFY"` | `quote(exchange, token)` | **One instrument per call** (`quote("NSE", "22")`; a symbol such as `"INFY-EQ"` works too). 5-level depth included. |
| `ltp(*instruments)` | `quote(exchange, token).last_price` | No separate LTP call. For many instruments use the stream or `AsyncTradeSmart` concurrently (rate limits apply). |
| `historical_data(instrument_token, from_date, to_date, interval, continuous=False, oi=False)` | `historical_data(exchange, token, from_date=None, to_date=None, interval=None)` | Intraday only; `interval` in minutes (`1, 3, 5, 10, 15, 30, 60, 120, 240`). Candles come **newest first**. Dates are a timezone-aware `datetime`, a `date` (00:00 IST) or epoch seconds (`int`); date strings are not accepted. |
| `historical_data(…)` with a daily interval | `daily_history(exchange, tradingsymbol, from_date, to_date)` | Daily candles, by trading symbol (`"INFY-EQ"`). |
| `instruments(exchange=None)` | `instruments(exchange)` | Exchange required; returns `list[dict[str, str]]` with TradeSmart's own columns. `download_instruments()` saves the file. |

Further TradeSmart methods: `profile()`, `search_instruments(text)`,
`security_info(exchange, token)`, `option_chain(exchange, tradingsymbol, strike_price, count)`,
the GTT calls `place_gtt`, `modify_gtt`, `cancel_gtt`, `pending_gtts()`, `triggered_gtts()` and
`gtt_alert_types()` (see [GTT orders](../gtt.md)), the option analytics `option_greeks()`, `option_chain_snapshot()` and `buildup()` (estimates
computed by the SDK; see [Option Greeks and analytics](../option-analytics.md)) and the low-level `request(endpoint, payload, *, state_changing)` for endpoints without a
typed method.

## Parameters

| Kite Connect | TradeSmart | Notes |
|---|---|---|
| `variety` | — | `VARIETY_AMO` → `amo=True`; `VARIETY_CO` → `product="CO"` with `book_loss_price`; iceberg and auction have no equivalent. |
| `exchange` | `exchange` | Same codes. |
| `tradingsymbol` | `tradingsymbol` | TradeSmart's format, e.g. `INFY-EQ`. Pass symbols such as `M&M-EQ` unescaped. |
| `transaction_type`, `quantity`, `product`, `order_type` | same names | `quantity` must be an `int >= 1`. |
| `price` | `price` | Required (`> 0`) for `LIMIT` and `SL`; leave `None` for `MARKET` / `SL-M` (TradeSmart refuses `MARKET` API orders; see [Market orders](#constants)). |
| `trigger_price` | `trigger_price` | Allowed **only** on `SL` / `SL-M`; passing it (even `0`) on other types raises `RequestValidationError`. |
| `validity` | `validity` | `DAY` (default), `IOC`, `EOS`. |
| `validity_ttl` | — | No TTL validity. |
| `disclosed_quantity` | `disclosed_quantity` | Must not exceed `quantity`. |
| `iceberg_legs`, `iceberg_quantity` | — | The SDK does not offer iceberg orders (`request()` can reach them, never retried); `place_sliced_order` sends a large order in slices. |
| `auction_number` | — | Not available. |
| `tag` | `tag` | Sent as `remarks`; read back as `order.tag`. |
| `market_protection` | `market_protection` | Place only. |
| `parent_order_id` | — | Use `exit_order(order_id, product)` for CO / BO. |
| — | `book_loss_price`, `book_profit_price`, `trail_price` | Cover (`CO`) and bracket (`BO`) order legs. |

Invalid combinations are rejected locally with `RequestValidationError` before anything is
sent (see [validation](../orders.md#validation-before-sending) in the Orders guide).

## Constants

`tradesmart` has enums instead of class attributes. Transaction type, order type, product and
validity parameters also accept the common word in any case, and exchanges take the upper-case
code, so the string values of the Kite constants below work unchanged — except variety and TTL.

| Kite Connect | Value | TradeSmart | Wire code sent |
|---|---|---|---|
| `EXCHANGE_NSE`, `_BSE`, `_NFO`, `_CDS`, `_BFO`, `_MCX`, `_BCD` | `"NSE"` … | `Exchange.NSE` … (same seven) | same |
| `TRANSACTION_TYPE_BUY` / `_SELL` | `"BUY"` / `"SELL"` | `TransactionType.BUY` / `.SELL` | `B` / `S` |
| `ORDER_TYPE_MARKET` | `"MARKET"` | `OrderType.MARKET` | `MKT` |
| `ORDER_TYPE_LIMIT` | `"LIMIT"` | `OrderType.LIMIT` | `LMT` |
| `ORDER_TYPE_SL` | `"SL"` | `OrderType.SL` | `SL-LMT` |
| `ORDER_TYPE_SLM` | `"SL-M"` | `OrderType.SL_M` | `SL-MKT` |
| `PRODUCT_MIS` | `"MIS"` | `Product.MIS` | `I` |
| `PRODUCT_CNC` | `"CNC"` | `Product.CNC` | `C` |
| `PRODUCT_NRML` | `"NRML"` | `Product.NRML` | `M` |
| `PRODUCT_CO` | `"CO"` | `Product.CO` | `H` |
| — | — | `Product.BO` (bracket), `Product.MTF` | `B`, `F` |
| `VARIETY_REGULAR` | `"regular"` | default (nothing to pass) | — |
| `VARIETY_AMO` | `"amo"` | `amo=True` | `amo: "Yes"` |
| `VARIETY_CO` | `"co"` | `product="CO"` | — |
| `VARIETY_ICEBERG`, `VARIETY_AUCTION` | `"iceberg"`, `"auction"` | — | — |
| `VALIDITY_DAY` / `_IOC` | `"DAY"` / `"IOC"` | `Validity.DAY` / `.IOC` (also `.EOS`) | same |
| `VALIDITY_TTL` | `"TTL"` | — | — |
| `GTT_*` | | no direct equivalent; see [GTT orders](../gtt.md) (`GttAlertType`, `GttValidity`; single GTTs only) | — |
| `KiteTicker.MODE_LTP`, `MODE_QUOTE` | `"ltp"`, `"quote"` | `subscribe_touchline(...)` | — |
| `KiteTicker.MODE_FULL` | `"full"` | `subscribe_depth(...)` | — |

**Market orders:** TradeSmart refuses `MARKET` orders placed through the API (`OrderRejectedError`,
"ALGO_CHK: MKT Order type not allowed for API order"). Replace them with a `LIMIT` order at a price
that trades now (rounded with `round_to_tick()`); for stop-loss market (`SL-M`, not confirmed yet)
use `SL` with a limit price. See the Orders guide.

Order statuses on TradeSmart are `OrderStatus.PENDING`, `OPEN`, `TRIGGER_PENDING`, `COMPLETE`,
`CANCELED`, `REJECTED`; an unknown value is kept as a plain string.

## Exceptions

| Kite Connect | Closest TradeSmart exception |
|---|---|
| `TokenException` | `AuthenticationError` (HTTP 401/403, session expired, no or expired token) |
| `InputException` | `RequestValidationError` (rejected locally, nothing sent; `exc.errors`) or `APIError` from the server |
| `OrderException` | `OrderRejectedError` (`exc.emsg` holds TradeSmart's message) |
| `NetworkException` | `NetworkError` / `RequestTimeoutError`; on order calls an ambiguous failure is `OrderStatusUnknownError` |
| `DataException` | `ResponseParseError` |
| `PermissionException` | `AuthenticationError` (HTTP 403) |
| `GeneralException` | `APIError`; base class of everything: `TradeSmartError` |
| — | `RateLimitError` (HTTP 429 or the client-side limiter), `ServerError` (5xx), `NotFoundError` |

## Response fields

The models reuse the request vocabulary, so most names you already pass as parameters are also
the attribute names you read back. Wire names in brackets.

| Data | TradeSmart attributes |
|---|---|
| Order (`orders()`, `order_history()`) | `order_id` (`norenordno`), `status`, `exchange`, `tradingsymbol`, `transaction_type`, `order_type`, `product`, `validity`, `quantity`, `price`, `trigger_price`, `disclosed_quantity`, `filled_quantity` (`fillshares`), `average_price` (`avgprc`), `reject_reason` (`rejreason`), `exchange_order_id`, `tag`, `token`, `norentm`, `exch_tm` |
| Trade (`trades()`) | `order_id`, `exchange`, `tradingsymbol`, `transaction_type`, `product`, `flid` (fill id), `flqty`, `flprc`, `fltm`, `average_price`, `exchange_order_id` |
| Position (`positions()`) | `exchange`, `tradingsymbol`, `token`, `product`, `net_quantity` (`netqty`), `netavgprc`, `last_price` (`lp`), `realized_pnl` (`rpnl`), `daybuyqty` / `daysellqty`, `daybuyavgprc` / `daysellavgprc`, `cfbuyqty` / `cfsellqty`, `totbuyamt` / `totsellamt`, `mult` |
| Holding (`holdings()`) | `instruments` (list with `exchange`, `tradingsymbol`, `token`, `last_price`), `holdqty`, `dpqty`, `usedqty`, `btstqty`, `upldprc` (average price uploaded with the holding) |
| Quote (`quote()`) | `last_price`, `open`, `high`, `low`, `close`, `volume`, `average_trade_price`, `open_interest`, `uc` / `lc` (circuit limits), `wk52_h` / `wk52_l`, `feed_time`, depth `bp1..5` / `bq1..5` / `bo1..5` and `sp1..5` / `sq1..5` / `so1..5`, `tbq` / `tsq` |
| Candle (`historical_data()`) | Kite's `date`, `open`, `high`, `low`, `close`, `volume`, `oi` → `time` (and `ssboe` epoch), `into`, `inth`, `intl`, `intc`, `intv`, `intoi` |
| Funds (`margins()`) | `cash`, `payin`, `payout`, `marginused`, `span`, `expo`, `premium`, `collateral`, `brokerage`, `realized_pnl`, `unmtom` (unrealised MTM, account level) |

TradeSmart documents no unrealised P&L field per position; only `realized_pnl`.

## Streaming

| KiteTicker | `TradeSmartStream` (threads) | Notes |
|---|---|---|
| `KiteTicker(api_key, access_token, …)` | `TradeSmartStream(client_id, access_token, *, config=None, on_…=…)` | Callbacks are constructor arguments. |
| `reconnect=True`, `reconnect_max_tries=50`, `reconnect_max_delay=60` | `ClientConfig(reconnect_max_attempts=20, reconnect_backoff_cap=60.0)` (defaults) | Always reconnects; `reconnect_max_attempts=None` = unlimited, `0` = none. |
| `connect_timeout=30` | `ClientConfig(ws_auth_timeout=10.0)` | Bounds the socket open and the login acknowledgement. |
| `connect(threaded=True)` | `stream.start()` | Returns once connected; raises the connect error. |
| `connect(threaded=False)` | `stream.run_forever()` | Blocks until the stream is closed; `stream.stop()` closes it. |
| `subscribe(tokens)` + `set_mode(MODE_LTP` or `MODE_QUOTE, tokens)` | `stream.subscribe_touchline(["NSE\|22", ("NFO", "35001")])` | Instruments are `"EXCH\|TOKEN"` strings or `(exchange, token)` tuples, not ints. |
| `set_mode(MODE_FULL, tokens)` | `stream.subscribe_depth([...])` | 5 depth levels. |
| — | `stream.subscribe_orders()` | **Required** before `on_order_update` receives anything. |
| — | `stream.subscribe_positions()` | Position updates (`on_position`). |
| `on_ticks(ws, ticks)` (a list) | `on_touchline(msg)` / `on_depth(msg)`, or `on_message(msg)` for everything | **One message per call**, typed (`TouchlineFeed`, `DepthFeed`, …); feed messages carry only changed fields, `stream.snapshot("NSE\|22")` holds the merged latest values. |
| `on_connect(ws, response)` | `on_connect()` | Fires after every (re)connect. |
| `on_close(ws, code, reason)` | `on_disconnect()` | |
| `on_error(ws, code, reason)` | `on_error(exc)` | Receives the exception, including callback errors and terminal errors. |
| `on_reconnect(ws, attempts_count)` | `on_reconnect()` | Fires once after each **successful** reconnect (right after its `on_connect()`), not per attempt; `stream.reconnect_count` counts them. Updates sent during the outage are not replayed: call `orders()` / `positions()` there. Per-attempt progress: `AsyncTradeSmartStream(on_state_change=...)` sees `StreamState.RECONNECTING`. |
| `on_noreconnect(ws)` | `on_error(exc)` with `NetworkError("reconnect attempts exhausted")` | Terminal. |
| `on_order_update(ws, data)` | `on_order_update(msg)` | `OrderUpdate` with `order_id`, `status`, `filled_quantity`, `average_price`, `reject_reason`, … |

Kite's ticks are decoded from a binary feed; TradeSmart's feed is JSON and arrives as typed
messages (`TouchlineFeed.last_price`, `.volume`, `.open` … `.close`, `.bp1` / `.sp1`). All
callbacks run one at a time on one dedicated thread, never on the socket reader; keep them
short. For asyncio use `AsyncTradeSmartStream` with `async for msg in stream`.

## No equivalent in TradeSmart

- **Two-leg (OCO) GTTs**: TradeSmart supports one-cancels-other GTTs, but their request fields
  are not documented yet, so the SDK has no method for them. Single GTTs are `place_gtt`,
  `modify_gtt`, `cancel_gtt`, `pending_gtts()` and `triggered_gtts()`: see
  [GTT orders](../gtt.md) (a GTT whose condition is already true fires at once).
- **Iceberg orders** (`VARIETY_ICEBERG`, `iceberg_legs`, `iceberg_quantity`): documented by
  TradeSmart but not offered by the SDK yet; `place_sliced_order` sends a large order in slices.
- Auction orders (`VARIETY_AUCTION`, `auction_number`) and TTL validity (`validity_ttl`).
- Multi-instrument `quote()` / `ltp()` and multi-order `order_margins()`: call per instrument
  or order (the multi-quote endpoint is reachable only via `client.request()`; its schema is
  undocumented).
- `margins(segment)`: `margins()` returns one combined `Limits` object.
- `historical_data(continuous=…, oi=…)`: no continuous contracts; open interest is included
  in the candle when the server sends it.

## Symbols

Kite identifies an NSE equity as `"INFY"` (or `"NSE:INFY"` for quotes) and streams by integer
`instrument_token`. TradeSmart uses the exchange plus its own trading symbol (`"INFY-EQ"`) or
a string token (`"22"`); derivative symbols follow TradeSmart's script master too. Do not
reuse Kite instrument tokens: they are unrelated numbers.

*Illustrative:*

```python
from tradesmart import TradeSmart

client = TradeSmart.from_env()

# Quick lookup by text.
for m in client.search_instruments("INFY"):
    print(m.exchange, m.tradingsymbol, m.token, m.lot_size)

# Whole script master for one exchange (columns are TradeSmart's own; inspect them once).
rows = client.instruments("NSE")
print(rows[0].keys())
```

## Port a strategy

A small strategy: buy one share when the last 5-minute close is above the average close, then
check what happened to the order.

**Before** (Kite Connect; `kite` is a logged-in client, `token`, `start`, `end` and `interval`
are whatever your code already uses):

```python
candles = sorted(kite.historical_data(token, start, end, interval), key=lambda c: c["date"])
closes = [c["close"] for c in candles]
last, average = closes[-1], sum(closes) / len(closes)
if last > average:
    order_id = kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=kite.EXCHANGE_NSE,
        tradingsymbol="INFY",
        transaction_type=kite.TRANSACTION_TYPE_BUY,
        quantity=1,
        product=kite.PRODUCT_CNC,
        order_type=kite.ORDER_TYPE_LIMIT,
        price=last,
    )
    print("order", order_id)
```

**After** (TradeSmart, *illustrative* — running it places a real order):

```python
from datetime import datetime, timedelta, timezone

from tradesmart import OrderRejectedError, OrderStatusUnknownError, TradeSmart

client = TradeSmart.from_env()
infy = next(
    m for m in client.search_instruments("INFY")
    if m.exchange == "NSE" and m.tradingsymbol == "INFY-EQ"
)

now = datetime.now(timezone.utc)  # timezone-aware datetimes only
candles = client.historical_data("NSE", infy.token, from_date=now - timedelta(hours=3),
                                 to_date=now, interval=5)
# Newest first -> oldest first; skip candles without a close. Values are Decimal.
closes = [c.intc for c in reversed(candles) if c.intc is not None]
last = closes[-1] if closes else None  # no candles (e.g. market closed): do nothing
if last is not None and last > sum(closes) / len(closes):
    try:
        order_id = client.place_order(
            exchange="NSE",
            tradingsymbol="INFY-EQ",
            transaction_type="BUY",
            quantity=1,
            product="CNC",
            order_type="LIMIT",
            price=last,
        )
    except OrderRejectedError as exc:
        print("rejected:", exc.emsg)
    except OrderStatusUnknownError:
        print("outcome unknown: check client.orders() before doing anything else")
    else:
        # An order id means "accepted", not "filled".
        status = next((o.status for o in client.orders() if o.order_id == order_id), None)
        print("order", order_id, status)
```

What changed: the symbol and token come from TradeSmart's own lookup; `historical_data` takes
the exchange and a string token and returns candles newest first with `intc` as the close;
there is no `variety`; constants become plain words; and the order is wrapped in `try` because
failures are exceptions and an unknown outcome must be reconciled, not resent.

---

Zerodha and Kite Connect are trademarks of their respective owners and are used here only to
identify those products.
