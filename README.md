# TradeSmart Python SDK

Official open-source SDKs for the [TradeSmart API V2](https://apidocs.tradesmartonline.in/v2/)
(REST + WebSocket) from TradeSmart Fintech Securities Limited. Log in, read your account, find
instruments, place and manage orders, and stream live prices and order updates from your own
code.

| SDK | Folder | Status |
|---|---|---|
| Python (`tradesmart`) | [`sdk-python/`](sdk-python/) | **2.0.0 available** — [user guide](sdk-python/docs/README.md) · [reference](sdk-python/README.md) · [changelog](sdk-python/CHANGELOG.md) |
| JavaScript/TypeScript, .NET, Java, Go, Rust, PHP | — | Planned |

Each SDK is self-contained in its own folder: code, tests, examples, user guide and changelog.
Documentation shared by all SDKs is in [`docs/`](docs/README.md).

## Python: your first order in 10 minutes

```bash
pip install "tradesmart @ git+https://github.com/trade-smart/tradesmart-sdks.git#subdirectory=sdk-python"
python -m tradesmart login
```

```python
from tradesmart import TradeSmart

with TradeSmart.from_env() as client:
    print(client.profile().uname, client.margins().cash)
```

You need a TradeSmart account and an API key, which you generate yourself (free, immediate) on the
TradeSmart web platform at <https://web.tradesmartonline.in/api>: App Key, Secret Key, a Redirect URL
and a static IP. (The dashboard calls the App Key "App ID".) For the login command register
`http://127.0.0.1:8080/callback` as the Redirect URL. (With any other registered URL, use
`python -m tradesmart login --no-browser` and paste the code.) TradeSmart requires a registered
static IP; requests from other IPs may be refused
([troubleshooting](sdk-python/docs/troubleshooting.md)). The step-by-step guide
(step 1 covers API access) takes you through a first order placed,
checked, modified and cancelled:
**[Your first order in 10 minutes](sdk-python/docs/getting-started.md)**.

## Documentation

Shared by every SDK ([index](docs/README.md)):

- [API access and login](docs/api-access.md): getting API access, the login flow, token lifetime
  and security rules.

Python, all in [`sdk-python/`](sdk-python/):

- [Python user guide](sdk-python/docs/README.md): getting started, authentication, orders,
  streaming, market data, configuration, troubleshooting, versioning and support.
- [Python SDK reference](sdk-python/README.md): every method, model, enum and error.
- [Moving from another broker's SDK](sdk-python/docs/migration/README.md).
- [Python SDK architecture](sdk-python/docs/architecture.md), for contributors.

## Project

- [Security policy](SECURITY.md): how to report a vulnerability privately.
- [Contributing](CONTRIBUTING.md): how to contribute; each SDK's guide covers its development
  set-up, tests and pull requests.
- Bugs and questions: [GitHub issues](https://github.com/trade-smart/tradesmart-sdks/issues).
  Account and API-access questions: `support@tradesmartonline.in` (or your TradeSmart relationship manager).
  A community forum for API users is coming soon.
- Licence: Apache License 2.0 ([`LICENSE`](LICENSE), [`NOTICE`](NOTICE)); copyright TradeSmart Fintech Securities Limited.

Trading involves real money. The SDK never retries an order and reports an unknown outcome
instead of guessing; read the [Orders guide](sdk-python/docs/orders.md) before running a strategy.
