# GTT orders

A **GTT (good till triggered)** order is held on TradeSmart's server as an *alert*: you choose
what to watch (the last traded price, the % change, open interest, ...), a direction (greater
than or less than) and a **trigger value**. When the live value crosses the trigger, the server
sends the order you described to the exchange. Nothing reaches the exchange before then.

Every code block on this page is *illustrative*: it uses real SDK calls with placeholder values,
and **running one that places, modifies or cancels a GTT changes your real account**. `client` is
a `TradeSmart` (or, with `await`, an `AsyncTradeSmart`). For orders in general (tags, statuses,
never-resend rules) read [Orders](orders.md) first.

> **A GTT whose condition is already true when you place it fires at once** and places a real
> order immediately. The SDK does not compare your trigger with the market. Read a fresh
> `client.quote(...)` first and make sure the condition is not already met (for `LTP_A_O`: the
> last price is below the trigger; for `LTP_B_O`: above it). The same holds for a modify that
> moves the trigger past the current value.

## The six GTT calls

| Call | What it does | Returns |
|---|---|---|
| `client.place_gtt(...)` | New GTT (`PlaceGTTOrder`) | the alert id (`al_id`) |
| `client.modify_gtt(alert_id, ...)` | Change the trigger or the order of a pending GTT (`ModifyGTTOrder`); every field is sent again | the alert id the server acknowledged |
| `client.cancel_gtt(alert_id)` | Cancel a GTT that has not triggered (`CancelGTTOrder`) | the alert id the server acknowledged |
| `client.pending_gtts()` | GTTs that have not triggered yet (`GetPendingGTTOrder`) | `list[Gtt]` (empty when none) |
| `client.triggered_gtts()` | GTTs that triggered and sent their order (`GetTrgOi`) | `list[TriggeredGtt]` |
| `client.gtt_alert_types()` | Alert families enabled for your account (`GetEnabledGTTs`) | `list[str]`, e.g. `["ATP", "LTP"]` |

The same methods, with the same parameters, are on `AsyncTradeSmart` (`await` them).

## Place a GTT

*Illustrative — places a real GTT (buy 1 ITC share at 380 once the last price falls below 380):*

```python
from tradesmart import GttAlertType, GttValidity

last_price = client.quote("NSE", "1660").last_price  # the instrument's token
if last_price is not None and last_price > 380:  # LTP_B_O at 380 is not already true
    alert_id = client.place_gtt(
        exchange="NSE",
        tradingsymbol="ITC-EQ",
        alert_type=GttAlertType.LTP_BELOW,   # "LTP_B_O": fires when the LTP is less than 380
        trigger_value="380",                 # the GTT condition (wire "d")
        quantity=1,
        transaction_type="BUY",
        product="CNC",                       # CNC, NRML or CO only (no MIS)
        order_type="LIMIT",
        price="380",                         # the order placed when it fires
        tag="dip-itc-01",                    # required: find the GTT again by this tag
        alert_validity=GttValidity.GTT,      # until it fires or is cancelled (see below)
    )
```

| Parameter | Wire key | What it is |
|---|---|---|
| `alert_type` | `ai_t` | What to watch and the direction: a `GttAlertType` member or its code (table below) |
| `trigger_value` | `d` | The GTT condition: a finite number (Decimal, int, float or numeric text). Not `trigger_price`, which is the stop-loss trigger of an `SL-LMT` / `SL-MKT` order (`trgprc`) |
| `alert_validity` | `validity` | `GttValidity.GTT` (default on `place_gtt`) or `GttValidity.DAY` |
| `validity` | `ret` | The retention of the order the GTT places: `DAY` (default on `place_gtt`), `EOS` or `IOC` |
| `tag` | `remarks` | **Required**, a non-blank string; your key to the GTT after an unknown outcome |
| `exchange`, `tradingsymbol`, `quantity`, `transaction_type`, `product`, `order_type`, `price`, `trigger_price`, `disclosed_quantity` | as `place_order` | The order placed when the GTT fires. Exchanges NSE, NFO, BSE, BFO and MCX; products CNC, NRML and CO. `quantity` is in the exchange's trade units, a whole multiple of the lot size, on MCX too (CRUDEOIL lot 100: 2 lots = 200; GOLD lot 1: 1 lot = 1) |

