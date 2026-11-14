"""The UAT matrix's contracts: one per role, chosen once per run from the day's data (read-only).

Roles (owner answer U6): NSE ``ITC-EQ``, a low-priced small-tick stock and ``M&M-EQ``; BSE ``ITC``
(the F26 canary); NFO NIFTY and BANKNIFTY index futures with a CE and PE ladder (ATM, ITM 1-2,
OTM 1-2 strikes), one stock future and one stock option; BFO SENSEX future (``BSXFUT``) and ATM
options (``BSXOPT``, strike column ``Strike``); MCX CRUDEOIL, GOLD, GOLDM and GOLDPETAL futures.

Rules (inventory §3.1): futures and options take the nearest expiry **strictly after today**;
cash and stock roles try candidates in order and the first with a last price above 0 wins; an
option ladder is built from its future's last price and every rung quotes strikes (each at most
once, at most ``STRIKE_TRIES`` per rung) until one has a last price. Lot size comes from the
script master and must agree with ``quote().ls``, except on UAT (``uat=True``; its quotes report
``ls`` 1 for F&O, 2026-11-13): there the master lot is used and the quote's ``ls`` is kept in
``Contract.quote_lot_size``; a master row without a lot is refused everywhere. The tick comes from
the quote, else the master. A quote refused for rate (``exceeds Limit``) is read again after
0.5 s, 1 s and 2 s (``read_quote``; read-only).
Quantity is always in trade units, whole multiples of the lot size (``Contract.quantity``; MCX
too, owner answer U7). A role that cannot be resolved is listed in ``Contracts.unavailable`` with
its kind and reason, so its cases skip (Review Focus 4).

**Prices.** A contract identifies an instrument; it is not a price source. Its
``resolved_last_price`` / ``resolved_lower_circuit`` / ``resolved_upper_circuit`` are the
resolution-time snapshot (recorded, and used to pick candidates and strikes), never used to price
an order: ``contracts.json`` is reused on a resume later the same day, when they are stale. Every
order is priced from ``pricing.fresh_quote``, a quote taken just before placing.

``resolve_contracts`` writes ``<run>/contracts.json``; a resumed run of the same day reads it
back without any call. A file of another day, or one that does not cover every role, is refused;
one written before the open with every role unavailable is resolved again. An expired session
(``AuthenticationError``, ``ConfigurationError``) propagates: it is not a data gap.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import httpx

from tests.live_orders import safety
from tests.live_orders.safety import Refusal
from tests.live_orders.uat_matrix.quote_pacer import QuotePacer, minute_wait
from tradesmart.errors import APIError, AuthenticationError, ConfigurationError, TradeSmartError
from tradesmart.models.market import Quote

CONTRACTS_FILE = "contracts.json"
FILE_VERSION = 1
STRIKE_TRIES = 10  # strikes considered per ladder rung before the rung is given up
LADDER_RUNGS = ("atm", "itm1", "itm2", "otm1", "otm2")
ATM_ONLY = ("atm",)
# Low-priced NSE stocks with a 0.01 tick (first with a last price wins).
LOW_TICK_CANDIDATES = ("YESBANK-EQ", "IDEA-EQ", "SUZLON-EQ", "NHPC-EQ", "IRFC-EQ")
# Liquid stocks with derivatives (first whose nearest future has a last price wins).
STOCK_CANDIDATES = ("RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "ITC")
MCX_COMMODITIES = ("CRUDEOIL", "GOLD", "GOLDM", "GOLDPETAL")

# Why a role has no contract (each is its own known-issue key in ``expect``).
NO_LAST_PRICE = "no-last-price"
NOT_IN_MASTER = "not-in-master"
MASTER_UNAVAILABLE = "master-unavailable"
LOT_SIZE_MISMATCH = "lot-size-mismatch"
NO_TICK = "no-tick"
INCOMPLETE_ROW = "incomplete-row"
NO_UNDERLYING = "no-underlying"
UNAVAILABLE_KINDS = (
    NO_LAST_PRICE,
    NOT_IN_MASTER,
    MASTER_UNAVAILABLE,
    LOT_SIZE_MISMATCH,
    NO_TICK,
    INCOMPLETE_ROW,
    NO_UNDERLYING,
)

# A session that is not usable is a clear error, never a data gap.
SESSION_ERRORS = (AuthenticationError, ConfigurationError)
# Script-master or quote failures that are a data gap of the environment, not a bug.
DATA_ERRORS = (TradeSmartError, httpx.HTTPError, ValueError, OSError)
# A quote read refused for rate (UAT, 2026-11-13: "... exceeds Limit 10 for user") is read again
# after each of these waits (read-only, safe); any other error is unchanged.
QUOTE_RATE_WAITS = (0.5, 1.0, 2.0)
RATE_WORDS = "exceeds Limit"


class MarketReader(Protocol):
    """The two read-only client calls this module needs (``TradeSmart`` has both)."""

    def instruments(self, exchange: str) -> list[dict[str, str]]: ...

    def quote(self, exchange: str, token: str) -> Quote: ...


class QuoteReader(Protocol):
    def quote(self, exchange: str, token: str) -> Quote: ...


def is_rate_refusal(exc: BaseException) -> bool:
    """An ``APIError`` whose message says the per-second limit was exceeded (``exceeds Limit``)."""
    if not isinstance(exc, APIError):
        return False
    return RATE_WORDS in f"{exc} {exc.emsg or ''}"


def read_quote(
    client: QuoteReader,
    exchange: str,
    token: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    pacer: QuotePacer | None = None,
) -> Quote:
    """``client.quote(exchange, token)``, read again after 0.5 s, 1 s and 2 s while it is refused
    for rate (a read: safe to repeat); a ``current minute`` refusal waits for the next minute
    (``quote_pacer.minute_wait``, at most 60 s). Any other error, or the fourth rate refusal,
    propagates. ``pacer`` (UAT): every read waits on the session's quote pacer first."""
    for wait in QUOTE_RATE_WAITS:
        if pacer is not None:
            pacer.wait()
        try:
            return client.quote(exchange, token)
        except APIError as exc:
            if not is_rate_refusal(exc):
                raise
            message = f"{exc} {exc.emsg or ''}"
        sleep(pacer.refusal_wait(message, wait) if pacer is not None else rate_wait(message, wait))
    if pacer is not None:
        pacer.wait()
    return client.quote(exchange, token)


