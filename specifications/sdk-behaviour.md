# TradeSmart SDK behaviour contract

Language-neutral contract for every TradeSmart API V2 SDK. An SDK that follows this document
behaves like the reference implementation, the Python SDK 2.1.0 (`sdk-python/`), without its code
being read. Python names are used for examples; other languages translate them (section 2).

Keywords: **must** = required for conformance; **should** = recommended; **may** = optional.
Where the official API docs and the live server differ, this document states the **live**
behaviour and gives the open-item id (F-number) under which the difference is tracked.
"Python reference:" lines name the module under `sdk-python/src/tradesmart/` that implements
each section.

---

## 1. Scope and conformance levels

| Level | Area | Sections |
|---|---|---|
| Required | REST client (all typed methods, `request()` escape hatch) | 2, 3, 4 |
| Required | Authentication (authorize URL, checksum, GenAcsTok, 06:00 IST expiry, `from_env` from environment variables; its saved-login part needs the token store of 6.5) | 6 |
| Required | Errors and order safety (taxonomy, no retries for state-changing calls, unknown outcome) | 4, 5 |
| Required | Models (Decimal prices, string ids, integer quantities, unknown fields and raw payload kept) | 7 |
| Required | Instruments (script master download, freshness, parsing) | 8 |
| Required | Stream (connect, heartbeat, reconnect, resubscribe, snapshots, never blocking the reader) | 9 |
| Required | Security rules | 13 |
| Recommended | Option Greeks and implied volatility | 10 |
| Recommended | Analytics (PCR, max pain, buildup, chain snapshot) | 11 |
| Recommended | Login CLI, token store and saved keys | 6.5, 6.6, 6.7 |
| Optional | Offline broker for client tests | 16 |

An SDK claims **"TradeSmart SDK behaviour 2.0 — required"** when it passes every required
conformance vector, or **"... — full"** when it also passes the recommended ones.

### 1.1 Conformance vectors

