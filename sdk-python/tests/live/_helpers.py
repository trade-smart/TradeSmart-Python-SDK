"""Helpers of the live suite: result recording and dynamic contract discovery.

Nothing here touches the network; the discovery functions work on parsed script-master rows
(``list[dict[str, str]]``) and on callables handed in by the fixtures, so they are unit tested
with synthetic rows (``tests/unit/test_live_helpers.py``). No token or expiry is hard-coded:
every contract is found from the current script master.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest

from tests._environment import environment_name, is_uat
from tradesmart._option_contracts import expiry_instant
from tradesmart.errors import TradeSmartError
from tradesmart.models.base import IST

RESULTS_DIR = Path(__file__).resolve().parents[2] / ".live-results"
# Keys that are secrets or account ids (exact, case-insensitive): no test account id is ever
# recorded. ``Token`` / ``token`` alone is usually an instrument id (the script-master column), so
# it is redacted only when the value looks secret.
_SECRET_KEYS = frozenset(
    {
        "client_id",
        "user_id",
        "account_id",
        "uid",
        "actid",
        "access_token",
        "susertoken",
        "secret",
        "secret_key",
        "api_key",
        "apikey",
        "app_key",
        "checksum",
        "code",
        "auth_code",
        "authorization",
        "password",
    }
)
_LONG_VALUE = 24


def _secret_like(key: str, value: Any) -> bool:
    lowered = key.lower()
    if lowered in _SECRET_KEYS:
        return True
    if lowered == "token" and isinstance(value, str):
        return len(value) >= _LONG_VALUE or value.lower().startswith("bearer ")
    return False


def redact(value: Any) -> Any:
    """Copy of ``value`` with secret-valued keys replaced by ``"<redacted>"`` (see above)."""
    if isinstance(value, Mapping):
        return {
            str(k): ("<redacted>" if _secret_like(str(k), v) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def record(suite: str, test: str, results_dir: Path | None = None, **data: Any) -> Path:
    """Append one JSON line (observations of a test, no secrets) to ``<suite>.jsonl``.

    Each line carries ``environment``: the ``TRADESMART_ENV`` name (``production`` when unset).
    """
    directory = results_dir if results_dir is not None else RESULTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{suite}.jsonl"
    line = {"at": datetime.now(UTC).isoformat(), "test": test, **redact(data)}
    line["environment"] = environment_name(os.environ)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, default=str, sort_keys=True) + "\n")
    return path


# --------------------------------------------------------------------------- contracts


@dataclass(frozen=True)
class Case:
    """One segment/kind to test: ``symbols`` are tried in order until one is found."""

    segment: str  # "nse" | "nfo" | "mcx" (also the pytest marker)
    exchange: str
    kind: str  # "eq" | "fut" | "opt"
    symbols: tuple[str, ...]


@dataclass(frozen=True)
class Contract:
    exchange: str
    tradingsymbol: str
    token: str
    symbol: str
    kind: str
    expiry: date | None = None
    strike: Decimal | None = None
    option_type: str | None = None
    lot_size: int | None = None


CASES: tuple[Case, ...] = (
    Case("nse", "NSE", "eq", ("INFY", "RELIANCE")),
    Case("nfo", "NFO", "fut", ("NIFTY",)),
    Case("nfo", "NFO", "opt", ("NIFTY",)),
    Case("mcx", "MCX", "fut", ("CRUDEOIL", "GOLD")),
    Case("mcx", "MCX", "opt", ("CRUDEOIL", "GOLD")),
)


def case_id(case: Case) -> str:
    return f"{case.segment}-{case.kind}"


def parse_expiry(text: str | None) -> date | None:
    """``DD-MON-YYYY`` (any case) -> date; ``None`` when blank or unparseable."""
    if not text or not text.strip():
        return None
    try:
        return datetime.strptime(text.strip().title(), "%d-%b-%Y").date()
    except ValueError:
        return None


def _decimal(text: str | None) -> Decimal | None:
    try:
        value = Decimal((text or "").strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _int(text: str | None) -> int | None:
    value = _decimal(text)
    return int(value) if value is not None else None


def _matches(row: Mapping[str, str], exchange: str, symbol: str, prefix: str) -> bool:
    if row.get("Exchange", "").strip().upper() != exchange.upper():
        return False
    if row.get("Symbol", "").strip().upper() != symbol.upper():
        return False
    return row.get("Instrument", "").strip().upper().startswith(prefix)


def _contract(row: Mapping[str, str], exchange: str, kind: str) -> Contract | None:
    token = row.get("Token", "").strip()
    tradingsymbol = row.get("TradingSymbol", "").strip()
    if not token or not tradingsymbol:
        return None
    is_option = kind == "opt"
    return Contract(
        exchange=exchange.upper(),
        tradingsymbol=tradingsymbol,
        token=token,
        symbol=row.get("Symbol", "").strip(),
        kind=kind,
        expiry=parse_expiry(row.get("Expiry")),
        # NFO/MCX "StrikePrice", BFO "Strike"; the StrikePrice key decides when present (10.4).
        strike=_decimal(row.get("StrikePrice" if "StrikePrice" in row else "Strike"))
        if is_option
        else None,
        option_type=(row.get("OptionType", "").strip().upper() or None) if is_option else None,
        lot_size=_int(row.get("LotSize")),
    )


def _now() -> datetime:
    """Current time in IST (a seam for tests)."""
    return datetime.now(IST)


def _is_live(contract: Contract, now: datetime | None) -> bool:
    """True while the contract's expiry instant (exchange close on its expiry day) is ahead."""
    if contract.expiry is None:
        return False
    current = now if now is not None else _now()
    return expiry_instant(contract.exchange, contract.expiry) > current


