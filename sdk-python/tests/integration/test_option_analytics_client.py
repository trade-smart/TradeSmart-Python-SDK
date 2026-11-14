"""``option_chain_snapshot()`` and ``buildup()`` on both clients (spec E2), respx-mocked."""

from __future__ import annotations

import asyncio
import gc
import inspect
import json
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from tests.conftest import BASE, fixture
from tradesmart import (
    APIError,
    AsyncTradeSmart,
    AuthenticationError,
    Buildup,
    BuildupReading,
    ClientConfig,
    ConfigurationError,
    NetworkError,
    NotFoundError,
    OptionChainSnapshot,
    RateLimitError,
    RequestTimeoutError,
    RequestValidationError,
    ResponseParseError,
    ServerError,
    TradeSmart,
    TradeSmartError,
)
from tradesmart import _option_contracts as oc
from tradesmart.models.base import IST

AnyClient = TradeSmart | AsyncTradeSmart

NOW = datetime(2026, 11, 12, 10, 15, 30, tzinfo=IST)


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


async def call(client: AnyClient, method: str, *args: Any, **kwargs: Any) -> Any:
    result = getattr(client, method)(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def _payload(request: httpx.Request) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(request.content.decode()[len("jData=") :])
    return body


def contract(tsym: str, token: str, opt: str, strike: str) -> dict[str, str]:
    return {"exch": "NFO", "token": token, "tsym": tsym, "optt": opt, "strprc": strike, "ls": "75"}


CHAIN = [  # deliberately unsorted
    contract("NIFTY26NOV26P24000", "12", "PE", "24000.00"),
    contract("NIFTY26NOV26C24000", "11", "CE", "24000.00"),
    contract("NIFTY26NOV26P23900", "22", "PE", "23900.00"),
    contract("NIFTY26NOV26C23900", "21", "CE", "23900.00"),
]


@respx.mock
async def test_snapshot_lists_failed_quotes(any_client: AnyClient) -> None:
    chain = respx.post(f"{BASE}/GetOptionChain").respond(200, json={"stat": "Ok", "values": CHAIN})
    quotes = {
        "11": {"lp": "150.5", "oi": "1000", "v": "5000", "c": "140"},
        "21": {"lp": "210", "oi": "3000", "v": "2500", "c": "200"},
        "22": {"lp": "90", "oi": "2000", "v": "1500", "c": "95"},
    }

    def reply(request: httpx.Request) -> httpx.Response:
        token = _payload(request)["token"]
        if token not in quotes:
            return httpx.Response(200, json=fixture("errors")["no_data"])
        return httpx.Response(
            200, json={"stat": "Ok", "exch": "NFO", "token": token, **quotes[token]}
        )

    route = respx.post(f"{BASE}/GetQuotes").mock(side_effect=reply)

    snap = await call(any_client, "option_chain_snapshot", "nfo", "NIFTY", 24000, count=2)

    assert isinstance(snap, OptionChainSnapshot)
    assert _payload(chain.calls.last.request) == {
        "uid": "TEST123",
        "exch": "NFO",
        "tsym": "NIFTY",
        "strprc": "24000",
        "cnt": "2",
    }
    assert len(route.calls) == 4
    assert snap.missing == ("NIFTY26NOV26P24000",)
    assert snap.exchange == "NFO" and snap.tradingsymbol == "NIFTY"
    assert snap.fetched_at == NOW
    assert [(r.strike, r.option_type) for r in snap.rows] == [
        (Decimal("23900"), "CE"),
        (Decimal("23900"), "PE"),
        (Decimal("24000"), "CE"),
    ]
    first = snap.rows[0]
    assert first.tradingsymbol == "NIFTY26NOV26C23900" and first.token == "21"
    assert first.last_price == Decimal("210") and first.open_interest == Decimal("3000")
    assert first.volume == Decimal("2500") and first.close == Decimal("200")
    assert snap.pcr() == Decimal(2000) / Decimal(4000)
    assert snap.pcr(by="volume") == Decimal(1500) / Decimal(7500)


@respx.mock
async def test_snapshot_unusable_contracts_go_to_missing(any_client: AnyClient) -> None:
    chain = [
        {"exch": "NFO", "tsym": "NIFTY26NOV26C24100", "optt": "CE", "strprc": "24100"},
        {"exch": "NFO", "token": "31", "tsym": "NIFTY26NOV26X24100", "strprc": "24100"},
        {"exch": "NFO", "optt": "pe", "strprc": "24200.00"},
        contract("NIFTY26NOV26C24000", "11", "CE", "24000"),
    ]
    respx.post(f"{BASE}/GetOptionChain").respond(200, json={"stat": "Ok", "values": chain})
    route = respx.post(f"{BASE}/GetQuotes").respond(
        200, json={"stat": "Ok", "lp": "10", "oi": "100", "v": "1"}
    )

    snap = await call(any_client, "option_chain_snapshot", "NFO", "NIFTY", 24000, count=1)

    assert sorted(_payload(c.request)["token"] for c in route.calls) == ["11", "31"]
    assert snap.missing == ("NIFTY26NOV26C24100", "NIFTY26NOV26X24100", "24200.00 PE")
    assert "" not in snap.missing
    assert [r.tradingsymbol for r in snap.rows] == ["NIFTY26NOV26C24000"]


@respx.mock
async def test_snapshot_default_count_is_25(any_client: AnyClient) -> None:
    chain = respx.post(f"{BASE}/GetOptionChain").respond(200, json=fixture("errors")["no_data"])

    snap = await call(any_client, "option_chain_snapshot", "NFO", "NIFTY", 24000)

    assert _payload(chain.calls.last.request)["cnt"] == "25"
    assert snap.rows == () and snap.missing == ()


@respx.mock
async def test_snapshot_async_concurrency_bounded(async_client: AsyncTradeSmart) -> None:
    chain = [
        contract(f"NIFTY26NOV26{kind[0]}{23000 + 50 * i}", f"{i}{kind}", kind, f"{23000 + 50 * i}")
        for i in range(15)
        for kind in ("CE", "PE")
    ]
    respx.post(f"{BASE}/GetOptionChain").respond(200, json={"stat": "Ok", "values": chain})
    in_flight = 0
    peak = 0

    async def reply(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        token = _payload(request)["token"]
        return httpx.Response(
            200, json={"stat": "Ok", "token": token, "lp": "10", "oi": "100", "v": "1"}
        )

    route = respx.post(f"{BASE}/GetQuotes").mock(side_effect=reply)

    snap = await async_client.option_chain_snapshot("NFO", "NIFTY", 23500, count=15)
    await async_client.aclose()

    assert len(route.calls) == 30
    assert len(snap.rows) == 30 and snap.missing == ()
    assert 1 < peak <= 10


@respx.mock
async def test_buildup_rest_since_open(any_client: AnyClient) -> None:
    respx.post(f"{BASE}/GetQuotes").respond(
        200, json={"stat": "Ok", "exch": "NFO", "token": "1001", "lp": "24105.5", "c": "24000"}
    )
    candles = [
        {"stat": "Ok", "time": "12-11-2026 10:15:00", "intc": "24105", "intoi": "300"},
        {"stat": "Ok", "time": "12-11-2026 10:14:00", "intc": "24100", "intoi": "-50"},
        {"stat": "Ok", "time": "12-11-2026 09:15:00", "intc": "24010", "intoi": "1250"},
    ]
    tps = respx.post(f"{BASE}/TPSeries").respond(200, json=candles)

    reading = await call(any_client, "buildup", "nfo", "1001")

    assert isinstance(reading, BuildupReading)
    assert reading.state is Buildup.LONG_BUILDUP
    assert reading.price_change == Decimal("105.5")
    assert reading.oi_change == Decimal("1500")
    assert reading.basis == "since_open"
    start = datetime(2026, 11, 12, 0, 0, tzinfo=IST)
    assert _payload(tps.calls.last.request) == {
        "uid": "TEST123",
        "exch": "NFO",
        "token": "1001",
        "st": str(int(start.timestamp())),
        "et": str(int(NOW.timestamp())),
        "intrv": "1",
    }


@respx.mock
async def test_buildup_missing_pieces_are_none(any_client: AnyClient) -> None:
    respx.post(f"{BASE}/GetQuotes").respond(
        200, json={"stat": "Ok", "exch": "NFO", "token": "1001", "lp": "24105.5"}
    )
    respx.post(f"{BASE}/TPSeries").respond(200, json=fixture("errors")["no_data"])

    reading = await call(any_client, "buildup", "NFO", "1001")

    assert reading.state is Buildup.NEUTRAL
    assert reading.price_change is None and reading.oi_change is None
    assert reading.basis == "since_open"


def _api(cls: type[APIError]) -> APIError:
    return cls("GetQuotes failed", endpoint="GetQuotes", http_status=200)


@pytest.mark.parametrize(
    ("error", "per_contract"),
    [
        (_api(APIError), True),
        (_api(NotFoundError), True),
        (ResponseParseError("bad", endpoint="GetQuotes", http_status=200, body="x"), True),
        (_api(AuthenticationError), False),
        (_api(RateLimitError), False),
        (_api(ServerError), False),
        (ConfigurationError("closed"), False),
        (NetworkError("down"), False),
        (RequestTimeoutError("slow"), False),
        (RequestValidationError([]), False),
        (TradeSmartError("other"), False),
    ],
)
def test_quote_failure_classification(error: TradeSmartError, per_contract: bool) -> None:
    assert oc.is_contract_quote_failure(error) is per_contract


@respx.mock
async def test_snapshot_session_expired_raises(any_client: AnyClient) -> None:
    respx.post(f"{BASE}/GetOptionChain").respond(200, json={"stat": "Ok", "values": CHAIN})

    def reply(request: httpx.Request) -> httpx.Response:
        token = _payload(request)["token"]
        if token == "22":
            return httpx.Response(200, json=fixture("errors")["session_expired"])
        if token == "12":
            return httpx.Response(200, json={"stat": "Not_Ok", "emsg": "Invalid Token"})
        return httpx.Response(200, json={"stat": "Ok", "token": token, "lp": "10", "oi": "1"})

    respx.post(f"{BASE}/GetQuotes").mock(side_effect=reply)

    with pytest.raises(AuthenticationError, match="Session Expired"):
        await call(any_client, "option_chain_snapshot", "NFO", "NIFTY", 24000, count=2)


@respx.mock
async def test_snapshot_async_fatal_error_cancels_pending_quotes(
    async_client: AsyncTradeSmart,
) -> None:
    chain = [
        contract(f"NIFTY26NOV26C{23000 + 50 * i}", f"{i}", "CE", f"{23000 + 50 * i}")
        for i in range(30)
    ]
    respx.post(f"{BASE}/GetOptionChain").respond(200, json={"stat": "Ok", "values": chain})
    started: list[str] = []

    async def reply(request: httpx.Request) -> httpx.Response:
        token = _payload(request)["token"]
        started.append(token)
        if token == "0":
            return httpx.Response(200, json=fixture("errors")["session_expired"])
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"stat": "Ok", "token": token, "lp": "10", "oi": "1"})

    respx.post(f"{BASE}/GetQuotes").mock(side_effect=reply)
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        with pytest.raises(AuthenticationError):
            await async_client.option_chain_snapshot("NFO", "NIFTY", 23500, count=15)
        sent_at_raise = len(started)
        await asyncio.sleep(0.2)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)
        await async_client.aclose()

    assert len(started) == sent_at_raise
    # The failed quote frees its slot, so at most one waiting quote starts before the raise.
    assert sent_at_raise <= 11
    assert unhandled == []