`specifications/conformance/` holds JSON test vectors generated from the Python SDK and kept in
sync by a Python test. Every file has the shape
`{"description", "source", "cases": [{"name", "input", "expected"}]}`; numeric tolerances are
stated per file (e.g. `"tolerance": {"abs": 1e-9}`) and Decimal values are strings (compare them
as decimals, never as floats, except where a file's tolerance is empty: there the text is compared,
for example the strike `"24000.00"` in `contract_selection.json`; `conformance/README.md`, "Value
encoding"). An SDK test suite should load each file and assert every case.

| File | Level | Covers (section) |
|---|---|---|
| `README.md` | — | How the vectors are generated and used |
| `checksum.json` | Required | SHA-256 checksum (6.2) |
| `not_ok_classification.json` | Required | Response classification table (4.2) |
| `expiry_instants.json` | Recommended | Expiry instants per exchange (10.2) |
| `contract_selection.json` | Recommended | Option lookup and future selection (10.4) |
| `greeks_prices.json` | Recommended | Black-76 / BSM prices and Greeks (10.1) |
| `implied_volatility.json` | Recommended | IV solver (10.6) |
| `greeks_edge_cases.json` | Recommended | Status precedence and expiry-day rules (10.5) |
| `carry_forward.json` | Recommended | Carry adjustment (10.3) |
| `pcr.json` | Recommended | Put-call ratio (11.1) |
| `max_pain.json` | Recommended | Max pain (11.2) |
| `buildup.json` | Recommended | Buildup classification (11.3) |
| `ws_connect_ack.json` | Required | Connect-ack acceptance (9.1) |
| `config_defaults.json` | Required | Configuration defaults (12) |
| `jdata_encoding.json` | Required | Exact request body bytes, nested values included (3.2) |
| `token_expiry.json` | Required | Token expiry at 06:00 IST and the `is_expired` boundary (6.3) |
| `token_file.json` | Recommended | Token-file timestamp format, accepted forms and expiry cap (6.5) |
| `scrip_keys.json` | Required | Stream subscription keys (2.6, 9.4) |
| `time_conversion.json` | Required | Request times and response time formats (2.3, 7.1) |
| `round_to_tick.json` | Required | `round_to_tick` (2.7) |
| `retry_schedule.json` | Required | Read retries, back-off and `Retry-After` (5.1) |
| `order_validation.json` | Required | Wire payloads and validation of `place_order`, `modify_order`, `cancel_order`, `exit_order`, `order_margin`, `place_gtt`, `modify_gtt`, `cancel_gtt` (the GTT calls refuse the OCO alert types), with the wire keys a validation error names (2.3, 2.4, 2.8, 4.1) |
| `oco_requests.json` | Required | Wire payloads (exact nested bodies, key order significant) and validation of `place_oco`, `modify_oco`, `cancel_oco` (2.9) |
| `oco_rows.json` | Required | OCO list rows: legs, triggers, `stop_loss` / `target`, a failing nested record `null` as a whole (2.9) |
| `read_payloads.json` | Required | Wire payloads and validation of every read method (2.3) |
| `request_names.json` | Required | `request()` name canonicalisation, state-changing flag and rate group (2.5) |
| `outcome_words.json` | Required | Order outcome log word and WARNING per result (5.6) |
| `success_words.json` | Required | Per-endpoint success words: GTT, OCO and iceberg writes, and `Invalid Oi` (4.2.1) |
| `transport_failures.json` | Required | Send-time failure mapping and retries per failure kind (5.1, 5.2) |
| `script_master_parse.json` | Required | Script-master parsing (8.3) |
| `script_master_freshness.json` | Required | Script-master cache and re-check decisions (8.2) |
| `stream_backlog.json` | Required | Stream backpressure merge rule (9.7) |
| `slice_plan.json` | Required | Sliced-order slice sizes and refusals (5.7) |

The Greeks and analytics result types and the `StreamState` / `Buildup` enums are in
`models.md` with the response models; their functions are listed in 10.8 and 11.5.

Shared request/response fixtures for mocked tests live in `specifications/test-fixtures/`
(section 14).

---

## 2. Public API surface

Python reference: `client.py`, `async_client.py`, `endpoints/*.py`, `stream/*.py`.

### 2.1 Naming in other languages

- Keep the class names `TradeSmart`, `AsyncTradeSmart`, `TradeSmartStream`, `AsyncTradeSmartStream`
  (or the language's async convention, e.g. methods returning a `Promise`/`Task`/`Future`).
- Keep every method and parameter name, converted to the language's case style:
  `place_order` → `placeOrder` (Java, JS), `PlaceOrder` (.NET, Go exported). Do not rename
  (`orders`, not `getOrderBook`). Flat methods on the client; no namespaces.
- Keyword-only parameters map to an options object/struct/builder where the language has no
  keyword arguments. Optional parameters stay optional with the defaults below.
- Error class names keep their names (`OrderStatusUnknownError` → `OrderStatusUnknownException`
  is acceptable in Java/.NET).
- Sync and async clients expose the same methods with the same parameters; only the calling
  convention differs. Python's sync `close()` is `aclose()` on the async client.

### 2.2 Client construction

| Member | Behaviour |
|---|---|
| `TradeSmart(client_id, *, access_token=None, app_key=None, secret_key=None, config=None, http_client=None)` | `client_id` must be non-empty (`ConfigurationError`). `access_token` is a plain string or an `AccessToken`, checked by the token rules of 6.3 when it is set. An injected HTTP client is used as is and not closed by the SDK. |
| `from_env(*, config=None)` | Build from environment variables / saved login (6.4). |
| `close()` / `aclose()`, context manager | Release connections. Every later call raises `ConfigurationError("client is closed; create a new client")` and sends nothing (also when an injected HTTP client was closed); an order call logs `not_sent` (5.6). |
| `set_access_token(token)` | Replace the token (same checks as the constructor). |
| `repr` | Shows only `client_id`; never a token or key. |

The injected HTTP client (`http_client`, Python: an `httpx.Client` / `httpx.AsyncClient`) is a
Python convenience for proxies, custom TLS and tests. Another SDK may offer the equivalent of its
platform (an `HttpClient`, a `fetch` implementation, a transport or handler) or none at all; if it
does, the same rules hold: the SDK never closes it, never lets it follow redirects (3.1), builds
every request before it is sent (5.2), and treats an exception from the caller's own hooks or
transport as 5.1 and 5.2 describe.

### 2.3 REST methods

All payload values are strings on the wire (section 3.2). `uid` and `actid` are always the
client id. "List" returns are empty lists for the "no data" signal (4.3).

| Method | Endpoint | Parameters (Python types, defaults) | Wire payload | Returns |
|---|---|---|---|---|
| `login_url()` | — | — (needs `app_key`) | — | `str` (6.1) |
| `generate_session(code)` | `GenAcsTok` | `code: str` | `code`, `checksum` | `AccessToken`, stored on the client |
| `profile()` | `UserDetails` | — | `uid` | `UserDetails` |
| `margins()` | `Limits` | — | `uid`, `actid` | `Limits` |
| `positions()` | `PositionBook` | — | `uid`, `actid` | `list[Position]` |
| `holdings(product="CNC")` | `Holdings` | `product: Product\|str`, resolved through `Product` like `place_order` (any case, member name or wire code; unknown → `RequestValidationError` field `prd`, nothing sent) | `uid`, `actid`, `prd` (default sent as `C`) | `list[Holding]` |
| `place_order(...)` | `PlaceOrder` | see 2.4 | see 2.4 | `str` order id |
| `place_sliced_order(...)` | script master + `GetSecurityInfo`, then `PlaceOrder` per slice | see 5.7 | as `place_order`, per slice | `SlicedOrderResult` (5.7) |
| `modify_order(order_id, ...)` | `ModifyOrder` | see 2.4 | see 2.4 | `str` order id (5.4) |
| `cancel_order(order_id)` | `CancelOrder` | `order_id: str` (non-empty) | `uid`, `norenordno` | `str` = `order_id` |
| `exit_order(order_id, product)` | `ExitSNOOrder` | `order_id: str`, `product` CO (`H`) or BO (`B`) only | `uid`, `norenordno`, `prd` | `str` = `order_id` |
| `orders()` | `OrderBook` | — | `uid` (no `actid`: the docs mark `actid` "Required", but their examples send only `uid`, and `uid` alone works on the live server) | `list[Order]` |
| `order_history(order_id)` | `SingleOrdHist` | `order_id: str` | `uid`, `norenordno` | `list[OrderHistoryEntry]` (entry order: open item F14) |
| `trades()` | `TradeBook` | — | `uid`, `actid` | `list[Trade]` |
| `order_margin(*, exchange, tradingsymbol, quantity, price, product, transaction_type, order_type, trigger_price=None, book_loss_price=None, order_id=None, sno_number=None)` | `GetOrderMargin` | as named | `uid, actid, exch, tsym, qty, prc, prd, trantype, prctyp[, trgprc, blprc, norenordno, snonum]` | `OrderMargin` (a read: retried) |
| `place_gtt(...)` | `PlaceGTTOrder` | see 2.8 | see 2.8 | `str` alert id (`al_id`) |
| `modify_gtt(alert_id, ...)` | `ModifyGTTOrder` | see 2.8 | see 2.8 | `str` alert id |
| `cancel_gtt(alert_id)` | `CancelGTTOrder` | `alert_id: str` (non-blank) | `uid`, `al_id` (no `actid`, as documented) | `str` alert id |
| `place_oco(...)` | `PlaceOCOOrder` | see 2.9 | see 2.9 | `str` alert id (`al_id`) |
| `modify_oco(alert_id, ...)` | `ModifyOCOOrder` | see 2.9 | see 2.9 | `str` alert id |
| `cancel_oco(alert_id)` | `CancelOCOOrder` | `alert_id: str` (non-blank) | `uid`, `al_id` | `str` alert id |
| `pending_gtts()` | `GetPendingGTTOrder` | — | `uid` | `list[Gtt]` |
| `triggered_gtts()` | `GetTrgOi` | — | `uid`, `actid` | `list[TriggeredGtt]` |
| `gtt_alert_types()` | `GetEnabledGTTs` | — | `uid` | `list[str]`: the `ai_ts[].ai_t` values (alert families such as `LTP`), in the server's order |
| `search_instruments(text)` | `SearchScrip` | `text: str` | `uid`, `stext` | `list[ScripMatch]` (from `values`) |
| `quote(exchange, token)` | `GetQuotes` | `exchange`, `token: str` (any string, no format check; a non-string such as `2885` → `RequestValidationError` field `token`, never converted) | `uid`, `exch`, `token` | `Quote` |
| `security_info(exchange, token)` | `GetSecurityInfo` | as `quote` | `uid`, `exch`, `token` | `SecurityInfo` |
| `option_chain(exchange, tradingsymbol, strike_price, count)` | `GetOptionChain` | `count: int >= 1` (no maximum) | `uid, exch, tsym, strprc, cnt` | `list[OptionContract]` (from `values`) |
| `historical_data(exchange, token, from_date=None, to_date=None, interval=None)` | `TPSeries` | times: aware datetime, date (00:00 IST) or epoch seconds; `interval` minutes (an integer or its text) in `1,3,5,10,15,30,60,120,240` | `uid, exch, token[, st, et, intrv]` (omitted when `None`) | `list[Candle]` (newest first) |
| `daily_history(exchange, tradingsymbol, from_date, to_date)` | `EODChartData` | as above | `sym` = `EXCH:TSYM`, `from`, `to` (no `uid`) | `list[DailyCandle]` |
| `option_greeks(exchange, tradingsymbol, *, rate=0.065, dividend_yield=0.0, option_price=None, underlying_price=None, spot_price=None, expiry_time=None)` | script master + `GetQuotes` | section 10.7 | — | `OptionGreeks` |
| `option_chain_snapshot(exchange, tradingsymbol, strike_price, count=25)` | `GetOptionChain` + `GetQuotes` | section 11.4 | — | `OptionChainSnapshot` |
| `buildup(exchange, token)` | `GetQuotes` + `TPSeries` | section 11.3 | — | `BuildupReading` |
| `instruments(exchange)` | script master | `exchange` | GET (8) | `list[dict[str,str]]` |
| `download_instruments(exchange, dest_dir=None, *, force=False)` | script master | | GET (8) | path of `<EXCH>_symbols.txt` |
| `request(endpoint, payload, *, state_changing, expect="object")` | any | section 2.5 | payload + `uid` | parsed JSON body |

Input conversion rules:

- `exchange` accepts any case and the enum; it is sent upper-case. An unknown exchange raises
  `RequestValidationError` (field `exch`) before anything is sent (`ValueError` for
  `instruments`/`download_instruments`).
- Time inputs accept exactly three forms: a timezone-aware datetime, a date (00:00 IST that
  day) or an integer of epoch seconds (not a boolean). Anything else (a naive datetime, a float,
  text, a boolean, `null` where a time is required) raises `RequestValidationError` (field
  `time`) and nothing is sent: no value is guessed, truncated or converted.
- Ids and tokens (`order_id`, instrument `token`, `text`) must be strings: an integer is
  rejected with `RequestValidationError` (the wire key as field), never converted, because the
  wire carries strings only and an id such as `"00022"` must not lose its zeros.
- Historical windows (official docs): NSE/BSE 360 days, MCX 70 days, NFO/BFO/CDS/BCD 65 days.
- Vectors for every read method's wire payload and these input rules: `read_payloads.json`.

### 2.4 Order parameters and client-side validation

`place_order(*, exchange, tradingsymbol, quantity, transaction_type, product, order_type, price=None, trigger_price=None, validity="DAY", disclosed_quantity=None, amo=False, tag=None, book_loss_price=None, book_profit_price=None, trail_price=None, market_protection=None, algo_id=None)`

`modify_order(order_id, *, exchange, tradingsymbol, quantity, price, order_type, product, trigger_price=None, validity=None, tag=None, book_loss_price=None, book_profit_price=None, trail_price=None, algo_id=None)`

Wire keys, in this order (optional ones only when given): PlaceOrder `uid, actid, exch, tsym, qty,
prc, trgprc, dscqty, prd, trantype, prctyp, ret, amo, remarks, ordersource, blprc, bpprc,
trailprc, mkt_protection, algo_id`; ModifyOrder `uid, actid, exch, norenordno, tsym, qty, prc, prctyp, prd,
trgprc, ret, remarks, ordersource, blprc, bpprc, trailprc, algo_id`. `amo` is always sent (`"Yes"`/`"No"`):
this deliberately differs from TradeSmart's docs, which say to omit `amo` unless the order is an
AMO; sending `"No"` was accepted by the live server. `dscqty` is sent only when
`disclosed_quantity` is given, although the docs mark it "Required": PlaceOrder without it was
accepted by the live server. `ordersource` is always `"API"`; `tag` →
`remarks`. Every enum-typed parameter (`exchange`, `transaction_type`, `product`, `order_type`,
`validity`) on every method resolves strings through its enum (7.3). ModifyOrder sends no `amo`
and `ret` only when `validity` is given. Vectors (place, modify, cancel, exit and margin bodies):
`order_validation.json`.

`algo_id` (F29, resolved): TradeSmart's PlaceOrder and ModifyOrder tables list `algo_id` (String,
Optional, "Algo ID. Required when placing more than 10 orders per second"). The SDK sends it
when the per-call `algo_id` or, failing that, `ClientConfig.algo_id` (12) is set: the per-call
value overrides the config for that call; with neither set no `algo_id` key is sent (the body is
unchanged). It is the last wire key (after the documented fields above), sent once, on
`PlaceOrder` and `ModifyOrder` only, so on every `place_sliced_order` slice (5.7) too; never on
`CancelOrder`, `ExitSNOOrder`, `GetOrderMargin` or a read, and `request()` (2.5) sends its
payload as given (the config value is not added). It is not a secret: it may appear in DEBUG
payload logs like the other order fields, but not in the order outcome lines (5.6).

Order types: `modify_order` and `order_margin` accept every `OrderType` (`MKT` and `SL-MKT`
included). This deliberately goes beyond TradeSmart's docs, whose `ModifyOrder` and
`GetOrderMargin` tables list `prctyp` as `LMT` / `SL-LMT` only (`02-orders.md`); the SDK leaves
the decision to the server, which answers `Not_Ok` for a type it does not take. The
`product=MTF` with `exchange=MCX` rule below is an owner-confirmed rule, not in the official
docs (15).

Validation (raises `RequestValidationError`, nothing is sent):

| Rule | Applies to |
|---|---|
| `quantity` integer ≥ 1 (booleans rejected) | place, modify, margin |
| Prices ≥ 0, finite | all order price fields (`option_chain`'s `strike_price` is only checked for finiteness) |
| `MKT`/`SL-MKT` with no price → `prc = "0"` | place, modify |
| Price > 0 for `LMT`/`SL-LMT` | place, modify |
| `trigger_price` > 0 required for `SL-LMT`/`SL-MKT`; not allowed for other types | place, modify |
| `book_loss_price`/`book_profit_price`/`trail_price` only for CO/BO; `book_profit_price` BO only | place, modify |
| CO requires `book_loss_price`; BO requires `book_loss_price` and `book_profit_price` | place |
| `disclosed_quantity` ≥ 0 and ≤ `quantity` | place |
| `product=MTF` with `exchange=MCX` rejected | place |
| `order_id` non-empty | modify, cancel, exit |
| `algo_id` a non-blank string when given (a boolean, number or blank text rejected) | place, modify, sliced |
| `product` must be CO or BO | exit |

### 2.5 Low-level `request()`

- `endpoint` is trimmed of whitespace and slashes and must then be ASCII letters only, else
  `ConfigurationError`. The names the safety and rate-limit rules know (`PlaceOrder`,
  `PlaceMultiOrder`, `ModifyOrder`, `CancelOrder`, `ExitSNOOrder`, `GenAcsTok`, `PlaceGTTOrder`,
  `ModifyGTTOrder`, `CancelGTTOrder`, `PlaceOCOOrder`, `ModifyOCOOrder`, `CancelOCOOrder`,
  `PlaceIceBergOrder`, `ModifyIceBergOrder`,
  `CancelIceBergOrder`, `GetQuotes`, `GetQuotesMultiple`, `TPSeries`) match case-insensitively and are rewritten to that spelling
  (`placeorder` → `PlaceOrder`) so the rules cannot be bypassed. Every other name is sent as
  given (`limits` stays `limits`).
- `uid` is added when absent; values are converted to wire strings (7.3). A value may also be a
  nested object or list of such values (the OCO bodies, 2.9): the key and item order is kept and
  the body is encoded as in 3.2. A `None` value, at any depth, is not
  allowed: it raises `TypeError` (a programming error, not an SDK error) and nothing is sent.
- The call is always authenticated (`Authorization: Bearer`, even for `GenAcsTok`, so use
  `generate_session()` for the login) and always uses `allow_empty=False`: a `Not_Ok` "no data"
  reply is an `APIError`, never `[]` (4.2 row 10 does not apply). The body is returned as parsed
  JSON with no endpoint-specific decoding (`EODChartData` elements stay JSON-encoded strings).
- With `state_changing=True` and `expect="list"`, a `stat: Ok` **object** raises
  `OrderStatusUnknownError` (the request may have been executed); a list where an object is
  expected is unknown too (4.2 rows 4 and 9). Reads keep `ResponseParseError` for a wrong shape.
- The call is state-changing if `state_changing=True` **or** the name is one of `PlaceOrder`,
  `PlaceMultiOrder`, `ModifyOrder`, `CancelOrder`, `ExitSNOOrder`, `GenAcsTok`, `PlaceGTTOrder`,
  `ModifyGTTOrder`, `CancelGTTOrder`, `PlaceOCOOrder`, `ModifyOCOOrder`, `CancelOCOOrder`,
  `PlaceIceBergOrder`, `ModifyIceBergOrder`, `CancelIceBergOrder`.
- Success words by name (4.2.1): the GTT, OCO and iceberg writes succeed only on their
  documented or observed `stat` word (and, for GTT and OCO, a non-blank `al_id`); `Invalid Oi`
  on the modify and cancel writes of GTT and OCO is a definite failure; every other name
  succeeds only on `Ok`. The GTT, OCO and iceberg writes have no rate group (the docs list none).
- Rate group by name: `PlaceOrder`/`PlaceMultiOrder` → orders; `GetQuotes`/`GetQuotesMultiple`
  → quotes; `TPSeries` → tpseries. `PlaceMultiOrder` and `GetQuotesMultiple` exist but their
  schema is undocumented (open item F1): reachable only through `request()`.
- Vectors (name, state-changing flag, rate group, success words, required ack field, body,
  rejected names): `request_names.json`.

### 2.6 Stream classes

| Member | Async (`AsyncTradeSmartStream`) | Sync (`TradeSmartStream`) |
|---|---|---|
| Constructor | `(client_id, access_token, *, config=None, on_message=None, on_state_change=None)` | `(client_id, access_token, *, config=None, on_touchline, on_depth, on_order_update, on_position, on_connect, on_disconnect, on_error, on_message, on_reconnect)` all default `None` |
| Factories | `from_env(...)`, `from_client(client, ...)` | same |
| Lifecycle | `connect()`, `close()`, `async with`, `async for msg in stream` | `start(timeout=None)`, `stop(timeout=5.0)`, `run_forever()`, `with` |
| Subscriptions | `subscribe_touchline/unsubscribe_touchline(instruments)`, `subscribe_depth/unsubscribe_depth(instruments)`, `subscribe_orders/unsubscribe_orders()`, `subscribe_positions/unsubscribe_positions()` | same (blocking) |
| State | `state`, `reconnect_count`, `heartbeat_acks`, `snapshot(key)` | same |

`instruments` is one `"EXCH|TOKEN"` string, one `(exchange, token)` pair, or a list of them. Any
other argument, including a non-iterable one (`22`, `None`), raises `RequestValidationError`
("instruments.0 (expected 'EXCH|TOKEN', (exchange, token) or a list of them)"), never a raw
type error.

Factories: `from_env()` reads the client id and token like the client's `from_env()` (6.4; a
stream ignores `TRADESMART_APP_KEY`). `from_client(client, ...)` takes the client's id and its
held token (an `AccessToken` keeps its expiry check), and the client's `config` unless one is
passed; a client without a token, or with an expired `AccessToken` (under
`check_token_expiry`), raises `AuthenticationError` and nothing is connected.

Callbacks: `on_message` and `on_state_change` of the async stream may be plain functions or
coroutine functions (an awaitable result is scheduled as a task, 9.7). Every callback of the sync
stream must be a plain function: an `async def` callback, or an object whose `__call__` is
`async def`, raises `TypeError` in the constructor naming the parameter. `heartbeat_acks` is a
read-only count of the `hk` replies received over all connections of the stream.

### 2.7 Other public helpers

`round_to_tick(price, tick_size)` (nearest multiple of the tick, a half-way price rounded **away
from zero** (`-100.025` at tick `0.05` → `-100.05`; not JavaScript's `Math.round`), written with
the tick's decimal places, the tick's trailing zeros ignored; `ValueError` for tick ≤ 0 or
non-finite input; vectors `round_to_tick.json`); `build_authorize_url`, `compute_checksum` (6);
`tradesmart.greeks`: `black76`, `black_scholes`, `implied_volatility`, `greeks_from_price`
(signatures and results: 10.8); `tradesmart.analytics`: `pcr`, `max_pain`, `classify_buildup`,
`buildup_from_snapshot` (signatures and results: 11.5). Their parameter and result field names
are part of the contract like the client's (2.1).

### 2.8 GTT orders

Python reference: `endpoints/gtt.py`, `models/gtt.py`, `client.py`, `async_client.py`. Docs:
the GTT page of the TradeSmart API docs (local copy of 2026-11-12, `03-gtt-orders.md`).

A GTT (good till triggered) order is held on TradeSmart's server as an alert: when the live value
chosen by the alert type crosses the trigger value, the server sends the order described by the
other fields to the exchange. Nothing reaches the exchange before then.

- **A GTT whose condition is already true when it is placed fires at once** (owner, 2026-11-12):
  it places a real order immediately. The SDK does not check the condition against the market.
- **Validity.** `alert_validity=GTT` keeps the GTT until it triggers or is cancelled, at most
  **365 days or the contract's expiry, whichever is earlier** (owner, 2026-11-12); `DAY` ends it
  with the current trading day.
- After it triggers, the GTT is listed by `triggered_gtts()` instead of `pending_gtts()`, and its
  order is in the order book with `add_ord_id` = the alert id (`Order.parent_id`, `models.md`).

`place_gtt(*, exchange, tradingsymbol, alert_type, trigger_value, quantity, transaction_type, product, order_type, tag, price=None, trigger_price=None, validity="DAY", alert_validity="GTT", disclosed_quantity=None)` → `str` alert id

`modify_gtt(alert_id, *, exchange, tradingsymbol, alert_type, trigger_value, quantity, transaction_type, product, order_type, tag, price=None, trigger_price=None, validity, alert_validity, disclosed_quantity=None)` → `str` alert id (keyword-only, so `validity` and `alert_validity` are required although they follow optional keywords)

- Every field is sent again, also the unchanged ones. The docs say `ai_t` cannot be changed
  (send the original; the server decides, the SDK does not compare).
- **`validity` and `alert_validity` have no defaults on `modify_gtt`** (they do on `place_gtt`).
  The docs make both required on Modify, so both are always sent. A default would silently turn a
  `DAY` GTT, or an `EOS` / `IOC` order, back into `GTT` / `DAY` when the caller only meant to move
  the trigger. Leaving either out is a language-level argument error, and nothing is sent.
- A modify that makes the condition already true fires the GTT at once, as a place does.
- Whether the server keeps or clears an existing disclosed quantity when a modify omits `dscqty`
  is undocumented. Pass `disclosed_quantity` again to keep it.

`cancel_gtt(alert_id)` → `str` alert id. Only a GTT that has not triggered can be cancelled.

`modify_gtt` and `cancel_gtt` return the ack's `al_id` exactly as the server sent it (never
stripped or replaced). The docs do not say it equals the id that was sent, so it may differ. When
it differs, the SDK logs one DEBUG line on the `tradesmart` logger,
`<Endpoint>: the acknowledged alert id differs from the one sent`, with no ids or other fields.

| Argument | Wire key | Values |
|---|---|---|
| `alert_type` | `ai_t` | `GttAlertType` (7.3): the 13 documented codes; the two OCO codes `LMT_BOS_O` / `LMT_SOB_O` are known to the enum but refused by `place_gtt` / `modify_gtt` (2.9) |
| `trigger_value` | `d` | a finite number (Decimal, int, float or numeric text; not a boolean); no sign or range rule (the docs give none: a % change can be negative). Not `trigger_price`, which stays the stop-loss trigger `trgprc` of the order |
| `alert_validity` | `validity` | `GttValidity`: `GTT` (default on place; none on modify) or `DAY` |
| `validity` | `ret` | `Validity` DAY (default on place; none on modify), EOS, IOC, as in `place_order` |
| `tag` | `remarks` | required, a non-blank string (the docs mark it Required; it is the key to find a GTT in `pending_gtts()` after `OrderStatusUnknownError`) |
| `alert_id` | `al_id` | a non-blank string (an integer is rejected, never converted) |
| `quantity` | `qty` | as `place_order`: in the exchange's trade units, a whole multiple of the lot size, on MCX too (owner, 2026-11-12; CRUDEOIL lot 100: 2 lots = 200; GOLD lot 1: 1 lot = 1). Sent as given |
| other order fields | `exch`, `tsym`, `trantype`, `prctyp`, `prd`, `prc`, `trgprc`, `dscqty` | as `place_order` |

Wire keys, in this order (the docs' examples; optional ones only when given): PlaceGTTOrder
`uid, actid, exch, tsym, ai_t, d, validity, remarks, trantype, prctyp, prd, ret, qty, prc[, trgprc][, dscqty]`;
ModifyGTTOrder the same with `al_id` third (`uid, actid, al_id, exch, ...`); CancelGTTOrder
`uid, al_id`. No `ordersource`, no `amo` and no `algo_id` (the docs list none of them for GTT;
`ClientConfig.algo_id` is not sent either). Vectors: `order_validation.json` (cases `gtt_*`).

Validation (raises `RequestValidationError`, nothing is sent), on place and modify:

| Rule | Field |
|---|---|
| `alert_type` resolves through `GttAlertType` and is not an OCO code (`LMT_BOS_O`, `LMT_SOB_O`: use `place_oco` / `modify_oco`, 2.9) | `ai_t` |
| `alert_validity` is `DAY` or `GTT` | `validity` |
| `exchange` is NSE, NFO, BSE, BFO or MCX (CDS and BCD refused: not documented for GTT) | `exch` |
| `product` is CNC (`C`), NRML (`M`) or CO (`H`); MIS, BO and MTF refused (not documented for GTT). A CO GTT needs no `book_loss_price`: the GTT request has no `blprc` | `prd` |
| `trigger_value` a finite number, booleans rejected | `d` |
| `tag` a non-blank string | `remarks` |
| `alert_id` a non-blank string (modify, cancel) | `al_id` |
| `quantity`, prices, `MKT`/`SL-MKT` price `0`, price > 0 for `LMT`/`SL-LMT`, `trigger_price` rules and `disclosed_quantity` ≤ `quantity`: exactly as `place_order` (2.4) | as in 2.4 |

The writes are order calls (5.2): never retried, success only on the exact word with a non-blank
`al_id` (4.2.1), an ambiguous outcome is `OrderStatusUnknownError` pointing to `pending_gtts()`,
outcome lines with `al_id=` (5.6). The reads are retried like every read (5.1); their empty
signal is in 4.3. Models `Gtt` and `TriggeredGtt` are in `models.md`; their fields are read by wire
key only (never by field name), so a row without `ret` leaves `validity` empty: the wire
`validity` (DAY/GTT) is `alert_validity`. The `request_time` of `GetEnabledGTTs` has its own format
(`DDMMYYYYHHMMSS`) and is not parsed.

### 2.9 OCO orders

Python reference: `endpoints/oco.py`, `models/oco.py`, `client.py`, `async_client.py`. Docs: the OCO
page of the TradeSmart API docs (local copy of 2026-11-18, `04-oco-orders.md`); observations: F35.
Names below are Python's; other languages use their own case style (2.1).

An OCO (one cancels the other) is a **multi-day exit with a target and a stop-loss**: two orders
wait on TradeSmart's server against one instrument, each behind its own trigger value; when the
last price reaches one trigger, that order is sent to the exchange and the other is cancelled.
Nothing reaches the exchange before then. It is held as an alert identified by `al_id`, like a GTT
(2.8), and is listed by `pending_gtts()` / `triggered_gtts()`.

- **A trigger that is already crossed when the OCO is placed may fire at once** (as a GTT, 2.8).
  The SDK does not read the last price and does not check the triggers against the market.
- `alert_validity` and `validity` as on a GTT (2.8): `GTT` keeps the OCO until one side triggers or
  it is cancelled, `DAY` ends it with the trading day.
- A `MARKET` leg may be refused by the exchange for API orders (F27); the SDK sends what it is given.

| Method | Endpoint | Parameters (Python types, defaults) | Returns |
|---|---|---|---|
| `place_oco(*, exchange, tradingsymbol, transaction_type, quantity, product, stop_loss, target, tag=None, validity="DAY", alert_validity="GTT")` | `PlaceOCOOrder` | `stop_loss`, `target`: `OcoLeg` | `str` alert id (`al_id`) |
| `modify_oco(alert_id, *, <the same keywords>, validity, alert_validity)` | `ModifyOCOOrder` | as `place_oco`; `validity` and `alert_validity` have **no defaults** (as `modify_gtt`: a default could silently turn a `DAY` OCO into a `GTT` one) | `str` alert id |
| `cancel_oco(alert_id)` | `CancelOCOOrder` | `alert_id`: a non-blank string | `str` alert id |

`modify_oco` sends the whole OCO again (the docs: every required field, both triggers and both
legs). `modify_oco` and `cancel_oco` return the ack's `al_id` exactly as the server sent it (the
GTT rule of 2.8, including the DEBUG line when it differs from the one sent).

`stop_loss` and `target` are an `OcoLeg` each (Python: a frozen request model exported from
`tradesmart`; other languages: a plain object type with the same fields):

| `OcoLeg` field | Wire (inside the leg) | Rule |
|---|---|---|
| `trigger_value` | the leg's `oivariable` item, `d` | a finite number (Decimal, int, float or numeric text; not a boolean), **greater than 0**; sent as given (digits kept) |
| `price` | `prc` | as `place_order` for the order type (2.4): required and > 0 for `LIMIT` / `SL-LMT`, `0` when omitted on `MARKET` / `SL-MKT` |
| `order_type` | `prctyp` | `LIMIT` (default, as the web terminal), `MARKET`, `SL`, `SL_M` (7.3): `LMT`, `MKT`, `SL-LMT`, `SL-MKT` |
| `trigger_price` | `trgprc` | as `place_order`: required for `SL-LMT` / `SL-MKT`, refused for the others. It is the stop-loss trigger of the **order** the leg places, not the OCO trigger value |

Shared by both legs (one instrument, one side, one size: an OCO protects one position):

| Argument | Wire key | Values |
|---|---|---|
| `exchange` | `exch` (top level and each leg) | NSE, NFO, BSE, BFO, MCX (as GTT) |
| `tradingsymbol` | `tsym` (top level and each leg) | as `place_order` |
| `transaction_type` | `ai_t` and each leg's `trantype` | BUY: `ai_t` `LMT_BOS_O`, `trantype` `B`; SELL: `LMT_SOB_O`, `S` |
| `quantity` | each leg's `qty` | as `place_gtt` (2.8): trade units, a whole multiple of the lot size, MCX included; an integer > 0, not a boolean |
| `product` | each leg's `prd` | CNC (`C`), NRML (`M`) or CO (`H`); MIS, BO and MTF refused (as GTT) |
| `validity` | each leg's `ret` | `Validity` DAY, EOS, IOC |
| `alert_validity` | top-level `validity` | `GttValidity`: `GTT` or `DAY` |
| `tag` | each leg's `remarks`, when given | optional (the docs mark the leg `remarks` optional); a non-blank string when given; whether list rows return it is open (F35) |
| `alert_id` | `al_id` | a non-blank string (modify, cancel; an integer is rejected, never converted) |

**Slots (F35 a).** The stop-loss is always sent as `x` and `place_order_params`, the target as `y`
and `place_order_params_leg2`, buy and sell, as TradeSmart's web terminal does. The docs' sell
example puts the target in `x`; whether the server cares is a question to TradeSmart (F35). The
SDK never swaps the slots.

**Side check (before sending).** A BUY OCO closes a short: the stop-loss trigger must be **above**
the target trigger. A SELL OCO closes a long: the stop-loss trigger must be **below** the target
trigger. Equal triggers are refused. This is the web terminal's rule without the last price. It
is checked only when both triggers are valid numbers; the refusal is `loc` `["oivariable"]`.

**Wire bodies** (key order is part of the contract; every number is a string; optional keys only
when given). `PlaceOCOOrder`:
`uid, tsym, exch, validity, ai_t, oivariable, place_order_params, place_order_params_leg2`, where
`oivariable` is the list `[{"var_name": "x", "d": <stop-loss trigger>}, {"var_name": "y", "d":
<target trigger>}]` and each leg is `tsym, exch, trantype, prctyp, prd, ret, actid, uid, qty, prc,
ordersource[, trgprc][, remarks]` with `actid` and `uid` both the client id and `ordersource`
`API` (the docs list it for legs and their example sends it). `ModifyOCOOrder`: the same with
`al_id` second, after `uid`. `CancelOCOOrder`: `uid, al_id`. There is no top-level `actid`,
`remarks` or `dscqty`, no `algo_id`, no `exchsym`. The nested bodies are encoded as in 3.2.
Vectors: `oco_requests.json` (exact payloads, key order significant, and every refusal with its
`loc`).

Validation (raises `RequestValidationError`, nothing is sent; `loc` is the wire path of the error):

| Rule | `loc` |
|---|---|
| `exchange` is NSE, NFO, BSE, BFO or MCX | `exch` |
| `product` is CNC, NRML or CO | `prd` |
| `quantity` an integer > 0, not a boolean | `qty` |
| `trigger_value` a finite number > 0, not a boolean | `oivariable`, 0 (stop-loss) or 1 (target), `d` |
| `order_type` resolves through `OrderType` | `place_order_params` or `place_order_params_leg2`, `prctyp` |
| `price` rules of the order type | the leg key, `prc` |
| `trigger_price` required for `SL-*`, refused otherwise | the leg key, `trgprc` |
| the side check above | `oivariable` |
| `alert_id` a non-blank string (modify, cancel) | `al_id` |

`place_gtt` and `modify_gtt` refuse `alert_type` `LMT_BOS_O` / `LMT_SOB_O` (`RequestValidationError`
on `ai_t`, the message naming `place_oco` / `modify_oco`): the docs say an OCO is modified and
cancelled only with the OCO endpoints. `GttAlertType` has the two codes (`OCO_BUY`, `OCO_SELL`, 7.3)
so that list rows parse to them. `cancel_gtt` cannot tell an OCO alert id from a GTT's without a
lookup: not checked.

**Outcome (5.2, 4.2.1).** The three names are state-changing: sent exactly once, never retried,
timeouts and ambiguous failures as 5.2, outcome lines with `al_id=` (5.6), the unknown-outcome
warning points to `pending_gtts()`. Success words (letter case ignored, `al_id` non-blank, no
non-blank `emsg`): `PlaceOCOOrder` `OI created`, `ModifyOCOOrder` `OI replaced`, `CancelOCOOrder`
`OI deleted`. **`Invalid Oi`** (any letter case) on `ModifyOCOOrder` / `CancelOCOOrder` is a
**definite failure**, `OrderRejectedError` with the message `Invalid Oi` (the test environment
answers it, with an `al_id` and no `emsg`, for an alert that no longer exists, F35 b); on
`PlaceOCOOrder` it is not documented, so it is `OrderStatusUnknownError`. The docs' rule "treat
`al_id` and no `emsg` as success" is **not** followed: `Invalid Oi` shows why. Anything else
(another word, `Ok`, a list, no `al_id`) is `OrderStatusUnknownError`. No rate group (the docs give
none).

**Reading back.** `pending_gtts()` lists OCO rows, `triggered_gtts()` lists a fired or cancelled
one (a cancelled OCO has the row `stat` `CANCELLED`, as a cancelled GTT, F32 / F35 c). `Gtt` and
`TriggeredGtt` (`models.md`) have these fields, `null` on a plain GTT row:

- `oco_first` / `oco_second` (`OcoLegRow`): the legs `place_order_params` /
  `place_order_params_leg2` (fields `transaction_type`, `order_type`, `quantity` a JSON number or
  string kept as an int, `price`, `trigger_price` when present, `product`, `account_id`,
  `ordersource`, `tag`; everything else, such as `s_prdt_ali`, `C` and `ipaddr`, stays an extra).
  A leg carries **no trigger value**: the triggers are in `oco_triggers`.
- `oco_triggers` (list of `OcoTrigger`, `var_name` and `trigger_value`): the `oivariable` items.
  The trigger of a leg is the item whose `var_name` is `x` for the first leg and `y` for the
  second, matched by `var_name`, never by position. List rows return the values with two decimals
  (`"1400.00"`).
- Read-only helpers: **`stop_loss` and `target`** each give an **`OcoSide`** (`models.md`: `leg`, an
  `OcoLegRow`, and `trigger_value`) or `None`, chosen **by trigger value and side, not by slot**.
  The side is decided by the row's `alert_type`: for `OCO_BUY` the leg with the **higher**
  trigger is the stop-loss and the lower one the target; for `OCO_SELL` the leg with the **lower**
  trigger is the stop-loss. The leg's own `trantype` is **not** used for the choice. So an OCO
  placed by another front end, in either slot order (the docs' example has the target in `x`),
  reads correctly. If `alert_type` is not an OCO code (a plain GTT row), the two triggers are
  equal, or either trigger or leg is missing: both are `None`. `is_oco` is true when `alert_type`
  is one of the two OCO codes.
- The row's own top-level `d`, `trantype`, `prctyp`, `qty` and `prc` are leg 1's values copied by
  the server and keep their GTT meaning; for an OCO read the leg fields.

A nested record that fails to parse (a leg, or the trigger list) is `null` as a whole; the row itself
still parses (vectors: `oco_rows.json`).

**Offline broker (16).** It accepts the three endpoints, keeps OCOs with the GTTs, answers the
observed words and `Invalid Oi` for an unknown or already cancelled alert, and lists a cancelled
OCO as `CANCELLED`. It does **not** trigger an OCO: no fired OCO has been observed, so the fired
row and order shapes would be invented; `trigger_gtt` on an OCO alert is an error that says so.

Fixtures: `place_oco_success.json`, `modify_oco_success.json`, `cancel_oco_success.json`,
`modify_oco_invalid.json`, and OCO rows in `pending_gtts_success.json` /
`triggered_gtts_success.json`.

---

## 3. Transport

Python reference: `_transport/sync_http.py`, `_transport/async_http.py`, `_transport/codec.py`,
`_transport/ratelimit.py`, `config.py`.

### 3.1 Endpoints and headers

| Item | Value |
|---|---|
| REST base | `https://v2api.tradesmartonline.in/NorenWClientAPIv2` (trailing `/` stripped) |
| REST call | `POST {base}/{Endpoint}` for every endpoint |
| WebSocket | `wss://v2api.tradesmartonline.in/NorenWSAPI/` |
| Authorize | `https://v2api.tradesmartonline.in/OAuthlogin/authorize/oauth` |
| Script master | `GET https://v2api.tradesmartonline.in/{EXCH}_symbols.txt.zip` |
| `Authorization` | `Bearer <access_token>` on every REST call except `GenAcsTok` through `generate_session()` (none); `request()` always sends it (2.5) |
| `User-Agent` | `tradesmart-<lang>/<version>` (Python: `tradesmart-python/2.0.0`) on REST, WebSocket handshake and script-master downloads. A configured `user_agent` without this token gets it appended after a space. `user_agent` must be printable ASCII (U+0020 to U+007E): a control or non-ASCII character → `ConfigurationError` when the configuration is created (value never echoed). |
| Content-Type | none required |

All base URLs are configurable (12); `http(s)://` and `ws(s)://` schemes are validated.

**Never follow HTTP redirects** (Required), on REST calls and script-master downloads alike, and
also when the caller injected an HTTP client configured to follow them (pass the "do not follow"
option on every request, not only when building the client). A 3xx response is classified like
any other status (4.2): a non-JSON body is `OrderStatusUnknownError` on a state-changing call and
`ResponseParseError` on a read or a script-master download. Nothing is ever re-sent to the
redirect target. (Many HTTP libraries follow redirects by default and re-send a POST body on
307/308, possibly to another host: that must not happen to an order.) Vectors:
`not_ok_classification.json` (`redirect_*`).

### 3.2 `jData` body encoding

1. Serialise the payload as compact JSON (`","` and `":"` separators, no spaces) with the keys
   in the payload's own order (the wire key orders of 2.3 and 2.4; never sorted). Every value is
   a string, or a nested object or list of such values (the OCO bodies, 2.9: `oivariable` is a
   list of objects, each leg an object): keys and items keep their order and every string at
   any depth follows the rules below. Strings use exactly these escapes: `"` → `\"`, `\` → `\\`; U+0008, U+0009,
   U+000A, U+000C, U+000D → `\b`, `\t`, `\n`, `\f`, `\r`; every other character below U+0020 →
   `\u00XX` with **lower-case** hex (`\u0001`, `\u001f`). Everything else is written as itself
   in UTF-8: non-ASCII characters (no `\uXXXX`, "ensure_ascii" off), DEL (U+007F) and U+2028 /
   U+2029 (some JSON libraries escape these two; do not), and **no HTML escaping** (`<`, `>`,
   `&`, `'` are not turned into `<`, `&lt;` and the like, which some JSON libraries do by
   default).
2. In the JSON text replace, in this order, `%` → `%25`, `&` → `%26`, `+` → `%2B`. Nothing else
   is escaped (the server URL-decodes the body; `M&M` must survive). `=`, space, `/`, `?`, `#`
   are sent as is.
3. Body = `jData=` + that text, sent as UTF-8 bytes. The bytes are produced before anything is
   sent (5.2): a key or value, **at any depth**, that cannot be encoded as UTF-8 (in Python a
   lone surrogate, for example from a mis-decoded file name) raises `RequestValidationError`
   with one `errors` entry per top-level key that holds one (`type` `string_unicode`, that
   top-level key as field: a nested value is named by its top-level key) and nothing is sent.

Example: `{"uid":"TEST123","tsym":"M&M-EQ"}` → `jData={"uid":"TEST123","tsym":"M%26M-EQ"}`.
Vectors (exact text and bytes): `jdata_encoding.json`.

### 3.3 Timeouts and connection pool

| Setting | Default | Notes |
|---|---|---|
| Connect timeout | 5 s | |
| Read timeout | 10 s | |
| Write timeout | = read timeout | All calls. |
| Pool-wait timeout, reads | = read timeout | |
| Pool-wait timeout, state-changing calls | = connect timeout | A pool timeout proves nothing was sent: the order fails fast and definitively. |
| Keep-alive expiry | 50 s | Below the server's measured idle timeout of 55–65 s (F6, measured live). |
| Max connections / keep-alive connections | 10 / 5 | Placeholders until TradeSmart states a limit (F7). |

One connection pool per client instance; keep connections warm (live warm `GetQuotes` p50 ≈ 11 ms).

### 3.4 Client-side rate limits

Sliding windows per client instance and per group; a call is recorded only when allowed.

| Group | Endpoints | Limits |
|---|---|---|
| orders (`place_order`) | `PlaceOrder`, `PlaceMultiOrder` | `order_rate_limit_per_second` (default 10, 1 to 30; above 10 needs `algo_id`) / 1 s and `order_rate_limit_per_minute` (default 200, 1 to 600) / 60 s |
| quotes | `GetQuotes`, `GetQuotesMultiple` | 100 / 1 s and 1000 / 60 s |
| tpseries | `TPSeries` | 100 / 1 s and 1000 / 60 s |
| — | everything else (incl. Modify/Cancel) | none |

| Mode | Behaviour |
|---|---|
| `wait` (default) | Sleep until the window allows the call. For the orders group the total wait is capped at `order_rate_limit_max_wait` (1 s); if the needed wait exceeds it, raise `RateLimitError(retry_after=<wait>, http_status=None)` and send nothing. |
| `raise` | Raise `RateLimitError` (with `retry_after`) at once. |

The official docs say 30 per second and 600 per minute for orders; the owner reports that most client accounts are limited to 10 orders per second, and that a higher rate needs an exchange-approved strategy and its algo ID, which the TradeSmart backend checks. The SDK therefore defaults to 10 per second and lets the caller raise it to at most 30 with `order_rate_limit_per_second`. The docs name the field: `algo_id` on `PlaceOrder` and `ModifyOrder`, "Required when placing more than 10 orders per second" (F29, resolved; 2.4). A configuration with `order_rate_limit_per_second` above 10 and no `ClientConfig.algo_id` is refused with `ConfigurationError` when it is built (12). The per-minute window defaults to 200, not the documented 600, because production refused an order on 14 Nov 2026 with "Order Recieved 202 in a current minute exceeds Limit 200 for user" (F33); `order_rate_limit_per_minute` (an integer 1 to 600, never below `order_rate_limit_per_second`) raises it if TradeSmart confirms a higher limit for the account. Only PlaceOrder and PlaceMultiOrder count in this window (F33 asks TradeSmart whether ModifyOrder, CancelOrder and ExitSNOOrder do too). With the default `order_rate_limit_max_wait` of 1 s a call that would wait longer than that for the minute window raises `RateLimitError` with `retry_after` set to the wait and nothing is sent. The message keeps the prefix `client-side rate limit for <group>` and names the window that refused, for example `client-side rate limit for place_order (200 per minute)` or `(10 per second)`; `place_sliced_order`'s up-front refusal names the minute window.

The limiter is acquired before each attempt, including retries. A client-side `RateLimitError`
is not retried.

---

## 4. Response parsing and errors

Python reference: `_transport/codec.py`, `errors.py`, `endpoints/base.py`.

### 4.1 Error taxonomy

| Error | Parent | Meaning | Definitive? |
|---|---|---|---|
| `TradeSmartError` | — | Base of every SDK error | — |
| `ConfigurationError` | TradeSmartError | Bad configuration, missing keys, closed client, bad `request()` name | yes (nothing sent) |
| `RequestValidationError` | TradeSmartError | Parameters failed client validation; `errors` list (below) | yes (nothing sent) |
| `SubscriptionLimitError` | TradeSmartError | > 1000 stream subscriptions | yes |
| `TransportError` | TradeSmartError | Network-level base | — |
| `NetworkError` | TransportError | Could not connect / connection lost | read: yes; order: only when the connection never opened |
| `RequestTimeoutError` | TransportError | Timed out | read: yes; order: only connect/pool timeout |
| `OrderStatusUnknownError` | TradeSmartError | A state-changing request may or may not have executed; `endpoint`, `message`, `http_status`, `body` (≤ 2000 chars, redacted), `sliced_result` (`None` unless raised by `place_sliced_order`, 5.7) | **ambiguous** |
| `APIError` | TradeSmartError | API error; `message`, `endpoint`, `http_status`, `emsg`, `request_time`, `raw` (redacted) | yes |
| `AuthenticationError` | APIError | Missing, invalid or expired credentials | yes |
| `RateLimitError` | APIError | Server 429 or client limiter; `retry_after` seconds or `None`; `endpoint` is the endpoint name (`PlaceOrder`) for the client limiter too, the group appears only in the message | yes |
| `NotFoundError` | APIError | HTTP 404 | yes |
| `ServerError` | APIError | HTTP 5xx on a read | yes |
| `OrderRejectedError` | APIError | `Not_Ok` on a state-changing call | yes (not accepted) |
| `ResponseParseError` | TradeSmartError | Body is not the expected JSON; `body` ≤ 2000 chars, redacted | yes (reads only) |

`RequestValidationError.errors` holds one entry per failed check, each an object with:
`type` (a short machine-readable kind, for example `value_error`, `enum`, `greater_than_equal`,
`string_too_short`, `string_unicode`), `loc` (the path of the failing field as a list of names:
the **wire key**, for example `["qty"]`; empty for a rule that spans several fields, such as
"MTF is not offered for MCX"), and `msg` (an English description that never contains the
value). Python's entries are pydantic error dictionaries and may carry more keys (`input`,
`ctx`); other SDKs need only `type`, `loc` and `msg`, and the `type` words are informative (only
the `loc` wire keys are pinned by vectors: `fields` in `order_validation.json` and
`read_payloads.json`). The error's text starts with `"Invalid request parameters: "` and names
the wire keys (or a cross-field rule's message); it never contains a parameter value.

On the REST call path (transport-error mapping and response parsing), SDK errors must not chain
the underlying exception (Python: `raise ... from None`, so neither `__cause__` nor the implicit
context is shown): its message or attributes can hold raw values, the unredacted body or the
request with its `Authorization` header. This covers validation errors from parsing (only field
names and a redacted body are kept), errors mapped from REST transport failures (5.2) and errors
for non-JSON bodies. `KeyboardInterrupt`, `SystemExit` and the async cancellation pass through
unchanged. Elsewhere the Python reference still chains (`raise ... from exc`) where the cause
carries no token: script-master download, zip and UTF-8 errors (no `Authorization` header is
sent there), request-parameter validation, the stream connect errors and `generate_session()`
(whose `APIError` is already redacted). Other SDKs may keep or drop those causes.

### 4.2 Classification

Always parse the body, whatever the HTTP status. `stat` is compared exactly (`"Ok"`, `"Not_Ok"`,
and the per-endpoint success words of 4.2.1: no case folding, no trimming; a `stat` that is not a
string never matches). In rows 8 and 9 "`stat == "Ok"`" means "`stat` is the endpoint's success
word" (and the extra GTT conditions of 4.2.1 hold). Evaluate top to bottom; "status class"
means 429 → `RateLimitError`, 401/403 → `AuthenticationError`, ≥ 500 → `ServerError`.

| # | Body | Condition | Result |
|---|---|---|---|
| 1 | Not JSON | state-changing | `OrderStatusUnknownError` |
| 2 | Not JSON | read, status class applies | that class |
| 3 | Not JSON | read, otherwise | `ResponseParseError` |
| 4 | JSON array | state-changing and (status ≥ 400 or endpoint expects an object) | `OrderStatusUnknownError` |
| 5 | JSON array | status ≥ 400 | status class, else 404 → `NotFoundError`, else `APIError` |
| 6 | JSON array | endpoint expects a list, or array is empty and endpoint allows empty | return the array |
| 7 | JSON array | otherwise | `ResponseParseError` |
| 8 | Object, `stat == "Ok"` | endpoint expects an object | return it |
| 9 | Object, `stat == "Ok"` | endpoint expects a list | state-changing: `OrderStatusUnknownError` (the request may have been executed); read: `ResponseParseError` |
| 10 | Object, `stat == "Not_Ok"` | endpoint allows empty and `emsg` contains `no data` (case-insensitive) | `[]` |
| 11 | `Not_Ok` | HTTP 401/403, or `emsg` matches `session expired`, `invalid access token`, `invalid session`, `invalid user id`, `invalid acct id` (case-insensitive) | `AuthenticationError` |
| 12 | `Not_Ok` | HTTP 429 | `RateLimitError` |
| 13 | `Not_Ok` | HTTP ≥ 500 | state-changing: `OrderRejectedError`; read: `ServerError` |
| 14 | `Not_Ok` | HTTP 404 | `NotFoundError` |
| 15 | `Not_Ok` | state-changing | `OrderRejectedError` |
| 16 | `Not_Ok` | otherwise | `APIError` |
| 17 | Any other JSON (object without valid `stat`, scalar) | state-changing | `OrderStatusUnknownError` |
| 18 | Any other JSON | read | status class, else `ResponseParseError` |

`APIError.message` (rows 11–16) = `"<Endpoint> failed: <emsg>"` when `emsg` is a string that
is not blank, else `"<Endpoint> failed (no error message from the server)"` (`emsg` missing,
`null`, blank or not a string). The `emsg` attribute keeps the server's string as is (possibly
blank); a missing or non-string `emsg` gives `emsg = null`. Other `APIError`s (status class on a
body without `stat`) read `"<Endpoint> failed: HTTP <status>"`.
On a 429 a finite numeric `Retry-After` header sets `retry_after` (5.1). A 3xx status is not a
status class: it falls through to the rows like a 2xx (redirects are never followed, 3.1). The
vectors are in `not_ok_classification.json` (the error type, `emsg` and `message`).

