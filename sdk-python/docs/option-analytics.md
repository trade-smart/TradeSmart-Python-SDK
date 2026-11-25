# Option Greeks and analytics

Three read-only helpers for option traders, on both `TradeSmart` and `AsyncTradeSmart`:

| Method | What you get |
|---|---|
| `option_greeks(exchange, tradingsymbol, ...)` | Implied volatility (IV), delta, gamma, theta, vega and rho of one option |
| `option_chain_snapshot(exchange, tradingsymbol, strike_price, count=25)` | Quotes for the contracts around a strike, with put-call ratio (PCR) and max pain |
| `buildup(exchange, token)` | Long or short buildup, unwinding or covering, from price and open-interest changes |

None of them places or changes an order. The async client has the same methods and parameters;
`await` them.

> **The Greeks are estimates.** The SDK computes them with standard option-pricing models from
> the prices TradeSmart gives you. They are **not** exchange values, they can differ from the
> numbers other platforms show (different rate, dividend, expiry-time or future choices), and
> they must not be the only basis for a risk decision. Max pain, PCR and buildup are
> indicators, not signals to trade.

## Greeks: `option_greeks()`

*Illustrative — symbols are placeholders; copy a real option `tradingsymbol` from
`client.search_instruments()` or the script master:*

```python
from tradesmart import TradeSmart

with TradeSmart.from_env() as client:
    g = client.option_greeks("NFO", "YOUR_NFO_OPTION_SYMBOL")
    print(g.status, g.iv, g.delta, g.gamma, g.theta, g.vega, g.rho)
    print(g.model, g.underlying_tradingsymbol, g.forward, g.years_to_expiry)
```

The result is a `tradesmart.OptionGreeks` with the Greeks and every input the SDK used (option
price, underlying price, `forward`, the rate, the expiry instant), so you can explain a number.
The SDK fetches what you do not supply: the instrument list and at most two `quote()` calls (the
option and its future). The parsed instrument list is kept on the client, one copy per exchange,
and read again only when the script-master file changes (after TradeSmart's daily refresh or
`download_instruments(force=True)`), so repeated calls do not re-parse it; `instruments()` itself
still returns a fresh list each time. Each call still makes its quotes, so for many contracts use
the bulk route in [Greeks for a whole chain](#greeks-for-a-whole-chain).

### Risk-free rate and dividend yield: the defaults and how to change them

| Setting | Default | Used by |
|---|---|---|
| `rate` | `0.065` (6.5 % a year) | both models, and the carry below |
| `dividend_yield` | `0.0` | the spot model (`spot_price=...`), and the carry when the SDK brings a future of another expiry to the option's expiry (see [Which model](#which-model-and-which-underlying)). The Black-76 formula itself does not use it: a futures price already includes dividends |

Both are annual, continuously compounded decimals (`0.065` means 6.5 %). Any number type works
(`float`, `int` or `Decimal`); a value that is not a finite number raises `ValueError` naming the
parameter before any quote is requested. **Change them on each
call** with the keyword arguments; nothing is stored on the client, so every call starts from the
defaults again:

*Illustrative:*

```python
g = client.option_greeks("NFO", "YOUR_NFO_OPTION_SYMBOL", rate=0.07)  # 7 % rate

g = client.option_greeks(
    "NFO",
    "YOUR_NFO_OPTION_SYMBOL",
    rate=0.07,
    dividend_yield=0.012,  # 1.2 %: the spot model's yield
    spot_price=24500,
)
```

The defaults are also available as `tradesmart.greeks.DEFAULT_RATE` and
`DEFAULT_DIVIDEND_YIELD`.

### Which model, and which underlying

- **Default: Black-76 on the underlying future.** The SDK finds the future in the instrument list
  (the one with the same underlying symbol; when there is none and the option's symbol ends in
  `OPT`, the future whose symbol ends in `FUT` instead, as on BFO, where SENSEX options are listed
  under `BSXOPT` and its futures under `BSXFUT`) and quotes it. It picks the future with the **same
  expiry** as the option; if there is none (typical for weekly options), the **nearest later**
  future, and failing that the **nearest earlier** one. On NFO, BFO, CDS and BCD a future with a
  different expiry stands in for the index/spot forward and is brought to the option's expiry
  with a carry adjustment:

  `forward = F × e^(−(rate − dividend_yield) × (Tf − To))`

  where `F` is the future's price and `Tf`, `To` the years to the future's and the option's
  expiry. The result's `forward` is that adjusted price and `same_expiry_future` is `False`.
  When the expiries match, `same_expiry_future` is `True` and `forward` is `F`.
  **Set `dividend_yield` for these options**, or the forward is off and so are IV and delta
  (a call/put skew on an index; on a weekly currency option a few IV points, or a put with no
  IV at all): for an **index option**, the index's dividend yield (for example `0.013`); for a
  **currency option** (CDS, BCD), the **foreign currency's interest rate** (for example `0.045`
  for USD in USDINR, the Garman–Kohlhagen convention). With the default `0` the carry uses the
  rate alone. Alternatively pass `underlying_price=` with your own forward for the option's
  expiry.