def rate_wait(message: str, default: float) -> float:
    """The wait after a rate refusal without a pacer: the minute's rest, else ``default``."""
    wait = minute_wait(message)
    return default if wait is None else wait


# -- the plan of roles ----------------------------------------------------------------------


@dataclass(frozen=True)
class CashRole:
    role: str
    exchange: str
    candidates: tuple[str, ...]  # exact trading symbols


@dataclass(frozen=True)
class FutureRole:
    role: str
    exchange: str
    instrument: str  # FUTIDX | FUTSTK | FUTCOM
    candidates: tuple[str, ...]  # script-master Symbol values


@dataclass(frozen=True)
class OptionRoles:
    prefix: str  # roles are "<prefix>-<ce|pe>-<rung>"
    exchange: str
    instrument: str  # OPTIDX | OPTSTK
    symbol: str | None  # options' Symbol; None: the future's Symbol (stock options)
    future_role: str
    option_types: tuple[str, ...] = ("CE", "PE")
    rungs: tuple[str, ...] = LADDER_RUNGS

    def roles(self) -> list[str]:
        return [
            f"{self.prefix}-{option_type.lower()}-{rung}"
            for option_type in self.option_types
            for rung in self.rungs
        ]


CASH: tuple[CashRole, ...] = (
    CashRole("nse-b", "NSE", ("ITC-EQ",)),
    CashRole("nse-t", "NSE", LOW_TICK_CANDIDATES),
    CashRole("nse-e", "NSE", ("M&M-EQ",)),
    CashRole("bse-b", "BSE", ("ITC",)),  # BSE cash symbols carry no "-EQ"
)
FUTURES: tuple[FutureRole, ...] = (
    FutureRole("nfo-nifty-f", "NFO", "FUTIDX", ("NIFTY",)),
    FutureRole("nfo-banknifty-f", "NFO", "FUTIDX", ("BANKNIFTY",)),
    FutureRole("nfo-stk-f", "NFO", "FUTSTK", STOCK_CANDIDATES),
    # BFO script master: SENSEX futures have Symbol BSXFUT, its options BSXOPT.
    FutureRole("bfo-sensex-f", "BFO", "FUTIDX", ("BSXFUT",)),
    *(FutureRole(f"mcx-{name.lower()}-f", "MCX", "FUTCOM", (name,)) for name in MCX_COMMODITIES),
)
OPTIONS: tuple[OptionRoles, ...] = (
    OptionRoles("nfo-nifty", "NFO", "OPTIDX", "NIFTY", "nfo-nifty-f"),
    OptionRoles("nfo-banknifty", "NFO", "OPTIDX", "BANKNIFTY", "nfo-banknifty-f"),
    OptionRoles("nfo-stk", "NFO", "OPTSTK", None, "nfo-stk-f", ("CE",), ATM_ONLY),
    OptionRoles("bfo-sensex", "BFO", "OPTIDX", "BSXOPT", "bfo-sensex-f", rungs=ATM_ONLY),
)
ROLES: tuple[str, ...] = (
    *(c.role for c in CASH),
    *(f.role for f in FUTURES),
    *(role for o in OPTIONS for role in o.roles()),
)


