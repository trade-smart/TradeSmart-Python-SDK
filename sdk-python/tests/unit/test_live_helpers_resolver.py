"""Unit tests: expiry instants, memoised contract resolution and redaction of the live suite."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from tests.live import _helpers
from tests.live._helpers import (
    Case,
    ContractResolver,
    ContractUnavailable,
    nearest_future,
    nearest_option,
    redact,
    resolve_or_skip,
)
from tests.unit.test_live_helpers import ROWS, TODAY, _no_price, _row
from tradesmart.errors import ConfigurationError
from tradesmart.models.base import IST

# --------------------------------------------------------------------------- expiry instant

EXPIRY_ROWS = [
    _row("NIFTYTODAYFUT", "FUTIDX", "10-NOV-2026", exch="NFO", sym="NIFTY"),
    _row("NIFTYNEXTFUT", "FUTIDX", "17-NOV-2026", exch="NFO", sym="NIFTY"),
    _row("NIFTYTODAYC", "OPTIDX", "10-NOV-2026", exch="NFO", sym="NIFTY", opt="CE", strike="100"),
    _row("NIFTYNEXTC", "OPTIDX", "17-NOV-2026", exch="NFO", sym="NIFTY", opt="CE", strike="100"),
    _row("CRUDETODAYFUT", "FUTCOM", "10-NOV-2026"),
    _row("CRUDENEXTFUT", "FUTCOM", "19-NOV-2026"),
    _row("CRUDETODAYC", "OPTFUT", "10-NOV-2026", opt="CE", strike="100"),
    _row("CRUDENEXTC", "OPTFUT", "19-NOV-2026", opt="CE", strike="100"),
]


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 11, 10, hour, minute, tzinfo=IST)


@pytest.mark.parametrize(
    ("exchange", "symbol", "hour", "minute", "future", "option"),
    [
        ("NFO", "NIFTY", 15, 29, "NIFTYTODAYFUT", "NIFTYTODAYC"),
        ("NFO", "NIFTY", 15, 31, "NIFTYNEXTFUT", "NIFTYNEXTC"),
        ("MCX", "CRUDEOIL", 23, 29, "CRUDETODAYFUT", "CRUDETODAYC"),
        ("MCX", "CRUDEOIL", 23, 31, "CRUDENEXTFUT", "CRUDENEXTC"),
        ("MCX", "CRUDEOIL", 15, 31, "CRUDETODAYFUT", "CRUDETODAYC"),  # MCX still open
    ],
)
def test_finders_use_the_expiry_instant(
    monkeypatch: pytest.MonkeyPatch,
    exchange: str,
    symbol: str,
    hour: int,
    minute: int,
    future: str,
    option: str,
) -> None:
    monkeypatch.setattr(_helpers, "_now", lambda: _at(hour, minute))
    fut = nearest_future(EXPIRY_ROWS, exchange, symbol)
    opt = nearest_option(EXPIRY_ROWS, exchange, symbol)
    assert fut is not None and fut.tradingsymbol == future
    assert opt is not None and opt.tradingsymbol == option


def test_finders_take_an_explicit_now() -> None:
    fut = nearest_future(EXPIRY_ROWS, "NFO", "NIFTY", _at(15, 31))
    assert fut is not None and fut.tradingsymbol == "NIFTYNEXTFUT"


def test_matches_requires_an_exact_exchange() -> None:
    blank = [_row("X", "FUTCOM", "19-NOV-2026", sym="X", exch="")]
    assert nearest_future(blank, "MCX", "X", TODAY) is None
    other = [_row("X", "FUTCOM", "19-NOV-2026", sym="X", exch="NFO")]
    assert nearest_future(other, "MCX", "X", TODAY) is None


# --------------------------------------------------------------------------- resolver

MCX_FUT = Case("mcx", "MCX", "fut", ("CRUDEOIL",))
MCX_OPT = Case("mcx", "MCX", "opt", ("CRUDEOIL",))


def _counting_rows(
    calls: list[str], error: BaseException | None = None
) -> Callable[[str], list[dict[str, str]]]:
    def rows_for(exchange: str) -> list[dict[str, str]]:
        calls.append(exchange)
        if error is not None:
            raise error
        return ROWS

    return rows_for


def _resolver(
    rows_for: Callable[[str], list[dict[str, str]]],
    last_price: Callable[[str, str], Decimal | None] = _no_price,
    failures: list[tuple[str, str]] | None = None,
) -> ContractResolver:
    sink = failures if failures is not None else []
    return ContractResolver(
        rows_for,
        last_price,
        lambda _t: [],
        now=lambda: TODAY,
        on_failure=lambda key, message: sink.append((key, message)),
    )


@pytest.mark.parametrize(
    "error",
    [ConfigurationError("no login"), ValueError("bad row"), httpx.ConnectError("down")],
)
def test_rows_failure_is_memoised_and_recorded_once(error: BaseException) -> None:
    calls: list[str] = []
    failures: list[tuple[str, str]] = []
    resolver = _resolver(_counting_rows(calls, error), failures=failures)
    for case in (MCX_FUT, MCX_OPT, MCX_FUT):
        with pytest.raises(ContractUnavailable) as caught:
            resolver.resolve(case)
        assert "could not resolve contract" in str(caught.value)
        assert type(error).__name__ in str(caught.value)
    assert calls == ["MCX"]  # the failing download is not retried per test
    assert len(failures) == 1


def test_quote_failure_is_memoised_per_case() -> None:
    quotes: list[str] = []
    failures: list[tuple[str, str]] = []

    def last_price(_e: str, token: str) -> Decimal | None:
        quotes.append(token)
        raise httpx.ReadTimeout("slow")

    resolver = _resolver(_counting_rows([]), last_price, failures)
    for _ in range(3):
        with pytest.raises(
            ContractUnavailable, match="mcx-opt: could not resolve contract: ReadTimeout"
        ):
            resolver.resolve(MCX_OPT)
    assert len(quotes) == 1
    assert len(failures) == 1
    assert resolver.resolve(MCX_FUT).kind == "fut"  # other cases are unaffected


def test_success_is_memoised() -> None:
    calls: list[str] = []
    resolver = _resolver(_counting_rows(calls))
    first = resolver.resolve(MCX_FUT)
    assert resolver.resolve(MCX_FUT) is first
    resolver.resolve(MCX_OPT)
    assert calls == ["MCX"]


def test_not_found_is_unavailable_and_memoised() -> None:
    failures: list[tuple[str, str]] = []
    resolver = _resolver(lambda _e: [], failures=failures)
    for _ in range(2):
        with pytest.raises(ContractUnavailable, match="no fut contract"):
            resolver.resolve(MCX_FUT)
    assert len(failures) == 1


def test_unexpected_errors_are_not_swallowed() -> None:
    resolver = _resolver(_counting_rows([], RuntimeError("bug")))
    with pytest.raises(RuntimeError):
        resolver.resolve(MCX_FUT)


def test_resolve_or_skip_converts_to_a_skip() -> None:
    resolver = _resolver(_counting_rows([], ValueError("bad row")))
    with pytest.raises(pytest.skip.Exception) as skipped:
        resolve_or_skip(resolver, MCX_FUT)
    assert "mcx-fut: could not resolve contract: ValueError: bad row" in str(skipped.value)
    assert resolve_or_skip(_resolver(_counting_rows([])), MCX_FUT).kind == "fut"


# --------------------------------------------------------------------------- redact


def test_redact_exact_secret_keys_only() -> None:
    data = {
        "access_token": "a",
        "susertoken": "b",
        "Secret_Key": "c",
        "api_key": "d",
        "apikey": "e",
        "app_key": "f",
        "checksum": "g",
        "code": "h",
        "Authorization": "Bearer xyz",
        "password": "p",
    }
    assert set(redact(data).values()) == {"<redacted>"}


def test_redact_keeps_instrument_tokens_and_look_alike_keys() -> None:
    data: dict[str, Any] = {
        "Token": "12345",
        "token": "12345",
        "token_kind": "AccessToken",
        "tokens": 3,
        "exchange_code": "MCX",
        "decode": "x",
        "keyword": "k",
        "first_row": {"Token": "999", "TradingSymbol": "X"},
    }
    assert redact(data) == data


def test_redact_token_with_secret_looking_value() -> None:
    assert redact({"token": "Bearer abc"}) == {"token": "<redacted>"}
    assert redact({"token": "x" * 40}) == {"token": "<redacted>"}
    assert redact({"Token": "y" * 40}) == {"Token": "<redacted>"}
