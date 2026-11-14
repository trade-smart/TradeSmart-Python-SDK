# `tradesmart` Python SDK — architecture

How the Python SDK in `sdk-python/` is structured, how a call flows through it, and which design
decisions keep it safe. User-facing reference: `sdk-python/README.md`; development workflow:
`sdk-python/CONTRIBUTING.md`.

## Principles

1. **Faithful** — paths, field names and behaviour come only from TradeSmart's official API
   documentation (or TradeSmart's written answers to open questions). Nothing is invented;
   undocumented endpoints are reachable only through the low-level `request()`.
2. **Safe** — state-changing calls are never retried; ambiguous outcomes surface as
   `OrderStatusUnknownError`; secrets never reach logs, `repr()` or exception messages.
3. **Strict out, lenient in** — requests are validated before sending; responses keep unknown
   fields and the raw payload.
4. **Sans-IO core** — request building and response parsing are pure functions shared by the sync
   and async clients and by the tests.
5. **Familiar and flat** — one client object with flat methods (`client.place_order()`,
   `client.orders()`) and the parameter names algo developers already know (`order_type`,
   `validity`, `order_id`, `tag`, `from_date` / `to_date`).
6. **Fast by default, nothing new to learn** — warm connection reuse and a stream reader that
   never waits for user code, without extra concepts in the public API.
7. **Lean** — runtime dependencies are `httpx`, `pydantic` v2 and `websockets` only.

## Layers

```
User code
  │
  ├── TradeSmart / AsyncTradeSmart              client.py / async_client.py   public facades, flat methods
  │      └── script master                       instruments.py              download / parse (load, find)
  ├── TradeSmartStream / AsyncTradeSmartStream  stream/                       WebSocket
  │
  ├── greeks.py         pure option pricing: Black-76 / Black-Scholes-Merton, IV solver, `Greeks`, `OptionGreeks`
  ├── analytics.py      pure chain analytics: `pcr`, `max_pain`, `classify_buildup`, `buildup_from_snapshot`
  ├── _option_contracts.py  contract lookup, underlying-future selection, expiry instants, Greeks planning, chain snapshot (no I/O)
  ├── _contract_cache.py    per-client cache of the parsed script master (`ContractCache`, `AsyncContractCache`)
  ├── _client_core.py   credentials + token provider, env loading (incl. the saved login), raw-call builder
  ├── _cli.py           `python -m tradesmart login | token-path | logout` (the only module that prints)
  ├── _token_store.py   the per-user token file: path, atomic owner-only save, safe load
  ├── _key_store.py     the App Key and Secret Key saved per Client ID by the login command (`credentials.json`)
  ├── _slicing.py       sliced orders: slice planning and checks for `place_sliced_order` (no I/O)
  ├── _ticks.py         `round_to_tick` (Decimal price helper)
  ├── _session.py       login URL, GenAcsTok call and its error mapping (shared by both clients)
  ├── endpoints/        EndpointSpec (name, auth?, state_changing, expect, allow_empty, rate_group, parser)
  │                     + *_call() builders: Pythonic args → validated request model → Call(spec, wire payload)
  ├── models/           pydantic v2 request + response models, field types (Price, Volume, Quantity, EpochTime, ISTDateTime, enum-or-str)
  ├── _transport/       codec (jData encode, response classification), sync/async httpx transports, order outcome logging,
  │                     connection pool limits, retry policy, rate limiter
  ├── auth.py           authorize URL, checksum, AccessToken, GenAcsTok spec
  ├── config.py         ClientConfig (frozen dataclass)
  ├── enums.py          Exchange, TransactionType, OrderType, Validity, Product, OrderStatus, GttAlertType, GttValidity
  ├── errors.py         exception hierarchy
  ├── _logging.py       `tradesmart` logger + redaction filter
  └── testing/          offline broker (`OfflineBroker`, `drills`, pytest plugin); imported only explicitly
```

Dependencies point downwards only: the facades depend on `endpoints/`, `_session.py` and a
transport; `endpoints/` depends on `models/`; the transports depend on `codec`, `retry`,
`ratelimit` and `endpoints.base`; nothing below the facades performs I/O except the two
transports, `instruments.py` and `stream/`.

## Module responsibilities

| Module | Responsibility |
|---|---|
| `client.py`, `async_client.py` | Build `ClientConfig`, `Credentials` and a transport; one flat method per endpoint (`profile`, `margins`, `place_order`, `orders`, `quote`, `historical_data`, `instruments`, …), each building a `Call` with an `endpoints` builder and handing it to the transport. Order methods return the order id. `from_env()`, `set_access_token()`, `request()`, context-manager close. |
| `_client_core.py` | `Credentials` (holds token and keys privately; `bearer()` is the token provider and enforces the local 06:00 IST expiry check), `env_kwargs()` (`TRADESMART_*`, stripped, a blank value counting as unset; else the saved login from `_token_store`), `canonical_endpoint()` (strips whitespace and `/`, letters only, documented spelling for known names) and `build_raw_call()`, the rate-group table for `request()` and the set of endpoints that are always state-changing. |
| `_session.py` | `login_url()`, the `GenAcsTok` call, and the mapping of every token-exchange failure (error answer, unknown outcome, unreadable success body) to `AuthenticationError`. |
| `_cli.py`, `__main__.py` | `python -m tradesmart` (also the `tradesmart` console script): `login` (prompts or `TRADESMART_*` / flags; opens the browser and serves the callback on `127.0.0.1:<port>/callback` by default (`--listen-host`, IPv4 only, for containers), one thread per connection so the first valid code wins (a later one gets HTTP 409), else a pasted code or URL; `--token-file` made absolute; one `generate_session()`; refuses a token issued to another account; saves via `_token_store`), `token-path`, `logout`. Plain-ASCII messages; never prints the token, secret key or code. Exit codes 0 / 1 / 2. |
| `_token_store.py` | Token-file location (`TRADESMART_TOKEN_FILE`, stripped, blank = unset, a relative path made absolute; else `%APPDATA%\tradesmart\token.json` / `$XDG_CONFIG_HOME/tradesmart/token.json`), atomic `0o600` save (timestamps `YYYY-MM-DDTHH:MM:SS+HH:MM`), load that accepts exactly `YYYY-MM-DDTHH:MM:SS`, optional 1-9 fraction digits, then `Z`, `z` or `±HH:MM` (the same on every Python version), refuses (on POSIX) a file owned by another user or open to group/others, refuses an `issued_at` more than 5 minutes in the future and caps the expiry at the 06:00 IST rule. Used by `env_kwargs()` so `from_env()` falls back to the saved login. |
| `_transport/outcome.py` | One INFO line per order call on `tradesmart.orders` (`PlaceOrder`, `PlaceMultiOrder`, `ModifyOrder`, `CancelOrder`, `ExitSNOOrder`, the GTT writes with `al_id=`, and the iceberg writes through `request()`): `accepted` / `rejected` / `unknown` / `not_sent` (never reached the server: no or expired token, client-side rate limit, closed client, connect-phase failure; `SendProgress` tracks whether the request was written), order id, elapsed time including any rate-limit wait; one WARNING per `OrderStatusUnknownError`. Invalid parameters are refused before the logged block and log no line. Never payloads, prices or tokens. |
| `greeks.py` | Stdlib-only European option maths (no I/O): `black76` (default, on the underlying future; no dividend yield) and `black_scholes` (spot plus continuous `dividend_yield`), `implied_volatility` (bracketed Newton/bisection solver), `greeks_from_price`; every number may be a `float`, `int` or `Decimal`. Conventions: years of 365 calendar days, annual continuously compounded `rate` (default 6.5 %), theta per calendar day, vega per volatility point, rho per 1 %. Expiry-day safety: time floored at one minute, clamped `d1`/`d2`, non-finite values become `None`, and every result carries a `status` (`ok`, `near_expiry` under 30 minutes, `expired`, `price_at_or_below_intrinsic`, `no_price`). Results are estimates. |
| `_option_contracts.py` | The clients' option helpers without any I/O: `find_option` (script-master row to `OptionContractInfo`; strike from `StrikePrice`, else BFO's `Strike`), `select_future` (the underlying future for the option's expiry, looked up by the option's `Symbol`, else, for a `Symbol` ending in `OPT`, by the stem plus `FUT` as on BFO; MCX options are written on the future itself), `ContractIndex` / `build_contract_index`, `EXPIRY_CLOSE` and `expiry_instant()` (NFO/BFO 15:30, CDS/BCD 12:30, MCX 23:30 IST; not yet confirmed by TradeSmart, see `specifications/sdk-behaviour.md` §15), `carry_adjusted_forward`, `plan_option_greeks` / `option_greeks_result` (what to quote, then how to turn the quotes into `OptionGreeks`; an expired option needs no quote of its own, and only after expiry does a contract-specific failure of the underlying future's quote degrade to `no_price` instead of raising: before expiry every quote failure raises), `chain_snapshot`, `buildup_reading` (since-open candle window). |
| `_contract_cache.py` | `ContractCache` (thread-safe; concurrent first calls parse once) and `AsyncContractCache`: one parsed `ContractIndex` per exchange, keyed by the script-master file's path, `st_mtime_ns` and size, so the daily refresh or `download_instruments(force=True)` re-parses once. `instruments()` does not use it. |
| `analytics.py` | Pure chain analytics: `ChainRow`, `OptionChainSnapshot` (`.pcr(by)`, `.max_pain()`), `pcr`, `max_pain` (`MaxPain`), `Buildup` / `classify_buildup(price_change, oi_change)` (long buildup, short buildup, long unwinding, short covering, neutral) and `buildup_from_snapshot`; Decimal in, Decimal out, non-finite values rejected. |
| `endpoints/base.py` | `EndpointSpec` (static endpoint description), `Call` (spec + wire payload), `parse_object` / `parse_list`. |
| `endpoints/{user,portfolio,market,orders,gtt}.py` | One spec per endpoint and a `*_call()` builder per spec; market-specific parsers (`values` lists, double-encoded `EODChartData`), `to_epoch` (aware `datetime`, `date` = 00:00 IST, or an `int` epoch; anything else is `RequestValidationError`), `eod_symbol`, `require_str` (ids, tokens and text must be `str`); order-acknowledgement parser (an accepted ack without an order id, or one that cannot be read, raises `OrderStatusUnknownError`). |
| `auth.py` | `build_authorize_url`, `compute_checksum`, `next_expiry`, `AccessToken`, the `GenAcsTok` spec (`authenticated=False`, `state_changing=True`). |
| `models/base.py` | `TradeSmartModel` (frozen, `extra="allow"`, `.raw`, `from_wire()` → `ResponseParseError`), `RequestModel` (`extra="forbid"`, `to_wire()`), `validate_request()` → `RequestValidationError`, `to_wire_value()`, lenient parsing for costly-to-lose records (`lenient_parsing`: a failing field becomes `None`, `.raw` keeps it, one WARNING names it; identity keys in `lenient_protected_keys`, `norenordno` and `status` (`al_id` on `Gtt` / `TriggeredGtt`), are never nulled), field types (`LenientStr` for every free-text / id field, prices, volumes, quantities, timestamps with a once-per-format WARNING for unknown shapes, enum-or-raw-string fields, the stream ATO price sentinel) and the `IST` timezone. |
| `models/{user,portfolio,market,orders,gtt,stream}.py` | Response models per endpoint / stream message; request models with validation rules (`PlaceOrderRequest`, `ModifyOrderRequest`, `OrderMarginRequest`, …). |
| `_transport/codec.py` | `encode_jdata()` (compact JSON, `%`→`%25`, `&`→`%26`, `+`→`%2B`) and `parse_response()` (JSON decode, `stat` handling, "no data", shape checks, error classification, redacted error bodies, `failure_message()`: "`<Endpoint>` failed: `<emsg>`" or "... failed (no error message from the server)"); no exception chains. |
| `_transport/sync_http.py`, `async_http.py` | `execute(call)`: rate-limit → headers (Bearer from the token provider) → build the request (body and headers encoded before it counts as sent: a value that cannot be encoded is `RequestValidationError` / `ConfigurationError`, an order logs `not_sent`) → POST → classify → parse; retry loop for reads; transport-error mapping (`OrderStatusUnknownError` for ambiguous state-changing failures, also for any non-httpx exception raised by a caller's hook or transport; on reads such an exception is never retried: an SDK error passes through, anything else becomes `NetworkError`; all mapped `from None`); redirects never followed (`follow_redirects=False` on the client and on every request); connection-pool limits for SDK-created `httpx` clients (`keepalive_expiry`, `max_connections`, `max_keepalive_connections`); state-changing calls wait at most `connect_timeout` for a pool slot; `User-Agent` always carries `tradesmart-python/<version>`; DEBUG logging (response inspected only when DEBUG is enabled). |
| `_transport/retry.py` | `RetryPolicy`: never for state-changing calls; only network/timeout/429/5xx; capped exponential backoff with jitter or a finite `Retry-After` clamped to `[0, cap]`. |
| `_transport/ratelimit.py` | Thread-safe sliding-window `RateLimiter`; `RateLimiterRegistry` with the documented groups (`place_order`, `quotes`, `tpseries`) and `wait` / `raise` modes; the `place_order` group has two windows (`order_rate_limit_per_second` / 1 s and `order_rate_limit_per_minute` / 60 s, default 200); the registry and the transports take an injectable clock (`use_time`, a test hook); in `wait` mode an order never waits longer than `order_rate_limit_max_wait` (else `RateLimitError`, nothing sent). |
| `instruments.py` | Script-master download (exchange validated first; zip → `.txt`, atomic write, file mtime set from `Last-Modified`, file cache fresh while written at or after the last daily refresh `script_master_refresh_time`, an older file re-checked with `If-Modified-Since` at most every `script_master_recheck_minutes` (last check in the `.checked` sidecar), default folder `~/.cache/tradesmart`) and header-driven parsing (`load`, `find`; a file that is not UTF-8 or not valid CSV, e.g. a NUL byte, raises `ResponseParseError` on every Python version); downloads never follow redirects. The clients download a file removed between download and parse once more (removed again: `ResponseParseError`). |
| `stream/protocol.py` | Sans-IO WebSocket protocol: request builders (`a`, `t`, `u`, `d`, `ud`, `o`, `uo`, `p`, `up`, `h`), instrument-key validation, `parse_message()` (never raises). |
| `stream/state.py` | `StreamState`, `SubscriptionRegistry` (restore after reconnect, 1,000-subscription guard), `SnapshotCache` (merges `tk`/`tf`/`dk`/`df` deltas). |
| `stream/delivery.py` | `DeliveryBuffer`: the bounded, merging hand-off between the reader and the consumer. `put` never blocks; it tracks each instrument's two newest waiting entries, and at or above the backlog limit a `tf`/`df` update is merged into the newest if that is a feed of the same type, or into the second newest if that is of the same type and the newest a feed of the other type (which then takes the new shared field values); nothing else is merged or dropped. |
| `stream/async_stream.py` | `AsyncTradeSmartStream`: connect + login, reader/supervisor task, dispatcher task (`on_message`, then `async for`), heartbeat task (`heartbeat_acks` counts `hk`), reconnect with backoff (token expiry checked before each attempt), `on_state_change` awaitables run as tasks, closing handshake bounded by `CLOSE_TIMEOUT` (2 s). |
| `stream/sync_stream.py` | `TradeSmartStream`: runs the async stream on a daemon thread with its own loop; runs every callback on a second daemon thread; thread-safe subscribe/stop (after `stop()` no further message reaches the callbacks; waiting ones are discarded and counted at DEBUG); an `async def` callback is a `TypeError` at construction. `on_reconnect()` (no arguments) runs right after `on_connect` on every reconnect, never on the first connect: updates sent during the outage are not replayed, so call `orders()` and `positions()` there. |
| `TradeSmartStream.from_env()` / `from_client()` (and the async twins) | `from_env()` uses the same precedence as the client's `from_env()` (env variables, then the saved login) but ignores `TRADESMART_APP_KEY`, because a stream cannot log in; without a usable token it raises `ConfigurationError` (`AuthenticationError` for an expired saved login). `from_client(client)` reuses the client's identity and `AccessToken`. |
| Closed client | Every call on a closed client (including one whose own `httpx` client the caller closed) raises `ConfigurationError("client is closed; create a new client")` from the transport (`closed_error()`), before anything is sent; a client is single-use after `close()`. |
| `errors.py`, `enums.py`, `config.py`, `_logging.py` | Exceptions, wire enums (members send wire codes, lookups also accept common words case-insensitively), configuration validation, logger factory + redaction. |

## Data flow of a REST call

Example: `client.place_order(exchange="NSE", tradingsymbol="M&M-EQ", quantity=1, transaction_type="BUY", ...)`.

1. **Facade** (`client.py`) calls `place_order_call(uid, ...)`.
2. **Endpoint builder** (`endpoints/orders.py`) validates the arguments through `PlaceOrderRequest`
   (`validate_request` → `RequestValidationError` on failure) and serialises them with
   `to_wire()`: every value becomes a string (enums become their wire codes, e.g. `"BUY"` →
   `"B"`), `None` fields are dropped, `amo` becomes `"Yes"`/`"No"`, `ordersource` is `"API"`, a
   market order without a price gets `"0"`. Result:
   `Call(PLACE_ORDER, {"uid": ..., "tsym": "M&M-EQ", ...})`.
3. **Transport** (`_transport/sync_http.py`):
   1. acquires a slot from the `place_order` rate limiter (`wait`, capped by
      `order_rate_limit_max_wait`, or `raise`);
   2. asks the token provider for the bearer token (raises `AuthenticationError` if none is set or
      the held `AccessToken` has expired);
   3. encodes the body with `encode_jdata` (`jData={"tsym":"M%26M-EQ",...}`) and POSTs it to
      `{base_url}/PlaceOrder` with `Authorization` and `User-Agent` headers, no `Content-Type`,
      reusing a pooled connection when one is open;
   4. maps `httpx` failures (any `httpx.RequestError`): connect errors → `NetworkError` /
      `RequestTimeoutError`; anything after the request may have been sent (incl. `DecodingError`) →
      `OrderStatusUnknownError` (state-changing specs only); any other exception raised while
      sending (a caller's event hook or transport) → `OrderStatusUnknownError` for orders,
      `NetworkError` (not retried) for reads, an SDK error from such a hook passing through on a
      read; redirects are never followed, so a 3xx goes to `parse_response` like any status;
   5. passes status and text to `parse_response`, which decodes JSON regardless of the HTTP status,
      returns the body on `stat: "Ok"` and raises the classified `APIError` subclass on
      `stat: "Not_Ok"` (`OrderRejectedError` for an order, also on HTTP 5xx); for state-changing specs any other body (non-JSON, empty, unexpected
      shape, list) raises `OrderStatusUnknownError`;
   6. for read endpoints, retries retryable errors according to `RetryPolicy`; state-changing specs
      never loop.
4. **Parser** (`EndpointSpec.parse`) turns the body into `OrderAck`; an acknowledgement that
   cannot be read or carries no order id raises `OrderStatusUnknownError`.
5. **Facade** returns `ack.order_id`.

The async client runs exactly the same steps with `AsyncTransport` (`await` on the limiter, sleep
and HTTP call); steps 1, 2, 3.5, 4 and 5 are the same functions.

## WebSocket architecture

```
AsyncTradeSmartStream
  connect(): open socket (ws_auth_timeout) → send {"t":"a",...} → wait for "ak" (ws_auth_timeout)
             → replay registry → CONNECTED → start tasks
  ├── reader/supervisor task:  recv() with 2×heartbeat timeout → parse_message
  │        → snapshot cache ← tk/tf/dk/df  → DeliveryBuffer.put(message)   (never waits for user code)
  │        on socket loss/silence: RECONNECTING → backoff → reopen + re-login + replay registry
  │        on ak Not_Ok / attempts exhausted: terminate → CLOSED, error raised by the iterator
  ├── dispatcher task: DeliveryBuffer.get() → on_message (awaited if async; errors logged)
  │        → iteration buffer (only once `async for` has started)
  └── heartbeat task: every heartbeat_interval send {"t":"h"} while CONNECTED

TradeSmartStream (sync)
  stream thread    ── asyncio.run(main) ── AsyncTradeSmartStream (iteration started before connect)
                                          └── for each message: queue a job, wait until it ran
  callback thread  ── runs jobs in order: on_message → on_touchline/on_depth/on_order_update/on_position;
                      on_connect / on_disconnect / on_error
  caller thread    ── subscribe_*() → run_coroutine_threadsafe (waits ≤ 10 s), also from a callback
```

- One stream owns one connection. Sends are serialised by a lock so a subscribe cannot interleave
  with the registry replay after a reconnect.
- The `SubscriptionRegistry` is the source of truth: subscribe/unsubscribe update it first and send
  only while `CONNECTED`; after every (re)login it is replayed once.
- Backlog: once `ClientConfig.stream_backlog_limit` or more messages wait for user code, the
  `DeliveryBuffer` merges a market-data update into one of the instrument's **two newest** waiting
  entries of the **same type** (`tf` into `tf`, `df` into `df`, never `tf` with `df`; latest value per
  field wins; the entry keeps its position): the newest, or the second newest when the newest is a
  feed of the other type (that newest then takes the new shared values, so each field is delivered
  in arrival order). Nothing merges across an ack. Acks, order and position updates are never
  merged, dropped or reordered. A WARNING marks the start and end of merging.
- `StreamState.CLOSED` is terminal; a stream is single-use.

## Safety design

| Risk | Mitigation | Where |
|---|---|---|
| Duplicate orders from retries | `state_changing=True` specs (`PlaceOrder`, `ModifyOrder`, `CancelOrder`, `ExitSNOOrder`, `GenAcsTok`, `PlaceGTTOrder`, `ModifyGTTOrder`, `CancelGTTOrder`) are never retried, and the GTT and iceberg writes succeed only on their exact documented `stat` word; `request()` normalises the endpoint name (whitespace and `/` stripped, letters only, case-insensitive match to the documented spelling) and treats those names, `PlaceMultiOrder` and the iceberg writes as state-changing whatever the caller passes, and `request(..., state_changing=True)` likewise. | `endpoints/orders.py`, `auth.py`, `_client_core.py`, `_transport/retry.py` |
| Unknown order outcome | Any transport failure after the request may have been sent (any `httpx.RequestError` except provably-unsent connect/pool-timeout failures) and any non-definitive response body (non-JSON at any status incl. 502/503/504 gateway pages, empty 200, unexpected shape, list) raise `OrderStatusUnknownError` (with `http_status`/redacted `body` when a response arrived) advising to check the order book. Only a JSON `stat` answer is definitive. An accepted (`stat: Ok`) acknowledgement that fails validation, or a PlaceOrder acknowledgement without an order id, also raises `OrderStatusUnknownError` (order ids are coerced to `str` first). A ModifyOrder `Ok` acknowledgement without an order id is not ambiguous: `modify_order()` returns `norenordno`, else the ack's `result`, else the caller's order id (the real success body is not yet confirmed; `specifications/sdk-behaviour.md` §15). | `_transport/sync_http.py::map_transport_error`, `_transport/codec.py::parse_response`, `endpoints/orders.py::_parse_ack` |
| Stale pooled connection | `keepalive_expiry` (50 s) stays below the server's idle timeout (measured live at 55-65 s) so an order is never sent on a connection the server has already closed (the connection-limit defaults are placeholders until TradeSmart confirms the limit). No background keep-alive requests. | `config.py`, `_transport/sync_http.py::pool_limits` |
| Invalid orders reaching the exchange | Request models validate price/trigger/leg rules, enums, quantities (booleans rejected), margin and strike prices, MTF-on-MCX before anything is sent. | `models/orders.py`, `models/market.py` |
| Mangled symbols (`M&M`) | `%`, `&`, `+` percent-encoded in the `jData` body. | `_transport/codec.py::encode_jdata` |
| Silent data corruption | List endpoints must return arrays; an `Ok` object raises `ResponseParseError` instead of being wrapped (`OrderStatusUnknownError` on a state-changing `request()`); a list item that is not an object raises `ResponseParseError`; malformed values raise instead of becoming wrong numbers; for orders, trades, positions, holdings, GTTs and order/position updates a malformed field becomes `None` (value kept in `.raw`, WARNING names the field) so one odd field never drops the record; prices and volumes are `Decimal`; the stream ATO sentinel price becomes `None`; unknown timestamp formats are logged. | `_transport/codec.py`, `models/base.py` |
| Secret leakage | `SecretStr` token fields; redacting filter on every `tradesmart` logger; payload logging off by default; `websockets` frame logs suppressed; secret-free exception messages; redacted `raw` / `body` on every error. | `_logging.py`, `auth.py`, `stream/async_stream.py`, `models/base.py`, `_transport/codec.py` |
| Expired-token calls | Local 06:00 IST expiry check before sending. | `_client_core.py::Credentials.bearer` |
| Leaked or foreign login | The login command never prints the token, secret key or code, silences the callback's access log, binds the callback to `127.0.0.1` by default (`--listen-host`, IPv4 only, for containers, with a warning), and refuses a token whose `uid`/`actid` is not the Client ID entered (TradeSmart's OAuth has no `state`). The token file is owner-only and refused when others can read it (POSIX). | `_cli.py`, `_token_store.py` |
| Order outcome not visible in logs | One `tradesmart.orders` INFO line per order call (`accepted`, `rejected`, `unknown`, `not_sent`) and a WARNING per unknown outcome, without prices or tokens. | `_transport/outcome.py` |
| Order re-sent to another host | Redirects are never followed, also on an injected `httpx` client that follows them; a 3xx on an order is `OrderStatusUnknownError`. | `_transport/sync_http.py`, `async_http.py`, `instruments.py` |
| Stale instrument list | A cached script-master file is fresh only if written at or after the most recent daily refresh (`script_master_refresh_time`, 07:00 IST, observed); no extra request. A file the server had not regenerated since then is re-checked with a conditional request (`If-Modified-Since`; `304` keeps it) at most every `script_master_recheck_minutes` (15), never re-downloaded on every call. If the re-check fails (TradeSmart outage) the cached file is used, one WARNING is logged per failed re-check and the server is asked again at most once per `script_master_recheck_minutes`. | `instruments.py::_is_fresh`, `_request_headers` |
| Server throttling | Client-side sliding-window limiter for the documented groups; 429 retried for reads honouring `Retry-After`. | `_transport/ratelimit.py`, `_transport/retry.py` |
| Slow user code stalling the stream | The reader never waits for callbacks; a bounded, merging buffer holds the backlog; sync callbacks run on their own thread. | `stream/async_stream.py`, `stream/delivery.py`, `stream/sync_stream.py` |
| Stream hammering with a bad token | WebSocket login rejection is terminal; reconnects are bounded with capped backoff and jitter. | `stream/async_stream.py` |
| Over-subscription | 1,000-subscription guard raises before sending. | `stream/state.py` |
| WebSocket ack spelling | The connect ack status is compared case-insensitively (the docs say `Ok`, the live server sends `OK`; fixture `ak_upper_ok`). | `stream/async_stream.py` |
| Live-test accidents | The live read-only suite is opt-in and kept read-only by a guard in the test suite; the live order suite needs a second opt-in and follows the gates in `specifications/sdk-behaviour.md` section 14 (see Testing architecture). Examples that change state default to dry runs. | `tests/live_orders/safety.py`, `examples/place_order.py` |

## Testing architecture

The test suite lives in the source repository (it is not shipped in the wheel) and runs in
Docker on Python 3.12 and on 3.11 (the floor), 3.13, 3.14 and 3.15 (several thousand tests, about 99 % line coverage; the
numbers are not pinned here):

- Unit tests (`tests/unit/`) — pure functions and models (codec, checksum, expiry boundaries,
  validation, redaction, rate limiter, delivery buffer, stream protocol, Greeks, analytics, token
  store, CLI), plus documentation/example checks (`test_examples_import.py`).
- Integration tests (`tests/integration/`) — full client flows with `respx`-mocked HTTP and a local
  WebSocket server on `127.0.0.1`.
- **Network guard** — an autouse fixture in `tests/conftest.py` blocks every socket connect to a
  non-loopback address (`RuntimeError: network access is blocked in tests`), pinned by
  `tests/test_network_guard.py`. The live suites lift it only for themselves. Another autouse
  fixture points the token file at an empty folder so a developer's saved login is never read.
- **Live read-only suite** (`tests/live/`, marker `live`) — real API, opt-in
  (`TRADESMART_LIVE_TESTS=1` plus credentials from `from_env()`); never places, modifies or cancels
  an order and never calls `request()`, which `tests/unit/test_live_suite_is_read_only.py` enforces
  for every module in the folder. Parametrised by segment (marks `nse`, `nfo`, `mcx`).
- **Live order suite** (`tests/live_orders/`, marker `live_orders`) — real order path, opt-in.
  Deselected by every plain `pytest` run (`-m "not live_orders"` in `addopts`); runs only with an
  explicit `-m live_orders` and a second opt-in (`TRADESMART_LIVE_TESTS=1` and
  `TRADESMART_LIVE_ORDER_TESTS=1`). Its gates (account checks, order caps, never resent, cancel in
  clean-up) are those of `specifications/sdk-behaviour.md` section 14; the rules live in `safety.py`
  and are pinned by `tests/unit/test_live_orders_safety.py`; see `tests/live_orders/README.md`.
- **Probes** — `scripts/live_probes.py` (not in the wheel): read-only measurements (`latency`,
  `keepalive`, `freshness`, `ratelimit`), gated by `TRADESMART_LIVE_TESTS=1`; results go to a
  git-ignored JSON-lines file.
- **Docker login volume** — `sdk-python/compose.yaml` mounts the named volume `tradesmart-login` at
  `/root/.config/tradesmart`, so `docker compose run --rm --service-ports login --client-id <ID>`
  (browser redirect to the published loopback port) or `docker compose run --rm dev python -m
  tradesmart login --no-browser` saves the daily login outside the repository; `... logout`
  removes it.
- Shared synthetic fixtures (`specifications/test-fixtures/`, no real ids or tokens) and the shared
  conformance vectors (`specifications/conformance/`), reusable by other-language SDKs.

See `sdk-python/CONTRIBUTING.md` for the commands and quality gates.

## Other languages

`specifications/sdk-behaviour.md` is the language-neutral behaviour contract: what any TradeSmart SDK
must do (request building, response classification, safety rules, Greeks, analytics, stream
behaviour). This document describes the Python realisation of that contract. A new SDK follows the
contract and the conformance vectors, not this file.