# -- contracts ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Unavailable:
    """Why a role has no contract: ``kind`` (one of ``UNAVAILABLE_KINDS``) and the reason."""

    kind: str
    reason: str


@dataclass(frozen=True)
class Contract:
    """An instrument of the run. The ``resolved_*`` values are the resolution-time snapshot:
    recorded only, never a price source (orders are priced from ``pricing.fresh_quote``)."""

    role: str
    exchange: str
    tradingsymbol: str
    token: str
    symbol: str  # script-master Symbol ("" for cash)
    kind: str  # "eq" | "fut" | "opt"
    lot_size: int
    tick_size: Decimal  # the quote's tick at resolution, else the master's (static per day)
    resolved_last_price: Decimal
    resolved_lower_circuit: Decimal | None = None
    resolved_upper_circuit: Decimal | None = None
    expiry: date | None = None
    strike: Decimal | None = None
    option_type: str | None = None
    # Options: strikes from the one nearest the reference (> 0 above it), and that reference
    # (the underlying future's last price at resolution).
    strikes_from_atm: int | None = None
    resolved_reference_price: Decimal | None = None
    # UAT only: the quote's ``ls`` when it disagreed with the master lot (the master lot is used).
    quote_lot_size: int | None = None

    def __post_init__(self) -> None:
        if not self.resolved_last_price > 0:
            raise ValueError(f"{self.role}: a contract needs a last price above 0")
        if not self.tick_size > 0 or self.lot_size < 1:
            raise ValueError(f"{self.role}: a contract needs a tick and a lot size")

    def quantity(self, lots: int = 1) -> int:
        """Order quantity in trade units: ``lots`` whole lots (MCX too, owner answer U7)."""
        if lots < 1:
            raise ValueError(f"{self.role}: at least one whole lot, got {lots} lots")
        return lots * self.lot_size

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, Decimal):
                data[key] = str(value)
            elif isinstance(value, date):
                data[key] = value.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Contract:
        def dec(key: str) -> Decimal | None:
            value = data.get(key)
            return None if value is None else Decimal(str(value))

        expiry = data.get("expiry")
        steps = data.get("strikes_from_atm")
        option_type = data.get("option_type")
        return cls(
            role=str(data["role"]),
            exchange=str(data["exchange"]),
            tradingsymbol=str(data["tradingsymbol"]),
            token=str(data["token"]),
            symbol=str(data["symbol"]),
            kind=str(data["kind"]),
            lot_size=int(data["lot_size"]),
            tick_size=Decimal(str(data["tick_size"])),
            resolved_last_price=Decimal(str(data["resolved_last_price"])),
            resolved_lower_circuit=dec("resolved_lower_circuit"),
            resolved_upper_circuit=dec("resolved_upper_circuit"),
            expiry=None if expiry is None else date.fromisoformat(str(expiry)),
            strike=dec("strike"),
            option_type=None if option_type is None else str(option_type),
            strikes_from_atm=None if steps is None else int(steps),
            resolved_reference_price=dec("resolved_reference_price"),
            quote_lot_size=None
            if data.get("quote_lot_size") is None
            else int(data["quote_lot_size"]),
        )


