# Your first order in 10 minutes

This page takes you from nothing to a real order that you place, check, modify and cancel with
the `tradesmart` Python SDK. Follow the steps in order; every command and snippet can be copied
as it is. Code blocks are *illustrative*: they show real SDK calls, and the ones that change
something on your account say so.

> **Real money.** Steps 6 to 8 place a real order on your TradeSmart account. The guide uses a
> LIMIT BUY well **below** the market price so that it waits in the order book instead of
> filling, and then cancels it. Read each step before you run it.

Contents: [1. Get API access](#1-get-api-access) ·
[2. Install](#2-install-the-sdk) · [3. Log in](#3-log-in-once-a-day) ·
[4. First call](#4-your-first-call) · [5. Find an instrument](#5-find-an-instrument) ·
[6. Place an order](#6-place-your-first-order-safely) · [7. Check its status](#7-check-the-status) ·
[8. Modify and cancel](#8-modify-then-cancel) · [9. Watch updates live](#9-watch-order-updates-live) ·
[10. If something goes wrong](#10-if-something-goes-wrong)

## 1. Get API access

You need a TradeSmart trading account (no account yet?
[open one online](https://signup.tradesmartonline.in/?utm_source=PythonSDK&utm_medium=Organic&utm_campaign=SDK_Open_Demat&utm_content=getting_started&channel=Organic&platform=web))
and an **API application** that you create yourself on the TradeSmart web platform. For personal
use this is **self-service, free and immediate**: a one-time step in your browser, no approval.

- **Personal use**: tools for your own trading account; only you log in through the app. This
  is what this guide assumes.
- **Platform use**: a product that other TradeSmart clients log into. Platform apps still need
  review and approval from TradeSmart.

1. **Open the API Dashboard.** Log in to the TradeSmart web platform at
   <https://web.tradesmartonline.in> with your user ID, password and two-factor authentication
   (2FA, a TOTP code). Then click your user ID at the top right (the profile menu) and choose
   **Tools** then **API**. You can also open <https://web.tradesmartonline.in/api> directly. The
   page is called **API Dashboard**; it links to the "API Documentation" and to "Add/Setup TOTP".
2. **Fill in the "Generate New API Key" form:**

   | Field | Required | What to enter |
   |---|---|---|
   | **Name** | yes | The name of your app, for example `my-trading-scripts`. |
   | **Primary IP Address** | yes | The public static IP of the machine that will run your strategy (see point 4 below). |
   | **Secondary IP Address** | no | An additional static IP (optional). |
   | **Redirect URL** | yes | Where TradeSmart sends your browser after login (see point 3 below). |
   | **I acknowledge the API Terms & Conditions** | yes | Tick the box after reading the terms. |

   Then press **Generate API Key**.
3. **Choose the Redirect URL.** For the SDK's command-line login on your own computer, enter
   this address:

   ```
   http://127.0.0.1:8080/callback
   ```

   After you log in, TradeSmart sends your browser to this address with a one-time `code`; the
   SDK's login command listens there, on your own machine only. If you register a different
   URL, log in with `python -m tradesmart login --no-browser` and paste the code (step 3 of the
   guide, "Log in once a day"). (A web application that other people log into uses a page on
   its own server instead.)
4. **Use a static IP.** TradeSmart requires a registered static IP; requests from other IPs may
   be refused (see [step 10](#10-if-something-goes-wrong)). Enter the public address of the
   machine that will run your strategy in **Primary IP Address** (and a second one in
   **Secondary IP Address** if you need it). A server with a fixed IP is best; home connections
   often change their IP. Under the exchange rules the registered static IP can be changed
   **at most 2 times per day**.

   > **IPv4 for now.** An IPv6 static IP can be registered, but IPv6 clients must use a
   > different API domain, because TradeSmart could not serve IPv4 and IPv6 on one domain. That
   > domain is not yet published. Use an IPv4 static IP for now; the SDK will document how to
   > use the IPv6 domain when TradeSmart publishes it.
5. **Find your credentials.** After you generate the key, the dashboard lists each app under
   **Active Credentials**: App Name, Redirect URL, Primary Static IP, Secondary Static IP,
   **App ID** and **Secret Key**. The App ID and Secret Key are masked; use the **Show
   Credentials** toggle to reveal them and the copy buttons to copy them. The edit icon on each
   row lets you change the app's details.

   | Credential | What it is | Keep it |
   |---|---|---|
   | **Client ID** | Your TradeSmart trading account id | Not secret |
   | **App Key** | Identifies your app on the login page. **The dashboard calls it "App ID"**: the API documentation and this SDK call the same value the **App Key** (`app_key`, `TRADESMART_APP_KEY`, `--app-key`) | Private: do not publish it |
   | **Secret Key** | Proves the token request comes from you | **Secret** — never share it, never put it in code you publish or in a browser/mobile app |

**If your App Key or Secret Key leaks:** remove that app on the API Dashboard and create a new
one. The new app has a new App ID (App Key) and Secret Key. Update your environment variables
(or `.env` file), forget the saved keys (the login command saves them; see step 3) with
`python -m tradesmart logout --forget-keys` and run `python -m tradesmart login` again.

## 2. Install the SDK

You need **Python 3.11 or newer** (3.11 – 3.15). Use a virtual environment so the SDK
and its three dependencies (`httpx`, `pydantic`, `websockets`) stay separate from other
projects.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install tradesmart
```

The newest, not yet released code installs from GitHub (needs `git`):

```bash
pip install "tradesmart @ git+https://github.com/trade-smart/TradeSmart-Python-SDK.git#subdirectory=sdk-python"
```

Check it:

```bash
python -c "import tradesmart; print(tradesmart.__version__)"    # 2.0.0
```

## 3. Log in once a day

TradeSmart access tokens are valid until the **next 06:00 IST**, so you log in once per trading
day. Run this on the machine that will trade (the one with the registered static IP):

```bash
python -m tradesmart login
```

(`tradesmart login` does the same.) It asks for your Client ID, App Key (the dashboard's **App ID**) and Secret Key — the
keys are not shown while you type — opens the TradeSmart login page in your browser and waits
up to 120 seconds for the redirect to `http://127.0.0.1:8080/callback`. After you log in in the
browser you see *"Login complete. You can close this window and return to the terminal."* and
the terminal shows something like:

```
Logged in as YOUR NAME (YOUR_CLIENT_ID).
The token is valid until 2026-11-11 06:00 IST (TradeSmart tokens expire daily at 06:00 IST).
Saved to /home/you/.config/tradesmart/token.json; TradeSmart.from_env() uses it.
Remove the saved login with `python -m tradesmart logout`.
```

The token is saved in a file only your user can read (`%APPDATA%\tradesmart\token.json` on
Windows, `~/.config/tradesmart/token.json` on Linux and macOS). The command never prints the
token itself. The keys you typed are saved beside it (not encrypted) so later logins skip them:
[saved keys](authentication.md#daily-login-the-app-key-and-secret-key-are-saved).

Normally nothing is pasted: the login finishes by itself after you log in. No browser on this
machine? `python -m tradesmart login --no-browser` lets you paste the code instead. That, another
port and Docker are explained in
[Authentication](authentication.md#no-browser-another-port-or-docker).
`python -m tradesmart logout` deletes the saved login.

## 4. Your first call

Save this as `hello.py` and run `python hello.py`. `TradeSmart.from_env()` picks up the saved
login; nothing else is needed.

*Illustrative (read-only):*

```python
from tradesmart import TradeSmart

with TradeSmart.from_env() as client:
    me = client.profile()
    print("Logged in as", me.uname, me.user_id)
    print("Exchanges enabled:", me.exarr)

    funds = client.margins()
    print("Cash:", funds.cash, "Margin used:", funds.marginused)
```

`with` closes the network connections at the end. Prices and amounts are `Decimal` values.

## 5. Find an instrument

An order needs the exchange and TradeSmart's **trading symbol** (for NSE equities with the
series, e.g. `SBIN-EQ`); quotes need the instrument **token** (a string such as `"3045"`).
Search for them:

*Illustrative (read-only):*

```python
from decimal import Decimal

from tradesmart import TradeSmart, round_to_tick

with TradeSmart.from_env() as client:
    for match in client.search_instruments("SBIN"):
        print(match.exchange, match.tradingsymbol, match.token, match.tick_size)

    sbin = next(
        (
            m
            for m in client.search_instruments("SBIN")
            if m.exchange == "NSE" and m.tradingsymbol == "SBIN-EQ"
        ),
        None,
    )
    if sbin is None:
        raise SystemExit("SBIN-EQ not found on NSE.")
    quote = client.quote("NSE", sbin.token)
    print("Last price:", quote.last_price, "price band:", quote.lc, "-", quote.uc)

    info = client.security_info("NSE", sbin.token)
    print("Tick size:", info.tick_size, "lot size:", info.lot_size)

    # 5 % below the last price, on the tick grid (prices must be multiples of the tick size).
    if quote.last_price is not None and info.tick_size is not None:
        print(round_to_tick(quote.last_price * Decimal("0.95"), info.tick_size))
```

`round_to_tick(price, tick_size)` rounds to the nearest valid price and returns a `Decimal`.
`quote.lc` / `quote.uc` are the day's lower and upper price limits: an order outside them is
rejected, so stay inside the band. For whole exchange lists see
[Market data](market-data.md).

## 6. Place your first order, safely

Steps 6, 7 and 8 build **one script**. Create a file `first_order.py`, paste the code of
step 6 into it, then add the code of steps 7 and 8 **at the end of the same file**, and run it
**once, after step 8** (`python first_order.py`). Running it earlier, or more than once,
places one more order each time. If you did run it after step 6, cancel that order with the
[cancel snippet](#cancel-an-order-by-its-id) at the end of step 8.

Two habits make orders safe from the first day:

- **A LIMIT price below the market** (for a BUY) so the order rests in the order book. This
  guide uses 5 % below the last price, but never below the lower price limit `lc`.
- **A short, unique `tag`** on every order. If the network fails at the wrong moment you can
  find the order by its tag instead of guessing (step 10).

Run it during market hours (NSE equity: 09:15–15:30 IST): outside them TradeSmart rejects the
order. A `CNC` (delivery) BUY also needs enough funds in your account for the order value;
`client.order_margin(...)` shows what an order needs before you place it.

*Illustrative — step 6 of `first_order.py`; running the finished file places a real order:*

```python
import secrets
import sys
import time
from decimal import Decimal

from tradesmart import (
    OrderRejectedError,
    OrderStatus,
    TradeSmart,
    TradeSmartError,
    round_to_tick,
)

client = TradeSmart.from_env()  # closed at the end of step 8

sbin = next(
    (
        m
        for m in client.search_instruments("SBIN")
        if m.exchange == "NSE" and m.tradingsymbol == "SBIN-EQ"
    ),
    None,
)
if sbin is None:
    sys.exit("SBIN-EQ not found on NSE.")
quote = client.quote("NSE", sbin.token)
tick = client.security_info("NSE", sbin.token).tick_size
if quote.last_price is None or tick is None:
    sys.exit("No last price or tick size yet (market closed?); try again in market hours.")

target = quote.last_price * Decimal("0.95")
if quote.lc is not None:
    target = max(target, quote.lc)  # stay inside the day's price band
price = round_to_tick(target, tick)
if price + tick >= quote.last_price:
    sys.exit("The price would be too close to the market for a resting order; stopping.")

tag = "first-" + secrets.token_hex(3)  # e.g. first-a1b2c3
order_id = client.place_order(
    exchange="NSE",
    tradingsymbol="SBIN-EQ",
    quantity=1,
    transaction_type="BUY",
    product="CNC",  # delivery; "MIS" = intraday
    order_type="LIMIT",
    price=price,
    tag=tag,
)
print("Order id:", order_id, "tag:", tag, "price:", price)
```

`place_order()` returns the **order id** as soon as TradeSmart's order system has *accepted the
request*. That is an acknowledgement, **not** a fill: the exchange can still reject the order.
Always check the status (next step).

## 7. Check the status

Every order has **one row** in today's order book, `client.orders()`, with its current status.
Read the status from that row:

*Illustrative — step 7, add it at the end of `first_order.py` (read-only):*

```python
FINAL = (OrderStatus.COMPLETE, OrderStatus.CANCELED, OrderStatus.REJECTED)


def order_row(client, order_id):
    """This order's row in today's order book, or None if it is not visible (yet) or unreadable."""
    try:
        return next((o for o in client.orders() if o.order_id == order_id), None)
    except TradeSmartError:
        return None


row = order_row(client, order_id)
for _ in range(10):  # right after placing it can be missing or still PENDING
    if row is not None and row.status != OrderStatus.PENDING:
        break
    time.sleep(1)
    row = order_row(client, order_id)
if row is None:
    print("Status: status unknown - check client.orders()")
else:
    print("Status:", row.status, row.price, row.reject_reason or "")
```

An order moves through these states (`OrderStatus`):

| Status | Meaning | Final? |
|---|---|---|
| `PENDING` | Accepted by TradeSmart, waiting for the exchange | no |
| `OPEN` | Working at the exchange (your resting limit order) | no |
| `TRIGGER_PENDING` | A stop-loss order waiting for its trigger price | no |
| `COMPLETE` | Fully executed | **yes** |
| `CANCELED` | Cancelled | **yes** |
| `REJECTED` | Rejected by TradeSmart's risk checks or the exchange; `reject_reason` says why | **yes** |

Compare with the enum (`row.status == OrderStatus.OPEN`) or the text (`"OPEN"`).
`client.order_history(order_id)` lists every state change of one order. TradeSmart does not
document the order of its entries; in its example the newest entry is listed first, so prefer
the order-book row for the *current* status. More in [Orders](orders.md#order-status-lifecycle).

## 8. Modify, then cancel

*Illustrative — step 8, add it at the end of `first_order.py`; it changes and cancels your
real order:*

```python
if row is not None and row.status in FINAL:
    print("Nothing to modify or cancel: the order is", row.status)
else:
    try:
        if row is None:
            print("Not modifying: the status is unknown - check client.orders()")
        else:
            client.modify_order(
                order_id,
                exchange="NSE",
                tradingsymbol="SBIN-EQ",
                quantity=1,
                price=price + tick,  # one tick higher, still below the market
                order_type="LIMIT",
                product="CNC",
            )
            time.sleep(1)
            row = order_row(client, order_id)
            print("After modify:", row.price if row else "status unknown - check client.orders()")
    except OrderRejectedError as exc:
        print("Modify rejected:", exc.emsg)  # the order is unchanged; cancel it anyway
    finally:
        try:
            client.cancel_order(order_id)  # never re-places anything
            time.sleep(1)
            row = order_row(client, order_id)
            print("After cancel:", row.status if row else "status unknown - check client.orders()")
        except TradeSmartError as exc:
            print(f"Cancel failed: {exc} - check client.orders()")
client.close()
```

`modify_order()` needs the order's exchange, symbol, quantity, price, order type and product
again. Like placing, modifying and cancelling return once TradeSmart has *accepted* the request;
the order-book row confirms the result. The `finally` cancels the order even when the modify
fails. If step 7 could not read the status, nothing is modified (the order may not be open), the
cancel is still tried, and a failed cancel prints "check client.orders()" instead of a traceback.

### Cancel an order by its id

Stopped after step 6, or the script ended early? This cancels one order, if it is still open.
Put the order id that the script printed into `ORDER_ID`:

*Illustrative — cancels a real order:*

```python
from tradesmart import OrderStatus, TradeSmart

ORDER_ID = "your-order-id"  # the "Order id:" the script printed
FINAL = (OrderStatus.COMPLETE, OrderStatus.CANCELED, OrderStatus.REJECTED)

with TradeSmart.from_env() as client:
    row = next((o for o in client.orders() if o.order_id == ORDER_ID), None)
    if row is None:
        print("Not in today's order book - check the id.")
    elif row.status in FINAL:
        print("Nothing to cancel: the order is", row.status)
    else:
        client.cancel_order(ORDER_ID)
        print("Cancel requested; check client.orders() for CANCELED.")
```

## 9. Watch order updates live

Instead of asking again and again, let TradeSmart push every order update to you over the
WebSocket stream. Run this in a second terminal, then run [`first_order.py`](#6-place-your-first-order-safely)
and watch the updates arrive. Press Ctrl+C to stop.

`TradeSmartStream.from_env()` uses the same login as `TradeSmart.from_env()` (the one saved in
step 3):

*Illustrative (read-only):*

```python
from tradesmart import TradeSmartStream


def on_order_update(update):
    print(update.order_id, update.status, update.price, update.reject_reason or "")


with TradeSmartStream.from_env(on_order_update=on_order_update) as stream:
    stream.subscribe_orders()
    stream.run_forever()  # blocks; Ctrl+C stops
```

`with stream:` connects (and raises if the login is refused or has expired) and stops the
stream at the end. If the connection drops, the stream reconnects by itself; updates sent while
it was down are not replayed, so check `client.orders()` after a reconnect. A script that
already has a client can use `TradeSmartStream.from_client(client, ...)` instead. More in
[Streaming](streaming.md).

## 10. If something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `ConfigurationError: ... run python -m tradesmart login` | No saved login (or none for this client id) | Run `python -m tradesmart login` (step 3). |
| `AuthenticationError: No access token set: ...` | The saved login could not be used (it expired, or it belongs to another client id); the message says which | Run `python -m tradesmart login` again (step 3). |
| `AuthenticationError: ... expired ...` or `Session Expired` | Tokens end at 06:00 IST every day | Log in again. |
| HTTP **403** / `AuthenticationError` with a fresh token | Often: the request did not come from your **registered static IP** | Compare the machine's public IP with the **Primary** and **Secondary Static IP** shown for your app on the API Dashboard (changeable at most twice a day). An IPv6 machine: use IPv4 for now (see step 1). Still stuck: support@tradesmartonline.in. |
| `RequestValidationError` | The SDK found a mistake in your order before sending it (e.g. `LIMIT` without a price) | Read the message; nothing was sent. |
| `OrderRejectedError` | TradeSmart or the exchange refused the order; `exc.emsg` says why (margin, price band, market closed, `MKT` not allowed for API orders, ...) | Fix the order; don't just retry. |
| `OrderStatusUnknownError` | The request **may** have reached TradeSmart, but no clear answer came back (timeout, dropped connection) | **Never resend blindly.** Look for your `tag` in `client.orders()` (see below). Found: the order exists — follow it. Not found: wait a few seconds and look again before deciding. |
| `RateLimitError` | Too many orders too fast (10 per second by default, `order_rate_limit_per_second`; 200 per minute, `order_rate_limit_per_minute`) | Slow down. An order never waits more than 1 second for the SDK's rate limiter; after that it is not sent at all. |
| `NetworkError` / `RequestTimeoutError` | The connection could not be made; for orders this is raised only when the request was provably **not** sent | Check the internet connection, then try again. |

Finding an order by its tag after `OrderStatusUnknownError` (`client`, `price` and `tag` as in
step 6):

*Illustrative:*

```python
from tradesmart import OrderStatusUnknownError

try:
    order_id = client.place_order(
        exchange="NSE", tradingsymbol="SBIN-EQ", quantity=1, transaction_type="BUY",
        product="CNC", order_type="LIMIT", price=price, tag=tag,
    )
except OrderStatusUnknownError:
    found = [o for o in client.orders() if o.tag == tag]
    if found:
        order_id = found[0].order_id  # it exists: follow it, do not place it again
    else:
        raise SystemExit("Not in the order book yet: check again before placing anything.")
```

More answers: [Troubleshooting](troubleshooting.md).

## Next steps

- [`examples/order_lifecycle.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/order_lifecycle.py):
  a fuller `first_order.py` with a dry-run mode (`--live` checks your prices against a live
  quote before sending), which finds the order by its tag after an unknown outcome.
- [Orders](orders.md): every order type, validation rules, the status lifecycle, recovery.
- [Streaming](streaming.md): live prices, depth and order updates.
- [Market data](market-data.md): instruments, quotes, candles, option chains.
- [Option Greeks and analytics](option-analytics.md): IV and Greeks, chain snapshot, PCR, max pain, buildup.
- [Configuration](configuration.md): timeouts, rate limits, logging, low-latency set-up.
- The [SDK reference](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/reference.md)
  lists every method, model and error.
