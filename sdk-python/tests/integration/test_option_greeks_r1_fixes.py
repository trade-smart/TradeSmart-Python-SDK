"""Round-1 review fixes for ``option_greeks()`` on both clients.

G1: the carry from a future of another expiry uses (rate - dividend_yield), so an index option
takes the dividend yield and a currency option the foreign interest rate. Also: prices are
checked before any request, and a script master that vanishes after its download is fetched
again instead of raising a raw ``FileNotFoundError``.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pytest
import respx

from tests.integration.test_option_greeks_client import (
    NOW,
    ROWS,
    AnyClient,
    _clock,  # noqa: F401  (autouse fixture)
    _row,
    any_client,  # noqa: F401  (fixture)
    call,
    mock_quotes,
    patch_instruments,
    years_to,
)
from tradesmart import AsyncTradeSmart, ClientConfig, TradeSmart
from tradesmart.errors import ResponseParseError
from tradesmart.greeks import black76
from tradesmart.models.base import IST

MONTHLY = date(2026, 11, 26)
WEEKLY = date(2026, 11, 19)


# --- G1: index option, later future, dividend yield ----------------------------------------


@respx.mock
async def test_index_option_carry_uses_rate_minus_dividend_yield(
    any_client: AnyClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_instruments(monkeypatch, any_client)
    mock_quotes({"1001": 24100, "2003": 180})

    g = await call(any_client, "option_greeks", "NFO", "NIFTY19NOV26C24000", dividend_yield=0.013)

    gap = years_to(MONTHLY) - years_to(WEEKLY)
    assert g.forward == pytest.approx(24100 * math.exp(-(0.065 - 0.013) * gap), rel=1e-12)
    assert g.same_expiry_future is False and g.model == "black76"
    assert g.dividend_yield == 0.013


@respx.mock
async def test_index_option_default_dividend_yield_keeps_the_rate_only_carry(
    any_client: AnyClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_instruments(monkeypatch, any_client)
    mock_quotes({"1001": 24100, "2003": 180})

    default = await call(any_client, "option_greeks", "NFO", "NIFTY19NOV26C24000")
    zero = await call(any_client, "option_greeks", "NFO", "NIFTY19NOV26C24000", dividend_yield=0)

    gap = years_to(MONTHLY) - years_to(WEEKLY)
    assert default.forward == 24100 * math.exp(-0.065 * gap)
    assert zero == default


@respx.mock
async def test_index_option_true_iv_is_recovered_for_call_and_put(
    any_client: AnyClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [*ROWS, _row("NIFTY19NOV26P24000", "2006", "OPTIDX", "19-NOV-2026", "PE", "24000")]
    patch_instruments(monkeypatch, any_client, rows)
    q, rate, iv = 0.013, 0.065, 0.12
    to, tf = years_to(WEEKLY), years_to(MONTHLY)
    spot = 24000.0
    future = spot * math.exp((rate - q) * tf)
    true_forward = spot * math.exp((rate - q) * to)
    call_price = black76(true_forward, 24000, to, rate, iv, "CE").price
    put_price = black76(true_forward, 24000, to, rate, iv, "PE").price
    mock_quotes({"1001": future, "2003": call_price, "2006": put_price})

    ce = await call(any_client, "option_greeks", "NFO", "NIFTY19NOV26C24000", dividend_yield=q)
    pe = await call(any_client, "option_greeks", "NFO", "NIFTY19NOV26P24000", dividend_yield=q)

    assert ce.iv == pytest.approx(iv, abs=1e-4) and pe.iv == pytest.approx(iv, abs=1e-4)


# --- G1: currency option, later future, foreign interest rate ------------------------------


def _cds(
    tsym: str, token: str, instrument: str, expiry: str, opt: str = "XX", strike: str = "-1"
) -> dict[str, str]:
    row = _row(tsym, token, instrument, expiry, opt, strike)
    row.update({"Exchange": "CDS", "Symbol": "USDINR", "LotSize": "1000", "TickSize": "0.0025"})
    return row


CDS_ROWS = [
    _cds("USDINR29DEC26F", "7001", "FUTCUR", "29-DEC-2026"),
    _cds("USDINR04DEC26C88.5", "7002", "OPTCUR", "04-DEC-2026", "CE", "88.5"),
    _cds("USDINR04DEC26P88.5", "7003", "OPTCUR", "04-DEC-2026", "PE", "88.5"),
]


def _cds_years(expiry: date) -> float:
    return (datetime.combine(expiry, time(12, 30), tzinfo=IST) - NOW).total_seconds() / (
        365 * 86400
    )


@respx.mock
async def test_currency_option_carry_uses_the_foreign_rate(
    any_client: AnyClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_instruments(monkeypatch, any_client, CDS_ROWS)
    rate, r_foreign, iv = 0.065, 0.045, 0.04
    to, tf = _cds_years(date(2026, 12, 4)), _cds_years(date(2026, 12, 29))
    future = 88.5 * math.exp((rate - r_foreign) * (tf - to))  # forward at the option's expiry: 88.5
    put_price = black76(88.5, 88.5, to, rate, iv, "PE").price
    call_price = black76(88.5, 88.5, to, rate, iv, "CE").price
    mock_quotes({"7001": future, "7002": call_price, "7003": put_price})

    pe = await call(
        any_client, "option_greeks", "CDS", "USDINR04DEC26P88.5", dividend_yield=r_foreign
    )
    ce = await call(
        any_client, "option_greeks", "CDS", "USDINR04DEC26C88.5", dividend_yield=r_foreign
    )

    assert pe.forward == pytest.approx(88.5, rel=1e-12)
    assert pe.status == "ok" and pe.iv == pytest.approx(iv, abs=1e-6)
    assert ce.status == "ok" and ce.iv == pytest.approx(iv, abs=1e-6)
    assert ce.delta == pytest.approx(pe.delta + math.exp(-rate * to), abs=1e-9)  # parity


# --- G1: no carry, so no dividend yield, on MCX or with a given underlying price ------------


@respx.mock
async def test_dividend_yield_is_ignored_without_carry(
    any_client: AnyClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_instruments(monkeypatch, any_client)
    mock_quotes({"1001": 24100, "2001": 300, "2003": 180})

    same = await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C24000", dividend_yield=0.02)
    given = await call(
        any_client,
        "option_greeks",
        "NFO",
        "NIFTY19NOV26C24000",
        underlying_price=24100,
        dividend_yield=0.02,
    )

    assert same.same_expiry_future is True and same.forward == 24100.0
    assert given.forward == 24100.0


# --- Minor 1: prices follow the numeric rules, checked before any request -------------------


@respx.mock
@pytest.mark.parametrize("name", ["option_price", "underlying_price", "spot_price"])
@pytest.mark.parametrize("bad", [True, 10**400])
async def test_bad_price_raises_value_error_before_any_request(
    any_client: AnyClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    bad: Any,
) -> None:
    patch_instruments(monkeypatch, any_client)
    route = mock_quotes({"1001": 24100, "2001": 300})

    with pytest.raises(ValueError, match=name):
        await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C24000", **{name: bad})

    assert route.call_count == 0


# --- Minor 5: a script master removed between download and parse is fetched again ----------


def _write_master(path: Path) -> Path:
    header = list(ROWS[0])
    lines = [",".join(header)] + [",".join(r.get(k, "") for k in header) for r in ROWS]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _vanishing_download(
    client: AnyClient, monkeypatch: pytest.MonkeyPatch, folder: Path, vanish: int
) -> list[int]:
    """The first ``vanish`` downloads return a path that no longer exists."""
    calls: list[int] = []
    master = folder / "NFO_symbols.txt"

    def path_for_call() -> Path:
        calls.append(1)
        if len(calls) <= vanish:
            master.unlink(missing_ok=True)
        else:
            _write_master(master)
        return master

    if isinstance(client, AsyncTradeSmart):

        async def adownload(exchange: Any, *args: Any, **kw: Any) -> Path:
            return path_for_call()

        monkeypatch.setattr(client._instruments, "download", adownload)
    else:

        def download(exchange: Any, *args: Any, **kw: Any) -> Path:
            return path_for_call()

        monkeypatch.setattr(client._instruments, "download", download)
    return calls


@pytest.fixture(params=["sync", "async"])
async def real_client(request: pytest.FixtureRequest) -> Any:
    kwargs: dict[str, Any] = {"access_token": "test-access-token", "config": ClientConfig()}
    if request.param == "sync":
        client = TradeSmart("TEST123", **kwargs)
        yield client
        client.close()
    else:
        aclient = AsyncTradeSmart("TEST123", **kwargs)
        yield aclient
        await aclient.aclose()


@respx.mock
async def test_script_master_removed_after_download_is_downloaded_again(
    real_client: AnyClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloads = _vanishing_download(real_client, monkeypatch, tmp_path, vanish=1)
    mock_quotes({"1001": 24100, "2001": 300})

    g = await call(real_client, "option_greeks", "NFO", "NIFTY26NOV26C24000")

    assert g.status == "ok" and len(downloads) == 2


@respx.mock
async def test_script_master_removed_twice_raises_response_parse_error(
    real_client: AnyClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloads = _vanishing_download(real_client, monkeypatch, tmp_path, vanish=2)
    mock_quotes({"1001": 24100, "2001": 300})

    with pytest.raises(ResponseParseError, match="NFO_symbols.txt"):
        await call(real_client, "option_greeks", "NFO", "NIFTY26NOV26C24000")
    assert len(downloads) == 2


async def test_instruments_removed_after_download_is_downloaded_again(
    real_client: AnyClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloads = _vanishing_download(real_client, monkeypatch, tmp_path, vanish=1)

    rows = await call(real_client, "instruments", "NFO")

    assert rows[0]["TradingSymbol"] == ROWS[0]["TradingSymbol"] and len(downloads) == 2
