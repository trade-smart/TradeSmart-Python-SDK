"""Fetch several quotes concurrently with ``AsyncTradeSmart`` (read-only).

Credentials: ``from_env()`` -- the login saved by ``python -m tradesmart login``, or
``TRADESMART_CLIENT_ID`` and ``TRADESMART_ACCESS_TOKEN``.

Run: ``python examples/async_quotes.py "NSE|22" "NSE|2885"`` (``EXCH|TOKEN`` pairs).

``GetQuotes`` is limited to 100 requests/second and 1000/minute. The client-side limiter
keeps concurrent calls within that: with the default ``rate_limit_mode="wait"`` calls wait
for a free slot; with ``"raise"`` they raise ``RateLimitError`` instead.
"""

from __future__ import annotations

import asyncio
import sys

from tradesmart import APIError, AsyncTradeSmart, TradeSmartError
from tradesmart.models.market import Quote


async def run(pairs: list[tuple[str, str]]) -> int:
    try:
        client = AsyncTradeSmart.from_env()
    except TradeSmartError as exc:  # e.g. TRADESMART_CLIENT_ID not set
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return 2
    async with client:
        results = await asyncio.gather(
            *(client.quote(exchange, token) for exchange, token in pairs),
            return_exceptions=True,
        )
    failed = 0
    for (exchange, token), result in zip(pairs, results, strict=True):
        if isinstance(result, Quote):
            print(f"{exchange}|{token} {result.tradingsymbol} last={result.last_price}")
        elif isinstance(result, APIError):
            failed += 1
            print(f"{exchange}|{token} failed: {result.emsg or result.message}", file=sys.stderr)
        elif isinstance(result, TradeSmartError):  # network failure, timeout, ...
            failed += 1
            print(f"{exchange}|{token} failed: {result}", file=sys.stderr)
        else:
            raise result
    return 1 if failed else 0


def main() -> int:
    keys = sys.argv[1:] or ["NSE|22"]
    pairs = [(key.partition("|")[0], key.partition("|")[2]) for key in keys]
    return asyncio.run(run(pairs))


if __name__ == "__main__":
    sys.exit(main())
