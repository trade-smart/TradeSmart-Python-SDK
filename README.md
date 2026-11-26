# TradeSmart Python SDK

The official Python SDK for the [TradeSmart API V2](https://apidocs.tradesmartonline.in/v2/), from
TradeSmart Fintech Securities Limited. Log in, read your account, find instruments, place and manage
orders, and stream live prices and order updates from your own Python code. Version **2.1.0**.

## Install

Python 3.11 to 3.15:

```bash
pip install tradesmart
```

You need a TradeSmart account and an API app (App Key and Secret Key), which you create yourself,
free, on the [TradeSmart web platform](https://web.tradesmartonline.in/api). Register your static
IP, and `http://127.0.0.1:8080/callback` as the Redirect URL.

No TradeSmart account yet? [Open one online][open-account].

[open-account]: https://signup.tradesmartonline.in/?utm_source=PythonSDK&utm_medium=Organic&utm_campaign=SDK_Open_Demat&utm_content=readme&channel=Organic&platform=web

## Quick start

Log in once a day (the token is valid until 06:00 IST). The command asks for your Client ID,
App Key and Secret Key:

```bash
python -m tradesmart login
```

Then call the API, with no keys in your code (*illustrative*):

```python
from tradesmart import TradeSmart

with TradeSmart.from_env() as client:  # uses the saved login
    print(client.profile().user_id)
    print(client.margins().cash)
```

## Next steps

- **[Your first order in 10 minutes](sdk-python/docs/getting-started.md)**: API access, login, and a
  first order placed, checked, modified and cancelled.
- [User guide](sdk-python/docs/README.md): orders, GTT and OCO orders, streaming, market data, option Greeks,
  offline testing, configuration.
- [Reference](sdk-python/docs/reference.md): every method, model, enum and error.
- [Troubleshooting](sdk-python/docs/troubleshooting.md) and [examples](sdk-python/examples/).

Trading involves real money. The SDK never retries an order on its own and tells you when an
order's outcome is unknown; read the [Orders guide](sdk-python/docs/orders.md) before running a strategy.

## Support

- Bugs and questions: [GitHub issues](https://github.com/trade-smart/TradeSmart-Python-SDK/issues).
  Account and API access: `support@tradesmartonline.in`. A community forum for API users is coming soon.
- Security issues: [security policy](SECURITY.md). Contributing: [CONTRIBUTING.md](CONTRIBUTING.md).
- Licence: Apache License 2.0 ([`LICENSE`](LICENSE), [`NOTICE`](NOTICE)), copyright TradeSmart Fintech
  Securities Limited.
