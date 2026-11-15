# Versioning and support

## Version numbers

The `tradesmart` Python SDK follows [Semantic Versioning](https://semver.org/):
`MAJOR.MINOR.PATCH`.

- **PATCH** (2.0.**1**): bug fixes only; no change to how you call the SDK.
- **MINOR** (2.**1**.0): new features that keep existing code working.
- **MAJOR** (**3**.0.0): changes that can require you to change your code. Each one is listed in
  the [changelog](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/CHANGELOG.md).

**2.0.0 is the first public version.** Earlier numbers (0.1, 0.2) were internal previews that
were never published, so there is nothing to migrate from.

Check your installed version:

```bash
python -c "import tradesmart; print(tradesmart.__version__)"
```

To stay on a major version, pin it, e.g. `tradesmart>=2,<3` once the SDK is on PyPI, or a
release tag of the git repository (`git+https://github.com/trade-smart/TradeSmart-Python-SDK.git@<tag>#subdirectory=sdk-python`).

## Supported Python versions

Python **3.11, 3.12, 3.13, 3.14 and 3.15**. Support for a Python version is dropped only in a
minor or major release, and announced in the changelog first.

Runtime dependencies, installed by pip: `httpx>=0.27,<1`, `pydantic>=2.6,<3` (at least 2.8 on Python 3.13,
2.12 on 3.14 and 2.14 on 3.15) and `websockets>=13,<18`; nothing else.

## What counts as the public API

Everything documented in the
[SDK reference](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/docs/reference.md)
and in this guide: the names exported by `tradesmart` (`TradeSmart`, `AsyncTradeSmart`, the two
streams, `ClientConfig`, the enums, errors and models), the `tradesmart.instruments`,
`tradesmart.models.*` and `tradesmart.endpoints.*` `*_call()` helpers, and the
`python -m tradesmart` commands. Names starting with an underscore (`tradesmart._cli`, …) are
internal and can change in any release.

Response models keep fields TradeSmart adds later (`extra` attributes and `.raw`), so new server
fields never break your code.

## Deprecation policy

1. A feature to be removed is first **deprecated in a minor release**: it keeps working, emits a
   `DeprecationWarning` that names the replacement, and the changelog says so.
2. It is **removed in the next major release** at the earliest.

Run your tests with warnings visible (`python -W default::DeprecationWarning ...`) to see
deprecations early.

Exception: when TradeSmart changes or withdraws an API endpoint, the SDK follows TradeSmart's
schedule; such changes are flagged prominently in the changelog.

## Reporting bugs and security issues

- **Bugs and questions:** open an issue on
  [GitHub](https://github.com/trade-smart/TradeSmart-Python-SDK/issues) with the SDK version, your
  Python version, the steps, and the full error message. Never include tokens, keys, login
  codes or account details.
- **Security vulnerabilities:** do **not** open a public issue. Follow the repository's
  [security policy](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/SECURITY.md).
- **Account or API access** (credentials, static IP, API permissions, order rejections by the
  exchange): email `support@tradesmartonline.in` (or contact your TradeSmart relationship manager). A community forum for API users is coming soon.
