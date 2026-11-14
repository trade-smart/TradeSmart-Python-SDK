# Contributing

Thanks for helping improve the TradeSmart SDKs. This repository holds one SDK per language, each
self-contained in its own `sdk-<language>/` folder with its own docs and its own contributing guide
with setup, test, and style instructions:

- Python: [sdk-python/CONTRIBUTING.md](sdk-python/CONTRIBUTING.md)

Material shared by every SDK lives at the root: language-neutral documentation in
[`docs/`](docs/README.md) and the behaviour contract, conformance vectors and test fixtures in
[`specifications/`](specifications/). A change there affects every SDK, so keep it free of
language-specific code.

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