- **MCX: no carry.** An MCX option is an option on the future itself, so `forward` is that
  future's price as quoted, whatever its expiry; `same_expiry_future` still says whether the
  expiries match. `dividend_yield` has no effect.
- **`underlying_price=...`**: you give the future's price (for example from your own stream). The
  SDK selects no future and applies no carry (`dividend_yield` has no effect);
  `same_expiry_future` is `None`.
- **`spot_price=...`**: Black-Scholes-Merton on that spot price, with `dividend_yield`. No future is
  used.
- `option_price=...` overrides the option's last traded price (otherwise fetched with `quote()`),
  for example to test a price you would like to get.
- Passing both `spot_price` and `underlying_price` raises `ValueError`.

### Expiry time and units

Time to expiry runs to the exchange's closing instant on the expiry date, in IST: **NFO and BFO
15:30, CDS and BCD 12:30, MCX 23:30**. Pass `expiry_time=datetime.time(...)` to use another time
of day (and for any exchange not in that list, which otherwise raises `ValueError`). The time must
be naive and is read as IST: a `time` with a `tzinfo` raises `ValueError`.

*Illustrative — an MCX contract that stops trading earlier:*

```python
from datetime import time

g = client.option_greeks("MCX", "YOUR_MCX_OPTION_SYMBOL", expiry_time=time(17, 0))
```

Time is calendar time in years of 365 days. Units of the results:

| Value | Unit |
|---|---|
| `theta` | change in the option price **per calendar day** |
| `vega` | change in price per **1 volatility point** (0.01 of IV) |
| `rho` | change in price per **1 % change in the rate** |
| `iv` | decimal volatility (`0.18` = 18 %) |
| `delta`, `gamma` | per unit move of the underlying price |

The results never contain NaN or infinity: a value that cannot be computed is `None`.

### `status`: what it means and what to do

`status` is always set, and so is `years_to_expiry`. Check it before you use the numbers.

| `status` | Meaning | What to do |
|---|---|---|
| `"ok"` | Normal result. | Use the values. |
| `"near_expiry"` | Under 30 minutes to expiry. The values are valid but unstable: a small price change moves IV and the Greeks a lot. | Use with care; do not size positions from them without judgement. |
| `"expired"` | You called after the exchange close on the expiry day, or later. This is **not an error**: the option has no time value, so `price` is the intrinsic value, `iv` is `None`, `delta` is the at-expiry delta (1 or 0 for a call, 0 or -1 for a put, ±0.5 exactly at the money) and gamma, theta, vega and rho are `0.0`. | Nothing is wrong. Skip expired contracts in your loop, or use the intrinsic value. |
| `"price_at_or_below_intrinsic"` | The option's price is at the intrinsic value (within half a tick) or below it, or no volatility reproduces it, so there is no IV. The second case includes a price **above** what any volatility up to 500 % gives (typically a stale last price on expiry day). `iv` is `None` and the Greeks are the at-expiry values for the option's moneyness (delta as above, the others `0.0`). | Typical for deep in-the-money options and stale prices. Check the quote; pass a fresher `option_price` if you have one. |
| `"no_price"` | The option or the underlying has no last price (illiquid strike, market closed, no trade yet), or, after expiry, the future's quote could not be read for that contract. All values are `None`. | Retry when the market is open, or pass `option_price` / `underlying_price`. |

So there is no exception for "Greeks fail on expiry day": the call returns a result with one of
the statuses above.

