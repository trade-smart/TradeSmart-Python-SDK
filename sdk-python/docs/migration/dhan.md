# Moving from Dhan (`dhanhq`) to TradeSmart

This guide maps the Dhan Python SDK (`dhanhq` 2.3: `DhanContext`, `dhanhq`, `DhanLogin`,
`MarketFeed`, `OrderUpdate`) to `tradesmart`, TradeSmart's official Python SDK. Both use flat,
snake_case methods on one client object, but they differ in three ways that affect almost every
line you port: **errors are exceptions instead of status dicts**, **instruments are trading
symbols instead of security ids**, and **constant values are different**. Full SDK reference:
[SDK reference](../reference.md).

In the tables, `dhan` is a `dhanhq` instance and `client` a `tradesmart.TradeSmart` instance.
Every method shown on `TradeSmart` exists with the same parameters on `AsyncTradeSmart` as a
coroutine.

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

| Topic | `dhanhq` | TradeSmart |
|---|---|---|
| Results and errors | Every REST call returns `{"status": "success" \| "failure", "remarks": …, "data": …}`; network exceptions are caught and also returned as `"failure"` | A call returns the result directly (a typed model or list) **or raises** a subclass of `TradeSmartError`. There is no status field to check. |
| Response data | `data` is the server's JSON, unchanged | Typed, frozen pydantic models (`Order`, `Position`, `Quote`, …) with snake_case attributes; `.raw` keeps the wire payload, `.model_dump()` gives a dict. |
| Instrument | `security_id` + `exchange_segment` (`"NSE_EQ"`) | `exchange` (`"NSE"`) + `tradingsymbol` (`"INFY-EQ"`) for orders; `exchange` + string `token` for quotes and candles. See [Symbols](#symbols). |
| Order calls | Return the status dict | Return the **order id** once TradeSmart's order system has accepted the request. That is an acknowledgement, not a fill: confirm with `orders()`, `order_history()` or the order stream. |
| Exchange constants | REST `dhan.NSE == "NSE_EQ"`, feed `MarketFeed.NSE == 1` | One `Exchange` enum everywhere (`Exchange.NSE == "NSE"`). |
| HTTP timeout | 60 s | `ClientConfig(connect_timeout=5.0, read_timeout=10.0)` (defaults). |
| Logging | Standard `logging` plus some `print` output | Logger `tradesmart` only; never prints; tokens and secrets are masked. |

Also new in TradeSmart:

- **Unknown outcome — reconcile, never resend.** If `place_order`, `modify_order`,
  `cancel_order` or `exit_order` times out or gets an unreadable answer *after* the request may
  have reached the server, the SDK raises `OrderStatusUnknownError`. The order may exist, so
  check `orders()` before you act again. (Code that treated every `"failure"` dict as "not
  placed, try again" must not be ported as-is.) State-changing calls are never retried.
- **Automatic retries for reads only** (network errors, timeouts, HTTP 429, 5xx; 2 retries by
  default, `ClientConfig(max_retries=0)` disables them).
- **Client-side rate limits** per client instance: `place_order` 10/s (configurable up to 30 with an exchange-approved algo ID: `order_rate_limit_per_second` and `algo_id`) and 200/min (`order_rate_limit_per_minute`); `quote` and
  `historical_data` 100/s and 1,000/min each. By default a call waits for a free slot;
  `ClientConfig(rate_limit_mode="raise")` raises `RateLimitError` instead.
- **Local validation.** Invalid combinations raise `RequestValidationError` before anything is
  sent — for example `trigger_price` on a `LIMIT` order (even `0`), or `price=0` on a `LIMIT`.
- **Positions are a flat list** of `Position` objects (`[]` when there are none).
- **Prices are `Decimal`** in every model; do not mix them with `float` arithmetic.
- **Enum fields hold wire codes.** `transaction_type`, `order_type`, `product` and `validity`
  are enum members whose values are TradeSmart's codes, so `trade.transaction_type == "BUY"` is
  **False** (the value is `"B"`); compare with `TransactionType.BUY`.
- **Static IP.** API calls must come from the static IP registered with TradeSmart.

## Login flow

| Step | `dhanhq` (`DhanLogin`) | TradeSmart |
|---|---|---|
| App credentials | `app_id`, `app_secret` | `app_key`, `secret_key`, your TradeSmart **Client ID** and the registered redirect URL |
| Start the login | `generate_login_session(app_id, app_secret)` → creates a consent, opens the browser, returns `consentAppId` | `client.login_url()` → URL string; open it yourself (nothing is opened for you) |
| Value on the redirect | `tokenId` | `code` (single use) |
| Exchange for a token | `consume_token_id(token_id, app_id, app_secret)` → dict | `client.generate_session(code)` → `AccessToken`, also stored on the client. Never retried; any failure raises `AuthenticationError` — restart the login. |
| Daily login for your own account | — | `python -m tradesmart login`, then `TradeSmart.from_env()` |
| PIN + TOTP login | `generate_token(pin, totp)` | — (OAuth only) |
| Renew a token | `renew_token(access_token)` | — (no refresh endpoint; log in again after expiry) |
| Profile | `user_profile(access_token)` | `client.profile()` → `UserDetails` |
| Static IP | `set_ip(...)`, `modify_ip(...)`, `get_ip(...)` | — (registered with TradeSmart outside the API) |

A TradeSmart token is valid until the **next 06:00 IST** (`AccessToken.expires_at`,
`is_expired()`); an expired `AccessToken` is refused locally before any request is sent. The
token exchange needs the secret key, so run it server-side.

## Client construction

| `dhanhq` | TradeSmart |
|---|---|
| `ctx = DhanContext(client_id, access_token)`; `dhan = dhanhq(ctx)` | `client = TradeSmart(client_id, access_token=access_token)` |
| — | `TradeSmart.from_env()` reads `TRADESMART_CLIENT_ID`, `TRADESMART_ACCESS_TOKEN`, `TRADESMART_APP_KEY`, `TRADESMART_SECRET_KEY`, else the login saved by `python -m tradesmart login` |
| `DhanContext(..., pool=...)` | `ClientConfig(max_connections=…, max_keepalive_connections=…, keepalive_expiry=…)` |
| `DhanContext(..., disable_ssl=...)` | Pass your own `httpx.Client` as `http_client=` |
| Streams take the context: `MarketFeed(ctx, ...)` | Streams take the credentials: `TradeSmartStream(client_id, access_token, ...)` |

Use `with TradeSmart(...) as client:` (or `client.close()`) to close the connection pool, and
`AsyncTradeSmart` for asyncio.

## Methods

| `dhanhq` | TradeSmart | Notes |
|---|---|---|
| `place_order(security_id, exchange_segment, transaction_type, quantity, order_type, product_type, price, trigger_price=0, disclosed_quantity=0, after_market_order=False, validity='DAY', amo_time='OPEN', bo_profit_value=None, bo_stop_loss_Value=None, tag=None, should_slice=False)` | `place_order(*, exchange, tradingsymbol, quantity, transaction_type, product, order_type, price=None, trigger_price=None, validity="DAY", disclosed_quantity=None, amo=False, tag=None, book_loss_price=None, book_profit_price=None, trail_price=None, market_protection=None, algo_id=None)` | Keyword-only. Returns the order id. |
| `place_slice_order(...)` | `place_sliced_order(*, <the place_order arguments>, max_slice_quantity=None, max_slices=25)` | Slices of the largest whole number of lots within the freeze quantity (`security_info().frzqty`), sent one after another. Returns a `SlicedOrderResult` (`order_ids`, `slice_quantities`, `complete`, `error`); stops at the first failed slice. Quantity in units on every exchange, MCX included, as in `dhanhq`. See the [Orders guide](../orders.md#sliced-orders). |
| `modify_order(order_id, order_type, leg_name, quantity, price, trigger_price, disclosed_quantity, validity)` | `modify_order(order_id, *, exchange, tradingsymbol, quantity, price, order_type, product, trigger_price=None, validity=None, tag=None, book_loss_price=None, book_profit_price=None, trail_price=None, algo_id=None)` | Also needs `exchange`, `tradingsymbol` and `product`; `price=None` for market types. No `leg_name` or `disclosed_quantity`. |
| `cancel_order(order_id)` | `cancel_order(order_id)` | Returns `order_id`. |
| — | `exit_order(order_id, product)` | Exit a cover (`CO`) or bracket (`BO`) order. |
| `place_forever(..., order_flag="SINGLE", ...)` | `place_gtt(*, exchange, tradingsymbol, alert_type, trigger_value, quantity, transaction_type, product, order_type, tag, price=None, trigger_price=None, validity="DAY", alert_validity="GTT", disclosed_quantity=None)` | Single GTTs only (`order_flag="OCO"` has no method yet). You choose the condition: `alert_type` `"LTP_A_O"` (LTP above) or `"LTP_B_O"` (LTP below), and others (% change, ATP, OI); `trigger_value` is the condition, `trigger_price` the stop-loss trigger of the order. `tag` is required. **A GTT whose condition is already true fires at once.** Products `CNC`, `NRML`, `CO`. Returns the alert id. See [GTT orders](../gtt.md). |
| `modify_forever(...)` | `modify_gtt(alert_id, *, <every place_gtt field>, validity, alert_validity)` | Every field is sent again; `validity` and `alert_validity` have no defaults. |
| `get_forever()` | `pending_gtts()`, `triggered_gtts()` | `list[Gtt]` / `list[TriggeredGtt]`; a triggered GTT's order is in `orders()` with `parent_id` = the alert id. |
| `cancel_forever(order_id)` | `cancel_gtt(alert_id)` | Only a GTT that has not triggered. |
| `get_order_list()` | `orders()` | `list[Order]`. |
| `get_order_by_id(order_id)` | `order_history(order_id)` | All state transitions of the order (`list[OrderHistoryEntry]`); or pick it from `orders()`. |
| `get_order_by_correlationID(correlation_id)` | filter `orders()` on `order.tag` | `tag` is TradeSmart's free-text order remark. |
| `get_trade_book(order_id=None)` | `trades()` | Filter on `trade.order_id` yourself. |
| `get_positions()` | `positions()` | Flat `list[Position]`. |
| `get_holdings()` | `holdings(product="CNC")` | `list[Holding]`. |
| `get_fund_limits()` | `margins()` | `Limits` (`cash`, `payin`, `marginused`, `collateral`, `unmtom`, …). |
| `margin_calculator(security_id, exchange_segment, transaction_type, quantity, product_type, price, trigger_price=0)` | `order_margin(*, exchange, tradingsymbol, quantity, price, product, transaction_type, order_type, trigger_price=None, book_loss_price=None, order_id=None, sno_number=None)` | Needs `order_type`; returns `OrderMargin` (`ordermargin`, `marginused`, `cash`, `remarks`). |
| `ticker_data(securities)` | `quote(exchange, token).last_price` | **One instrument per call**; for many, use the stream or `AsyncTradeSmart` concurrently. |
| `ohlc_data(securities)` | `quote(exchange, token)` → `open`, `high`, `low`, `close`, `last_price` | |
| `quote_data(securities)` | `quote(exchange, token)` | 5 depth levels, volume, OI, circuit limits. |
| `intraday_minute_data(security_id, exchange_segment, instrument_type, from_date, to_date, interval=1, oi=False)` | `historical_data(exchange, token, from_date=None, to_date=None, interval=None)` | `interval` in minutes from `1, 3, 5, 10, 15, 30, 60, 120, 240` (no 25). Candles come **newest first**. |
| `historical_daily_data(security_id, exchange_segment, instrument_type, from_date, to_date, expiry_code=0, oi=False)` | `daily_history(exchange, tradingsymbol, from_date, to_date)` | By trading symbol; no `expiry_code`. |
| `option_chain(under_security_id, under_exchange_segment, expiry)` | `option_chain(exchange, tradingsymbol, strike_price, count)` | Returns the contracts (`tradingsymbol`, `token`, `option_type`, `strike_price`, `lot_size`) `count` strikes either side of `strike_price`; no Greeks or IV. Quote contracts for prices, or use `option_chain_snapshot()` (quotes, PCR, max pain) and `option_greeks()` (IV and Greeks computed by the SDK). |
| `fetch_security_list(mode)` | `instruments(exchange)` / `download_instruments(exchange)` | Per exchange; `list[dict[str, str]]`, no pandas. |
| — | `search_instruments(text)`, `security_info(exchange, token)` | Lookup helpers. |

Dates: `dhanhq` takes `"YYYY-MM-DD"` strings. TradeSmart dates are a timezone-aware
`datetime`, a `date` (meaning 00:00 IST) or epoch seconds (`int`); date strings such as
`"2026-11-09"` are not accepted, and a naive `datetime` raises `RequestValidationError`.
`to_date=date.today()` ends at midnight and misses today's session; pass
`datetime.now(timezone.utc)`.

## Parameters

| `dhanhq` | TradeSmart | Notes |
|---|---|---|
| `security_id` | `tradingsymbol` (orders, margin, option chain, daily history) or `token` (quote, security info, intraday candles) | Different numbering: look it up, do not reuse. |
| `exchange_segment` | `exchange` | See constants. |
| `transaction_type`, `quantity`, `order_type`, `price`, `trigger_price`, `disclosed_quantity`, `validity` | same names | `price` is required (`> 0`) for `LIMIT` / `SL` and may be omitted for `MARKET` / `SL-M` (TradeSmart refuses `MARKET` API orders; see [Constants](#constants)). `trigger_price` only on `SL` / `SL-M` (pass nothing otherwise, not `0`). |
| `product_type` | `product` | |
| `after_market_order` | `amo` | |
| `amo_time` | — | |
| `bo_profit_value`, `bo_stop_loss_Value` | `book_profit_price`, `book_loss_price` (+ `trail_price`) | Bracket order: `product="BO"`; cover order: `product="CO"` with `book_loss_price`. TradeSmart passes these values through as given — check they mean the same thing for your strategy. |
| `tag` (sent as `correlationId`) | `tag` (sent as `remarks`) | Read back as `order.tag`. |
| `should_slice` | — | Call `place_sliced_order(...)` instead of `place_order(...)`. |
| `leg_name` (modify) | — | |
| `instrument_type`, `expiry_code`, `oi` (candles) | — | Not needed; open interest is in the candle when sent. |

## Constants

`tradesmart` has enums. Transaction type, order type, product and validity parameters also
accept the common word in any case (`"BUY"`, `"LIMIT"`, `"SL-M"`, `"MIS"`, …); exchanges take
the upper-case code (`"NSE"`). Several Dhan **values** are not accepted and must be changed.

| `dhanhq` | Value | TradeSmart | Accepted as-is? |
|---|---|---|---|
| `NSE` | `"NSE_EQ"` | `Exchange.NSE` / `"NSE"` | No |
| `BSE` | `"BSE_EQ"` | `Exchange.BSE` / `"BSE"` | No |
| `FNO`, `NSE_FNO` | `"NSE_FNO"` | `Exchange.NFO` / `"NFO"` | No |
| `BSE_FNO` | `"BSE_FNO"` | `Exchange.BFO` / `"BFO"` | No |
| `CUR` | `"NSE_CURRENCY"` | `Exchange.CDS` / `"CDS"` | No |
| `MCX` | `"MCX_COMM"` | `Exchange.MCX` / `"MCX"` | No |
| `INDEX` | `"IDX_I"` | — (no index segment) | No |
| `INX` | `"INX_EQ"` | — (no US stocks) | No |
| `BUY` / `SELL` | `"BUY"` / `"SELL"` | `TransactionType.BUY` / `.SELL` (wire `B` / `S`) | Yes |
| `LIMIT` / `MARKET` | `"LIMIT"` / `"MARKET"` | `OrderType.LIMIT` / `.MARKET` (wire `LMT` / `MKT`) | Yes (refused for API orders, see below) |
| `SL` | `"STOP_LOSS"` | `OrderType.SL` / `"SL"` (wire `SL-LMT`) | No |
| `SLM` | `"STOP_LOSS_MARKET"` | `OrderType.SL_M` / `"SL-M"` (wire `SL-MKT`) | No |
| `CNC` | `"CNC"` | `Product.CNC` (wire `C`) | Yes |
| `INTRA` | `"INTRADAY"` | `Product.MIS` / `"MIS"` (wire `I`) | No |
| `MARGIN` | `"MARGIN"` | closest: `Product.NRML` / `"NRML"` (wire `M`, carry-forward) | No |
| `CO` / `BO` | `"CO"` / `"BO"` | `Product.CO` / `.BO` (wire `H` / `B`) | Yes |
| `MTF` | `"MTF"` | `Product.MTF` (wire `F`; not on MCX) | Yes |
| `DAY` / `IOC` | `"DAY"` / `"IOC"` | `Validity.DAY` / `.IOC` (also `.EOS`) | Yes |

**Market orders:** TradeSmart refuses `MARKET` orders placed through the API (`OrderRejectedError`,
"ALGO_CHK: MKT Order type not allowed for API order"). Replace them with a `LIMIT` order at a price
that trades now (rounded with `round_to_tick()`); for stop-loss market (`SL-M`, not confirmed yet)
use `SL` with a limit price. See the Orders guide.

Which products your account may use is listed in `client.profile().prarr`. TradeSmart order
statuses: `OrderStatus.PENDING`, `OPEN`, `TRIGGER_PENDING`, `COMPLETE`, `CANCELED`, `REJECTED`.

## Errors

| `dhanhq` | TradeSmart |
|---|---|
| `resp["status"] == "success"` | The call returned normally |
| `resp["status"] == "failure"` with `remarks` `error_code` / `error_type` / `error_message` | An exception: `exc.emsg` (TradeSmart's message), `exc.http_status`, `exc.endpoint` on `APIError` subclasses |
| order rejected | `OrderRejectedError` |
| invalid or expired token | `AuthenticationError` |
| network failure (caught, returned as `"failure"`) | `NetworkError` / `RequestTimeoutError`; on order calls an ambiguous failure is `OrderStatusUnknownError` |
| — | `RequestValidationError` (rejected locally, nothing sent), `RateLimitError`, `ServerError`, `NotFoundError`, `ResponseParseError`; base class `TradeSmartError` |

## Response fields

`dhanhq` returns the server's JSON unchanged in `data`, so the keys your code reads come from
that API. The camelCase names in the left column are the ones the `dhanhq` library itself
uses in its request bodies; TradeSmart attributes are snake_case (wire names in brackets).

| Dhan request key | TradeSmart attribute |
|---|---|
| `orderId` | `order_id` (`norenordno`) |
| `securityId` | `token` (plus `tradingsymbol`) |
| `exchangeSegment` | `exchange` |
| `transactionType`, `orderType`, `productType`, `validity` | `transaction_type`, `order_type`, `product`, `validity` |
| `quantity`, `price`, `triggerPrice`, `disclosedQuantity` | `quantity`, `price`, `trigger_price`, `disclosed_quantity` |
| `correlationId` | `tag` |

| Data | Main TradeSmart attributes |
|---|---|
| Order (`orders()`, `order_history()`) | `order_id`, `status`, `exchange`, `tradingsymbol`, `transaction_type`, `order_type`, `product`, `validity`, `quantity`, `price`, `trigger_price`, `filled_quantity` (`fillshares`), `average_price` (`avgprc`), `reject_reason` (`rejreason`), `exchange_order_id`, `tag`, `norentm`, `exch_tm` |
| Trade (`trades()`) | `order_id`, `tradingsymbol`, `transaction_type`, `flid` (fill id), `flqty`, `flprc`, `fltm`, `average_price` |
| Position (`positions()`) | `exchange`, `tradingsymbol`, `token`, `product`, `net_quantity` (`netqty`), `netavgprc`, `last_price` (`lp`), `realized_pnl` (`rpnl`), `daybuyqty` / `daysellqty`, `daybuyavgprc` / `daysellavgprc`, `cfbuyqty` / `cfsellqty`, `mult` |
| Holding (`holdings()`) | `instruments` (list with `exchange`, `tradingsymbol`, `token`, `last_price`), `holdqty`, `dpqty`, `usedqty`, `btstqty`, `upldprc` (average price uploaded with the holding) |
| Funds (`margins()`) | `cash`, `payin`, `payout`, `marginused`, `collateral`, `span`, `expo`, `realized_pnl`, `unmtom` (unrealised MTM, account level) |
| Quote (`quote()`) | `last_price`, `open`, `high`, `low`, `close`, `volume`, `average_trade_price`, `open_interest`, `uc` / `lc`, `ltq`, `ltt`, `tbq` / `tsq`, depth `bp1..5` / `bq1..5` / `sp1..5` / `sq1..5` |
| Candle (`historical_data()`) | `time` (and `ssboe` epoch), `into`, `inth`, `intl`, `intc`, `intv`, `intoi` |

TradeSmart documents no unrealised P&L field per position; only `realized_pnl`.

## Streaming

| `dhanhq` | TradeSmart (`TradeSmartStream`, threads) | Notes |
|---|---|---|
| `MarketFeed(ctx, instruments, version='v2', on_connect=None, on_message=None, on_close=None, on_error=None, on_ticks=None)` | `TradeSmartStream(client_id, access_token, *, config=None, on_touchline=None, on_depth=None, on_order_update=None, on_position=None, on_connect=None, on_disconnect=None, on_error=None, on_message=None, on_reconnect=None)` | Subscriptions are sent after `start()`, not passed to the constructor. |
| instrument tuple `(MarketFeed.NSE, security_id, MarketFeed.Ticker)` | `"NSE\|22"` or `("NSE", "22")` | Exchange code + TradeSmart token. |
| request code `Ticker` (15) / `Quote` (17) | `stream.subscribe_touchline([...])` | |
| request code `Full` (21) | `stream.subscribe_depth([...])` | 5 depth levels. |
| `FullDepth` (20 / 200 levels) | — | |
| `start()` (thread) / `run()` / `run_forever()` + `get_data()` loop | `stream.start()` then `stream.run_forever()` | `start()` returns once connected and raises the connect error. |
| `subscribe_symbols(symbols)` / `unsubscribe_symbols(symbols)` | `subscribe_touchline` / `unsubscribe_touchline`, `subscribe_depth` / `unsubscribe_depth` | Max 1,000 instruments per connection (`SubscriptionLimitError`). |
| `close_connection()` | `stream.stop()` | |
| `on_message(feed, data)` / `on_ticks` | `on_touchline(msg)`, `on_depth(msg)`, or `on_message(msg)` for everything | Typed messages (`TouchlineFeed`, `DepthFeed`, …); feed messages carry only changed fields, `stream.snapshot("NSE\|22")` holds the merged latest values. |
| `on_connect(feed)` | `on_connect()` | After every (re)connect. |
| `on_close(feed)` | `on_disconnect()` | |
| `on_error(feed, exc)` | `on_error(exc)` | Also receives callback errors and terminal errors. |
| `OrderUpdate(ctx)`; `order_client.on_update = fn`; `connect_to_dhan_websocket_sync()` | `on_order_update=fn` on the same stream, then `stream.subscribe_orders()` | One connection for market data and orders. Connection errors are raised or passed to `on_error`, not printed. |
| — | `stream.subscribe_positions()` + `on_position` | Position updates. |

Reconnects are automatic with capped exponential backoff (`ClientConfig.reconnect_max_attempts`,
default 20; `None` = unlimited), and every subscription is restored after a reconnect. Updates
sent while the stream was down are not replayed: `on_reconnect()` (no arguments) runs after each
successful reconnect, so call `orders()` and `positions()` there. All
callbacks run one at a time on one dedicated thread, never on the socket reader. For asyncio
use `AsyncTradeSmartStream` with `async for msg in stream`.

Stream fields:

| `dhanhq` packet / update key | TradeSmart attribute |
|---|---|
| `security_id`, `exchange_segment` | `token`, `exchange` (and `key`, e.g. `"NSE\|22"`) |
| `LTP` (formatted string) | `last_price` (`Decimal`) |
| `LTQ`, `LTT` | `ltq`, `ltt` (depth messages) |
| `avg_price`, `volume` | `average_trade_price`, `volume` |
| `open`, `high`, `low`, `close` | same names |
| `OI` | `open_interest` |
| `total_buy_quantity`, `total_sell_quantity` | `tbq`, `tsq` (depth messages) |
| `depth` bid / ask levels | `bp1..5`, `bq1..5`, `bo1..5`, `sp1..5`, `sq1..5`, `so1..5` (depth messages) |
| order update `Data["orderNo"]`, `Data["status"]` | `OrderUpdate.order_id`, `OrderUpdate.status` |

## No equivalent in TradeSmart

- **OCO forever orders** (`order_flag="OCO"`): TradeSmart supports one-cancels-other GTTs, but
  their request fields are not documented yet, so the SDK has no method for them. Single forever
  orders map to `place_gtt` (see [Methods](#methods)).
- Super orders (`place_super_order`, `modify_super_order`, `cancel_super_order`,
  `get_super_order_list`): the nearest TradeSmart feature is a bracket order (`product="BO"`
  with `book_profit_price`, `book_loss_price`, optional `trail_price`).
- Conditional orders, `amo_time`.
- `exit_all_positions()`, `convert_position(...)`, `margin_calculator_multi(...)`.
- Kill switch and P&L-based exit (`kill_switch`, `set_pnl_exit`, …).
- eDIS / T-PIN (`generate_tpin`, `open_browser_for_tpin`, `edis_inquiry`).
- Statements: `get_trade_history(...)`, `ledger_report(...)`.
- `expiry_list(...)`, `expired_options_data(...)`; Greeks and IV in the option chain from the server (the SDK computes estimates instead: `option_greeks()`, `option_chain_snapshot()`).
- Global (US) stocks and their feed; 20/200-level full depth.
- PIN + TOTP login, token renewal, IP management through the API.

## Symbols

`dhanhq` identifies an instrument by a numeric `security_id` within an `exchange_segment`.
TradeSmart uses the exchange plus its own **trading symbol** (NSE equities carry the series,
e.g. `INFY-EQ`) for orders, and a numeric **token** passed as a string (e.g. `"22"`) for quotes,
intraday candles and the stream. The numbers are unrelated to Dhan's security ids.

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

Enter with a limit buy, then protect it with a stop-loss sell.

**Before** (`dhanhq`; `dhan` is your client, `security_id`, `entry` and `stop` come from your
strategy):

```python
resp = dhan.place_order(
    security_id=security_id,
    exchange_segment=dhan.NSE,
    transaction_type=dhan.BUY,
    quantity=1,
    order_type=dhan.LIMIT,
    product_type=dhan.INTRA,
    price=entry,
)
if resp["status"] == "success":
    dhan.place_order(
        security_id=security_id,
        exchange_segment=dhan.NSE,
        transaction_type=dhan.SELL,
        quantity=1,
        order_type=dhan.SL,
        product_type=dhan.INTRA,
        price=stop - 0.5,
        trigger_price=stop,
    )
else:
    print("entry failed:", resp["remarks"])
```

This snippet has a flaw worth fixing while you port it: it places the stop as soon as the entry
is *accepted*, not filled. If the entry never fills, the stop can trigger on its own and open
an unintended short position.

**After** (TradeSmart, *illustrative* — running it places real orders). The stop is placed only
once the entry is `COMPLETE`:

```python
import time
from decimal import Decimal

from tradesmart import (
    OrderRejectedError,
    OrderStatus,
    OrderStatusUnknownError,
    TradeSmart,
    TradeSmartError,
)

client = TradeSmart.from_env()
entry, stop = Decimal("1500.00"), Decimal("1480.00")  # from your strategy
FINAL = (OrderStatus.COMPLETE, OrderStatus.REJECTED, OrderStatus.CANCELED)


def final_status(order_id, timeout=30.0):
    """Poll the order's history until it reaches a final status; None if the wait ends first."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        seen = {step.status for step in client.order_history(order_id)}  # any order of steps
        for status in FINAL:
            if status in seen:
                return status
        time.sleep(1)
    return None


def enter_with_stop():
    try:
        entry_id = client.place_order(
            exchange="NSE",
            tradingsymbol="INFY-EQ",  # was security_id
            transaction_type="BUY",
            quantity=1,
            order_type="LIMIT",
            product="MIS",  # was dhan.INTRA ("INTRADAY")
            price=entry,
        )
    except OrderRejectedError as exc:
        print("entry rejected:", exc.emsg)
        return
    except OrderStatusUnknownError:
        print("entry outcome unknown: reconcile with client.orders(); do not resend")
        return
    except TradeSmartError as exc:  # anything else, e.g. not sent: report and stop, never resend
        print("entry NOT placed:", exc)
        return

    try:
        status = final_status(entry_id)
    except TradeSmartError as exc:  # e.g. a network blip while polling; never re-send the entry
        print("could not read the entry's status:", exc)
        print("the entry was NOT re-sent: check client.orders() before acting")
        return
    if status is None:  # still open after the wait: withdraw it
        try:
            client.cancel_order(entry_id)
        except TradeSmartError as exc:
            print(
                "cancel failed — the entry may have filled; check client.orders() "
                "and protect the position manually:",
                exc,
            )
            return
        print("entry not filled in time; cancel requested (confirm with client.orders())")
        return
    if status != OrderStatus.COMPLETE:  # rejected or cancelled: no stop
        print("entry not filled:", status)
        return

    try:
        client.place_order(
            exchange="NSE",
            tradingsymbol="INFY-EQ",
            transaction_type="SELL",
            quantity=1,
            order_type="SL",  # was dhan.SL ("STOP_LOSS")
            product="MIS",
            price=stop - Decimal("0.5"),
            trigger_price=stop,
        )
    except OrderStatusUnknownError:
        print("stop outcome unknown: check client.orders() before placing another")
    except TradeSmartError as exc:
        print("stop NOT placed, position is unprotected:", exc)


enter_with_stop()
```

What changed: the security id became a trading symbol; `INTRADAY` and `STOP_LOSS` became `MIS`
and `SL`; `product_type` became `product`; prices are `Decimal`; status checks became `try`
blocks; and the stop waits for the entry to fill, because an order id only means "accepted".
Instead of polling you can react to `on_order_update` after `stream.subscribe_orders()`. Where
your account offers cover orders, a single order with `product="CO"` and `book_loss_price`
carries its own stop.

---

Dhan and DhanHQ are trademarks of their respective owners and are used here only to identify
those products.
