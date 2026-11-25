# OCO orders

An **OCO (one cancels the other)** order is a multi-day exit held on TradeSmart's server: you give
it a **target** and a **stop-loss**, each with its own trigger value and its own order. When the
last price reaches one trigger, the server sends that side's order to the exchange and cancels
the other side. Nothing reaches the exchange before then. Bracket orders, by contrast, are
intraday only; an OCO stays until one side triggers, you cancel it, or its validity ends.

Every code block on this page is *illustrative*: it uses real SDK calls with placeholder values,
and **running one that places, modifies or cancels an OCO changes your real account**. `client` is
a `TradeSmart` (or, with `await`, an `AsyncTradeSmart`). For orders in general (tags, statuses,
never-resend rules) read [Orders](orders.md) first, and for the alerts an OCO is built on, see
[GTT orders](gtt.md).

> **A trigger that is already crossed when you place or modify the OCO may fire at once** and
> place a real order. The SDK does not compare your triggers with the market. Read a fresh
> `client.quote(...)` first and keep the last price between the two triggers.

## The three OCO calls

| Call | What it does | Returns |
|---|---|---|
| `client.place_oco(...)` | New OCO (`PlaceOCOOrder`) | the alert id (`al_id`) |
| `client.modify_oco(alert_id, ...)` | Change a pending OCO (`ModifyOCOOrder`); everything is sent again | the alert id the server acknowledged |
| `client.cancel_oco(alert_id)` | Cancel a pending OCO (`CancelOCOOrder`) | the alert id the server acknowledged |

The same methods, with the same parameters, are on `AsyncTradeSmart` (`await` them). Like every
order call they are **never retried**: after `OrderStatusUnknownError`, read `pending_gtts()` and
find the OCO (by its alert id, or by comparing its symbol, quantity and triggers with what you sent: the
`tag` may not be listed on OCO rows) before sending anything again.

## Stop-loss and target, and the side rule

`stop_loss` and `target` are an `OcoLeg` each: the `trigger_value` the last price must reach, and
the order sent when it does (`order_type`, `LIMIT` by default, and `price`). A stop order type
(`SL` / `SL_M`) in a leg also takes that order's own `trigger_price`, which is not the OCO trigger
value.

The side rule: for a **BUY** OCO the stop-loss trigger must be **above** the target trigger; for
a **SELL** OCO it must be **below** it. The two are never equal. The SDK refuses a call that
breaks the rule before sending anything (`RequestValidationError`).

*Illustrative: exit a long position of 10 shares, selling at 440 (target) or 360 (stop-loss),
places a real OCO:*

```python
from tradesmart import OcoLeg

alert_id = client.place_oco(
    exchange="NSE",
    tradingsymbol="ITC-EQ",
    transaction_type="SELL",
    quantity=10,
    product="CNC",
    target=OcoLeg(trigger_value="440", price="439.50"),
    stop_loss=OcoLeg(trigger_value="360", price="359.50"),
    tag="exit-itc-01",
)
```

An OCO is offered on NSE, NFO, BSE, BFO and MCX, with the products `CNC`, `NRML` or `CO` (not
`MIS`, `BO` or `MTF`); anything else is refused before sending.

`validity` (the retention of the orders, default `DAY`) and `alert_validity` (`GTT`, the default,
keeps the OCO until one side triggers or you cancel it, otherwise at most 365 days or the
contract's expiry; `DAY` ends it with the trading day) work as for GTTs.

**Slots.** On the wire the two triggers are the variables `x` and `y`. The SDK sends the
stop-loss as `x` and the target as `y`, as TradeSmart's web terminal does, and the OCO type
(`OCO_BUY` or `OCO_SELL`) is chosen from `transaction_type`. You never set them.

**MARKET legs.** A leg may use `MKT` (or `SL-M`) as its order type, but TradeSmart may refuse market
orders from the API: a refusal raises `OrderRejectedError` as for any order. LIMIT legs are the
safe choice.

## Modify and cancel

`modify_oco` sends the whole OCO again, the unchanged parts too, and `validity` and
`alert_validity` are required (no defaults), so nothing changes by accident. A modify that moves a
trigger past the last price may fire at once.

*Illustrative: move the stop-loss of a pending OCO, then cancel it:*

```python
from tradesmart import OcoLeg

client.modify_oco(
    alert_id,
    exchange="NSE",
    tradingsymbol="ITC-EQ",
    transaction_type="SELL",
    quantity=10,
    product="CNC",
    target=OcoLeg(trigger_value="440", price="439.50"),
    stop_loss=OcoLeg(trigger_value="370", price="369.50"),
    tag="exit-itc-01",
    validity="DAY",
    alert_validity="GTT",
)
client.cancel_oco(alert_id)
```

`cancel_oco` and `modify_oco` of an alert that no longer exists (observed on TradeSmart's test
environment for an already cancelled OCO) answer `Invalid Oi`; the SDK raises `OrderRejectedError`
for it, decided by that answer alone. The answer for an OCO that has fired has not been observed
yet. **Use these three
calls for OCOs, not `modify_gtt` or `cancel_gtt`**, which are for plain GTTs.

## Reading OCOs back

`pending_gtts()` lists OCOs that have not triggered, next to the plain GTTs, and `triggered_gtts()`
the ones that are no longer pending (on the test environment, cancelled OCOs; a fired OCO has not
been observed yet). A row tells them apart with `Gtt.is_oco`, and `Gtt.stop_loss` and `Gtt.target`
give each side as an `OcoSide`: its order `leg` (`OcoLegRow`: `transaction_type`, `order_type`,
`quantity`, `price`, `product`, `tag`, ...) and its `trigger_value`. They work for OCOs placed from
any front end, not only through the SDK. They are `None` when the row is ambiguous (for example
equal triggers, or a repeated `x` / `y` slot): the SDK never guesses a side.

*Illustrative:*

```python
for row in client.pending_gtts():
    if row.is_oco and row.stop_loss and row.target:
        print(row.alert_id, "stop-loss", row.stop_loss.trigger_value, "target", row.target.trigger_value)
```

A cancelled OCO leaves `pending_gtts()` and, on TradeSmart's test environment, is listed by
`triggered_gtts()` with `stat` `CANCELLED`: check `stat` before treating a row as fired. What a
fired OCO looks like has **not been observed yet** (a question open with TradeSmart): whether its
order carries `Order.parent_id` equal to the alert id, as a fired GTT's does, is not known, so do
not rely on it for OCOs.

## Test offline

`tradesmart.testing.OfflineBroker` supports the OCO calls: `place_oco`, `modify_oco` and
`cancel_oco` change `pending_gtts()`, and a cancelled OCO is listed by `triggered_gtts()` as
`CANCELLED`. **The offline broker does not simulate OCO triggering**: it watches no prices, and
`broker.trigger_gtt()` on an OCO raises `ValueError`. See
[Testing your strategy offline](testing.md) and the runnable
[`examples/oco_exit.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/oco_exit.py).
