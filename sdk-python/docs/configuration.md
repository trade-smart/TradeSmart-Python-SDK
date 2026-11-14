# Configuration

Every setting of the `tradesmart` Python SDK lives in one object, `ClientConfig`, shared by both
clients and both streams. The defaults suit most users; this page explains each setting, the
rate limits and retries, logging, and how to set up for low latency. Code blocks are
*illustrative*.

*Illustrative:*

```python
from tradesmart import ClientConfig, TradeSmart

config = ClientConfig(read_timeout=20, rate_limit_mode="raise")
client = TradeSmart.from_env(config=config)
```

`ClientConfig` is frozen (create a new one to change a value). An invalid value raises
`ConfigurationError` when you create it.

## All settings

| Field | Default | Meaning |
|---|---|---|
| `base_url` | `https://v2api.tradesmartonline.in/NorenWClientAPIv2` | REST base URL. |
| `ws_url` | `wss://v2api.tradesmartonline.in/NorenWSAPI/` | WebSocket URL. |
| `authorize_url` | `https://v2api.tradesmartonline.in/OAuthlogin/authorize/oauth` | Login page. |
| `script_master_base_url` | `https://v2api.tradesmartonline.in` | Host of the script-master files. |
| `connect_timeout` | `5.0` | Seconds to open a connection. Also the longest an order waits for a free pooled connection. |
| `read_timeout` | `10.0` | Seconds to wait for a response. |
| `max_retries` | `2` | Retries for **read** calls (`0` disables). Orders are never retried. |
| `backoff_base` | `0.5` | Base of the exponential retry delay, seconds. |
| `backoff_cap` | `8.0` | Longest retry delay, seconds (also caps `Retry-After`). |
| `rate_limit_mode` | `"wait"` | `"wait"` for a free slot or `"raise"` `RateLimitError` at once. |
| `order_rate_limit_max_wait` | `1.0` | Longest an order waits for the rate limiter in `"wait"` mode; longer → `RateLimitError`, nothing sent. |
| `order_rate_limit_per_second` | `10` | Orders per second the client sends (integer 1 to 30); a rate above 10 needs an exchange-approved strategy and algo ID, so it requires `algo_id` (else `ConfigurationError`). |
| `order_rate_limit_per_minute` | `200` | PlaceOrder calls per rolling 60 seconds (integer 1 to 600, at least `order_rate_limit_per_second`). TradeSmart documents 600, but production refused an order once 200 had been received in the current minute, so the default is 200. A call that would wait longer than `order_rate_limit_max_wait` for this window raises `RateLimitError` and sends nothing. |
| `algo_id` | `None` | Exchange-approved algo ID, sent as `algo_id` on every `PlaceOrder` and `ModifyOrder` (every `place_sliced_order` slice too); a per-call `algo_id=` overrides it. A non-blank string or `None`. |
| `user_agent` | `"tradesmart-python/2.0.0"` | `User-Agent` header; `tradesmart-python/<version>` is always included. Printable ASCII only: control characters and non-ASCII text are rejected. |
| `log_payloads` | `False` | Log (redacted) request payloads at DEBUG. |
| `check_token_expiry` | `True` | Refuse calls locally once a held `AccessToken` passed 06:00 IST. `False` lets the server decide. |
| `heartbeat_interval` | `30.0` | Seconds between stream heartbeats; twice this without any frame means a dead connection. |
| `ws_auth_timeout` | `10.0` | Seconds for the WebSocket to open, and again for the login acknowledgement. |
| `reconnect_max_attempts` | `20` | Stream reconnect attempts before giving up (`None` = unlimited). |
| `reconnect_backoff_base` | `1.0` | Base of the reconnect delay, seconds. |
| `reconnect_backoff_cap` | `60.0` | Longest reconnect delay, seconds. |
| `keepalive_expiry` | `50.0` | Seconds an idle HTTP connection is kept for reuse; below the server's idle timeout measured on the live server (about 60 s). |
| `max_connections` | `10` | Simultaneous HTTP connections per client. **Pending confirmation by TradeSmart** (connection limit). |
| `max_keepalive_connections` | `5` | Idle connections kept for reuse (`<= max_connections`). **Pending confirmation by TradeSmart**. |
| `stream_backlog_limit` | `1000` | Waiting stream messages at which price/depth updates start to be merged. |
| `script_master_refresh_time` | `"07:00"` | IST time of TradeSmart's daily script-master refresh. **Observed, pending confirmation by TradeSmart.** |
| `script_master_recheck_minutes` | `15` | Minutes between re-checks of a cached script master the server has not regenerated yet (also during an outage). |
| `instruments_dir` | `None` | Folder for the cached script master when no `dest_dir` is given (`None`: `~/.cache/tradesmart`); `~` is expanded. |

All timeouts, backoff values, the heartbeat interval and `keepalive_expiry` must be finite
numbers greater than 0, and `order_rate_limit_max_wait` a finite number `>= 0` (NaN and
infinity raise `ConfigurationError`). `max_retries`, `reconnect_max_attempts` (or `None`),
`max_connections`, `max_keepalive_connections`, `stream_backlog_limit` and
`script_master_recheck_minutes` must be whole numbers (`int`; a float or a bool is rejected).
The URLs, `user_agent` and `script_master_refresh_time` must be text, and `log_payloads` and
`check_token_expiry` `True` or `False`. A wrong type raises `ConfigurationError` too.

**Values pending confirmation by TradeSmart.** `max_connections` and
`max_keepalive_connections` are safe provisional defaults until TradeSmart confirms its
connection limit. `keepalive_expiry` (50 s) stays below the server's idle timeout measured on the
live server (about 60 s); `script_master_refresh_time` is the refresh time observed on the
files. Do not raise `keepalive_expiry` on your own: if the server closes an idle connection
before the SDK does, an order sent on it can end as `OrderStatusUnknownError`.

