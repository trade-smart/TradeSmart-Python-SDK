"""Dev-only latency benchmark for the SDK hot paths (not shipped in the wheel/sdist).

Run from sdk-python/:  docker compose run --rm dev python benchmarks/latency.py

REST calls go through httpx.MockTransport (no network) and the client-side rate limiter is
bypassed, so the numbers show SDK overhead only. Each figure is the minimum over REPEATS
runs of ITERATIONS calls, in microseconds per call.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from tradesmart import AsyncTradeSmart, TradeSmart
from tradesmart.stream.delivery import DeliveryBuffer
from tradesmart.stream.protocol import parse_message

FIXTURES = Path(__file__).resolve().parents[2] / "specifications" / "test-fixtures"
ITERATIONS = 2000
REPEATS = 7
ORDER = {
    "exchange": "NSE",
    "tradingsymbol": "TCS-EQ",
    "quantity": 1,
    "transaction_type": "BUY",
    "product": "MIS",
    "order_type": "LIMIT",
    "price": "1500.05",
}


class _NoLimiter:
    """Measurement only: replaces the rate limiter so tight loops are not throttled."""

    def acquire(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def aacquire(self, *args: Any, **kwargs: Any) -> None:
        pass


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _mock_transport() -> httpx.MockTransport:
    order, quote = _fixture("place_order_success.json"), _fixture("get_quotes_success.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=quote if request.url.path.endswith("GetQuotes") else order)

    return httpx.MockTransport(handler)


def _best_us(fn: Callable[[], Any]) -> float:
    best = float("inf")
    for _ in range(REPEATS):
        start = time.perf_counter()
        for _ in range(ITERATIONS):
            fn()
        best = min(best, time.perf_counter() - start)
    return best / ITERATIONS * 1e6


def _buffer_roundtrip(msg: Any) -> Callable[[], Any]:
    buf = DeliveryBuffer(1000)
    loop = asyncio.new_event_loop()

    def once() -> Any:
        buf.put(msg)
        return loop.run_until_complete(buf.get())

    return once


def main() -> None:
    rows: list[tuple[str, float]] = []
    transport = _mock_transport()
    url = "https://example.invalid/NorenWClientAPI/PlaceOrder"

    with httpx.Client(transport=transport) as raw:
        rows.append(("raw httpx POST (baseline)", _best_us(lambda: raw.post(url, data="jData={}"))))

    with httpx.Client(transport=transport) as http:
        client = TradeSmart("TEST123", access_token="synthetic-token", http_client=http)
        client._transport._limiter = _NoLimiter()  # type: ignore[assignment]  # measurement only
        rows.append(("sync place_order()", _best_us(lambda: client.place_order(**ORDER))))
        rows.append(("sync quote()", _best_us(lambda: client.quote("NSE", "11536"))))

    async def measure_async() -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        async with httpx.AsyncClient(transport=transport) as ahttp:
            aclient = AsyncTradeSmart("TEST123", access_token="synthetic-token", http_client=ahttp)
            aclient._transport._limiter = _NoLimiter()  # type: ignore[assignment]  # measurement only
            for label, make in (
                ("async place_order()", lambda: aclient.place_order(**ORDER)),
                ("async quote()", lambda: aclient.quote("NSE", "11536")),
            ):
                best = float("inf")
                for _ in range(REPEATS):
                    start = time.perf_counter()
                    for _ in range(ITERATIONS):
                        await make()
                    best = min(best, time.perf_counter() - start)
                out.append((label, best / ITERATIONS * 1e6))
        return out

    rows.extend(asyncio.run(measure_async()))

    ws = _fixture("ws_messages.json")
    for kind in ("tf", "df", "dk"):
        raw_msg = json.dumps(ws[kind])
        rows.append(
            (f"parse_message({kind})", _best_us(lambda raw_msg=raw_msg: parse_message(raw_msg)))
        )
    rows.append(
        ("DeliveryBuffer put+get", _best_us(_buffer_roundtrip(parse_message(json.dumps(ws["tf"])))))
    )

    width = max(len(name) for name, _ in rows)
    print(f"{'operation':<{width}}  {'us/call':>10}")
    for name, us in rows:
        print(f"{name:<{width}}  {us:>10.1f}")


if __name__ == "__main__":
    main()