Deeply nested bodies (Python: one that makes the JSON parser or the redaction recurse too
deeply, `RecursionError`): a body too deep to **parse** counts as not valid JSON (rows 1 to 3);
a body that parses but is too deep to **redact** keeps its classification, and the error's
`raw` / `body` is the fixed text `"<response nested too deeply to show>"` instead of the
payload; a body too deep for the model parser is `OrderStatusUnknownError` on a state-changing
call and `ResponseParseError` on a read. Never a language-level stack overflow, never the
unredacted body. SDKs whose parsers have no depth limit need no special case.

#### 4.2.1 Per-endpoint success words

Owner decisions (2026-11-12, amended 2026-11-13 and 2026-11-18): only the documented or the
observed word is a success. The six **GTT and OCO** words compare **ignoring letter case only**
(Unicode case folding on both sides, Python `casefold()`; nothing trimmed, no whitespace or
punctuation normalised): the test environment answers `OI created` for the documented
`Oi created` (F32), and a case variant of a word carries no other meaning. Every other word, the
iceberg `true` and `false` included, stays exact and case-sensitive. **The cancel word** of both
`CancelGTTOrder` and `CancelOCOOrder` is **`OI deleted`**, the word the test environment answers
(F32, F35 b); the documented `Oi delete success` is **no longer a success**: it is
`OrderStatusUnknownError` (the caller confirms in `pending_gtts()`). Risk accepted by the owner
(2026-11-18): if production answers the documented word, production cancels report unknown until
the SDK is changed (open question to TradeSmart, F35).

| Endpoint | Success `stat` | Also required for success | Definite failure |
|---|---|---|---|
| `PlaceGTTOrder` | `Oi created` | `al_id` a non-blank string; `emsg` missing, `null` or a blank string | `Not_Ok` (rows 11–16; row 10 needs an endpoint that allows empty, which these writes never do, so a `Not_Ok` "no data" goes by rows 11–16 too: `OrderRejectedError` at HTTP 200) |
| `ModifyGTTOrder` | `Oi replaced` | same | `Not_Ok`; `Invalid Oi` (below) |
| `CancelGTTOrder` | `OI deleted` | same | `Not_Ok`; `Invalid Oi` (below) |
| `PlaceOCOOrder` | `OI created` | same | `Not_Ok` |
| `ModifyOCOOrder` | `OI replaced` | same | `Not_Ok`; `Invalid Oi` (below) |
| `CancelOCOOrder` | `OI deleted` | same | `Not_Ok`; `Invalid Oi` (below) |
| `PlaceIceBergOrder`, `ModifyIceBergOrder`, `CancelIceBergOrder` | `true` (the string) | — | `Not_Ok`, classified as in 4.2 (message from `emsg`); or `false` (these three names only): classified the same way, always definite (401/403 → `AuthenticationError`, 429 → `RateLimitError`, 404 → `NotFoundError`, else `OrderRejectedError`), the message from a non-blank `rejreason`, else `emsg` (`emsg` attribute = that text) |
| every other endpoint | `Ok` | — | `Not_Ok` |

**`Invalid Oi`.** A reply whose `stat` is `Invalid Oi` (letter case ignored by the same
`casefold()` rule, nothing trimmed), decided by the `stat` alone, whatever `al_id` and `emsg` the body
holds (missing, blank or not) and at any HTTP status, on **`ModifyGTTOrder`, `CancelGTTOrder`, `ModifyOCOOrder` or
`CancelOCOOrder`**, is a **definite failure**: `OrderRejectedError` with the message exactly
`Invalid Oi` (not the usual `<Endpoint> failed: ...` form) and `emsg` the `stat` text as received.
The docs name it for a modify "when the OCO can't be modified"; the test environment answers it
for a modify or cancel of an alert that no longer exists, with an `al_id` and **no** `emsg`
(F35 b), so the docs' "an `al_id` and no `emsg` is success" is not followed. This check comes
before the success check. On `PlaceOCOOrder`, `PlaceGTTOrder` and every other endpoint it is not a
failure word: `OrderStatusUnknownError` on a state-changing call, `ResponseParseError` on a read.
It is checked before row 17 and after the JSON checks (a non-JSON body is still row 1).

Anything else on these state-changing endpoints is row 17, `OrderStatusUnknownError`: `Ok`,
another endpoint's word, another word (the documented cancel word `"Oi delete success"`, F32 / F35 b),
other spacing (`"Oi  created"`, `" Oi created"`, `"Oi created "`), an
iceberg word in another case (`"True"`),
a JSON boolean, a missing `stat`, a GTT or OCO word without a non-blank string `al_id` (a numeric
`al_id` included) or with an `emsg` that is not missing, `null` or a blank string (a number, a
boolean, a list or non-blank text). Any JSON **array** body on these nine endpoints is
`OrderStatusUnknownError` too, whatever status or `expect` (including `request(...,
expect="list")`): they acknowledge with an object. The `false` rule belongs to the three iceberg
names; another endpoint whose success word is `true` would not inherit it. A false success is
the dangerous error (the caller would believe a GTT exists); a false "unknown" only makes the
caller check `pending_gtts()`.
The same table applies through `request()` (2.5). Iceberg methods are on hold: only `request()`
reaches the iceberg writes. Vectors: `success_words.json`.

### 4.3 Endpoint shapes and the empty signal

| Endpoint | Expects | Allows empty | Notes |
|---|---|---|---|
| `OrderBook`, `SingleOrdHist`, `TradeBook`, `PositionBook`, `Holdings`, `TPSeries` | list | yes | |
| `EODChartData` | list | yes | Elements are JSON-encoded strings; decode each (objects accepted as is). |
| `SearchScrip`, `GetOptionChain` | object | yes | Rows from `values`; a missing `values` list → `ResponseParseError`. SearchScrip no-match is HTTP 404 `Not_Ok "No Data"` → `[]` (row 10 precedes row 14). |
| `GetPendingGTTOrder`, `GetTrgOi` | list | yes | Empty list as `[]` or `Not_Ok` "no data" (shape undocumented). |
| `GetEnabledGTTs` | object | yes | Families from `ai_ts` (each item an object with a string `ai_t`, else `ResponseParseError`); `ai_ts` missing or `null` → `[]`. |
| all others | object | no | "no data" is an error. |

Both empty forms (`Not_Ok` + "no data", and a bare `[]`) give an empty list. A success object
on a list endpoint is never wrapped into a list. Every list item (and every `values` row) must be
a JSON object: any other item (`["x"]`, `values: [1]`) raises `ResponseParseError`
("Could not parse `<Endpoint>` response (expected a JSON object)", `body` = the redacted item),
never a language-level type error. This table is for the typed methods; `request()`
uses the caller's `expect`, never allows empty and does no decoding (2.5).

The empty check comes **before** the authentication check (row 10 before row 11): on an
allow-empty endpoint a `Not_Ok` "no data" reply is `[]` even with HTTP 401/403. A non-JSON body
with HTTP 404 on a read is `ResponseParseError`, not `NotFoundError` (404 is not a status class;
rows 2–3).

---

## 5. Retry and order safety

Python reference: `_transport/retry.py`, `_transport/outcome.py`, `endpoints/orders.py`,
`_client_core.py`.

### 5.1 Retries (reads only)

| Item | Rule |
|---|---|
| Retried errors | `NetworkError`, `RequestTimeoutError`, `RateLimitError` (server 429), `ServerError`. Vectors: `retry_schedule.json` |
| Count | `max_retries` = 2 retries (3 attempts) |
| Delay | With a finite `retry_after`: `max(0, min(cap, retry_after))` (a negative value means "now"; no jitter). Else (none, NaN or infinite) `min(cap, base · 2^attempt) · (0.5 + U/2)`, `U` uniform in [0, 1), `attempt` 0-based; base 0.5 s, cap 8 s |
| `Retry-After` | Read only on an HTTP 429 `RateLimitError`, only as a number of seconds; a non-finite value (`nan`, `inf`) or an HTTP date leaves `retry_after = null` |
| Never retried | Every state-changing call (5.2), every other error type, and any error other than an HTTP-library transport error raised by the caller's own HTTP event hook or transport (below; an HTTP-library transport error raised there, e.g. a connect error, is mapped and retried like any other) |
| Caller's hook or transport on a read | An SDK error (`TradeSmartError`) raised there passes through unchanged; any other exception (not an HTTP-library transport error) becomes `NetworkError("<Endpoint>: unexpected error (<TypeName>)")`. Neither is retried: such an error is deterministic. |

### 5.2 State-changing calls