@dataclass
class Contracts:
    """The run's contracts by role, and why the other roles have none."""

    day: date
    contracts: dict[str, Contract] = field(default_factory=dict)
    unavailable: dict[str, Unavailable] = field(default_factory=dict)

    def get(self, role: str) -> Contract | None:
        return self.contracts.get(role)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": FILE_VERSION,
            "day": self.day.isoformat(),
            "contracts": {role: c.to_dict() for role, c in self.contracts.items()},
            "unavailable": {role: asdict(u) for role, u in self.unavailable.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Contracts:
        if data.get("version") != FILE_VERSION:
            raise Refusal(
                f"{CONTRACTS_FILE} has version {data.get('version')!r}, expected {FILE_VERSION}"
            )
        return cls(
            day=date.fromisoformat(str(data["day"])),
            contracts={role: Contract.from_dict(c) for role, c in data["contracts"].items()},
            unavailable={
                str(role): Unavailable(str(u["kind"]), str(u["reason"]))
                for role, u in data["unavailable"].items()
            },
        )


# -- reading the market ---------------------------------------------------------------------


class _Market:
    """Script masters (one read per exchange) and quotes (one per token) of one resolution."""

    def __init__(
        self,
        client: MarketReader,
        *,
        uat: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        pacer: QuotePacer | None = None,
    ) -> None:
        self._client = client
        self._pacer = pacer  # UAT: the session's quote pacer
        self.uat = uat  # UAT: the F&O / MCX lot from the master when quote().ls disagrees
        self._sleep = sleep
        self._rows: dict[str, list[dict[str, str]] | Unavailable] = {}
        self._quotes: dict[tuple[str, str], Quote | None] = {}

    def rows(self, exchange: str) -> list[dict[str, str]] | Unavailable:
        """The exchange's rows, or why there are none."""
        if exchange not in self._rows:
            try:
                rows = self._client.instruments(exchange)
            except SESSION_ERRORS:
                raise
            except DATA_ERRORS as exc:
                self._rows[exchange] = Unavailable(
                    MASTER_UNAVAILABLE,
                    f"the {exchange} script master is unavailable ({type(exc).__name__}: {exc})",
                )
            else:
                self._rows[exchange] = (
                    rows
                    if rows
                    else Unavailable(MASTER_UNAVAILABLE, f"the {exchange} script master is empty")
                )
        return self._rows[exchange]

    def quote(self, exchange: str, token: str) -> Quote | None:
        key = (exchange, token)
        if key not in self._quotes:
            try:
                self._quotes[key] = read_quote(
                    self._client, exchange, token, sleep=self._sleep, pacer=self._pacer
                )
            except SESSION_ERRORS:
                raise
            except DATA_ERRORS:
                self._quotes[key] = None  # no data: the same as no last price
        return self._quotes[key]


def _norm(value: str | None) -> str:
    return (value or "").strip().upper()


def _positive(value: Decimal | None) -> Decimal | None:
    return value if value is not None and value > 0 else None


def _build(
    role: str,
    exchange: str,
    kind: str,
    row: Mapping[str, str],
    quote: Quote | None,
    *,
    uat: bool = False,
    **option: Any,
) -> Contract | Unavailable:
    """The contract of ``row``, or why it cannot be used. On UAT (``uat``) an F&O / MCX lot
    that ``quote().ls`` contradicts is taken from the master (the quote's value is kept)."""
    tradingsymbol = (row.get("TradingSymbol") or "").strip()
    token = (row.get("Token") or "").strip()
    last = None if quote is None else _positive(quote.last_price)
    if last is None or quote is None:
        return Unavailable(NO_LAST_PRICE, f"{tradingsymbol}: no last price")
    lot = safety._lot_size(row.get("LotSize"))
    if lot is None or not tradingsymbol or not token:
        return Unavailable(
            INCOMPLETE_ROW, f"{tradingsymbol or token}: incomplete script-master row"
        )
    quote_lot = None
    if quote.lot_size is not None and quote.lot_size > 0 and quote.lot_size != lot:
        if uat and kind != "eq":
            quote_lot = quote.lot_size  # UAT quotes report ls 1 for F&O (2026-11-13)
        else:
            return Unavailable(
                LOT_SIZE_MISMATCH,
                f"{tradingsymbol}: lot size {lot} in the script master but {quote.lot_size} in "
                "quote().ls",
            )
    tick = _positive(quote.tick_size) or _positive(safety._decimal(row.get("TickSize")))
    if tick is None:
        return Unavailable(NO_TICK, f"{tradingsymbol}: no tick size in quote() or the master")
    is_option = kind == "opt"
    return Contract(
        role=role,
        exchange=exchange,
        tradingsymbol=tradingsymbol,
        token=token,
        symbol=(row.get("Symbol") or "").strip() if kind != "eq" else "",
        kind=kind,
        lot_size=lot,
        tick_size=tick,
        resolved_last_price=last,
        resolved_lower_circuit=_positive(quote.lc),
        resolved_upper_circuit=_positive(quote.uc),
        expiry=safety._expiry(row.get("Expiry")) if kind != "eq" else None,
        strike=safety._decimal(safety.strike_text(row)) if is_option else None,
        option_type=(_norm(row.get("OptionType")) or None) if is_option else None,
        quote_lot_size=quote_lot,
        **option,
    )


def _first_priced(
    market: _Market,
    role: str,
    exchange: str,
    kind: str,
    candidates: Sequence[tuple[str, Mapping[str, str] | None]],
) -> Contract | Unavailable:
    """The first candidate ``(name, row)`` that builds a contract; else every reason, with the
    kind of the first (preferred) candidate's failure."""
    failures: list[Unavailable] = []
    for name, row in candidates:
        if row is None:
            failures.append(Unavailable(NOT_IN_MASTER, f"{name}: not in the {exchange} master"))
            continue
        built = _build(
            role, exchange, kind, row, market.quote(exchange, row.get("Token", "")), uat=market.uat
        )
        if isinstance(built, Contract):
            return built
        failures.append(built)
    reasons = "; ".join(f.reason for f in failures)
    return Unavailable(
        failures[0].kind if failures else NOT_IN_MASTER,
        f"{role}: no usable {exchange} contract ({reasons})",
    )


def _within(role: str, value: Unavailable) -> Unavailable:
    return Unavailable(value.kind, f"{role}: {value.reason}")


def _cash(market: _Market, spec: CashRole) -> Contract | Unavailable:
    rows = market.rows(spec.exchange)
    if isinstance(rows, Unavailable):
        return _within(spec.role, rows)
    by_symbol: dict[str, dict[str, str]] = {}
    for row in rows:
        if _norm(row.get("Exchange", spec.exchange)) == spec.exchange:
            by_symbol.setdefault(_norm(row.get("TradingSymbol")), row)
    candidates = [(name, by_symbol.get(name.upper())) for name in spec.candidates]
    return _first_priced(market, spec.role, spec.exchange, "eq", candidates)


def _nearest(
    rows: Sequence[Mapping[str, str]],
    exchange: str,
    instrument: str,
    symbol: str,
    today: date,
    keep: Callable[[Mapping[str, str]], bool] = lambda row: True,
) -> list[Mapping[str, str]]:
    """Rows of ``instrument`` / ``symbol`` of the nearest expiry strictly after ``today``."""
    found: list[tuple[date, Mapping[str, str]]] = []
    for row in rows:
        if _norm(row.get("Exchange", exchange)) != exchange:
            continue
        if _norm(row.get("Instrument")) != instrument or _norm(row.get("Symbol")) != symbol:
            continue
        expiry = safety._expiry(row.get("Expiry"))
        if expiry is not None and expiry > today and keep(row):
            found.append((expiry, row))
    if not found:
        return []
    nearest = min(expiry for expiry, _ in found)
    return [row for expiry, row in found if expiry == nearest]


def _future(market: _Market, spec: FutureRole, today: date) -> Contract | Unavailable:
    rows = market.rows(spec.exchange)
    if isinstance(rows, Unavailable):
        return _within(spec.role, rows)
    candidates: list[tuple[str, Mapping[str, str] | None]] = []
    for symbol in spec.candidates:
        nearest = _nearest(rows, spec.exchange, spec.instrument, symbol, today)
        name = f"{spec.instrument} {symbol} (expiry after {today.isoformat()})"
        candidates.append((name, nearest[0] if nearest else None))
    return _first_priced(market, spec.role, spec.exchange, "fut", candidates)


def _strike(row: Mapping[str, str]) -> Decimal:
    """A row's strike (rows without one are filtered out before)."""
    return safety._decimal(safety.strike_text(row)) or Decimal(0)


def _is_option(option_type: str) -> Callable[[Mapping[str, str]], bool]:
    """A row filter: an ``option_type`` (CE/PE) row with a strike."""

    def keep(row: Mapping[str, str]) -> bool:
        return (
            _norm(row.get("OptionType")) == option_type
            and safety._decimal(safety.strike_text(row)) is not None
        )

    return keep


def _rung_order(rung: str, option_type: str, by_distance: Sequence[int]) -> list[int]:
    """Strike indices (of the strikes sorted by strike) a rung tries, in order.

    ``by_distance`` is every index, nearest the reference price first. ATM tries them in that
    order; ITM/OTM ``k`` starts ``k`` strikes from the nearest one and moves away from the money
    (a CE is in the money below the reference, a PE above).
    """
    if rung == "atm":
        return list(by_distance)
    steps = int(rung[3:])
    direction = -1 if rung.startswith("itm") == (option_type == "CE") else 1
    index = by_distance[0] + direction * steps
    indices: list[int] = []
    while 0 <= index < len(by_distance):
        indices.append(index)
        index += direction
    return indices


def _ladder(
    market: _Market,
    spec: OptionRoles,
    today: date,
    future: Contract | None,
    why: Unavailable | None,
) -> dict[str, Contract | Unavailable]:
    roles = spec.roles()
    if future is None:
        detail = "" if why is None else f" ({why.reason})"
        return {
            role: Unavailable(
                NO_UNDERLYING, f"{role}: no underlying future {spec.future_role}{detail}"
            )
            for role in roles
        }
    rows = market.rows(spec.exchange)
    if isinstance(rows, Unavailable):
        return {role: _within(role, rows) for role in roles}
    symbol = spec.symbol or future.symbol.upper()
    reference = future.resolved_last_price
    result: dict[str, Contract | Unavailable] = {}
    for option_type in spec.option_types:
        options = _nearest(
            rows, spec.exchange, spec.instrument, symbol, today, keep=_is_option(option_type)
        )
        strikes = sorted(options, key=_strike)
        by_distance = sorted(
            range(len(strikes)),
            key=lambda i: (abs(_strike(strikes[i]) - reference), _strike(strikes[i])),
        )
        used: set[str] = set()
        for rung in spec.rungs:
            role = f"{spec.prefix}-{option_type.lower()}-{rung}"
            if not strikes:
                result[role] = Unavailable(
                    NOT_IN_MASTER,
                    f"{role}: no {spec.instrument} {symbol} {option_type} of an expiry after "
                    f"{today.isoformat()} in the {spec.exchange} script master",
                )
                continue
            tried: list[str] = []
            failures: list[Unavailable] = []
            chosen: Contract | None = None
            for index in _rung_order(rung, option_type, by_distance):
                if len(tried) >= STRIKE_TRIES:
                    break
                row = strikes[index]
                token = (row.get("Token") or "").strip()
                if token in used:
                    continue
                tried.append((row.get("TradingSymbol") or token).strip())
                built = _build(
                    role,
                    spec.exchange,
                    "opt",
                    row,
                    market.quote(spec.exchange, token),
                    strikes_from_atm=index - by_distance[0],
                    resolved_reference_price=reference,
                    uat=market.uat,
                )
                if isinstance(built, Contract):
                    chosen = built
                    used.add(token)
                    break
                failures.append(built)
            if chosen is not None:
                result[role] = chosen
                continue
            if not tried:
                result[role] = Unavailable(
                    NOT_IN_MASTER, f"{role}: no {symbol} {option_type} strike left for {rung}"
                )
                continue
            result[role] = Unavailable(
                failures[0].kind,
                f"{role}: no {symbol} {option_type} strike with a usable quote near "
                f"{reference} ({failures[0].reason}; tried {', '.join(tried)})",
            )
    return result


# -- resolution -----------------------------------------------------------------------------


def _load(path: Path, today: date) -> Contracts | None:
    """The saved contracts of today; ``None`` when they must be resolved again (every role was
    unavailable, e.g. resolved before the open); ``Refusal`` when they cannot be reused."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refusal(f"cannot read {path.name} of the resumed run ({exc})") from exc
    if not isinstance(data, dict):
        raise Refusal(f"{path.name} of the resumed run is not a JSON object")
    loaded = Contracts.from_dict(data)
    if loaded.day != today:
        raise Refusal(
            f"{path.name} was resolved on {loaded.day.isoformat()}, not today "
            f"({today.isoformat()}): a resumed run must be on the same trading day; refusing"
        )
    saved = set(loaded.contracts) | set(loaded.unavailable)
    if saved != set(ROLES) or set(loaded.contracts) & set(loaded.unavailable):
        missing = sorted(set(ROLES) - saved)
        extra = sorted(saved - set(ROLES))
        raise Refusal(
            f"{path.name} of the resumed run does not match this suite's roles "
            f"(missing: {', '.join(missing) or 'none'}; unknown: {', '.join(extra) or 'none'}); "
            "refusing"
        )
    if not loaded.contracts:
        return None
    return loaded


def resolve_contracts(
    client: MarketReader,
    today: date,
    *,
    run_dir: Path | None = None,
    uat: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    pacer: QuotePacer | None = None,
) -> Contracts:
    """Every role's contract for ``today`` (read-only: script masters and quotes).

    With ``run_dir``: an existing ``contracts.json`` of today is reused without any call (resume);
    see the module docstring for the files that are refused or resolved again. Otherwise the
    result is written there.
    ``uat``: the UAT lot rule (module docstring); ``sleep``: the waits of a rate-refused quote.
    """
    path = None if run_dir is None else run_dir / CONTRACTS_FILE
    if path is not None and path.exists():
        loaded = _load(path, today)
        if loaded is not None:
            return loaded
    market = _Market(client, uat=uat, sleep=sleep, pacer=pacer)
    resolved: dict[str, Contract | Unavailable] = {}
    for cash in CASH:
        resolved[cash.role] = _cash(market, cash)
    for spec in FUTURES:
        resolved[spec.role] = _future(market, spec, today)
    for options in OPTIONS:
        underlying = resolved.get(options.future_role)
        if isinstance(underlying, Contract):
            resolved.update(_ladder(market, options, today, underlying, None))
        else:
            resolved.update(_ladder(market, options, today, None, underlying))
    result = Contracts(day=today)
    for role in ROLES:
        value = resolved[role]
        if isinstance(value, Contract):
            result.contracts[role] = value
        else:
            result.unavailable[role] = value
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    return result
