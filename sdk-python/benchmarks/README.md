# Latency benchmark (dev only)

Not shipped in the wheel or sdist. Measures SDK overhead (no network): a raw httpx POST baseline,
`place_order()` and `quote()` (sync and async, via `httpx.MockTransport`), `parse_message` for
`tf`/`df`/`dk`, and `DeliveryBuffer.put` + `get`. The client-side rate limiter is bypassed for
measurement only. Each figure is the minimum of several repeats, in microseconds per call.

```
docker compose run --rm dev python benchmarks/latency.py
```
