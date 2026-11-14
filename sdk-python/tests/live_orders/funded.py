"""Money limits of funded mode (pinned by tests/unit/test_live_orders_funded.py).

Funded mode (``safety.funded_mode``) sends live orders on a small funded production account.
Besides the order caps, every order is held to a money budget: one order's value (price, or the
trigger when higher, times quantity; an option's premium times its quantity) stays within
``MAX_ORDER_VALUE``, and the BUY orders of one run within ``MAX_RUN_VALUE``. A BUY reserves its
value before it is sent; only a BUY that ends with no fill (or is never accepted) gives it back.
Every check fails closed: a value that cannot be judged is refused.

Instruments (``FUNDED_SEGMENTS``): ITC on NSE and BSE and a NIFTY / SENSEX index call option, all
MIS. The option strike is the nearest out-of-the-money CE whose premium times its lot stays
within ``STRIKE_PREMIUM_CEILING``, 90 % of ``MAX_ORDER_VALUE`` (``pick_option_by_premium``).
Stage-1 orders rest far below the market (``far_buy_price``); stage 2 (tiny fills) runs only
when ``stage_two_reason`` gives no reason, and sends a BUY only before 14:30 IST
(``stage_two_time_reason``).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime
from decimal import Decimal
from typing import Any

from tests.live_orders import lifecycle, safety
from tests.live_orders.safety import (
    FILLS_ENV,
    Instrument,
    OrderPlan,
    OrderSession,
    Refusal,
    Segment,
    floor_to_tick,
)
from tradesmart import TradeSmart
from tradesmart.models.base import IST

MAX_ORDER_VALUE = Decimal("2000")  # rupees, one order
MAX_RUN_VALUE = Decimal("5000")  # rupees, all BUY orders of one run
FUNDED_PER_TEST_CAP = 2  # one BUY and the SELL that closes its fill


def order_value(plan: OrderPlan) -> Decimal:
    """``max(price, trigger_price or 0) × quantity``: the most the order can cost."""
    trigger = plan.trigger_price if plan.trigger_price is not None else Decimal("0")
    if not (plan.price.is_finite() and trigger.is_finite()):
        raise Refusal(
            f"funded mode: order value of {plan.tradingsymbol} cannot be judged "
            f"(price {plan.price}, trigger {plan.trigger_price}); refusing"
        )
    return max(plan.price, trigger) * plan.quantity


def _judged(value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise Refusal(f"funded mode: order value {value} cannot be judged; refusing")


@dataclass
class MoneyBudget:
    """Value limits of one funded run: ``per_order`` for each order, ``per_run`` for the
    BUY orders reserved so far (``committed``)."""

    per_order: Decimal = MAX_ORDER_VALUE
    per_run: Decimal = MAX_RUN_VALUE
    committed: Decimal = Decimal("0")

    def check(self, value: Decimal) -> None:
        """Refuse an order worth more than ``per_order`` (or a value that cannot be judged)."""
        _judged(value)
        if value > self.per_order:
            raise Refusal(
                f"funded mode: order value {value} is above the limit of {self.per_order} "
                "per order; refusing"
            )

    def reserve(self, value: Decimal) -> None:
        """``check``, then add ``value`` unless the run's BUY orders would pass ``per_run``."""
        self.check(value)
        if self.committed + value > self.per_run:
            raise Refusal(
                f"funded mode: a BUY of {value} would take this run's BUY orders to "
                f"{self.committed + value}, above the limit of {self.per_run} per run; refusing"
            )
        self.committed += value

    def release(self, value: Decimal) -> None:
        """Give back a reservation of a BUY that ended with no fill (never more than held)."""
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise ValueError(f"cannot release {value}")
        if value > self.committed:
            raise ValueError(f"cannot release {value}: only {self.committed} is reserved")
        self.committed -= value


# -- instruments -------------------------------------------------------------------------
# The UAT instruments, cash and index options only (no futures: one lot is far above the
# order limit), all intraday (MIS: the broker squares off a leftover position at the end of
# the day, a safety net, not the plan).

FUNDED_SEGMENTS: dict[str, Segment] = {
    key: dataclasses.replace(lifecycle.UAT_SEGMENTS[key], product="MIS")
    for key in ("nse", "bse", "nfoopt", "bfoopt")
}
OPTION_IDS = frozenset({"nfoopt", "bfoopt"})

