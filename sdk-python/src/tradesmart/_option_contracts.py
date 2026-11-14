"""Option contracts, their underlying future, and the clients' option helpers (no I/O here)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from tradesmart.analytics import BuildupReading, ChainRow, OptionChainSnapshot, classify_buildup
from tradesmart.errors import APIError, NotFoundError, ResponseParseError
from tradesmart.greeks import OptionGreeks, _num, _nums, greeks_from_price
from tradesmart.models.base import IST
from tradesmart.models.market import Candle, OptionContract, Quote

EXPIRY_CLOSE: dict[str, time] = {
    "NFO": time(15, 30),
    "BFO": time(15, 30),
    "CDS": time(12, 30),
    "BCD": time(12, 30),
    "MCX": time(23, 30),
}

_SECONDS_PER_YEAR = 365 * 86400

_OPTIONS_ON_FUTURES = frozenset({"MCX"})
"""Exchanges whose options are written on the future itself (Black-76 on it, no carry)."""

_STRIKE_COLUMNS = ("StrikePrice", "Strike")
"""Strike column names, in order: NFO and MCX write ``StrikePrice``, BFO writes ``Strike``."""


@dataclass(frozen=True)
class OptionContractInfo:
    exchange: str
    tradingsymbol: str
    token: str
    symbol: str
    expiry: date
    option_type: str
    strike: Decimal
    tick_size: Decimal | None
    lot_size: int | None


@dataclass(frozen=True)
class UnderlyingFuture:
    tradingsymbol: str
    token: str
    expiry: date
    same_expiry: bool


def _parse_expiry(value: str) -> date:
    return datetime.strptime(value.strip().title(), "%d-%b-%Y").date()


def _opt_decimal(value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        return None


def _opt_int(value: str | None) -> int | None:
    """Blank -> ``None``; fractions truncate; NaN/Infinity/garbage -> ``ValueError``."""
    if value is None or not value.strip():
        return None
    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        raise ValueError(f"invalid integer {value!r}") from None
    if not number.is_finite():
        raise ValueError(f"invalid integer {value!r}")
    return int(number)


def find_option(rows: Sequence[Mapping[str, str]], tradingsymbol: str) -> OptionContractInfo:
    """Find the option row for ``tradingsymbol`` (case-insensitive).

    The strike is read from ``StrikePrice`` when the row has that key (NFO, MCX), else from
    ``Strike`` (the BFO script master). A matching row that is malformed (missing/invalid
    ``Expiry``, strike, ``OptionType`` not CE/PE, or blank ``Symbol``) raises ``ValueError``.
    A fractional ``LotSize`` such as ``"75.5"`` is truncated to ``75``.
    """
    wanted = tradingsymbol.strip().upper()
    if not wanted:
        raise ValueError("tradingsymbol is required")
    for row in rows:
        if row.get("TradingSymbol", "").strip().upper() != wanted:
            continue
        if not row.get("Instrument", "").upper().startswith("OPT"):
            continue

        def bad(why: str) -> ValueError:
            return ValueError(f"malformed instrument row for {tradingsymbol}: {why}")

        symbol = row.get("Symbol", "").strip()
        if not symbol:
            raise bad("missing Symbol")
        if "OptionType" not in row:
            raise bad("missing OptionType")
        option_type = row["OptionType"].strip().upper()
        if option_type not in ("CE", "PE"):
            raise bad(f"OptionType {row.get('OptionType')!r} is not CE or PE")
        if "Expiry" not in row:
            raise bad("missing Expiry")
        try:
            expiry = _parse_expiry(row["Expiry"])
        except ValueError:
            raise bad(f"unparseable Expiry {row['Expiry']!r}") from None
        column = next((c for c in _STRIKE_COLUMNS if c in row), None)
        if column is None:
            raise bad("missing StrikePrice (or Strike)")
        try:
            strike = Decimal(row[column].strip())
        except InvalidOperation:
            raise bad(f"invalid {column} {row[column]!r}") from None
        if not strike.is_finite():
            raise bad(f"invalid {column} {row[column]!r}")
        try:
            lot_size = _opt_int(row.get("LotSize"))
        except ValueError:
            raise bad(f"invalid LotSize {row.get('LotSize')!r}") from None
        return OptionContractInfo(
            exchange=row.get("Exchange", "").strip().upper(),
            tradingsymbol=row["TradingSymbol"].strip(),
            token=row.get("Token", "").strip(),
            symbol=symbol,
            expiry=expiry,
            option_type=option_type,
            strike=strike,
            tick_size=_opt_decimal(row.get("TickSize")),
            lot_size=lot_size,
        )
    raise ValueError(f"option contract not found in instrument master: {tradingsymbol}")


def future_symbols(symbol: str) -> tuple[str, ...]:
    """The ``Symbol``s to look up the underlying future by, in order of preference.

    The option's own ``Symbol`` (trimmed, upper-cased) first; when it ends in ``OPT`` and has a
    stem, the same stem plus ``FUT`` as a fallback (BFO files SENSEX options under ``BSXOPT``
    and its futures under ``BSXFUT``).
    """
    own = symbol.strip().upper()
    if len(own) > 3 and own.endswith("OPT"):
        return (own, own[:-3] + "FUT")
    return (own,)


def _usable_futures(
    rows: Sequence[Mapping[str, str]], symbol: str, exchange: str
) -> list[tuple[date, Mapping[str, str]]]:
    futures: list[tuple[date, Mapping[str, str]]] = []
    for row in rows:
        if row.get("Symbol", "").strip().upper() != symbol:
            continue
        if not row.get("Instrument", "").upper().startswith("FUT"):
            continue
        if row.get("Exchange", "").strip().upper() != exchange:
            continue
        if not row.get("TradingSymbol", "").strip():
            continue
        try:
            futures.append((_parse_expiry(row["Expiry"]), row))
        except (KeyError, ValueError):
            continue
    return futures


def select_future(
    rows: Sequence[Mapping[str, str]], option: OptionContractInfo
) -> UnderlyingFuture:
    """Pick the underlying future: same expiry, else nearest later, else nearest earlier.

    Futures are looked up by the option's own ``Symbol``; only when none of those is usable and
    the ``Symbol`` ends in ``OPT`` are they looked up by the stem plus ``FUT``
    (``future_symbols``). Future rows with a missing or unparseable ``Expiry``, or a
    missing/blank ``TradingSymbol``, are skipped. When several rows share the chosen expiry,
    the first one in ``rows`` wins.
    """
    symbols = future_symbols(option.symbol)
    if not symbols[0]:
        raise ValueError(f"option {option.tradingsymbol} has a blank symbol; cannot find future")
    exchange = option.exchange.upper()
    futures: list[tuple[date, Mapping[str, str]]] = []
    for symbol in symbols:
        futures = _usable_futures(rows, symbol, exchange)
        if futures:
            break
    if not futures:
        tried = "" if len(symbols) == 1 else f" (tried {', '.join(symbols)})"
        raise ValueError(f"no underlying future found for symbol {symbols[0]}{tried}")
    same = [f for f in futures if f[0] == option.expiry]
    later = sorted((f for f in futures if f[0] > option.expiry), key=lambda f: f[0])
    earlier = sorted((f for f in futures if f[0] < option.expiry), key=lambda f: f[0], reverse=True)
    expiry, row = (same or later or earlier)[0]
    return UnderlyingFuture(
        tradingsymbol=row["TradingSymbol"].strip(),
        token=row.get("Token", "").strip(),
        expiry=expiry,
        same_expiry=expiry == option.expiry,
    )


@dataclass(frozen=True)
class ContractIndex:
    """Script-master lookups built once per parse (``build_contract_index``).

    ``options``: option rows by upper-cased ``TradingSymbol``; ``futures``: future rows by
    upper-cased (``Symbol``, ``Exchange``). Each keeps the script master's row order, so
    ``find_option`` / ``select_future`` on a bucket give what a scan of every row gives.
    ``future_rows`` returns the rows for every symbol of ``future_symbols`` (own symbol
    first), so ``select_future`` can apply its fallback. ``instruments``: the first row of
    every instrument (equity, future, option, ...) by upper-cased, trimmed ``TradingSymbol``
    (``instrument_row``, used by ``place_sliced_order``).
    """

    options: Mapping[str, tuple[Mapping[str, str], ...]]
    futures: Mapping[tuple[str, str], tuple[Mapping[str, str], ...]]
    instruments: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def option_rows(self, tradingsymbol: str) -> tuple[Mapping[str, str], ...]:
        return self.options.get(tradingsymbol.strip().upper(), ())

    def instrument_row(self, tradingsymbol: str) -> Mapping[str, str] | None:
        """The first row of any instrument with this ``TradingSymbol`` (trimmed, any case)."""
        return self.instruments.get(tradingsymbol.strip().upper())

    def future_rows(self, option: OptionContractInfo) -> tuple[Mapping[str, str], ...]:
        exchange = option.exchange.upper()
        return tuple(
            row
            for symbol in future_symbols(option.symbol)
            for row in self.futures.get((symbol, exchange), ())
        )


def build_contract_index(rows: Sequence[Mapping[str, str]]) -> ContractIndex:
    """Index a parsed script master: option and future rows, and every row by trading symbol."""
    options: dict[str, list[Mapping[str, str]]] = {}
    futures: dict[tuple[str, str], list[Mapping[str, str]]] = {}
    by_symbol: dict[str, Mapping[str, str]] = {}
    for row in rows:
        tradingsymbol = row.get("TradingSymbol", "").strip().upper()
        if tradingsymbol:
            by_symbol.setdefault(tradingsymbol, row)
        instrument = row.get("Instrument", "").upper()
        if instrument.startswith("OPT"):
            name = row.get("TradingSymbol", "").strip().upper()
            if name:
                options.setdefault(name, []).append(row)
        elif instrument.startswith("FUT"):
            key = (row.get("Symbol", "").strip().upper(), row.get("Exchange", "").strip().upper())
            futures.setdefault(key, []).append(row)
    return ContractIndex(
        options={k: tuple(v) for k, v in options.items()},
        futures={k: tuple(v) for k, v in futures.items()},
        instruments=by_symbol,
    )


def expiry_instant(exchange: str, expiry: date, expiry_time: time | None = None) -> datetime:
    """IST-aware expiry instant; ``expiry_time`` (naive, IST) overrides the exchange close."""
    if expiry_time is None:
        key = exchange.strip().upper()
        if key not in EXPIRY_CLOSE:
            raise ValueError(f"unknown exchange {exchange!r}: pass expiry_time explicitly")
        expiry_time = EXPIRY_CLOSE[key]
    elif expiry_time.tzinfo is not None:
        raise ValueError("expiry_time must be a naive time in IST")
    return datetime.combine(expiry, expiry_time, tzinfo=IST)


def years_between(start: datetime, end: datetime) -> float:
    """Year fraction on a 365-day basis; negative when ``end`` precedes ``start``."""
    return (end - start).total_seconds() / _SECONDS_PER_YEAR


def carry_adjusted_forward(
    future_price: float,
    rate: float,
    future_years: float,
    option_years: float,
    dividend_yield: float = 0.0,
) -> float:
    """Bring a future's price to the option's expiry: ``F * exp(-(r - q) * (Tf - To))``.

    ``q`` (``dividend_yield``) is the index's dividend yield, or the foreign interest rate for
    a currency pair; ``q = 0`` (the default) gives ``F * exp(-r * (Tf - To))``. Numbers may be
    ``float``, ``int`` or ``Decimal``.
    """
    future_price, rate, future_years, option_years, dividend_yield = _nums(
        "future_price rate future_years option_years dividend_yield",
        future_price,
        rate,
        future_years,
        option_years,
        dividend_yield,
    )
    return future_price * math.exp(-(rate - dividend_yield) * (future_years - option_years))


def _now() -> datetime:
    """Clock seam (IST-aware)."""
    return datetime.now(IST)


# --------------------------------------------------------------------------- client assembly
# Non-I/O steps shared by ``TradeSmart`` and ``AsyncTradeSmart``: each client method fetches
# the data and hands it to these functions.

PriceLike = Decimal | int | float


def check_rates(rate: object, dividend_yield: object) -> tuple[float, float]:
    """``rate`` and ``dividend_yield`` as finite floats (``Decimal``/``int`` accepted).

    Raises ``ValueError`` naming the parameter. Called before any quote is requested.
    """
    values: list[float] = []
    for name, value in (("rate", rate), ("dividend_yield", dividend_yield)):
        number = _num(value, name)
        if not (isinstance(number, float) and math.isfinite(number)):
            raise ValueError(f"{name} must be a finite number, got {value!r}")
        values.append(number)
    return values[0], values[1]


@dataclass(frozen=True)
class GreeksPlan:
    """What ``option_greeks()`` must fetch: ``future`` is quoted unless its price was given."""

    exchange: str
    option: OptionContractInfo
    future: UnderlyingFuture | None
    model: str


def option_has_expired(plan: GreeksPlan, expiry_time: time | None) -> bool:
    """True once the option's expiry instant is at or before now (the clock seam ``_now``).

    An expired option needs no quote of its own: the expired result uses only the underlying
    price.
    """
    return expiry_instant(plan.exchange, plan.option.expiry, expiry_time) <= _now()


def plan_option_greeks(
    rows: Sequence[Mapping[str, str]] | ContractIndex,
    exchange: str,
    tradingsymbol: str,
    *,
    underlying_price: PriceLike | None,
    spot_price: PriceLike | None,
) -> GreeksPlan:
    """Resolve the option and choose the model: ``"bsm"`` on a given spot, else ``"black76"``.

    ``rows`` is the script master or its ``ContractIndex``. The underlying future is selected
    only when no price was supplied for it.
    """
    if spot_price is not None and underlying_price is not None:
        raise ValueError(
            "pass spot_price (Black-Scholes-Merton) or underlying_price (Black-76), not both"
        )
    index = rows if isinstance(rows, ContractIndex) else build_contract_index(rows)
    option = find_option(index.option_rows(tradingsymbol), tradingsymbol)
    if spot_price is not None:
        return GreeksPlan(exchange, option, None, "bsm")
    future = select_future(index.future_rows(option), option) if underlying_price is None else None
    return GreeksPlan(exchange, option, future, "black76")


def _as_float(value: PriceLike | None, name: str) -> float | None:
    """A price as a float; ``None`` or non-finite -> ``None``.

    A ``bool``, an int too large for a float, or anything that is not a ``float``, ``int`` or
    ``Decimal`` raises ``ValueError`` naming the parameter (the rules of ``greeks_from_price``).
    """
    if value is None:
        return None
    number = _num(value, name)
    if not isinstance(number, float):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return number if math.isfinite(number) else None


def check_prices(
    *,
    option_price: object,
    underlying_price: object,
    spot_price: object,
) -> None:
    """Validate the prices given to ``option_greeks()`` before any request (``ValueError``)."""
    for name, value in (
        ("option_price", option_price),
        ("underlying_price", underlying_price),
        ("spot_price", spot_price),
    ):
        _as_float(value, name)  # type: ignore[arg-type]


def option_greeks_result(
    plan: GreeksPlan,
    *,
    option_price: PriceLike | None,
    underlying_price: PriceLike | None,
    rate: float,
    dividend_yield: float,
    expiry_time: time | None,
) -> OptionGreeks:
    """Greeks for ``plan`` from the fetched (or given) prices.

    ``underlying_price`` is the spot for ``"bsm"`` and the future's price for ``"black76"``. On
    NFO/BFO/CDS/BCD a future with a different expiry stands in for the index/spot forward and
    is brought to the option's expiry by cost of carry, ``F * exp(-(rate - dividend_yield) *
    (Tf - To))`` (``dividend_yield``: the index's dividend yield, or the foreign interest rate
    of a currency pair); an MCX option is an option on that future itself, so its price is
    the forward as it is.
    """
    rate, dividend_yield = check_rates(rate, dividend_yield)
    now = _now()
    expiry = expiry_instant(plan.exchange, plan.option.expiry, expiry_time)
    years = years_between(now, expiry)
    option = _as_float(option_price, "option_price")
    underlying = _as_float(
        underlying_price, "spot_price" if plan.model == "bsm" else "underlying_price"
    )
    forward: float | None = None
    if plan.model == "black76":
        forward = underlying
        future = plan.future
        carry = plan.exchange.strip().upper() not in _OPTIONS_ON_FUTURES
        if forward is not None and future is not None and not future.same_expiry and carry:
            future_years = years_between(
                now, expiry_instant(plan.exchange, future.expiry, expiry_time)
            )
            forward = _as_float(
                carry_adjusted_forward(forward, rate, future_years, years, dividend_yield),
                "forward",
            )
    tick = plan.option.tick_size
    greeks = greeks_from_price(
        option,
        forward if plan.model == "black76" else underlying,
        float(plan.option.strike),
        years,
        rate,
        plan.option.option_type,
        model=plan.model,
        dividend_yield=dividend_yield,
        tick_size=float(tick) if tick is not None and tick.is_finite() else 0.0,
    )
    return OptionGreeks(
        price=greeks.price,
        iv=greeks.iv,
        delta=greeks.delta,
        gamma=greeks.gamma,
        theta=greeks.theta,
        vega=greeks.vega,
        rho=greeks.rho,
        status=greeks.status,
        years_to_expiry=greeks.years_to_expiry,
        option_tradingsymbol=plan.option.tradingsymbol,
        option_price=option,
        underlying_tradingsymbol=plan.future.tradingsymbol if plan.future else None,
        underlying_price=underlying,
        forward=forward,
        same_expiry_future=plan.future.same_expiry if plan.future else None,
        model=plan.model,
        rate=rate,
        dividend_yield=dividend_yield,
        expiry=expiry,
        calculated_at=now,
    )


def is_contract_quote_failure(error: BaseException) -> bool:
    """True for a quote failure that concerns one contract only (it goes to ``missing``).

    Only a plain ``APIError`` (a ``Not_Ok`` for that contract), ``NotFoundError`` and
    ``ResponseParseError`` qualify. Authentication, rate-limit, server, configuration,
    validation and transport errors (and any other error) are not specific to one contract
    and must propagate.
    """
    return type(error) is APIError or isinstance(error, (NotFoundError, ResponseParseError))


def chain_snapshot(
    exchange: str,
    tradingsymbol: str,
    quoted: Sequence[tuple[OptionContract, Quote | None]],
) -> OptionChainSnapshot:
    """Build the snapshot; a contract without a quote (or unusable) goes to ``missing``.

    ``missing`` names it by tradingsymbol, else token, else ``"<strike> <type>"``.
    """
    rows: list[ChainRow] = []
    missing: list[str] = []
    for contract, quote in quoted:
        kind = (contract.option_type or "").strip().upper()
        if quote is None or contract.strike_price is None or kind not in ("CE", "PE"):
            strike = "?" if contract.strike_price is None else str(contract.strike_price)
            missing.append(contract.tradingsymbol or contract.token or f"{strike} {kind or '?'}")
            continue
        rows.append(
            ChainRow(
                strike=contract.strike_price,
                option_type=kind,
                tradingsymbol=contract.tradingsymbol or "",
                token=contract.token or "",
                last_price=quote.last_price,
                open_interest=quote.open_interest,
                volume=quote.volume,
                close=quote.close,
            )
        )
    rows.sort(key=lambda r: (r.strike, r.option_type))
    return OptionChainSnapshot(
        exchange=exchange,
        tradingsymbol=tradingsymbol,
        fetched_at=_now(),
        rows=tuple(rows),
        missing=tuple(missing),
    )


def since_open_window() -> tuple[datetime, datetime]:
    """Today 00:00 IST and now: the ``historical_data()`` window ``buildup()`` reads."""
    now = _now().astimezone(IST)
    return now.replace(hour=0, minute=0, second=0, microsecond=0), now


def buildup_reading(quote: Quote, candles: Sequence[Candle]) -> BuildupReading:
    """Price change since the previous close (``lp - c``) and OI change since today's open (the
    sum of ``intoi``); a missing piece is ``None``."""
    last, close = quote.last_price, quote.close
    price_change = last - close if last is not None and close is not None else None
    changes = [c.intoi for c in candles if c.intoi is not None]
    oi_change = sum(changes, Decimal(0)) if changes else None
    return BuildupReading(
        state=classify_buildup(price_change, oi_change),
        price_change=price_change,
        oi_change=oi_change,
        basis="since_open",
    )
