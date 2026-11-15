# Troubleshooting

Common problems with the `tradesmart` Python SDK and how to fix them. Every SDK error is a
subclass of `TradeSmartError`; catch that (not only `APIError`) to also handle network failures,
timeouts and unreadable responses. Code blocks are *illustrative*.

## Login and access

| Symptom | Cause and fix |
|---|---|
| `ConfigurationError: ... run python -m tradesmart login` from `TradeSmart.from_env()` | No usable saved login: never logged in, logged in for another client id (`TRADESMART_CLIENT_ID` differs), or the token file is unsafe (on Linux/macOS it must be readable only by you: `chmod 600`). The message says which. Run `python -m tradesmart login`. |
| `ConfigurationError: TRADESMART_ACCESS_TOKEN is set but TRADESMART_CLIENT_ID is not` | A token from the environment is never paired with the saved login's client id. Set both variables, or unset `TRADESMART_ACCESS_TOKEN` to use the saved login. |
| `ConfigurationError: client is closed; create a new client` | The client was closed (`close()`, `aclose()` or the end of its `with` block); nothing was sent. Create a new client. |
| `AuthenticationError: No usable access token: the saved login ... expired` | TradeSmart tokens end at the next 06:00 IST. Log in again. |
| `AuthenticationError: Access token expired at …` | A running client's token passed 06:00 IST; nothing was sent. Log in again and create a new client. If the machine's clock is wrong, fix it (or `ClientConfig(check_token_expiry=False)` lets the server decide). |
| `AuthenticationError: ... Session Expired : Invalid Session Key` | The server refused the token (expired or replaced by a newer login). Log in again. |
| HTTP **403** / `AuthenticationError` on every call although the token is fresh | Compare the machine's public IP with the **Primary** and **Secondary Static IP** of your app on the API Dashboard of the TradeSmart web platform (TradeSmart's documentation does not say which status an unregistered IP receives). An IPv6 machine: use an IPv4 static IP for now (TradeSmart serves IPv6 on a separate domain that is not yet published). The registered IP can be changed at most **2 times per day**; plan for cloud hosts, VPNs and dynamic IPs. |
| The login command waits and then asks for a code | No redirect reached `http://127.0.0.1:8080/callback`: no browser on this machine, port 8080 in use, or your app's Redirect URL is different. Paste the address the browser ended on (or its `code`). Use `--port N` if you registered another port; `--no-browser` on a server. In a Docker container the redirect reaches your machine, not the container, unless you publish the port: from a clone of the SDK repository, in `sdk-python`, run `docker compose run --rm --service-ports login --client-id YOUR_CLIENT_ID` and open the URL it prints in your browser (the warning about `0.0.0.0` is expected there); in your own image, `docker run --rm -it -p 127.0.0.1:8080:8080 YOUR_IMAGE python -m tradesmart login --listen-host 0.0.0.0` ([Authentication](authentication.md#no-browser-another-port-or-docker)). |
| `Login failed: GenAcsTok ... restart the login` | The one-time code was used, expired, or its outcome is unknown. Run `python -m tradesmart login` again (a code never works twice). |
| `This login belongs to account X, not Y - nothing was saved.` | The browser was logged in to another TradeSmart account. Log out of TradeSmart in the browser, then log in again with the right account. |
| Logged in with `--token-file PATH`, but `from_env()` does not find it | Set `TRADESMART_TOKEN_FILE` to that path in your scripts' environment; the login command prints the command for your shell, with the path quoted. `python -m tradesmart logout --token-file PATH` removes that file. |

## Orders

