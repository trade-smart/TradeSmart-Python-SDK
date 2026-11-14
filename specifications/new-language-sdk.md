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