`PlaceOrder`, `PlaceMultiOrder`, `ModifyOrder`, `CancelOrder`, `ExitSNOOrder`, `GenAcsTok`,
`PlaceGTTOrder`, `ModifyGTTOrder`, `CancelGTTOrder`, `PlaceOCOOrder`, `ModifyOCOOrder`,
`CancelOCOOrder`, `PlaceIceBergOrder`, `ModifyIceBergOrder`,
`CancelIceBergOrder`, and any `request(..., state_changing=True)`: sent **exactly once**, never
retried.

Transport failure mapping for a state-changing call:

| Failure | Error |
|---|---|
| Connect timeout | `RequestTimeoutError` (definitive) |
| Connect refused / DNS / TLS before sending | `NetworkError` (definitive) |
| Pool-wait timeout | `RequestTimeoutError` (definitive) |
| Read/write timeout, connection reset, protocol or decoding error, any other HTTP-library transport error | `OrderStatusUnknownError` |
| Any other exception while sending (raised by the caller's event hook, e.g. a `raise_for_status` response hook, or a custom transport; an SDK error raised there included) | `OrderStatusUnknownError("<TypeName> during the request; check orders() before retrying")` (`check pending_gtts()` for the GTT writes); the original exception's text is never used |
| Async task cancelled while the request is in flight | re-raise the cancellation; log outcome `unknown` |
| `KeyboardInterrupt` / `SystemExit` (and any other non-`Exception` error) | pass through unchanged; logged `unknown` when raised after the send (5.6) |

For reads: timeouts → `RequestTimeoutError`, other transport failures → `NetworkError`; an error
from the caller's hook or transport follows 5.1. Redirects are never followed (3.1), so a 3xx
reaches the classification of 4.2 instead of causing a second request. Vectors (error and number
of requests per failure kind, for an order, a state-changing `request()` and a read):
`transport_failures.json`.

**Build before send** (Required). Every request is fully built (the body encoded to bytes, every
header value checked) **before** the call counts as sent, so a failure to build it is definitive
and is never mistaken for a send-time error:

| Build failure | Error | Outcome (5.6) |
|---|---|---|
| A payload key or value that is not valid UTF-8 text (3.2) | `RequestValidationError` (`string_unicode`, the key as field) | `not_sent`, never retried |
| A header value that cannot be sent: a non-ASCII or control character from the injected HTTP client's own headers (the token and `user_agent` are already checked when set, 6.3 and 12) | `ConfigurationError("<Endpoint>: the request could not be built (<reason>); check the access token, user_agent and the http_client's headers; nothing was sent")`, value never shown | `not_sent`, never retried |

Only then is the request marked as sent and handed to the HTTP library.

### 5.3 `OrderStatusUnknownError` rules

Raised when the server may have executed the request: an ambiguous transport failure (5.2), a
non-JSON / unexpected-shape / array body on a state-changing call (4.2 rows 1, 4, 17), an `Ok`
object where a state-changing `request()` expects a list (row 9), or an `Ok` acknowledgement that
cannot be parsed, or a reply to a GTT, OCO or iceberg write that is not its exact success word (4.2.1;
`Invalid Oi` on a modify or cancel is a rejection, not unknown).
Its message tells the caller to check `orders()` (`pending_gtts()` for the GTT and OCO writes) before
retrying. Callers must never resend automatically.

Examples and guides shipped with an SDK must show the safe pattern: every example order carries
a `tag` (`remarks`, unique per order, generated when the user gives none), and after an
`OrderStatusUnknownError` the example looks the order up by that tag in `orders()` (a few times,
with a short pause) and reports what it found; it never sends the order again. (Python:
`examples/place_order.py --tag`, `examples/order_lifecycle.py`.)

### 5.4 Acknowledgements

| Endpoint | Return value |
|---|---|
| `PlaceOrder` | `norenordno` as received (numbers coerced to string; **not trimmed**). Missing, blank or whitespace-only, or ack unparseable → `OrderStatusUnknownError`. |
| `ModifyOrder` | `norenordno`, else the ack's `result`, else the caller's `order_id` (real success shape: open item F9); the ack values are trimmed, and a blank one is skipped. An unparseable `Ok` ack → `OrderStatusUnknownError`. |
| `CancelOrder`, `ExitSNOOrder` | The caller's `order_id`. An unparseable `Ok` ack → `OrderStatusUnknownError`. |

**Live:** an `Ok` reply means TradeSmart **accepted the request**, not that the order is open,
cancelled or filled. Checks can refuse it moments later; the final state is only in
`orders()` / `order_history()` / stream order updates (F23).

### 5.5 Cancel and quantity differences (live, F23, F21)

| Case | NSE / NFO | MCX |
|---|---|---|
| New order failing RMS/exchange checks | `Ok` with order id, then `REJECTED` with `rejreason` | same (MCX API trading pending: F19) |
| Cancel of an order that is no longer open | `Ok` (`result` = order id), then refused: `rejreason` "SAF:order is not open to cancel" | `Not_Ok` "ORA:Order not found to Cancel" in the reply → `OrderRejectedError` |
| Quantity unit | NFO `qty` is in **units** (NIFTY lot 65 → `qty=65` for one lot) | **Units** too, a whole multiple of the lot size (CRUDEOIL lot 100 → `qty=100` for one lot, `qty=200` for two; GOLD lot 1 → `qty=1`); owner, 2026-11-12, F21 resolved |

MCX quantity evidence (2026-11-12): on UAT a CRUDEOIL order with `qty=1` was rejected
"ORA:Quantity 1 is not a multiple of lot size 100.00". Production `security_info()` and the
script master (read-only) give, in the same trade units as the lot size: CRUDEOIL lot 100 (BBL),
`frzqty` 2500; CRUDEOILM lot 10 (BBL), `frzqty` 2500; GOLD lot 1 (KG), `frzqty` 1; GOLDM lot 100
(g), `frzqty` 1500; SILVER lot 30 (KG), `frzqty` 90; SILVERM lot 5 (KG), `frzqty` 100. The SDK
sends `quantity` as given on every exchange (no lots argument, no conversion).

**Price factor and units (informational; no SDK behaviour).** `security_info()` also returns the
price factor `prcftr` (the "multiplier": value = price × quantity × `prcftr`), the price-quote
quantity `prcqqty` and unit `prcunt`, and the trade unit `trdunt`; `mult` was 1 on every MCX
contract read. GOLD is quoted per 10 g (`prcqqty` 10, `prcunt` GRMS) with `prcftr` 100 (script
master `GNGD` 100), so 1 lot (1 KG) at price P is worth P × 1 × 100; GOLDM has `prcftr` 0.1 and
CRUDEOIL `prcftr` 1. The SDK passes these fields through unchanged.

### 5.6 Order outcome log

Logger `tradesmart.orders`, only for `PlaceOrder`, `PlaceMultiOrder`, `ModifyOrder`,
`CancelOrder`, `ExitSNOOrder` and the GTT, OCO and iceberg writes (`PlaceGTTOrder`, `ModifyGTTOrder`,
`CancelGTTOrder`, `PlaceOCOOrder`, `ModifyOCOOrder`, `CancelOCOOrder`, `PlaceIceBergOrder`,
`ModifyIceBergOrder`, `CancelIceBergOrder`, also through
`request()`; not `GenAcsTok`, not other `request()` calls):

- One INFO line per call that reaches the logged block (exceptions below):
  `<Endpoint> <result> order_id=<id> in <ms> ms`.
  `<id>` = the ack's order id, else the payload's `norenordno`, else `-`. For the GTT and OCO writes the
  key is `al_id` instead of `order_id`: `<Endpoint> <result> al_id=<id> in <ms> ms`, `<id>` = the
  ack's `al_id`, else the payload's `al_id`, else `-` (e.g. `PlaceGTTOrder accepted
  al_id=26111200000010 in 41 ms`). The time includes any
  rate-limit wait. `<result>` is one of four words, decided in this order:

  | Word | When |
  |---|---|
  | `accepted` | the call returned (an `Ok` acknowledgement) |
  | `unknown` | `OrderStatusUnknownError` (checked first among the failures, whatever the send progress) |
  | `not_sent` | the request provably never reached the server: no token or an expired `AccessToken`, a client-side `RateLimitError` (raise mode, or a wait longer than the `order_rate_limit_max_wait` cap, 1 s by default), a closed client, or a connect-phase failure (connect error, connect timeout, pool-wait timeout) |
  | `rejected` | any other SDK error after the request was sent: the server answered with an error (`OrderRejectedError`, `AuthenticationError`, a server 429 `RateLimitError`, ...) |

  The "closed client" and "token" checks run inside the logged block, so they log `not_sent`.
  A `RequestValidationError` from the parameters is raised while the call is built, before the
  logged block, so **invalid parameters log no line**. A failure to build the request inside the
  logged block (5.2: a payload that is not valid UTF-8, an unsendable header) logs `not_sent`.
- `OrderStatusUnknownError` adds one WARNING: `<Endpoint>: order status unknown; check orders() before acting`
  (`check pending_gtts() before acting` for the GTT and OCO writes).
  `not_sent` has no WARNING.
- Interruptions (not SDK errors): an async cancellation or a sync `KeyboardInterrupt` /
  `SystemExit` (any non-`Exception` error, async included) **after** the request was handed to
  the HTTP library logs `unknown` plus the WARNING `<Endpoint>: outcome unknown - check orders()`
  (`check pending_gtts()` for the GTT and OCO writes),
  then the interruption propagates unchanged. An interruption **before** that point (for example
  during a rate-limit wait) logs no line: nothing was sent. The async cancellation line measures
  the time from the start of the send (the rate-limit wait is not included); every other line
  measures from the start of the call.
- Vectors (the INFO line without its time, the WARNING, the error and the number of requests
  per kind of result): `outcome_words.json`.
- Never log payloads, prices, symbols or tokens (for GTT: never `d`, prices, symbols or `remarks`).

### 5.7 Sliced orders

Python reference: `_slicing.py` (the planner and `SlicedOrderResult`), `client.py`,
`async_client.py`.

An order larger than the exchange freeze limit is split by the SDK into slices that each fit
within the limit, placed one after another as ordinary `PlaceOrder` calls. TradeSmart API V2 has
no slicing endpoint (`PlaceMultiOrder` exists but its schema is undocumented, F1), so slicing is
client-side.

`place_sliced_order(*, exchange, tradingsymbol, quantity, transaction_type, product, order_type, price=None, trigger_price=None, validity="DAY", disclosed_quantity=None, amo=False, tag=None, book_loss_price=None, book_profit_price=None, trail_price=None, market_protection=None, algo_id=None, max_slice_quantity=None, max_slices=25)`
→ `SlicedOrderResult`, on both clients (flat method).

Every `place_order` argument (2.4) has the same meaning and is sent unchanged on every slice,
except `quantity`. `quantity` is in the exchange's **trade units** on every exchange, **MCX
included**, as for `place_order` (5.5, F21): a whole multiple of the lot size (2 CRUDEOIL lots of
100 are `quantity` 200). `max_slice_quantity` is in the same units.

**Slice size.**

1. Resolve the contract from the exchange's script master (the parsed-row cache of 8.4; the row
   whose `TradingSymbol` equals `tradingsymbol`, trimmed, case-insensitive): its `Token` and
   `LotSize`. No such row, or no lot size → `ValueError`, nothing sent.
2. `security_info(exchange, token)` (a read: the read retry rules of 5.1 apply) gives the freeze
   quantity `frzqty` ("Freeze Qty", in units on every exchange). When `security_info().ls` and
   the script master's lot size are both present they must be equal, else `ValueError` (stale
   data), nothing sent.
3. The largest slice, in units on every exchange (MCX included; a slice may equal `frzqty`, the
   limit is inclusive): `floor(frzqty / lot) * lot`.

   - `max_slice_quantity` given: the smaller of the two (it never raises the freeze limit). It
     must be an integer ≥ 1 and a whole number of lots.
   - `frzqty` blank, 0 or negative: the freeze quantity is unknown and `max_slice_quantity`
     is required, else `ValueError` "freeze quantity unknown"; the largest slice is then
     `max_slice_quantity`.
   - `frzqty` present but not an integer (`abc`, `1801.5`): the `security_info()` read itself
     fails (`ResponseParseError`, 7.1 quantity parsing), even when `max_slice_quantity` is
     given; nothing is sent.
   - A largest slice below one lot (`frzqty` smaller than the lot size) → `ValueError`.
4. Slices: as many full slices as fit, then the remainder. A quantity at or below the largest
   slice goes out as **one slice** (owner, 2026-11-12): a small order is never refused for being
   small (NFO 65 → 65; NFO 130 → 130, also with `max_slice_quantity` 650; BFO 20 → 20; MCX
   CRUDEOIL 300 → 300); the only small-quantity refusal is "not a whole number of lots" (NFO 100).

Live production values (2026-11-12):

| Contract | Lot | `frzqty` | Largest slice | Example |
|---|---|---|---|---|
| NIFTY option / future (NFO) | 65 | 1801 | 1755 (27 lots) | 3965 (61 lots) → 1755, 1755, 455 |
| SENSEX option (BFO) | 20 | 1000 | 1000 (50 lots; BSE accepts exactly `frzqty`) | 2000 → 1000, 1000 |
| SENSEX future (BFO) | 20 | 500 | 500 | 600 → 500, 100 |
| CRUDEOIL (MCX) | 100 | 2500 | 2500 (25 lots) | 6000 (60 lots) → 2500, 2500, 1000 |
| GOLD (MCX) | 1 | 1 | 1 | 3 → 1, 1, 1 |
| ITC (NSE cash) | 1 | 350854 | 350854 | 1 → 1 |

**Refused before any order is sent** (`ValueError`, the `ConfigurationError` below, or the error
`place_order` raises for its own checks; no order request is made):

- `quantity` not an integer ≥ 1 (a boolean is refused), or not a whole number of lots (MCX
  included: CRUDEOIL 150); `max_slices` not an integer ≥ 1; `max_slice_quantity` not an integer ≥ 1.
- More slices needed than `max_slices` (default **25**): the message gives the number of slices
  needed and says to pass a larger `max_slices`.
- `disclosed_quantity` larger than the smallest slice.
- Everything `place_order` refuses before sending (2.4, unchanged), checked once on the first
  slice's arguments.
- Slices the client-side order limiter (3.4) would always stop part-way (`ConfigurationError`,
  the error for a refusal caused by the client configuration; the message names the setting and
  says nothing was sent): more slices than `order_rate_limit_per_second` while
  `rate_limit_mode` is `raise` (it never waits for the next second), or while
  `order_rate_limit_max_wait` is below 1.0 (too short to wait out the 1-second window). The
  default configuration (`wait`, 1.0 s, 10 per second) is not affected: 25 slices go out in
  about 2 s. Also `ConfigurationError`: more slices than `order_rate_limit_per_minute` (default
  200; they could never all go out within one minute).
- Not enough room in the current minute (`RateLimitError`, `endpoint` `PlaceOrder`,
  `http_status` none, `retry_after` = seconds until the whole order fits; raised, not returned,
  because nothing was sent): other orders already sent in the rolling 60 s leave fewer free
  slots than there are slices, and `rate_limit_mode` is `raise` or that wait is above
  `order_rate_limit_max_wait`. When the room frees within `order_rate_limit_max_wait` the order
  is sent and the limiter waits as usual.

Together with the slice-size refusals above (no script-master row or lot size; `ls` differing
from the script master's lot size; a largest slice below one lot; an unknown freeze quantity
without `max_slice_quantity`; a `max_slice_quantity` that is not a whole number of lots; a
failed `security_info()` read, raised as that read's error), these are every refusal.

**Sending.**

- Slices are placed strictly one after another through the `place_order` path: the same
  validation, the outcome log of 5.6 per slice, never retried (5.2), redirects never followed
  (3.1). They are paced by the client-side orders rate group (3.4: 10 per second by default,
  200 per minute; `rate_limit_mode` and `order_rate_limit_max_wait` apply unchanged). Before
  the first slice the client checks the per-minute window (read only, nothing recorded) and
  refuses the whole order when it has no room (see the refusals above). Only a race with another
  thread sending orders at the same time can still stop a call part-way: that slice raises
  `RateLimitError` inside the loop, which is a definite failure and is returned in
  `result.error` (below), never raised.
- No waiting for fills between slices; every slice carries the same `tag`.
- **Stop at the first failure** (owner decision; no continue, no all-or-nothing):
  - A definite failure on slice *k* (`OrderRejectedError`; a `RateLimitError` raised by the
    client limiter before sending; a validation error; `AuthenticationError`; any other error
    whose outcome is known: the slice was not placed) → return
    `SlicedOrderResult(complete=False, error=<that error>)` with slices 1 … *k*−1 placed; nothing
    more is sent.
  - An ambiguous outcome on slice *k* (`OrderStatusUnknownError`) → raise that error with the
    partial result attached as `sliced_result`: slices 1 … *k*−1 placed; slice *k* unknown, in
    neither `order_ids` nor `placed_quantity`, and counted in `unsent_quantity`. The message says
    to check the order book before sending anything again.
  - An interruption on slice *k* (anything else, such as Ctrl-C, process exit or a task
    cancellation, including the cancellation by which a caller's timeout such as
    `asyncio.wait_for` stops the call) → the SDK adds a note to that same exception listing the
    order ids placed and the unsent quantity (slice *k* may have been sent), attaches the partial
    result as `sliced_result` when the exception accepts attributes (never failing when it does
    not), logs a WARNING summary and re-raises it unchanged; nothing is sent after it. (Python:
    `add_note`; a caller's `asyncio.wait_for` raises its own `TimeoutError`, whose `__cause__`
    is the noted `CancelledError`.)
- The summary line on the orders logger (5.6) is INFO for a complete call and WARNING when the
  call stopped, ended unknown or was interrupted; it gives counts and quantities only.
- There is no group modify or cancel: the caller has every order id.

**Result.** `SlicedOrderResult` (immutable; fields in `models.md`): `requested_quantity`,
`slice_quantities` (the planned slices, in order), `order_ids` (the acknowledged slices, in
order), `placed_quantity` (their sum), `unsent_quantity` (`requested_quantity - placed_quantity`),
`complete` (every slice placed) and `error` (the definite failure that stopped sending, else
`null`).

Vectors (slice sizes and every refusal of the planner, from `exchange`, `quantity`, `lot_size`,
`freeze_quantity`, `max_slice_quantity`, `max_slices`): `slice_plan.json`.

---

## 6. Authentication

Python reference: `auth.py`, `_session.py`, `_client_core.py`, `_token_store.py`, `_cli.py`.

### 6.1 Flow

1. The user creates an app on the web platform's **API Dashboard** (Tools → API,
   `https://web.tradesmartonline.in/api`). The Dashboard's **App ID is the App Key** (`app_key`,
   `TRADESMART_APP_KEY`, `--app-key`); it also shows the Secret Key, Redirect URL and static IPs
   (at most 2 IP changes per day). Register an **IPv4** static IP for now: IPv6 clients need a
   separate API domain that TradeSmart has not yet published (F16; the base URLs are
   configurable, 12). If the App Key **or** the Secret Key leaks, the user removes that app on the
   Dashboard, creates a new one (new App Key and Secret Key), updates the environment and logs in
   again; `support@tradesmartonline.in` is the last resort. An SDK's user guide and security
   policy state these facts (owner answers; not in the official API docs).
2. Authorize URL: `{authorize_url}?client_id=<percent-encoded app_key>` (every reserved character
   encoded). Empty `app_key` → `ConfigurationError`.
3. TradeSmart redirects to the registered Redirect URL with a **single-use** `code`.
4. `GenAcsTok` with `{"code", "checksum"}`, no `Authorization` header, never retried.
5. Use `Authorization: Bearer <access_token>`.

### 6.2 Checksum

`checksum = lowercase hex SHA-256 of UTF-8(app_key + secret_key + code)`: plain concatenation, no
separators. All three must be non-empty (`ConfigurationError`). Vectors: `checksum.json`.

### 6.3 Access token and expiry

| Field | Source |
|---|---|
| `access_token`, `susertoken`, `refresh_token` | response; secret (masked in `repr`/`raw`) |
| `expires_in`, `user_id` (`uid`), `account_id` (`actid`), `uname` | response |
| `issued_at` | local clock at receipt (aware) |
| `expires_at` | the first 06:00 IST strictly after `issued_at` (IST = UTC+05:30, no DST); `expires_in` is ignored |