**Your own HTTP client.** For proxies or custom TLS pass `http_client=httpx.Client(...)` (an
`httpx.AsyncClient` for `AsyncTradeSmart`). The SDK uses it as it is: the three connection
settings above are not applied, it is not closed by `client.close()`, and the SDK still sets its
own timeouts per request. The SDK never follows HTTP redirects, also on a client created with
`follow_redirects=True`: a redirect on an order is an `OrderStatusUnknownError` (nothing is
re-sent to the other address), and on a read or a script-master download it is handled like any
other unexpected answer (usually `ResponseParseError`).

## Rate limits and retries

The SDK enforces these limits per client instance:

| Calls | Per second | Per minute |
|---|---|---|
| `place_order` (and `PlaceMultiOrder` via `request()`) | `order_rate_limit_per_second` (10; up to 30 with `algo_id`) | `order_rate_limit_per_minute` (200; up to 600) |
| `quote` (and `GetQuotesMultiple` via `request()`) | 100 | 1,000 |
| `historical_data` | 100 | 1,000 |
| everything else | no limit | no limit |

- `"wait"` mode (default): a call waits for a free slot. An order waits at most
  `order_rate_limit_max_wait` (1 s); if it would need longer it raises `RateLimitError` (with
  `retry_after`) and is not sent. Reads keep waiting.
- `"raise"` mode: the call raises `RateLimitError` at once (`http_status=None`, `retry_after` =
  seconds until a slot frees).
- Two clients do not share a limiter: use one client per account and process.

**Retries.** Only read calls are retried, only after `NetworkError`, `RequestTimeoutError`, HTTP
429 and server errors, up to `max_retries` times with a growing, randomised delay (or the
server's `Retry-After`, when it is a finite number). Order calls and the login token exchange
are **never** retried. An exception other than an `httpx` transport error raised by your own
`httpx` event hook or transport on a read is not retried either: an SDK error passes through
unchanged, anything else becomes `NetworkError`. (An `httpx` transport error raised there, such
as `ConnectError`, is handled and retried like any other network failure.)

## Logging

The SDK logs through Python's `logging` module under the `tradesmart` logger. It never prints
and never configures logging for you.

*Illustrative:*

```python
import logging

logging.basicConfig(level=logging.INFO)  # order outcome lines, warnings, errors
logging.getLogger("tradesmart").setLevel(logging.DEBUG)  # plus one line per REST call
```

| Level | Logger | What |
|---|---|---|
| DEBUG | `tradesmart.transport` | One line per REST call (`POST /GetQuotes -> HTTP 200 stat=Ok in 85 ms`); request payloads only with `log_payloads=True`, redacted. |
| INFO | `tradesmart.orders` | One line per `PlaceOrder`, `PlaceMultiOrder`, `ModifyOrder`, `CancelOrder`, `ExitSNOOrder`, GTT write (`PlaceGTTOrder`, `ModifyGTTOrder`, `CancelGTTOrder`: the alert id as `al_id=`) and iceberg write (through `request()`) call: `PlaceOrder accepted order_id=26110700000001 in 42 ms` (`accepted`; `rejected` when the server answered with an error; `unknown` for `OrderStatusUnknownError`; `not_sent` when the request never reached the server: no or an expired token, the client-side rate limit, a closed client or a connect-phase failure; `-` when the id is not known). The time includes any client-side rate-limit wait. Invalid parameters (`RequestValidationError`) log no line. See [Orders](orders.md#order-logging). |
| WARNING | `tradesmart.orders` | Every `OrderStatusUnknownError` ("check orders() before acting"; for a GTT write "check pending_gtts() before acting"); an async order task cancelled after the request may have been sent. |
| WARNING | `tradesmart.stream`, others | Connection lost / reconnect attempts; slow consumer (merging started / caught up); a cached script master used after a failed re-check; an unknown timestamp format. |
| ERROR | `tradesmart.stream` | Stream stopped by a terminal error; exceptions raised by your callbacks. |

**Secrets are masked.** Tokens, the secret key, app key, checksum and login code are replaced by
`***` in every `tradesmart` log line and in the bodies stored on exceptions, and never appear in
`repr()`. Masking is by key name: values you log yourself under other names are not masked.

## Low-latency setup

Measured with the network mocked, the SDK adds about 0.1–0.2 ms to a REST call and tens of
microseconds to a stream message. The network dominates, so most of the latency you can save is
in where and how you run:

1. **Host close to TradeSmart.** Run on a server with a short, stable path to
   `v2api.tradesmartonline.in` — typically in India, not over a home connection or an overseas
   cloud region — with a fixed public IP that is your registered static IP.
2. **Warm start.** Create one client per process and keep it all day. Idle connections close
   after `keepalive_expiry` and the SDK sends no background pings, so make a cheap read
   (`client.profile()`) shortly before you expect to trade: the first order then does not pay for
   the connection set-up.
3. **Connections.** With the default cap of 10, more than 10 concurrent requests from one client
   queue for a free connection (orders at most `connect_timeout`, then `RequestTimeoutError` with
   nothing sent).
4. **Fast stream callbacks.** Do the minimum in a callback (update state, put work on a queue).
   With `AsyncTradeSmartStream` prefer `async for` or an `async` `on_message`.
5. **Backlog merging.** If your code falls 1,000 messages behind (`stream_backlog_limit`), price
   and depth updates for the same instrument are merged so you always see the latest values;
   order and position updates are never merged or dropped. `stream.snapshot(key)` always holds
   the latest values.
