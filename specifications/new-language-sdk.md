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
- Pass every conformance vector and every fixture in `specifications/`; add a test per file that
  loads it. Some vectors drive the real client through an in-memory HTTP transport
  (`read_payloads`, `outcome_words`, `transport_failures`, `script_master_freshness`); their README
  notes say how.
- Run the CI on Linux, Windows and macOS from the start, and keep the tests independent of the
  operating system (home folder, platform-specific output, signals, floating-point tolerance,
  helper threads): spec §14, "Operating systems". Docker on Linux alone hides these failures.
  macOS may run only on release tags and manual runs (it costs ten times the minutes while the
  repository is private), but must pass before every release.
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
  `ModifyOrder`, `CancelOrder`, `ExitSNOOrder`, `GenAcsTok`, the GTT and iceberg writes), an
  ambiguous outcome is `OrderStatusUnknown` (exact success words per endpoint, spec 4.2.1), no
  tokens, secret keys, checksums or codes in logs, parse the body whatever the HTTP status, never
  follow HTTP redirects (most HTTP libraries follow them by default and may re-send an order body).
  The client-side order limits of spec §3.4 (per second and per minute) apply too.
- Add live suites with the same gates: read-only opt-in; orders only with a second opt-in, the
  test account check and an order cap; mocks by default. Funded mode (spec §14, optional) only with
  its own opt-ins, the balance ceiling and every money limit there.
- No publishing, no push, and no other broker names outside the migration guides.