After the expiry instant the SDK does not ask TradeSmart for the option's own quote (the
intrinsic value needs only the underlying; TradeSmart still serves quotes for the option and its
future after the close on expiry day). `option_price` in the result is the one you
passed, else `None`. If the future's quote still cannot be read for that contract (`Not_Ok`,
not found, an unreadable reply), the result is `"no_price"` instead of an error; session,
rate-limit, server and network errors are still raised. `"expired"` is what you get from the close on the expiry day until
TradeSmart's next daily script-master refresh (07:00 IST); after that the contract is gone from
the instrument list and `option_greeks()` raises `ValueError` ("option contract not found in
instrument master").

Errors that **are** raised:

- `ValueError` for a symbol that is not in the instrument list, when no underlying future exists,
  for an exchange without a known close time (and no `expiry_time`), for conflicting
  arguments, or for an `option_price` / `underlying_price` / `spot_price` that is not a number
  (`True`, an integer too large for a float); argument errors are raised before any request.
- `ValueError` for a `rate` or `dividend_yield` so extreme (for example `-1e6`) that a discount
  or carry factor such as `exp(-rate * years)` is too large for a float; this is found while
  computing, after the quotes.
- `ResponseParseError` when the cached instrument list is damaged (not UTF-8 or CSV text):
  delete the file and call again.
- `APIError` when TradeSmart answers a `GetQuotes` call with `Not_Ok` (before expiry), plus the usual
  `AuthenticationError`, `RateLimitError` and network errors.

## Option chain snapshot: `option_chain_snapshot()`

*Illustrative — symbol and strike are placeholders:*

```python
snap = client.option_chain_snapshot("NFO", "YOUR_NFO_SYMBOL", strike_price=24000, count=10)

for row in snap.rows:  # sorted by strike, then CE before PE
    print(row.strike, row.option_type, row.tradingsymbol, row.last_price, row.open_interest)

print("PCR (open interest):", snap.pcr())  # or snap.pcr(by="volume")
pain = snap.max_pain()  # None when no open interest is known
if pain is not None:
    print("Max pain strike:", pain.strike)
if snap.missing:
    print("No usable quote for:", ", ".join(snap.missing))
```

It calls `option_chain()` (`count` strikes either side of `strike_price`, CE and PE) and then
`quote()` for every contract. **That is about 4 × `count` quotes** (the default `count=25` means
about 100 `GetQuotes` calls), and quotes are rate-limited (100 a second, 1,000 a minute), so keep
`count` small and do not poll it in a tight loop. The sync client quotes one contract after
another; the async client keeps at most 10 in flight and cancels the pending ones if a fatal
error occurs.

- `snapshot.rows` has one `ChainRow` per contract (`strike`, `option_type`, `tradingsymbol`,
  `token`, `last_price`, `open_interest`, `volume`, `close`), all prices as `Decimal`.
- `snapshot.missing` lists the contracts that have **no usable quote**: the quote for that
  contract alone failed (`Not_Ok`, not found) or was unreadable, or the contract itself is
  unusable (no token, no strike, or a type other than CE or PE). Each entry is the trading symbol, else the token, else
  `"<strike> <type>"`. The analytics are computed on the rest.
- Errors that are not about one contract are raised, not hidden in `missing`: an expired session
  (`AuthenticationError`), `RateLimitError`, `ServerError`, `ConfigurationError`,
  `RequestValidationError`, network errors (`NetworkError`, `RequestTimeoutError`) and any other
  unexpected error. Only a plain `APIError` (`Not_Ok` for that contract), `NotFoundError` and
  `ResponseParseError` are treated as that one contract's failure. A failure of the chain lookup
  itself is raised too.

### Put-call ratio

`snapshot.pcr(by="oi")` is the total put quantity divided by the total call quantity, by open
interest (`"oi"`, the default) or by traded volume (`"volume"`). It is `None` when the call total
is zero. It covers **only the contracts in the snapshot**, that is the window of strikes you asked
for, not the whole chain; a different `count` gives a different PCR. The same function is
`tradesmart.analytics.pcr(snapshot, by=...)`.

### Max pain

`snapshot.max_pain()` returns the strike (candidate strikes are those in the snapshot) at which
the total payout to option buyers would be lowest, with the payout at each candidate in
`.payouts`; ties go to the lowest strike. It returns a `MaxPain` (fields `strike` and `payouts`)
or `None`; `None` only when every open-interest value is missing. If all open interest is zero
(for example before the market opens) every strike ties and the lowest strike is returned: that
result is meaningless, so check that open interest is non-zero first.
**Outside the window it is approximate:** open interest on strikes you did not fetch is not
counted, so widen `count` until the result stops changing before you rely on it. Also available
as `tradesmart.analytics.max_pain(snapshot)`.

### Greeks for a whole chain

`option_greeks()` reads the instrument list and quotes the option and its future on every call, so
for many contracts compute in bulk: take one snapshot, quote the future once, and call
`tradesmart.greeks.greeks_from_price()` for each row. It needs the underlying price, the strike,
the time to expiry in years (calendar time, 365-day years, to the exchange's closing instant) and
the rate; `status` works as described above. Every number may be a `float`, an `int` or a
`Decimal` (the SDK's prices are `Decimal`); the `float()` calls below are optional.

*Illustrative — symbols, tokens and the expiry date are placeholders:*

```python
from datetime import date, datetime, time

from tradesmart import TradeSmart
from tradesmart.greeks import greeks_from_price
from tradesmart.models.base import IST

EXPIRY = date(2026, 11, 26)  # the chain's expiry date
CLOSE = datetime.combine(EXPIRY, time(15, 30), tzinfo=IST)  # NFO close; see the table above

with TradeSmart.from_env() as client:
    snap = client.option_chain_snapshot("NFO", "YOUR_NFO_SYMBOL", strike_price=24000, count=10)
    future = client.quote("NFO", "YOUR_FUTURE_TOKEN").last_price  # one quote for the whole chain
    years = (CLOSE - datetime.now(IST)).total_seconds() / (365 * 24 * 3600)
    for row in snap.rows:
        if row.last_price is None or future is None:
            continue
        g = greeks_from_price(
            float(row.last_price),
            float(future),  # Black-76 on the future (same expiry as the chain)
            float(row.strike),
            years,
            0.065,  # your rate
            row.option_type,
        )
        print(row.strike, row.option_type, g.status, g.iv, g.delta)
```

Use a future with the same expiry as the chain, or adjust it yourself with the formula above
(`future × math.exp(-(rate - dividend_yield) * (future_years - years))`): `option_greeks()` does
the carry adjustment for you only for the single option it is asked about (and never on MCX,
where the option is on the future itself).

## Buildup: `buildup()`

*Illustrative — `"12345"` stands for a futures or option token from the script master or
`search_instruments()`:*

```python
reading = client.buildup("NFO", "12345")
print(reading.state, reading.price_change, reading.oi_change, reading.basis)
```

`reading.state` is a `tradesmart.Buildup`:

| State | Price | Open interest |
|---|---|---|
| `LONG_BUILDUP` | up | up |
| `SHORT_BUILDUP` | down | up |
| `LONG_UNWINDING` | down | down |
| `SHORT_COVERING` (also called short unwinding) | up | down |
| `NEUTRAL` | unchanged, or a change is missing (or not a finite number) | |

**Two sources, two baselines** (the reading's `basis` says which one was used):

- **From the stream (recommended).** `tradesmart.analytics.buildup_from_snapshot(...)` compares
  today's last price with the **previous close** and today's open interest with the **previous
  day's open interest** (`poi`). Both sides use the previous-day baseline, so the reading is
  consistent. Use it when you already run a stream, and for anything chain-wide.
- **REST `buildup()`.** The price change is the last price minus the **previous close**, but
  TradeSmart's REST API has no previous-day open interest, so the open-interest change is the
  sum of today's candle changes: **since today's open** (`basis="since_open"`). The two sides use
  different baselines, so it can disagree with the stream reading, for example after an
  overnight gap that moved the price but where the day's OI change is small or opposite.

*Illustrative — subscribe, then classify from the latest merged values (`stream.snapshot(key)` is
`None` until the first tick arrives):*

```python
from tradesmart import TradeSmartStream
from tradesmart.analytics import buildup_from_snapshot

with TradeSmartStream.from_env() as stream:
    stream.subscribe_touchline(["NFO|12345"])
    # ... wait for ticks, then:
    tick = stream.snapshot("NFO|12345")
    if tick is not None:
        print(buildup_from_snapshot(tick).state)
```

Buildup for a whole chain needs one stream subscription per contract (a REST loop would need
several calls per contract and would hit the rate limits), then
`buildup_from_snapshot()` on each contract's latest values.

## Related

- [Market data](market-data.md): quotes, candles and the plain `option_chain()`.
- [Streaming](streaming.md): subscriptions and snapshots.
- [Troubleshooting](troubleshooting.md): error messages.
