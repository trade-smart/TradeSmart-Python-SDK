"""Safety logic of the opt-in live order suite (pinned by tests/unit/test_live_orders_safety.py).

Everything that decides *whether* and *how* a real order is sent lives here, so it can be
tested offline with fakes:

- the double opt-in gate and the account / zero-funds refusals;
- UAT mode (a non-production test environment): no zero-funds check and a cap of 40 orders,
  only when ``TRADESMART_ENV=uat`` and the client's REST host is a UAT host, never production;
- funded mode (a small funded production account, ``TRADESMART_LIVE_ORDER_FUNDED=1``): only on
  the production host with ``TRADESMART_ENV`` unset or ``production`` and never outside market
  hours; the zero-funds check becomes a balance ceiling, the cap is 10 orders and only limit
  order types are sent. Funded mode and UAT mode are never both on; any failed check refuses;
- the order shape: a one-lot DAY LIMIT BUY priced well below the market, never below ``lc``
  (``run_order`` refuses any other shape unless fills are allowed: the UAT lifecycle tests);
- the per-run order cap and the "one open order at a time" rule;
- the order lifecycle: one place call, never resent; an ambiguous outcome is looked up in
  ``orders()``; an accepted order is cancelled once in a ``finally`` block and must reach a
  final state, else the test fails with "CHECK THE ACCOUNT: order <id>".

A ``Refusal`` means "do not place an order"; the live tests turn it into ``pytest.skip``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import secrets
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from datetime import time as dtime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from tests._environment import PRODUCTION, environment_name
from tradesmart import TradeSmart
from tradesmart.enums import OrderType
from tradesmart.errors import APIError, RequestValidationError
from tradesmart.models.base import IST
from tradesmart.models.orders import Order, OrderHistoryEntry
from tradesmart.models.portfolio import Limits
from tradesmart.models.user import UserDetails

if TYPE_CHECKING:  # funded.py imports this module: no import at run time
    from tests.live_orders.funded import MoneyBudget

LIVE_ENV = "TRADESMART_LIVE_TESTS"
ORDER_ENV = "TRADESMART_LIVE_ORDER_TESTS"
ACCOUNT_ENV = "TRADESMART_LIVE_ORDER_ACCOUNT"
# Owner-approved (2026-11-10): also place orders on weekdays outside the trading window, to see how
# TradeSmart rejects them. Weekends stay refused.
OUTSIDE_HOURS_ENV = "TRADESMART_LIVE_ORDER_OUTSIDE_HOURS"
XDIST_WORKER_ENV = "PYTEST_XDIST_WORKER"  # set by pytest-xdist in each worker process
MAX_ORDERS_PER_RUN = 3
UAT_MAX_ORDERS_PER_RUN = 40  # the UAT order-type matrix (see README.md)
UAT_ENV_NAME = "uat"
PRODUCTION_HOST = "v2api.tradesmartonline.in"
# Funded mode: a small funded production account (owner-approved defaults).
FUNDED_ENV = "TRADESMART_LIVE_ORDER_FUNDED"
FILLS_ENV = "TRADESMART_LIVE_ORDER_FILLS"
MAX_BALANCE_ENV = "TRADESMART_LIVE_ORDER_MAX_BALANCE"
IGNORE_OPEN_ENV = "TRADESMART_LIVE_ORDER_UAT_IGNORE_OPEN"  # UAT only: orders stuck open (F26)
FUNDED_MAX_ORDERS_PER_RUN = 10
FUNDED_DEFAULT_MAX_BALANCE = Decimal("25000")
CHECK_THE_ACCOUNT = "CHECK THE ACCOUNT"
TAG_PREFIX = "sdklive-"
RESULTS_FILE = Path(__file__).resolve().parents[2] / ".live-results" / "live_orders.jsonl"

# Order-book statuses after which nothing more can happen to an order.
FINAL_STATUSES = frozenset({"COMPLETE", "CANCELED", "CANCELLED", "REJECTED"})
FILLED = "COMPLETE"

# Documented ``Limits`` cash / collateral fields (the API documentation, "Limits"), plus the
# undocumented ``aux_*`` cash fields of the official example: being stricter only refuses more.
FUNDS_FIELDS: tuple[str, ...] = (
    "cash",
    "payin",
    "remarks_amt",
    "cac_daycash",
    "brkcollamt",
    "unclearedcash",
    "daycash",
    "collateral",
    "cash_coll",
    "grcoll",
    "aux_daycash",
    "aux_brkcollamt",
    "aux_unclearedcash",
)

BELOW_MARKET = Decimal("0.9")  # BUY at last price - 10 %
WELL_BELOW = Decimal("0.99")  # refuse a price less than 1 % below the last price


class Refusal(Exception):
    """Do not place an order (the live test skips with ``reason``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CheckTheAccount(AssertionError):
    """An order may still be live or was filled: a human must look at the account."""


# -- gates ------------------------------------------------------------------------------


def gate_skip_reason(environ: Mapping[str, str]) -> str | None:
    """``None`` only when both opt-in variables are exactly ``1``, in a serial run.

    The order cap and the "one open order at a time" rule are per process, so inside a
    pytest-xdist worker the gate stays closed (``tests/conftest.py`` already forces a serial run
    when a live opt-in is set; this is the second line of defence).
    """
    if environ.get(LIVE_ENV) != "1" or environ.get(ORDER_ENV) != "1":
        return (
            f"live order tests are opt-in: set {LIVE_ENV}=1 and {ORDER_ENV}=1 "
            "(approved zero-funds test account only; see tests/live_orders/README.md)"
        )
    if environ.get(XDIST_WORKER_ENV):
        return "live order tests run serially only: the order cap is per process (use -n 0)"
    return None


def is_live_orders_item(nodeid: str) -> bool:
    """Whether the item's *file* is under tests/live_orders (parameter ids are ignored)."""
    path = nodeid.split("::", 1)[0].replace("\\", "/")
    return "/tests/live_orders/" in "/" + path


def _norm(value: str | None) -> str:
    return (value or "").strip().upper()


def account_refusal(details: UserDetails, expected: str | None, client_id: str) -> str | None:
    """Refuse unless both the profile and the client are the approved test account."""
    wanted = _norm(expected)
    if not wanted:
        return f"set {ACCOUNT_ENV} to the owner-approved test account id; refusing to place orders"
    if details.stat != "Ok":
        return f"profile() did not answer Ok, so {ACCOUNT_ENV} cannot be confirmed; refusing"
    if _norm(details.user_id) != wanted:
        return f"profile() user id is not {ACCOUNT_ENV} ({wanted}); refusing to place orders"
    if _norm(client_id) != wanted:
        return f"the client id is not {ACCOUNT_ENV} ({wanted}); refusing to place orders"
    return None


def funds_refusal(limits: Limits) -> str | None:
    """Refuse unless every cash / collateral field is absent or <= 0."""
    if limits.stat != "Ok":
        return "margins() did not answer Ok, so zero funds cannot be confirmed; refusing"
    funded = [
        name
        for name in FUNDS_FIELDS
        if isinstance(getattr(limits, name, None), Decimal) and getattr(limits, name) > 0
    ]
    if funded:
        return (
            "the account shows usable cash or collateral (" + ", ".join(funded) + "); live order "
            "tests run only on the zero-funds test account; refusing"
        )
    return None


# -- UAT mode ---------------------------------------------------------------------------

_HOST_NAME = re.compile(r"[a-z0-9.-]+")  # plain ASCII host names only (no IDNA, % or \ tricks)


def _rest_host(base_url: str) -> str | None:
    """The REST host, lower case without trailing dots; ``None`` when it cannot be trusted.

    Parsed with ``urllib.parse``. User info, an invalid port, non-ASCII or escaped characters
    and anything that does not parse give ``None``.
    """
    try:
        parts = urlsplit(base_url)
        _ = parts.port  # ValueError for an invalid port
    except ValueError:
        return None
    if "@" in parts.netloc or parts.hostname is None:
        return None
    host = parts.hostname.rstrip(".")
    return host if _HOST_NAME.fullmatch(host) else None


def uat_mode(environ: Mapping[str, str], base_url: str) -> bool:
    """UAT order rules apply only when every condition holds.

    ``TRADESMART_ENV`` is ``uat``; the REST host of ``base_url`` parses cleanly, is not the
    production host (nor contains it: no sub-domain or look-alike) and contains ``uat``.
    ``uat`` in the path, query or user info never counts. ``TRADESMART_LIVE_ORDER_FUNDED`` set
    (to any value) means never UAT mode: funded mode and UAT mode are never both on.
    """
    if FUNDED_ENV in environ:
        return False
    if environment_name(environ) != UAT_ENV_NAME:
        return False
    host = _rest_host(base_url)
    if host is None or PRODUCTION_HOST in host:
        return False
    return UAT_ENV_NAME in host


_ORDER_NUMBER = re.compile(r"\d{1,30}")


def ignored_open_orders(
    environ: Mapping[str, str], base_url: str, *, mock: bool = False
) -> frozenset[str]:
    """Order numbers named in ``TRADESMART_LIVE_ORDER_UAT_IGNORE_OPEN`` (comma-separated).

    For an order stuck open on UAT that cannot be cancelled (F26), or (``mock``: the matrix in
    a valid mock session, owner 14 Nov 2026) an order the API still lists as open after it was
    cancelled: the one-open-order check skips exactly these numbers; any other open order
    still stops the run, and the ignored ones are never touched. Unset: nothing is ignored.
    Set outside UAT mode and a mock session, with funded mode, or to anything but order
    numbers: refused.
    """
    if IGNORE_OPEN_ENV not in environ:
        return frozenset()
    allowed = uat_mode(environ, base_url) or (mock and FUNDED_ENV not in environ)
    if not allowed:
        raise Refusal(
            f"{IGNORE_OPEN_ENV} is allowed only in UAT mode or a mock session; refusing to "
            "place orders"
        )
    parts = [part.strip() for part in environ[IGNORE_OPEN_ENV].split(",")]
    if not all(_ORDER_NUMBER.fullmatch(part) for part in parts):
        raise Refusal(
            f"{IGNORE_OPEN_ENV} must be comma-separated order numbers; refusing to place orders"
        )
    return frozenset(parts)


def environment_refusal(environ: Mapping[str, str], base_url: str) -> str | None:
    """Orders go to production (every production rule) or, in UAT mode, to UAT; else refuse."""
    name = environment_name(environ)
    if name == PRODUCTION or uat_mode(environ, base_url):
        return None
    return (
        f"TRADESMART_ENV={name}: live order tests run on production or in UAT mode only "
        f"(TRADESMART_ENV=uat and a REST host that contains 'uat' and is not {PRODUCTION_HOST}); "
        "refusing to place orders"
    )


# -- UAT preflight (owner, 2026-11-13) --------------------------------------------------
# Before any order of a UAT order run (the matrix session, the older UAT suites' session; the
# cleanup tool only cancels and is exempt), a read-only ``margins()`` must show each of these
# fields at ``UAT_MIN_LIMIT`` or more, else the run is refused before any order. A field
# that is missing or cannot be read refuses too (fails closed). UAT only: production, funded and
# zero-funds runs never read it.
UAT_MIN_LIMIT = Decimal("100000000")  # the UAT preflight minimum (no figure in any doc)
# The mock-session preflight minimum (``mock_session``; ``limits_preflight``).
MOCK_MIN_LIMIT = Decimal("2000000")
UAT_PREFLIGHT_FIELDS: tuple[str, ...] = (
    "cash",  # cash
    "turnoverlmt",  # turnover limit
    "pendordvallmt",  # pending-order value limit
)
UAT_RMS_ASK = "ask the UAT RMS team to raise it"


@dataclass(frozen=True)
class LimitsPreflight:
    """A limits preflight's reading: each field's value (``None``: missing or unreadable), every
    unmet item (empty: passed), the minimum, the run's label (``UAT``, ``mock-session``) and the
    ask in the refusal."""

    values: dict[str, Decimal | None]
    unmet: tuple[str, ...]
    minimum: Decimal = UAT_MIN_LIMIT
    label: str = "UAT"
    ask: str = UAT_RMS_ASK

    @property
    def refusal(self) -> str | None:
        """One message listing every unmet item, or ``None`` when every limit is met."""
        if not self.unmet:
            return None
        return (
            f"{self.label} preflight refused before any order: " + "; ".join(self.unmet)
            + f"; {self.ask}; refusing to place orders"
        )  # fmt: skip

    def record(self) -> dict[str, Any]:
        """The values for the run record (these three fields only: no account ids)."""
        data: dict[str, Any] = {
            name: None if value is None else str(value) for name, value in self.values.items()
        }
        data["minimum"] = str(self.minimum)
        data["passed"] = not self.unmet
        return data


UatPreflight = LimitsPreflight  # the UAT preflight's reading (its original name)


def limits_preflight(
    client: TradeSmart, minimum: Decimal, label: str, *, ask: str = UAT_RMS_ASK
) -> LimitsPreflight:
    """Read ``margins()`` once (read-only) and judge the limits (``UAT_PREFLIGHT_FIELDS`` each at
    ``minimum`` or more); any doubt is an unmet item. UAT: ``UAT_MIN_LIMIT``; the mock session:
    ``MOCK_MIN_LIMIT``."""

    def reading(values: dict[str, Decimal | None], unmet: Sequence[str]) -> LimitsPreflight:
        return LimitsPreflight(values, tuple(unmet), minimum=minimum, label=label, ask=ask)

    empty: dict[str, Decimal | None] = dict.fromkeys(UAT_PREFLIGHT_FIELDS)
    try:
        limits = client.margins()
    except Exception as exc:  # any failure: the limits cannot be confirmed
        return reading(empty, (f"margins() could not be read ({type(exc).__name__})",))
    if limits.stat != "Ok":
        return reading(empty, ("margins() did not answer Ok, so the limits are unknown",))
    values: dict[str, Decimal | None] = {}
    unmet: list[str] = []
    for name in UAT_PREFLIGHT_FIELDS:
        value = getattr(limits, name, None)
        if not isinstance(value, Decimal) or not value.is_finite():
            values[name] = None
            unmet.append(f"{name} missing or unreadable")
            continue
        values[name] = value
        if value < minimum:
            unmet.append(f"{name} {value} is below the minimum {minimum}")
    return reading(values, unmet)


def uat_preflight(client: TradeSmart) -> LimitsPreflight:
    """The UAT preflight: ``limits_preflight`` with ``UAT_MIN_LIMIT``."""
    return limits_preflight(client, UAT_MIN_LIMIT, "UAT")


def max_orders_per_run(uat: bool) -> int:
    """The per-run order cap: 40 in UAT mode, 3 on production."""
    return UAT_MAX_ORDERS_PER_RUN if uat else MAX_ORDERS_PER_RUN


# -- funded mode ------------------------------------------------------------------------


def is_production_host(base_url: str) -> bool:
    """Whether the REST host of ``base_url`` is exactly the production host (UAT-mode parser)."""
    return _rest_host(base_url) == PRODUCTION_HOST


def funded_refusal(environ: Mapping[str, str], base_url: str) -> str | None:
    """``None`` when ``TRADESMART_LIVE_ORDER_FUNDED`` is unset or funded mode holds; else why not.

    Funded mode needs the flag set to exactly ``1``, ``TRADESMART_ENV`` unset or ``production``,
    the production REST host and ``TRADESMART_LIVE_ORDER_OUTSIDE_HOURS`` not enabled. A set flag
    with any failed check refuses the run (never a fallback to other rules).
    """
    value = environ.get(FUNDED_ENV)
    if value is None:
        return None
    if value != "1":
        return f"{FUNDED_ENV} must be exactly 1 to enable funded mode; refusing to place orders"
    name = environment_name(environ)
    if name != PRODUCTION:
        return (
            f"funded mode runs on production only (TRADESMART_ENV={name}); refusing to place orders"
        )
    if not is_production_host(base_url):
        return (
            f"funded mode needs the REST host {PRODUCTION_HOST} exactly; refusing to place orders"
        )
    if environ.get(OUTSIDE_HOURS_ENV) == "1":  # the same parsing as market_closed_reason
        return (
            f"{OUTSIDE_HOURS_ENV} is not allowed in funded mode (orders only inside market "
            "hours); refusing to place orders"
        )
    return None


def funded_mode(environ: Mapping[str, str], base_url: str) -> bool:
    """Funded mode holds only when the flag is exactly ``1`` and every funded check passes."""
    return environ.get(FUNDED_ENV) == "1" and funded_refusal(environ, base_url) is None


_WHOLE_RUPEES = re.compile(r"[1-9][0-9]*")  # ASCII digits only, no sign, point or exponent


def max_balance(environ: Mapping[str, str]) -> Decimal:
    """The funded-mode balance ceiling: 25000 by default, else a whole number from 1 to 25000."""
    value = environ.get(MAX_BALANCE_ENV)
    if value is None:
        return FUNDED_DEFAULT_MAX_BALANCE
    text = value.strip()
    if _WHOLE_RUPEES.fullmatch(text) and len(text) <= len(str(FUNDED_DEFAULT_MAX_BALANCE)):
        amount = Decimal(int(text))
        if amount <= FUNDED_DEFAULT_MAX_BALANCE:
            return amount
    raise Refusal(
        f"{MAX_BALANCE_ENV} must be a whole number from 1 to {FUNDED_DEFAULT_MAX_BALANCE}; "
        "refusing to place orders"
    )


def balance_refusal(limits: Limits, ceiling: Decimal) -> str | None:
    """Refuse unless ``margins()`` answered Ok with a usable ``cash`` at most ``ceiling``.

    The positive ``FUNDS_FIELDS`` values (cash, pay-in, collateral ...) together must also be at
    most ``ceiling``; the reason names the fields, never the amounts or an account id.
    """
    if limits.stat != "Ok":
        return "margins() did not answer Ok, so the available cash cannot be checked; refusing"
    cash = limits.cash
    if not isinstance(cash, Decimal) or not cash.is_finite():
        return (
            "margins() gave no usable cash value, so the funded-mode balance ceiling cannot be "
            "checked; refusing"
        )
    if cash > ceiling:
        return (
            f"the available cash is above the funded-mode ceiling of {ceiling} "
            f"({MAX_BALANCE_ENV}); refusing"
        )
    # Every positive cash / collateral field counts (the zero-funds check's fields), so funds
    # held outside ``cash`` cannot pass the small-account ceiling.
    funded: dict[str, Decimal] = {}
    for name in FUNDS_FIELDS:
        value = getattr(limits, name, None)
        if isinstance(value, Decimal) and value.is_finite() and value > 0:
            funded[name] = value
    if sum(funded.values(), Decimal(0)) > ceiling:
        return (
            "the account's cash and collateral fields together (" + ", ".join(funded) + ") are "
            f"above the funded-mode ceiling of {ceiling} ({MAX_BALANCE_ENV}); refusing"
        )
    return None


_FUNDED_ORDER_TYPES = frozenset({OrderType.LIMIT, OrderType.SL})  # LMT and SL-LMT only


def _is_limit_type(order_type: str) -> bool:
    try:
        parsed = OrderType(order_type)
    except ValueError:
        return False
    return parsed in _FUNDED_ORDER_TYPES


# -- price ------------------------------------------------------------------------------


def _places(tick: Decimal) -> Decimal:
    exponent = tick.normalize().as_tuple().exponent
    places = exponent if isinstance(exponent, int) and exponent < 0 else 0
    return Decimal(1).scaleb(places)


def floor_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    ticks = (value / tick).to_integral_value(rounding=ROUND_FLOOR)
    return (ticks * tick).quantize(_places(tick))


def ceil_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    ticks = (value / tick).to_integral_value(rounding=ROUND_CEILING)
    return (ticks * tick).quantize(_places(tick))


def buy_limit_price(
    last_price: Decimal | None,
    tick_size: Decimal | None,
    lower_circuit: Decimal | None,
    ratio: Decimal = BELOW_MARKET,
) -> Decimal:
    """Last price x ``ratio`` (default: last price - 10 %), rounded down to the tick, but never
    below ``lc`` (rounded up)."""
    if last_price is None or last_price <= 0:
        raise Refusal("no last price from quote() (market closed or no trades yet)")
    if tick_size is None or tick_size <= 0:
        raise Refusal("no tick size from quote() or the script master")
    price = floor_to_tick(last_price * ratio, tick_size)
    if lower_circuit is not None and lower_circuit > 0 and price < lower_circuit:
        price = ceil_to_tick(lower_circuit, tick_size)
    if price <= 0:
        raise Refusal(f"no positive price below the market (last price {last_price})")
    if price > last_price * WELL_BELOW:
        raise Refusal(
            f"price band too narrow: the lowest allowed price {price} is not well below the "
            f"last price {last_price}"
        )
    return price


def modify_price(
    price: Decimal,
    tick_size: Decimal,
    lower_circuit: Decimal | None,
    last_price: Decimal,
) -> Decimal:
    """One tick lower if the band allows, else one tick higher (still well below the market)."""
    down = (price - tick_size).quantize(_places(tick_size))
    if down > 0 and (lower_circuit is None or lower_circuit <= 0 or down >= lower_circuit):
        return down
    up = (price + tick_size).quantize(_places(tick_size))
    return up if up <= last_price * WELL_BELOW else price


# -- segments and market hours ----------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    id: str
    exchange: str
    product: str
    opens: dtime
    closes: dtime
    tradingsymbol: str | None = None  # equity: an exact trading symbol
    instrument: str | None = None  # futures: script-master Instrument ...
    symbol: str | None = None  # ... and Symbol; the nearest expiry after today is used
    # Options: script-master Symbol of the index future when it differs from the options'
    # Symbol (BFO SENSEX: future BSXFUT, options BSXOPT; NFO NIFTY: both NIFTY).
    future_symbol: str | None = None
    name: str | None = None  # the underlying in messages (BFO "SENSEX", not "BSXOPT")

    @property
    def label(self) -> str:
        """The underlying as a person knows it: ``name``, else the Symbol or trading symbol."""
        return self.name or self.symbol or self.tradingsymbol or self.id


def future_segment(segment: Segment) -> Segment:
    """The lookup of an option segment's nearest index future (``FUTIDX``, ``future_symbol``)."""
    return dataclasses.replace(
        segment,
        instrument="FUTIDX",
        symbol=segment.future_symbol or segment.symbol,
        future_symbol=None,
        name=segment.label,
    )


SEGMENTS: dict[str, Segment] = {
    "mcx": Segment(
        "mcx",
        "MCX",
        "NRML",
        dtime(9, 0),
        dtime(23, 30),
        instrument="FUTCOM",
        symbol="CRUDEOIL",
    ),
    "nse": Segment("nse", "NSE", "CNC", dtime(9, 15), dtime(15, 30), tradingsymbol="SBIN-EQ"),
    "nfo": Segment(
        "nfo", "NFO", "NRML", dtime(9, 15), dtime(15, 30), instrument="FUTIDX", symbol="NIFTY"
    ),
}

AFTER_OPEN_MINUTES = 5  # skip the opening minutes
BEFORE_CLOSE_MINUTES = 15  # leave time to cancel before the close


def _minutes(t: dtime) -> int:
    return t.hour * 60 + t.minute


def market_closed_reason(segment: Segment, now: datetime) -> str | None:
    """Skip reason when ``segment`` is outside its trading window (holidays are not known)."""
    local = now.astimezone(IST)
    name = segment.id.upper()
    if local.weekday() >= 5:
        return f"{name} market is closed (weekend, IST {local:%a %H:%M})"
    current = _minutes(local.time())
    start = _minutes(segment.opens) + AFTER_OPEN_MINUTES
    end = _minutes(segment.closes) - BEFORE_CLOSE_MINUTES
    if not start <= current <= end and os.environ.get(OUTSIDE_HOURS_ENV) != "1":
        return (
            f"{name} market is closed or about to close (IST {local:%H:%M}; orders are placed "
            f"only {start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d})"
        )
    return None


# -- instrument from the script master --------------------------------------------------


@dataclass(frozen=True)
class Instrument:
    tradingsymbol: str
    token: str
    lot_size: int
    tick_size: Decimal | None
    expiry: date | None = None


def strike_text(row: Mapping[str, str]) -> str:
    """A script-master row's strike: NFO and MCX name the column ``StrikePrice``, BFO ``Strike``
    (production headers, 2026-11-12). As in the SDK (spec 10.4), the ``StrikePrice`` key decides
    when the row has it, even when blank; ``""`` when the column is blank or absent."""
    column = "StrikePrice" if "StrikePrice" in row else "Strike"
    return (row.get(column) or "").strip()


def _decimal(value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _lot_size(value: str | None) -> int | None:
    number = _decimal(value)
    if number is None or number <= 0 or number != number.to_integral_value():
        return None
    return int(number)


def _expiry(value: str | None) -> date | None:
    try:
        return datetime.strptime((value or "").strip().title(), "%d-%b-%Y").date()
    except ValueError:
        return None


def pick_instrument(segment: Segment, rows: list[dict[str, str]], today: date) -> Instrument:
    """The segment's instrument row (script-master headers are undocumented; observed names)."""
    exchange = segment.exchange
    candidates: list[tuple[date, dict[str, str]]] = []
    for row in rows:
        if _norm(row.get("Exchange", exchange)) != exchange:
            continue
        if segment.tradingsymbol is not None:
            if _norm(row.get("TradingSymbol")) == segment.tradingsymbol:
                candidates.append((today, row))
                break
            continue
        if _norm(row.get("Instrument")) != segment.instrument:
            continue
        if _norm(row.get("Symbol")) != segment.symbol:
            continue
        expiry = _expiry(row.get("Expiry"))
        if expiry is not None and expiry > today:
            candidates.append((expiry, row))
    if not candidates:
        wanted = segment.tradingsymbol or f"{segment.instrument} {segment.symbol}"
        raise Refusal(f"{wanted} not found in the {exchange} script master")
    expiry, row = min(candidates, key=lambda c: c[0])
    tradingsymbol = (row.get("TradingSymbol") or "").strip()
    token = (row.get("Token") or "").strip()
    lot = _lot_size(row.get("LotSize"))
    if lot is None:
        raise Refusal(f"no usable lot size for {tradingsymbol} in the script master")
    if not tradingsymbol or not token:
        raise Refusal(f"script-master row for {segment.id} lacks TradingSymbol or Token")
    tick = _decimal(row.get("TickSize"))
    return Instrument(
        tradingsymbol=tradingsymbol,
        token=token,
        lot_size=lot,
        tick_size=tick if tick is not None and tick > 0 else None,
        expiry=None if segment.tradingsymbol else expiry,
    )


# -- the order plan ---------------------------------------------------------------------


def new_tag(segment_id: str) -> str:
    """Unique order tag (``remarks``): finds the order again after an ambiguous outcome."""
    return f"{TAG_PREFIX}{segment_id}-{secrets.token_hex(3)}"


@dataclass(frozen=True)
class OrderPlan:
    segment_id: str
    exchange: str
    tradingsymbol: str
    token: str
    quantity: int
    price: Decimal
    product: str
    tag: str
    tick_size: Decimal | None = None
    lower_circuit: Decimal | None = None
    last_price: Decimal | None = None
    upper_circuit: Decimal | None = None
    # The shape. Production sends only the defaults (a DAY LIMIT BUY); ``run_order`` refuses
    # any other shape unless fills are allowed (the UAT lifecycle tests only).
    transaction_type: str = "BUY"
    order_type: str = "LIMIT"
    validity: str = "DAY"
    trigger_price: Decimal | None = None

    @property
    def is_production_shape(self) -> bool:
        """A DAY LIMIT BUY without a trigger: the only shape sent outside UAT."""
        return (self.transaction_type, self.order_type, self.validity, self.trigger_price) == (
            "BUY",
            "LIMIT",
            "DAY",
            None,
        )

    def place_kwargs(self) -> dict[str, Any]:
        """The ``place_order`` arguments; only the limit types (LIMIT, SL-LMT) send a price."""
        kwargs: dict[str, Any] = {
            "exchange": self.exchange,
            "tradingsymbol": self.tradingsymbol,
            "quantity": self.quantity,
            "transaction_type": self.transaction_type,
            "product": self.product,
            "order_type": self.order_type,
            "price": self.price,
            "validity": self.validity,
            "tag": self.tag,
        }
        if self.order_type not in ("LIMIT", "SL-LMT"):
            del kwargs["price"]
        if self.trigger_price is not None:
            kwargs["trigger_price"] = self.trigger_price
        return kwargs

    def describe(self) -> dict[str, Any]:
        """The plan for the record; ``trigger_price`` only on a stop order."""
        described: dict[str, Any] = {
            "segment": self.segment_id,
            "exchange": self.exchange,
            "tradingsymbol": self.tradingsymbol,
            "quantity": self.quantity,
            "price": self.price,
            "product": self.product,
            "order_type": self.order_type,
            "transaction_type": self.transaction_type,
            "validity": self.validity,
            "tag": self.tag,
            "last_price": self.last_price,
            "lower_circuit": self.lower_circuit,
            "tick_size": self.tick_size,
        }
        if self.trigger_price is not None:
            described["trigger_price"] = self.trigger_price
        return described


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


InstrumentPicker = Callable[
    [TradeSmart, Segment, list[dict[str, str]], date, dict[str, Any]], Instrument
]


def _pick_from_rows(
    client: TradeSmart,
    segment: Segment,
    rows: list[dict[str, str]],
    today: date,
    record: dict[str, Any],
) -> Instrument:
    return pick_instrument(segment, rows, today)


def prepare_plan(
    client: TradeSmart,
    segment: Segment,
    record: dict[str, Any],
    *,
    now: datetime | None = None,
    uat: bool = False,
    pick: InstrumentPicker = _pick_from_rows,
    funded_ceiling: Decimal | None = None,
    any_hour: bool = False,
) -> OrderPlan:
    """Check hours and funds, pick the instrument and price it (read-only calls only).

    The market-hours check is skipped only when ``uat`` **and** ``any_hour`` are true (the UAT
    order suites pass ``any_hour=session.uat``: UAT tests run at any hour, owner 2026-11-13; a
    case the closed market refuses is recorded). Production, the zero-funds account, funded mode
    and the GTT live test (which does not pass ``any_hour``) keep it.

    The zero-funds check is skipped only when ``uat`` is true (the session's UAT mode). With
    ``funded_ceiling`` (the session's funded mode) the balance ceiling replaces it.
    ``pick`` chooses the instrument from the script master (the UAT options pass their own:
    the at-the-money strike, which needs quotes).
    """
    if uat and funded_ceiling is not None:
        raise Refusal("UAT mode and funded mode cannot both be on; refusing to place orders")
    now = now if now is not None else datetime.now(IST)
    if uat and any_hour:
        record["hours_check"] = "skipped (UAT: any hour)"
    else:
        reason = market_closed_reason(segment, now)
        if reason is not None:
            raise Refusal(reason)
    if funded_ceiling is not None:
        reason = balance_refusal(client.margins(), funded_ceiling)
        if reason is not None:
            raise Refusal(reason)
        record["funds_check"] = "funded mode: balance <= ceiling"
    elif uat:
        record["funds_check"] = "skipped (UAT mode)"
    else:
        reason = funds_refusal(client.margins())
        if reason is not None:
            raise Refusal(reason)
        record["funds_check"] = "passed"
    inst = pick(
        client, segment, client.instruments(segment.exchange), now.astimezone(IST).date(), record
    )
    record["instrument"] = {
        "tradingsymbol": inst.tradingsymbol,
        "token": inst.token,
        "lot_size": inst.lot_size,
        "tick_size": _text(inst.tick_size),
        "expiry": _text(inst.expiry),
    }
    try:
        quote = client.quote(segment.exchange, inst.token)
    except Exception as exc:  # any failure means: no price, so no order
        raise Refusal(f"quote() failed ({type(exc).__name__}); not placing an order") from exc
    tick = quote.tick_size if quote.tick_size is not None and quote.tick_size > 0 else None
    tick = tick if tick is not None else inst.tick_size
    record["quote"] = {
        "lp": _text(quote.last_price),
        "lc": _text(quote.lc),
        "uc": _text(quote.uc),
        "ti": _text(quote.tick_size),
        "ls": _text(quote.lot_size),
    }
    record["lot_size_check"] = {"script_master": inst.lot_size, "quote": quote.lot_size}
    if quote.lot_size is not None and quote.lot_size != inst.lot_size:
        raise Refusal(
            f"lot size mismatch for {inst.tradingsymbol}: script master {inst.lot_size}, "
            f"quote() ls {quote.lot_size}; not placing an order"
        )
    # One lot, in units on every exchange: MCX qty is in trade units too (F21, owner 2026-11-12).
    quantity = inst.lot_size
    record["quantity_rule"] = "script-master LotSize"
    price = buy_limit_price(quote.last_price, tick, quote.lc)
    assert tick is not None and quote.last_price is not None  # buy_limit_price refused otherwise
    return OrderPlan(
        segment_id=segment.id,
        exchange=segment.exchange,
        tradingsymbol=inst.tradingsymbol,
        token=inst.token,
        quantity=quantity,
        price=price,
        product=segment.product,
        tag=new_tag(segment.id),
        tick_size=tick,
        lower_circuit=quote.lc,
        last_price=quote.last_price,
        upper_circuit=quote.uc,
    )


# -- budget -----------------------------------------------------------------------------


@dataclass
class OrderBudget:
    """At most ``limit`` place calls per test run; spent *before* the order is sent."""

    limit: int = MAX_ORDERS_PER_RUN
    used: int = 0

    def reserve(self) -> None:
        if self.used >= self.limit:
            raise Refusal(f"order cap reached: at most {self.limit} orders per run")
        self.used += 1


# -- recording --------------------------------------------------------------------------

_SECRET_KEYS = frozenset(
    {
        "uid",
        "actid",
        "pan",
        "email",
        "m_num",
        "uname",
        "ipaddr",
        "src_uid",
        "kidid",
        "authorization",
        "jkey",
        "checksum",
        "code",
        "auth_code",
        "password",
        "pwd",
        "api_key",
        "apikey",
        "app_key",
        "appkey",
    }
)


def _is_secret_key(key: str) -> bool:
    k = key.lower()
    if k in _SECRET_KEYS:
        return True
    return ("token" in k and k != "token") or "secret" in k or "password" in k


def scrub(value: Any) -> Any:
    """Drop secret or personal keys at any depth (the instrument ``token`` is kept)."""
    if isinstance(value, Mapping):
        return {str(k): scrub(v) for k, v in value.items() if not _is_secret_key(str(k))}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


class Recorder:
    """Appends one JSON object per line to ``path`` (secrets and personal fields removed).

    Each line carries ``environment``: the ``TRADESMART_ENV`` name of the run.
    """

    def __init__(self, path: Path = RESULTS_FILE) -> None:
        self.path = path

    def write(self, record: Mapping[str, Any]) -> None:
        data = scrub(dict(record))
        data["recorded_at"] = datetime.now(UTC).isoformat()
        data["environment"] = environment_name(os.environ)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(data, default=str, sort_keys=True) + "\n")


ORDER_ENDPOINTS = frozenset(
    {
        "PlaceOrder",
        "ModifyOrder",
        "CancelOrder",
        "ExitSNOOrder",
        "PlaceGTTOrder",
        "ModifyGTTOrder",
        "CancelGTTOrder",
    }
)


class ResponseLog:
    """httpx response hook that keeps order-endpoint responses to record their raw bodies.

    The hook only stores the response object; bodies are read later, after the SDK has
    read them, so the hook cannot change what the SDK sees.
    """

    def __init__(self) -> None:
        self._responses: list[tuple[str, httpx.Response]] = []

    def hook(self, response: httpx.Response) -> None:
        name = response.request.url.path.rstrip("/").rsplit("/", 1)[-1]
        if name in ORDER_ENDPOINTS:
            self._responses.append((name, response))

    def attach(self, http_client: httpx.Client) -> None:
        hooks = http_client.event_hooks
        http_client.event_hooks = {
            "request": list(hooks.get("request", [])),
            "response": [*hooks.get("response", []), self.hook],
        }

    def mark(self) -> int:
        """Position to pass as ``since``: only responses recorded after it are attributed."""
        return len(self._responses)

    def last(self, endpoint: str, since: int = 0) -> dict[str, Any] | None:
        for name, response in reversed(self._responses[since:]):
            if name != endpoint:
                continue
            try:
                body = response.json()
            except httpx.ResponseNotRead:
                return {"http_status": response.status_code, "body": None}
            except ValueError:
                return {"http_status": response.status_code, "text": response.text[:500]}
            return {"http_status": response.status_code, "body": scrub(body)}
        return None


def _error_record(exc: BaseException) -> dict[str, Any]:
    data: dict[str, Any] = {"exception": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, APIError):
        raw = exc.raw if isinstance(exc.raw, Mapping) else {}
        data["stat"] = raw.get("stat")
        data["emsg"] = exc.emsg
    return data


def _mark(responses: ResponseLog | None) -> int:
    return 0 if responses is None else responses.mark()


def _attach_response(
    entry: dict[str, Any], responses: ResponseLog | None, endpoint: str, since: int
) -> None:
    """Attach the newest ``endpoint`` body recorded after ``since`` (never an older one)."""
    if responses is None:
        return
    seen = responses.last(endpoint, since=since)
    if seen is not None:
        entry["response"] = seen


# -- the order lifecycle ----------------------------------------------------------------


def status_of(order: Order | None) -> str | None:
    if order is None or order.status is None:
        return None
    text = str(order.status).strip().upper()
    return text or None


def is_final(status: str | None) -> bool:
    return status is not None and status in FINAL_STATUSES


def _find(book: list[Order], order_id: str | None, tag: str) -> Order | None:
    for order in book:
        if order_id is not None and (order.order_id or "").strip() == order_id:
            return order
    for order in book:
        if (order.tag or "").strip() == tag:
            return order
    return None


def _order_row(order: Order) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "status": status_of(order),
        "rejreason": order.reject_reason,
        "raw": dict(order.raw),
    }