# A stage-1 LIMIT BUY rests this far below the last price: cash -10 %, options -50 %.
STAGE1_RATIO: dict[str, Decimal] = {"cash": Decimal("0.9"), "option": Decimal("0.5")}
# Strikes above the future quoted (nearest first) for a premium. 12 was too few: on 2026-11-12
# (NIFTY ~25,280, lot 65, so premium <= 27.69) the 12 nearest strikes, 50 apart, were all
# dearer. Quotes are cheap (client limit 100/s) and each quoted strike is recorded.
PREMIUM_STRIKE_TRIES = 40
# The strike's premium × lot stays within 90 % of the order limit (1800), so a stage-2 BUY at
# the best offer, a little above the last price quoted here, still fits MAX_ORDER_VALUE.
STRIKE_PREMIUM_CEILING = MAX_ORDER_VALUE * 9 / 10


def _positive(value: Decimal | None) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def far_buy_price(last_price: Decimal, tick: Decimal, ratio: Decimal) -> Decimal:
    """``last_price * ratio`` floored to the tick, at least one tick, and below the last price.

    ``Refusal`` (the test skips) without a usable last price or tick, or when even one tick is
    not below the last price (such a BUY could fill). ``ratio`` must lie strictly between 0 and
    1; anything else is a bug (``ValueError``).
    """
    if not (_positive(ratio) and ratio < 1):
        raise ValueError(f"stage-1 ratio {ratio} must lie between 0 and 1")
    if not _positive(last_price):
        raise Refusal(f"no last price to place a BUY far below the market (got {last_price})")
    if not _positive(tick):
        raise Refusal(f"no tick size to place a BUY far below the market (got {tick})")
    price = max(floor_to_tick(last_price * ratio, tick), tick)
    if price >= last_price:
        raise Refusal(
            f"no price below the last price {last_price} on the tick {tick}: a BUY there could "
            "fill; no order placed"
        )
    return price


def choose_strike(
    candidates: Sequence[tuple[Decimal, Decimal | None, int]],
    reference: Decimal,
    max_value: Decimal,
) -> int | None:
    """Index of the lowest strike above ``reference`` whose last price is above 0 and whose
    ``last price * lot`` is at most ``max_value``; ``None`` if there is none.

    ``candidates`` are ``(strike, last price, lot size)`` of CE rows. A row whose value cannot be
    judged (no or a non-finite last price, a lot below 1) is never chosen.
    """
    best: int | None = None
    for index, (strike, last, lot) in enumerate(candidates):
        if not (strike.is_finite() and strike > reference):
            continue
        if last is None or not _positive(last) or not isinstance(lot, int) or lot < 1:
            continue
        if last * lot > max_value:
            continue
        if best is None or strike < candidates[best][0]:
            best = index
    return best


def pick_option_by_premium(
    client: TradeSmart,
    segment: Segment,
    rows: list[dict[str, str]],
    today: date,
    record: dict[str, Any],
    *,
    max_value: Decimal = STRIKE_PREMIUM_CEILING,
) -> Instrument:
    """The nearest-expiry ``OPTIDX`` CE of ``segment.symbol`` nearest the money whose premium
    times its lot stays within ``max_value``.

    The reference is the last price of the underlying's nearest index future (``FUTIDX``,
    ``safety.future_segment``: BFO SENSEX looks up ``BSXFUT``), as in
    ``lifecycle.pick_option``. At most ``PREMIUM_STRIKE_TRIES`` strikes above it are quoted,
    nearest first, until ``choose_strike`` accepts one. ``Refusal`` (the test skips) names what
    is missing: the future, its last price, the options, or an affordable strike. The strike,
    premium, lot and value are recorded in ``record["option"]``.
    """
    try:
        future = safety.pick_instrument(safety.future_segment(segment), rows, today)
    except Refusal as exc:
        raise Refusal(
            f"no {segment.label} index future (FUTIDX) in the {segment.exchange} script master: "
            f"its last price is the reference of the option strike ({exc.reason}); "
            "no order placed"
        ) from exc
    reference = lifecycle._quote_lp(client, segment.exchange, future.token)
    info: dict[str, Any] = {
        "underlying": future.tradingsymbol,
        "underlying_last_price": None if reference is None else str(reference),
        "max_value": str(max_value),
    }
    record["option"] = info
    if reference is None or not _positive(reference):
        raise Refusal(
            f"no last price for the underlying future {future.tradingsymbol}; no order placed"
        )
    options = lifecycle._option_rows(segment, rows, "CE", today)
    if not options:
        raise Refusal(
            f"no {segment.instrument} {segment.label} CE of a future expiry in the "
            f"{segment.exchange} script master; no order placed"
        )
    above = sorted((item for item in options if item[0] > reference), key=lambda item: item[0])
    tried: list[dict[str, Any]] = []
    info["tried"] = tried
    candidates: list[tuple[Decimal, Decimal | None, int]] = []
    quoted: list[dict[str, str]] = []
    for strike, row in above[:PREMIUM_STRIKE_TRIES]:
        tradingsymbol = (row.get("TradingSymbol") or "").strip()
        token = (row.get("Token") or "").strip()
        lot = safety._lot_size(row.get("LotSize"))
        lp = lifecycle._quote_lp(client, segment.exchange, token) if token else None
        tried.append(
            {"tradingsymbol": tradingsymbol, "lp": None if lp is None else str(lp), "lot": lot}
        )
        complete = bool(tradingsymbol and token) and lot is not None
        # an incomplete row (no symbol, token or lot) cannot be judged: never chosen
        candidates.append((strike, lp if complete else None, lot if lot is not None else 0))
        quoted.append(row)
        chosen = choose_strike(candidates, reference, max_value)
        if chosen is None:
            continue
        _, premium, lot_size = candidates[chosen]
        picked = quoted[chosen]
        assert premium is not None
        info.update(
            {
                "strike": safety.strike_text(picked),
                "premium": str(premium),
                "lot": lot_size,
                "value": str(premium * lot_size),
            }
        )
        tick = safety._decimal(picked.get("TickSize"))
        return Instrument(
            tradingsymbol=(picked.get("TradingSymbol") or "").strip(),
            token=(picked.get("Token") or "").strip(),
            lot_size=lot_size,
            tick_size=tick if tick is not None and _positive(tick) else None,
            expiry=safety._expiry(picked.get("Expiry")),
        )
    names = ", ".join(t["tradingsymbol"] for t in tried) or "none"
    raise Refusal(
        f"no {segment.label} CE strike above {reference} with a last price and a premium "
        f"times lot of at most {max_value} (tried {len(tried)} of at most "
        f"{PREMIUM_STRIKE_TRIES}: {names}); no order placed"
    )