- **Token rules when a REST client's token is set** (constructor, `set_access_token`,
  `from_env`; the stream classes send their token in the connect message, not in a header, and
  do not apply this check): a string token is stripped of surrounding whitespace (the newline of a token
  read from a file) and a blank one counts as **no token**. The token (a string, or the
  `access_token` of an `AccessToken`) must then consist of visible ASCII only (U+0021 to
  U+007E: no space, no control character, nothing non-ASCII), else `ConfigurationError`
  ("access_token must contain only printable ASCII characters ...; check how the token was
  copied or read"), raised at once, value never shown. Any other type → `ConfigurationError`.
  So a bad token can never reach the `Authorization` header.
- Before every authenticated call, if the token is an `AccessToken` and `check_token_expiry` is
  on, an expired token raises `AuthenticationError` (with the login hint) and nothing is sent.
  A plain string token is not checked. No token (also a blank one) → `AuthenticationError`,
  nothing sent; an order call logs `not_sent` (5.6).
- `generate_session()` errors: only three kinds become `AuthenticationError`: any `APIError`
  (a `Not_Ok` or HTTP error answer, including its subclasses; an `AuthenticationError` gets the
  same message) → `"GenAcsTok failed: <emsg>"` when `emsg` is a non-blank string, else
  `"GenAcsTok failed (no error message from the server)"` (never a doubled prefix, never
  `None`; the 4.2 message rule); `OrderStatusUnknownError` (unknown outcome) and
  `ResponseParseError` (unparseable success) → "the authorisation code is single-use; restart
  the login". Provably-unsent failures keep their types: a connect failure is `NetworkError` or
  `RequestTimeoutError` (the code was not used and may be tried again). A missing `app_key` or
  `secret_key`, or an empty code, is `ConfigurationError` before anything is sent. Vectors for the
  expiry rule: `token_expiry.json`. There is no refresh or logout endpoint.
- `is_expired(now)` is `now >= expires_at`: a token is already expired at the exact instant
  `expires_at` (vectors `is_expired_*`). A token issued exactly at 06:00:00 IST expires at 06:00
  the next day (vector `issued_exactly_at_06_00_expires_next_day`).

### 6.4 `from_env` precedence

| Value | Taken from, in order |
|---|---|
| Client id | `TRADESMART_CLIENT_ID`; else the saved login's `client_id`; else `ConfigurationError` |
| Token | `TRADESMART_ACCESS_TOKEN` (only with `TRADESMART_CLIENT_ID`, else `ConfigurationError`); else the saved login when its client id matches (case-insensitive) and it is not expired (loaded as an `AccessToken`, so the 06:00 check applies) |
| App/secret key | `TRADESMART_APP_KEY`, `TRADESMART_SECRET_KEY` (clients only; streams ignore the app key) |
| Token file | `TRADESMART_TOKEN_FILE`; else 6.5 default |

Every `TRADESMART_*` value above is stripped of surrounding whitespace, and a value that is then
empty counts as unset (`TRADESMART_CLIENT_ID="ABC123 "` is `ABC123` and matches a saved login for
`ABC123`; `TRADESMART_ACCESS_TOKEN="tok\n"` is `tok`). The same applies to the streams'
`from_env()`.

Without a usable token and without an app key: `ConfigurationError` naming the login command, or
`AuthenticationError` when the saved login has expired. With an app key the client is built
without a token; the first authenticated call raises `AuthenticationError` with the reason.
An unreadable or unsafe token file is reported as the reason, never with its contents.

### 6.5 Token store (recommended)

| Item | Rule |
|---|---|
| Location | `TRADESMART_TOKEN_FILE` (stripped; blank = unset; a leading `~` expanded; a relative path made absolute against the current folder; a symbolic link is **not** resolved, so its own path is used); Windows `%APPDATA%\tradesmart\token.json` (fallback `~\AppData\Roaming`); elsewhere `$XDG_CONFIG_HOME/tradesmart/token.json` (fallback `~/.config`) |
| Content | JSON object: `client_id`, `access_token`, `issued_at`, `expires_at` (timestamps below) |
| Timestamp format (write) | Exactly `YYYY-MM-DDTHH:MM:SS±HH:MM`: seconds precision (fractions dropped), a numeric offset with a colon (the token's own offset; the login command writes `+05:30`), never `Z` |
| Timestamp format (read) | Every field value is first trimmed of surrounding whitespace (vector `read_padded_is_accepted`). Then accept exactly `YYYY-MM-DDTHH:MM:SS`, optionally `.` and 1 to 9 fraction digits (more than 6 are truncated to microseconds), then `Z`, `z` (= `+00:00`) or `±HH:MM`, so the forms other writers produce (JavaScript `...123Z`, Go nanoseconds) are read. Every other form refuses the file: no offset, a date alone, an offset without a colon (`+0530`), the basic format (`20261110T091530+05:30`), a space or lower-case `t` separator, no seconds, 10 or more fraction digits. The reader matches this pattern before parsing, so the result is the same on every runtime (Python 3.11+ `fromisoformat` alone accepts more). Vectors: `token_file.json` |
| Write | Directory mode 0700; temp file `.<name>.<random>.tmp` created exclusively with mode 0600, fsync, atomic rename; no partial file on failure |
| Read (POSIX) | Refuse a file owned by another user or with any group/other permission bit |
| Read (Windows) | Mode bits not enforced; the per-user profile folder protects it |
| Validation | All four fields non-empty strings (after trimming); timestamps timezone-aware; an `issued_at` more than 5 minutes after this computer's clock refuses the file ("... has an 'issued_at' in the future (check this computer's clock); log in again"; vector `read_issued_at_in_the_future_is_refused`), because the cap below is anchored on it; `expires_at = min(stored, next 06:00 IST after issued_at)` so an edited file cannot extend validity |

### 6.6 Login CLI (recommended)

Command `<runner> tradesmart [login|token-path|logout]` (Python: `python -m tradesmart`; `login` is
the default). Options: `--token-file PATH` (all commands; `~` expanded and made absolute, so the
printed hints work from any directory); `login`: `--client-id`, `--app-key`,
`--port` (1–65535, default 8080), `--no-browser`, `--listen-host IP` (default `127.0.0.1`, so the
callback listens on the loopback only unless this option is given; an **IPv4** literal: anything
else, an IPv6 address included, is a usage error, exit 2, "accepts IPv4 addresses only"; any
non-loopback value prints one warning; meant for containers,
e.g. `0.0.0.0` with the port published only to the host's `127.0.0.1` — the redirect URL shown and
expected stays `http://127.0.0.1:<port>/callback`), `--no-save-keys` (6.7); `logout`:
`--forget-keys`, `--client-id` (6.7). The secret key comes only from `TRADESMART_SECRET_KEY`, the
saved keys (6.7) or a no-echo prompt; the client id from flag, env, or prompt; the app key from
flag, env, the saved keys, or prompt.
Paste is a fallback only: run directly on a machine with a browser (or in a container with the
published port), the login completes by itself from the redirect.

1. Open the authorize URL in a browser and listen on `<listen-host>:<port>` (loopback by default) for
   `GET /callback?code=...` for up to 120 s (200 with a code, 400 without, 404 other paths; no
   access log). Each connection is served on its own (daemon) thread, so an idle speculative
   connection that a browser opens first cannot delay the real redirect; the first valid code
   wins, and a later callback with a code gets HTTP 409 "Login already received. You can close
   this window." The registered Redirect URL must be `http://127.0.0.1:8080/callback` (accepted
   by the Dashboard, F13).
2. If no browser can be opened, print the URL and keep listening. If the port is busy, the wait
   times out, or `--no-browser` is given: ask the user to paste the code or the whole redirect URL.
3. Exchange the code once (`generate_session`).
4. **Account check:** TradeSmart defines no OAuth `state`, so if the token's `uid` or `actid` is
   present and differs (case-insensitive) from the requested client id, save nothing and fail.
5. Save the login under the server's spelling of the client id; print the name, id and expiry
   (IST); print shell-specific `TRADESMART_TOKEN_FILE` hints when a non-default file is used.

Exit codes: 0 success, 1 login failed / cancelled, 2 usage or configuration error. The CLI never
prints the token, secret key or code.

Example login receivers (for instance a web-server example that receives the redirect itself,
Python `examples/auth_server_flow.py`) must keep the CLI's protections: the account check of
step 4 (case-insensitive), a callback server that serves each connection on its own thread or
task with a short idle timeout (10 s) so an idle browser connection cannot block the redirect,
404 for other paths, the first code wins (a later one is answered but not used), and an overall
deadline (300 s in the Python example). They never print the token, secret key or code.

### 6.7 Saved keys of the login CLI (recommended)

So that the daily login asks only for the browser step, the login CLI saves the App Key and
Secret key per client id.

