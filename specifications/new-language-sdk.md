# Starting another language SDK

The Python SDK (`sdk-python/`) is the reference implementation. For a new language:

- Read, in order: `specifications/sdk-behaviour.md` (the behaviour contract; its §15 carries the
  live-verified facts and open items, F-numbers), `specifications/models.md` (every model field and
  enum, and the Greeks/analytics result types), `specifications/conformance/README.md`,
  `specifications/test-fixtures/`. Everything a new SDK needs is in `specifications/`: the spec
  carries the live-verified table in §15 and every confirmed rule.
- Create `sdk-<lang>/` with a Dockerfile and compose file. Docker only, like Python: never install
  the language toolchain on the host; run build, test and lint through compose.
- Keep the same public method names (`place_order`, `orders`, `quote`, `option_greeks`, ...) in the
  language's case style; flat client methods, no namespaces; sync and async variants where the
  language has both.
- Implement the OCO feature too (`specifications/sdk-behaviour.md` 2.9): `place_oco`, `modify_oco`, `cancel_oco`, the
  OCO leg, trigger and side models, the `is_oco` / `stop_loss` / `target` readers on the GTT row, and the
  `oco_requests.json` and `oco_rows.json` vectors (and the OCO fixtures in `test-fixtures/`). The three OCO writes are never retried (success words and `Invalid Oi`: 4.2.1).
- Pass every conformance vector and every fixture in `specifications/`; add a test per file that
  loads it. Some vectors drive the real client through an in-memory HTTP transport
  (`read_payloads`, `outcome_words`, `transport_failures`, `script_master_freshness`); their README
  notes say how.
- Skip a conformance case only when its input cannot be expressed in the language at all (an
  integral float such as `2.0` given to an integer setting, in a language with one number type).
  Pin the exact skipped case names, with the reason, in one list in the SDK's conformance tests;
  every skip goes through that list, and a test fails when a listed name no longer exists or an
  unlisted case is skipped (`specifications/conformance/README.md`, "Inexpressible cases").
- Run the CI on Linux, Windows and macOS from the start, and keep the tests independent of the
  operating system (home folder, platform-specific output, signals, floating-point tolerance,
  helper threads): spec §14, "Operating systems". Docker on Linux alone hides these failures.
  A public repository runs macOS on every push and pull request (free there); while the
  repository is private, macOS may run only on release tags and manual runs (it costs ten times
  the minutes), but must pass before every release.
- Link newcomers to account opening, like Python: the README (the package registry page), the
  getting-started guide's "Get API access" step and the package metadata's project URLs carry
  `https://signup.tradesmartonline.in/?utm_source=<Lang>SDK&utm_medium=Organic&utm_campaign=SDK_Open_Demat&utm_content=<place>&channel=Organic&platform=web`
  (`<Lang>SDK` such as `TypeScriptSDK`; `<place>` is `readme` for a repository-root README,
  `registry` for the package README, then `getting_started`, `registry_sidebar` and `cli`; Python
  keeps its older names `pypi` and `pypi_sidebar`). The login command shows it once, only when it
  is about to ask for the Client ID on a first login (no saved token or keys yet). One constant
  builds it; a test pins every place. Never in errors, logs or docstrings.
- Keep a public `sdk-<lang>/.gitignore` of its own (paths anchored to that folder), so package
  builds never pick up the monorepo root's ignore file.
- Follow the same safety rules: no retries on order calls (`PlaceOrder`, `PlaceMultiOrder`,
  `ModifyOrder`, `CancelOrder`, `ExitSNOOrder`, `GenAcsTok`, the GTT, OCO and iceberg writes), an
  ambiguous outcome is `OrderStatusUnknown` (exact success words per endpoint, spec 4.2.1), no
  tokens, secret keys, checksums or codes in logs, parse the body whatever the HTTP status, never
  follow HTTP redirects (most HTTP libraries follow them by default and may re-send an order body).
  The client-side order limits of spec §3.4 (per second and per minute) apply too.
- Add live suites with the same gates: read-only opt-in; orders only with a second opt-in, the
  test account check and an order cap; mocks by default. Funded mode (spec §14, optional) only with
  its own opt-ins, the balance ceiling and every money limit there.
- No publishing, no push, and no other broker names outside the migration guides.
- Commits that reach a public repository carry only the TradeSmart author: no `Co-Authored-By`
  or other lines naming tools or assistants, in the commit message or anywhere else.

## Models: generate them from `specifications/models.json`

