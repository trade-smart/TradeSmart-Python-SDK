# Live tests (opt-in, read-only)

These tests call the real TradeSmart API with **real credentials**. They are skipped unless
`TRADESMART_LIVE_TESTS=1`. They never place, modify or cancel orders and never call `request()`;
only read-only methods are used. `tests/unit/test_live_suite_is_read_only.py` guards every module
in this folder against state-changing calls.

## One-time login

The tests build the client with `TradeSmart.from_env()`, so log in once (the saved login lives in a
Docker volume, never in the repo). From `sdk-python/`:

```
docker compose run --rm --service-ports login --client-id <YOUR_CLIENT_ID>
```

The `login` service publishes port 8080 on the host's loopback only, so the browser redirect to
`http://127.0.0.1:8080/callback` reaches the container and the login finishes by itself (no
pasting). Access tokens expire daily at 06:00 IST; log in again after that. Alternatively set
`TRADESMART_CLIENT_ID` and `TRADESMART_ACCESS_TOKEN` in the host shell.

## Run

All commands run from `sdk-python/`. In PowerShell set `$env:TRADESMART_LIVE_TESTS = "1"` first;
in a POSIX shell prefix the command with `TRADESMART_LIVE_TESTS=1`.

| Group | File | Command (after `TRADESMART_LIVE_TESTS=1`) |
| --- | --- | --- |
| Everything | all | `docker compose run --rm dev pytest -m live -v` |
| Auth / session | `test_live_auth.py` | `docker compose run --rm dev pytest tests/live/test_live_auth.py -v` |
| Account | `test_live_account.py` | `docker compose run --rm dev pytest tests/live/test_live_account.py -v` |
| Market data | `test_live_market.py` | `docker compose run --rm dev pytest tests/live/test_live_market.py -v` |
| Instruments | `test_live_instruments.py` | `docker compose run --rm dev pytest tests/live/test_live_instruments.py -v` |
| Greeks / analytics | `test_live_analytics.py` | `docker compose run --rm dev pytest tests/live/test_live_analytics.py -v` |
| Stream | `test_live_stream.py` | `docker compose run --rm dev pytest tests/live/test_live_stream.py -v -s` |
| Smoke (older) | `test_live_readonly.py` | `docker compose run --rm dev pytest tests/live/test_live_readonly.py -v` |
| UAT only: account and options | `test_live_uat.py` | `docker compose run --rm dev pytest tests/live/test_live_uat.py -v` (skips unless `TRADESMART_ENV=uat`) |

Pass `TRADESMART_LIVE_TESTS` through Compose, e.g.
`TRADESMART_LIVE_TESTS=1 docker compose run --rm dev pytest -m live -v`.

Live runs are always serial (one session client and one script-master download, within the rate
limits): `TRADESMART_LIVE_TESTS=1` turns off the parallel workers that a plain `pytest` starts, so
no `-n 0` is needed (and `--pdb` works); `-f/--looponfail` is refused.

### One segment only (for example MCX tonight)

Market-data, analytics and instrument tests are parametrised by segment with the ids
`nse-eq`, `nfo-fut`, `nfo-opt`, `mcx-fut`, `mcx-opt` (instruments: `nse`, `nfo`, `mcx`), and carry the
markers `nse`, `nfo`, `mcx`:

```
TRADESMART_LIVE_TESTS=1 docker compose run --rm dev pytest -m "live and mcx" -v
TRADESMART_LIVE_TESTS=1 docker compose run --rm dev pytest tests/live -k mcx -v
```

The auth and account tests are segment-independent and carry all three markers, so
`-m "live and mcx"` runs them too. The stream tests are marked `mcx` (one MCX future). Contracts are found at run time from the
script master (nearest contract whose expiry instant, i.e. the exchange close on its expiry day, is still ahead; CRUDEOIL, else GOLD; NIFTY; INFY, else RELIANCE) - no tokens or
expiries are hard-coded. If a contract cannot be found, or the script-master download or the
future's quote fails, that case is skipped with the reason (remembered once per session and
recorded in `.live-results/contracts.jsonl`), not repeated per test.

## Behaviour to expect

- An empty account (no holdings, positions, orders, trades) is a pass.
- Market closed: quotes/candles may be empty (pass); the stream test waits up to 20 s for a tick and
  then **skips** with "market closed"; it never fails for that.
- `option_chain_snapshot` uses `count=5` (about 10 quotes); the script master is downloaded once per
  session.

## Recorded observations

Each test appends a JSON line (shapes, counts, column names, headers, latencies; never tokens or
keys) to `.live-results/<suite>.jsonl` (`auth`, `account`, `market`, `instruments`, `analytics`,
`stream`); the folder is git-ignored. Useful for checking the holdings shape on an empty account,
the script-master columns and `Last-Modified`/`ETag`, and the stream connect and first-tick
latency. Every line carries `environment`, the `TRADESMART_ENV` name of the run (`production`
when unset), so a run against a test environment is never mistaken for a production one.

## A non-production test environment

To run the suite against a TradeSmart test environment instead of production, set
`TRADESMART_ENV` to its name (`uat` for the UAT-only checks above) and its URLs in
`TRADESMART_ENV_BASE_URL`, `TRADESMART_ENV_WS_URL` and `TRADESMART_ENV_AUTHORIZE_URL`
(`TRADESMART_ENV_SCRIPT_MASTER_URL` is optional). A missing URL stops the run; there is never a
fallback to production. The environment keeps its own saved login (`<env>-token.json` next to
`token.json`), and the run refuses the production token file and a `TRADESMART_ACCESS_TOKEN` set
in the shell. The rules are in `tests/_environment.py` and in
[`tests/live_orders/README.md`](../live_orders/README.md). With `TRADESMART_ENV=uat`, data the
test environment does not have (MCX contracts, daily candles from `daily_history()`) is skipped
with "not available in this test environment".

## Notes

- Never commit credentials or paste them into logs.
- Optional, production only (`TRADESMART_ENV` unset): `TRADESMART_BASE_URL` targets another
  REST base URL.