@dataclass
class OrderOutcome:
    order_id: str | None = None
    final_status: str | None = None
    exception: str | None = None  # what place_order raised, if anything
    definitive_rejection: bool = False  # place_order raised an APIError (stat Not_Ok)
    was_open: bool = False
    cancelled: bool = False
    filled: bool = False  # a fill was seen (returned only by run_order(allow_fill=True))
    # The order's filled quantity (``fillshares``, a running total) on the final order-book row
    # seen; ``None`` when that row has no usable value (never an earlier row's).
    filled_quantity: int | None = None

    @property
    def settled(self) -> bool:
        """Rejected or cancelled for sure (a fill is never "settled": it is a failure)."""
        if self.final_status is not None:
            return self.final_status != FILLED and is_final(self.final_status)
        return self.definitive_rejection and self.order_id is None


OpenHook = Callable[[Order, dict[str, Any]], None]


def run_order(
    client: TradeSmart,
    plan: OrderPlan,
    budget: OrderBudget,
    record: dict[str, Any],
    *,
    on_open: OpenHook | None = None,
    responses: ResponseLog | None = None,
    recorder: Recorder | None = None,
    settle_seconds: float = 8.0,
    poll_interval: float = 1.0,
    cancel_settle_seconds: float = 15.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    allow_fill: bool = False,
    uat: bool = False,
    funded: bool = False,
    ignore_open: frozenset[str] = frozenset(),
    test_budget: OrderBudget | None = None,
) -> OrderOutcome:
    """Send ``plan`` once, observe it, cancel it once if it is still open; never resend.

    ``ignore_open`` (UAT mode only; see :func:`ignored_open_orders`) names orders stuck open on
    UAT that the one-open-order check skips; they are never touched.

    ``test_budget`` (the running test's own cap) is spent together with ``budget``, right
    before the place call: an order refused earlier (another order open, a refused shape) is
    never counted as sent (N4).

    A (partial) fill fails with "CHECK THE ACCOUNT" unless ``allow_fill`` is true (the UAT
    lifecycle tests and funded mode only); then it is returned as ``outcome.filled`` with
    ``outcome.filled_quantity``. Only then may the plan have another shape than the DAY LIMIT
    BUY (the UAT order-type matrix). Both need ``uat`` (the session's UAT mode) or ``funded``
    (the session's funded mode) as well. In funded mode only limit order types (LIMIT, SL-LMT)
    are sent: MARKET, SL-MKT and anything unknown are refused before any call.
    """
    if uat and funded:
        raise Refusal("UAT mode and funded mode cannot both be on; refusing to place orders")
    if ignore_open and not uat:
        raise Refusal("ignoring a stuck open order is allowed only in UAT mode; refusing")
    if funded and not _is_limit_type(plan.order_type):
        raise Refusal(
            "funded mode sends limit orders only (LIMIT, SL-LMT); refusing "
            f"{plan.validity} {plan.order_type} {plan.transaction_type}"
        )
    if (allow_fill or not plan.is_production_shape) and not (uat or funded):
        raise Refusal(
            "fills and shapes other than a DAY LIMIT BUY are allowed only in UAT mode or funded "
            f"mode; refusing {plan.validity} {plan.order_type} {plan.transaction_type}"
        )
    if not allow_fill and not plan.is_production_shape:
        raise Refusal(
            "only a DAY LIMIT BUY is sent outside the UAT lifecycle tests; refusing "
            f"{plan.validity} {plan.order_type} {plan.transaction_type}"
        )
    record["plan"] = plan.describe()
    open_rows = [o for o in client.orders() if not is_final(status_of(o))]
    ignored = sorted({o.order_id for o in open_rows if (o.order_id or "").strip() in ignore_open})
    if ignored:
        record["ignored_open_orders"] = ignored
    still_open = [o for o in open_rows if (o.order_id or "").strip() not in ignore_open]
    if still_open:
        ids = ", ".join(str(o.order_id) for o in still_open)
        raise Refusal(
            f"{CHECK_THE_ACCOUNT}: the account already has open order(s) {ids}; "
            "only one open order at a time"
        )
    if test_budget is not None and test_budget.used >= test_budget.limit:
        raise Refusal(f"order cap per test reached: at most {test_budget.limit} orders")
    budget.reserve()
    if test_budget is not None:
        test_budget.reserve()
    record["orders_sent_this_run"] = budget.used

    outcome = OrderOutcome()
    order_id: str | None = None
    place_mark = _mark(responses)  # bodies recorded before this call are never attributed
    try:
        order_id = client.place_order(**plan.place_kwargs())
    except RequestValidationError as exc:
        record["place"] = {"outcome": "not sent (client-side validation)", **_error_record(exc)}
        raise AssertionError(
            f"order failed client-side validation; nothing was sent: {exc}"
        ) from exc
    except APIError as exc:  # stat Not_Ok: a definitive answer
        record["place"] = {"outcome": "rejected", **_error_record(exc)}
        outcome.exception = type(exc).__name__
        outcome.definitive_rejection = True
    except Exception as exc:  # OrderStatusUnknownError, network errors: it may exist
        record["place"] = {"outcome": "unknown", **_error_record(exc)}
        outcome.exception = type(exc).__name__
    except BaseException as exc:  # Ctrl-C, SystemExit, a test timeout: it may exist
        _interrupted_during_place(client, plan, record, exc, responses, place_mark, recorder)
        raise
    else:
        record["place"] = {"outcome": "acknowledged", "order_id": order_id}
    _attach_response(record["place"], responses, "PlaceOrder", place_mark)
    # From here on the order is never sent again, whatever happens.

    order: Order | None = None
    status: str | None = None
    seen: list[Order] = []  # every order-book row seen for this order (fill check)
    try:
        deadline = clock() + settle_seconds
        read_ok = False
        while True:
            try:
                book = client.orders()
            except Exception as exc:
                record.setdefault("order_book_errors", []).append(type(exc).__name__)
            else:
                read_ok = True
                found = _find(book, order_id, plan.tag)
                if found is not None:
                    order = found
                    seen.append(found)
                    order_id = (found.order_id or "").strip() or order_id
                    status = status_of(found)
                    if is_final(status):
                        break
                elif outcome.definitive_rejection:
                    break  # rejected before reaching the book
            if clock() >= deadline:
                break
            sleep(poll_interval)
        if order is not None:
            record["order_book"] = _order_row(order)
        if order_id is None and not outcome.definitive_rejection:
            why = "the order book could not be read" if not read_ok else "it is not in orders()"
            raise CheckTheAccount(
                f"{CHECK_THE_ACCOUNT}: order tag {plan.tag}: place outcome unknown and {why}"
            )
        if order is not None and order_id is not None and not is_final(status):
            outcome.was_open = True
            if on_open is not None:
                on_open(order, record)
    finally:
        if order_id is not None and not is_final(status):
            status, cancelled = _cancel_and_confirm(
                client,
                order_id,
                plan.tag,
                record,
                responses,
                seen,
                settle_seconds=cancel_settle_seconds,
                poll_interval=poll_interval,
                sleep=sleep,
                clock=clock,
            )
            outcome.cancelled = cancelled

    outcome.order_id = order_id
    outcome.final_status = status
    if order_id is None:
        return outcome
    history = _record_history(client, order_id, record)
    filled = status == FILLED or any(_has_fill(row) for row in [*seen, *history])
    filled = _trades_for_order(client, order_id, record) > 0 or filled
    if filled and not allow_fill:
        raise CheckTheAccount(f"{CHECK_THE_ACCOUNT}: order {order_id} was (partly) filled")
    outcome.filled = filled
    outcome.filled_quantity = final_fill(seen)
    return outcome