- Generate the response models, enums and field-type tables from `specifications/models.json` (the
  machine-readable form of `specifications/models.md`, produced by the reference SDK's generator). Do
  not write models by hand.
- Commit the generated code, mark it "generated, do not edit", make the generator deterministic, and
  add a test that regenerates it in memory and fails when the committed code is stale.
- The generated parsers apply the field-type rules, leniency, protected keys and nested models of the
  behaviour contract (§7), so a hand-written layer only adds the language's own conveniences.
- A field's name in the language's own case (snake_case in the contract, camelCase in some
  languages) is **not** a wire alias: the wire key is a separate, fixed string. Keep `raw` (the
  redacted wire object) and `extra` (the keys the model does not name) so nothing the server sends is
  lost, and so callers can always read the original key.

## Folder template: `sdk-<lang>/`

Every SDK folder has the same shape, in the language's own conventions where they differ:

- `README.md` (the package page: absolute links only, because a package registry renders it away from
  the repository), `CHANGELOG.md`, `CONTRIBUTING.md`, `LICENSE`, `NOTICE`.
- Its own `.gitignore` and `.dockerignore`, a `Dockerfile` and a compose file (Docker only: no
  toolchain on the host).
- `src/` (the library), `scripts/` (generators and tooling), `docs/` (the user guide: getting started,
  configuration, orders, streaming, instruments, errors, logging, testing, reference, architecture),
  `examples/` (runnable; every one type-checked or import-checked by a test; any that places an order
  is marked for a test account and uses a limit price far from the market).
- `tests/` with `unit/`, `conformance/` (one test file per vector file), `integration/` (mocked HTTP
  and a local WebSocket server), `live/` (opt-in, read-only) and `live_orders/`.
- `repo-root/` with the `README.md` and `CONTRIBUTING.md` that the language's public repository shows
  at its root (see "Export and CI per language" below).
- Test every fenced code block of the README and the guide the way the language allows (compile or
  type-check it), so the documentation cannot drift from the code.

## Language-mapping notes

Decide these once, state them in the user guide, and list every place the SDK deviates from the
reference SDK:

- **Keyword-only parameters.** Where the reference SDK has keyword-only or optional parameters,
  required positional parameters stay positional and the rest go into one trailing options object (or
  the language's equivalent). An option name the SDK does not know is an error, so a typo cannot
  silently drop a value; nothing is sent.
- **Time inputs.** The contract accepts three forms: an instant, integer epoch seconds, and a calendar
  date. In a language without a separate date type, the conformance vectors encode a calendar date as
  `{"date": "YYYY-MM-DD"}` (00:00 IST that day); map it to the same input shape, with a helper to
  build it. Any other value is a validation error on the field `time`.
- **"A naive datetime."** A language with no naive datetime type maps that vector case to the nearest
  input that cannot be an instant (for example a date-time string without an offset) and rejects it
  with the same error on the same field.
- **Promise-only languages.** Where every call is already asynchronous, there is no `Async` prefix:
  one client class and one stream class serve both of the reference SDK's calling styles (callbacks
  and iteration). State the consequences of that merge in the guide (the single-class stream limits).
- **Numbers.** Prices, money and volumes use an exact decimal type; never read a JSON number through a
  float (keep its exact text until a field's type converts it). Say how a price given as a float, as
  text or as a decimal object reaches the wire (text exactly as written; a float as its shortest
  round-trip text).
- **Cancellation.** Map the contract's interruption rules (§4.1, §5.2, §5.6) to the language's own
  mechanism: an interruption before the request is handed to the HTTP library sends nothing and logs
  no outcome line; after it, a state-changing call's outcome is unknown and it is never resent.

## Export and CI per language

Each language has its own public repository. A new language adds, in the shared tooling:

- an export profile that lists what it exports: the shared root files (`LICENSE`, `NOTICE`,
  `SECURITY.md`, `CODE_OF_CONDUCT.md`), `specifications/`, `docs/`, its own `sdk-<lang>/`, its own CI
  workflow and the shared issue templates;
- its repository root files, taken from `sdk-<lang>/repo-root/` and placed at the root of that
  language's export, replacing the same-named files of the monorepo root;
- its build, cache and dependency folders to the never-public names, and its ignore entries to the
  repository's ignore file (the package keeps its own anchored ignore file as well);
- a CI workflow with path filters for `sdk-<lang>/**`, `specifications/**` and `docs/**` that runs the
  Docker gates of the language (lint, type-check or compile, tests on the supported runtime versions,
  build and package checks), and a dependency-update entry for its package manager.

Every check of the export (forbidden ids, secret-shaped values, internal paths, other brokers' names,
test hosts) runs per profile, unchanged. No public file may name the repository's internal material, so
keep such paths out of source folder names and comments as well.

## Shared environment variables and the saved login

- The live suites of every SDK use the same `TRADESMART_*` variable names (credentials:
  `TRADESMART_CLIENT_ID`, `TRADESMART_ACCESS_TOKEN`, `TRADESMART_APP_KEY`, `TRADESMART_SECRET_KEY`;
  the location of the saved login: `TRADESMART_TOKEN_FILE`; and the live-test opt-ins documented in
  the run book), passed through the compose file only when set on the host.
- The saved login (the token file of `specifications/sdk-behaviour.md` §6.5) is shared: the same
  location rules, JSON format, permissions and atomic write, so a login made with any SDK's command
  serves every SDK. Implement the token file reader against the same vectors.
- A default test run allows no network except loopback and uses an empty temporary folder for the
  saved login, so a developer's own login can never leak into a test.

## Live order testing: write a driver, do not port the matrix

The live-order matrix (UAT and mock-session modes, with every gate and limit) exists once, in the
shared harness. A new SDK adds a **driver** that executes exactly one SDK call per request of the
matrix's driver protocol (`specifications/live-driver.md`) and reports the outcome: it never retries,
batches or adds calls of its own, and it holds no gate of its own beyond refusing to start without the
harness's per-run nonce (`TRADESMART_LIVE_DRIVER_RUN`) and both order opt-ins; the harness holds every
other gate. Do not port the order suites to the new language. Until the driver exists the
SDK has the read-only live suite only, with the same gates as above.

The driver covers the **state-changing calls only** (place, sliced place, modify, cancel, exit, and the
GTT and OCO calls; driver protocol 2). The harness's own reads (book lookups, account match, limits preflight, quotes, instruments,
teardown checks) stay on the Python client as an independent referee, even though the protocol carries
the read methods so a driver can be tested in full. The async-client and stream family of the matrix runs
for Python only and is skipped, with a reason, for other SDKs. A driver never runs in funded mode or in
the live GTT test.
