# tradesmart — Python SDK for the TradeSmart API V2

TradeSmart's official Python client for the
[TradeSmart API V2](https://apidocs.tradesmartonline.in/v2/). Log in once a day, read your account,
find instruments, place and manage orders, and stream live prices and order updates, from plain
Python scripts or `asyncio` programs.

> **Version 2.1.0.** Trading involves real money: read the
> [Orders guide](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/orders.md)
> before sending anything that changes state.

## What you get

- **Two clients with the same methods:** `TradeSmart` (blocking) and `AsyncTradeSmart` (`asyncio`).
- **One-command daily login:** `python -m tradesmart login`; your scripts pick the token up with
  `TradeSmart.from_env()`, so no keys live in your code.
- **Orders:** place, modify, cancel and exit; large orders sent in slices; GTT (good-till-triggered)
  orders; OCO (one-cancels-other) exits. Orders are never retried automatically, and an unclear outcome is reported, not guessed.
- **Market data:** instrument search, the script master, quotes, candles, option chains, option Greeks.
- **Live streaming:** prices and order updates over a WebSocket that reconnects and resubscribes by itself.
- **Typed results:** Pydantic models, with prices as `Decimal`.
- **Offline testing:** `OfflineBroker` runs your strategy against a local fake broker, with no account
  or network.

## New to TradeSmart? Start trading from code in three steps

1. **[Open a TradeSmart account online][open-account].** TradeSmart Fintech Securities Limited is a
   SEBI-registered stockbroker (NSE, BSE, MCX).
2. **Create your API app** on the TradeSmart web platform's API Dashboard: self-service, with no API
   fee and no approval to wait for, for your own trading.
3. **`pip install tradesmart`**, log in with one command, and place your first order. The
   [getting-started guide](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/getting-started.md)
   takes about 10 minutes.

**[Open your TradeSmart account →][open-account]** Already a client? Go straight to
[Get API access](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/getting-started.md#1-get-api-access).

[open-account]: https://signup.tradesmartonline.in/?utm_source=PythonSDK&utm_medium=Organic&utm_campaign=SDK_Open_Demat&utm_content=pypi&channel=Organic&platform=web

## Install

Python 3.11 to 3.15:

```bash
pip install tradesmart
```

You also need a TradeSmart API app (free, self-service on the TradeSmart web platform's API
Dashboard): its **App Key** (shown as "App ID"), **Secret Key**, your **Client ID**, the Redirect URL
`http://127.0.0.1:8080/callback` and your static IP. Step by step:
[Get API access](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/getting-started.md#1-get-api-access).

## Quick start

Log in once a day (the token is valid until 06:00 IST):

```bash
python -m tradesmart login
```

Then, in your script (*illustrative*):

```python
from tradesmart import TradeSmart

with TradeSmart.from_env() as client:  # uses the saved login
    print(client.profile().user_id)
    print(client.margins().cash)
```

**New here?** Follow
[Your first order in 10 minutes](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/getting-started.md):
API access, install, login, and a first order placed, checked, modified and cancelled.

## User guide

| Page | What is in it |
|---|---|
| [Getting started](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/getting-started.md) | API access, install, login, first call, first order |
| [Authentication](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/authentication.md) | The login command, the token file, `from_env()`, login in your own server |
| [Orders](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/orders.md) | Every order call, validation, order status, finding an order after an unclear outcome |
| [GTT orders](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/gtt.md) | Good-till-triggered orders |
| [OCO orders](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/oco.md) | A target and a stop-loss in one order |
| [Account and portfolio](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/account.md) | Profile, funds, positions, holdings, trades |
| [Market data](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/market-data.md) | Search, script master, quotes, candles, option chains |
| [Option Greeks and analytics](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/option-analytics.md) | Implied volatility, Greeks, chain snapshots, put-call ratio, max pain |
| [Streaming](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/streaming.md) | Live prices and order updates |
| [Testing your strategy offline](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/testing.md) | `OfflineBroker`: fills, rejections, lost replies, dropped streams |
| [Configuration](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/configuration.md) | Settings, rate limits, retries, logging |
| [Troubleshooting](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/troubleshooting.md) | Error messages and what to do |
| [Reference](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/reference.md) | Every method, model, enum, error and setting; known limitations |

Runnable scripts are in
[`examples/`](https://github.com/trade-smart/TradeSmart-Python-SDK/tree/main/sdk-python/examples),
for example a strategy tested offline:
[`offline_strategy_test.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/offline_strategy_test.py).

## Switching from another broker

Guides for moving existing code:
[migration guides](https://github.com/trade-smart/TradeSmart-Python-SDK/tree/main/sdk-python/docs/migration).

## Support

- Bugs and questions: [GitHub issues](https://github.com/trade-smart/TradeSmart-Python-SDK/issues).
- Security issues: the [security policy](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/SECURITY.md).
- Account and API access: `support@tradesmartonline.in`.
- Versions and deprecations: [Versioning and support](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/versioning.md);
  changes: [changelog](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/CHANGELOG.md).
- Contributing: [CONTRIBUTING.md](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/CONTRIBUTING.md).

## Risk notice

Investments in the securities market are subject to market risks; read all the related documents
carefully before investing. Automated and API trading can place orders quickly: test your strategy
offline first and start with small quantities.

## Licence

Apache License 2.0, see [LICENSE](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/LICENSE)
and [NOTICE](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/NOTICE).
Copyright (c) 2026 TradeSmart Fintech Securities Limited.