def fill_of(order: Order) -> int | None:
    """The row's ``fillshares`` (parsed, or from the raw row if parsing nulled it); ``None``.

    ``fillshares`` is the order's running total, never one trade's quantity.
    """
    if order.filled_quantity is not None:
        return order.filled_quantity
    raw = order.raw.get("fillshares")
    number = _decimal(raw) if isinstance(raw, str) else None
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def final_fill(rows: Sequence[Order]) -> int | None:
    """The filled quantity on the FINAL of ``rows``; ``None`` when that row has no usable value.

    Never an earlier row's: an earlier running total may be smaller than the final one.
    """
    return fill_of(rows[-1]) if rows else None


def _has_fill(order: Order) -> bool:
    """``fillshares`` > 0 (parsed, or from the raw row if parsing nulled it)."""
    quantity = fill_of(order)
    if quantity is not None:
        return quantity > 0
    raw = order.raw.get("fillshares")
    number = _decimal(raw) if isinstance(raw, str) else None
    return number is not None and number > 0


def _trades_for_order(client: TradeSmart, order_id: str, record: dict[str, Any]) -> int:
    """One ``trades()`` call after settle: how many trades list this order id."""
    try:
        trades = client.trades()
    except Exception as exc:
        record["trades_error"] = _error_record(exc)
        return 0
    count = sum(1 for t in trades if (t.order_id or "").strip() == order_id)
    record["trades_for_order"] = count
    return count


