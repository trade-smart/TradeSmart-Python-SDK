from decimal import Decimal

import pytest
import respx

from tests.conftest import BASE, fixture
from tradesmart import AsyncTradeSmart, RequestValidationError, ResponseParseError, TradeSmart


@respx.mock
def test_limits(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/Limits").respond(200, json=fixture("limits_success"))
    lim = client.margins()
    assert route.calls.last.request.content == b'jData={"uid":"TEST123","actid":"TEST123"}'
    assert isinstance(lim.cash, Decimal)


@respx.mock
def test_positions_no_data_is_empty(client: TradeSmart) -> None:
    respx.post(f"{BASE}/PositionBook").respond(200, json=fixture("errors")["no_data"])
    assert client.positions() == []


@respx.mock
def test_positions_list(client: TradeSmart) -> None:
    respx.post(f"{BASE}/PositionBook").respond(200, json=fixture("position_book_success"))
    p = client.positions()[0]
    assert isinstance(p.net_quantity, int) and p.exchange == "NSE"
    assert p.tradingsymbol == "TESTCO-EQ" and p.daybuyamt == Decimal("500.00")


@respx.mock
def test_holdings_object_response_is_parse_error(client: TradeSmart) -> None:
    respx.post(f"{BASE}/Holdings").respond(200, json={"stat": "Ok", "holdqty": "1"})
    with pytest.raises(ResponseParseError):
        client.holdings()


@respx.mock
def test_holdings_list(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/Holdings").respond(200, json=fixture("holdings_success"))
    h = client.holdings()
    assert (
        route.calls.last.request.content == b'jData={"uid":"TEST123","actid":"TEST123","prd":"C"}'
    )
    assert h[0].instruments is not None and h[0].instruments[0].exchange == "NSE"
    assert h[0].holdqty == 10 and h[0].upldprc == Decimal("100.00")


@respx.mock
async def test_async_portfolio(async_client: AsyncTradeSmart) -> None:
    limits = respx.post(f"{BASE}/Limits").respond(200, json=fixture("limits_success"))
    respx.post(f"{BASE}/Holdings").respond(200, json=fixture("holdings_success"))
    respx.post(f"{BASE}/PositionBook").respond(200, json=fixture("position_book_success"))
    lim = await async_client.margins()
    assert limits.calls.last.request.content == b'jData={"uid":"TEST123","actid":"TEST123"}'
    assert isinstance(lim.cash, Decimal)
    assert len(await async_client.holdings()) == 1
    assert (await async_client.positions())[0].exchange == "NSE"


# -- holdings(product=...) resolves strings through Product (K1) ---------------------

PRODUCT_SPELLINGS = [
    ("CNC", b"C"),
    ("cnc", b"C"),
    (" Cnc ", b"C"),
    ("C", b"C"),
    ("mis", b"I"),
    ("NRML", b"M"),
    ("MTF", b"F"),
]


@pytest.mark.parametrize(("spelling", "wire"), PRODUCT_SPELLINGS)
@respx.mock
def test_holdings_string_product_sends_the_wire_code(
    client: TradeSmart, spelling: str, wire: bytes
) -> None:
    route = respx.post(f"{BASE}/Holdings").respond(200, json=fixture("holdings_success"))
    client.holdings(spelling)
    assert route.calls.last.request.content == (
        b'jData={"uid":"TEST123","actid":"TEST123","prd":"' + wire + b'"}'
    )


@pytest.mark.parametrize(("spelling", "wire"), PRODUCT_SPELLINGS)
@respx.mock
async def test_async_holdings_string_product_sends_the_wire_code(
    async_client: AsyncTradeSmart, spelling: str, wire: bytes
) -> None:
    route = respx.post(f"{BASE}/Holdings").respond(200, json=fixture("holdings_success"))
    await async_client.holdings(spelling)
    assert route.calls.last.request.content == (
        b'jData={"uid":"TEST123","actid":"TEST123","prd":"' + wire + b'"}'
    )


@pytest.mark.parametrize("bad", ["XYZ", "", "delivery"])
@respx.mock
def test_holdings_unknown_product_raises_before_sending(client: TradeSmart, bad: str) -> None:
    route = respx.post(f"{BASE}/Holdings").respond(200, json=fixture("holdings_success"))
    with pytest.raises(RequestValidationError, match="prd"):
        client.holdings(bad)
    assert not route.called


@pytest.mark.parametrize("bad", ["XYZ", "", "delivery"])
@respx.mock
async def test_async_holdings_unknown_product_raises_before_sending(
    async_client: AsyncTradeSmart, bad: str
) -> None:
    route = respx.post(f"{BASE}/Holdings").respond(200, json=fixture("holdings_success"))
    with pytest.raises(RequestValidationError, match="prd"):
        await async_client.holdings(bad)
    assert not route.called
