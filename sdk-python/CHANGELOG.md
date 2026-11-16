# Changelog

All notable changes to the `tradesmart` Python SDK are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [2.0.0] — 2026-11-15

**The first public release**, on PyPI: `pip install tradesmart`. Version numbers 0.1.0
and 0.2.0 were internal previews that were never published, so there is nothing to migrate
from: 2.0.0 contains everything listed under them below plus the changes in this section, which
are described relative to the 0.2.0 preview. Default `User-Agent`: `tradesmart-python/2.0.0`.

### Changed — licence

- **The SDK is licensed under the Apache License 2.0** (previously MIT, never published), copyright
  TradeSmart Fintech Securities Limited. The package ships `LICENSE` and a `NOTICE` file; the
  metadata is `License-Expression: Apache-2.0`. The licence grants no rights to the TradeSmart name.

### Changed — order rate limit

- The client-side order rate limit is now **10 orders per second** by default (was 30), and a
  new **per-minute window of 200 orders** (`order_rate_limit_per_minute`, integer 1 to 600, default
  200; was a fixed 600) follows what production enforces: on 14 Nov 2026 it refused an order
  once 200 had been received in the current minute ("Order Recieved 202 in a current minute
  exceeds Limit 200 for user"). Once 200 orders are in the minute, `place_order` raises
  `RateLimitError` before anything is sent, unless the oldest order leaves the window within
  `order_rate_limit_max_wait` (default 1 second; then it waits and goes out). Open item F33: 200 or
  600, and whether modify and cancel count. `place_sliced_order` checks the minute window up
  front: more slices than `order_rate_limit_per_minute` raise `ConfigurationError`, and too little room in the
  current minute raises `RateLimitError` (with `retry_after`), both before any slice is sent.
  The message names the window that refused, for example `client-side rate limit for place_order
  (200 per minute)`. Most TradeSmart clients are limited to 10 per second; a higher rate needs an
  exchange-approved strategy and its algo ID, which TradeSmart checks.
  New `ClientConfig.order_rate_limit_per_second` (integer 1 to 30) sets it for an approved
  account together with `ClientConfig.algo_id`, which is sent with every order (see "Added").
- **What this changes for you:** the default order rate is now **10 per second, down from 30**.
  A burst of 11 to 30 orders within one second, which went straight out before, now waits for
  the next second (at most `order_rate_limit_max_wait`, 1 second by default) or, with
  `rate_limit_mode="raise"`, gets `RateLimitError` for the 11th order and nothing is sent. If
  your account has an exchange-approved strategy with an algo ID (the only way TradeSmart
  allows more), set `ClientConfig(order_rate_limit_per_second=..., algo_id=...)` up to 30 (a rate
  above 10 without `algo_id` raises `ConfigurationError`). The per-minute window is new too:
  more than 200 orders in a minute now get `RateLimitError` (nothing sent), where up to 600 went
  out before; raise `order_rate_limit_per_minute` only if TradeSmart confirms a higher limit for
  your account. Sliced orders are paced the same way.

### Changed — MCX order quantity

- **MCX `quantity` is in trade units, a whole multiple of the lot size, exactly like NFO and
  BFO** (not in lots, as an earlier internal build of `place_sliced_order` assumed). The SDK sends
  `quantity` as given on every exchange: two lots of CRUDEOIL (lot 100) are `quantity=200`, one
  lot of GOLD (lot 1) is `quantity=1`; TradeSmart rejects a quantity that is not a multiple of
  the lot size ("Quantity 1 is not a multiple of lot size 100.00"). `place_sliced_order()` now
  sizes MCX slices in units like every other exchange (CRUDEOIL freeze quantity 2500: slices of
  2500, so 6000 goes out as 2500, 2500, 1000) and refuses an MCX quantity or
  `max_slice_quantity` that is not a whole number of lots. The user guide explains the price
  factor `security_info().prcftr` (value = price × quantity × `prcftr`; GOLD 100).

### Changed — supported Python versions

- **Python 3.11 or newer is required** (`requires-python = ">=3.11"`); 3.11, 3.12, 3.13, 3.14 and
  3.15 are tested. Python 3.9 and 3.10 no longer receive security fixes from the Python project
  and are no longer supported. The SDK enums now derive from the standard `enum.StrEnum` (their
  text, formatting, lookups, pickling and hashing are unchanged; `isinstance(member, StrEnum)` is
  now true). The public type aliases (`TimeInput`, `PriceInput`, `InstrumentKey`, `Instruments`)
  are written as `X | Y` unions, so `typing.get_origin()` returns `types.UnionType` for them on
  Python 3.11-3.13.
- **Dependencies:** `websockets>=13,<18` (was `<16`, which held Python 3.14 and 3.15 on a
  websockets release without support for them); pydantic needs at least 2.8 on Python 3.13, 2.12
  on 3.14 and 2.14 on 3.15 (the first releases with wheels there).

### Fixed — order safety

- **`request()` endpoint names are normalised** before the safety guards: surrounding whitespace
  and `/` are stripped, anything that is not ASCII letters raises `ConfigurationError` (nothing
  sent), and known names match case-insensitively and are sent in their documented spelling.
  Before, `"/PlaceOrder"`, `"placeorder"` or `" PlaceOrder "` bypassed the never-retry rule and
  the order rate limit (a timed-out order could be sent three times).
- **HTTP 5xx with a JSON `stat: Not_Ok` on a state-changing call** is now `OrderRejectedError`
  (with `http_status`), not `ServerError`. Non-JSON or ambiguous bodies are unchanged
  (`OrderStatusUnknownError`).
- **Orders wait at most `connect_timeout` for a pooled connection** (`pool=connect_timeout`), so an
  order never queues for the full read timeout; a pool timeout stays definitive (nothing sent).
- **New `ClientConfig.order_rate_limit_max_wait`** (default `1.0` s, `>= 0`): in `"wait"` mode an
  order never waits longer than this for the client-side rate limit; a longer wait raises
  `RateLimitError` (with `retry_after`) and nothing is sent. Reads are unchanged.

### Fixed — parsing

- **One odd field no longer drops an order update or the order book.** Every free-text / id field
  on response and stream models accepts JSON numbers (`"tm": 1731234567` → `"1731234567"`); before,
  such a value turned an `om` frame into `UnknownMessage` (so `on_order_update` never fired) and
  made `orders()` raise for the whole book.
- `Order`, `OrderHistoryEntry`, `Trade`, `Position`, `Holding`, `OrderUpdate`, `PositionAck` and
  `PositionUpdate` set a field that still fails validation to `None`, keep the original value in
  `.raw` and log one WARNING naming the field (never its value). A record that still fails is
  handled as before.
- The lenient fallback never nulls identity fields (`lenient_protected_keys`: `norenordno`,
  `status`): a record whose order id or status cannot be read fails as before
  (`ResponseParseError` on REST, `UnknownMessage` on the stream).
- Every remaining `ResponseParseError.body` (list parsers, `SearchScrip` / `GetOptionChain`,
  `EODChartData`) and the script-master status error `raw` are redacted like all other errors.
- **The ATO "price not set" value (`42949672.95`) becomes `None` only on quote, depth and
  order-update price fields** (`lp`, `o`, `h`, `l`, `c`, `ap`, `bp1`–`bp5`, `sp1`–`sp5`, `lc`,
  `uc`, `52h`, `52l`; `prc`, `trgprc`, `avgprc`, `flprc`). Money amounts and other numbers
  (`rpnl`, `daybuyamt`, `daysellamt`, position averages, `ti`, `cp`, `blprc`, `bpprc`,
  `trailprc`) now keep the value as sent instead of turning into `None`.
- The "unrecognised timestamp format" WARNING records at most 50 distinct shapes per process
  (one further line announces the cap); later new shapes are not logged.

### Fixed — streams and script master

- `AsyncTradeSmartStream.connect()` on a stream that was already connected (or closed) raises
  `TradeSmartError` instead of `RuntimeError` (same message).
- **The exchange is accepted in any case everywhere** (`"nse"`, `"Nse"`, `Exchange.NSE`) and the
  upper-case code is always sent: orders and margins used to reject `"nse"`, quotes, security
  info, option chain and history sent it unchanged, and stream subscriptions rejected it. An
  unknown exchange raises `RequestValidationError` in every request builder.
- Script-master download failures while decoding the body (`httpx.DecodingError`) raise
  `NetworkError` like other network failures instead of a raw httpx exception.
- `instruments.load()` reads files with a UTF-8 byte-order mark correctly (the first header no
  longer starts with `﻿`).
- `subscribe_*` / `unsubscribe_*` (sync and async) accept a single instrument: `"NSE|22"` or
  `("NSE", "22")` (a bare string used to be iterated character by character).
- **Script-master freshness follows TradeSmart's daily refresh.** New
  `ClientConfig.script_master_refresh_time` (`"07:00"` IST, `HH:MM`; observed, pending
  confirmation): a cached file is fresh only if written at or after the most recent refresh
  instant. Before, a file downloaded between 00:00 and the refresh was kept all day. No extra
  request is made; `force=True` is unchanged.
- After a download the script-master file's modification time is set to the response's
  `Last-Modified` (when present and parseable), so a download between the refresh time and the
  server's actual regeneration (observed 07:00:24 IST) counts as stale.
- **A script master the server has not regenerated is no longer downloaded on every call.** When
  the cached file is older than the last refresh (weekend, holiday, late refresh), the SDK
  re-checks it with a conditional request (`If-Modified-Since`) at most every
  `ClientConfig.script_master_recheck_minutes` (new, default `15`, `>= 1`), recording each check
  in `<EXCH>_symbols.txt.checked`; `304 Not Modified` keeps the file, `200` replaces it. Before,
  three calls at 10:00 IST against a file stamped 06:55 IST made three full downloads.
- **A failed script-master re-check keeps the cached file.** When the conditional re-check
  (not a first or `force=True` download) fails with a network error, a timeout or an HTTP 5xx
  while the file exists, `download_instruments()` / `instruments()` return the cached file and
  log one WARNING per failed re-check, not per call ("using the cached script master; re-check
  failed: <ErrorType>"), sync and async. During an outage the server is asked again at most
  once per `script_master_recheck_minutes`. A missing file, a forced download and HTTP 4xx still
  raise; a `304` answer for a cached file deleted in the meantime triggers one full download.
- `snapshot(key)` on both streams normalises the key like a subscription: `snapshot("nse|22")`
  finds `"NSE|22"` (an invalid key raises `RequestValidationError`); a non-`str` key (e.g. an
  `("NSE", "22")` tuple or a one-item list) is normalised the same way instead of raising
  `TypeError`.
- `close()` no longer waits on a reconnect loop: each reconnect attempt first checks whether the
  stream is closing and stops.
- An `AccessToken` passed to a stream is checked for the 06:00 IST expiry before connecting
  (`AuthenticationError` naming `python -m tradesmart login`, nothing sent), unless
  `ClientConfig.check_token_expiry` is False. A plain `str` token is still checked only by the
  server.
- **A backlog merge no longer puts a newer tick in front of an older ack for the same
  instrument.** While a consumer lags, a `tf`/`df` is merged only into one of its instrument's
  two newest waiting messages: the newest if it is a feed of the same type, or the second newest
  if that is a feed of the same type and the newest a feed of the other type (which then takes
  the new values of the fields it can carry, e.g. `v`, `lp`, `bp1`). Otherwise it is queued, so
  nothing merges across an ack (`tk`/`dk`, e.g. the snapshots re-sent after a reconnect). Each
  field's values are delivered in arrival order, applying the messages in order ends with the
  latest values, and an instrument with both touchline and depth holds about two waiting
  entries. Before, the consumer could end with the ack's older price.
- **An `async def` `on_state_change` on `AsyncTradeSmartStream` now runs** (as its own task; its
  exceptions are logged). Before, the coroutine was discarded. `close()` gives the ones still
  running up to 2 s, then cancels them (one WARNING); called from such a callback (or a task it
  started or returned), `close()` returns at once and the same bounded wait and cancel run in
  the background. `TradeSmartStream` raises `TypeError` at construction for an `async def`
  callback (any of them); a callback that returns an awaitable anyway (e.g.
  `lambda m: handler(m)`) has it closed and is reported once per callback (logged and passed to
  `on_error` as a `TypeError`) instead of the coroutine being dropped silently.
- **`close()` / `stop()` return within about 2 s when the server ignores the close frame**
  (`close_timeout=2`; the socket is then aborted). Before, closing waited 10 s and `stop()`
  logged "stream thread did not stop within 5.0s".
- **An expired `AccessToken` ends the stream before a reconnect attempt** with the terminal
  `AuthenticationError` (as at connect) instead of retrying until the attempts run out, unless
  `ClientConfig.check_token_expiry` is False.
- `subscribe_*` / `unsubscribe_*` with a non-iterable argument (`22`, `None`) raise
  `RequestValidationError` instead of a raw `TypeError` (sync and async).
- New read-only `heartbeat_acks` on `TradeSmartStream` and `AsyncTradeSmartStream`: heartbeat
  replies (`hk`) received so far. A live read-only test holds an orders-only stream for 150 s and
  checks there is no reconnect and at least one `hk`.

### Fixed — clients

- **A closed client raises `ConfigurationError("client is closed; create a new client")`** from
  every call on `TradeSmart` / `AsyncTradeSmart` after `close()` / `aclose()` (or leaving `with`),
  also when you injected an `httpx` client that you closed yourself. Nothing is sent, so the
  error is definitive and never retried (an order call logs `not_sent`). Before, httpx's
  `RuntimeError` escaped.
- **`TRADESMART_ACCESS_TOKEN` is used only together with `TRADESMART_CLIENT_ID`** in
  `from_env()`: a token from the environment is never paired with the saved login's client id;
  `from_env()` raises `ConfigurationError` saying to set both (or to unset the token and use the
  saved login).
- **`holdings(product=...)` resolves a string through `Product`** like `place_order`: `"CNC"`,
  `"cnc"` and `"C"` all send `prd=C`. Before, a string was sent unchanged (`holdings("CNC")` sent
  `prd=CNC`). An unknown product now raises `RequestValidationError` (field `prd`) and nothing is
  sent.
- **Option analytics accept `Decimal` and `int`** for every number: `black76`, `black_scholes`,
  `implied_volatility`, `greeks_from_price` and `option_greeks(rate=..., dividend_yield=...)`.
  `option_greeks` checks `rate` and `dividend_yield` first: a non-finite or non-numeric value
  raises `ValueError` naming the parameter, and no quote is requested.
- **A script-master file that is not valid UTF-8** raises `ResponseParseError` (saying to delete
  the file and download again) from `instruments.load()` / `instruments()` / `option_greeks()`,
  instead of a raw `UnicodeDecodeError`.
- **`status="price_at_or_below_intrinsic"` is documented to also cover a price above what any
  volatility up to 500 % gives** (typically a stale last price on expiry day; `iv=None`), and the
  `buildup()` docstring states the two windows (price change since the previous close, OI change
  since today's open).
- **Packaging:** removed the `License :: OSI Approved :: MIT License` classifier; the licence is
  the PEP 639 licence expression only (a classifier next to it is rejected by newer tools).
- **Login command:** `--token-file` is resolved to an absolute path, so the printed
  `TRADESMART_TOKEN_FILE` and `logout` hints work from any directory; the redirect server handles
  each connection in its own thread, so an idle browser pre-connect no longer delays the real
  redirect (the first code wins; while the login command is still waiting, a later one gets a "login already received" page);
  `--listen-host` accepts IPv4 addresses only (an IPv6 address is a usage error, exit 2).
- **Token file:** `issued_at` / `expires_at` are written as `YYYY-MM-DDTHH:MM:SS+HH:MM` (seconds
  precision). Reading accepts exactly that form, optionally with 1-9 fraction digits and with `Z`
  instead of the offset, on every Python version; other forms (`+0530`, the basic format, a space
  separator, no seconds, 10 or more fraction digits) refuse the file on every Python version.
- `TRADESMART_TOKEN_FILE` is stripped of surrounding whitespace, and a blank value counts as unset
  (the default token file is used), like the other `TRADESMART_*` variables of `from_env()`.
- **Live order tests** are excluded from every plain `pytest` run (`-m "not live_orders"` in
  `addopts`); they run only with an explicit `-m live_orders` and the separate opt-in variables.
  The compose `login` service now has the same `build:` as `dev`.
- **Any exception raised while sending is mapped.** An exception that is not an httpx
  `RequestError` (for example `HTTPStatusError` from an injected client's `raise_for_status`
  response hook, or `RuntimeError` from a custom transport) becomes `OrderStatusUnknownError`
  ("`<Type>` during the request; check orders() before retrying", logged `unknown`) on
  state-changing calls and `NetworkError` ("`<Endpoint>`: unexpected error (`<Type>`)") on reads.
  On a read, a `TradeSmartError` raised by your own hook or transport is passed through
  unchanged; neither is retried (such an error is deterministic). Before, the raw exception
  escaped, so an accepted order could miss `except TradeSmartError`.
- A list item that is not a JSON object (`OrderBook` `["x"]`, `SearchScrip` `values: [1]`, ...)
  raises `ResponseParseError` ("Could not parse `<Endpoint>` response (expected a JSON object)")
  instead of a raw `ValueError` / `TypeError`.
- SDK errors raised for transport failures and non-JSON bodies no longer chain the httpx error or
  the `JSONDecodeError` (`from None`), so no exception chain holds the bearer header or the
  unredacted body.
- Nested response models keep `.raw` (for example `Holding.instruments[0].raw`,
  `UserDetails.prarr[0].raw`); values under secret keys such as `access_token` are masked there.
- The order outcome log has a fourth word, **`not_sent`**, for order calls that never reached the
  server: a missing or expired token, a client-side rate-limit refusal, a closed client and
  connect-phase failures (`ConnectError`, `ConnectTimeout`, `PoolTimeout`). These were logged as
  `rejected` (a closed client logged nothing). Invalid parameters still log no line.
- `ClientConfig` rejects NaN and infinity: every "positive" setting must be a finite positive
  number and `order_rate_limit_max_wait` a finite number `>= 0`. A `Retry-After` of `nan` / `inf`
  is ignored (the backoff schedule applies).
- A `Not_Ok` answer without a usable `emsg` reads "`<Endpoint>` failed (no error message from the
  server)" instead of "... failed: None". `generate_session()` errors read
  "GenAcsTok failed: `<emsg>`" or "GenAcsTok failed (no error message from the server)", never
  a doubled prefix.
- `from_env()` strips every `TRADESMART_*` value and treats a blank one as unset, so
  `TRADESMART_CLIENT_ID="ABC123 "` or `TRADESMART_ACCESS_TOKEN="tok\n"` work.
- `request(..., state_changing=True, expect="list")` that gets a `stat: Ok` object raises
  `OrderStatusUnknownError` (the request may have been executed), not `ResponseParseError`.
- **Redirects are never followed**, also on an injected `httpx` client created with
  `follow_redirects=True`: a 3xx on an order is `OrderStatusUnknownError` and on a read keeps its
  usual classification (`ResponseParseError` for a non-JSON body); nothing is re-sent to the
  redirect target. The script-master download does not follow redirects either (a 3xx is a
  `ResponseParseError` with the real HTTP status and the message "script master download was
  redirected (HTTP 302); redirects are never followed").
- A sync order call interrupted by `KeyboardInterrupt` or `SystemExit` after sending started now
  logs the `unknown` outcome line and WARNING, as async cancellation does, and re-raises the
  exception unchanged (nothing new is logged when it lands before sending).
- The saved login is refused (with a "check this computer's clock; log in again" message) when its
  `issued_at` lies more than 5 minutes in the future, so an edited file or a wrong clock at login
  can no longer keep a token valid past 06:00 IST.
- A relative `TRADESMART_TOKEN_FILE` is resolved against the current folder, so `login`,
  `token-path` and `logout` print an absolute path and `from_env()` reads the same file.
- The source distribution ships only the SDK's own files: no repository-wide ignore file and no
  documentation READMEs picked up by an unanchored `README.md` pattern.
- **Offline broker** (`tradesmart.testing`, new in this release) now follows the documented
  API more closely: `historical_data()` and `daily_history()` candles come back newest first and
  only within the requested range; `drills.server_error()` sends a non-JSON gateway page, so on
  an order it is `OrderStatusUnknownError` (`server_error(json=True)` keeps the JSON `Not_Ok`,
  `OrderRejectedError`); `exit_order()` works only on cover and bracket orders and answers with
  the documented `dmsg`. A client or stream keeps its broker alive (a helper may return only
  `broker.client()`). The synthetic login lasts 24 hours instead of ending at 06:00 IST.
  `set_quote()`, `tick()` and `set_candles()` reject unknown field names and values that are not
  numbers with `ValueError`.
- `option_greeks()`: the carry that brings a future of another expiry to the option's expiry
  (NFO, BFO, CDS, BCD; never MCX) now uses `rate − dividend_yield`:
  `forward = F · e^(−(rate − dividend_yield)·(Tf − To))`. Pass the index's dividend yield, or
  for a currency option the foreign interest rate, as `dividend_yield`; with the default `0`
  the results are unchanged. Before, weekly currency puts could show no IV and index calls and
  puts a spurious skew.
- `option_greeks()` checks `option_price`, `underlying_price` and `spot_price` before any
  request: `True` or an integer too large for a float raises `ValueError` naming the parameter
  (before: accepted as `1.0`, or a raw `OverflowError`).
- A damaged script master (a NUL byte, or a field over the csv size limit after a stray quote)
  raises `ResponseParseError` from `instruments()`, `instruments.load()` and `option_greeks()`
  on every Python version, instead of a raw `_csv.Error` or silently parsed rows (depending on
  the Python version).
- `download_instruments(exchange, dest_dir)` expands `~` in `dest_dir`, like
  `ClientConfig.instruments_dir` (before: a literal `./~` folder).
- A script master removed between its download and its parse (a cleanup job, another process)
  is downloaded once more by `instruments()` and `option_greeks()`; removed again, it raises
  `ResponseParseError` instead of a raw `FileNotFoundError`.
- `tradesmart.greeks`: an underlying or volatility so small that `σ√t` or `U·σ√t` underflows to
  zero gives `gamma=None` instead of a raw `ZeroDivisionError`.
- `examples/place_order.py` tags every order (`--tag`, else a generated `po-...` tag) and, after
  `OrderStatusUnknownError`, looks the order up by that tag instead of printing the whole order
  book; it never sends the order a second time.
- `examples/auth_server_flow.py` refuses, saving nothing, a token whose `uid` or `actid` is not
  `TRADESMART_CLIENT_ID` (TradeSmart's OAuth has no `state`). Its callback server serves each
  connection in its own thread with a 10 s timeout, so an idle browser pre-connect cannot block
  the login; it answers 404 outside `/callback` and keeps only the first code. Its server also
  skips the reverse-DNS lookup of the host when it starts (as the login command does), which
  could delay it by several seconds on macOS.
- Docs: `SECURITY.md` gives the leaked-key procedure (remove the app on the API Dashboard, create
  a new one, update your environment variables or `.env`, log in again); *Your first order in 10
  minutes* builds one `first_order.py` (the second "whole script" is gone; the fuller version is
  `examples/order_lifecycle.py`) and links the Docker and no-browser login details, now in
  Authentication, which also says what the Docker login prints; the README states that MCX API
  orders are not yet enabled and when an expired saved login makes `from_env()` raise; the API
  access page says IPv4 for now.
- **A request that cannot be built is "not sent", not "status unknown".** The body and headers
  are encoded before the request is marked as sent. A payload value that is not valid UTF-8 (a
  lone surrogate) raises `RequestValidationError` naming the field; a header that cannot be sent
  (a non-ASCII or control character in the token, `user_agent` or the injected `http_client`'s
  headers) raises `ConfigurationError`. Both log `not_sent` for an order and are never retried.
  These used to give `OrderStatusUnknownError` on orders and retried `NetworkError` on reads.
- **The access token is checked when it is set** (constructor, `set_access_token()`,
  `from_env()`): a `str` token is stripped of surrounding whitespace, so a token read from a file
  with its newline works; a token with a space, control character or non-ASCII character raises
  `ConfigurationError` (the value is never shown). A blank token counts as no token:
  `AuthenticationError`, nothing is sent (it used to send `Authorization: Bearer `).
- `ClientConfig.user_agent` must be printable ASCII (non-ASCII text used to fail every request).
- `ClientConfig` raises `ConfigurationError` for wrongly typed values instead of a raw
  `TypeError`/`AttributeError`: `max_retries`, `stream_backlog_limit`, `reconnect_max_attempts`
  (or `None`), `max_connections`, `max_keepalive_connections` and `script_master_recheck_minutes`
  must be integers (a float, NaN or bool is rejected); the timeouts and other number settings
  must be numbers (not text, `None` or a bool); the URLs, `user_agent` and
  `script_master_refresh_time` must be strings; `log_payloads` and `check_token_expiry` bools.
- A reply nested too deeply for the JSON parser (`RecursionError`) no longer escapes as a raw
  Python error: it is `OrderStatusUnknownError` on an order call (logged `unknown`, also on the
  async client) and `ResponseParseError` on a read; a deep `Not_Ok` stays a rejection.
- Errors from transport failures and non-JSON bodies are raised outside the handler, so their
  `__context__` is empty too (it held the httpx error with the bearer header, or the
  `JSONDecodeError` with the unredacted body).
- A client-side `RateLimitError` names the endpoint (`endpoint="PlaceOrder"`, `"GetQuotes"`,
  `"TPSeries"`) like every other API error; the rate group stays in the message.
- `historical_data()`, `daily_history()` and `to_epoch()` accept only an aware `datetime`, a
  `date` or an `int` epoch; text, a float or a bool raises `RequestValidationError` (field `time`)
  instead of a raw `ValueError` or a silently truncated value. `order_history()`, `quote()`,
  `security_info()`, `search_instruments()` and `daily_history()` require `str` ids and text
  (`RequestValidationError`; an `int` used to go out as a JSON number).
- `TradeSmartStream.stop()` ends delivery at once: no further message reaches the callbacks,
  also the ones still waiting (their count is logged at DEBUG), matching
  `AsyncTradeSmartStream.close()`. Before, every buffered message was still passed to the
  callbacks after `stop()`. `on_disconnect` still fires, and a terminal error still delivers the
  messages received before it to the callbacks, then `on_error`.
- A `CancelledError` raised inside an `AsyncTradeSmartStream` `on_message` (e.g. it awaited a
  task cancelled elsewhere) is logged like other callback errors and delivery continues. Before,
  it silently ended delivery while the stream stayed `CONNECTED`. Cancelling the stream itself
  (`close()`, the event loop shutting down) still stops the callback.
- `snapshot()` with a list or tuple of several instruments (or none) raises
  `RequestValidationError` instead of a raw `ValueError`.
- A subscribe or unsubscribe made while the stream re-sends its subscriptions after a reconnect is
  sent once (before, it could be sent twice, giving a duplicate acknowledgement).
- `examples/stream_sync.py` exits with code 1 when the stream reported an error (e.g. reconnects
  exhausted), not 0.

### Fixed — BFO option contracts

- **`option_greeks()` works for BFO options such as SENSEX.** The BFO script master names its
  strike column `Strike` (NFO and MCX: `StrikePrice`) and files an index's options and futures
  under different `Symbol`s (SENSEX: options `BSXOPT`, futures `BSXFUT`), so every BFO option
  failed with "no underlying future found for symbol BSXOPT". The strike is now read from
  `StrikePrice`, else `Strike`; the underlying future is looked up by the option's own `Symbol`,
  and only when none is usable and that `Symbol` ends in `OPT`, by the same stem plus `FUT`
  (never another family: an `SX50OPT` option never takes a `BSXFUT` future). NFO and MCX
  selection is unchanged.
- The offline broker's (`tradesmart.testing`) BFO script master now names the strike column
  `Strike`, like the real file; its NFO and MCX files keep `StrikePrice`.

### Added

- **GTT (good till triggered) orders** on `TradeSmart` and `AsyncTradeSmart` (TradeSmart's GTT
  endpoints, documented 2026-11-12): `place_gtt()`, `modify_gtt()`, `cancel_gtt()`,
  `pending_gtts()`, `triggered_gtts()` and `gtt_alert_types()`; new models `Gtt` and
  `TriggeredGtt`, enums `GttAlertType` (the 13 documented alert codes, e.g. `LTP_A_O`) and
  `GttValidity` (`GTT`, `DAY`), and `Order.parent_id` (`add_ord_id`: the GTT that placed an
  order). `tag` is required on a GTT (the key to find it again); `modify_gtt` sends every field
  again and has **no defaults for `validity` and `alert_validity`**. Products CNC, NRML and CO;
  exchanges NSE, NFO, BSE, BFO and MCX. **A GTT whose condition is already true when it is placed
  fires at once**; alert validity `GTT` lasts at most 365 days or the contract's expiry,
  whichever is earlier. The GTT writes are never retried and succeed only on their documented
  `stat` word (`Oi created`, `Oi replaced`, `Oi delete success`, each with an alert id),
  compared **ignoring letter case only** (TradeSmart's test environment answers `OI created`;
  spacing and other words are not relaxed, so its cancel answer `OI deleted` is unknown);
  anything else is `OrderStatusUnknownError`, and the WARNING says to check
  `pending_gtts()`. TradeSmart's test environment also lists cancelled GTTs in
  `triggered_gtts()` with `stat` `CANCELLED`. Their order log lines carry `al_id=`. The offline broker keeps GTTs too and fires
  one on `broker.trigger_gtt(alert_id)`. New guide page `docs/gtt.md` and example
  `examples/gtt_alert.py` (offline broker). OCO (one-cancels-other) GTTs are supported by
  TradeSmart but not documented yet, so they have no method.
- **Iceberg order safety rules** (the SDK does not offer iceberg methods):
  `PlaceIceBergOrder`, `ModifyIceBergOrder` and `CancelIceBergOrder` sent through `request()`
  are never retried, succeed only on `stat` `true` (`false` is `OrderRejectedError`) and any
  other answer is `OrderStatusUnknownError`.
- **The login command saves the App Key and Secret Key per Client ID.** After a successful
  `python -m tradesmart login`, the keys typed at the prompt (or given with `--app-key`) are saved
  in `credentials.json` beside the token file, one entry per Client ID, so the next day's login
  asks only for the browser step. Keys from `TRADESMART_APP_KEY` / `TRADESMART_SECRET_KEY` are
  never saved, and they still win over the saved ones (then the saved entry, then the prompt).
  The file is owner-only (mode 0600, atomic replace) but **not encrypted**. New options:
  `login --no-save-keys`, `logout --forget-keys` (all Client IDs) and
  `logout --forget-keys --client-id ID`; plain `logout` still removes only the token. A login
  that fails with saved keys says so and prints the command to forget them. The keys are never
  printed or logged; a damaged or unknown `credentials.json` stops the login with a message that
  names the file but not its content. Guide: `docs/authentication.md`.

- **`algo_id`** (TradeSmart `PlaceOrder` / `ModifyOrder`: "Required when placing more than 10
  orders per second"). New `ClientConfig.algo_id` (default `None`; a non-blank string) is sent as
  `algo_id` on every `place_order`, `modify_order` and `place_sliced_order` slice; the new per-call
  `algo_id=` keyword of those three methods (both clients) overrides it for that call. It is sent
  after the other order fields and on no other endpoint; with neither set nothing changes on the
  wire. `order_rate_limit_per_second` above 10 now requires `ClientConfig.algo_id`
  (`ConfigurationError` otherwise).

- **Sliced orders, `place_sliced_order()`** on `TradeSmart` and `AsyncTradeSmart`: an order
  above the exchange's freeze quantity is sent as slices, one after another, as ordinary
  `PlaceOrder` calls. It takes every `place_order` argument plus `max_slice_quantity` (a smaller
  slice size) and `max_slices` (default 25). The slice size is the largest whole number of lots
  within `security_info().frzqty` (NIFTY lot 65, freeze quantity 1801: slices of 1755, so 3965
  goes out as 1755, 1755, 455); a quantity within one slice is one order. Quantities are in
  units on every exchange, MCX included ("Changed — MCX order quantity"). Slices are paced by the
  order rate limit and never retried; sending stops at the first failure: a definite one is
  returned as `SlicedOrderResult(complete=False, error=...)`, an ambiguous one raises
  `OrderStatusUnknownError` with the partial result in `.sliced_result`. An interruption
  (Ctrl-C, a task cancellation, a caller's timeout) is re-raised unchanged with a note listing
  the order ids already placed (and `.sliced_result` where the exception accepts it). An order
  of more slices than `order_rate_limit_per_second` is refused up front (`ConfigurationError`,
  nothing sent) when `rate_limit_mode="raise"` or `order_rate_limit_max_wait` is below 1.0,
  since the rate limit would stop it part-way every time. New result type
  `SlicedOrderResult` (exported from `tradesmart`) and the example `examples/sliced_order.py`,
  which runs on the offline broker; the offline broker's instruments can carry a freeze
  quantity (`add_instrument(..., frzqty=1801)`).
- **Offline broker, `tradesmart.testing`**: `OfflineBroker` is a local TradeSmart server inside
  your test. `broker.client()`, `async_client()`, `stream()` and `async_stream()` give the real
  clients and streams, already logged in, wired to it (REST in memory, the stream on
  `127.0.0.1`). The test sets instruments, quotes, candles, funds, holdings and positions, and
  drives each order (`accept`, `fill` with partial fills, `trigger`, `cancel`, `reject`;
  `default_place`); the order book, history, trade book, positions and stream order updates
  follow. Failure drills: `fail_next(endpoint, drill)` with `reject`, `timeout_after_send` (the
  order is placed, the reply is lost), `connect_error`, `rate_limit`, `server_error`, `bad_body`
  and `session_expired`; stream drills `drop_stream`, `refuse_stream_login`, `silence_stream` /
  `resume_stream`. `set_reply(endpoint, body)` answers endpoints the offline broker does not model (such
  as `PlaceMultiOrder`). Reads keep the SDK's retries and streams its reconnects, with waits of
  about 1 ms and a 0.5 s heartbeat, so no retry or reconnect waits in real time (the SDK's own
  order rate limit, 10 orders per second by default, still applies). Optional pytest fixtures
  via `pytest_plugins = ["tradesmart.testing.pytest_plugin"]`. `import tradesmart` does not load
  it. Guide: `sdk-python/docs/testing.md`; example `sdk-python/examples/offline_strategy_test.py`.
- **Option Greeks and analytics** on `TradeSmart` and `AsyncTradeSmart`:
  `option_greeks()` (implied volatility, delta, gamma, theta per calendar day, vega per volatility
  point and rho per 1 % rate; Black-76 on the auto-selected underlying future, or
  Black-Scholes-Merton with `spot_price`; a later or earlier future is carry-adjusted to the
  option's expiry on NFO/BFO/CDS/BCD, while an MCX option uses its future's price without carry;
  default rate 6.5 % and dividend yield 0.0, overridable per call; expiry at the exchange close,
  `expiry_time` (naive, IST) overrides; `status` of `ok`, `near_expiry`, `expired`,
  `price_at_or_below_intrinsic` or `no_price`, never NaN or infinity; the parsed script master
  is cached per client and exchange and re-read only when the file changes),
  `option_chain_snapshot()` (one quote per contract, `missing` for contracts without a usable
  quote, `pcr()` and `max_pain()`) and `buildup()` (long/short buildup, unwinding, covering;
  `buildup_from_snapshot()` for stream data). New exports `OptionGreeks`, `OptionChainSnapshot`,
  `Buildup`, `BuildupReading`; modules `tradesmart.greeks` and `tradesmart.analytics`. The
  Greeks are estimates, not exchange values. Guide: `sdk-python/docs/option-analytics.md`.
- **`python -m tradesmart login`** (also the `tradesmart` command): opens the browser, catches the
  redirect on `http://127.0.0.1:<port>/callback` (`--port`, default 8080; 120 s) or accepts a
  pasted `code` / redirected URL (`--no-browser`, or when the port is busy or the wait times
  out; **`--listen-host HOST`** sets the address the redirect server binds, for use in a
  container: `0.0.0.0` with the port published to the host's loopback only, e.g.
  `docker run -p 127.0.0.1:8080:8080 ...`; a non-loopback value prints a warning, the redirect
  URL stays `http://127.0.0.1:<port>/callback`), exchanges the code once (never retried) and saves the day's token to a per-user token
  file: `%APPDATA%\tradesmart\token.json` on Windows, `$XDG_CONFIG_HOME/tradesmart/token.json`
  (default `~/.config/...`) elsewhere; `TRADESMART_TOKEN_FILE` or `--token-file` override it. The
  file is written atomically with mode `0o600`. Client id and app key come from `--client-id` /
  `--app-key`, the environment or a prompt; the secret key only from `TRADESMART_SECRET_KEY` or a
  hidden prompt. It prints the account, the expiry and the file path, never the token. A token
  whose `uid`/`actid` is not the client id entered is refused and nothing is saved (the API
  defines no OAuth `state`, so this stops a foreign code delivered to the local callback);
  otherwise the server's `uid` is saved. On POSIX a token file owned by another user or open to
  group/others is refused when read, and a loaded expiry is capped at the 06:00 IST rule. Also
  `token-path` and `logout`. Exit codes: 0 success, 1 login failed, 2 usage error. All messages
  are plain ASCII (e.g. "This login belongs to account <id>, not <typed> - nothing was saved.").
  After `login --token-file PATH` it shows copy-paste-safe commands to set
  `TRADESMART_TOKEN_FILE` for `from_env()` — `export TRADESMART_TOKEN_FILE='PATH'` on POSIX;
  PowerShell `$env:TRADESMART_TOKEN_FILE = 'PATH'` and cmd `set "TRADESMART_TOKEN_FILE=PATH"` on
  Windows (cmd is skipped for a path containing `%`) — and how to log out with
  `python -m tradesmart logout --token-file PATH`, the path quoted for the platform's shell.
- **`from_env()` uses the saved login.** `TradeSmart.from_env()` / `AsyncTradeSmart.from_env()`
  take a missing `TRADESMART_CLIENT_ID` / `TRADESMART_ACCESS_TOKEN` from the token file (the
  token as an `AccessToken`, so the local 06:00 IST check applies; only when it was issued for the
  same client id; a `TRADESMART_ACCESS_TOKEN` needs `TRADESMART_CLIENT_ID` from the environment
  too). With no usable token and no `TRADESMART_APP_KEY`, `from_env()` now raises
  `ConfigurationError` (an expired saved login: `AuthenticationError`) whose message says to run
  `python -m tradesmart login`; before, it built a client whose first call failed.
- **Order outcome logging** (logger `tradesmart.orders`): one INFO line per `PlaceOrder`,
  `PlaceMultiOrder`, `ModifyOrder`, `CancelOrder` and `ExitSNOOrder` call —
  `"<endpoint> accepted|rejected|not_sent|unknown order_id=<id or -> in <ms> ms"` (never payloads,
  prices or tokens; the time includes any client-side rate-limit wait) — and one WARNING per
  `OrderStatusUnknownError` ("check orders() before acting"). Only these five endpoints are
  logged: a low-level `request(..., state_changing=True)` to any other endpoint is never retried
  but writes no outcome line. On the async client, a task cancelled (e.g. by `asyncio.wait_for`) while an order
  request may already be on the wire logs a WARNING "<endpoint>: outcome unknown - check orders()"; the
  cancellation is re-raised unchanged. Cancellation while waiting for a rate-limit slot is silent
  (nothing was sent).
- **Stream reconnect awareness.** `TradeSmartStream(on_reconnect=...)`: an optional callback (no
  arguments) run on the callback thread right after `on_connect` each time the stream is
  `CONNECTED` again after a reconnect (never on the first connect) — call `orders()` and
  `positions()` there, because updates sent during the outage are not replayed.
  `TradeSmartStream.reconnect_count` and `AsyncTradeSmartStream.reconnect_count` (read-only):
  successful reconnects, `0` until the first.
- **`TradeSmartStream.from_env()` / `AsyncTradeSmartStream.from_env()`**: the client id and token
  from `TRADESMART_CLIENT_ID` + `TRADESMART_ACCESS_TOKEN`, else the saved login (same precedence
  as `TradeSmart.from_env()`; `TRADESMART_APP_KEY` is ignored because a stream cannot log in).
  Without a usable token: `ConfigurationError` (expired saved login: `AuthenticationError`) naming
  `python -m tradesmart login`. **`from_client(client)`** builds a stream from a `TradeSmart` or
  `AsyncTradeSmart` client's id and current token (and its `config` unless one is given); it
  raises `AuthenticationError` when the client has no token or it has expired. Both forward
  `config` and every callback keyword of the constructor.
- `TradeSmartStream` supports `with stream:` (`start()` on entry, `stop()` on exit).
- **Heartbeat supervision:** if the stream's heartbeat task fails unexpectedly, the error is
  logged at ERROR and the stream stops with a terminal `NetworkError` (raised by the async
  iterator, passed to `on_error` on the sync stream). Before, the heartbeat task could end
  silently while the stream stayed `CONNECTED`. A normal close logs no heartbeat ERROR. A
  `close()` that overlaps a heartbeat failure already under way waits for that shutdown, and the
  heartbeat's `NetworkError` is still surfaced (raised by the iterator / passed to `on_error`).
- `round_to_tick(price, tick_size) -> Decimal` (exported from `tradesmart`): nearest tick,
  half-way rounds up, quantised to the tick's decimal places.
- The `User-Agent` always carries the SDK token `tradesmart-python/<version>` (REST, script
  master, WebSocket handshake): a custom `user_agent` without it is sent as
  `"<custom> tradesmart-python/<version>"`.
- `ClientConfig.user_agent` with CR, LF, tab or any other ASCII control character raises
  `ConfigurationError` (the message does not echo the value).
- **New `ClientConfig.instruments_dir`** (default `None` = `~/.cache/tradesmart`): the folder
  where `download_instruments()`, `instruments()` and the option-chain and Greeks methods cache
  the script master when no `dest_dir` is given. A non-empty path or `None`, else
  `ConfigurationError`.
- **Live test suites** (source repository only, not in the wheel): opt-in and read-only by
  default; they can target a non-production environment through environment variables. The
  order tests follow the safety gates of the SDK behaviour specification (section 14) and the
  live order suite's README in the source repository.

### Changed — examples

- New `examples/order_lifecycle.py`: dry run by default (prints the `PlaceOrder`,
  `ModifyOrder` and `CancelOrder` requests); with `--live` it first checks both prices against a
  live quote (rounded to the tick; refused unless `lc <= price < last price`, the same for the
  new price), then places a tagged LIMIT order, reads its status from `orders()`, modifies it and
  cancels it. Whenever the order exists and its last known status is not final it tries to
  cancel it on the way out (also after a failed modify); an unreadable status is reported as
  unknown. After an unknown outcome it finds the order by its tag and never sends it again.
- Every example works with the login saved by `python -m tradesmart login`: `first_call.py` and
  `place_order.py` use `TradeSmart.from_env()` (`first_call.py --token-file PATH` still works),
  and the stream examples use `TradeSmartStream.from_env()` / `AsyncTradeSmartStream.from_env()`.
  `place_order.py`'s dry run no longer needs `TRADESMART_CLIENT_ID`.
- `place_order.py` (live) confirms the order's state from its row in `orders()`, polling until it
  leaves `PENDING`, instead of printing every `order_history()` entry; an unreadable order book is
  reported as "status unknown".

### Documentation

- **Open an account:** the README, the getting-started guide, the API-access page and the package's
  project URLs link to TradeSmart's account signup, and `python -m tradesmart login` shows the link
  once before it asks for a Client ID on a first login (no saved token or keys; not when
  `--client-id` or `TRADESMART_CLIENT_ID` is given).
- **Market orders are refused for API orders** (confirmed by TradeSmart): the Orders guide, README,
  migration guides and `examples/place_order.py` now say that TradeSmart rejects `MARKET` orders
  placed through the API (`OrderRejectedError`, "ALGO_CHK: MKT Order type not allowed for API
  order") and show a `LIMIT` order at a price that trades now instead. The SDK does not block
  `MARKET` itself.
- Reference and guides checked against the code: `PlaceMultiOrder` listed with the never-retried
  calls; `RateLimitError.endpoint` is the endpoint name; the `ClientConfig` type rules and the
  printable-ASCII `user_agent`; `str`-only ids, tokens and text and the accepted date types;
  the token checks when a token is set and the future-`issued_at` refusal of the token file;
  stream key normalisation; the "no public test environment" note.
- **User guide** in `sdk-python/docs/` on GitHub: *Your first order in 10 minutes* (API access,
  Redirect URL and static IP, install from GitHub, the login command with its paste-code
  fallback, a first order placed, checked, modified and cancelled, live order updates, an
  error table), plus pages on orders (status lifecycle, tag-based
  recovery, never-resend rules, rate limits, AMO), streaming, market data, configuration,
  troubleshooting and versioning (SemVer, Python 3.11–3.15, deprecation policy, reporting bugs and
  security issues).
- README: install from GitHub or a GitHub Release wheel, a quick start with the login command,
  absolute links (so they also work in the package description), tutorial material moved to the
  guide; the API reference stays.
- Recovery guidance (a short unique `tag` per order, matched in `orders()` after
  `OrderStatusUnknownError`), warm-start advice, backlog-merging wording (same type, same
  instrument) and known limitations (ModifyOrder response shape unconfirmed; OCO GTTs and iceberg orders not
  offered by the SDK yet; multi-leg orders not exposed).
- The Redirect URL `http://127.0.0.1:8080/callback` is stated as the one to register (the API
  Dashboard accepts it); a copyable Docker login command (`docker compose run --rm
  --service-ports login --client-id ...`); MCX API orders not yet enabled for TradeSmart (rejected
  with "CTCLID not assigned"); the order log's `not_sent` word; redirects never followed.


## [0.2.0] — internal preview, not published

Safer order handling, familiar names and faster defaults. An internal preview; never published.

**This release is not backwards compatible with 0.1.0.** The namespaces (`client.orders`,
`client.market`, …) are replaced by flat client methods with the names algo developers already
know, and several parameters, attributes and enum members are renamed. No aliases for the old
names are kept. Every rename is listed below.

### Breaking — client methods

`TradeSmart` and `AsyncTradeSmart` have the same flat methods (coroutines on `AsyncTradeSmart`,
except `from_env`, `login_url` and `set_access_token`).

| 0.1.0 | 0.2.0 | Notes |
|---|---|---|
| `client.auth.authorize_url()` | `client.login_url()` | |
| `client.auth.generate_access_token(code)` | `client.generate_session(code)` | returns `AccessToken`, stored on the client |
| `client.user.details()` | `client.profile()` | |
| `client.portfolio.limits()` | `client.margins()` | returns `Limits` |
| `client.portfolio.positions()` | `client.positions()` | |
| `client.portfolio.holdings(product=...)` | `client.holdings(product=...)` | |
| `client.orders.place(...)` | `client.place_order(...)` | returns the **order id** (`str`) instead of `OrderAck` |
| `client.orders.modify(order_number, ...)` | `client.modify_order(order_id, ...)` | returns the order id (from the ack, else the one you passed) |
| `client.orders.cancel(order_number)` | `client.cancel_order(order_id)` | returns the order id you passed |
| `client.orders.exit_cover_bracket(order_number, product)` | `client.exit_order(order_id, product)` | returns the order id you passed |
| `client.orders.book()` | `client.orders()` | |
| `client.orders.history(order_number)` | `client.order_history(order_id)` | |
| `client.orders.trades()` | `client.trades()` | |
| `client.orders.margin(...)` | `client.order_margin(...)` | |
| `client.market.search(text)` | `client.search_instruments(text)` | |
| `client.market.quote(exchange, token)` | `client.quote(exchange, token)` | |
| `client.market.security_info(exchange, token)` | `client.security_info(exchange, token)` | |
| `client.market.option_chain(...)` | `client.option_chain(...)` | |
| `client.market.time_price_series(exchange, token, start, end, interval)` | `client.historical_data(exchange, token, from_date, to_date, interval)` | |
| `client.market.eod_chart(eod_symbol(exchange, tradingsymbol), start, end)` | `client.daily_history(exchange, tradingsymbol, from_date, to_date)` | the `NSE:SBIN-EQ` symbol is built for you |
| `client.instruments.download(exchange, dest_dir)` + `load(path)` | `client.instruments(exchange)` | rows; the file is downloaded at most once per IST day |
| `client.instruments.download(exchange, dest_dir, *, force=False)` | `client.download_instruments(exchange, dest_dir=None, *, force=False)` | `dest_dir` is optional (default `~/.cache/tradesmart`) |
| `client.instruments.load(path)` / `client.instruments.find(rows, ...)` | `tradesmart.instruments.load(path)` / `tradesmart.instruments.find(rows, ...)` | module-level helpers |
| `from tradesmart import eod_symbol` | — | no longer needed; still available as `tradesmart.endpoints.market.eod_symbol` |

### Breaking — parameters, attributes and enums

| 0.1.0 | 0.2.0 | Where |
|---|---|---|
| `price_type=` | `order_type=` | order and margin methods, `*_call()` builders |
| `retention=` | `validity=` | `place_order`, `modify_order`, `*_call()` builders |
| `order_number` | `order_id` | every argument (`modify_order`, `cancel_order`, `exit_order`, `order_history`, `order_margin`, `*_call()` builders) and model attribute (`Order`, `OrderHistoryEntry`, `Trade`, `OrderUpdate`, `OrderAck`) |
| `remarks=` / `.remarks` | `tag=` / `.tag` | order arguments and the `Order`, `OrderHistoryEntry`, `Trade`, `OrderUpdate` attributes (the wire field stays `remarks`; `OrderMargin.remarks` is unchanged — it is the margin message) |
| `.price_type` | `.order_type` | `Order`, `OrderHistoryEntry`, `Trade`, `OrderUpdate` |
| `.retention` | `.validity` | `Order`, `OrderHistoryEntry`, `Trade`, `OrderUpdate` |
| `start=`, `end=` | `from_date=`, `to_date=` | historical and daily data |
| `PriceType` | `OrderType` | enum |
| `PriceType.STOP_LOSS_LIMIT`, `PriceType.STOP_LOSS_MARKET` | `OrderType.SL`, `OrderType.SL_M` | enum members (`LIMIT`, `MARKET` unchanged) |
| `Retention` | `Validity` | enum (members unchanged) |
| `Product.COVER_ORDER`, `Product.BRACKET_ORDER` | `Product.CO`, `Product.BO` | enum members (wire codes `H`, `B` unchanged) |
| `TradeSmartStream(on_order=...)` | `TradeSmartStream(on_order_update=...)` | stream callback |

### Breaking — behaviour

- `transaction_type`, `order_type`, `product`, `validity` and `status` on response models are now
  the enum member when the value is known (e.g. `Product.MIS`) and the raw string otherwise
  (they were always `str`). Comparisons such as `order.status == OrderStatus.COMPLETE` work as
  before. A blank string becomes `None`.
- Volume and open-interest fields (`v`/`volume`, `oi`/`open_interest`, `poi`, `toi`, `intv`,
  `intoi`) are now `Decimal` (were `int`), so fractional values no longer fail.
- `TradeSmartStream` runs **all** callbacks (including `on_connect`, `on_disconnect` and
  `on_error`) on a dedicated thread, `tradesmart-callbacks`, instead of the stream thread.
  `on_connect` may therefore run just after `start()` returns, and `on_connect` /
  `on_disconnect` can run before callbacks for messages from the previous connection.
- `TradeSmartStream.subscribe_*()` / `unsubscribe_*()` called from inside a callback now wait
  until the request was processed and raise in the callback on failure (they used to be
  scheduled without waiting).
- `TradeSmartStream.start()` waits `2 × ws_auth_timeout + 5` seconds by default (25 s with the
  default config) instead of 15 s.
- The script-master default folder is `~/.cache/tradesmart` (per user).

### Added

- Request parameters accept the common words, in any case, as well as TradeSmart's codes:
  `"BUY"` / `"SELL"`, `"LIMIT"` / `"MARKET"` / `"SL"` / `"SL-M"`, `"CNC"` / `"MIS"` / `"NRML"` /
  `"CO"` / `"BO"` / `"MTF"` (e.g. `transaction_type="BUY"` and `transaction_type="B"` both work).
- `from_date` / `to_date` accept a timezone-aware `datetime`, a `date` (meaning 00:00 IST that
  day, so `to_date=date.today()` excludes today's session) or epoch seconds.
- `ClientConfig` fields `keepalive_expiry` (50 s), `max_connections` (10) and
  `max_keepalive_connections` (5) for the SDK-created HTTP connection pool. **These defaults are
  placeholders** until TradeSmart confirms its server idle timeout and connection limit.
- `ClientConfig.stream_backlog_limit` (1,000): once this many stream messages (or more) wait for
  your code, market-data updates are merged.
- `examples/auth_server_flow.py --save-token PATH` writes the access token (only) to a file
  created with owner-only permissions; `examples/first_call.py --token-file PATH` reads it.
- README: client installation from the wheel file, a "Login → first call" walk-through, a
  "Low-latency setup" section and a "Switching from another broker" section linking the
  migration guides.

### Fixed

- **Order without an order id:** an accepted `PlaceOrder` answer that carries no order id now
  raises `OrderStatusUnknownError` (the order may exist; check the order book) instead of
  returning nothing usable. An accepted `ModifyOrder` answer is a definitive acceptance:
  `modify_order()` returns its `norenordno`, else its `result` field, else the order id you
  passed.
- **Enum text:** every SDK enum prints as its wire value (`str(OrderStatus.OPEN) == "OPEN"`,
  also in f-strings and `format()`), the same on Python 3.9 and 3.11+.
- **Async script master blocked the event loop:** `AsyncTradeSmart.instruments()` /
  `download_instruments()` unzip, write and parse the file in a worker thread.
- **Order updates with numeric ids:** stream order and position updates whose ids, tokens or
  text fields arrive as JSON numbers are accepted (as `str`) instead of being delivered as
  `UnknownMessage`.
- **`request()` could retry an order:** `PlaceOrder`, `PlaceMultiOrder`, `ModifyOrder`,
  `CancelOrder`, `ExitSNOOrder` and `GenAcsTok` are never retried through the low-level
  `request()`, even when called with `state_changing=False`.
- **Token in an error body:** the `body` / `raw` stored on every exception (including
  `OrderStatusUnknownError`) has tokens and other secrets masked.
- **Margin price validation:** `order_margin()` checks `price`, `trigger_price` and
  `book_loss_price` like `place_order()` (numeric strings accepted, anything else raises
  `RequestValidationError` instead of a raw `TypeError`); `option_chain()` checks
  `strike_price` the same way.
- **`ordersource` missing:** `PlaceOrder` and `ModifyOrder` always send `"ordersource": "API"`,
  as TradeSmart's examples do.
- **Fractional volume / open interest** (e.g. `"123.50"`) no longer fails a whole response or
  drops a stream message.
- **ATO placeholder price:** TradeSmart's "price not set" value `42949672.95` in stream messages
  becomes `None` on price fields (the original stays in `.raw`) instead of looking like a real
  price.
- **Unknown timestamp formats** still become `None`, but are now logged once per format at
  WARNING, so a gap in the data is visible.
- **`quantity=True`** is rejected instead of being sent as 1.
- **Script master:** the exchange is validated (any case) before a download URL or file path is
  built; anything else raises `ValueError`.
- **Sync stream start timeout** is derived from the configuration (`2 × ws_auth_timeout + 5` s), so
  a slow but valid connect no longer times out.
- **Sync subscribe timeout:** the `RequestTimeoutError` message now says the subscription may
  still take effect and will be restored on reconnect.
- **Token exchange:** numeric `expires_in`, `uid` and `actid` values are accepted, and any
  `GenAcsTok` success answer that cannot be read raises `AuthenticationError` telling you to
  restart the login (the authorisation code is single-use).

### Performance

- **Warm connections:** HTTP connections are kept open for reuse (`keepalive_expiry`,
  `max_connections`, `max_keepalive_connections`; placeholder defaults, see above). No
  background keep-alive requests are sent. A client you inject with `http_client=` is used
  as-is. The README recommends one cheap call (`profile()`) before the market opens so the first
  order does not pay for connection setup.
- **No duplicate parsing:** the per-request DEBUG log line no longer re-parses every response
  body; that work is done only when DEBUG logging is enabled.
- **The stream never waits for your code:** the socket reader only reads, updates the snapshot
  cache and queues messages. Callbacks and `async for` are fed by a separate task (async stream)
  or a dedicated callback thread (sync stream), in arrival order, so a slow callback can no longer
  stall reading, heartbeats or reconnect detection.
- **Bounded backlog:** once `stream_backlog_limit` or more messages wait for your code,
  touchline/depth updates for the same instrument are merged (latest value per field wins; no
  field change is lost; the merged entry keeps its position). Order and position updates are
  never merged or dropped. A WARNING is logged while merging is active. An async-stream
  `close()` discards messages `on_message` has not received yet and logs one WARNING with their
  count.
- **Measured overhead** (microseconds per call, network mocked, Python 3.12; script
  `benchmarks/latency.py` in the source repository, not shipped):

  | Operation | µs |
  |---|---|
  | Bare `httpx` POST (baseline) | 88.6 |
  | `place_order()` | 194.1 |
  | `quote()` | 246.6 |
  | `AsyncTradeSmart.place_order()` | 204.5 |
  | `AsyncTradeSmart.quote()` | 224.1 |
  | Parse a stream touchline update (`tf`) | 11.8 |
  | Parse a stream depth update (`df`) | 21.5 |
  | Parse a stream depth snapshot (`dk`) | 54.2 |
  | Stream delivery buffer put + get (mostly event-loop overhead) | 7.6 |

### Changed

- Version 0.2.0; default `User-Agent` `tradesmart-python/0.2.0`.
- Examples catch `TradeSmartError` (network, timeout and parse errors), not only `APIError`;
  `examples/place_order.py` stays a dry run by default, refuses to go live without
  `TRADESMART_ACCESS_TOKEN`, and survives a failing order-book check after an unknown outcome.

### Known limitations

- `PlaceMultiOrder` and `GetQuotesMultiple` have no typed methods: their schemas are not
  documented (use `client.request()`).
- GTT and iceberg orders: not offered by the TradeSmart API V2; no commitment yet.
- Connection-pool defaults are placeholders until TradeSmart confirms its server idle timeout and
  connection limit.
- Not yet verified against a live environment; all tests use mocks built from TradeSmart's
  official examples.
- The `Holdings` success shape is undocumented; the SDK requires a JSON array and raises
  `ResponseParseError` otherwise.
- Script-master column layout is undocumented, so rows are untyped `dict`s.
- No token refresh or logout (not documented); multi-leg orders are not exposed.

## [0.1.0] — internal preview, not published

First internal preview; never published.

### Added

- `TradeSmart` (blocking) and `AsyncTradeSmart` (asyncio) clients with the namespaces `auth`,
  `user`, `portfolio`, `market`, `orders` and `instruments`, context-manager support,
  `from_env()` (`TRADESMART_*` variables) and an injectable `httpx` client.
- Typed methods for all 19 documented REST endpoints: `GenAcsTok`, `UserDetails`, `Holdings`,
  `Limits`, `PositionBook`, `SearchScrip`, `GetQuotes`, `GetSecurityInfo`, `GetOptionChain`,
  `TPSeries`, `EODChartData`, `OrderBook`, `SingleOrdHist`, `TradeBook`, `GetOrderMargin`,
  `PlaceOrder`, `ModifyOrder`, `CancelOrder`, `ExitSNOOrder`.
- Low-level `client.request()` for any endpoint (e.g. the undocumented-schema `PlaceMultiOrder` and
  `GetQuotesMultiple`), with the same auth, encoding, rate limiting and error handling.
- OAuth helpers: `build_authorize_url`, `compute_checksum`, `AccessToken` with the
  "valid until the next 06:00 IST" expiry and an optional local expiry check.
- Pydantic v2 response models (Decimal prices, int quantities, timezone-aware timestamps, unknown
  fields kept, `.raw` payload) and strict request validation for orders (price/trigger rules,
  cover/bracket legs, MTF not on MCX, disclosed quantity).
- `jData` encoding that percent-encodes `&`, `+` and `%` (symbols such as `M&M`).
- Error hierarchy rooted at `TradeSmartError`, including `OrderStatusUnknownError` for ambiguous
  state-changing outcomes and `ResponseParseError` for unexpected bodies; "no data" returns `[]`
  on list endpoints.
- Retries with capped exponential backoff and jitter for read endpoints only (never for
  state-changing calls); `Retry-After` support.
- Client-side sliding-window rate limiter for `PlaceOrder` (30/s at the time, 600/min; since lowered to 10/s by default), `GetQuotes` and
  `TPSeries` (100/s, 1000/min) with `wait` and `raise` modes.
- Script master helpers: download + extract per exchange (cached per IST day), header-driven
  parsing (`load`) and filtering (`find`).
- WebSocket clients `AsyncTradeSmartStream` (async iteration, callbacks) and `TradeSmartStream`
  (background thread, callbacks): touchline, depth, order and position subscriptions; heartbeat;
  dead-socket detection; reconnect with backoff and subscription restore; terminal auth failure;
  1,000-subscription guard; snapshot cache merging deltas.
- `tradesmart` logger with secret redaction; payload logging opt-in.
- `ClientConfig` for URLs, timeouts, retries, rate-limit mode, user agent, token-expiry check and
  stream settings.
- Examples (`examples/`), README with full API reference, architecture and authentication guides,
  opt-in read-only live test suite, MIT licence.

### Order and data safety

- An order acknowledgement the server accepted (`stat: Ok`) but that cannot be parsed raises
  `OrderStatusUnknownError` on `PlaceOrder`, `ModifyOrder`, `CancelOrder` and `ExitSNOOrder`,
  so an accepted order is never mistaken for a failure and re-placed. Order numbers and the
  `stat` / `dmsg` text accept JSON numbers (coerced to `str`).
- State-changing calls raise `OrderStatusUnknownError` for any non-definitive response — non-JSON
  at any status (incl. 502/503/504 gateway pages), an empty body, an unexpected JSON shape or a
  list. A JSON `stat: Not_Ok` stays definitive (`OrderRejectedError`, or `ServerError` /
  `AuthenticationError` / `RateLimitError` by status). `OrderStatusUnknownError` carries optional
  `http_status` and `body` (first 2,000 chars) attributes.
- Every `httpx.RequestError` is mapped: `DecodingError` / `TooManyRedirects` raise
  `OrderStatusUnknownError` on state-changing calls and `NetworkError` on reads.
- A JSON list on an HTTP error status (≥ 400) is never returned as data; it raises by status.
- `ResponseParseError` from model validation does not chain the pydantic `ValidationError`, so
  raw input values such as tokens cannot appear in a printed traceback.
- An unknown `GenAcsTok` outcome raises `AuthenticationError("GenAcsTok outcome unknown: the
  authorisation code is single-use; restart the login")`.
- A non-finite epoch value raises `ResponseParseError` instead of a bare `OverflowError`, and a
  stream frame that fails to parse is delivered as `UnknownMessage` instead of ending the stream.
- `subscribe_*` / `unsubscribe_*` on a closed stream raise `TradeSmartError("stream is closed;
  create a new stream")` immediately.
