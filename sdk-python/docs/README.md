# `tradesmart` Python SDK — user guide

`tradesmart` is TradeSmart's official Python SDK for the
[TradeSmart API V2](https://apidocs.tradesmartonline.in/v2/): log in, read your account, find
instruments, place and manage orders, and stream live prices and order updates — from
ordinary Python scripts or `asyncio` programs.

This guide is for traders and developers who want to automate trading on a TradeSmart account
with Python. You do not need to read the SDK's source code: everything you need is here and in
the [SDK reference](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/reference.md).

## The 10-minute path

1. **Get API access** yourself, free and immediately, on the TradeSmart web platform
   (<https://web.tradesmartonline.in/api>): generate an API key with your static IP and Redirect URL
   `http://127.0.0.1:8080/callback`. The dashboard shows an **App ID**:
   that is the **App Key** of this SDK. Details: [step 1 of the guide](getting-started.md#1-get-api-access).
2. **Install:**
   `pip install tradesmart`
3. **Log in** (once a day; the token is valid until 06:00 IST): `python -m tradesmart login`
4. **Call the API** with `TradeSmart.from_env()` — no keys in your code.
5. **Place, check, modify and cancel** your first order.

Step by step, with copy-paste code: **[Your first order in 10 minutes](getting-started.md)**.

## Pages

| Page | What is in it |
|---|---|
| [Getting started](getting-started.md) | Credentials, install, login, first call, first order (place → status → modify → cancel), live order updates, common errors, one complete script |
| [Authentication](authentication.md) | The login command, the token file, `from_env()`, your own login flow in a server, token lifetime in the SDK, keeping credentials safe |
| [Orders](orders.md) | Every order call and parameter, validation rules, the order status lifecycle, tag-based recovery, never-resend rules, rate limits, AMO, cover/bracket orders |
| [GTT orders](gtt.md) | Good-till-triggered orders: alert types, the "fires at once" rule, validity (365 days or the contract expiry), modify with both validities, reading pending and triggered GTTs, recovery by tag |
| [OCO orders](oco.md) | One-cancels-the-other exits: a target and a stop-loss in one order, the side rule, modify and cancel, reading OCO rows back (`is_oco`, `stop_loss`, `target`), offline testing |
| [Account and portfolio](account.md) | Profile, funds and margins, positions (one flat `Position` per instrument and product), holdings and today's trades |
| [Streaming](streaming.md) | `TradeSmartStream` (callbacks) and `AsyncTradeSmartStream` (`async for`), reconnects, catching up after a reconnect, message types |
| [Market data](market-data.md) | Instrument search, the script master and when it is refreshed, quotes, candles, option chains |
| [Option Greeks and analytics](option-analytics.md) | `option_greeks()` (IV and Greeks, rate and dividend defaults and how to override them, expiry-day statuses), `option_chain_snapshot()`, put-call ratio, max pain, `buildup()` |
| [Testing your strategy offline](testing.md) | `OfflineBroker`: test your code with the real client and no account or network; fills, rejections, lost replies and dropped streams on demand |
| [Configuration](configuration.md) | Every `ClientConfig` setting, rate limits and retries, logging, low-latency set-up |
| [Troubleshooting](troubleshooting.md) | Error messages and what to do about them |
| [Versioning and support](versioning.md) | Version numbers, supported Python versions, deprecation policy, reporting bugs and security issues |
| [Reference](reference.md) | Every method, model, enum, error and setting in one page, the login command in detail, and the known limitations |

## More

- [Examples](https://github.com/trade-smart/TradeSmart-Python-SDK/tree/main/sdk-python/examples):
  runnable scripts (login, first calls, a dry-run order lifecycle, market data, streams, GTT,
  OCO, sliced orders, offline testing).
- [Authentication](authentication.md), and [API access and login](../../docs/api-access.md):
  the login flow, token lifetime and security rules.
- [Moving from another broker's SDK](migration/README.md).
- [Changelog](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/CHANGELOG.md).

> Trading involves real money. The SDK never retries an order and tells you when an order's
> outcome is unknown; read [Orders](orders.md#recovery-tag-every-order-never-resend-blindly)
> before running a strategy.
