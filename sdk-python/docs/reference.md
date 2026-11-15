# Reference

Everything the SDK offers, in one place to look things up: login details, every method, models,
errors, retries, rate limits, logging, configuration and known limitations. New to the SDK? Start with
[Your first order in 10 minutes](getting-started.md); the [user guide](README.md) explains each topic
step by step.

Every code snippet below is *illustrative*: it shows how to call the SDK with placeholder values
such as `"YOUR_CLIENT_ID"` and is not meant to be run as-is.

## The login command in detail

**The login command** asks for your **Client ID** and **App Key** (the "App ID" on the API Dashboard; or reads `--client-id` /
`--app-key`, or `TRADESMART_CLIENT_ID` / `TRADESMART_APP_KEY`) and for the **Secret Key**
without showing it (or reads `TRADESMART_SECRET_KEY`; it is never a command-line option). It
opens the TradeSmart login page in your browser and waits up to 120 seconds for the redirect on
`http://127.0.0.1:8080/callback` — register that Redirect URL for your app, or pass `--port N`
for `http://127.0.0.1:N/callback`. With any other registered URL, use `python -m tradesmart login
--no-browser`. If no redirect arrives (no browser, port busy, a different Redirect URL), paste
the `code` from the address bar — or the whole redirected URL — when asked; `--no-browser` goes
straight to that and prints the login URL instead. On success it
prints your name, client id, the expiry (the next 06:00 IST) and where the token was saved —
never the token itself. A token issued to an account other than the client id you entered is
refused and nothing is saved (exit code 1), so a stray or malicious redirect to the local
callback cannot replace your login. Prefer the environment variable or the prompt over
`--app-key`: command-line arguments are visible to other local users. All messages are plain
ASCII.

