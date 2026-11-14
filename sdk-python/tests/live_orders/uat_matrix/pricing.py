"""Prices of the UAT matrix orders (inventory §3.1 "Pricing rules"), from a fresh quote only.

**Every price comes from a quote taken just before placing**: ``fresh_quote(client, contract)``
returns a ``PriceSnapshot`` and every pricing function takes that snapshot, never a
``Contract`` (a contract's ``resolved_*`` values are the resolution-time snapshot, stale on a
resume later the same day; a "far" order priced from a stale premium could be marketable and
fill). Passing anything but a ``PriceSnapshot`` raises ``TypeError``.

- Non-filling BUY: last price - 10 % floored to the tick, never below ``lc`` (rounded up), and
  refused unless at least 1 % below the last price (``far_buy``).
- Non-filling SELL: last price + 10 % ceiled to the tick, never above ``uc`` (rounded down), and
  refused unless at least 1 % above the last price (``far_sell``).
- Stop BUY above the market (``stop_buy``), stop SELL below it (``stop_sell``).
- Fill: exactly the last price on the tick (UAT matching rule, ``at_last``).

A refusal is ``safety.Refusal`` (the case skips). The BUY side, the stop BUY and the fill price
reuse the helpers of the existing live suites (``safety.buy_limit_price``,
``lifecycle.stop_buy_prices``, ``lifecycle.at_last_price``); the SELL side mirrors them.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from tests.live_orders import lifecycle, safety
from tests.live_orders.safety import Refusal, ceil_to_tick, floor_to_tick
from tests.live_orders.uat_matrix.contracts import DATA_ERRORS, SESSION_ERRORS, Contract, read_quote
from tests.live_orders.uat_matrix.quote_pacer import QuotePacer
from tradesmart.models.market import Quote

ABOVE_MARKET = Decimal("1.1")  # SELL at last price + 10 %
WELL_ABOVE = Decimal("1.01")  # refuse a SELL price less than 1 % above the last price
OPTION_ABOVE = Decimal("1.04")  # option SELL at last price + 4 % (the exchange LPP band)
OPTION_BELOW = Decimal("0.96")  # option BUY at last price - 4 %
STOP_SELL_TRIGGER = Decimal("0.97")  # a stop SELL triggers 3 % below the last price ...
STOP_SELL_LIMIT = Decimal("0.96")  # ... with its limit 4 % below it
OPTION_STOP_SELL_TRIGGER = Decimal("0.90")  # options: 10 % and 12 % below
OPTION_STOP_SELL_LIMIT = Decimal("0.88")


class QuoteReader(Protocol):
    def quote(self, exchange: str, token: str) -> Quote: ...


@dataclass(frozen=True)
class PriceSnapshot:
    """Last price, tick and circuits from a quote taken just before placing."""

    tradingsymbol: str
    last_price: Decimal
    tick_size: Decimal
    lower_circuit: Decimal | None = None
    upper_circuit: Decimal | None = None
    # UAT only: the quote's ``ls`` disagreed with the master lot, which is used (recorded).
    lot_mismatch: str | None = None

    def __post_init__(self) -> None:
        if not self.last_price > 0 or not self.tick_size > 0:
            raise ValueError(f"{self.tradingsymbol}: a price snapshot needs a last price and tick")


def _positive(value: Decimal | None) -> Decimal | None:
    return value if value is not None and value > 0 else None


def fresh_quote(
    client: QuoteReader,
    contract: Contract,
    *,
    uat: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    pacer: QuotePacer | None = None,
) -> PriceSnapshot:
    """The price snapshot of ``contract`` from a ``quote()`` taken now (one read call; a read
    refused for rate, ``exceeds Limit``, is read again after 0.5 s, 1 s and 2 s).

    Refuses (``safety.Refusal``) without a quote, a last price above 0, or with a lot size other
    than the contract's; on UAT (``uat``) an F&O / MCX lot that the quote contradicts is the
    contract's (the master lot), noted in ``lot_mismatch``. The tick is the contract's (live UAT
    run, 13 Nov 2026: a NIFTY future quoted ``ti`` 0.05 while its exchange tick is 0.10), or a
    multiple of both when the quote reports another one (``common_tick``). ``pacer``: the
    session's UAT quote pacer. An expired session propagates.
    """
    name = contract.tradingsymbol
    try:
        quote = read_quote(client, contract.exchange, contract.token, sleep=sleep, pacer=pacer)
    except SESSION_ERRORS:
        raise
    except DATA_ERRORS as exc:
        raise Refusal(f"{name}: no fresh quote ({type(exc).__name__}: {exc}); no order") from exc
    last = _positive(quote.last_price)
    if last is None:
        raise Refusal(f"{name}: no last price in a fresh quote (market closed or no trades)")
    lot_mismatch = None
    if quote.lot_size is not None and quote.lot_size > 0 and quote.lot_size != contract.lot_size:
        if not (uat and contract.kind != "eq"):
            raise Refusal(
                f"{name}: lot size {quote.lot_size} in a fresh quote, {contract.lot_size} at "
                "resolution; refusing"
            )
        lot_mismatch = (
            f"quote ls {quote.lot_size}, script-master lot {contract.lot_size} (UAT: the master "
            "lot is used)"
        )
    return PriceSnapshot(
        tradingsymbol=name,
        last_price=last,
        tick_size=common_tick(contract.tick_size, _positive(quote.tick_size)),
        lower_circuit=_positive(quote.lc),
        upper_circuit=_positive(quote.uc),
        lot_mismatch=lot_mismatch,
    )


def common_tick(contract_tick: Decimal, quote_tick: Decimal | None) -> Decimal:
    """The contract's tick, or the least common multiple of it and the quote's tick when they
    differ (a price on it is a multiple of both)."""
    if quote_tick is None or quote_tick == contract_tick:
        return contract_tick
    scale = max(-int(t.as_tuple().exponent) for t in (contract_tick, quote_tick))
    a, b = (int(t.scaleb(scale)) for t in (contract_tick, quote_tick))
    return Decimal(math.lcm(a, b)).scaleb(-scale)


def _snapshot(value: Any) -> PriceSnapshot:
    if not isinstance(value, PriceSnapshot):
        raise TypeError(
            f"prices come only from fresh_quote(client, contract), not {type(value).__name__}"
        )
    return value


def far_buy(snapshot: PriceSnapshot, *, option: bool = False) -> Decimal:
    """A BUY limit that rests: 10 % below the last price (an option 4 %, the exchange's LPP band
    refuses a far option price), within ``lc``, at least 1 % below."""
    s = _snapshot(snapshot)
    ratio = OPTION_BELOW if option else safety.BELOW_MARKET
    return safety.buy_limit_price(s.last_price, s.tick_size, s.lower_circuit, ratio)


def far_sell(snapshot: PriceSnapshot, *, option: bool = False) -> Decimal:
    """A SELL limit that rests: 10 % above the last price (an option 4 %), within ``uc``, at
    least 1 % above."""
    s = _snapshot(snapshot)
    last, tick, uc = s.last_price, s.tick_size, s.upper_circuit
    price = ceil_to_tick(last * (OPTION_ABOVE if option else ABOVE_MARKET), tick)
    if uc is not None and price > uc:
        price = floor_to_tick(uc, tick)
    if price < last * WELL_ABOVE:
        raise Refusal(
            f"price band too narrow: the highest allowed price {price} is not well above the "
            f"last price {last}"
        )
    return price


def stop_buy(snapshot: PriceSnapshot, *, option: bool = False) -> tuple[Decimal, Decimal]:
    """``(trigger, limit)`` of a stop BUY that is not hit: 3 % / 4 % above the last price
    (options 10 % / 12 %), the limit within ``uc`` and never below the trigger."""
    s = _snapshot(snapshot)
    ratios = (
        (lifecycle.OPTION_STOP_TRIGGER, lifecycle.OPTION_STOP_LIMIT)
        if option
        else (lifecycle.STOP_TRIGGER, lifecycle.STOP_LIMIT)
    )
    return lifecycle.stop_buy_prices(s.last_price, s.tick_size, s.upper_circuit, ratios=ratios)


def stop_sell(snapshot: PriceSnapshot, *, option: bool = False) -> tuple[Decimal, Decimal]:
    """``(trigger, limit)`` of a stop SELL that is not hit: 3 % / 4 % below the last price
    (options 10 % / 12 %), both floored to the tick; the limit never below ``lc`` (rounded up)
    and never above the trigger, so a trigger below the circuit is refused."""
    s = _snapshot(snapshot)
    last, tick, lc = s.last_price, s.tick_size, s.lower_circuit
    if option:
        trigger_ratio, limit_ratio = OPTION_STOP_SELL_TRIGGER, OPTION_STOP_SELL_LIMIT
    else:
        trigger_ratio, limit_ratio = STOP_SELL_TRIGGER, STOP_SELL_LIMIT
    trigger = floor_to_tick(last * trigger_ratio, tick)
    price = floor_to_tick(last * limit_ratio, tick)
    if lc is not None and price < lc:
        price = ceil_to_tick(lc, tick)
    if price > trigger or price <= 0:
        raise Refusal(
            f"no stop price within the lower circuit {lc} (last price {last}, trigger {trigger})"
        )
    return trigger, price


def at_last(snapshot: PriceSnapshot, side: str) -> Decimal:
    """The fill price: the last price on the tick, rounded toward a fill (up for a BUY, down
    for a SELL), within the circuits."""
    s = _snapshot(snapshot)
    return lifecycle.at_last_price(
        s.last_price,
        s.tick_size,
        side,
        lower_circuit=s.lower_circuit,
        upper_circuit=s.upper_circuit,
    )