def nearest_future(
    rows: Iterable[Mapping[str, str]], exchange: str, symbol: str, now: datetime | None = None
) -> Contract | None:
    """The future of ``symbol`` whose expiry instant is nearest and still ahead of ``now``."""
    found: list[Contract] = []
    for row in rows:
        if _matches(row, exchange, symbol, "FUT"):
            contract = _contract(row, exchange, "fut")
            if contract is not None and _is_live(contract, now):
                found.append(contract)
    return min(found, key=lambda c: (c.expiry or date.max, c.tradingsymbol), default=None)


def nearest_option(
    rows: Iterable[Mapping[str, str]],
    exchange: str,
    symbol: str,
    now: datetime | None = None,
    reference_price: Decimal | None = None,
    option_type: str = "CE",
) -> Contract | None:
    """An option of the nearest unexpired expiry, strike closest to the reference price.

    Without a ``reference_price`` the middle strike of that expiry is chosen.
    """
    same = _nearest_expiry_options(rows, exchange, symbol, now, option_type)
    if not same:
        return None
    if reference_price is None:
        return same[len(same) // 2]
    return options_by_distance(rows, exchange, symbol, now, reference_price, option_type)[0]


def _nearest_expiry_options(
    rows: Iterable[Mapping[str, str]],
    exchange: str,
    symbol: str,
    now: datetime | None,
    option_type: str,
) -> list[Contract]:
    """The ``option_type`` options of the nearest unexpired expiry, by strike."""
    options: list[Contract] = []
    for row in rows:
        if _matches(row, exchange, symbol, "OPT"):
            contract = _contract(row, exchange, "opt")
            if (
                contract is not None
                and _is_live(contract, now)
                and contract.strike is not None
                and contract.option_type == option_type.upper()
            ):
                options.append(contract)
    if not options:
        return []
    expiry = min(c.expiry for c in options if c.expiry is not None)
    return sorted((c for c in options if c.expiry == expiry), key=lambda c: c.strike or 0)


def options_by_distance(
    rows: Iterable[Mapping[str, str]],
    exchange: str,
    symbol: str,
    now: datetime | None,
    reference_price: Decimal,
    option_type: str = "CE",
) -> list[Contract]:
    """The nearest expiry's options, the strike closest to ``reference_price`` first."""
    same = _nearest_expiry_options(rows, exchange, symbol, now, option_type)
    return sorted(
        same, key=lambda c: (abs((c.strike or Decimal(0)) - reference_price), c.strike or 0)
    )


def find_equity(rows: Iterable[Mapping[str, str]], exchange: str, symbol: str) -> Contract | None:
    """The ``<SYMBOL>-EQ`` row of the script master."""
    wanted = f"{symbol.upper()}-EQ"
    for row in rows:
        if row.get("TradingSymbol", "").strip().upper() == wanted:
            return _contract(row, exchange, "eq")
    return None


def equity_from_search(matches: Sequence[Any], exchange: str, symbol: str) -> Contract | None:
    """Fallback: the ``<SYMBOL>-EQ`` match of ``search_instruments`` (duck-typed ScripMatch)."""
    wanted = f"{symbol.upper()}-EQ"
    for match in matches:
        if (
            (getattr(match, "exchange", None) or "").upper() == exchange.upper()
            and (getattr(match, "tradingsymbol", None) or "").upper() == wanted
            and getattr(match, "token", None)
        ):
            return Contract(exchange.upper(), match.tradingsymbol, match.token, symbol, "eq")
    return None


def resolve_contract(
    case: Case,
    rows_for: Callable[[str], Sequence[Mapping[str, str]]],
    last_price: Callable[[str, str], Decimal | None],
    search: Callable[[str], Sequence[Any]],
    now: datetime | None = None,
) -> Contract | None:
    """Find the contract of ``case`` (first of ``case.symbols`` that exists), or ``None``.

    ``rows_for(exchange)`` gives the script master, ``last_price(exchange, token)`` a quote's
    last price (or ``None``), ``search(text)`` the scrip-search matches.
    """
    rows = rows_for(case.exchange)
    for symbol in case.symbols:
        if case.kind == "eq":
            found = find_equity(rows, case.exchange, symbol) or equity_from_search(
                search(symbol), case.exchange, symbol
            )
        elif case.kind == "fut":
            found = nearest_future(rows, case.exchange, symbol, now)
        else:
            future = nearest_future(rows, case.exchange, symbol, now)
            reference = last_price(case.exchange, future.token) if future else None
            found = nearest_option(rows, case.exchange, symbol, now, reference)
        if found is not None:
            return found
    return None


class ContractUnavailable(Exception):
    """A contract could not be resolved (message names the case and the cause)."""


_RESOLVE_ERRORS = (TradeSmartError, ValueError, httpx.HTTPError)


class ContractResolver:
    """Resolves each case once; a failure is memoised, recorded once and never re-run.

    ``rows_for`` (script master), ``last_price`` and ``search`` do the I/O. ``TradeSmartError``,
    ``ValueError`` and ``httpx.HTTPError`` become :class:`ContractUnavailable`; anything else is a
    bug and propagates. ``on_failure(key, message)`` is called once per distinct cause.
    """

    def __init__(
        self,
        rows_for: Callable[[str], Sequence[Mapping[str, str]]],
        last_price: Callable[[str, str], Decimal | None],
        search: Callable[[str], Sequence[Any]],
        now: Callable[[], datetime] | None = None,
        on_failure: Callable[[str, str], None] | None = None,
    ) -> None:
        self._rows_for = rows_for
        self._last_price = last_price
        self._search = search
        self._now = now
        self._on_failure = on_failure
        self._rows: dict[str, Sequence[Mapping[str, str]]] = {}
        self._rows_failed: dict[str, BaseException] = {}
        self._found: dict[Case, Contract] = {}
        self._failed: dict[Case, ContractUnavailable] = {}

    def _report(self, key: str, message: str) -> None:
        if self._on_failure is not None:
            self._on_failure(key, message)

    def rows(self, exchange: str) -> Sequence[Mapping[str, str]]:
        """The script master of ``exchange``; downloaded (or failed) once."""
        if exchange in self._rows_failed:
            raise self._rows_failed[exchange]
        if exchange not in self._rows:
            try:
                self._rows[exchange] = self._rows_for(exchange)
            except _RESOLVE_ERRORS as error:
                self._rows_failed[exchange] = error
                self._report(
                    f"script_master:{exchange}",
                    f"{exchange}: {type(error).__name__}: {error}",
                )
                raise
        return self._rows[exchange]

    def resolve(self, case: Case) -> Contract:
        if case in self._found:
            return self._found[case]
        if case in self._failed:
            raise self._failed[case]
        try:
            found = resolve_contract(
                case,
                self.rows,
                self._last_price,
                self._search,
                self._now() if self._now is not None else None,
            )
        except _RESOLVE_ERRORS as error:
            problem = (
                f"{case_id(case)}: could not resolve contract: {type(error).__name__}: {error}"
            )
            # A script-master failure was already reported by rows(); report the rest once.
            self._fail(case, problem, report=error not in self._rows_failed.values())
        if found is None:
            self._fail(
                case,
                f"{case_id(case)}: no {case.kind} contract of "
                f"{'/'.join(case.symbols)} on {case.exchange}",
                report=True,
            )
        self._found[case] = found
        return found

    def _fail(self, case: Case, problem: str, *, report: bool) -> NoReturn:
        if report:
            self._report(case_id(case), problem)
        self._failed[case] = ContractUnavailable(problem)
        raise self._failed[case]


def resolve_or_skip(resolver: ContractResolver, case: Case) -> Contract:
    """The contract of ``case``, or ``pytest.skip`` with the reason it is unavailable."""
    try:
        return resolver.resolve(case)
    except ContractUnavailable as unavailable:
        pytest.skip(str(unavailable))


def skip_if_unavailable_in_uat() -> None:
    """Skip the running test when ``TRADESMART_ENV=uat``: that environment lacks the data."""
    if is_uat(os.environ):
        pytest.skip("not available in this test environment")
