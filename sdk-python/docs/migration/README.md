# Migrating to the TradeSmart Python SDK

These guides are for developers who already run a strategy on another Indian broker's Python
SDK and want to move it to `tradesmart`, TradeSmart's official Python SDK for the TradeSmart
API V2. Each guide maps the SDK you know to `tradesmart` in tables: login, client set-up, every
order, portfolio and market-data method, parameter names, constant values, response fields and
stream callbacks. It also lists what has no TradeSmart equivalent and walks through porting a
small strategy.

| You are coming from | Guide |
|---|---|
| Zerodha Kite Connect (`pykiteconnect` v4, `KiteConnect` / `KiteTicker`) | [zerodha-kite.md](zerodha-kite.md) |
| Dhan (`dhanhq` 2.3, `DhanContext` / `dhanhq` / `MarketFeed` / `OrderUpdate`) | [dhan.md](dhan.md) |

New to the SDK? Start with [Your first order in 10 minutes](../getting-started.md). The
[SDK reference](../reference.md) lists every method and error; read its
"Placing orders" section before you send anything that changes state.

## Read this first

- **GTT yes; no OCO GTT or iceberg methods.** Single good-till-triggered (GTT, "forever") orders
  are `place_gtt`, `modify_gtt`, `cancel_gtt`, `pending_gtts()` and `triggered_gtts()` (see
  [GTT orders](../gtt.md); a GTT whose condition is already true when placed fires at once).
  One-cancels-other (OCO) GTTs (not documented by TradeSmart yet) and iceberg orders have no
  methods: keep that logic in your own code, or send a large order with `place_sliced_order`.
- **No market orders through the API.** TradeSmart refuses `MARKET` orders placed through the
  API (`OrderRejectedError`, "ALGO_CHK: MKT Order type not allowed for API order"). Replace them
  with a `LIMIT` order at a price that trades now, rounded with `round_to_tick()`; each guide
  shows how.
- **Symbols are different.** TradeSmart identifies an instrument by its **trading symbol**
  (for NSE equities with a series suffix, e.g. `INFY-EQ`) plus a numeric **token** (sent as a
  string, e.g. `"22"`) and the exchange (`NSE`, `BSE`, `NFO`, `BFO`, `CDS`, `BCD`, `MCX`). Look
  them up with:
  - `client.search_instruments("INFY")` → `ScripMatch` objects with `exchange`,
    `tradingsymbol`, `token`, `lot_size`, `tick_size`;
  - `client.instruments("NSE")` → every row of TradeSmart's script master for one exchange
    (a list of `dict[str, str]`; the file is downloaded again only after TradeSmart's daily
    refresh, `ClientConfig.script_master_refresh_time`, 07:00 IST by default). The file's
    column layout is not documented, so inspect `rows[0].keys()` once and filter with
    `tradesmart.instruments.find(rows, **{"<column>": "<value>"})`.

  Build a mapping from your old identifiers to `(exchange, tradingsymbol, token)` once per day,
  before the market opens, rather than looking symbols up inside your trading loop.
- **Errors are exceptions, acknowledgements are not fills.** Every `tradesmart` call either
  returns a typed result or raises a subclass of `TradeSmartError`. `place_order()` returns the
  order id as soon as TradeSmart's order system *accepts* the request; confirm the outcome with
  `orders()`, `order_history()` or the order stream. If a state-changing call raises
  `OrderStatusUnknownError`, the order may exist: **reconcile with `orders()`, never resend
  blindly.**

Product names used in these guides are trademarks of their respective owners and are used
only to identify those products.
