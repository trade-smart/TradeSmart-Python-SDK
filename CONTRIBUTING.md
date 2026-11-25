# Contributing

Thanks for helping improve the TradeSmart Python SDK. The SDK lives in `sdk-python/`; its
contributing guide has the setup, test and style instructions:
[sdk-python/CONTRIBUTING.md](sdk-python/CONTRIBUTING.md).

The JavaScript SDK lives in its own repository (to be announced), with its own contributing guide.

Language-neutral material lives at the root: documentation in [`docs/`](docs/README.md) and the
behaviour contract, conformance vectors and test fixtures in [`specifications/`](specifications/).
SDKs for other languages use the same contract, so keep it free of Python-specific code.

## Pull requests

- Keep changes focused and explain the reason for them.
- Add or update tests, and update the docs and the relevant CHANGELOG.
- Make sure the CI checks pass.
- No Developer Certificate of Origin sign-off is required.
- Be kind: see the [code of conduct](CODE_OF_CONDUCT.md).

## Never commit credentials

Do not commit tokens, secret keys, app keys, checksums, auth codes, or `.env` files. Tests and fixtures
must use synthetic values only. Report security issues privately as described in
[SECURITY.md](SECURITY.md).