# -- the stage-two gate ------------------------------------------------------------------


# No stage-2 BUY from 14:30 IST: a fill then leaves time to sell it back well before the
# broker's intraday (MIS) square-off. Fixed (the outside-hours override never applies here).
STAGE2_LAST_BUY_IST = dtime(14, 30)


def now_ist() -> datetime:
    """The current time in IST (the dry run replaces it with a fake clock)."""
    return datetime.now(IST)


def stage_two_time_reason(now: datetime) -> str | None:
    """Why a stage-2 BUY must not be sent at ``now``: from ``STAGE2_LAST_BUY_IST`` (14:30 IST)
    on, or when ``now`` has no time zone (cannot be judged); ``None`` before 14:30 IST."""
    cut_off = f"{STAGE2_LAST_BUY_IST:%H:%M} IST"
    if now.tzinfo is None or now.utcoffset() is None:
        return (
            f"stage 2 (fills): the time {now} has no time zone (BUYs only before {cut_off}); "
            "no order placed"
        )
    local = now.astimezone(IST)
    if local.time() >= STAGE2_LAST_BUY_IST:
        return (
            f"stage 2 (fills): BUYs only before {cut_off} (now IST {local:%H:%M}), to leave "
            "time to sell a fill back before the intraday square-off; skipped, no order placed"
        )
    return None


def fills_enabled(environ: Mapping[str, str]) -> bool:
    """Whether the fills opt-in is set: ``TRADESMART_LIVE_ORDER_FILLS`` exactly ``1``."""
    return environ.get(FILLS_ENV) == "1"


def stage_two_reason(
    session: OrderSession, environ: Mapping[str, str], stage1_failures: Collection[str]
) -> str | None:
    """Why stage 2 (tiny fills) must not run; ``None`` only when every condition holds: fills
    are enabled, the session is in funded mode, no CHECK THE ACCOUNT stopped the run and no
    stage-1 test failed.

    ``stage1_failures`` are the node ids of the stage-1 tests that failed in setup, call or
    teardown (``conftest.stage1_failures(config)``, filled by the report hook; stage-1 tests
    are named ``test_stage1_*``). It is required, so a caller cannot forget it.
    """
    if not fills_enabled(environ):
        return f"stage 2 (fills) needs {FILLS_ENV}=1; skipped"
    if not session.funded:
        return "stage 2 (fills) runs only in funded mode; skipped"
    if session.stopped:
        return f"stage 2 (fills): the run was stopped ({safety.CHECK_THE_ACCOUNT}); skipped"
    if stage1_failures:
        names = ", ".join(sorted(stage1_failures))
        return (
            f"stage 2 (fills): {len(stage1_failures)} stage-1 test(s) failed in this run "
            f"({names}); skipped"
        )
    if session.stage1_failed:
        return "stage 2 (fills): a stage-1 test failed in this run; skipped"
    return None
