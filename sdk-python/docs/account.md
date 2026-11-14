# Account and portfolio

Reads of your own account: who is logged in, funds and margins, positions, holdings and today's
trades. They are reads: nothing is placed, and they are retried on network errors like every
other read. Every value arrives as text from TradeSmart and is parsed for you: prices and money
are `Decimal`, quantities `int`, ids `str`. Fields the SDK does not model stay reachable as
attributes and in `.raw`.

| Call | TradeSmart endpoint | Returns |
|---|---|---|
| `client.profile()` | `UserDetails` | `UserDetails` |
| `client.margins()` | `Limits` | `Limits` |
| `client.positions()` | `PositionBook` | `list[Position]` (empty when there are none) |
| `client.holdings(product="CNC")` | `Holdings` | `list[Holding]` (empty when there are none) |
| `client.trades()` | `TradeBook` | `list[Trade]` (today's fills; empty when there are none) |

*Illustrative:*

```python
from tradesmart import TradeSmart

with TradeSmart.from_env() as client:
    me = client.profile()
    print(me.user_id, me.uname, me.exarr)  # client id, name, enabled exchanges

    funds = client.margins()
    print(funds.cash, funds.marginused, funds.collateral, funds.realized_pnl)

    for p in client.positions():
        print(p.exchange, p.tradingsymbol, p.product, p.net_quantity, p.netavgprc, p.last_price)

    for h in client.holdings():
        print([i.tradingsymbol for i in h.instruments or []], h.holdqty, h.upldprc)

    for t in client.trades():
        print(t.order_id, t.tradingsymbol, t.transaction_type, t.flqty, t.flprc, t.fltm)
```

## Profile

`profile()` returns `UserDetails`: `user_id` and `account_id` (your client id), `uname`, `email`,
`brkname`, the exchanges (`exarr`), order types (`orarr`) and products (`prarr`) enabled for the
account.

## Funds and margins

`margins()` returns one `Limits` record: `cash`, `payin`, `payout`, `collateral`, `marginused`,
`realized_pnl` (wire `rpnl`), `brokerage`, the span and exposure parts (`span`, `expo`,
`premium`) and the other limit fields TradeSmart sends. Fields TradeSmart leaves out are `None`.

To see what one order would block before you send it, use `client.order_margin(...)` (see
[Orders](orders.md#margin-before-you-order)).

## Positions

`positions()` returns one flat `Position` per instrument and product (no separate day and net
lists): `exchange`, `tradingsymbol`, `token`, `product`, `net_quantity` (wire `netqty`),
`netavgprc`, `last_price`, `realized_pnl`, the day and carry-forward quantities and averages
(`daybuyqty` / `daysellqty`, `daybuyavgprc` / `daysellavgprc`, `cfbuyqty` / `cfsellqty`), the
totals (`totbuyamt` / `totsellamt`), `bep` (break-even price), `lot_size`,
`mult` and `prcftr` (the MCX price factor, see [Orders](orders.md#place)).

## Holdings

`holdings(product)` (default `CNC`) returns `Holding` records: the quantities (`holdqty`, `dpqty`,
`btstqty`, `usedqty`, the collateral quantities), `upldprc` (the uploaded average price) and
`instruments`, the exchange listings of the same security (`exchange`, `tradingsymbol`,
`token`, `last_price`). TradeSmart publishes no example of a non-empty reply: a reply that is not
a JSON array raises `ResponseParseError` (please report it with `exc.body`, personal data removed).

## Trades

`trades()` returns today's fills as `Trade` records: `order_id`, `exchange`, `tradingsymbol`,
`transaction_type`, `product`, the fill (`flid` fill id, `flqty` quantity, `flprc` price, `fltm`
time), `average_price`, `filled_quantity` and your order `tag`. Use it with `orders()` to see
which part of an order traded.

## Malformed fields

For positions, holdings and trades a field that cannot be parsed becomes `None` (with one WARNING
naming the field, never its value) instead of dropping the whole record; the original value stays
in `.raw`. Only an unreadable identity field (a trade's order id) fails the whole record.

After a stream reconnect, call `positions()` again: position updates sent while the stream was
down are not replayed (see [Streaming](streaming.md)).