The order fields are checked exactly as in `place_order` (prices, the `MARKET` price of 0, the
trigger price, the disclosed quantity). A refused value raises `RequestValidationError` and
nothing is sent: an unknown alert type, an alert validity other than `DAY` / `GTT`, an exchange
other than the five above, `MIS` (or `BO` / `MTF`), a `trigger_value` that is not a finite number
(a boolean is refused), or a blank `tag`.

### Alert types

| Watch | Greater than the trigger | Less than the trigger |
|---|---|---|
| Last traded price | `LTP_A_O` (`GttAlertType.LTP_ABOVE`) | `LTP_B_O` (`GttAlertType.LTP_BELOW`) |
| % change | `CH_PER_A_O` (`CHANGE_PERCENT_ABOVE`) | `CH_PER_B_O` (`CHANGE_PERCENT_BELOW`) |
| Average traded price | `ATP_A_O` (`ATP_ABOVE`) | `ATP_B_O` (`ATP_BELOW`) |
| Open interest | `OI_A_O` (`OI_ABOVE`) | `OI_B_O` (`OI_BELOW`) |
| Total open interest | `TOI_A_O` (`TOI_ABOVE`) | `TOI_B_O` (`TOI_BELOW`) |
| 52-week high | `LTP_A_52HIGH_O` (`HIGH_52W_ABOVE`) | `LTP_B_52HIGH_O` (`HIGH_52W_BELOW`) |
| Volume | `VOLUME_A_O` (`VOLUME_ABOVE`) | — |

`client.gtt_alert_types()` lists the families enabled for your account (e.g. `["ATP", "LTP"]`).
A % change can be negative: the SDK puts no sign rule on `trigger_value`.

### How long a GTT lasts

- `alert_validity=GttValidity.GTT`: until it fires or you cancel it, **at most 365 days or the
  contract's expiry, whichever is earlier**.
- `alert_validity=GttValidity.DAY`: until the end of the current trading day.

## Modify and cancel

`modify_gtt` sends **every field again**, also the ones you do not change, and **`validity` and
`alert_validity` have no defaults** there: pass both, so that moving a trigger never silently
turns a `DAY` GTT into `GTT` or an `IOC` order into `DAY`. The alert type cannot change (send the
original). Read the current values from `pending_gtts()`; when a row lacks one of them (a
field the server left out, e.g. `validity`), do not guess: pass the value you placed the GTT with.

*Illustrative — moves a real GTT's trigger to 370:*

```python
gtt = next(g for g in client.pending_gtts() if g.tag == "dip-itc-01")
if gtt.validity is None or gtt.alert_validity is None:
    raise RuntimeError("row without a validity: pass the values the GTT was placed with")
client.modify_gtt(
    gtt.alert_id,
    exchange=gtt.exchange, tradingsymbol=gtt.tradingsymbol, alert_type=gtt.alert_type,
    trigger_value="370", quantity=gtt.quantity, transaction_type=gtt.transaction_type,
    product=gtt.product, order_type=gtt.order_type, price=gtt.price,
    trigger_price=gtt.trigger_price,  # required again for an SL-LMT / SL-MKT order
    tag=gtt.tag, validity=gtt.validity, alert_validity=gtt.alert_validity,
)
```

Whether the server keeps an existing disclosed quantity when a modify leaves it out is not
documented: pass `disclosed_quantity` again to keep it.

`client.cancel_gtt(alert_id)` cancels a GTT that has not triggered. A GTT that already fired
cannot be cancelled: cancel its order instead (below).

`modify_gtt` and `cancel_gtt` return the alert id exactly as the server acknowledged it. The
documentation does not promise it equals the one you sent; when it differs, the SDK logs one
DEBUG line on the `tradesmart` logger (without any ids).

## Reading GTTs