**Saved keys.** After a successful login the App Key and Secret Key you typed are saved, per
Client ID, in `credentials.json` beside the token file (owner-only, **not encrypted**), so the
next login only needs `--client-id`. Skip this with `login --no-save-keys`; remove them with
`python -m tradesmart logout --forget-keys [--client-id ID]` (a plain `logout` removes only the
token file). Details:
[Authentication](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/authentication.md#daily-login-the-app-key-and-secret-key-are-saved).

**Token file.** `%APPDATA%\tradesmart\token.json` on Windows;
`$XDG_CONFIG_HOME/tradesmart/token.json` (default `~/.config/tradesmart/token.json`) on Linux and
macOS. Set `TRADESMART_TOKEN_FILE` (or pass `--token-file PATH` to the command) to use another
file. It holds JSON with `client_id`, `access_token`, `issued_at` and `expires_at`, is replaced
atomically on every login and is created with owner-only permissions (`0o600`). On Windows
those mode bits are not enforced; the file is protected by living in your own user profile,
which other (non-administrator) users cannot read. On Linux and macOS a token file owned by
another user, or readable or writable by group or others, is refused (like `ssh` does with keys).
An edited `expires_at` never extends the token past the next 06:00 IST after `issued_at`. Treat
it like a password. After `login --token-file PATH` the command prints ready-to-paste commands
that set `TRADESMART_TOKEN_FILE` for `from_env()` (`export` on Linux and macOS; PowerShell and
cmd on Windows), and the `python -m tradesmart logout --token-file PATH` command, with the path
quoted for your shell.
`python -m tradesmart token-path` prints the path; `python -m tradesmart logout` deletes the file
(and, with `--forget-keys`, the saved keys).
Exit codes: `0` success, `1` login failed, `2` usage or configuration error.

**How `from_env()` decides:** `TRADESMART_CLIENT_ID` and `TRADESMART_ACCESS_TOKEN` win when set
(a `TRADESMART_ACCESS_TOKEN` without `TRADESMART_CLIENT_ID` raises `ConfigurationError`: an
environment token is never paired with the saved login's client id); otherwise the token
file's client id and token are used. A saved token issued for a different
client id than `TRADESMART_CLIENT_ID` is ignored. The saved token keeps its expiry, so after
06:00 IST `from_env()` raises `AuthenticationError` without sending anything (when
`TRADESMART_APP_KEY` is not set; otherwise the client's first call raises it), and any call made
after 06:00 by a running client raises it too — run `python -m tradesmart login` again. With no
usable token (and no `TRADESMART_APP_KEY` for a login in code) `from_env()` raises
`ConfigurationError` naming that command.

## Authentication

TradeSmart uses an OAuth authorisation-code flow with a checksum. The token exchange needs the
**Secret Key, so it must run server-side** — never ship the secret key to a browser or mobile
app. The login command above does all of this for your own account; to build the flow into a
server, see
[Authentication](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/authentication.md).

1. Send the user to the login URL
   (`https://v2api.tradesmartonline.in/OAuthlogin/authorize/oauth?client_id=<APP_KEY>`).
2. TradeSmart redirects to your Redirect URL with a one-time `code`.
3. The SDK computes `checksum = sha256(app_key + secret_key + code)` and calls `GenAcsTok`.
4. Every later REST call sends `Authorization: Bearer <access_token>`.

*Illustrative (`code` comes from the redirect):*

```python
from tradesmart import TradeSmart

client = TradeSmart("YOUR_CLIENT_ID", app_key="...", secret_key="...")
print(client.login_url())  # open this in a browser
token = client.generate_session(code)  # code from the redirect; stored on the client
print(token.expires_at)  # the next 06:00 IST after issue
```

`GenAcsTok` is never retried: the authorisation code is single-use. Any error answer raises
`AuthenticationError`. So does an **unknown outcome** (a timeout after sending, or a non-JSON
answer such as a 504 gateway page) and a success answer the SDK cannot read; the message ends
with *"the authorisation code is single-use; restart the login"* — send the user through the
login URL again.

**Token lifetime — TradeSmart's rule.** TradeSmart access tokens are valid until the **next
06:00 IST** (Asia/Kolkata, UTC+05:30) after they were issued: a token issued at 05:59:59 IST
expires at 06:00 the same day; one issued at 06:00:00 expires at 06:00 the next day. The SDK
applies this rule: `AccessToken.expires_at` holds that instant and
`AccessToken.is_expired(now=None)` checks it. When the client holds an `AccessToken` object (from
the saved login, `generate_session()` or `set_access_token(token)`) and
`ClientConfig.check_token_expiry` is `True` (default), an expired token raises
`AuthenticationError` **locally, before any request is sent**; set `check_token_expiry=False` to
let the server decide instead. A token passed as a plain `str` carries no issue time and is
never checked locally — the server answers `Session Expired` instead. TradeSmart documents no
refresh or logout endpoint: `refresh_token` and `expires_in` are exposed as received but not
used.

**Token secrecy.** `access_token`, `susertoken` and `refresh_token` are `pydantic.SecretStr`;
`repr(token)` and `token.raw` mask them. To persist a token (e.g. in a secrets manager) call
`token.access_token.get_secret_value()` explicitly.

**Reusing a token.** *Illustrative:*

```python
from tradesmart import TradeSmart

# A plain string token: no local expiry check.
client = TradeSmart("YOUR_CLIENT_ID", access_token="...")

# A token obtained elsewhere (e.g. a secrets manager): replace the token of an existing
# client at any time (str or AccessToken).
client.set_access_token("...")
```

Stand-alone helpers (no client needed): `build_authorize_url(app_key, *, authorize_url=...)`,
`compute_checksum(app_key, secret_key, code)`.

`uid` is filled from `client_id` in every request body (except `EODChartData`) automatically;
`actid` (also set to `client_id`) is sent only to the endpoints that take it (`Holdings`,
`Limits`, `PositionBook`, `TradeBook`, `GetOrderMargin`, `PlaceOrder`, `ModifyOrder`,
`PlaceGTTOrder`, `ModifyGTTOrder`, `GetTrgOi`).

## Placing orders

Full guide — every parameter, the validation rules, the order status lifecycle and recovery:
[Orders](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/orders.md).

*Illustrative (running it places a real order):*

```python
from tradesmart import TradeSmart

with TradeSmart.from_env() as client:
    order_id = client.place_order(
        exchange="NSE",
        tradingsymbol="SBIN-EQ",
        quantity=1,
        transaction_type="BUY",
        product="CNC",
        order_type="LIMIT",
        price="500.05",  # str, int, float or Decimal; sent as "500.05"
        tag="mystrat-0001",  # short and unique: your key for recovery
    )
    # The order book has one row per order with its current status.
    row = next((o for o in client.orders() if o.order_id == order_id), None)
    print(row.status if row else "status unknown - check client.orders()")
```

Enum parameters accept the enum member (`TransactionType.BUY`), the common word (`"BUY"`,
`"LIMIT"`, `"SL-M"`, `"CNC"`, `"MIS"`, `"NRML"`, `"CO"`, `"BO"`, `"MTF"`, any case) or
TradeSmart's own code (`"B"`, `"LMT"`, `"SL-MKT"`, `"C"`, …). Exchanges are given as
`Exchange.NSE` or the code in any case (`"NSE"`, `"nse"`); the upper-case code is always sent.
See [Enums](#enums).

**An acknowledgement is not an execution.** `place_order()`, `modify_order()`,
`cancel_order()` and `exit_order()` return the **order id** once TradeSmart's order system
(OMS) has *accepted the request* (`place_order` returns the id from the acknowledgement;
`modify_order` returns the acknowledgement's `norenordno`, else its `result` field, else the id
you passed; `cancel_order` / `exit_order` return the id you passed). Whether the order is
open, filled, cancelled or rejected by the exchange must be confirmed with `order_history()`,
`orders()` or WebSocket order updates (`stream.subscribe_orders()`). Order statuses:
`PENDING`, `OPEN`, `TRIGGER_PENDING` (not final) and `COMPLETE`, `CANCELED`, `REJECTED` (final).

**Never blindly retry an order.** State-changing calls (`PlaceOrder`, `PlaceMultiOrder`,
`ModifyOrder`, `CancelOrder`, `ExitSNOOrder`, `GenAcsTok`, the GTT writes `PlaceGTTOrder`,
`ModifyGTTOrder`, `CancelGTTOrder`, and the iceberg writes `PlaceIceBergOrder`,
`ModifyIceBergOrder`, `CancelIceBergOrder`) are **never retried** by the SDK — also not
through the low-level `request()`, whatever `state_changing` says. The GTT and iceberg writes
succeed only on their documented `stat` word (`Oi created`, `Oi replaced`,
`Oi delete success`, compared ignoring letter case only, with a non-blank alert id and an `emsg` that is missing, `null` or blank; iceberg `true`, exact); any other answer is
`OrderStatusUnknownError` (see [GTT orders](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/gtt.md)). If the request may have
reached the server but no definitive answer arrived (read timeout, connection dropped after
sending, protocol or decoding error, or a response that is not a JSON `stat` answer — e.g. a
502/503/504 HTML gateway page or an empty 200 body), the SDK raises `OrderStatusUnknownError`
instead of guessing. The same applies when the server **accepted** the request
(`stat: "Ok"`) but the acknowledgement cannot be read, or a `PlaceOrder` acknowledgement
carries no order id: the order may exist, so it is never reported as a failure that invites a
re-send. Only a JSON `stat: "Ok"` / `stat: "Not_Ok"` answer is definitive (a JSON `Not_Ok` on a
5xx status is `OrderRejectedError`, with `http_status` set). A failure that provably happened
*before* sending (connect error, connect timeout, connection-pool timeout) raises
`NetworkError` / `RequestTimeoutError` instead; an order waits at most `connect_timeout` for a
free pooled connection, so it never queues for the full `read_timeout`.

**Recovery: tag every order.** Pass a short unique `tag` with each order (it is sent as
`remarks`; keep it short, TradeSmart does not document the maximum length). After an
`OrderStatusUnknownError`, look for that tag in `orders()` **before deciding anything**: if it is
there the order exists, so follow it and do not resend; if it is not, check again shortly (the
order book may lag) before you consider a new order. *Illustrative:*

```python
from tradesmart import OrderRejectedError, OrderStatusUnknownError, TradeSmartError

try:
    order_id = client.place_order(**order, tag=tag)
except OrderRejectedError as exc:  # stat=Not_Ok from the OMS, e.g. "Rejected : ..."
    print("rejected:", exc.emsg)
except OrderStatusUnknownError:  # may have been placed: find it by its tag, don't resend
    found = [o for o in client.orders() if o.tag == tag]
except TradeSmartError as exc:  # anything else, e.g. provably not sent (connect failure)
    print("not placed:", exc)
```

**Sliced orders.** An order above the exchange's freeze quantity (`security_info().frzqty`) is
refused by the exchange. `place_sliced_order()` takes the `place_order` arguments and sends the
quantity as slices of the largest whole number of lots within the freeze quantity, one after
another (paced by the order rate limit, never retried; `max_slice_quantity` sets a smaller slice,
`max_slices` caps the count, 25 by default). It stops at the first failed slice and reports:
a definite failure is returned as `SlicedOrderResult(complete=False, error=...)`; an unknown
outcome raises `OrderStatusUnknownError` with the partial result in `exc.sliced_result`.
Quantities are in units on every exchange, MCX included (2 CRUDEOIL lots of 100 are 200).
*Illustrative (running it places real orders):*

```python
result = client.place_sliced_order(
    exchange="NFO",
    tradingsymbol="NIFTY26NOVFUT",
    quantity=3965,  # NIFTY: lot 65, freeze 1801
    transaction_type="BUY",
    product="NRML",
    order_type="LIMIT",
    price="24000",
    tag="big-1",
)
print(result.slice_quantities)  # (1755, 1755, 455): three orders, every one tagged "big-1"
print(result.order_ids, result.complete, result.error)
```

Details: [Orders guide, sliced orders](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/orders.md#sliced-orders).

**Prices on the tick grid.** `round_to_tick(price, tick_size)` rounds to the nearest tick
(half-way rounds up) and returns a `Decimal` with the tick's decimal places, e.g.
`round_to_tick(1584.45 * 1.001, "0.05") == Decimal("1586.05")`. The tick size is in
`security_info()` / `quote()` (`tick_size`) or the script master.

**Client-side validation** raises `RequestValidationError` before anything is sent (e.g. `LIMIT`
without a price, `trigger_price` on a non-stop order, a cover order without `book_loss_price`,
`MTF` on `MCX`); the full table is in the
[Orders guide](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/orders.md#validation-before-sending).

**Wire details** (`PlaceOrder`): `amo` is always sent as `"Yes"` or `"No"` (`amo=False` by
default; `amo=True` places an after-market order); `ordersource` is always sent as `"API"` (also
on `ModifyOrder`); `dscqty` is sent only when you pass `disclosed_quantity`; `validity` defaults
to `DAY` (wire `ret`); `tag` is sent as `remarks`; `market_protection` as `mkt_protection`;
`book_loss_price` (`blprc`), `book_profit_price` (`bpprc`) and `trail_price` (`trailprc`) are
passed through as given. `ModifyOrder` sends `ret` only when `validity` is given. TradeSmart refuses
`MARKET` orders placed through the API (`OrderRejectedError`, "ALGO_CHK: MKT Order type not
allowed for API order"); send a `LIMIT` order at a price that trades now instead. The SDK does not
block `MKT` itself, so nothing changes in your code if TradeSmart allows it later. Symbols containing `&`, `+` or `%` (e.g. `M&M-EQ`) are percent-encoded in the body
(`%26`, `%2B`, `%25`) so they survive the server's form decoding — pass them unescaped.

**Dry run.** To see exactly what would be sent without sending it, build the call yourself.
*Illustrative:*

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
)
print(call.spec.name)  # PlaceOrder
print(call.payload)  # {'uid': ..., 'trantype': 'B', 'prctyp': 'LMT', 'ret': 'DAY', ...}
```

The examples `place_order.py` and `order_lifecycle.py` (see [Examples](#examples)) do this by
default and only send a real order when asked to explicitly.

### GTT orders

A GTT (good till triggered) order waits on TradeSmart's server until its condition is met, then
the server places the order: `place_gtt`, `modify_gtt`, `cancel_gtt`, `pending_gtts`,
`triggered_gtts` and `gtt_alert_types` (guide:
[GTT orders](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/gtt.md)).
**A GTT whose condition is already true when it is placed fires at once** and places a real
order, so compare the trigger with a fresh last price first. `alert_validity=GTT` (the default)
keeps it until it fires or is cancelled, at most 365 days or the contract's expiry, whichever is
earlier; `DAY` ends it with the trading day. `tag` is required: after `OrderStatusUnknownError`,
find the GTT by its tag in `pending_gtts()` instead of resending. *Illustrative (places a real
GTT):*

```python
from tradesmart import GttAlertType

alert_id = client.place_gtt(
    exchange="NSE",
    tradingsymbol="ITC-EQ",
    alert_type=GttAlertType.LTP_BELOW,
    trigger_value="380",
    quantity=1,
    transaction_type="BUY",
    product="CNC",
    order_type="LIMIT",
    price="380",
    tag="dip-itc-01",
)
```

## Market data

Full guide:
[Market data](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/market-data.md).
*Illustrative (tokens and symbols are placeholders):*

```python
from datetime import date, datetime, timedelta, timezone

from tradesmart import instruments
from tradesmart.models.market import TPS_INTERVALS

matches = client.search_instruments("SBIN")  # [] when nothing matches
quote = client.quote("NSE", "22")  # "22": ACC-EQ's token (str; "TCS-EQ" style symbols too)
print(quote.last_price, quote.bp1, quote.sp1)  # Decimal; 5 depth levels bp1..5/sp1..5
info = client.security_info("NSE", "22")
chain = client.option_chain("NFO", "YOUR_NFO_SYMBOL", strike_price=24000, count=5)

now = datetime.now(timezone.utc)  # datetimes must be timezone-aware
candles = client.historical_data(
    "NSE", "22", from_date=now - timedelta(hours=3), to_date=now, interval=5
)
daily = client.daily_history("NSE", "SBIN-EQ", from_date=date(2026, 1, 1), to_date=now)
print(TPS_INTERVALS)  # ('1', '3', '5', '10', '15', '30', '60', '120', '240')

rows = client.instruments("NFO")  # script master: list[dict[str, str]]
hits = instruments.find(rows, **{"<column>": "<value>"})  # exact string match
path = client.download_instruments("NFO", "./data")  # ./data/NFO_symbols.txt
rows = instruments.load(path)  # parse a downloaded file yourself
```

- **Dates.** `from_date` / `to_date` accept a timezone-aware `datetime`, a `date` or epoch
  seconds (`int`). A naive `datetime` raises `RequestValidationError`. A **`date` means
  00:00 IST on that day**, so `to_date=date.today()` ends at midnight and **excludes today's
  session**; pass `datetime.now(timezone.utc)` to include it.
- `historical_data()` (`TPSeries`) returns intraday candles **newest first**; omitted dates are
  not sent; `interval` is in minutes (`TPS_INTERVALS`, TradeSmart's default 1).
  `daily_history()` (`EODChartData`) needs both dates; the SDK builds the `NSE:SBIN-EQ` symbol
  this endpoint needs and sends no `uid` (the endpoint is documented without it).
- `option_chain()`: `count >= 1` strikes either side of `strike_price`, in the server's order.
- **Option Greeks and analytics.** `option_greeks()`, `option_chain_snapshot()` and `buildup()` are
  described in the
  [Option Greeks and analytics guide](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/option-analytics.md).
  The Greeks are estimates computed by the SDK (default rate 6.5 %, dividend yield 0.0, both
  overridable per call), not exchange values.
- History windows (TradeSmart's documented limits): NSE/BSE 360 days, MCX 70 days,
  NFO/BFO/CDS/BCD 65 days.
- **Script master.** `instruments(exchange)` downloads `<EXCH>_symbols.txt` into
  `ClientConfig.instruments_dir` (default `~/.cache/tradesmart`) and parses it; the file is reused until TradeSmart's next daily refresh
  (`ClientConfig.script_master_refresh_time`, 07:00 IST). After a download the file's
  modification time is set from `Last-Modified`. A cached file the server has not regenerated
  since is re-checked with `If-Modified-Since` at most every `script_master_recheck_minutes`
  (15; `304` keeps it). If a re-check fails (network error, timeout, HTTP 5xx) the cached file is
  returned with one WARNING per failed re-check, and the server is asked again at most once per
  `script_master_recheck_minutes`; a first or `force=True` download still raises. The rows are
  re-parsed on every call. `download_instruments(exchange, dest_dir=None, *, force=False)` only
  downloads and returns the `pathlib.Path`. The column layout is undocumented, so parsing is
  purely header-driven (first non-blank row; delimiter detected among `,` `|` tab `;`).
  Script-master errors: HTTP 401/403 → `AuthenticationError`, 404 → `NotFoundError`, 429 →
  `RateLimitError`, 5xx → `ServerError`, other ≥ 400 → `APIError`; not a zip / no `.txt` member
  → `ResponseParseError`; network failures → `NetworkError` / `RequestTimeoutError`. An unknown
  exchange raises `ValueError` before a URL or path is built. Downloads are not retried or
  rate-limited.

## WebSocket streaming

Full guide:
[Streaming](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/streaming.md).
Two clients share one implementation: `TradeSmartStream` (background threads with callbacks)
and `AsyncTradeSmartStream` (asyncio, `async for`). Both log in with the Client ID and access
token. `TradeSmartStream.from_env()` / `AsyncTradeSmartStream.from_env()` take them from
`TRADESMART_CLIENT_ID` + `TRADESMART_ACCESS_TOKEN` or the saved login (like
`TradeSmart.from_env()`, but `TRADESMART_APP_KEY` is ignored: a stream cannot log in);
`from_client(client)` reuses a `TradeSmart` / `AsyncTradeSmart` client's id and token; the
constructor takes them directly (`str` or `AccessToken`). An expired `AccessToken` raises
`AuthenticationError` before connecting (unless `check_token_expiry=False`); a plain `str`
token is not checked locally. The socket reader never waits for your code, so a slow consumer
cannot stall reading, heartbeats or reconnect detection.

*Illustrative:*

```python
from tradesmart import TradeSmart, TradeSmartStream

client = TradeSmart.from_env()


def catch_up() -> None:  # updates sent while the stream was down are not replayed
    orders, positions = client.orders(), client.positions()
    print(f"reconciled {len(orders)} orders, {len(positions)} positions")


stream = TradeSmartStream.from_client(  # the client's id and token; nothing is sent yet
    client,
    on_touchline=lambda msg: print(msg.key, msg.last_price),
    on_order_update=lambda msg: print(msg.order_id, msg.status),
    on_reconnect=catch_up,
)
with client, stream:  # stream: start() on entry (raises the connect error), stop() on exit
    stream.subscribe_touchline(["NSE|22", ("BSE", "508123")])
    stream.subscribe_orders()
    stream.run_forever()  # blocks; Ctrl+C stops the stream
```

- **`TradeSmartStream`**: every callback (`on_touchline`, `on_depth`, `on_order_update`,
  `on_position`, `on_message`, `on_connect`, `on_disconnect`, `on_error`, `on_reconnect`) runs on
  one dedicated thread, `tradesmart-callbacks`, one at a time in arrival order. `start()` returns
  once connected (timeout `2 × ws_auth_timeout + 5` s); `subscribe_*` / `unsubscribe_*` wait up
  to 10 s, also from a callback. A stream is single-use. `on_reconnect` (no arguments) runs
  right after `on_connect` each time the stream is connected again after a reconnect;
  `reconnect_count` counts successful reconnects.
- **`AsyncTradeSmartStream`**: `async with` connects and closes; **start iterating
  (`async for`) before you subscribe**; optional `on_message` (plain or async) and
  `on_state_change`; a terminal error is raised by the iterator; `reconnect_count` counts
  successful reconnects.
- Without a client, `TradeSmartStream.from_env(on_order_update=...)` uses the login saved by
  `python -m tradesmart login` directly.

| Topic | Behaviour |
|---|---|
| Instruments | `"EXCH\|TOKEN"` strings or `(exchange, token)` tuples, a list or a single one; validated and de-duplicated. |
| Subscription limit | 1,000 per connection (touchline + depth keys); more raises `SubscriptionLimitError` before anything is sent. |
| Heartbeat | `{"t":"h"}` every `heartbeat_interval` (30 s); no frame for `2 × heartbeat_interval` → reconnect. A failing heartbeat task stops the stream with a terminal `NetworkError`. |
| Reconnect | Capped exponential backoff with jitter (`reconnect_backoff_base` 1 s, `reconnect_backoff_cap` 60 s), at most `reconnect_max_attempts` (20; `None` = unlimited); exhaustion → terminal `NetworkError("reconnect attempts exhausted")`. |
| Auth failure | `ak` with `s=Not_Ok` → `AuthenticationError`; terminal, never retried. |
| Restore | After every re-login all subscriptions are re-sent once. The stream never sends orders. |
| State | `stream.state` (`StreamState`): `DISCONNECTED → CONNECTING → AUTHENTICATING → CONNECTED ⇄ RECONNECTING → CLOSED`; `CLOSED` is terminal. |
| Snapshot | `stream.snapshot("NSE\|22")`: the latest merged wire-keyed dict (`"lp"`, `"bp1"`, …) or `None`; key normalised like a subscription. |
| Slow consumer | At `ClientConfig.stream_backlog_limit` (1,000) waiting messages, `tf`/`df` updates for the same instrument are merged; acks, order and position updates are never merged or dropped. |
| Prices | TradeSmart's "price not set" value `42949672.95` becomes `None` on quote, depth and order-update price fields; the original stays in `.raw`. |
| Delivered messages | `TouchlineAck`, `TouchlineFeed`, `DepthAck`, `DepthFeed`, `OrderUpdate`, `PositionAck`, `PositionUpdate`, `UnsubscribeAck`, `UnknownMessage`; `ConnectAck` and `HeartbeatAck` are consumed internally. Parsing never raises. |

## Error handling

Every SDK exception derives from `TradeSmartError`. Catch `TradeSmartError` (not only
`APIError`) to also handle network failures, timeouts and unreadable responses.

```
TradeSmartError
├── ConfigurationError
├── RequestValidationError
├── SubscriptionLimitError
├── TransportError
│   ├── NetworkError
│   └── RequestTimeoutError
├── OrderStatusUnknownError
├── APIError
│   ├── AuthenticationError
│   ├── RateLimitError
│   ├── NotFoundError
│   ├── ServerError
│   └── OrderRejectedError
└── ResponseParseError
```

| Exception | Raised when | Attributes |
|---|---|---|
| `TradeSmartError` | Base class; also raised directly for misuse of a stream (`TradeSmartStream` not started or already stopped, `run_forever` from a callback, subscribing on a closed stream). | — |
| `ConfigurationError` | Invalid `ClientConfig`; empty `client_id`; `from_env` (client or stream) finds no client id or no usable token (the message says to run `python -m tradesmart login`), or `TRADESMART_ACCESS_TOKEN` without `TRADESMART_CLIENT_ID`; any call on a closed client ("client is closed; create a new client", nothing sent); app/secret key missing for `login_url` / `generate_session`; empty arguments to `build_authorize_url` / `compute_checksum`; an access token with a space, control or non-ASCII character (the value is never shown); a `request()` endpoint name that is not ASCII letters; a header that cannot be sent (nothing sent). | — |
| `RequestValidationError` | Request parameters fail client-side validation (nothing is sent). | `errors` (list of pydantic-style error dicts) |
| `SubscriptionLimitError` | A subscribe would exceed 1,000 stream subscriptions. | — |
| `TransportError` | Base of network-level failures. | — |
| `NetworkError` | Connection could not be established or was lost (read endpoints); connect failure on any endpoint; WebSocket open failure; reconnects exhausted. | — |
| `RequestTimeoutError` | Connect timeout; read timeout on a read endpoint; pool timeout; no WebSocket `ak` within `ws_auth_timeout`; sync stream start/subscribe timeouts. | — |
| `OrderStatusUnknownError` | A state-changing request may have been executed but no definitive answer arrived: timeout/disconnect after sending; a body that is not a JSON `stat` answer (non-JSON at any status incl. 5xx gateway pages, empty body, unexpected shape, a list); an accepted (`stat: Ok`) acknowledgement that cannot be read; or a `PlaceOrder` acknowledgement without an order id (a `ModifyOrder` `stat: Ok` acknowledgement is an acceptance and returns the id you passed when it carries none); on a GTT write, any answer but the exact success word with a non-blank alert id and an `emsg` that is missing, `null` or blank (also `Ok`). **Check `orders()` / `order_history()` (GTT: `pending_gtts()`) before acting.** | `endpoint`, `message`, `http_status` and `body` (first 2,000 chars, secrets masked) when a response was received |
| `APIError` | `stat: Not_Ok` (or an HTTP error) not covered by a subclass. | `message`, `endpoint`, `http_status`, `emsg`, `request_time`, `raw` (secrets masked) |
| `AuthenticationError` | HTTP 401/403; `emsg` containing *Session Expired*, *Invalid Access Token*, *Invalid Session*, *Invalid User Id* or *Invalid Acct Id*; any `GenAcsTok` error answer, unknown outcome or unreadable success answer; no token set; token expired locally; WebSocket login rejected. | as `APIError` |
| `RateLimitError` | HTTP 429; the client-side limiter in `"raise"` mode; or, in `"wait"` mode, an order that would wait longer than `order_rate_limit_max_wait` (nothing sent). | as `APIError` + `retry_after` (seconds or `None`) |
| `NotFoundError` | HTTP 404 that is not a "no data" result (and script-master 404). | as `APIError` |
| `ServerError` | HTTP 5xx on read endpoints (including non-JSON gateway pages and a JSON `stat: Not_Ok`). On state-changing endpoints a JSON `Not_Ok` on 5xx is `OrderRejectedError` and a non-JSON 5xx is `OrderStatusUnknownError`. | as `APIError` |
| `OrderRejectedError` | `stat: Not_Ok` from `PlaceOrder` / `ModifyOrder` / `CancelOrder` / `ExitSNOOrder` / `PlaceGTTOrder` / `ModifyGTTOrder` / `CancelGTTOrder` (and `PlaceMultiOrder` through `request()`; an iceberg write answering `false` through `request()`) (e.g. `Rejected : ...`), also on HTTP 5xx (`http_status` set), unless it is an authentication, rate-limit or 404 error. | as `APIError` |
| `ResponseParseError` | On read endpoints: body is not JSON (and status is not 401/403/429/5xx) or has an unexpected shape; on any read endpoint: a value fails model validation. (State-changing endpoints raise `OrderStatusUnknownError` for unexpected bodies; `GenAcsTok` raises `AuthenticationError`.) | `message`, `endpoint`, `http_status`, `body` (first 2,000 chars, secrets masked) |

Outside the hierarchy: `instruments()` / `download_instruments()` raise `ValueError` for an unknown exchange; the
low-level `request()` raises `TypeError` for an unsupported payload value; `option_greeks()` and
the `tradesmart.greeks` functions raise `ValueError` for an unknown option, a missing underlying
future or a value that is not a finite number (see the Greeks guide); `TradeSmartStream` raises
`TypeError` at construction for an `async def` callback.

**How responses are classified:** the body is always
parsed, whatever the HTTP status. `stat: "Ok"` is success. For `stat: "Not_Ok"` the order of
checks is: "no data" on a list endpoint → empty list; HTTP 401/403 or an authentication `emsg`
→ `AuthenticationError`; 429 → `RateLimitError`; ≥ 500 → `ServerError` (`OrderRejectedError` on a
state-changing endpoint); 404 → `NotFoundError`;
state-changing endpoint → `OrderRejectedError`; otherwise `APIError`.

**"No data" handling.** On list endpoints — `orders()`, `order_history()`, `trades()`,
`positions()`, `holdings()`, `search_instruments()`, `option_chain()`, `historical_data()`,
`daily_history()` — both `stat: Not_Ok` with an `emsg` containing `no data` (any case, any HTTP
status, e.g. SearchScrip's 404 "No Data") **and** a bare `[]` return `[]`. Every other `Not_Ok`
raises. Non-list endpoints never swallow "no data".

**Strict list shapes.** `OrderBook`, `SingleOrdHist`, `TradeBook`, `PositionBook`, `Holdings`,
`TPSeries` and `EODChartData` must return a JSON array on success. A `stat: "Ok"` **object** on
one of these endpoints raises `ResponseParseError` (with the raw body) instead of being silently
wrapped in a list. A list where an object is expected raises `ResponseParseError` too
(`OrderStatusUnknownError` on state-changing endpoints). A JSON list on an HTTP error status
(≥ 400) is never returned as data: it raises by status (429 `RateLimitError`, 401/403
`AuthenticationError`, ≥ 500 `ServerError`, 404 `NotFoundError`, otherwise `APIError`).
(`SearchScrip` and `GetOptionChain` succeed with an object holding a `values` list.)

## Retries

Only **read** endpoints are retried, only on `NetworkError`, `RequestTimeoutError`,
`RateLimitError` (HTTP 429) and `ServerError`, at most `max_retries` times (default 2, i.e. up to
three attempts). The delay before retry *n* (0-based) is `min(backoff_cap, backoff_base·2ⁿ) ×
U(0.5, 1)` (defaults 0.5 s and 8 s), or `min(backoff_cap, Retry-After)` when a 429 carries a
numeric `Retry-After` header. `max_retries=0` disables retries. `GetOrderMargin` is a read and is
retried. **State-changing calls are never retried.** A client-side `RateLimitError` (`"raise"`
mode) is not retried either.

## Rate limits

TradeSmart's documented limits are enforced client-side by a thread-safe sliding-window limiter,
one per client instance:

| Group | Endpoints | Per second | Per minute |
|---|---|---|---|
| `place_order` | `PlaceOrder` (+ `PlaceMultiOrder` via `request()`) | `order_rate_limit_per_second` (default 10; up to 30 with `algo_id`) | `order_rate_limit_per_minute` (default 200; up to 600) |
| `quotes` | `GetQuotes` (+ `GetQuotesMultiple` via `request()`) | 100 | 1,000 |
| `tpseries` | `TPSeries` | 100 | 1,000 |
| — | all other REST endpoints | no limit | no limit |

The order rate is set by `ClientConfig.order_rate_limit_per_second` (see
[Configuration](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/configuration.md#rate-limits-and-retries)
and [Orders](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/orders.md#rate-limits)).

`ClientConfig.rate_limit_mode`:
- `"wait"` (default): the call sleeps (or `await`s) until a slot frees. An order
  (`place_order` group) never waits longer than `ClientConfig.order_rate_limit_max_wait`
  (default 1 s): if a longer wait would be needed it raises `RateLimitError` (with
  `retry_after`) and nothing is sent, so a stale order is never placed late. Reads still wait.
- `"raise"`: the call raises `RateLimitError` immediately (`endpoint` = the endpoint name, e.g.
  `"PlaceOrder"`, `"GetQuotes"`, `"TPSeries"`; the group name is in the message;
  `http_status=None`, `retry_after` = seconds until a slot frees).

Two client instances do not share a limiter: use one client per account and process.
WebSocket limit: 1,000 subscriptions per connection (see above).

## Logging & redaction

The SDK logs through the standard `logging` module under the `tradesmart` logger
(`tradesmart.transport`, `tradesmart.orders`, `tradesmart.stream`, `tradesmart.stream.websockets`). It only attaches a
`NullHandler`, never configures the root logger and never prints.

*Illustrative:*

```python
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("tradesmart").setLevel(logging.DEBUG)  # per-request lines
```

- DEBUG: one line per REST call (`POST /GetQuotes -> HTTP 200 stat=Ok in 85 ms`; the extra work
  for this line is done only when DEBUG is enabled); request payloads only when
  `ClientConfig(log_payloads=True)`, and then redacted. Stream state changes.
- INFO (`tradesmart.orders`): one line per order call (`PlaceOrder`, `PlaceMultiOrder`,
  `ModifyOrder`, `CancelOrder`, `ExitSNOOrder`, the GTT writes `PlaceGTTOrder`, `ModifyGTTOrder`,
  `CancelGTTOrder`, and the iceberg writes through `request()`; a GTT line carries the alert id
  as `al_id=` instead of `order_id=`):
  `PlaceOrder accepted order_id=26110700000001 in 42 ms` — the result is `accepted`, `rejected`
  (the server answered with an error), `unknown` (`OrderStatusUnknownError`) or `not_sent` (never
  reached the server: no or an expired token, the client-side rate limit, a closed client, or a
  connect-phase failure); the order id is `-` when not known; the time includes any client-side
  rate-limit wait. Invalid parameters (`RequestValidationError`) log no line. Never payloads,
  prices or tokens. Only these order endpoints get the line: a low-level
  `request(..., state_changing=True)` to any other endpoint is never retried but logs no outcome.
- WARNING (`tradesmart.orders`): every `OrderStatusUnknownError` (once per call: "check
  orders() before acting"; for a GTT write "check pending_gtts() before acting"), and an async order call whose task was cancelled (e.g. by
  `asyncio.wait_for`) after the request may already have been sent ("<endpoint>: outcome
  unknown - check orders()"; the cancellation is re-raised unchanged).
- WARNING: a cached script master used after a failed re-check (once per re-check, not per
  call); stream connection lost / silent socket / failed reconnect attempts; stream consumer
  falling behind (merging started) and caught up again; a sync-stream callback still running
  after `stop()`'s timeout; undelivered messages discarded by an async-stream `close()`; a
  timestamp in an unrecognised format (logged once per format shape, for at most 50 shapes per
  process, never the value).
- ERROR: stream stopped by a terminal error; callback exceptions (with traceback).
- **Redaction:** a filter on every `tradesmart` logger masks values of the keys `authorization`,
  `access_token`, `accesstoken`, `susertoken`, `refresh_token`, `checksum`, `code`, `secret_key`,
  `app_key`, `password` (case-insensitive) in dicts, JSON-like text and `key=value` pairs, and
  every `Bearer <token>`. The same masking is applied to the `body` / `raw` stored on exceptions.
  The `websockets` library's frame-level DEBUG output is suppressed because the login frame
  carries the token.
- Secrets never appear in `repr()` (`TradeSmart(client_id='...')`, `AccessToken` with
  `SecretStr` fields) or in exception messages. Redaction is key-based: data you log yourself
  under other keys is not masked.

## Configuration

`ClientConfig` is a frozen dataclass shared by both clients and both streams
(`TradeSmart(..., config=ClientConfig(read_timeout=20))`). Invalid values raise
`ConfigurationError`.

| Field | Default | Meaning |
|---|---|---|
| `base_url` | `https://v2api.tradesmartonline.in/NorenWClientAPIv2` | REST base URL (`http(s)://`; trailing `/` stripped). Change it (with the other three URLs) only to reach another TradeSmart environment, such as a test environment TradeSmart gives you. |
| `ws_url` | `wss://v2api.tradesmartonline.in/NorenWSAPI/` | WebSocket URL (`ws(s)://`). |
| `authorize_url` | `https://v2api.tradesmartonline.in/OAuthlogin/authorize/oauth` | OAuth login page. |
| `script_master_base_url` | `https://v2api.tradesmartonline.in` | Host of the `<EXCH>_symbols.txt.zip` files. |
| `connect_timeout` | `5.0` | Seconds to establish a TCP/TLS connection (REST and script master). |
| `read_timeout` | `10.0` | Seconds to wait for response data (REST and script master). |
| `max_retries` | `2` | Retries for read endpoints (`>= 0`; `0` disables). |
| `backoff_base` | `0.5` | Base of the exponential retry delay, seconds. |
| `backoff_cap` | `8.0` | Maximum retry delay, seconds (also caps `Retry-After`). |
| `rate_limit_mode` | `"wait"` | `"wait"` or `"raise"` when a client-side rate limit is reached. |
| `order_rate_limit_max_wait` | `1.0` | Longest an order waits for the client-side rate limit in `"wait"` mode (`>= 0`); a longer wait raises `RateLimitError` and nothing is sent. |
| `order_rate_limit_per_second` | `10` | Orders per second the client sends (integer 1 to 30). Most TradeSmart accounts are limited to 10; a higher rate needs an exchange-approved strategy and its algo ID, so a value above 10 requires `algo_id` (else `ConfigurationError`). |
| `order_rate_limit_per_minute` | `200` | PlaceOrder calls per rolling 60 seconds (integer 1 to 600, at least `order_rate_limit_per_second`). TradeSmart documents 600, but production refused an order once 200 had been received in the current minute, so the default is 200. A call that would wait longer than `order_rate_limit_max_wait` for this window raises `RateLimitError` and sends nothing. |
| `algo_id` | `None` | Exchange-approved algo ID, sent as `algo_id` on every `PlaceOrder` and `ModifyOrder` (so on every `place_sliced_order` slice); the per-call `algo_id=` of `place_order`, `modify_order` and `place_sliced_order` overrides it. TradeSmart requires it for more than 10 orders per second. A non-blank string or `None`. Not sent on any other endpoint. |
| `user_agent` | `"tradesmart-python/2.0.0"` | `User-Agent` for REST, script-master and WebSocket requests. The SDK token `tradesmart-python/<version>` is always sent: a value without it is sent as `"<your value> tradesmart-python/<version>"`. Printable ASCII only: control characters (CR, LF, tab, ...) and non-ASCII text are rejected with `ConfigurationError`. |
| `log_payloads` | `False` | Log (redacted) request payloads at DEBUG. |
| `check_token_expiry` | `True` | Raise `AuthenticationError` locally when the held `AccessToken` has passed `expires_at` (TradeSmart's 06:00 IST rule). `False` lets the server decide. |
| `heartbeat_interval` | `30.0` | Seconds between stream heartbeats; `2×` this is the dead-socket timeout and the "stable connection" time. |
| `ws_auth_timeout` | `10.0` | Seconds allowed for the socket open and (separately) for the `ak` login acknowledgement. |
| `reconnect_max_attempts` | `20` | Reconnect attempts before giving up (`>= 0`; `None` = unlimited). |
| `reconnect_backoff_base` | `1.0` | Base of the reconnect delay, seconds. |
| `reconnect_backoff_cap` | `60.0` | Maximum reconnect delay, seconds. |
| `keepalive_expiry` | `50.0` | Seconds an idle HTTP connection is kept open for reuse. Must stay a little below TradeSmart's server idle timeout, which was measured on the live server at about 60 s. |
| `max_connections` | `10` | Maximum simultaneous HTTP connections per client. **Placeholder until TradeSmart confirms** its per-client connection limit. |
| `max_keepalive_connections` | `5` | Idle connections kept open for reuse; must be `<= max_connections`. **Placeholder until TradeSmart confirms** its connection limit. |
| `stream_backlog_limit` | `1000` | Once this many messages (or more) wait for your stream code, market-data updates for the same instrument are merged (`>= 1`). |
| `script_master_refresh_time` | `"07:00"` | IST time (`HH:MM`) at which TradeSmart regenerates the script-master files; a cached file written before the most recent refresh is downloaded again. **Placeholder: observed (files carry `Last-Modified` 01:30 GMT), not yet confirmed by TradeSmart.** |
| `script_master_recheck_minutes` | `15` | Minutes between conditional re-checks (`If-Modified-Since`) of a cached script-master file the server had not regenerated since the last refresh; in between the file is used as is (integer `>= 1`). |
| `instruments_dir` | `None` | Folder for the cached script master when no `dest_dir` is given (`None`: `~/.cache/tradesmart`); a non-empty path, `~` expanded. |

All timeouts, backoff values, the heartbeat interval and `keepalive_expiry` must be finite
numbers `> 0`; `order_rate_limit_max_wait` a finite number `>= 0` (NaN and infinity raise
`ConfigurationError`). `order_rate_limit_per_second` is an integer from 1 to 30 (above 10 only with `algo_id`, a non-blank string). `order_rate_limit_per_minute` is an integer from 1 to 600, at least `order_rate_limit_per_second`. `max_retries`, `reconnect_max_attempts` (or `None`), `max_connections`,
`max_keepalive_connections`, `stream_backlog_limit` and `script_master_recheck_minutes` must be
integers (a float or a bool is rejected). The URLs, `user_agent` and
`script_master_refresh_time` must be strings; `log_payloads` and `check_token_expiry` must be
`True` or `False`. A wrong type raises `ConfigurationError` too.

**Custom HTTP client.** Pass `http_client=httpx.Client(...)` (or `httpx.AsyncClient` for
`AsyncTradeSmart`) for proxies or custom TLS. The SDK uses it as-is: its own connection settings
(`keepalive_expiry`, `max_connections`, `max_keepalive_connections`) are **not** applied to it,
it does not close it on `close()`, and it still applies its own timeouts per request. The
script-master download uses the same client. Redirects are never followed, even when the client
was created with `follow_redirects=True`: a 3xx on an order raises `OrderStatusUnknownError`
(nothing is re-sent to the other address); on a read or a script-master download it is
classified like any other status (usually `ResponseParseError`).

## Low-latency setup

Measured with the network mocked, the SDK adds about 0.1–0.2 ms per REST call on top of the
HTTP request itself, and roughly 10–55 µs to parse a stream message; the network round trip
dominates. In short: run close to TradeSmart's servers on a fixed IP (your registered static
IP), keep one client per process and make a cheap read (`client.profile()`) shortly before you
trade so the first order does not pay for connection set-up, keep stream callbacks fast, and do
not raise `keepalive_expiry` without TradeSmart's confirmation. Details:
[Configuration — low-latency setup](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/configuration.md#low-latency-setup).

## Switching from another broker

The SDK uses the method and parameter names Indian algo developers already know from other
broker SDKs (`place_order`, `order_type`, `validity`, `tradingsymbol`, `order_id`, …). If you
are moving a strategy from another broker's Python SDK, the
[migration guides](https://github.com/trade-smart/TradeSmart-Python-SDK/tree/main/sdk-python/docs/migration) map
login, client set-up, every order, portfolio and market-data method, parameter names, constants,
response fields and stream callbacks to their TradeSmart equivalents, and list what has no
equivalent.

## API method reference

All REST calls are `POST {base_url}/{Endpoint}` with body `jData=<json>`. Enum parameters accept
the enum member, the common word (any case) or TradeSmart's code (`TransactionType.BUY` /
`"BUY"` / `"B"`); exchanges take `Exchange.NSE` or the code in any case (sent upper-case). Prices accept `Decimal`, `int`, `float` or `str`
(floats are converted via `str`, so `1500.1` is sent as `"1500.1"`).

### `TradeSmart`

| Method | Endpoint | Returns |
|---|---|---|
| `TradeSmart(client_id, *, access_token=None, app_key=None, secret_key=None, config=None, http_client=None)` | — | client |
| `TradeSmart.from_env(*, config=None)` | — | client from the `TRADESMART_*` environment variables, else the saved login ([login](#the-login-command-in-detail)) |
| `TradeSmart.login_url()` | — (builds the OAuth URL from `app_key`) | `str` |
| `TradeSmart.generate_session(code)` | `GenAcsTok` (never retried) | `AccessToken` (also stored on the client) |
| `TradeSmart.set_access_token(token: str \| AccessToken)` | — | `None` |
| `TradeSmart.profile()` | `UserDetails` | `UserDetails` |
| `TradeSmart.margins()` | `Limits` | `Limits` (funds, limits and margins) |
| `TradeSmart.positions()` | `PositionBook` | `list[Position]` |
| `TradeSmart.holdings(product=Product.CNC)` | `Holdings` | `list[Holding]` |
| `TradeSmart.place_order(*, exchange, tradingsymbol, quantity, transaction_type, product, order_type, price=None, trigger_price=None, validity=Validity.DAY, disclosed_quantity=None, amo=False, tag=None, book_loss_price=None, book_profit_price=None, trail_price=None, market_protection=None, algo_id=None)` | `PlaceOrder` (**state-changing**, rate-limited) | order id `str` |
| `TradeSmart.place_sliced_order(*, <the place_order arguments>, max_slice_quantity=None, max_slices=25)` | script master + `GetSecurityInfo`, then one `PlaceOrder` per slice (**state-changing**, rate-limited; stops at the first failure) | `SlicedOrderResult` |
| `TradeSmart.modify_order(order_id, *, exchange, tradingsymbol, quantity, price, order_type, product, trigger_price=None, validity=None, tag=None, book_loss_price=None, book_profit_price=None, trail_price=None, algo_id=None)` | `ModifyOrder` (**state-changing**) | order id `str` (ack `norenordno`, else `result`, else `order_id`) |
| `TradeSmart.cancel_order(order_id)` | `CancelOrder` (**state-changing**) | `order_id` |
| `TradeSmart.exit_order(order_id, product)` | `ExitSNOOrder` (**state-changing**; product `CO` or `BO`) | `order_id` |
| `TradeSmart.orders()` | `OrderBook` | `list[Order]` |
| `TradeSmart.order_history(order_id)` | `SingleOrdHist` | `list[OrderHistoryEntry]` |
| `TradeSmart.trades()` | `TradeBook` | `list[Trade]` |
| `TradeSmart.order_margin(*, exchange, tradingsymbol, quantity, price, product, transaction_type, order_type, trigger_price=None, book_loss_price=None, order_id=None, sno_number=None)` | `GetOrderMargin` (a read; nothing is placed) | `OrderMargin` |
| `TradeSmart.place_gtt(*, exchange, tradingsymbol, alert_type, trigger_value, quantity, transaction_type, product, order_type, tag, price=None, trigger_price=None, validity=Validity.DAY, alert_validity=GttValidity.GTT, disclosed_quantity=None)` | `PlaceGTTOrder` (**state-changing**, never retried; a GTT whose condition is already true fires at once; products CNC, NRML, CO; `tag` required; `alert_validity=GTT` lasts at most 365 days or the contract expiry, whichever is earlier; guide `docs/gtt.md`) | alert id `str` (`al_id`) |
| `TradeSmart.modify_gtt(alert_id, *, exchange, tradingsymbol, alert_type, trigger_value, quantity, transaction_type, product, order_type, tag, price=None, trigger_price=None, validity, alert_validity, disclosed_quantity=None)` | `ModifyGTTOrder` (**state-changing**; every field sent again; `validity` and `alert_validity` required, no defaults; a modify that makes the condition true fires at once) | the server's alert id `str` (may differ from `alert_id`) |
| `TradeSmart.cancel_gtt(alert_id)` | `CancelGTTOrder` (**state-changing**) | the server's alert id `str` (may differ from `alert_id`) |
| `TradeSmart.pending_gtts()` | `GetPendingGTTOrder` | `list[Gtt]` |
| `TradeSmart.triggered_gtts()` | `GetTrgOi` | `list[TriggeredGtt]` |
| `TradeSmart.gtt_alert_types()` | `GetEnabledGTTs` | `list[str]` (alert families, e.g. `["ATP", "LTP"]`) |
| `TradeSmart.search_instruments(text)` | `SearchScrip` | `list[ScripMatch]` |
| `TradeSmart.quote(exchange, token)` | `GetQuotes` (rate-limited) | `Quote` |
| `TradeSmart.security_info(exchange, token)` | `GetSecurityInfo` | `SecurityInfo` |
| `TradeSmart.option_chain(exchange, tradingsymbol, strike_price, count)` | `GetOptionChain` | `list[OptionContract]` |
| `TradeSmart.option_greeks(exchange, tradingsymbol, *, rate=0.065, dividend_yield=0.0, option_price=None, underlying_price=None, spot_price=None, expiry_time=None)` | `GetQuotes` (up to two, rate-limited) + the script master | `OptionGreeks` (IV and Greeks; estimates — [guide](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/option-analytics.md)) |
| `TradeSmart.option_chain_snapshot(exchange, tradingsymbol, strike_price, count=25)` | `GetOptionChain` + one `GetQuotes` per contract | `OptionChainSnapshot` (`rows`, `missing`, `.pcr(by="oi")`, `.max_pain()`) |
| `TradeSmart.buildup(exchange, token)` | `GetQuotes` + `TPSeries` | `BuildupReading` (`state`, `price_change`, `oi_change`, `basis="since_open"`) |
| `TradeSmart.historical_data(exchange, token, from_date=None, to_date=None, interval=None)` | `TPSeries` (rate-limited) | `list[Candle]` (intraday, newest first) |
| `TradeSmart.daily_history(exchange, tradingsymbol, from_date, to_date)` | `EODChartData` | `list[DailyCandle]` |
| `TradeSmart.instruments(exchange)` | `GET {script_master_base_url}/{EXCH}_symbols.txt.zip` (file reused until the next daily refresh) | `list[dict[str, str]]` |
| `TradeSmart.download_instruments(exchange, dest_dir=None, *, force=False)` | as above | `pathlib.Path` of the extracted `.txt` (default folder `ClientConfig.instruments_dir`, else `~/.cache/tradesmart`; `~` expanded) |
| `TradeSmart.request(endpoint, payload, *, state_changing, expect="object")` | any | parsed JSON (`dict` / `list`) — see [Low-level `request()`](#low-level-request) |
| `TradeSmart.close()` / `with TradeSmart(...)` | — | `None` |

`modify_order` requires `price` as a keyword. TradeSmart's ModifyOrder docs list `LMT` and
`SL-LMT` as the order types; a `MARKET` / `SL-M` modification (with `price=None`) is passed
through to the server, which decides whether it is allowed.

### `AsyncTradeSmart`

The same constructor arguments (`http_client` is an `httpx.AsyncClient`) and the same methods
with the same parameters and return values. `AsyncTradeSmart.from_env()`,
`AsyncTradeSmart.login_url()` and `AsyncTradeSmart.set_access_token()` are plain methods; every
other method is a coroutine:

`await AsyncTradeSmart.generate_session(code)`, `AsyncTradeSmart.profile()`,
`AsyncTradeSmart.margins()`, `AsyncTradeSmart.positions()`, `AsyncTradeSmart.holdings()`,
`AsyncTradeSmart.place_order()`, `AsyncTradeSmart.place_sliced_order()`,
`AsyncTradeSmart.modify_order()`,
`AsyncTradeSmart.cancel_order()`, `AsyncTradeSmart.exit_order()`, `AsyncTradeSmart.orders()`,
`AsyncTradeSmart.order_history()`, `AsyncTradeSmart.trades()`, `AsyncTradeSmart.order_margin()`,
`AsyncTradeSmart.place_gtt()`, `AsyncTradeSmart.modify_gtt()`, `AsyncTradeSmart.cancel_gtt()`,
`AsyncTradeSmart.pending_gtts()`, `AsyncTradeSmart.triggered_gtts()`,
`AsyncTradeSmart.gtt_alert_types()`,
`AsyncTradeSmart.search_instruments()`, `AsyncTradeSmart.quote()`,
`AsyncTradeSmart.security_info()`, `AsyncTradeSmart.option_chain()`,
`AsyncTradeSmart.option_greeks()`, `AsyncTradeSmart.option_chain_snapshot()` (at most 10 quotes in
flight), `AsyncTradeSmart.buildup()`,
`AsyncTradeSmart.historical_data()`, `AsyncTradeSmart.daily_history()`,
`AsyncTradeSmart.instruments()`, `AsyncTradeSmart.download_instruments()`,
`AsyncTradeSmart.request()`, `AsyncTradeSmart.aclose()` (also via `async with AsyncTradeSmart(...)`).

### Streams

| Method | Notes |
|---|---|
| `AsyncTradeSmartStream(client_id, access_token, *, config=None, on_message=None, on_state_change=None)` | `access_token`: `str` or `AccessToken` (an expired `AccessToken` raises `AuthenticationError` in `connect()` unless `check_token_expiry=False`) |
| `AsyncTradeSmartStream.from_env(*, config=None, on_message=None, on_state_change=None)` | `TRADESMART_CLIENT_ID` + `TRADESMART_ACCESS_TOKEN`, else the saved login; `TRADESMART_APP_KEY` is ignored (a stream cannot log in) |
| `AsyncTradeSmartStream.from_client(client, *, config=None, on_message=None, on_state_change=None)` | client id and current token of a `TradeSmart` or `AsyncTradeSmart` (default `config`: the client's); raises `AuthenticationError` without a usable token |
| `AsyncTradeSmartStream.connect()` / `AsyncTradeSmartStream.close()` | coroutines; also via `async with` |
| `AsyncTradeSmartStream.subscribe_touchline(instruments)` / `AsyncTradeSmartStream.unsubscribe_touchline(instruments)` | wire `t` / `u` |
| `AsyncTradeSmartStream.subscribe_depth(instruments)` / `AsyncTradeSmartStream.unsubscribe_depth(instruments)` | wire `d` / `ud` |
| `AsyncTradeSmartStream.subscribe_orders()` / `AsyncTradeSmartStream.unsubscribe_orders()` | wire `o` (for `actid` = client id) / `uo` |
| `AsyncTradeSmartStream.subscribe_positions()` / `AsyncTradeSmartStream.unsubscribe_positions()` | wire `p` / `up` |
| `AsyncTradeSmartStream.snapshot(key)` / `AsyncTradeSmartStream.state` | `dict \| None` / `StreamState` |
| `AsyncTradeSmartStream.reconnect_count` / `AsyncTradeSmartStream.heartbeat_acks` | `int`: successful reconnects (0 until the first) / heartbeat replies (`hk`) received |
| `TradeSmartStream(client_id, access_token, *, config=None, on_touchline=None, on_depth=None, on_order_update=None, on_position=None, on_connect=None, on_disconnect=None, on_error=None, on_message=None, on_reconnect=None)` | every callback runs on the `tradesmart-callbacks` thread; `with stream:` = `start()` / `stop()` |
| `TradeSmartStream.from_env(*, config=None, on_...=None)` / `TradeSmartStream.from_client(client, *, config=None, on_...=None)` | as for `AsyncTradeSmartStream`, with the same callback keywords as the constructor |
| `TradeSmartStream.start(timeout=None)` / `TradeSmartStream.stop(timeout=5.0)` / `TradeSmartStream.run_forever()` | lifecycle (`start` default timeout `2 × ws_auth_timeout + 5` s) |
| `TradeSmartStream.subscribe_touchline(instruments)` / `TradeSmartStream.unsubscribe_touchline(instruments)` | block until processed (≤ 10 s), also from a callback |
| `TradeSmartStream.subscribe_depth(instruments)` / `TradeSmartStream.unsubscribe_depth(instruments)` | 〃 |
| `TradeSmartStream.subscribe_orders()` / `TradeSmartStream.unsubscribe_orders()` | 〃 |
| `TradeSmartStream.subscribe_positions()` / `TradeSmartStream.unsubscribe_positions()` | 〃 |
| `TradeSmartStream.snapshot(key)` / `TradeSmartStream.state` | `dict \| None` / `StreamState` |
| `TradeSmartStream.reconnect_count` / `TradeSmartStream.heartbeat_acks` | `int`: successful reconnects (0 until the first; already counted when `on_reconnect` runs) / heartbeat replies (`hk`) received |

### Module-level helpers

| Name | Returns |
|---|---|
| `build_authorize_url(app_key, *, authorize_url=ClientConfig.authorize_url)` | OAuth URL with `client_id=<app_key>` (URL-quoted) |
| `compute_checksum(app_key, secret_key, code)` | SHA-256 hex digest of `app_key + secret_key + code` |
| `round_to_tick(price, tick_size)` | `Decimal` rounded to the nearest tick (half-way up), with the tick's decimal places; `ValueError` if `tick_size <= 0` |
| `AccessToken.from_response(data, *, issued_at=None)` | `AccessToken` from a `GenAcsTok` body (expiry = next 06:00 IST) |
| `AccessToken.is_expired(now=None)` | `bool` |
| `tradesmart.instruments.load(path)` | `list[dict[str, str]]` from a script-master file |
| `tradesmart.instruments.find(rows, **criteria)` | rows whose value for every given header equals the given string |
| `tradesmart.greeks.black76(...)`, `black_scholes(...)`, `implied_volatility(...)`, `greeks_from_price(...)` | the pure maths behind `option_greeks()`: `black76`, `black_scholes` and `greeks_from_price` return `Greeks`, `implied_volatility` returns `float \| None` (`None` when no volatility reproduces the price); `DEFAULT_RATE` 0.065, `DEFAULT_DIVIDEND_YIELD` 0.0 — estimates, see the [guide](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/option-analytics.md) |
| `tradesmart.analytics.pcr(snapshot, by="oi")`, `max_pain(snapshot)`, `classify_buildup(price_change, oi_change)`, `buildup_from_snapshot(stream_snapshot)` | `Decimal \| None`, `MaxPain \| None`, `Buildup`, `BuildupReading` (stream values: previous close and previous OI) |
| `tradesmart.endpoints.<area>.<name>_call(...)` | a `Call` (`.spec.name`, `.payload`) built and validated without sending — e.g. `place_order_call`, `modify_order_call`, `cancel_order_call`, `exit_sno_order_call`, `order_margin_call`, `quote_call`, `time_price_series_call`; the first argument is the client id |

## Models

Response models live in `tradesmart.models.<area>` (stream models are also exported from
`tradesmart`). They are frozen pydantic v2 models with these rules:

- Every field is optional (`None` when absent). Undocumented fields the server adds are kept
  (`extra="allow"`) and readable as attributes under their wire name.
- `model.raw` is the original wire payload (for `AccessToken` it is redacted).
- Text and id fields accept JSON numbers too (`22` becomes `"22"`). For `Order`,
  `OrderHistoryEntry`, `Trade`, `Position`, `Holding`, `OrderUpdate`, `PositionAck` and
  `PositionUpdate` one bad field never drops the record: a field that still fails validation is
  set to `None` (the original value stays in `raw`) and one WARNING names the field (never its
  value). Identity fields are never nulled: if `norenordno` (`order_id`) or `status`
  fails, the record fails as before (`ResponseParseError` on REST, `UnknownMessage` on the
  stream, still passed to `on_message`). Other models raise `ResponseParseError` (REST) or
  become `UnknownMessage` (stream).
- Renamed wire fields (glossary): `uid→user_id`, `actid→account_id`, `exch→exchange`,
  `tsym→tradingsymbol`, `qty→quantity`, `prc→price`, `trgprc→trigger_price`, `prd→product`,
  `trantype→transaction_type`, `prctyp→order_type`, `ret→validity`, `norenordno→order_id`,
  `remarks→tag` (except `OrderMargin.remarks`, which is the margin message),
  `avgprc→average_price`, `fillshares→filled_quantity`, `dscqty→disclosed_quantity`,
  `blprc→book_loss_price`, `bpprc→book_profit_price`, `trailprc→trail_price`,
  `mkt_protection→market_protection`, `rejreason→reject_reason`, `exchordid→exchange_order_id`,
  `snonum→sno_number`, `lp→last_price`, `pc→percent_change`, `v→volume`,
  `o/h/l/c→open/high/low/close`, `ap→average_trade_price`, `oi→open_interest`, `ls→lot_size`,
  `ti→tick_size`, `pp→price_precision`, `netqty→net_quantity`, `rpnl→realized_pnl`,
  `dmsg→display_message`. Every other field keeps its wire name (e.g. `daybuyqty`). Models
  accept either name when constructed.
- Types: prices/amounts → `Decimal` (blank → `None`); volume and open-interest fields (`v`,
  `oi`, `poi`, `toi`, `intv`, `intoi`) → `Decimal`; quantities → `int`; ids and tokens → `str`
  (JSON numbers are accepted and converted); epoch fields → timezone-aware UTC `datetime`
  (`ssboe`, `ordenttm`, `Quote.feed_time` from `lut`, stream `ft`; `"0"`/blank → `None`); text
  timestamps (`request_time`, `time`, `norentm`, `exch_tm`, `Trade.fltm`) → IST-aware
  `datetime`, parsed from `HH:MM:SS DD-MM-YYYY`, `DD-MM-YYYY HH:MM:SS`, `DD/MM/YYYY HH:MM:SS`,
  `DD-MM-YYYY` or `DD-MON-YYYY`. An unrecognised timestamp becomes `None` (the string stays in
  `raw`) and is logged once per format at WARNING.
- `transaction_type`, `order_type`, `product`, `validity` and `status` are the enum member when
  the value is known (`order.status == OrderStatus.COMPLETE`, `order.product is Product.MIS`)
  and the raw string otherwise; a blank string becomes `None`. They never cause a parse failure.

| Model (module) | Returned by | Key attributes |
|---|---|---|
| `AccessToken` (`tradesmart`) | `generate_session` | `access_token`, `susertoken`, `refresh_token` (`SecretStr`), `expires_in`, `user_id`, `account_id`, `uname`, `issued_at`, `expires_at`, `is_expired()` |
| `UserDetails` (`models.user`) | `profile` | `user_id`, `account_id`, `uname`, `email`, `m_num`, `brkname`, `brnchid`, `uprev`, `access_type`, `exarr` (exchanges), `orarr` (order types), `prarr` (`ProductInfo`: `product`, `s_prdt_ali`, `exch`), `request_time` |
| `Holding` (`models.portfolio`) | `holdings` | `instruments` (`exch_tsym` → `HoldingInstrument`: `exchange`, `tradingsymbol`, `token`, `last_price`, `lot_size`, …), `holdqty`, `dpqty`, `npoadqty`, `colqty`, `benqty`, `btstqty`, `usedqty`, `upldprc`, … |
| `Limits` (`models.portfolio`) | `margins` | `cash`, `payin`, `payout`, `marginused`, `realized_pnl`, `unmtom`, `span`, `expo`, `premium`, `collateral`, `brokerage`, `daycash`, `turnoverlmt`, `pendordvallmt`, … (all `Decimal`) |
| `Position` (`models.portfolio`) | `positions` | `exchange`, `tradingsymbol`, `token`, `product`, `net_quantity`, `netavgprc`, `last_price`, `realized_pnl`, `daybuyqty`/`daysellqty`, `daybuyavgprc`/`daysellavgprc`, `cfbuyqty`/`cfsellqty`, `totbuyamt`/`totsellamt`, `bep`, `mult`, … |
| `ScripMatch` (`models.market`) | `search_instruments` | `exchange`, `tradingsymbol`, `token`, `lot_size`, `tick_size`, `price_precision`, `instname`, `symname`, `optt`, `exd`, `cname`, `dname` |
| `Quote` (`models.market`) | `quote` | `last_price`, `open`/`high`/`low`/`close`, `volume`, `average_trade_price`, `open_interest`, `uc`/`lc`, `wk52_h`/`wk52_l`, `feed_time` (`lut`), `bp1..5`/`bq1..5`/`bo1..5`, `sp1..5`/`sq1..5`/`so1..5`, `tbq`/`tsq`, `ltq`, `ltt`, `ltd` |
| `SecurityInfo` (`models.market`) | `security_info` | `symname` (wire `symname` or `symnam`), `isin`, `lot_size`, `tick_size`, `frzqty`, `strprc`, `optt`, `exd`, margins (`elmmrg`, `varmrg`, `delmrg`, …), `uc`/`lc`, `prcftr`, … |
| `OptionContract` (`models.market`) | `option_chain` | `exchange`, `tradingsymbol`, `token`, `option_type` (`optt`), `strike_price` (`strprc`), `lot_size`, `tick_size` |
| `OptionGreeks` (`tradesmart`) | `option_greeks` | `iv`, `delta`, `gamma`, `theta` (per calendar day), `vega` (per 1 volatility point), `rho` (per 1 % rate), `price`, `status` (`ok`, `near_expiry`, `expired`, `price_at_or_below_intrinsic`, `no_price`), `years_to_expiry`, `model` (`black76` or `bsm`), the inputs used (`option_price`, `underlying_price`, `forward`, `same_expiry_future`, `rate`, `dividend_yield`, `expiry`, …); floats, never NaN or infinity |
| `OptionChainSnapshot` (`tradesmart`) | `option_chain_snapshot` | `rows` (`ChainRow`: `strike`, `option_type`, `tradingsymbol`, `token`, `last_price`, `open_interest`, `volume`, `close`), `missing`, `fetched_at`, `pcr(by)`, `max_pain()` |
| `BuildupReading`, `Buildup` (`tradesmart`) | `buildup`, `buildup_from_snapshot` | `state` (`LONG_BUILDUP`, `SHORT_BUILDUP`, `LONG_UNWINDING`, `SHORT_COVERING`, `NEUTRAL`), `price_change`, `oi_change`, `basis` |
| `Candle` (`models.market`) | `historical_data` | `time`, `ssboe`, `into`/`inth`/`intl`/`intc`, `intvwap`, `intv`, `volume`, `intoi`, `open_interest` |
| `DailyCandle` (`models.market`) | `daily_history` | `time`, `ssboe`, `into`/`inth`/`intl`/`intc`, `intv` |
| `Order` (`models.orders`) | `orders` | `order_id`, `status`, `exchange`, `tradingsymbol`, `quantity`, `price`, `trigger_price`, `order_type`, `product`, `transaction_type`, `validity`, `filled_quantity`, `average_price`, `reject_reason`, `exchange_order_id`, `norentm`, `ordenttm`, `exch_tm`, `amo`, `tag`, `sno_number`, `parent_id` (`add_ord_id`: the GTT that placed it), … |
| `OrderHistoryEntry` (`models.orders`) | `order_history` | everything in `Order` plus `pan`, `rpt`, `introp_exch` |
| `Trade` (`models.orders`) | `trades` | `order_id`, `exchange`, `tradingsymbol`, `quantity`, `filled_quantity`, `average_price`, `order_type`, `product`, `transaction_type`, `flid`, `flqty`, `flprc`, `fltm`, `exchange_order_id`, `tag`, `snoordt`, … |
| `OrderMargin` (`models.orders`) | `order_margin` | `cash`, `marginused`, `ordermargin`, `marginusedprev`, `remarks`, `request_time` |
| `Gtt` (`tradesmart`) | `pending_gtts` | `alert_id` (`al_id`), `alert_type` (`GttAlertType`), `trigger_value` (`d`), `alert_validity` (`GttValidity`, wire `validity`), `exchange`, `tradingsymbol`, `token`, `tag`, `transaction_type`, `order_type`, `product`, `validity` (`ret`), `quantity`, `price`, `trigger_price`, … |
| `TriggeredGtt` (`tradesmart`) | `triggered_gtts` | everything in `Gtt` plus `norentm`, `sms_flag`, `email_flag`, `push_flag` |
| `SlicedOrderResult` (`tradesmart`; a frozen dataclass) | `place_sliced_order` | `requested_quantity`, `slice_quantities`, `order_ids`, `placed_quantity`, `unsent_quantity`, `complete`, `error` |
| `TouchlineAck` / `TouchlineFeed` (`tradesmart`) | stream `tk` / `tf` | `key` (`EXCH\|TOKEN`), `exchange` (`e`), `token` (`tk`), `tradingsymbol` (`ts`), `last_price`, `percent_change`, `volume`, `open`/`high`/`low`/`close`, `average_trade_price`, `open_interest`, `bp1`/`bq1`/`sp1`/`sq1`, `ft` |
| `DepthAck` / `DepthFeed` (`tradesmart`) | stream `dk` / `df` | touchline fields plus five levels `bp1..5`, `bq1..5`, `bo1..5`, `sp1..5`, `sq1..5`, `so1..5`, `tbq`/`tsq`, `ltq`, `ltt`, `uc`/`lc`, `high_52w` (`52h`), `low_52w` (`52l`) |
| `OrderUpdate` (`tradesmart`) | stream `om` | `order_id`, `status`, `reporttype`, `quantity`, `price`, `order_type`, `transaction_type`, `validity`, `filled_quantity`, `average_price`, `flqty`/`flprc`/`fltm`, `reject_reason`, `trigger_price`, `tag`, `exch_tm`, … |
| `PositionAck` / `PositionUpdate` (`tradesmart`) | stream `pk` / `pm` | `net_quantity`, `realized_pnl`, `product`, `buyavgprc`/`sellavgprc`, day / carry-forward / open quantities and amounts |
| `UnsubscribeAck`, `ConnectAck`, `HeartbeatAck`, `UnknownMessage` (`tradesmart`) | stream | `k`; `status` (`s`); `ft`; `raw` |

Each model's docstring and field list in the package source (`tradesmart/models/`) is the
complete reference.

## Enums

All enums are `str` subclasses that print as their wire code on every Python version
(`str(OrderStatus.OPEN)` and `f"{OrderStatus.OPEN}"` are `"OPEN"`). Members send TradeSmart's
wire codes; when you pass a string, `Exchange`, `TransactionType`, `OrderType`, `Product`,
`Validity` and `OrderStatus` also accept the codes or common words below, in any case.

| Enum | Members → wire code | Also accepted |
|---|---|---|
| `Exchange` | `NSE`, `BSE`, `NFO`, `BFO`, `CDS`, `BCD`, `MCX` | the codes in any case (REST calls and stream keys) |
| `TransactionType` | `BUY` → `B`, `SELL` → `S` | `"BUY"`, `"SELL"` |
| `OrderType` | `LIMIT` → `LMT`, `MARKET` → `MKT`, `SL` → `SL-LMT`, `SL_M` → `SL-MKT` (TradeSmart refuses `MKT` orders placed through the API; see [Placing orders](#placing-orders)) | `"LIMIT"`, `"MARKET"`, `"SL"`, `"SL-M"`, `"SLM"` |
| `Validity` | `DAY`, `IOC`, `EOS` (TradeSmart documents NSE/NFO/CDS: DAY, IOC; BSE/BFO/BCD also EOS — not validated client-side) | the codes in any case |
| `Product` | `CNC` → `C`, `MIS` → `I`, `NRML` → `M`, `CO` → `H` (cover order), `BO` → `B` (bracket order), `MTF` → `F` (the codes accepted depend on the account; `profile().prarr` is authoritative) | `"CNC"`, `"MIS"`, `"NRML"`, `"CO"`, `"BO"`, `"MTF"` |
| `OrderStatus` | `PENDING`, `OPEN`, `TRIGGER_PENDING`, `COMPLETE`, `CANCELED`, `REJECTED` | the codes in any case |
| `GttAlertType` | `LTP_ABOVE` → `LTP_A_O`, `LTP_BELOW` → `LTP_B_O`, `CHANGE_PERCENT_ABOVE` → `CH_PER_A_O`, `CHANGE_PERCENT_BELOW` → `CH_PER_B_O`, `ATP_ABOVE` → `ATP_A_O`, `ATP_BELOW` → `ATP_B_O`, `OI_ABOVE` → `OI_A_O`, `OI_BELOW` → `OI_B_O`, `TOI_ABOVE` → `TOI_A_O`, `TOI_BELOW` → `TOI_B_O`, `HIGH_52W_ABOVE` → `LTP_A_52HIGH_O`, `HIGH_52W_BELOW` → `LTP_B_52HIGH_O`, `VOLUME_ABOVE` → `VOLUME_A_O` (a GTT's `ai_t`: what it watches and the direction) | the codes or member names in any case |
| `GttValidity` | `GTT`, `DAY` (a GTT's own validity, wire `validity`; not the order's `ret`) | the codes in any case |
| `StreamState` | `DISCONNECTED`, `CONNECTING`, `AUTHENTICATING`, `CONNECTED`, `RECONNECTING`, `CLOSED` | — |

## Low-level `request()`

For endpoints without a typed method — notably **`/PlaceMultiOrder` and `/GetQuotesMultiple`,
whose schemas are not documented** — use the escape hatch. It applies the same auth header,
encoding, redaction, error classification and rate-limit groups.

*Illustrative (the payloads are placeholders: these schemas are undocumented):*

```python
body = client.request("GetQuotesMultiple", {"...": "..."}, state_changing=False)
body = client.request("PlaceMultiOrder", {"...": "..."}, state_changing=True)  # never retried
rows = client.request("OrderBook", {}, state_changing=False, expect="list")
```

- `state_changing=True` → never retried; an ambiguous failure raises `OrderStatusUnknownError`.
  `PlaceOrder`, `PlaceMultiOrder`, `ModifyOrder`, `CancelOrder`, `ExitSNOOrder`, `GenAcsTok`, the
  GTT writes and the iceberg writes are **always** treated as state-changing, even with `state_changing=False`. Pass `True` for
  anything else that can create, change or cancel an order.
- The endpoint name is normalised first: surrounding whitespace and `/` are removed and the rest
  must be ASCII letters only (`"Place/Order"`, `"../x"` or `""` raise `ConfigurationError`;
  nothing is sent). Known names match case-insensitively and are sent in their documented
  spelling, so `"/PlaceOrder"`, `"placeorder"` or `" PLACEORDER "` are still `PlaceOrder`:
  state-changing and rate-limited.
- `uid` is added (= `client_id`) when absent; `actid` is not added.
- Payload values must be `str`, `int`, `float`, `Decimal`, `bool` (→ `"Yes"`/`"No"`) or an enum
  (→ its value); anything else (including `None`) raises `TypeError`.
- Returns the parsed JSON body (`dict` for `expect="object"`, `list` for `expect="list"`), with
  the same shape checks as typed endpoints. "No data" is **not** turned into `[]` here: it raises
  `APIError` (or a subclass, e.g. `NotFoundError` for an HTTP 404).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ConfigurationError: ... run python -m tradesmart login` | No usable saved login. Run `python -m tradesmart login` (again after 06:00 IST). |
| `AuthenticationError: ... expired ...` / `Session Expired : Invalid Session Key` | TradeSmart tokens end at the next 06:00 IST. Log in again. |
| HTTP **403** / `AuthenticationError` although the token is fresh | Compare the machine's public IP with the **Primary** and **Secondary Static IP** of your app on the API Dashboard (changeable at most 2 times per day). An IPv6 machine: use IPv4 for now. Last resort: `support@tradesmartonline.in`. |
| `OrderStatusUnknownError` | The order (or GTT) may or may not exist. **Do not resend.** Find it by its `tag` in `orders()` (a GTT: in `pending_gtts()`). |
| `OrderRejectedError` | Read `exc.emsg` (margin, price band, MKT not allowed for API orders, …); fix the request instead of retrying. |
| `OrderRejectedError` "CTCLID not assigned" on an `MCX` order | MCX orders through the API are not yet enabled for TradeSmart; nothing to fix on your side. |
| `RateLimitError` with `http_status=None` | The SDK's own limiter: wait `exc.retry_after` seconds; nothing was sent. |
| `historical_data()` misses today | A `date` means 00:00 IST; pass `datetime.now(timezone.utc)` as `to_date`. |

Every other message, with causes and fixes:
[Troubleshooting](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/troubleshooting.md).

## Testing your strategy

**Offline broker:** `tradesmart.testing.OfflineBroker` runs your code with the real clients and
streams against a local server inside your test, with fills, rejections, lost replies and dropped
streams on demand: [Testing your strategy offline](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/testing.md),
example [`examples/offline_strategy_test.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/offline_strategy_test.py).

Order methods have no "paper" mode, so test your code without the network: build calls with the
`*_call()` helpers (see the dry run above) or give the client an `httpx` client with a mock
transport, e.g.
`TradeSmart("YOUR_CLIENT_ID", access_token="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))`,
where `handler` returns TradeSmart-shaped JSON for each endpoint.

The SDK's own test suite is part of the source repository (not the wheel); how to run it is in
[CONTRIBUTING.md](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/CONTRIBUTING.md).

## Examples

Runnable scripts in the repository's
[`examples/`](https://github.com/trade-smart/TradeSmart-Python-SDK/tree/main/sdk-python/examples)
folder. All of them do nothing on import, catch `TradeSmartError` and never print a token; the
ones that call TradeSmart use the login saved by `python -m tradesmart login` (or the
`TRADESMART_*` environment variables).

| File | Shows |
|---|---|
| [`order_lifecycle.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/order_lifecycle.py) | **dry run by default**; with `--live`: check both prices against a live quote (refused unless below the last price and inside the band) → place a tagged order → status from `orders()` → modify → cancel (also on the way out after a failure), finding the order by its tag after an unknown outcome (never resends) |
| [`place_order.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/place_order.py) | **dry run by default** (prints the `PlaceOrder` payload); real order only with `--i-understand-this-places-a-real-order`; every order carries a unique tag, used to find it after an unknown outcome (never resends) |
| [`sliced_order.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/sliced_order.py) | `place_sliced_order` on the offline broker (no login): an order above the freeze quantity in three slices, a rejected slice that stops the order, and an unknown outcome found by its tag (looked up a few times, never resent) |
| [`gtt_alert.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/gtt_alert.py) | GTT orders on the offline broker (no login): a fresh last price checked first (a GTT already true fires at once), place, list, move the trigger with `modify_gtt` (both validities from the GTT's own row), fire it and find its order by `parent_id`; an unknown outcome found by its tag in `pending_gtts()` (never resent) and cancelled |
| [`offline_strategy_test.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/offline_strategy_test.py) | a strategy and its tests on the offline broker (no login): a fill, a rejection, a lost reply found by its tag, a dropped stream |
| [`first_call.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/first_call.py) | profile, funds, positions, order book |
| [`market_data.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/market_data.py) | search, quote, security info, intraday and daily candles, script master |
| [`stream_sync.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/stream_sync.py) | `TradeSmartStream.from_env()` with callbacks |
| [`stream_async.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/stream_async.py) | `AsyncTradeSmartStream.from_env()` with `async for` |
| [`async_quotes.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/async_quotes.py) | concurrent quotes with `AsyncTradeSmart` and the rate limiter |
| [`auth_server_flow.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/auth_server_flow.py) | the login flow written with the SDK's calls (localhost callback, token exchange), to adapt for a server; `--save-token PATH` writes the token to an owner-only file that `first_call.py --token-file PATH` reads |

## Known limitations

- **`/PlaceMultiOrder` and `/GetQuotesMultiple`**: the endpoints exist but their request/response
  schemas are undocumented, so there are no typed methods; use `client.request()` (rate-limit
  groups already apply). Multi-leg (2L / 3L) orders are therefore not exposed yet. Typed methods
  follow once TradeSmart supplies the schema.
- **Iceberg orders**: the SDK does not offer iceberg orders; `request()` can reach them (never
  retried, success only on the documented word). Use `place_sliced_order` to send a large order
  in pieces. **OCO (one-cancels-other) GTTs**: supported by TradeSmart, but their request fields
  and replies are not documented yet, so there is no method for them. Plain GTTs: see
  [GTT orders](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/gtt.md).
- **MCX orders through the API are not yet enabled** for TradeSmart: they are rejected with
  "CTCLID not assigned" (or acknowledged and then `REJECTED` with that reason). Nothing to fix on
  your side.
- **ModifyOrder success response shape is unconfirmed**: the SDK treats `stat: Ok` as accepted
  and returns the acknowledgement's id (else the id you passed); confirm the change with
  `order_history()`.
- **Values pending confirmation by TradeSmart**: `max_connections` and
  `max_keepalive_connections` use provisional defaults until TradeSmart confirms its connection
  limit; `keepalive_expiry` (50 s) stays below the server's idle timeout measured on the live
  server (about 60 s); `script_master_refresh_time` (07:00 IST) is the observed daily refresh of
  the script-master files.
- **No public test environment**: TradeSmart's API documentation describes no sandbox. The SDK
  is tested against mocks built from TradeSmart's official examples, the offline broker and
  TradeSmart's live API (the source repository's opt-in live suites: read-only by default; order
  tests follow the safety gates of the SDK behaviour specification, section 14, and the live
  order suite's README in the source repository). The four URLs in `ClientConfig` (`base_url`, `ws_url`,
  `authorize_url`, `script_master_base_url`) are configurable.
- **Holdings response shape is unconfirmed**: the docs have no example response. The SDK requires
  a JSON array; if a live account returns an object you will get `ResponseParseError` — please
  report it with `exc.body` (remove personal data).
- **Undocumented fields and formats**: response field types are not documented (all values
  arrive as strings); several timestamp formats are observed (`HH:MM:SS DD-MM-YYYY`,
  `DD-MM-YYYY HH:MM:SS`, `DD-MON-YYYY`, epochs); undocumented fields seen in examples are modelled
  as plain strings and anything new is kept as an extra attribute and in `raw`. Unparseable
  timestamps become `None` (with a WARNING).
- **Script master** column layout is undocumented → generic `dict` rows, no typed instrument model.
- No token refresh or logout endpoint (not documented by TradeSmart); `python -m tradesmart
  logout` removes only the local token file (and, with `--forget-keys`, the saved keys).

