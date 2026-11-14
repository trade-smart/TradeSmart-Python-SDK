"""``option_greeks()`` on both clients (spec A3, A5): respx-mocked quotes, synthetic instruments."""

from __future__ import annotations

import inspect
import json
import math
from collections.abc import AsyncIterator
from datetime import date, datetime, time
from typing import Any

import httpx
import pytest
import respx

from tests.conftest import BASE, fixture
from tradesmart import AsyncTradeSmart, ClientConfig, OptionGreeks, TradeSmart
from tradesmart import _option_contracts as oc
from tradesmart.errors import APIError, AuthenticationError
from tradesmart.greeks import black76
from tradesmart.models.base import IST

AnyClient = TradeSmart | AsyncTradeSmart

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=IST)
MONTHLY = date(2026, 11, 26)
WEEKLY = date(2026, 11, 19)


def _row(
    tsym: str, token: str, instrument: str, expiry: str, opt: str = "XX", strike: str = "-1"
) -> dict[str, str]:
    return {
        "Exchange": "NFO",
        "Token": token,
        "LotSize": "75",
        "Symbol": "NIFTY",
        "TradingSymbol": tsym,
        "Expiry": expiry,
        "Instrument": instrument,
        "OptionType": opt,
        "StrikePrice": strike,
        "TickSize": "0.05",
    }


ROWS = [
    _row("NIFTY26NOV26F", "1001", "FUTIDX", "26-NOV-2026"),
    _row("NIFTY31DEC26F", "1002", "FUTIDX", "31-DEC-2026"),
    _row("NIFTY26NOV26C24000", "2001", "OPTIDX", "26-NOV-2026", "CE", "24000"),
    _row("NIFTY26NOV26P24000", "2002", "OPTIDX", "26-NOV-2026", "PE", "24000"),
    _row("NIFTY19NOV26C24000", "2003", "OPTIDX", "19-NOV-2026", "CE", "24000"),
    _row("NIFTY-EQ", "3001", "EQ", "", "", ""),
]


@pytest.fixture(params=["sync", "async"])
async def any_client(request: pytest.FixtureRequest) -> AsyncIterator[AnyClient]:
    kwargs: dict[str, Any] = {"access_token": "test-access-token", "config": ClientConfig()}
    if request.param == "sync":
        client = TradeSmart("TEST123", **kwargs)
        yield client
        client.close()
    else:
        aclient = AsyncTradeSmart("TEST123", **kwargs)
        yield aclient
        await aclient.aclose()


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "_now", lambda: NOW)


def patch_instruments(
    monkeypatch: pytest.MonkeyPatch, client: AnyClient, rows: list[dict[str, str]] = ROWS
) -> list[Any]:
    """Serve ``rows`` as the client's script-master contracts; returns the exchanges asked for.

    The cached parse of the real file is covered in ``test_option_contract_cache.py``.
    """
    seen: list[Any] = []
    index = oc.build_contract_index(rows)
    if isinstance(client, AsyncTradeSmart):

        async def afake(exchange: Any) -> oc.ContractIndex:
            seen.append(exchange)
            return index

        monkeypatch.setattr(client, "_contract_index", afake)
    else:

        def fake(exchange: Any) -> oc.ContractIndex:
            seen.append(exchange)
            return index

        monkeypatch.setattr(client, "_contract_index", fake)
    return seen


def mock_quotes(prices: dict[str, Any]) -> respx.Route:
    """``/GetQuotes`` answering ``lp`` by token (``None`` → no ``lp`` in the reply)."""

    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()[len("jData=") :])
        token = body["token"]
        quote: dict[str, Any] = {"stat": "Ok", "exch": body["exch"], "token": token}
        if prices.get(token) is not None:
            quote["lp"] = str(prices[token])
        return httpx.Response(200, json=quote)

    return respx.post(f"{BASE}/GetQuotes").mock(side_effect=reply)


def quoted_tokens(route: respx.Route) -> list[str]:
    return [json.loads(c.request.content.decode()[6:])["token"] for c in route.calls]