| Item | Rule |
|---|---|
| Location | `credentials.json` in the folder of the token file in use (6.5; with `--token-file PATH`, beside that file). A tool for another environment uses its own name (`<env>-credentials.json`) and never reads or writes this file (it refuses an `<env>-credentials.json` that links to it). Mirror: the login CLI refuses a `credentials.json` that links (links followed) to a file named `*-credentials.json` (another environment's): login exits 2 before any request, `logout --forget-keys --client-id` exits 1; nothing is read or written through the link |
| Content | JSON object `{"version": 1, "clients": {"<client id>": {"app_key": "...", "secret_key": "..."}}}`; an entry may hold one of the two keys; client ids match ignoring case; an entry is stored under the server's spelling of the client id (6.6 step 5) |
| Write / read | As the token file (6.5): directory 0700, file 0600, temp file + atomic rename, no partial file; on POSIX a file owned by another user or open to group/others is refused (on Windows the per-user profile folder protects it). The owner/mode check and the read use one open file (no check-then-read race). **Not encrypted** (documented trade-off: whoever can read the file can read the keys). No locking: two logins saving at the same moment may lose one entry (the last write wins) |
| Unusable file | Not JSON (including nesting deeper than the parser allows), not an object, no `version`, another `version`, `clients` not an object, an entry not an object or with a key that is not a non-blank string, or an empty entry: refused with a message naming the file, never its content; login exits 2 before any request; the file is never overwritten |
| Key order (login) | For each key: `--app-key` / `TRADESMART_APP_KEY` and `TRADESMART_SECRET_KEY`, then the entry for the client id, then the no-echo prompt. The file is read only when a key is still missing. Using saved keys prints which keys came from which file (never the keys) |
| Saving | Only after the token was saved (6.6 step 5): the keys typed at the prompt or given with `--app-key`, never keys from environment variables; other client ids' entries and a key not typed are kept. `--no-save-keys` skips it. A failed save only warns (exit 0). The CLI prints the file and the command to forget them |
| Failed login with saved keys | The error is followed by a note that the saved keys were used and the `logout --forget-keys --client-id ID` command; no retry, no loop, the entry is kept |
| `logout` | Removes the token only. `--forget-keys` also removes the saved keys: of `--client-id ID` (the file is deleted when no entry is left; an unusable file then fails with exit 1 and the advice to forget all), or the whole file without `--client-id` (listing the client ids, or saying the file was unreadable). `--client-id` without `--forget-keys`, or a blank `--client-id`, is a usage error (exit 2, nothing removed); only an omitted `--client-id` forgets every client id |

The keys never appear in output, logs, exceptions or `repr`. (The login URL printed by
`--no-browser`, or when no browser opens, carries the App Key as `?client_id=` by the API's design,
6.1.)

---

## 7. Models

Python reference: `models/base.py`, `models/*.py`, `enums.py`.

**Every response model and stream message, field by field** (name, wire key, type, required,
lenient) and every enum with its wire values: `specifications/models.md`, generated from the
Python models and kept in sync by a test. This section states the rules; `models.md` is the
inventory. `models.json` is the machine-readable form of these tables.

### 7.1 Field types

| Wire value | SDK type | Rule |
|---|---|---|
| Prices, money, amounts | Decimal (`Price`) | From string/number via its decimal text; blank → null; non-finite or bool → parse error |
| Volume, open interest | Decimal | as prices |
| Quantities (`qty`, `ls`, `pp`, `fillshares`; full list in `models.md`) | integer | Must be integral (`"65.0"` ok, `"65.5"` error); blank → null |
| Ids, tokens, text | string | JSON numbers coerced to string |
| Epoch seconds (`lut`, `ssboe`, `ft`) | UTC instant | `0` → null |
| Date-times (`request_time`, `time`, `exch_tm`) | IST instant | Formats `HH:MM:SS DD-MM-YYYY`, `DD-MM-YYYY HH:MM:SS`, `DD/MM/YYYY HH:MM:SS`, `DD-MM-YYYY`, `DD-Mon-YYYY`; unknown → null and one WARNING per shape (digits shown as `9`, at most 50 shapes). Vectors: `time_conversion.json` |
| Stream prices | Decimal | `42949672.95` (ATO sentinel) → null, on quote/depth and order-update prices only |
| Enum fields | enum or the raw string | Unknown values are kept as strings, never an error |

### 7.2 Model rules

- Unknown fields are kept (accessible by name); the full wire payload is kept as `raw`
  (shallow copy; secrets redacted in `AccessToken.raw`). Models are immutable.
- Nested models keep their own `raw` too (for example `Holding.instruments[0].raw`,
  `UserDetails.prarr[0].raw`): a shallow copy of that nested record, in which the values of
  secret keys (`access_token`, `susertoken`, `refresh_token`, `checksum`, `code`, ...; the
  redaction list of section 13) are replaced by `***`. The same masking applies to a model validated
  directly from a mapping. The top-level `raw` of a parsed response is the original payload, and
  it is not copied again for the nested-raw rule (hot path).
- A response or list item that is not a JSON object raises `ResponseParseError`
  ("Could not parse `<Endpoint>` response (expected a JSON object)"), 4.3.
- Snake-case names map to wire keys (for example `order_id` ↔ `norenordno`, `tradingsymbol` ↔
  `tsym`, `last_price` ↔ `lp`; every mapping is listed in `models.md`); every documented wire key
  stays reachable.
- **Lenient records** (`Order`, `OrderHistoryEntry`, `Trade`, `Holding`, `Position`,
  `OrderUpdate`, `PositionAck`, `PositionUpdate`, `Gtt`, `TriggeredGtt`): a top-level field that
  fails parsing becomes null once, with one WARNING naming the field(s) (never values); `raw` keeps
  the original. Identity keys `norenordno` and `status` (and `al_id` on `Gtt` / `TriggeredGtt`)
  are never nulled: the record fails instead. Other
  models fail the whole record with `ResponseParseError` (`UnknownMessage` on a stream).

### 7.3 Request values and enums

Wire conversion: enum → its wire code; bool → `"Yes"`/`"No"`; Decimal → plain decimal text (no
exponent); float → via its shortest decimal text; int → decimal text; string as is.

Enum lookups accept the wire code or the listed words, case-insensitively, trimmed. A request
parameter typed as an enum (on every method, including `holdings(product=...)` and
`exit_order(product=...)`) is resolved this way before sending, so `"CNC"`, `"cnc"` and `"C"` all
send `C`; an unknown value raises `RequestValidationError` (the wire key as field) and nothing is
sent. A string is never passed through unchanged.

| Enum | Members → wire value | Extra accepted words |
|---|---|---|
| `Exchange` | NSE, BSE, NFO, BFO, CDS, BCD, MCX (same) | — |
| `TransactionType` | BUY `B`, SELL `S` | `BUY`, `SELL` |
| `OrderType` | LIMIT `LMT`, MARKET `MKT`, SL `SL-LMT`, SL_M `SL-MKT` | `LIMIT`, `MARKET`, `SL`, `SL-M`, `SLM` |
| `Validity` | DAY, IOC, EOS (same) | — |
| `Product` | CNC `C`, MIS `I`, NRML `M`, CO `H`, BO `B`, MTF `F` | member names |
| `OrderStatus` | PENDING, OPEN, TRIGGER_PENDING, COMPLETE, CANCELED, REJECTED (same) | — |
| `GttAlertType` | LTP_ABOVE `LTP_A_O`, LTP_BELOW `LTP_B_O`, CHANGE_PERCENT_ABOVE `CH_PER_A_O`, CHANGE_PERCENT_BELOW `CH_PER_B_O`, ATP_ABOVE `ATP_A_O`, ATP_BELOW `ATP_B_O`, OI_ABOVE `OI_A_O`, OI_BELOW `OI_B_O`, TOI_ABOVE `TOI_A_O`, TOI_BELOW `TOI_B_O`, HIGH_52W_ABOVE `LTP_A_52HIGH_O`, HIGH_52W_BELOW `LTP_B_52HIGH_O`, VOLUME_ABOVE `VOLUME_A_O`, OCO_BUY `LMT_BOS_O`, OCO_SELL `LMT_SOB_O` (the two OCO codes, 2.9) | member names |
| `GttValidity` | DAY, GTT (same) | — |
| `StreamState` | DISCONNECTED, CONNECTING, AUTHENTICATING, CONNECTED, RECONNECTING, CLOSED | — |
| `Buildup` | long_buildup, short_buildup, long_unwinding, short_covering, neutral | — |

Enums print as their wire value.

---

## 8. Instruments (script master)

Python reference: `instruments.py`, `_contract_cache.py`.

### 8.1 Download

- `GET {script_master_base_url}/{EXCH}_symbols.txt.zip`, headers `User-Agent` (+ conditional
  header), no `Authorization`, REST timeouts, no retries, no rate limit. Redirects are never
  followed, also on an injected HTTP client that follows them (3.1): a 3xx is a
  `ResponseParseError` (the body is not a zip).
- From the zip use the `.txt` member whose base name starts with the exchange code, else the
  first `.txt` (both the `.txt` suffix and the exchange prefix are matched case-insensitively);
  member paths are never used for writing. No `.txt` / bad zip → `ResponseParseError`.
- Save as `<folder>/<EXCH>_symbols.txt`, written atomically. The folder is `dest_dir` when
  given, else the configured `instruments_dir` (12), else the user cache folder
  (`~/.cache/tradesmart` in Python); a leading `~` in `dest_dir` or `instruments_dir` is
  expanded. Set the file's modification time to the response `Last-Modified`, never later than
  now.
- Errors: 401/403 `AuthenticationError`, 404 `NotFoundError`, 429 `RateLimitError`, ≥ 500
  `ServerError`, other ≥ 400 `APIError` (body redacted, ≤ 500 chars); timeouts
  `RequestTimeoutError`, other failures `NetworkError`.

### 8.2 Freshness

| State of the cached file | Action |
|---|---|
| Missing, or `force` | Full download; a successful one also writes the `.checked` sidecar |
| Modified at or after the last daily refresh (07:00 IST today, or yesterday if now is earlier) | Use it; no request |
| Older, server checked < 15 min ago (`.checked` sidecar) | Use it; no request |
| Older, otherwise | Conditional GET with `If-Modified-Since` = file mtime (HTTP date, GMT) |
| Conditional → 304 | Keep the file, record the check |
| Conditional → network error, timeout or 5xx | Keep the file, record the check, log one WARNING |
| Conditional → other 4xx | Raise |
| 304 but the file vanished meanwhile | One full download |
| First or forced download fails | Raise |

The `.checked` sidecar (`<file>.checked`) holds one ISO-8601 UTC timestamp of the last server
check; an unreadable, naive or future stamp counts as no check. Live: files are regenerated at
07:00 IST (`Last-Modified` 01:30 GMT); `ETag` and `If-None-Match` → 304 are supported too (F10).
An SDK may use either validator. Vectors (requests made, file used or downloaded, sidecar,
WARNINGs): `script_master_freshness.json`.

The callers that read the file after downloading it (`instruments()`, and `option_greeks()`
through the 8.4 cache) download it once more when it was removed
between the download and the read (a cleanup job, another process); removed again →
`ResponseParseError` ("script master file `<name>` was removed before it could be read;
download the instruments again").

### 8.3 Parsing

- Decode UTF-8 with an optional BOM. A file that is not valid UTF-8 raises `ResponseParseError`
  (HTTP status 200, empty body) "script master file `<name>` is not valid UTF-8 text; delete it
  and download the instruments again", from every caller (`instruments()`, the file parser (Python:
  `tradesmart.instruments.load(path)`), the
  `option_greeks` contract cache), never a raw decoding error. A file that is not valid CSV for
  the parser (a NUL byte, a field over the parser's size limit) likewise raises
  `ResponseParseError` ("script master file `<name>` is not a valid script master (...); delete
  it and download the instruments again"), the same on every runtime version (Python 3.11+
  would otherwise accept a NUL byte).
- Delimiter: whichever of `,` `|` TAB `;` occurs most in the first non-blank line (within the
  first 4096 characters); on a tie the first in that order wins; `,` if none occurs.
- CSV rules (RFC 4180 style, as Python's `csv` module): a field may be quoted with `"` (a quoted
  field may contain the delimiter, and `""` inside it is one `"`); rows whose cells are all blank
  are skipped. The first remaining row is the header; header cells and values are trimmed; every
  value is a string; rows are mapped by header (no column name is assumed). A row shorter than
  the header has no key for the missing columns; the extra cells of a longer row are dropped; with
  duplicate header names the last column wins. Vectors: `script_master_parse.json`.
- Observed headers (live, F10); each observed file ends with an **empty trailing column** (key
  `""`), so parsers must accept an empty header cell:

| Exchange | Columns |
|---|---|
| NSE | `Exchange, Token, LotSize, Symbol, TradingSymbol, Instrument, TickSize, ""` |
| NFO | `Exchange, Token, LotSize, Symbol, TradingSymbol, Expiry, Instrument, OptionType, StrikePrice, TickSize, ""` |
| MCX | `Exchange, Token, LotSize, GNGD, Symbol, TradingSymbol, Expiry, Instrument, OptionType, StrikePrice, TickSize, PriceUnits, TradeUnits, DeliveryUnits, ""` |
| BFO (verified live 2026-11-12) | `Exchange, Token, LotSize, Symbol, TradingSymbol, Expiry, Instrument, OptionType, Strike, TickSize, ""` |

On BFO the strike column is `Strike`, and an index's options and futures have different
`Symbol`s: SENSEX options `BSXOPT`, futures `BSXFUT` (for example `SENSEX26NOVFUT`, lot 20); also
`SX50OPT` / `SX50FUT` (SENSEX 50), `BKXOPT` / `BKXFUT` and `BITOPT` / `BITFUT` (observed in the
live BFO file 2026-11-12; only `BSXOPT` / `BSXFUT` are verified in use, F28, and the OPT->FUT
fallback of 10.4 applies to every family). BSE, CDS
and BCD layouts are not verified live. `Expiry` is `DD-MON-YYYY`; `Instrument` starts with `OPT`
for options and `FUT` for futures (`FUTIDX`, `FUTSTK`, `FUTCOM`).

### 8.4 Parsed-row cache (used by `option_greeks` and `place_sliced_order`)

Per client and exchange, an index of option rows by upper-cased `TradingSymbol` and future rows
by upper-cased (`Symbol`, `Exchange`), each in file order. It also keeps the first row of every
instrument (equity, future, option, any other) by trimmed, upper-cased `TradingSymbol`: the
contract `place_sliced_order` resolves (5.7). The future rows looked up for an
option are those of every symbol 10.4 may use, the option's own `Symbol` first and then the
`OPT`→`FUT` fallback symbol, so the cached path selects the same future as a scan of every row.
Keyed by (path, mtime in ns, size): a refreshed file is parsed again and the old entry dropped
(one entry per exchange). Concurrent first calls parse once. `instruments()` does not use it and
always returns a fresh list.

---

## 9. Streaming

Python reference: `stream/protocol.py`, `stream/state.py`, `stream/delivery.py`,
`stream/async_stream.py`, `stream/sync_stream.py`, `models/stream.py`.

### 9.1 Connect

1. Before opening, an expired `AccessToken` raises `AuthenticationError` (when
   `check_token_expiry`). The same check runs before every reconnect attempt (9.5). It runs only
   then: a stream that is already connected when its token passes 06:00 IST is **not** ended by
   the SDK (the server decides; a later reconnect then fails the check).
2. Open the WebSocket (`User-Agent` as 3.1, open timeout `ws_auth_timeout` = 10 s, no
   protocol-level pings). Failure → `NetworkError`.
3. Send `{"t":"a","uid":<id>,"actid":<id>,"source":"API","accesstoken":<token>}`.
4. Wait up to `ws_auth_timeout` for the `ak` message (other messages arriving first are
   dispatched normally). Timeout → `RequestTimeoutError`; socket closed → `NetworkError`.
5. Accept when `s`, trimmed, equals `ok` **case-insensitively**: the docs say `"Ok"`, the live
   server sends `"OK"` (F18). Anything else → `AuthenticationError(endpoint="WebSocket")`.
6. Replay registered subscriptions (9.4), then state CONNECTED.

`connect()` may be called once per stream; the initial connect never retries. Vectors:
`ws_connect_ack.json`.

### 9.2 Messages

Outbound (`k` = `#`-joined `EXCH|TOKEN` keys):

| Purpose | Message | Ack / feed |
|---|---|---|
| Touchline | `{"t":"t","k":...}` / unsubscribe `{"t":"u","k":...}` | `tk` per scrip, then `tf` / `uk` |
| Depth | `{"t":"d","k":...}` / `{"t":"ud","k":...}` | `dk`, then `df` / `udk` |
| Order updates | `{"t":"o","actid":<id>}` / `{"t":"uo"}` | `om` (no ack) / `uok` |
| Position updates | `{"t":"p","uid":<id>}` / `{"t":"up"}` | `pk`, then `pm` / `upk` |
| Heartbeat | `{"t":"h"}` | `hk` (`ft`) |

Instrument keys: `EXCH|TOKEN` or (exchange, token); exchange any case, sent upper-case; token
non-empty without `|`, `#` or whitespace; duplicates removed keeping order. Invalid input →
`RequestValidationError`. A 2-tuple of strings whose first item has no `|` is one instrument.

Inbound parsing never throws: invalid JSON, a non-object, an unknown `t`, or a model failure
gives `UnknownMessage` (raw kept; a WARNING names the failing fields only). `ak` and `hk` are
consumed internally and never delivered.

### 9.3 Snapshots

`tf`/`df` carry only the fields that changed. The SDK keeps the latest merged fields per
`e|tk` from `tk`, `tf`, `dk`, `df` (all keys except `t`), updated by the reader before delivery.
`snapshot(key)` returns a copy or null; the key is normalised like a subscription. It takes
exactly **one** instrument (an `"EXCH|TOKEN"` string or one `(exchange, token)` pair): a list,
a tuple that is not one pair, or an invalid key raises `RequestValidationError`.

### 9.4 Subscriptions and limits

- A registry remembers touchline keys, depth keys, and the order/position flags. Subscribing
  while not CONNECTED only updates the registry; the request is sent on (re)connect.
- Replay order after every (re)connect: touchline (keys sorted), depth (sorted), orders,
  positions.
- At most **1000** subscriptions per connection, counted as the distinct touchline keys plus the
  distinct depth keys (a key subscribed to both counts twice): exceeding it raises
  `SubscriptionLimitError` and nothing is sent. Key vectors: `scrip_keys.json`.
- After CLOSED every subscribe/unsubscribe raises `TradeSmartError` (a stream is single-use).
- **Atomic with the replay.** Updating the registry and sending the request happen together
  under one send lock that the (re)connect replay also holds. So a subscription made while a
  reconnect is replaying is sent **exactly once** (by the replay or by the call, never by both
  and never by neither), and an unsubscribe cannot be undone by a replay that read the registry
  before it. `SubscriptionLimitError` is checked after taking the lock, still before anything is
  sent or registered.

### 9.5 Heartbeat, liveness and reconnect

| Item | Rule |
|---|---|
| Heartbeat | Send `{"t":"h"}` every `heartbeat_interval` (30 s) while CONNECTED. A heartbeat task failure terminates the stream with `NetworkError`. |
| Liveness | No frame for 2 × heartbeat (60 s) → connection dead (WARNING), reconnect |
| Reconnect delay | `min(cap, base · 2^attempt) · (0.5 + U/2)`, base 1 s, cap 60 s, `attempt` 0-based. The delay is slept **before** every attempt, the first one included (attempt 0 waits 0.5–1 s) |
| Token expiry | After the delay and before each attempt, an expired `AccessToken` (under `check_token_expiry`) ends the stream with the terminal `AuthenticationError`, without opening a socket; a plain string token is not checked |
| Heartbeat count | `heartbeat_acks` counts every `hk` received, over all connections of the stream |
| Limit | `reconnect_max_attempts` = 20 consecutive attempts (`None` = unlimited); exhausted → terminal `NetworkError("reconnect attempts exhausted")` |
| Budget reset | Only after a connection that stayed up ≥ 2 × heartbeat |
| Each attempt | Open, authenticate, replay (9.4). Transport errors and ack timeouts count as failed attempts; an ack rejection (`AuthenticationError`) is terminal |
| `reconnect_count` | Successful reconnects; incremented before CONNECTED is reported |
| Not replayed | The server does not resend updates missed during an outage: after a reconnect, call `orders()` and `positions()` |

`on_reconnect` (sync stream, no arguments) runs right after `on_connect` each time CONNECTED is
reached again after a reconnect, never on the first connect.

### 9.6 State machine

`DISCONNECTED → CONNECTING → AUTHENTICATING → CONNECTED`; on loss `CONNECTED → RECONNECTING →
CONNECTED`; `close()`, a terminal error or a failed initial connect → `CLOSED` (terminal, no
further transitions). Each transition is reported once to `on_state_change`; callback exceptions
are logged and ignored.

Async `on_state_change`: a plain function runs inline. If it returns an awaitable (an `async def`
callback), that awaitable is scheduled as its own task (a strong reference is kept until it
ends) and its exception is logged at ERROR ("on_state_change callback raised"), never raised
into the stream. `close()` gives the state-callback tasks still running, the one reporting
CLOSED included, up to `CLOSE_TIMEOUT` (2 s) to finish, then cancels the rest with one WARNING.
`close()` called from inside a state callback, or from a task such a callback started or
returned, returns at once (waiting for itself would deadlock); a background task then applies
the same 2 s wait and cancellation.

### 9.7 Delivery (never block the reader)

- The reader only reads frames, updates snapshots and appends to a hand-off buffer; a separate
  dispatcher calls `on_message` (awaited if async), then feeds the iterator, one message at a time
  in arrival order. Callback exceptions are logged, never stop the stream.
- Async cancellation inside `on_message`: a cancellation raised **by** the callback (Python: an
  `asyncio.CancelledError` from user code, such as a cancelled inner task it awaited) is logged
  like any other callback error and delivery continues. Only a cancellation of the dispatcher
  itself (the stream is closing) ends delivery. (Python tells the two apart with
  `Task.cancelling()`.)
  `close()` called from inside `on_message` does not cancel that running call: it returns after
  the callback, which finishes normally.
- Messages are buffered only for an active consumer (`on_message` set, or iteration started).
- **Backpressure:** for each instrument (`e`, `tk` read from the raw message: `tf`, `tk`, `df`,
  `dk` and unknown messages that carry both) the buffer tracks only its **two newest** waiting
  entries. While ≥ `stream_backlog_limit` (1000) messages wait, a new `tf`/`df` is merged:
  (a) into the instrument's newest waiting entry if that is a feed of the same type; else
  (b) into its second-newest entry if that is a feed of the same type **and** the newest is a
  feed of the other type, and then the new message's values for the fields the other type can
  carry (the wire keys of its model plus any key that entry already holds, for example `v`,
  `lp`, `bp1`; never the identity keys) are also copied into that newest entry. Otherwise it is
  appended. Merging overwrites field by field (latest value wins) and the entry keeps its
  position. So nothing merges across an ack (`tk`/`dk`, such as the snapshots re-sent after a
  reconnect) or an unknown message of the instrument, every field's values are delivered in
  arrival order, applying the delivered messages in order ends with the latest value of every
  field, and with feeds only an instrument holds about two waiting entries (backlog about limit +
  2 × instruments). A feed without `e`/`tk` is never merged. Acks, order and position updates
  and unknown messages are never merged, dropped or reordered. One WARNING when merging starts,
  one when the backlog falls below half the limit (`max(1, limit // 2)`). Vectors (delivered
  payloads, merge count, WARNINGs): `stream_backlog.json`.
- `close()`: cancels tasks, closes the socket with a closing-handshake timeout of
  `CLOSE_TIMEOUT` = 2 s (a server that never answers the close frame is then disconnected after
  that wait; no overall bound on how long `close()` / `stop()` take is promised, since other
  steps such as a running callback can add to it), ends iteration (iteration still yields messages
  already received, then ends; a terminal error is raised after them). Undelivered `on_message`
  messages are discarded and counted in one WARNING (including how many were order/position
  updates). State-callback tasks: 9.6.

### 9.8 Sync wrapper

- `start(timeout)` runs the async stream on a daemon thread (`tradesmart-stream`) and returns once
  CONNECTED, re-raising the connect error; default timeout `2 × ws_auth_timeout + 5` s, then
  `RequestTimeoutError`.
- All callbacks run on a second daemon thread (`tradesmart-callbacks`), one at a time, in
  arrival order: `on_message` first, then `on_touchline` (`tk`/`tf`), `on_depth` (`dk`/`df`),
  `on_order_update` (`om`) or `on_position` (`pk`/`pm`). A slow callback never delays reading,
  heartbeats or reconnects.
- `on_connect` fires on every CONNECTED; `on_disconnect` once each time CONNECTED is left
  (paired with an earlier `on_connect`; never after a failed initial connect); `on_error` gets
  terminal errors (after earlier messages) and callback exceptions. A fatal (non-`Exception`)
  error in a callback stops the stream.
- Callbacks must be plain functions: an `async def` callback (or an object with an `async def`
  `__call__`) raises `TypeError` in the constructor naming the parameter. A callback that still
  returns an awaitable (e.g. `lambda m: handler(m)`) has it closed every time (never awaited, no
  "never awaited" warning) and a `TypeError` is reported once per callback name: logged, and
  passed to `on_error` (for `on_error` itself, only logged).
- Blocking subscribe/unsubscribe wait up to 10 s for the stream thread (`RequestTimeoutError`)
  and work inside callbacks. `stop(timeout=5)` is idempotent and returns immediately when called
  from a callback. `run_forever()` blocks until stopped (not allowed inside a callback).
- **After `stop()` is requested, no further message is dispatched** to `on_message`,
  `on_touchline`, `on_depth`, `on_order_update` or `on_position` (also for messages already
  waiting). The skipped messages are counted and logged once at DEBUG: "stream stopped: N
  received messages were not delivered to the callbacks (M order/position updates)".
  `on_disconnect` and `on_error` still run as described above. A terminal error (not a `stop()`)
  still delivers the messages received before it, then calls `on_error`.

---

## 10. Option Greeks (recommended)

Python reference: `greeks.py`, `_option_contracts.py`.

### 10.1 Models

| Model | Underlying | Discounting |
|---|---|---|
| Black-76 (default) | Future price `F` (forward) | underlying and strike at `r` (carry 0); the formulas ignore the dividend yield (`option_greeks()` uses it only to carry a future of another expiry to the option's expiry, 10.3) |
| Black-Scholes-Merton | Spot `S` | underlying at continuous yield `q`, strike at `r` |

With `y` = `r` for Black-76 or `q` for BSM, `t` = floored time:
`d1 = clamp((ln(U/K) + (r − y + σ²/2)·t) / (σ√t))`, then `d2 = clamp(d1 − σ√t)` computed from
the **already clamped** `d1`, where `clamp` limits to [−40, 40]. (Clamping the unclamped `d1 − σ√t`
instead gives different results when `|d1| > 40` and `σ√t` is large.) If `σ√t` underflows to 0
(a tiny volatility and time), `d1` takes its limit: `+40` or `−40` by the sign of the numerator,
`0` when the numerator is 0; if `U·σ√t` underflows to 0, gamma is non-finite and reported as
null. Never a division-by-zero error.
Call `U e^{−yt} N(d1) − K e^{−rt} N(d2)`; put `K e^{−rt} N(−d2) − U e^{−yt} N(−d1)`; price
floored at 0. Delta `e^{−yt} N(d1)` / `−e^{−yt} N(−d1)`; gamma `e^{−yt} φ(d1) / (U σ√t)`;
vega `U e^{−yt} φ(d1) √t`; annual theta `−U e^{−yt} φ(d1) σ / (2√t) ∓ r K e^{−rt} N(±d2) ± y U e^{−yt} N(±d1)`
(upper sign call); rho BSM `±K t e^{−rt} N(±d2)`, Black-76 `−t · price` (future held fixed).

Units: `rate`, `dividend_yield`, `iv` annual and continuous (0.065 = 6.5 %); **theta per calendar
day** (annual / 365); **vega per 0.01 volatility**; **rho per 0.01 rate**. Defaults:
`rate = 0.065`, `dividend_yield = 0.0`. Any non-finite output becomes null: results never
contain NaN or infinity. Invalid inputs (non-positive underlying/strike/volatility, non-finite
years/rate, option type not `CE`/`CALL`/`PE`/`PUT`, model not `black76`/`bsm`) raise
`ValueError`. Every numeric argument of `black76`, `black_scholes`, `implied_volatility` and
`greeks_from_price` (`tick_size` included) accepts a float, an integer or a Decimal (converted to
a float before any arithmetic; the SDK's prices are Decimals); a boolean, or an integer too
large for a float, raises `ValueError` naming the parameter.

### 10.2 Time convention

- `years = seconds(expiry_instant − now) / (365 × 86 400)`; negative after expiry.
- Expiry instant = expiry date at the exchange close in IST:

| Exchange | Close (IST) | Status |
|---|---|---|
| NFO, BFO | 15:30 | assumed, open item F11 |
| CDS, BCD | 12:30 | assumed, F11 |
| MCX | 23:30 | open item F20 (MCX trades to 23:55 in US winter) |
| NSE, BSE / other | — | `ValueError` unless `expiry_time` is given |

- `expiry_time` (naive, IST) overrides the close; an aware time → `ValueError`.
- Formulas use `max(years, 1 minute)`; `years_to_expiry` reports the real value.

### 10.3 Carry adjustment

For Black-76 with an SDK-selected future whose expiry differs from the option's, on every
exchange **except MCX**: `forward = F · e^{−(r − q)(Tf − To)}`, with `Tf`, `To` the year fractions
to the future's and the option's expiry instants (same close rule) and `q` the `dividend_yield`
argument (default 0, which gives `F · e^{−r (Tf − To)}` exactly). Pass the index's dividend yield
for index options, and the **foreign** interest rate for currency options (CDS/BCD:
Garman–Kohlhagen), or supply `underlying_price` instead. MCX options are written on the future
itself: `forward = F` unchanged whatever `q`; a same-expiry future and a caller-supplied
`underlying_price` are used as the forward as is, whatever `q`. Vectors: `carry_forward.json`
(`dividend_yield` is optional there; absent means 0).

Rate and yield range: `rate` and `dividend_yield` are only checked for being finite (10.7), but an
absurd finite value (for example `-1e6`, or `1e6` with a later future) can make an exponential
factor too large for a float. Every such factor is checked where it is computed, and an overflow
(a result that would be infinite) raises `ValueError` (never a language overflow error, never an
infinite or NaN result):

| Factor | Where | Message |
|---|---|---|
| `exp(−(r − q)(Tf − To))` | the carry above (`carry_adjusted_forward`, `option_greeks`) | `rate - dividend_yield is out of range: exp(-(rate - dividend_yield) * (future_years - option_years)) overflows a float (rate=<r>, dividend_yield=<q>, future_years=<Tf>, option_years=<To>)` |
| `exp(−r·t)` | `black76`, `black_scholes`, `implied_volatility`, `greeks_from_price` | `rate is out of range: exp(-rate * years) overflows a float (rate=<r>, years=<t>)` |
| `exp(−q·t)` | the same four, BSM only (Black-76 discounts the future at `rate`) | `dividend_yield is out of range: exp(-dividend_yield * years) overflows a float (dividend_yield=<q>, years=<t>)` |

`t` is the floored time `max(years, 1 minute)` (10.2); values print in the language's shortest
round-trip form. A factor is computed only when it is needed: an expired option (`years ≤ 0`) returns
the `expired` result without one, so it never raises this error, and in `option_greeks` the carry
check runs after the quotes (10.7 step 6). A factor that underflows to 0 is not an error (the result
is then the model's limit, for example a forward of 0). Vectors: the `*overflowing*` cases in
`carry_forward.json`, `greeks_prices.json`, `implied_volatility.json` and `greeks_edge_cases.json`.

### 10.4 Contract selection

- Symbols are compared trimmed and upper-cased on both sides (input and script-master
  `TradingSymbol`, `Symbol`, `Exchange`); the selected contract's `TradingSymbol` and `Token`
  are returned trimmed.
- Option: the first row whose `TradingSymbol` equals the input (case-insensitive) and whose
  `Instrument` starts with `OPT`. Not found → `ValueError`. A matching but malformed row (blank
  `Symbol`, `OptionType` not CE/PE, missing/unparseable `Expiry` or strike, bad `LotSize`)
  → `ValueError`. Fractional `LotSize` truncates.
- Strike column: `StrikePrice` when the row has that key (NFO, MCX), else `Strike` (BFO, e.g.
  `SENSEX26N1985200CE` with `Strike` `85200`). The `StrikePrice` key decides when present (blank
  or invalid → malformed, even if `Strike` is set); neither key → malformed.
- Future: rows with `Instrument` starting `FUT`, the option's `Symbol` and `Exchange`
  (case-insensitive), non-blank `TradingSymbol` and parseable `Expiry`. Choose **same expiry**,
  else the **nearest later**, else the **nearest earlier**; ties → first in file order. None →
  `ValueError`. Vectors: `contract_selection.json`.
- Future symbol fallback: only when no row of the option's own `Symbol` is usable, and that
  `Symbol` (trimmed, upper-cased) ends in `OPT` with a non-empty stem, the same rules are applied
  to the stem plus `FUT` (BFO: a `BSXOPT` option `SENSEX26N1985200CE` takes the `BSXFUT` future
  `SENSEX26NOVFUT`). Never another family (`SX50OPT` never takes `BSXFUT`); a `Symbol` that is
  just `OPT` has no fallback; the exchange must still match. The `ValueError` then names both
  symbols tried.

### 10.5 Status precedence (`greeks_from_price`)

| Order | Condition | Status | Values |
|---|---|---|---|
| 1 | Underlying missing, non-finite or ≤ 0 | `no_price` | all null |
| 2 | `years ≤ 0` | `expired` | price = intrinsic `max(U−K,0)` / `max(K−U,0)`; at-expiry Greeks |
| 3 | Option price missing, non-finite or ≤ 0 | `no_price` | all null |
| 4 | Price ≤ discounted intrinsic + tick/2, or no IV found | `price_at_or_below_intrinsic` | price = option price, iv null, at-expiry Greeks |
| 5 | Otherwise, `years` < 30 min | `near_expiry` | Greeks at the solved IV |
| 6 | Otherwise | `ok` | Greeks at the solved IV |

Discounted intrinsic: call `max(U e^{−yt} − K e^{−rt}, 0)`, put `max(K e^{−rt} − U e^{−yt}, 0)`.
At-expiry Greeks: delta 1/0 (call) or 0/−1 (put) by moneyness, 0.5/−0.5 at the money; gamma,
theta, vega, rho 0. `tick_size` is the option's `TickSize` (0 if unknown; a negative tick size
is treated as 0). A price **above what any volatility in the solver's bracket reproduces** (up
to 5.0 = 500 %; for example a stale last price on expiry day, or a call above the discounted
underlying) has no implied volatility, so it too gets status `price_at_or_below_intrinsic`
(row 4) with `iv = null`, not an error: the status name covers both "at or below intrinsic" and
"no volatility reproduces the price". Direct model calls
(`black76`, `black_scholes`) with `years ≤ 0` return the `expired` result. Vectors:
`greeks_edge_cases.json`, `greeks_prices.json`.

### 10.6 Implied volatility solver

Returns null for a non-positive/non-finite price or `years ≤ 0`. Otherwise, on the floored `t`:

1. Bracket `[1e-4, 5.0]`. Price tolerance `1e-8` (absolute). If the price at the low end exceeds
   the target by more than the tolerance, or the price at the high end is below it by more, →
   null. If either end is within tolerance, return that end.
2. Start at σ = 0.3; at most 200 iterations. Each step: tighten the bracket by the sign of
   `price(σ) − target`; take the Newton step `σ − diff/vega` if it lies strictly inside the
   bracket, else bisect.
3. Converged (|diff| ≤ tolerance): if the Newton step is inside the bracket, evaluate it once
   (polish) and return it only if it still re-prices within tolerance, else return σ.
4. Stop early when `hi − lo ≤ 1e-15 · hi`; finally return σ only if within tolerance, else null.

Vectors: `implied_volatility.json`.

### 10.7 `option_greeks()` flow

0. Check `rate` and `dividend_yield` first: each must be a finite number (float, integer or
   Decimal; converted to float), else `ValueError("<name> must be a finite number, got <value>")`
   (NaN, infinity, a string, `None`, a boolean). Then the prices `option_price`,
   `underlying_price` and `spot_price`: each may be null, a float, an integer or a Decimal; a
   boolean, an integer too large for a float or any other type raises `ValueError` naming the
   parameter (`"<name> must be a finite number, ..."`), while a NaN or infinite value
   counts as no price (null, which leads to `no_price`, 10.5). Nothing is read or requested
   before these checks.
1. Exchange code; load the script-master index (8.4); find the option (10.4).
2. Model: `spot_price` given → BSM (no future); `underlying_price` given → Black-76 with that
   forward (no future); both → `ValueError`; neither → select the future (10.4).
3. Expired = expiry instant ≤ now.
4. Quote the future (if selected) for its `lp`. A failure propagates, except when expired and the
   failure is contract-specific (11.4): the underlying becomes null (→ `no_price`).
5. Quote the option only if `option_price` is not given and not expired (≤ 2 quotes in total).
6. Apply 10.3 and 10.5. Result: the Greeks plus `option_tradingsymbol`, `option_price`,
   `underlying_tradingsymbol`, `underlying_price`, `forward` (null for BSM), `same_expiry_future`,
   `model`, `rate`, `dividend_yield`, `expiry`, `calculated_at`.

### 10.8 Public functions and results (`tradesmart.greeks`)

Parameter names, order and defaults are part of the contract (2.1); numbers are floats unless
noted, and every numeric argument also accepts an integer or a Decimal (10.1). The result types
are listed field by field in `models.md` ("Greeks and analytics results").

| Function | Returns | Notes |
|---|---|---|
| `black76(forward, strike, years, rate, volatility, option_type)` | `Greeks` | `forward` = the future's price; `years ≤ 0` → the `expired` result |
| `black_scholes(spot, strike, years, rate, volatility, option_type, dividend_yield=0.0)` | `Greeks` | BSM on spot with a continuous yield |
| `implied_volatility(option_price, underlying, strike, years, rate, option_type, *, model="black76", dividend_yield=0.0)` | float or null | 10.6; `underlying` is the future for `black76`, the spot for `bsm` |
| `greeks_from_price(option_price, underlying, strike, years, rate, option_type, *, model="black76", dividend_yield=0.0, tick_size=0.0)` | `Greeks` | 10.5; `option_price` and `underlying` may be null |
| client `option_greeks(exchange, tradingsymbol, *, rate=0.065, dividend_yield=0.0, option_price=None, underlying_price=None, spot_price=None, expiry_time=None)` | `OptionGreeks` | 10.7 |

| Result | Fields |
|---|---|
| `Greeks` | `price`, `iv`, `delta`, `gamma`, `theta`, `vega`, `rho` (floats or null), `status` (`ok`, `near_expiry`, `expired`, `no_price`, `price_at_or_below_intrinsic`), `years_to_expiry` (the real, unfloored value) |
| `OptionGreeks` | the `Greeks` fields, then `option_tradingsymbol`, `option_price`, `underlying_tradingsymbol`, `underlying_price`, `forward`, `same_expiry_future`, `model` (`black76` or `bsm`), `rate`, `dividend_yield`, `expiry`, `calculated_at` (10.7) |

Constants: `DEFAULT_RATE = 0.065`, `DEFAULT_DIVIDEND_YIELD = 0.0`, `MIN_YEARS` = one minute,
`NEAR_EXPIRY_YEARS` = thirty minutes (in 365-day years). Vector input keys map to these
parameter names as listed in `conformance/README.md` (for example `underlying` is `forward` for
`black76` and `spot` for `black_scholes`).

---

## 11. Analytics (recommended)

Python reference: `analytics.py`, `_option_contracts.py`.

### 11.1 PCR

`pcr(by="oi"|"volume")` = Σ PE / Σ CE of open interest (or volume) over the snapshot rows; a
missing value counts as 0; Σ CE = 0 → null; other `by` → `ValueError`. Decimal arithmetic.
Option types are compared exactly: only rows with `CE` / `PE` in upper case count (a lower-case
`ce` row is ignored).

### 11.2 Max pain

Null when no row has an open-interest value (every `open_interest` missing; open interest that
is present but zero everywhere still gives a result: the lowest strike, by the tie rule).
Candidates = the distinct strikes of the rows. For each
candidate `s`: payout = Σ CE `oi · max(0, s − K)` + Σ PE `oi · max(0, K − s)` (missing OI = 0).
Max pain = the strike with the minimum payout; **ties go to the lowest strike**. Returns the
strike and every candidate's payout.

### 11.3 Buildup

| Price change | OI change | State |
|---|---|---|
| > 0 | > 0 | `long_buildup` |
| > 0 | < 0 | `short_covering` |
| < 0 | > 0 | `short_buildup` |
| < 0 | < 0 | `long_unwinding` |
| 0, missing or non-finite in either | | `neutral` |

| Baseline | Source | Price change | OI change | `basis` |
|---|---|---|---|---|
| Stream | `buildup_from_snapshot(snapshot)` | `lp − c` | `oi − poi` | `previous_close_and_previous_oi` |
| REST | `buildup(exchange, token)` | `quote().lp − quote().c` | Σ `intoi` of today's 1-minute `TPSeries` (00:00 IST → now) | `since_open` |

REST has no previous-day OI, hence the different baseline. Unparseable snapshot values count as
missing. Vectors: `pcr.json`, `max_pain.json`, `buildup.json`.

### 11.4 Chain snapshot

`option_chain_snapshot(exchange, tradingsymbol, strike_price, count=25)`:

1. `option_chain(...)`, then one `quote()` per contract (sync: sequential; async: at most 10 in
   flight).
2. **Contract-specific failures** go to `missing`: exactly `APIError` (a `Not_Ok` for that
   contract, not a subclass), `NotFoundError`, `ResponseParseError`; also a contract without a
   token, without a strike, or with an option type other than CE/PE. A missing contract is named
   by tradingsymbol, else token, else `"<strike> <type>"` (`?` for unknown parts).
3. **All other errors propagate** (authentication, rate limit, server, configuration,
   validation, transport, rejected); async cancels the remaining quotes first.
4. Rows (`ChainRow`: `strike`, `option_type`, `tradingsymbol`, `token`, and from the quote
   `last_price` (wire `lp`), `open_interest` (`oi`), `volume` (`v`), `close` (`c`)) sorted by
   strike, CE before PE; plus `exchange`, `tradingsymbol`, `fetched_at`, `missing`; `pcr()` and
   `max_pain()` helpers.

### 11.5 Public functions and results (`tradesmart.analytics`)

| Function | Returns | Notes |
|---|---|---|
| `pcr(snapshot, by="oi")` | Decimal or null | 11.1; `by` is `"oi"` or `"volume"` |
| `max_pain(snapshot)` | `MaxPain` or null | 11.2 |
| `classify_buildup(price_change, oi_change)` | `Buildup` | 11.3; numbers (Decimal, integer or float) or null |
| `buildup_from_snapshot(snapshot)` | `BuildupReading` | 11.3; `snapshot` is a stream `snapshot()` mapping (wire keys `lp`, `c`, `oi`, `poi`) |
| `OptionChainSnapshot.pcr(by="oi")`, `.max_pain()` | as above | the same functions on the snapshot |
| client `option_chain_snapshot(exchange, tradingsymbol, strike_price, count=25)` | `OptionChainSnapshot` | 11.4 |
| client `buildup(exchange, token)` | `BuildupReading` | 11.3 (`basis` `since_open`) |

| Result | Fields |
|---|---|
| `ChainRow` | `strike` (Decimal), `option_type` (`CE` / `PE`), `tradingsymbol`, `token`, `last_price`, `open_interest`, `volume`, `close` (Decimal or null) |
| `OptionChainSnapshot` | `exchange`, `tradingsymbol`, `fetched_at` (aware instant), `rows` (list of `ChainRow`), `missing` (list of names, 11.4) |
| `MaxPain` | `strike` (Decimal), `payouts` (map strike → total payout, both Decimal) |
| `BuildupReading` | `state` (`Buildup`), `price_change`, `oi_change` (Decimal or null), `basis` (`previous_close_and_previous_oi` or `since_open`) |
| `Buildup` (enum) | `long_buildup`, `short_buildup`, `long_unwinding`, `short_covering`, `neutral` |

All of them are listed field by field in `models.md`.

---

## 12. Configuration defaults

Python reference: `config.py`. Vectors: `config_defaults.json`.

| Name | Default | Validation |
|---|---|---|
| `base_url` | `https://v2api.tradesmartonline.in/NorenWClientAPIv2` | `http(s)://`; trailing `/` stripped |
| `ws_url` | `wss://v2api.tradesmartonline.in/NorenWSAPI/` | `ws(s)://` |
| `authorize_url` | `https://v2api.tradesmartonline.in/OAuthlogin/authorize/oauth` | `http(s)://` |
| `script_master_base_url` | `https://v2api.tradesmartonline.in` | `http(s)://` |
| `connect_timeout` | 5.0 s | number, finite, > 0 |
| `read_timeout` | 10.0 s | number, finite, > 0 |
| `max_retries` | 2 | integer ≥ 0 |
| `backoff_base` | 0.5 s | number, finite, > 0 |
| `backoff_cap` | 8.0 s | number, finite, > 0 |
| `rate_limit_mode` | `"wait"` | `wait` or `raise` |
| `order_rate_limit_max_wait` | 1.0 s | number, finite, ≥ 0 |
| `order_rate_limit_per_second` | 10 | integer 1 to 30 (orders per second); above 10 only with `algo_id` set |
| `order_rate_limit_per_minute` | 200 | integer 1 to 600 (orders per rolling 60 s), at least `order_rate_limit_per_second` (F33) |
| `algo_id` | null | a non-blank string or null (a boolean or number rejected); sent as `algo_id` on every `PlaceOrder` / `ModifyOrder` unless the call passes its own (2.4) |
| `user_agent` | `tradesmart-<lang>/<version>` | string of printable ASCII (no control or non-ASCII character) |
| `log_payloads` | false | boolean |
| `check_token_expiry` | true | boolean |
| `heartbeat_interval` | 30.0 s | number, finite, > 0 |
| `ws_auth_timeout` | 10.0 s | number, finite, > 0 |
| `reconnect_max_attempts` | 20 | integer ≥ 0, or null (unlimited) |
| `reconnect_backoff_base` | 1.0 s | number, finite, > 0 |
| `reconnect_backoff_cap` | 60.0 s | number, finite, > 0 |
| `keepalive_expiry` | 50.0 s | number, finite, > 0 |
| `max_connections` | 10 | integer ≥ 1 |
| `max_keepalive_connections` | 5 | integer ≥ 1, ≤ `max_connections` |
| `stream_backlog_limit` | 1000 | integer ≥ 1 |
| `script_master_refresh_time` | `"07:00"` (IST) | string `HH:MM`, 24-hour |
| `script_master_recheck_minutes` | 15 | integer ≥ 1 |
| `instruments_dir` | null (the user cache folder, `~/.cache/tradesmart` in Python) | a non-empty path or null (a leading `~` is expanded); the script-master folder when no `dest_dir` is given (8.1) |

Configuration is immutable; invalid values raise `ConfigurationError` when the configuration is
created, with a message that names the setting and never echoes the value. Types are strict:

- A **number** is an integer or a float, never a boolean, a string or null; "finite" rejects
  NaN and ±infinity: `"<name> must be a finite positive number"`
  (`"order_rate_limit_max_wait must be a finite number >= 0"`).
- An **integer** setting rejects every float (also `2.0` and NaN) and booleans:
  `"<name> must be an integer >= <minimum>"`
  (`"reconnect_max_attempts must be an integer >= 0 or None"`). So a NaN can never disable
  retries, the backlog limit or the reconnect budget.
- The URL and text settings must be strings (`"<name> must be a string"`), the two flags
  booleans (`"<name> must be True or False"`; another SDK writes its own boolean words).
- `algo_id` is a non-blank string or null (`"algo_id must be a non-blank string or None"`;
  another SDK writes its own null word), and `order_rate_limit_per_second` above 10 needs it:
  `"order_rate_limit_per_second above 10 needs algo_id (TradeSmart requires an algo ID for more
  than 10 orders per second)"` (3.4).

The vectors spell the non-finite inputs as the strings `"NaN"`, `"Infinity"`, `"-Infinity"`
and give each error's `message`. Fixed constants (not configurable): subscription limit 1000,
rate-limit table (3.4), Greeks constants (10), async snapshot concurrency 10, CLI callback
timeout 120 s and default port 8080 (`--port` changes it, 6.6), stream closing-handshake timeout
2 s (`CLOSE_TIMEOUT`).

---

## 13. Security rules

Python reference: `_logging.py`, `_transport/codec.py`, `_token_store.py`.

| Rule | Detail |
|---|---|
| Redacted keys (any case) | `authorization`, `access_token`, `accesstoken`, `susertoken`, `refresh_token`, `checksum`, `code`, `secret_key`, `app_key`, `password` → `***` |
| Redacted patterns in text | `Bearer <anything>`; `"key": "value"` / `'key': 'value'` pairs; `key=value` pairs for the keys above |
| Where | Every SDK logger (each child logger has its own filter), error `raw`/`body`, `AccessToken.raw`, payload debug logs |
| Never logged | Tokens, secret keys, checksums, auth codes, order payloads, prices, symbols; WebSocket frame dumps (the connect frame carries the token) |
| Error bodies | Redacted and truncated to 2000 characters (500 for script-master errors) |
| Parse and transport errors | Name fields only; on the REST call path never chain the underlying exception (it can embed raw values, the unredacted body or the request headers), 4.1; a send-time error from the caller's hook is reported by its type name only |
| Nested `raw` | Secret-key values masked in the `raw` of nested models and of directly validated models (7.2) |
| `repr`/`toString` | Clients and streams show only the client id and state; token fields masked |
| Logging setup | Logger root `tradesmart` (children `transport`, `orders`, `stream`, `instruments`); a no-op handler only; never configure the application's root logger; never print (except the CLI) |
| Payload logging | Off by default (`log_payloads`); DEBUG and redacted when on |
| Token file | 0600 file in a 0700 directory, atomic write, ownership/permission check on POSIX (6.5) |
| CLI | Callback on the loopback by default (`--listen-host`, IPv4 only, for containers, with a warning for a non-loopback address; 6.6), no access log, secret key only from env, the owner-only saved-keys file (6.7) or a no-echo prompt, account check before saving; saved keys never printed or logged |
| Redirects | Never followed (3.1): an order body is never re-sent to another address |
| Header values | Tokens are checked when set (visible ASCII only, 6.3) and `user_agent` when configured (printable ASCII, 12); every request is built before it is sent (5.2), so a header can never be split or injected |
| Credentials | Never commit credentials; fixtures use synthetic values only; never copy the sample tokens published in the official docs |

---

## 14. Testing requirements

| Requirement | Rule |
|---|---|
| Docker-only | Build, test and lint inside the SDK's container image; never install runtimes or packages on the host |
| Mocked by default | Unit and integration tests use mocked HTTP and a local WebSocket server; no real orders, ever, in automated tests |
| Network block | The default test run must fail any non-loopback connection or DNS lookup (loopback allowed for local mock servers and the CLI callback); the live suites are the only exemption |
| Saved login isolation | Default tests point the token file at an empty temp folder, never the developer's real login |
| Shared fixtures | `specifications/test-fixtures/*.json` (synthetic or anonymised official examples) for every typed endpoint (the GTT acknowledgements, pending and triggered GTTs and the enabled alert types included; iceberg has none), the error bodies and one WebSocket message per `t` |
| Conformance | Load every file in `specifications/conformance/` and assert every case at its stated tolerance (1.1) |
| Live read-only suite | Opt-in (`TRADESMART_LIVE_TESTS=1`); uses `from_env()`; read-only methods only (enforced by a test that scans the suite for state-changing calls, including `request()`); market closed or missing contracts skip, never fail; records shapes and latencies, never secrets |
| Live order suite | Excluded from every default test run (Python: `-m "not live_orders"` in the default options; it runs only when selected explicitly with `-m live_orders`). Double opt-in (`TRADESMART_LIVE_TESTS=1` and `TRADESMART_LIVE_ORDER_TESTS=1`, own marker); only on the owner-approved zero-funds test account (`TRADESMART_LIVE_ORDER_ACCOUNT` must equal the profile id and client id); zero-funds check before each order; LIMIT BUY DAY well below market, never MARKET or SELL; ≤ 3 orders per run; one open order at a time; never resend; cancel once in cleanup and confirm a final state; any fill or lost order fails with "CHECK THE ACCOUNT". **Test-environment mode** (a non-production TradeSmart test environment, where orders move no real money): it applies only when the run names that environment explicitly **and** the client's REST host is a test host (judged on the host part of the base URL only; never the production host, nor a host that contains it); otherwise: with no environment named, every production rule applies; any other environment name, or the test environment's name with a host that is not a test host, refuses every order test. In this mode the zero-funds check is lifted, the cap is **40 orders per run and 2 per test** (an optional broad test-environment matrix replaces the cap by a per-run ledger of its own orders, at most 50 of them open at once, the SDK's pacing and per-segment circuit breakers; see the row below), and the lifecycle tests may send other order types (IOC, stops, MARKET) and let an order fill; every fill is closed by one DAY LIMIT SELL of the filled quantity, and the position must return to its earlier net quantity, else "CHECK THE ACCOUNT". Every other rule above still applies. Production keeps the cap of 3, the zero-funds check and the LIMIT BUY shape (Python: `TRADESMART_ENV=uat` and a REST host containing `uat`; `sdk-python/tests/live_orders/README.md`). **Funded mode** (optional; a production account approved by the owner that holds little money): a third opt-in turns it on, and a second one allows the fills of stage 2; it applies only when no test environment is named, the REST host is exactly the production host, inside market hours (the outside-hours override is refused), and the account's cash, and its cash and collateral fields together, are at most a balance ceiling (₹25,000; a setting may only lower it), checked when the run starts and again in each test before it places an order; any failed check refuses every order test, never a fall-back to other rules, and funded mode and test-environment mode are never both on. Its own tests only (the production-shape tests skip in this mode): cash and index options only, intraday product, LIMIT and stop-limit orders only (never MARKET or stop-market), 1 share or 1 lot; at most **10 orders per run and 2 per test**, **₹2,000 per order** (price or trigger, whichever is higher, times quantity; a closing SELL is exempt) and **₹5,000 of BUY orders per run** (reserved before sending, given back only when the BUY ends unfilled); option strike: the nearest out-of-the-money call of the nearest expiry whose premium × lot is at most 90 % of the order limit. Stage 1 never means to fill (LIMIT BUYs far below the market rested, modified and cancelled, an IOC far below, a stop-limit BUY above the market); an unexpected fill is sold back and fails the test. Stage 2 buys 1 share or 1 lot at the best offer and sells exactly the filled quantity back at the best bid, then checks positions and trades; it runs only with the fills opt-in and when no stage-1 test failed, and sends a BUY only before 14:30 IST (later it skips, nothing sent); a BUY the broker rejects fails the test. A SELL only ever closes a fill of the same test; a close that does not complete, a fill quantity that cannot be read, or any "CHECK THE ACCOUNT" stops the run, and every later test fails before any call. When the order cap does not fit both stages in one run, run stage 1 alone, then stage 2 alone only after a clean stage 1 (Python: `TRADESMART_LIVE_ORDER_FUNDED=1`, `TRADESMART_LIVE_ORDER_FILLS=1`, `TRADESMART_LIVE_ORDER_MAX_BALANCE`; `test_funded_live.py`, stage 2 with `-k stage2`; see the README above). The live suites always run **serially in one process**: a parallel test runner would break "one open order at a time" and the order cap (Python: a live opt-in forces `-n 0`, and a live order test refuses to run inside a parallel worker) |
| Live GTT test (optional) | A GTT whose condition is already true when it is placed fires at once and places a real order (2.8), so a live GTT test has its own gates, and an SDK adds none without them. It runs only with the live order suite's two opt-ins, serially, and only in test-environment mode or on the zero-funds production account (the order suite's account check, and the zero-funds check before the order is planned); never in funded mode: the funded opt-in, or the outside-hours override, set to **any** value refuses it before any call (Python: `TRADESMART_LIVE_ORDER_FUNDED`), as does a session in funded mode; inside market hours only. One GTT on an NSE cash share: alert type LTP above (`LTP_A_O`) with the trigger at **3 × a fresh last price** (a `quote()` read just before sending, rounded up to the tick), refused before sending when that quote has no last price or tick or the condition would already be true (the last price at or above the trigger); its order a **CNC LIMIT BUY of 1 share** well below the market (the order suite's price; refused when it is less than 1 % below the last price), order validity `DAY`, alert validity `DAY` (a GTT left behind ends with the trading day); a unique tag (`remarks`). `pending_gtts()` must be readable before the place. The place is sent once; after an accepted or ambiguous place the GTT is read back from `pending_gtts()` by its tag or acknowledged alert id (a few reads a short pause apart: the list can lag); the trigger is **modified once** (3.1 × a fresh last price, the same checks first); then, in a `finally` block, **one cancel per alert id** of the union of the pending rows of the tag and every acknowledged alert id (also one not yet visible). Place, modify and cancel each count against the run's order cap and the test's own cap of 3, checked for room for all three before anything is sent; nothing is resent. A GTT of the tag or of a known id still pending after the cancels, `pending_gtts()` unreadable after them, a sign that it fired (`triggered_gtts()` by tag or alert id, except a row whose `stat` is `CANCELLED` in any case: the test environment lists cancelled GTTs there, F32; such a row is recorded, not counted as fired; or an order of the tag or whose parent id is a known alert id in `orders()`, or neither readable), or an accepted or ambiguous place whose GTT was never seen and not cancelled fails with "CHECK THE ACCOUNT"; an interruption at any step prints and records a "CHECK THE ACCOUNT" alert. Two more tests only read `gtt_alert_types()` and `triggered_gtts()` (Python: `sdk-python/tests/live_orders/test_gtt_live.py`, `gtt.py`) |
| Live test-environment matrix (optional) | A broad live-order matrix of the documented order dimensions, in test-environment mode only: refused on production and whenever the funded opt-in is set to any value, with the order suite's opt-ins and account check, serially in one process. A small daily tier runs with those opt-ins; the full and stress tiers need a third opt-in (Python: `TRADESMART_LIVE_ORDER_UAT_MATRIX=1`; stress only when selected explicitly). It has no order cap; instead: a per-run ledger written before and after every state-changing call (an order is the run's only by its ledger: its order id, or the run's unique tag), every non-final order in the account must be the run's (else the run stops, the others are never touched), at most 50 of its orders open at once, a declared count of place calls per case, the SDK's pacing (never raised), per-segment circuit breakers (one stuck order, one "CHECK THE ACCOUNT", or three unexpected rejections in a row), a stop file, one cancel per order per session, resume on the same trading day only, and never a resend. Fills go one at a time per instrument; a fill the environment does not let close stays in the run's position ledger within a per-instrument ceiling (Python: `sdk-python/tests/live_orders/uat_matrix/`, `tests/live_orders/README.md`) |
| Test environment | Optional. The live suites may run against a non-production test environment chosen by name. Every URL of that environment must be given (a missing one stops the run; never a fall-back to the production URLs), and it keeps its own saved login, separate from the production one: the run refuses the production token file, a token taken from the shell, and a client id that differs from that saved login's. Read-only tests for data the test environment does not serve (F24, F25) skip there with a reason |
| Live results | Live recordings (shapes, latencies) are written to a local results folder (Python: `.live-results/`) that is internal at any depth: never committed to a public repository or exported. Recorders redact account identifiers (`client_id`, `user_id`, `account_id`, `uid`, `actid`) and record only whether they match (`*_matches`), never the values |
| Public export | When internal material is exported to a public repository, the export refuses to run without the list of identifiers that must never appear (Python tooling: `TRADESMART_EXPORT_FORBIDDEN_IDS`, with a clear message; a dry run only warns), and fails when any of them is found |
| Packaging | Each SDK folder keeps its own public ignore file (`sdk-<lang>/.gitignore`, paths anchored to that folder), so a package build never picks up the monorepo's root ignore file or internal paths |
| Gates | Tests on the newest and the oldest supported runtime, lint/format, strict type checks, package build |
| Operating systems | Every SDK supports Linux, Windows and macOS, and its CI runs the default test suite on all three: every supported runtime on Linux, at least one on Windows and on macOS (Python: `.github/workflows/python-ci.yml`). Linux and Windows run on every push and pull request. A public repository runs macOS on every push and pull request too (GitHub-hosted runners are free there); a private repository, where macOS runners cost ten times the minutes, may run the macOS job only on a release tag (`v*`) and on a manual run, and it must pass before any release or before the repository goes public. Tests must not depend on the operating system: point the home folder at a temp folder through every variable a platform reads (`HOME`, and `USERPROFILE` on Windows), or the tests write to the real home and share files; pin the platform when a test checks platform-specific output (shell commands for setting a variable, paths); skip, with a reason, a test whose mechanism an operating system lacks (for example a simulated interrupt signal on Windows); compare computed floating-point values with a relative tolerance (1e-12), never exactly, because maths libraries differ in the last bits; and wait (with a time limit) for a helper thread or task before checking its results, and accept a connection that the test itself has closed. A container-only test run (Docker on Linux) does not show any of these |

---

## 15. Live-verified facts and open items

| Id | Topic | Status | SDK behaviour |
|---|---|---|---|
| F1 | `PlaceMultiOrder`, `GetQuotesMultiple` schemas | Pending | `request()` only; rate groups applied |
| F2 | UAT (test) environment access | Resolved: TradeSmart provided test-environment access (2026-11-11) | No library change: the base URLs are configurable (12); default tests stay mocked; optional live runs there (14) |
| F4 | Holdings on an empty account | Verified live (empty case) | Empty list; non-empty shape unverified |
| F6 | Server idle keep-alive | Measured 55–65 s | Keep-alive 50 s |
| F7 | Concurrent connection limit | Pending | Pool 10 / 5 (placeholders) |
| F8 | GTT and iceberg orders | Documented 2026-11-12 (`03-gtt-orders.md`, `04-iceberg-orders.md`). GTT: owner answers 2026-11-12 (a GTT already true when placed fires at once; validity 365 days or the contract expiry, whichever is earlier). Iceberg methods on hold (owner) | GTT: six methods (2.8); iceberg: only the success words and never-retry rules through `request()` (4.2.1, 5.2) |
| F9 | Real `ModifyOrder` success body | Pending | `norenordno` → `result` → caller id |
| F10 | Script master refresh and validators | Verified: 07:00 IST, `Last-Modified`, `ETag`, 304; MCX extra columns, trailing empty column | 8.2, 8.3 |
| F11 | Expiry close times | Pending | NFO/BFO 15:30, CDS/BCD 12:30 |
| F12 | Multi-leg (2L/3L) orders | Pending | Not exposed |
| F13 | Loopback Redirect URL | Resolved: `http://127.0.0.1:8080/callback` accepted | CLI default |
| F14 | `order_history()` entry order | Pending | Returned as received |
| F15 | Quotes after expiry close | Resolved: quotes still returned | Option not quoted after expiry; `no_price` fallback kept |
| F16 | IPv6 API domain | Pending | Base URLs configurable |
| F17 | Community forum URL | Pending | — |
| F18 | WebSocket ack `s` | Live sends `"OK"`, docs say `"Ok"` | Case-insensitive |
| F19 | MCX API orders | Rejected until MCX API trading is approved for TradeSmart | No SDK change |
| F20 | MCX close for expiry instants | Pending | 23:30 IST |
| F21 | MCX `qty` units or lots | Resolved: MCX order quantity is in **trade units**, a whole multiple of the lot size, like NFO/BFO (owner, 2026-11-12, replacing an earlier "lots" answer). Evidence: UAT rejected CRUDEOIL `qty=1` with "ORA:Quantity 1 is not a multiple of lot size 100.00"; production `security_info()` lot sizes and `frzqty` are in the same units (CRUDEOIL lot 100 / `frzqty` 2500, GOLD 1 / 1, SILVER 30 / 90) | Sent as given; `place_sliced_order` sizes MCX slices in units like every exchange (5.5, 5.7); `prcftr` is informational (5.5) |
| F22 | Test-account RMS block | Account setup | No SDK change |
| F23 | Ok = accepted, cancel differences, NFO qty in units | Observed live | 5.4, 5.5 |
| F24 | `EODChartData` on the test environment | Pending: every call timed out there (NSE, NFO, MCX); production answers | No SDK change; the live test skips on the test environment |
| F25 | MCX data on the test environment | Pending: no MCX search results, quotes, security info or option chains there | No SDK change; MCX live tests skip on the test environment |
| F26 | Order stuck before the exchange (test environment, BSE) | Pending: acknowledged, then `PENDING` and never confirmed; `ModifyOrder` / `CancelOrder` refused ("SAF:order is not open to modify" / "... to cancel"), so it could not be cancelled through the API | No SDK change; a live order suite treats it as an open order ("CHECK THE ACCOUNT") |
| F27 | `MARKET` (`MKT`) orders placed through the API | Refused: `stat Not_Ok`, "ALGO_CHK: MKT Order type not allowed for API order" (owner-confirmed for production; observed on the test environment). `SL-MKT` refused the same way on the test environment ("ALGO_CHK: SL-MKT Order type not allowed for API order", 2026-11-12); not yet confirmed on production | Not blocked client-side; documented; raises `OrderRejectedError` |
| F28 | BFO script-master layout | Verified live 2026-11-12 (production): strike column `Strike`; options `BSXOPT`, futures `BSXFUT` (SENSEX) | 8.3; strike column and `OPT`→`FUT` future fallback (10.4) |
| F29 | Order rate limit and algo ID | Docs: 30/s and 600/min. Owner: most clients are limited to 10 orders per second; a higher rate needs an exchange-approved strategy and its algo ID, checked by the TradeSmart backend. **Resolved 2026-11-12:** the docs (2026-11-12 copy) list `algo_id` (String, Optional) on `PlaceOrder` and `ModifyOrder`, "Required when placing more than 10 orders per second" | Default 10 per second, configurable 1 to 30 (3.4); `ClientConfig.algo_id` and per-call `algo_id` sent on `PlaceOrder` / `ModifyOrder` (2.4); above 10 per second requires `algo_id` (12) |
| F30 | OCO (one-cancels-other) GTT | Documented 2026-11-18 (`04-oco-orders.md`); observed on the test environment and in the web terminal (F35) | Implemented: `place_oco`, `modify_oco`, `cancel_oco` and `OcoLeg` (2.9) |
| F31 | Closing SELL at the last price on the test environment | Pending (test environment only; recorded, not an SDK issue): a LIMIT BUY at the last price fills within seconds, but the LIMIT SELL that closes it, at the last price or the best bid, stays `OPEN` (observed 2026-11-12, also for a SELL entered by hand); not seen on production | No SDK change; a live order suite on the test environment records it as a non-strict expected failure (Python known-issue key `closing-sell`) and keeps the position in the run's own position ledger, within a per-instrument ceiling, instead of failing with "CHECK THE ACCOUNT" |
| F32 | GTT write `stat` words on the test environment | Observed 2026-11-13 (test environment): `PlaceGTTOrder` answered `{"stat": "OI created", "al_id": ...}` and `CancelGTTOrder` answered `{"stat": "OI deleted", "al_id": ...}`, while the docs say `Oi created` and `Oi delete success`. The words differ in letter case (`OI created`), and the cancel word is a different word (`OI deleted`). `GetTrgOi` (`triggered_gtts()`) also listed a **cancelled** GTT, its row `stat` `CANCELLED`. Pending: TradeSmart to confirm production's words | The GTT success words compare ignoring letter case only (4.2.1); Amended 2026-11-18 (owner, F35): `OI deleted` is the success word of `CancelGTTOrder` and `CancelOCOOrder`; the documented `Oi delete success` is `OrderStatusUnknownError` (the caller checks `pending_gtts()`); `Invalid Oi` on a GTT modify or cancel is a definite `OrderRejectedError`. A live GTT check treats a triggered row whose `stat` is `CANCELLED` (any case) as not fired |
| F33 | Order rate per minute | Open. Docs: 600 per minute. Observed 2026-11-14 (production): an order was refused once 200 had been received in the current minute, with "Order Recieved 202 in a current minute exceeds Limit 200 for user". Ask TradeSmart whether the limit is 200 or 600 and whether ModifyOrder, CancelOrder and ExitSNOOrder count toward it | Default `order_rate_limit_per_minute` 200 (3.4, 12); with 200 orders in the rolling minute the SDK waits for a slot within `order_rate_limit_max_wait`, else raises `RateLimitError` before sending |
| F35 | OCO behaviour against the docs (TradeSmart's web terminal, and the public API on the test environment) | Observed 2026-11-18. (a) **Slots:** the web terminal always sends the stop-loss as `x` (`place_order_params`) and the target as `y` (`place_order_params_leg2`), buy and sell; its form requires, for a buy, the stop-loss trigger above the last price and the target below (a sell mirrors it). The docs' sell example puts the target in `x`. (b) **Reply words (test environment, public API):** place `OI created`, modify `OI replaced`; cancel **`OI deleted`** (docs: `Oi delete success`; the same word as the GTT cancel, F32); a modify or cancel of an alert that was already cancelled answers **`Invalid Oi` with an `al_id` and no `emsg`**, so the docs' rule "`al_id` and no `emsg` is success" would read a failure as success. (c) **List rows:** an OCO row has a top-level `d` (= `x`'s) and `remarks`, leg 1's fields copied to the top level, legs without `tsym` / `exch` / `ret` / `trgprc`, `qty` a JSON number, only leg 1 with `s_prdt_ali`, legs with `ipaddr` (the placing IP address); a cancelled OCO is listed by `GetTrgOi` with `stat` `CANCELLED`. (d) The web terminal sends `LMT` legs only, no `trgprc`. (e) Nested request values (`oivariable`, the legs) need a nested body encoder. Ask TradeSmart: does the slot matter (is the direction derived from the last price, or fixed) and is the docs example right; production's cancel word (`OI deleted`?); is `Invalid Oi` always a definite failure; does a leg `remarks` come back in list rows | `place_oco` / `modify_oco` / `cancel_oco` (2.9): stop-loss in `x`, target in `y`, a side check before sending (BUY: stop-loss trigger above the target trigger; SELL: below; equal refused); words as in 4.2.1; **owner decision 2026-11-18 (amends F32):** `CancelOCOOrder` and `CancelGTTOrder` succeed only on `OI deleted` (the documented `Oi delete success` becomes `OrderStatusUnknownError`), and `Invalid Oi` is a definite `OrderRejectedError` on the OCO and GTT modify and cancel; nested bodies in `request()` and the typed calls (3.2); `stop_loss` / `target` of a list row chosen by trigger value and side, not by slot; no OCO triggering in the offline broker (16) |

Test environment (2026-11-13): orders without `algo_id` were accepted with a server-assigned default algo id until mid-day; from the evening the test environment refuses them (`ORA: Algo id not set for an algo order from source API`); production behaviour unconfirmed. The SDK sends no `algo_id` unless set.

**Owner-confirmed rules, not in the official docs.** These come from TradeSmart's answers to
the SDK owner, not from the published API docs; an SDK applies them like documented behaviour:

| Rule | SDK behaviour |
|---|---|
| An access token expires at the next 06:00 IST (the docs leave the lifetime undocumented) | 6.3, `token_expiry.json` |
| `product=MTF` is not offered on MCX | Rejected client-side (2.4) |
| The App ID on the API Dashboard is the App Key; IPv4 static IPs for now; the leaked-key procedure | 6.1 |
| `jData` is URL-decoded by the server, so only `%`, `&` and `+` are escaped | 3.2 |
| `MARKET` orders placed through the API are refused by TradeSmart | Not blocked client-side (the server decides; nothing changes if TradeSmart allows them later); documented for users |

**Deliberate deviations from the official docs** (each verified live or chosen for safety):
`amo` always sent and `dscqty` only when given (2.4), `OrderBook` without `actid` (2.3), the
ack `s` compared case-insensitively (F18), `Ok` read as "accepted" (F23), and `MKT` / `SL-MKT`
accepted for `modify_order` and `order_margin` (2.4).

Other live observations (2026-11-10): warm `GetQuotes` p50 ≈ 11 ms; all read-only endpoints,
the script master and the stream behave as specified above on the live server.
Live-verified 2026-11-11: the server answers the SDK heartbeat (`h`) with `hk`, and an idle orders-only
stream stays connected for 150 s with no reconnect (`heartbeat_acks` >= 1).
Test environment, 2026-11-11 (not production; prices there depend on the time of day): a LIMIT
BUY at the last price fills within seconds, one priced above it may stay open; `MARKET` is refused
as on production (F27); a cancel answers `stat Ok` with `result` = the order id, the same shape as
production; order book, order history and stream order updates agreed.

---

## 16. Offline broker (optional)

Python reference: `testing/` (`tradesmart.testing.OfflineBroker`); user guide `sdk-python/docs/testing.md`.

An SDK **may** offer an offline broker: a local TradeSmart server that the SDK's real clients and
streams talk to, so a client can test their own strategy code with no account, keys, network or
market hours. If it does, the lifecycle and drills below are the reference.

| Item | Rule |
|---|---|
| Isolation | Opt-in import (`import tradesmart` never loads it); no client, stream or config code depends on it; REST in memory, the stream server on `127.0.0.1` only; a private script-master folder (`instruments_dir`, 12), removed on close |
| Lifecycle | Clients and streams created by the offline broker keep it alive (a strong reference from each client or stream to the broker that made it), so a test that keeps only the client still works; the private folder lives until the offline broker is closed, or until no client or stream refers to it any more (then a finalizer removes it) |
| Clients | Real clients and streams wired to the offline broker, already logged in with a synthetic token valid for **24 hours** (so a test run never meets the 06:00 IST expiry); a login through the offline broker's `GenAcsTok` returns a synthetic token with the normal 06:00 IST expiry (6.3) |
| Waits | The production retry count (reads only, 5.1) and reconnect behaviour, with ≈1 ms backoff and a 0.5 s heartbeat, so no retry or reconnect waits in real time; orders are never retried (5.2); the client-side order rate limit (3.4: 10 per second by default, 200 per minute, waits capped at 1 s) still applies, so a test that places more than 200 orders a minute gets `RateLimitError`; such a test builds its client with a larger `order_rate_limit_max_wait` (for example 60 s) |
| No market | The offline broker never fills, prices or rejects by itself; no margin, RMS or funds model. It does not repeat the SDK's client-side validation |
| State set by the test | Instruments (also the script master, search, chains, Greeks), quotes, candles, funds, holdings, positions, user details, a fixed reply per endpoint. Setting a quote, a tick or candles rejects unknown field names and non-numeric values at once (a test error, not a reply); unset quote fields are `0` |
| Candles | `TPSeries` and `EODChartData` replies are newest first and filtered to the requested range: `TPSeries` by `st` .. `et` (epoch seconds, inclusive; every candle when absent), `EODChartData` by IST day `from` .. `to` (inclusive); candles without a time are kept, last. A non-numeric bound → `Not_Ok` "Invalid Input : ..." |
| New order | `Ok` with a new order id; `OPEN` (`TRIGGER_PENDING` for `SL-*`); by setting, `PENDING` until accepted, or `REJECTED` with a set message |
| Test-driven transitions | accept (`PENDING` → working), fill (full or partial; partials accumulate; `COMPLETE` when filled), trigger (`TRIGGER_PENDING` → `OPEN`), cancel, reject |
| Strategy calls | Modify / cancel / exit apply to the book; on a closed order an immediate `Not_Ok` "order is not open to modify" / "... to cancel" (live NSE first acknowledges such a cancel, then refuses it; F23). Exit (`ExitSNOOrder`) applies to cover and bracket orders only (product `H` / `B`): an unknown order → `Not_Ok` "Order not found to Exit"; another product → "order is not a cover or bracket order"; a closed order → "order is not open to exit"; otherwise the whole order becomes `CANCELED` and the reply is `{request_time, stat: "Ok", dmsg: "success"}` as documented (that an exit cancels the whole order is assumed) |
| Every change | Order book row and order history row (newest first) updated, and an `om` on subscribed streams; a fill also adds a trade-book row, updates the position net quantity and average (no funds change) and pushes a `pm` |
| GTT | Place, modify and cancel apply to a list of pending GTTs and answer the words `Oi created`, `Oi replaced` and `OI deleted` (the cancel word of the test environment, F32 / F35 b) with `request_time` and `al_id`; an unknown or already triggered alert id → `Not_Ok` "GTT not found to Modify" / "... to Cancel"; a modify that changes `ai_t` → `Not_Ok` "ai_t cannot be modified"; missing or non-numeric fields → `Not_Ok` "Invalid Input : ...". The pending and triggered lists answer the documented row shape (`qty` a JSON number), or `Not_Ok` "no data" when empty (assumed); the enabled alert types are the documented example (`ATP`, `LTP`). A GTT never fires by itself: the test fires one (`trigger_gtt`), which places its order as a new order would be (with `add_ord_id` = the alert id) and lists the GTT as triggered with `norentm` |
| OCO | `PlaceOCOOrder`, `ModifyOCOOrder` and `CancelOCOOrder` apply to the same list of pending alerts (an OCO has two legs and two trigger values, 2.9) and answer `OI created`, `OI replaced` and `OI deleted` with `request_time` and `al_id`; a modify or cancel of an unknown or already cancelled alert answers the observed `Invalid Oi` with an `al_id` and no `emsg`. The pending and triggered lists answer the observed OCO row shape; a cancelled OCO is listed as triggered with `stat` `CANCELLED`. An OCO is **never triggered by the offline broker**: no fired OCO has been observed, so the fired row and order shapes would be invented; `trigger_gtt` on an OCO alert is an error that says so |
| Not modelled | `PlaceMultiOrder` (F1), `GetOrderMargin`, the iceberg endpoints and any other endpoint without a route: `Not_Ok` "… does not model <endpoint>; set a reply …" |
| REST drills (once, on the next call of one endpoint) | `reject(message)` → `Not_Ok`; `timeout_after_send()` → the call takes effect, then a read timeout (`OrderStatusUnknownError` on an order); `connect_error()` → nothing sent or recorded (`NetworkError`); `rate_limit(retry_after)` → HTTP 429 with a JSON `Not_Ok` body (assumed shape); `server_error(status=503, json=False)` → by default a non-JSON HTML gateway page, like a real 5xx (an order gets `OrderStatusUnknownError`, a read `ServerError`), with `json=True` a JSON `Not_Ok` body instead (an order gets `OrderRejectedError`); `bad_body()` → non-JSON; `session_expired()` → `Not_Ok` "Session Expired" |
| Stream drills | Drop every connection (the client reconnects and resubscribes); refuse the next login (ack `s="Not_Ok"`); stop / resume answering heartbeats (silence detection) |
| Stream acks | As documented (9.2): no ack for an order subscription |
| Read access | The orders received (fields, status, fills, raw payload, history) and every REST call (endpoint and payload, secrets masked, 13) |
| Shapes | Built from the documented field names and `specifications/test-fixtures/`; every reply the offline broker can produce must parse with the SDK's models. Unconfirmed shapes are listed as "assumed" in the user guide |