def _interrupted_during_place(
    client: TradeSmart,
    plan: OrderPlan,
    record: dict[str, Any],
    exc: BaseException,
    responses: ResponseLog | None,
    since: int,
    recorder: Recorder | None,
) -> None:
    """Report loudly; unless it was Ctrl-C, one lookup by tag and at most one cancel."""
    alert = (
        f"{CHECK_THE_ACCOUNT}: order with tag {plan.tag} may exist; it was interrupted during "
        f"placement ({type(exc).__name__})"
    )
    print(alert, file=sys.stderr, flush=True)
    entry: dict[str, Any] = {
        "outcome": "interrupted",
        "exception": type(exc).__name__,
        "tag": plan.tag,
        "alert": alert,
    }
    record["place"] = entry
    if not isinstance(exc, KeyboardInterrupt):
        try:
            found = _find(client.orders(), None, plan.tag)
        except Exception as lookup_exc:
            entry["lookup"] = {"error": type(lookup_exc).__name__}
        else:
            entry["lookup"] = (
                None if found is None else {"order_id": found.order_id, "status": status_of(found)}
            )
            found_id = None if found is None else (found.order_id or "").strip()
            if found is not None and found_id and not is_final(status_of(found)):
                cancel_mark = _mark(responses)
                try:
                    client.cancel_order(found_id)
                except Exception as cancel_exc:
                    entry["cancel"] = {"outcome": "error", **_error_record(cancel_exc)}
                else:
                    entry["cancel"] = {"outcome": "accepted"}
                _attach_response(entry["cancel"], responses, "CancelOrder", cancel_mark)
    _attach_response(entry, responses, "PlaceOrder", since)
    if recorder is not None:
        with contextlib.suppress(Exception):  # reporting must not hide the interruption
            recorder.write(
                {"test": record.get("test", "run_order"), "segment": plan.segment_id, **entry}
            )


