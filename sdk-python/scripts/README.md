# Developer scripts

Not part of the published package (excluded from the wheel and sdist).

## `live_probes.py` - read-only measurement probes

Calls the live TradeSmart API with `profile`, `quote` and script-master GETs only; never orders.
Refuses to run unless `TRADESMART_LIVE_TESTS=1`. The client comes from `TradeSmart.from_env()`
(env token or the saved login). The SDK limiter paces calls; nothing bypasses the documented limits.

```
export TRADESMART_LIVE_TESTS=1
docker compose run --rm dev python scripts/live_probes.py --probe latency
docker compose run --rm dev python scripts/live_probes.py --probe keepalive --waits 30,55,65,95,125
docker compose run --rm dev python scripts/live_probes.py --probe freshness
docker compose run --rm dev python scripts/live_probes.py --probe ratelimit --exchange MCX --symbol CRUDEOIL
docker compose run --rm dev python scripts/live_probes.py --probe all
```

| Probe | What it measures |
|-------|------------------|
| `latency` | cold-connect `quote()` call, then 30 warm calls: p50 / p95 / max (`--count`) |
| `keepalive` | F6: idle waits (`--waits`, default 30,55,65,95,125 s, about 6 min) on a raw httpx client whose own keep-alive expiry is above every wait; records whether the same TCP connection was reused and the largest idle time that still was |
| `freshness` | F10: `Last-Modified`, `ETag`, `Cache-Control` of the NSE/NFO/MCX script master, plus the status of a conditional request (`If-None-Match`, else `If-Modified-Since`); bodies are not downloaded |
| `ratelimit` | client-side limiter wait over 120 rapid `quote()` calls (`--rapid`, default 120); observe only |

`--exchange` / `--symbol` (default `NSE` / `RELIANCE-EQ`) choose the instrument for the quote probes.
Results are printed and appended as JSON lines to `.live-results/probes.jsonl` (git-ignored).
Tokens and Authorization headers are never printed or recorded.

## `gen_conformance.py` and `gen_model_tables.py` - generated specifications

Write the language-neutral files under `../specifications/` from the Python SDK. Both are
deterministic (an unchanged SDK leaves `git diff` empty) and each has a sync test that fails when the
committed output is stale (`tests/unit/test_conformance_vectors.py`, `tests/unit/test_model_tables.py`).

```
docker compose run --rm dev python scripts/gen_conformance.py     # specifications/conformance/*.json
docker compose run --rm dev python scripts/gen_model_tables.py    # specifications/models.md
```