`pending_gtts()` returns `Gtt` rows: `alert_id`, `alert_type`, `trigger_value`, `alert_validity`
(the GTT's own `DAY` / `GTT`), `validity` (the retention of its order, wire `ret`), `tag`, and
the order fields (`exchange`, `tradingsymbol`, `token`, `transaction_type`, `order_type`,
`product`, `quantity`, `price`, `trigger_price`, ...). `triggered_gtts()` returns
`TriggeredGtt` rows: the same fields plus `norentm`, `sms_flag`, `email_flag` and `push_flag`.
Both return an empty list when there are none. TradeSmart's test environment also lists
**cancelled** GTTs in `triggered_gtts()`, with `stat` `CANCELLED`: check `stat` before treating a
row as fired.

When a GTT fires, it moves from `pending_gtts()` to `triggered_gtts()`, and **its order is in the
order book** with `Order.parent_id` equal to the alert id (TradeSmart's `add_ord_id`). Follow
that order as any other: [Order status lifecycle](orders.md#order-status-lifecycle).

*Illustrative — the orders your GTTs placed:*

```python
fired = {
    g.alert_id
    for g in client.triggered_gtts()
    if (g.stat or "").casefold() != "cancelled"  # the test environment lists cancelled GTTs
}
gtt_orders = [o for o in client.orders() if o.parent_id in fired]
```

## Recovery: find a GTT by its tag, never resend blindly

The GTT writes are order calls: the SDK **never retries** `PlaceGTTOrder`, `ModifyGTTOrder` or
`CancelGTTOrder`. Success is only the documented answer (`Oi created`, `Oi replaced`,
`Oi delete success`, each with a non-blank alert id and an `emsg` that is missing, `null` or blank); a definite error
raises `OrderRejectedError` (or `AuthenticationError` / `RateLimitError` / `NotFoundError`).
When the request may have reached TradeSmart but no clear answer came back, the call raises
**`OrderStatusUnknownError`**, and the WARNING on the `tradesmart.orders` logger says to check
`pending_gtts()`. The GTT may exist.

**Letter case.** The words are compared ignoring letter case only: TradeSmart's test environment
answers `OI created`, which is accepted. Nothing else is relaxed: extra or missing spaces, or a
different word, is `OrderStatusUnknownError`. The test environment answers a cancel with
`OI deleted`, a different word from the documented `Oi delete success`, so a cancel there raises
`OrderStatusUnknownError`: confirm with `pending_gtts()` that the GTT is gone.

*Illustrative — places a real GTT:*

```python
import secrets
import time

from tradesmart import OrderStatusUnknownError

tag = "dip-" + secrets.token_hex(3)  # unique per GTT
try:
    alert_id = client.place_gtt(
        exchange="NSE", tradingsymbol="ITC-EQ", alert_type="LTP_B_O", trigger_value="380",
        quantity=1, transaction_type="BUY", product="CNC", order_type="LIMIT", price="380",
        tag=tag,
    )
except OrderStatusUnknownError:
    alert_id = None
    for _ in range(5):  # the list can lag a little
        found = [g.alert_id for g in client.pending_gtts() if g.tag == tag]
        if found:
            alert_id = found[0]  # it exists: do NOT place it again
            break
        time.sleep(2)
    if alert_id is None:
        # Not pending. It may have fired at once: check triggered_gtts() and orders() for the
        # tag before deciding anything; never resend automatically.
        print("GTT outcome unknown; tag", tag, "not found yet")
```

The same applies to `modify_gtt` and `cancel_gtt`: after an unknown outcome, read
`pending_gtts()` (the trigger you sent, or the GTT gone) before sending anything again.

Each GTT write writes one INFO line on `tradesmart.orders`, with the alert id under `al_id=`:
`PlaceGTTOrder accepted al_id=26111200000010 in 40 ms` ([Order logging](orders.md#order-logging)).
Trigger values, prices, symbols and tags are never logged.

## Test offline

`tradesmart.testing.OfflineBroker` keeps GTTs too: `place_gtt`, `modify_gtt`, `cancel_gtt`,
`pending_gtts`, `triggered_gtts` and `gtt_alert_types` work against it, and a GTT fires only when
your test calls `broker.trigger_gtt(alert_id)` (its order is placed with `parent_id` set). See
[Testing your strategy offline](testing.md) and the runnable
[`examples/gtt_alert.py`](https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/sdk-python/examples/gtt_alert.py)
(place, list, modify, fire; an unknown outcome found by its tag; a GTT refused because it would
fire at once).

## Not available

- **OCO (one-cancels-other) GTTs**: TradeSmart supports them, but their request fields and
  replies are not documented yet, so the SDK has no method for them.
- **Iceberg orders**: the SDK does not offer iceberg orders; `request()` can reach them (never
  retried). To send a large order in pieces, use [`place_sliced_order`](orders.md#sliced-orders).