def _cancel_and_confirm(
    client: TradeSmart,
    order_id: str,
    tag: str,
    record: dict[str, Any],
    responses: ResponseLog | None,
    seen: list[Order],
    *,
    settle_seconds: float,
    poll_interval: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> tuple[str | None, bool]:
    """One cancel request (never retried), then wait for a final state or fail loudly."""
    accepted = False
    cancel_mark = _mark(responses)
    try:
        client.cancel_order(order_id)
    except Exception as exc:
        record["cancel"] = {"outcome": "error", **_error_record(exc)}
    else:
        accepted = True
        record["cancel"] = {"outcome": "accepted"}
    _attach_response(record["cancel"], responses, "CancelOrder", cancel_mark)

    status: str | None = None
    read_ok = False
    deadline = clock() + settle_seconds
    while True:
        try:
            book = client.orders()
        except Exception as exc:
            record.setdefault("order_book_errors", []).append(type(exc).__name__)
        else:
            read_ok = True
            found = _find(book, order_id, tag)
            status = status_of(found)
            if found is not None:
                seen.append(found)
                record["order_book_after_cancel"] = _order_row(found)
            if is_final(status):
                return status, accepted
        if clock() >= deadline:
            break
        sleep(poll_interval)
    if not read_ok:
        raise CheckTheAccount(
            f"{CHECK_THE_ACCOUNT}: order {order_id}: status unreadable after the cancel request"
        )
    raise CheckTheAccount(
        f"{CHECK_THE_ACCOUNT}: order {order_id} is still {status or 'not visible'} after one "
        "cancel request"
    )


def _record_history(
    client: TradeSmart, order_id: str, record: dict[str, Any]
) -> list[OrderHistoryEntry]:
    """``order_history()`` entries in the order received (F14: is the newest first?)."""
    try:
        entries = client.order_history(order_id)
    except Exception as exc:
        record["history_error"] = _error_record(exc)
        return []
    record["history"] = [
        {
            "index": i,
            "status": status_of(e),
            "rejreason": e.reject_reason,
            "norentm": e.raw.get("norentm", _text(e.norentm)),
            "rpt": e.rpt,
            "raw": dict(e.raw),
        }
        for i, e in enumerate(entries)
    ]
    return entries


def modify_once(
    client: TradeSmart,
    plan: OrderPlan,
    order_id: str,
    new_price: Decimal,
    record: dict[str, Any],
    responses: ResponseLog | None,
    *,
    trigger_price: Decimal | None = None,
) -> None:
    """One ModifyOrder request (never retried); records the ack id and raw body (F9).

    The order keeps the plan's order type and validity; ``trigger_price`` (stop orders) is sent
    only when given.
    """
    entry: dict[str, Any] = {"order_id": order_id, "new_price": new_price}
    extra: dict[str, Any] = {}
    if trigger_price is not None:
        extra["trigger_price"] = trigger_price
        entry["new_trigger_price"] = trigger_price
    modify_mark = _mark(responses)
    try:
        returned = client.modify_order(
            order_id,
            exchange=plan.exchange,
            tradingsymbol=plan.tradingsymbol,
            quantity=plan.quantity,
            price=new_price,
            order_type=plan.order_type,
            product=plan.product,
            validity=plan.validity,
            **extra,
        )
    except APIError as exc:
        entry.update(outcome="rejected", **_error_record(exc))
    except Exception as exc:
        entry.update(outcome="unknown", **_error_record(exc))
    else:
        entry.update(outcome="acknowledged", returned_order_id=returned)
    _attach_response(entry, responses, "ModifyOrder", modify_mark)
    record["modify"] = entry


def rejected_order_of_suite(book: list[Order], segment_id: str) -> str | None:
    """Latest REJECTED order placed by this suite for ``segment_id`` (by tag prefix)."""
    prefix = f"{TAG_PREFIX}{segment_id}-"
    found = [
        o
        for o in book
        if status_of(o) == "REJECTED" and (o.tag or "").startswith(prefix) and o.order_id
    ]
    return str(found[-1].order_id) if found else None


def cancel_rejected_once(
    client: TradeSmart,
    order_id: str,
    record: dict[str, Any],
    responses: ResponseLog | None,
) -> BaseException | None:
    """One CancelOrder for an already rejected order; returns what it raised (if anything)."""
    record["cancel_target"] = order_id
    error: BaseException | None = None
    cancel_mark = _mark(responses)
    try:
        client.cancel_order(order_id)
    except Exception as exc:
        error = exc
        record["cancel"] = {"outcome": "error", **_error_record(exc)}
    else:
        record["cancel"] = {"outcome": "accepted"}
    _attach_response(record["cancel"], responses, "CancelOrder", cancel_mark)
    try:
        found = _find(client.orders(), order_id, "\0")
    except Exception as exc:
        record["status_after_cancel_error"] = type(exc).__name__
    else:
        record["status_after_cancel"] = status_of(found)
    return error


@dataclass(frozen=True)
class PlacedOrder:
    """An order this run sent (its order id when known), for the tag checks."""

    segment_id: str
    tag: str
    order_id: str | None


@dataclass
class OrderSession:
    """Per-run state shared by the live order tests."""

    client: TradeSmart
    budget: OrderBudget = field(default_factory=OrderBudget)
    recorder: Recorder = field(default_factory=Recorder)
    responses: ResponseLog = field(default_factory=ResponseLog)
    rejected: dict[str, str] = field(default_factory=dict)  # segment id -> rejected order id
    raw_bodies: bool = False  # whether ResponseLog is attached to the client's http client
    uat: bool = False  # UAT mode: no zero-funds check (see open_session)
    placed: list[PlacedOrder] = field(default_factory=list)  # filled by the UAT lifecycle tests
    test_budget: OrderBudget | None = None  # the running UAT lifecycle test's own cap
    funded: bool = False  # funded mode: balance ceiling instead of zero funds (see open_session)
    max_balance: Decimal | None = None  # funded mode's balance ceiling
    money: MoneyBudget | None = None  # funded mode's money budget (see open_session)
    fills: dict[str, int] = field(default_factory=dict)  # tradingsymbol -> bought, not yet sold
    stage1_failed: bool = False  # funded mode: a stage-1 test failed (stage 2 must not run)
    stopped: bool = False  # funded mode: a CHECK THE ACCOUNT stopped the run (no more orders)
    ignored_open: frozenset[str] = frozenset()  # UAT only: orders stuck open (F26), never touched


def open_session(client: TradeSmart, environ: Mapping[str, str]) -> OrderSession:
    """The run's session, judged on the client's own REST base URL (where orders go).

    Production keeps every production rule; UAT mode gets the cap of 40 and no zero-funds
    check; funded mode (checked first: any failed funded check refuses) gets the cap of 10, the
    balance ceiling and a fresh money budget (``funded.MoneyBudget``); any other environment is
    refused before any API call.
    """
    from tests.live_orders.funded import MoneyBudget  # here: funded.py imports this module

    base_url = client.config.base_url
    reason = funded_refusal(environ, base_url)
    if reason is not None:
        raise Refusal(reason)
    reason = environment_refusal(environ, base_url)
    if reason is not None:
        raise Refusal(reason)
    ignored = ignored_open_orders(environ, base_url)  # refuses outside UAT mode
    if funded_mode(environ, base_url):
        return OrderSession(
            client,
            budget=OrderBudget(FUNDED_MAX_ORDERS_PER_RUN),
            funded=True,
            max_balance=max_balance(environ),
            money=MoneyBudget(),
        )
    uat = uat_mode(environ, base_url)
    return OrderSession(
        client, budget=OrderBudget(max_orders_per_run(uat)), uat=uat, ignored_open=ignored
    )
