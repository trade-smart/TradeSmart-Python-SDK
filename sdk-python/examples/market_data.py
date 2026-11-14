"""Market data tour (read-only): search, quote, security info, candles, script master.

Credentials: ``from_env()`` -- the login saved by ``python -m tradesmart login``, or
``TRADESMART_CLIENT_ID`` and ``TRADESMART_ACCESS_TOKEN``.

Run: ``python examples/market_data.py --search SBIN --exchange NSE``
Optional: ``--script-master-dir ./data`` also downloads and parses the exchange's script
master into that folder (reused until TradeSmart's next daily refresh, 07:00 IST by
default). Its column layout is undocumented, so rows are plain dicts keyed by the file's
own header.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from tradesmart import APIError, Exchange, TradeSmart, TradeSmartError
from tradesmart.instruments import load


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeSmart market data tour (read-only).")
    parser.add_argument("--search", default="SBIN", help="search text for SearchScrip")
    parser.add_argument("--exchange", choices=[e.value for e in Exchange], default="NSE")
    parser.add_argument("--script-master-dir", help="download the script master here")
    args = parser.parse_args(argv)

    try:
        client = TradeSmart.from_env()
    except TradeSmartError as exc:  # e.g. TRADESMART_CLIENT_ID not set
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return 2
    with client:
        try:
            matches = client.search_instruments(args.search)  # [] when nothing matches
            match = next((m for m in matches if m.exchange == args.exchange), None)
            if match is None or match.token is None or match.tradingsymbol is None:
                print(f"No {args.exchange} match for {args.search!r}", file=sys.stderr)
                return 1
            print(f"{match.exchange}:{match.tradingsymbol} token={match.token}")

            quote = client.quote(match.exchange, match.token)
            print(f"Last price {quote.last_price}  O {quote.open} H {quote.high} L {quote.low}")
            print(f"Best bid {quote.bp1} x {quote.bq1} / best ask {quote.sp1} x {quote.sq1}")

            info = client.security_info(match.exchange, match.token)
            print(f"Lot size {info.lot_size}, tick size {info.tick_size}, ISIN {info.isin}")

            # from_date / to_date: an aware datetime, a date (00:00 IST that day) or epoch secs.
            now = datetime.now(UTC)
            candles = client.historical_data(
                match.exchange,
                match.token,
                from_date=now - timedelta(hours=6),
                to_date=now,
                interval=5,
            )
            print(f"{len(candles)} five-minute candles (newest first)")
            for candle in candles[:3]:
                print(f"  {candle.time} close={candle.intc} volume={candle.intv}")

            daily = client.daily_history(
                match.exchange, match.tradingsymbol, now - timedelta(days=30), now
            )
            print(f"{len(daily)} daily candles")

            if args.script_master_dir:
                path = client.download_instruments(args.exchange, args.script_master_dir)
                rows = load(path)  # or simply client.instruments(exchange)
                print(f"Script master {path}: {len(rows)} rows")
                if rows:
                    print(f"Columns: {', '.join(rows[0])}")
        except APIError as exc:
            print(f"{exc.endpoint} failed: {exc.emsg or exc.message}", file=sys.stderr)
            return 1
        except TradeSmartError as exc:  # network failure, timeout or unexpected response
            print(f"Request failed: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