async def call(client: AnyClient, method: str, *args: Any, **kwargs: Any) -> Any:
    result = getattr(client, method)(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def years_to(expiry: date) -> float:
    instant = datetime.combine(expiry, time(15, 30), tzinfo=IST)
    return (instant - NOW).total_seconds() / (365 * 86400)


@respx.mock
async def test_option_greeks_black76_same_expiry(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    years = years_to(MONTHLY)
    model_price = black76(24100.0, 24000.0, years, 0.065, 0.15, "CE").price
    assert model_price is not None
    route = mock_quotes({"1001": 24100, "2001": model_price})

    g = await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C24000")

    assert isinstance(g, OptionGreeks)
    assert g.model == "black76" and g.status == "ok"
    assert g.same_expiry_future is True
    assert g.underlying_tradingsymbol == "NIFTY26NOV26F"
    assert g.underlying_price == pytest.approx(24100.0)
    assert g.forward == pytest.approx(24100.0)
    assert g.option_tradingsymbol == "NIFTY26NOV26C24000"
    assert g.option_price == pytest.approx(model_price)
    assert g.iv == pytest.approx(0.15, abs=1e-6)
    assert g.delta is not None and 0.5 < g.delta < 1.0
    assert g.years_to_expiry == pytest.approx(years)
    assert g.expiry == datetime(2026, 11, 26, 15, 30, tzinfo=IST)
    assert g.calculated_at == NOW
    assert len(route.calls) == 2
    assert sorted(quoted_tokens(route)) == ["1001", "2001"]


@respx.mock
async def test_option_greeks_weekly_reports_future_and_forward(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    route = mock_quotes({"1001": 24100, "2003": 180})

    g = await call(any_client, "option_greeks", "NFO", "NIFTY19NOV26C24000")

    expected_forward = 24100 * math.exp(-0.065 * (years_to(MONTHLY) - years_to(WEEKLY)))
    assert g.underlying_tradingsymbol == "NIFTY26NOV26F"
    assert g.same_expiry_future is False
    assert g.underlying_price == pytest.approx(24100.0)
    assert g.forward == pytest.approx(expected_forward, rel=1e-12)
    assert g.forward < 24100
    assert g.years_to_expiry == pytest.approx(years_to(WEEKLY))
    assert g.model == "black76" and g.status == "ok"
    assert len(route.calls) == 2


@respx.mock
async def test_option_greeks_spot_uses_bsm_and_dividend_yield(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    route = mock_quotes({"2001": 300})

    with_q = await call(
        any_client,
        "option_greeks",
        "NFO",
        "NIFTY26NOV26C24000",
        spot_price=24050,
        dividend_yield=0.02,
    )
    assert len(route.calls) == 1 and quoted_tokens(route) == ["2001"]
    no_q = await call(
        any_client,
        "option_greeks",
        "NFO",
        "NIFTY26NOV26C24000",
        spot_price=24050,
        dividend_yield=0.0,
    )

    assert with_q.model == "bsm" and no_q.model == "bsm"
    assert with_q.dividend_yield == 0.02 and no_q.dividend_yield == 0.0
    assert with_q.underlying_price == pytest.approx(24050.0)
    assert with_q.underlying_tradingsymbol is None and with_q.same_expiry_future is None
    assert with_q.forward is None and no_q.forward is None
    assert with_q.iv is not None and no_q.iv is not None
    assert with_q.iv != pytest.approx(no_q.iv, abs=1e-6)
    assert with_q.delta != pytest.approx(no_q.delta, abs=1e-6)
    assert len(route.calls) == 2


@respx.mock
async def test_option_greeks_defaults(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    mock_quotes({"1001": 24100, "2001": 300})

    g = await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C24000")

    assert g.rate == 0.065
    assert g.dividend_yield == 0.0


@respx.mock
async def test_option_greeks_no_option_price(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    mock_quotes({"1001": 24100, "2001": None})

    g = await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C24000")

    assert g.status == "no_price"
    assert g.option_price is None
    assert g.iv is None and g.delta is None and g.price is None


@respx.mock
async def test_option_greeks_no_future_price(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    mock_quotes({"1001": None, "2001": 300})

    g = await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C24000")

    assert g.status == "no_price"
    assert g.underlying_price is None and g.forward is None
    assert g.iv is None and g.delta is None


@respx.mock
async def test_option_greeks_after_close_on_expiry_day(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    after_close = datetime(2026, 11, 26, 15, 31, tzinfo=IST)
    monkeypatch.setattr(oc, "_now", lambda: after_close)
    mock_quotes({"1001": 24100, "2001": 101})

    g = await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C24000")

    assert g.status == "expired"
    assert g.price == pytest.approx(100.0)
    assert g.delta == 1.0 and g.gamma == 0.0 and g.iv is None
    assert g.years_to_expiry < 0
    assert g.calculated_at == after_close


@respx.mock
async def test_option_greeks_exchange_any_case(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    route = mock_quotes({"1001": 24100, "2001": 300})

    g = await call(any_client, "option_greeks", " nfo ", "nifty26nov26c24000")

    assert g.option_tradingsymbol == "NIFTY26NOV26C24000"
    assert g.status == "ok"
    assert {json.loads(c.request.content.decode()[6:])["exch"] for c in route.calls} == {"NFO"}


@respx.mock
async def test_option_greeks_unknown_symbol(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    route = mock_quotes({})

    with pytest.raises(ValueError, match="NIFTY26NOV26C99999"):
        await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C99999")
    with pytest.raises(ValueError):
        await call(any_client, "option_greeks", "NFO", "NIFTY-EQ")
    assert not route.called


@respx.mock
async def test_option_greeks_given_prices_skip_quotes(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    route = mock_quotes({"1001": 99999, "2001": 300})

    g = await call(any_client, "option_greeks", "NFO", "NIFTY19NOV26C24000", underlying_price=24100)
    assert quoted_tokens(route) == ["2003"]
    assert g.model == "black76"
    assert g.underlying_price == pytest.approx(24100.0)
    assert g.forward == pytest.approx(24100.0)  # no carry adjustment
    assert g.same_expiry_future is None and g.underlying_tradingsymbol is None

    both = await call(
        any_client,
        "option_greeks",
        "NFO",
        "NIFTY26NOV26C24000",
        underlying_price=24100,
        option_price=300,
    )
    assert len(route.calls) == 1  # nothing new fetched
    assert both.option_price == pytest.approx(300.0) and both.status == "ok"


async def test_option_greeks_expiry_time_override(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)

    g = await call(
        any_client,
        "option_greeks",
        "NFO",
        "NIFTY26NOV26C24000",
        underlying_price=24100,
        option_price=300,
        expiry_time=time(23, 59),
    )

    assert g.expiry == datetime(2026, 11, 26, 23, 59, tzinfo=IST)


async def test_option_greeks_rejects_spot_with_underlying_price(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)

    with pytest.raises(ValueError, match="spot_price"):
        await call(
            any_client,
            "option_greeks",
            "NFO",
            "NIFTY26NOV26C24000",
            spot_price=24050,
            underlying_price=24100,
        )


async def test_option_greeks_missing_future(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [r for r in ROWS if r["Instrument"] != "FUTIDX"]
    patch_instruments(monkeypatch, any_client, rows)

    with pytest.raises(ValueError, match="NIFTY"):
        await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C24000")


async def test_option_greeks_price_within_half_tick_of_intrinsic(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)  # TickSize 0.05 -> half a tick is 0.025
    intrinsic = 100 * math.exp(-0.065 * years_to(MONTHLY))

    g = await call(
        any_client,
        "option_greeks",
        "NFO",
        "NIFTY26NOV26C24000",
        underlying_price=24100,
        option_price=intrinsic + 0.02,
    )

    assert g.status == "price_at_or_below_intrinsic"
    assert g.iv is None and g.delta == 1.0


@respx.mock
async def test_option_greeks_earlier_future_carry(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        _row("NIFTY26NOV26F", "1001", "FUTIDX", "26-NOV-2026"),
        _row("NIFTY31DEC26C24000", "2004", "OPTIDX", "31-DEC-2026", "CE", "24000"),
    ]
    patch_instruments(monkeypatch, any_client, rows)
    route = mock_quotes({"1001": 24100, "2004": 400})

    g = await call(any_client, "option_greeks", "NFO", "NIFTY31DEC26C24000")

    future_years, option_years = years_to(MONTHLY), years_to(date(2026, 12, 31))
    assert future_years < option_years
    assert g.underlying_tradingsymbol == "NIFTY26NOV26F" and g.same_expiry_future is False
    assert g.forward == 24100.0 * math.exp(-0.065 * (future_years - option_years))
    assert g.forward > 24100.0
    assert len(route.calls) == 2


def _quoted_tokens(route: respx.Route) -> list[str]:
    return [json.loads(c.request.content.decode()[6:])["token"] for c in route.calls]


@respx.mock
async def test_option_greeks_after_expiry_quotes_only_the_future(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    monkeypatch.setattr(oc, "_now", lambda: datetime(2026, 11, 26, 15, 31, tzinfo=IST))
    route = mock_quotes({"1001": 24100, "2001": 101})

    g = await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C24000")

    assert _quoted_tokens(route) == ["1001"]  # the expired option itself is never quoted
    assert g.status == "expired"
    assert g.price == pytest.approx(100.0)
    assert g.option_price is None


@respx.mock
async def test_option_greeks_after_expiry_keeps_a_given_option_price(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    monkeypatch.setattr(oc, "_now", lambda: datetime(2026, 11, 26, 15, 31, tzinfo=IST))
    route = mock_quotes({"1001": 24100, "2001": 101})

    g = await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C24000", option_price=55)

    assert _quoted_tokens(route) == ["1001"]
    assert g.status == "expired" and g.option_price == 55.0


@respx.mock
async def test_option_greeks_after_expiry_with_underlying_price_makes_no_quote(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    monkeypatch.setattr(oc, "_now", lambda: datetime(2026, 11, 26, 15, 31, tzinfo=IST))
    route = mock_quotes({"1001": 24100, "2001": 101})

    g = await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C24000", underlying_price=24100)

    assert route.call_count == 0
    assert g.status == "expired" and g.option_price is None


@respx.mock
async def test_option_greeks_expiry_time_override_decides_whether_to_quote(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    monkeypatch.setattr(oc, "_now", lambda: datetime(2026, 11, 26, 15, 31, tzinfo=IST))
    route = mock_quotes({"1001": 24100, "2001": 300})

    # expiry_time 16:00 is still in the future at 15:31: the option is quoted as usual
    await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C24000", expiry_time=time(16, 0))

    assert sorted(_quoted_tokens(route)) == ["1001", "2001"]


# --- final review C2: an MCX option is priced on its future, without carry ---------------


def _mcx(
    tsym: str, token: str, instrument: str, expiry: str, opt: str = "", strike: str = ""
) -> dict[str, str]:
    row = _row(tsym, token, instrument, expiry, opt, strike)
    row.update({"Exchange": "MCX", "Symbol": "CRUDEOIL", "LotSize": "100", "TickSize": "1"})
    return row


@respx.mock
async def test_option_greeks_mcx_option_on_later_future_has_no_carry(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        _mcx("CRUDEOIL19NOV26", "5001", "FUTCOM", "19-NOV-2026"),
        _mcx("CRUDEOIL16NOV26C6000", "5002", "OPTFUT", "16-NOV-2026", "CE", "6000"),
    ]
    patch_instruments(monkeypatch, any_client, rows)
    route = mock_quotes({"5001": 6050, "5002": 150})

    g = await call(any_client, "option_greeks", "MCX", "CRUDEOIL16NOV26C6000")

    assert g.underlying_tradingsymbol == "CRUDEOIL19NOV26"
    assert g.same_expiry_future is False
    assert g.underlying_price == 6050.0
    assert g.forward == 6050.0  # exactly the future's price
    assert g.model == "black76" and g.status == "ok"
    assert len(route.calls) == 2


# --- final review C3: expired option, refused future quote -> no_price ------------------


def _refuse_future(future_reply: dict[str, Any]) -> respx.Route:
    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()[len("jData=") :])
        if body["token"] == "1001":
            return httpx.Response(200, json=future_reply)
        return httpx.Response(200, json={"stat": "Ok", "token": body["token"], "lp": "101"})

    return respx.post(f"{BASE}/GetQuotes").mock(side_effect=reply)


@respx.mock
async def test_option_greeks_expired_with_refused_future_quote_is_no_price(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    monkeypatch.setattr(oc, "_now", lambda: datetime(2026, 11, 26, 15, 31, tzinfo=IST))
    route = _refuse_future({"stat": "Not_Ok", "emsg": "Invalid Token"})

    g = await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C24000")

    assert g.status == "no_price"
    assert g.underlying_price is None and g.forward is None
    assert g.underlying_tradingsymbol == "NIFTY26NOV26F"
    assert g.price is None and g.iv is None
    assert quoted_tokens(route) == ["1001"]


@respx.mock
async def test_option_greeks_expired_with_fatal_future_error_raises(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    monkeypatch.setattr(oc, "_now", lambda: datetime(2026, 11, 26, 15, 31, tzinfo=IST))
    _refuse_future(fixture("errors")["session_expired"])

    with pytest.raises(AuthenticationError):
        await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C24000")


@respx.mock
async def test_option_greeks_not_expired_refused_future_quote_raises(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)
    _refuse_future({"stat": "Not_Ok", "emsg": "Invalid Token"})

    with pytest.raises(APIError):
        await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C24000")


# --- final review C7: an aware expiry_time is refused ------------------------------------


async def test_option_greeks_rejects_an_aware_expiry_time(
    any_client: AnyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_instruments(monkeypatch, any_client)

    with pytest.raises(ValueError, match="expiry_time must be a naive time in IST"):
        await call(
            any_client,
            "option_greeks",
            "NFO",
            "NIFTY26NOV26C24000",
            underlying_price=24100,
            option_price=300,
            expiry_time=time(15, 30, tzinfo=IST),
        )
