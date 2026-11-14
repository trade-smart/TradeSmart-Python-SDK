# Contributing to `tradesmart` (Python SDK)

Thank you for helping. This SDK talks to a real brokerage API, so the rules below are strict.

## Setup

Development runs in Docker; you only need `git` and Docker (with Compose) on your machine. Do
not create a host virtual environment.

```bash
git clone https://github.com/trade-smart/tradesmart-sdks.git
cd tradesmart-sdks/sdk-python
docker compose build        # builds the dev images (Python 3.12, and 3.11, 3.13, 3.14, 3.15)
```

`compose.yaml` mounts the whole repository into the container, so code edits are picked up
without rebuilding. Rebuild only after changing dependencies in `pyproject.toml`. Tool caches
(pytest, ruff, mypy, coverage) stay inside the container.

Python 3.11 is the floor: code must run on 3.11–3.15. Newer syntax and library features need a
version check or must wait until the floor reaches them.

## Workflow: test-driven

1. Write or extend a test that describes the behaviour (`tests/unit/` for pure logic,
   `tests/integration/` for client flows with `respx` or the local WebSocket server).
2. Run it and watch it fail for the expected reason.
3. Implement the smallest change that makes it pass.
4. Run the full gates (below) before opening a pull request.

Only implement what TradeSmart's official API documentation
(https://apidocs.tradesmartonline.in/v2/) specifies; the API documentation lists every
endpoint and field with the known ambiguities. Never invent endpoints, fields or behaviour: if
the documentation is unclear, open an issue describing the gap. Undocumented endpoints stay
behind `client.request()`.

## Quality gates (all must pass, output pristine)

```bash
docker compose run --rm dev pytest                       # unit + mocked integration, in parallel
docker compose run --rm dev pytest --cov=tradesmart --cov-report=term-missing   # target >= 90 %
docker compose run --rm dev ruff check .                 # includes examples/ and tests/
docker compose run --rm dev ruff format --check .
docker compose run --rm dev mypy src                     # --strict
docker compose run --rm py311 pytest -q                  # Python 3.11 floor
docker compose run --rm py313 pytest -q                  # also py314 and py315
docker compose run --rm dev sh -c "python -m build && twine check dist/*"   # writes dist/
```

Tests run in parallel by default: `addopts` in `pyproject.toml` carries `-n 4 --dist loadfile`
(pytest-xdist, 4 workers, all tests of one file on the same worker), and coverage still reports
the combined total. Run `pytest -n 0` for a serial run, for example to debug with `--pdb` or to
read a failing test's output in order. Keep tests independent of each other and of the order they
run in: use `tmp_path` and `monkeypatch`, and bind servers to port 0.

The live suites always run serially: setting `TRADESMART_LIVE_TESTS=1` or
`TRADESMART_LIVE_ORDER_TESTS=1` overrides `-n`, `--tx`, `-d` and `--dist` so that no worker starts,
and refuses `-f/--looponfail` (`tests/conftest.py`), because the live order cap and the "one open
order at a time" rule hold per process; a live order test inside an xdist worker skips itself.

The suite never touches the network: HTTP is mocked and the WebSocket runs against a local
server, with synthetic ids and tokens. **Live tests are opt-in and read-only** (user details,
limits, search, quote, order book) and need real credentials in your shell; Compose passes them
into the container and they are never written to a file (optional `TRADESMART_BASE_URL` for
another environment):

```bash
TRADESMART_LIVE_TESTS=1 TRADESMART_CLIENT_ID=... TRADESMART_ACCESS_TOKEN=... \
  docker compose run --rm dev pytest -m live -v
```

Instead of an environment token you can log in once inside Docker:
`docker compose run --rm --service-ports login --client-id YOUR_CLIENT_ID`. The token file then
lives in the `tradesmart-login` Docker volume (mounted at `/root/.config/tradesmart` in every
compose service), never in the repository; `docker compose run --rm dev python -m tradesmart
logout` removes it. A plain `pytest` never runs the live order tests in `tests/live_orders/`
(they need `-m live_orders` and their own opt-in; the order tests follow the gates in
`../specifications/sdk-behaviour.md`, section 14, and `tests/live_orders/README.md`).

`tests/unit/test_examples_import.py` keeps the examples and documentation honest: every example
must compile, import only real names, not run on import and catch `TradeSmartError`; every
Python snippet in the README and the user guide (`docs/`) must be labelled
*illustrative*, import only real names and call only real client and stream methods with real
keyword arguments; the guide's complete first-order script runs against a mocked API; every
public method, `ClientConfig` field and exported error must appear in the README; relative links
and anchors in the docs must resolve; and the published docs (READMEs, CHANGELOG, user guide,
`docs/architecture.md`, the shared `../docs/`, migration guides, examples) must not reference
internal notes, local reference folders or test files.

Latency: `benchmarks/latency.py` (dev-only, not shipped) measures the SDK's per-call and per-tick
overhead with the network mocked — `docker compose run --rm dev python benchmarks/latency.py`.
Run it before and after changes on a hot path.

## Public API conventions

- `TradeSmart` and `AsyncTradeSmart` expose the same **flat** methods (`client.place_order()`,
  `client.orders()`, …); add a new endpoint to both, with a `*_call()` builder in `endpoints/`,
  and document it in the README API reference.
- Use the familiar parameter names (`order_type`, `validity`, `order_id`, `tag`, `from_date`,
  `to_date`); model attributes follow the same names. Enums send TradeSmart's wire codes and
  accept the common words.
- Do not name other brokers or their SDKs anywhere except in the migration guides
  (`docs/migration/`).

## Safety rules (non-negotiable)

- **Never place real orders** from tests, examples or scripts you run while developing. Automated
  tests are mocked; live tests (`tests/live/`, `pytest -m live`, opt-in via
  `TRADESMART_LIVE_TESTS=1`) are **read-only** and a unit test enforces that.
- Never add retries to `PlaceOrder`, `PlaceMultiOrder`, `ModifyOrder`, `CancelOrder`,
  `ExitSNOOrder`, `GenAcsTok`, the GTT writes (`PlaceGTTOrder`, `ModifyGTTOrder`,
  `CancelGTTOrder`) or the iceberg writes (`PlaceIceBergOrder`, `ModifyIceBergOrder`,
  `CancelIceBergOrder`) (not even through `request()`); an ambiguous outcome must raise
  `OrderStatusUnknownError`, and success is only the exact documented `stat` word.
- Examples that change state must default to a dry run and require an explicit flag
  (see `examples/place_order.py`).

## Secrets

- Never commit credentials, tokens, `.env` files or real account data. Read credentials from
  environment variables only.
- Never log, print or put into exception messages any token, secret key, app key, checksum or
  authorisation code. Obtain loggers via `tradesmart._logging.get_logger()` so redaction applies;
  never call `print` in library code.
- Fixtures live in `../specifications/test-fixtures/` and use synthetic values only
  (`TEST123`, `test-access-token`, `user@example.com`, `Test User`). The official docs contain
  real-looking sample tokens — never copy them.

## Pull requests

1. Fork the repository and create a branch for your change.
2. Keep commits small and focused, with conventional messages (`feat(python): ...`,
   `fix(python): ...`, `docs(python): ...`). Update `CHANGELOG.md` (the `Unreleased` section)
   for user-visible changes and the README / user guide when behaviour changes.
3. Run all quality gates above, then open a pull request against `main` describing what changed
   and why. A maintainer reviews it; the gates must pass before it is merged.
4. Releases (version numbers, tags, GitHub Releases and package publishing) are made by the
   maintainers.

Security issues: please do not open a public issue or pull request; follow the repository's
security policy (`SECURITY.md` at the repository root).

## Licence

By contributing you agree that your contributions are licensed under the Apache License 2.0
(see `LICENSE`).
