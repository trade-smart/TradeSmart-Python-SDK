# Market data

Instruments, quotes, candles and option chains with the `tradesmart` Python SDK. All calls on
this page are reads. Code blocks are *illustrative*: tokens and symbols are placeholders. For
live prices use the [stream](streaming.md).

## Identify an instrument

TradeSmart identifies an instrument by its **exchange** (`NSE`, `BSE`, `NFO`, `BFO`, `CDS`,
`BCD`, `MCX`) plus either its **trading symbol** (for NSE equities with the series, e.g.
`SBIN-EQ`; used by orders and daily candles) or its numeric **token**, always passed as a
string (`"22"`, the token of `ACC-EQ` on NSE in these examples; used by quotes, security info,
intraday candles and the stream; `search_instruments()` gives the token of any instrument). An `int`
token, symbol or search text raises `RequestValidationError` before anything is sent.

### Search

*Illustrative:*

```python
for m in client.search_instruments("SBIN"):  # [] when nothing matches
    print(m.exchange, m.tradingsymbol, m.token, m.lot_size, m.tick_size, m.instname)
```

### Script master (every instrument of an exchange)

TradeSmart publishes one instrument file per exchange (the *script master*).

*Illustrative (`<column>` is a header of the downloaded file):*

```python
from tradesmart import instruments

rows = client.instruments("NFO")  # list[dict[str, str]]
print(rows[0].keys())  # the file's own column names
hits = instruments.find(rows, **{"<column>": "<value>"})  # exact string match

path = client.download_instruments("NFO", "./data")  # ./data/NFO_symbols.txt
rows = instruments.load(path)  # parse a downloaded file yourself
```

- **The column layout is not documented by TradeSmart**, so rows are plain dicts keyed by the
  file's own header row. Inspect `rows[0].keys()` once and filter with
  `tradesmart.instruments.find()`.
- **Freshness.** `instruments(exchange)` downloads the file into `~/.cache/tradesmart` (per user;
  set `ClientConfig(instruments_dir=...)` to use another folder, `~` expanded)
  and reuses it until TradeSmart's next daily refresh: a cached file is fresh if it was written
  at or after the most recent refresh time (`ClientConfig.script_master_refresh_time`, 07:00 IST
  by default — observed, *pending confirmation by TradeSmart*). No extra request is made while
  the file is fresh.
- **A file the server has not regenerated yet** (weekends, holidays, a late refresh) is not
  downloaded again on every call: at most every `script_master_recheck_minutes` (15) the SDK
  asks the server whether a newer file exists (`If-Modified-Since`) and keeps the cached file if
  not.
- **During a TradeSmart outage** such a re-check fails (network error, timeout or HTTP 5xx): the
  SDK returns the cached file and logs one WARNING per failed re-check (not per call: "using the
  cached script master; re-check failed: …"). The server is asked again at most once every
  `script_master_recheck_minutes`; calls in between use the cached file without a request. A
  first download, or `force=True`, still raises.
- `download_instruments(exchange, dest_dir=None, *, force=False)` only downloads and returns the
  file's path (into `dest_dir`, `~` expanded, else `instruments_dir`, else the user cache
  folder); `force=True` downloads again.
- A cached file that is not valid UTF-8 or CSV (for example a damaged download) raises
  `ResponseParseError`; delete it and call again.
- The rows are parsed again on every `instruments()` call; keep the list if you need it
  repeatedly. Build your symbol → token mapping once a day, before the market opens, not inside
  your trading loop.

## Quotes and security info

*Illustrative:*

```python
quote = client.quote("NSE", "22")
print(quote.last_price, quote.open, quote.high, quote.low, quote.close, quote.volume)
print(quote.bp1, quote.bq1, quote.sp1, quote.sq1)  # best bid / ask; levels 1..5
print(quote.lc, quote.uc)  # the day's lower / upper price limits

info = client.security_info("NSE", "22")
print(info.symname, info.isin, info.lot_size, info.tick_size, info.frzqty)
```

Prices are `Decimal` (`None` when absent). `GetQuotes` is limited to 100 calls per second and
1,000 per minute; the SDK waits for a free slot by default. For many instruments or continuous
prices use the [stream](streaming.md) instead of polling.

## Candles

*Illustrative:*

```python
from datetime import date, datetime, timedelta, timezone

now = datetime.now(timezone.utc)  # datetimes must be timezone-aware
candles = client.historical_data(
    "NSE", "22", from_date=now - timedelta(hours=3), to_date=now, interval=5
)
for c in candles[:3]:  # newest first
    print(c.time, c.into, c.inth, c.intl, c.intc, c.intv)  # open, high, low, close, volume

daily = client.daily_history("NSE", "SBIN-EQ", from_date=date(2026, 1, 1), to_date=now)
```

- `historical_data(exchange, token, from_date=None, to_date=None, interval=None)`: intraday
  candles, **newest first**. `interval` in minutes: `1, 3, 5, 10, 15, 30, 60, 120, 240`
  (TradeSmart's default is 1). Limited to 100 calls per second and 1,000 per minute.
- `daily_history(exchange, tradingsymbol, from_date, to_date)`: daily candles; both dates are
  required.
- **Dates** accept a timezone-aware `datetime`, a `date` or epoch seconds as an `int`. A naive
  `datetime`, text, a float or a bool raises `RequestValidationError` (nothing is sent). A `date` means **00:00 IST** that day, so
  `to_date=date.today()` ends at midnight and excludes today's session; pass
  `datetime.now(timezone.utc)` to include it.
- TradeSmart's history windows: NSE/BSE 360 days, MCX 70 days, NFO/BFO/CDS/BCD 65 days.

## Option chain

*Illustrative (the symbol is a placeholder — take a real one from search or the script master):*

```python
chain = client.option_chain("NFO", "YOUR_NFO_SYMBOL", strike_price=24000, count=5)
for contract in chain:
    print(contract.tradingsymbol, contract.option_type, contract.strike_price, contract.token)
```

`count` (at least 1) strikes either side of `strike_price`, calls and puts, in the order
TradeSmart sends them.

## Async

`AsyncTradeSmart` has the same methods; `await` them. Many quotes at once:

*Illustrative:*

```python
import asyncio

from tradesmart import AsyncTradeSmart


async def main():
    async with AsyncTradeSmart.from_env() as client:
        quotes = await asyncio.gather(client.quote("NSE", "22"), client.quote("NSE", "2885"))
        for q in quotes:
            print(q.tradingsymbol, q.last_price)


asyncio.run(main())
```

The client-side rate limiter keeps concurrent calls within TradeSmart's limits.