| Symptom | Cause and fix |
|---|---|
| `RequestValidationError` | The order broke a rule (e.g. `LIMIT` without `price`, `trigger_price` on a `LIMIT`); nothing was sent. `exc.errors` lists the problems. See [validation](orders.md#validation-before-sending). |
| `OrderRejectedError` | TradeSmart or the exchange refused the request; read `exc.emsg` (insufficient funds, price outside the band, market closed, `MKT` not allowed for API orders, …). Fix the request — retrying will not help. |
| `OrderRejectedError` "CTCLID not assigned" on an `MCX` order | MCX orders through the API are not yet enabled for TradeSmart (its exchange approval is pending), so MCX API orders are rejected this way (or acknowledged and then `REJECTED` with that `rejreason`). Nothing to fix on your side. |
| `OrderStatusUnknownError` | The request may or may not have reached the exchange. **Do not resend.** Find the order by its tag in `orders()` (see [Recovery](orders.md#recovery-tag-every-order-never-resend-blindly)). On the first order after a long idle time, see [warm start](configuration.md#low-latency-setup). |
| `RateLimitError` with `http_status=None` | The SDK's own limiter: an order would have waited more than 1 s, or `rate_limit_mode="raise"`. Nothing was sent; wait `exc.retry_after` seconds. |
| The order is `REJECTED` in `order_history()` although `place_order()` returned an id | `place_order()` only confirms that TradeSmart accepted the request; the exchange or risk checks can still reject it. Read `reject_reason`. |
| A symbol like `M&M-EQ` | Pass it unescaped; the SDK encodes `&`, `+` and `%`. Pre-encoding it breaks it. |

## Market data

| Symptom | Cause and fix |
|---|---|
| `historical_data()` / `daily_history()` misses today | A `date` means 00:00 IST, so `to_date=date.today()` ends at midnight. Pass `datetime.now(timezone.utc)`. |
| `RequestValidationError` about a naive datetime | Use timezone-aware datetimes (`datetime.now(timezone.utc)`). |
| Script-master columns | The layout is undocumented: inspect `rows[0].keys()` and filter with `tradesmart.instruments.find()`. |
| WARNING `using the cached script master; re-check failed: ...` | TradeSmart could not be reached while checking for a newer file; the cached file is used and checked again later. Nothing to do unless it persists. |
| `ResponseParseError: script master file ... is not valid UTF-8 text` | The cached instrument file is damaged. Delete the file named in the message and call `instruments()` (or `download_instruments()`) again. |
| `ValueError: unknown exchange ...` | Use `NSE`, `BSE`, `NFO`, `BFO`, `CDS`, `BCD` or `MCX` (any case). |

## Streaming

| Symptom | Cause and fix |
|---|---|
| `AuthenticationError: WebSocket authentication rejected (s=Not_Ok)` | Wrong client id or an expired token. Terminal: log in again and create a new stream. |
| `AuthenticationError: Access token expired at …` from `connect()` / `start()` | The stream's `AccessToken` (e.g. from `TradeSmartStream.from_env()`) passed 06:00 IST; nothing was sent. Log in again and create a new stream. |
| `NetworkError: reconnect attempts exhausted` | The connection kept failing. Check connectivity and the static IP; raise `reconnect_max_attempts` (or `None`). |
| WARNING `Stream consumer is falling behind ...` | Your callbacks are slower than the feed; price updates are being merged (order updates never are). Make callbacks faster. |
| `RequestTimeoutError: stream did not confirm the request within 10.0s ...` | The stream thread was busy; the subscription may still take effect and is restored on reconnect. |
| `SubscriptionLimitError` | More than 1,000 touchline + depth subscriptions on one stream. Unsubscribe some. |
| `TradeSmartError: stream is closed; create a new stream` | Streams are single-use. Create a new one. |

## Unexpected data

| Symptom | Cause and fix |
|---|---|
| `ResponseParseError: ... expected a JSON list but received an object` | A list endpoint answered with an object. `exc.body` holds the response (secrets masked). Please report it. |
| WARNING `Unrecognised timestamp format ...` | A timestamp in an unknown format; that attribute is `None` and the original stays in `.raw`. Please report the format shape (it contains no values). |

## Seeing what happens

*Illustrative:*

```python
import logging

logging.basicConfig(level=logging.DEBUG)  # one line per REST call, order outcomes, stream events
```

Secrets are masked in every SDK log line. See [Configuration](configuration.md#logging).

## Reporting a problem

Open an issue on the
[GitHub repository](https://github.com/trade-smart/TradeSmart-Python-SDK/issues) with the SDK version
(`python -c "import tradesmart; print(tradesmart.__version__)"`), your Python version, what you
did and the full error message. **Never include tokens, keys, codes or account details.**
Security problems: see [Versioning and support](versioning.md#reporting-bugs-and-security-issues).
Still stuck on an account or API-access problem: `support@tradesmartonline.in`. A community forum
for API users is coming soon.
