import json
from decimal import Decimal

import pytest
import respx

from tests.conftest import BASE, fixture
from tradesmart import (
    AsyncTradeSmart,
    Exchange,
    RequestValidationError,
    ResponseParseError,
    TradeSmart,
)


@respx.mock
def test_quote_symbol_token_and_decimal(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/GetQuotes").respond(200, json=fixture("get_quotes_success"))
    q = client.quote(Exchange.NSE, "TCS-EQ")
    assert (
        route.calls.last.request.content == b'jData={"uid":"TEST123","exch":"NSE","token":"TCS-EQ"}'
    )
    assert isinstance(q.last_price, Decimal) and q.feed_time is not None
    assert q.feed_time.tzinfo is not None
    assert q.wk52_h == Decimal("3538.00") and q.bp1 == Decimal("0.00") and q.sq1 == 7548
    assert q.uc == Decimal("2051.00") and q.lc == Decimal("1931.60")


@respx.mock
def test_search_no_match_is_empty(client: TradeSmart) -> None:
    respx.post(f"{BASE}/SearchScrip").respond(404, json=fixture("errors")["search_no_data"])
    assert client.search_instruments("zzzz") == []


@respx.mock
def test_search_results(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/SearchScrip").respond(200, json=fixture("search_scrip_success"))
    res = client.search_instruments("TCS")
    assert route.calls.last.request.content == b'jData={"uid":"TEST123","stext":"TCS"}'
    assert res[1].tradingsymbol == "TCS-EQ" and res[1].lot_size == 1
    assert res[1].tick_size == Decimal("0.10")


@respx.mock
def test_search_missing_values_is_parse_error(client: TradeSmart) -> None:
    respx.post(f"{BASE}/SearchScrip").respond(200, json={"stat": "Ok"})
    with pytest.raises(ResponseParseError):
        client.search_instruments("TCS")


@respx.mock
def test_search_empty_values(client: TradeSmart) -> None:
    respx.post(f"{BASE}/SearchScrip").respond(200, json={"stat": "Ok", "values": []})
    assert client.search_instruments("TCS") == []


@respx.mock
def test_option_chain_follows_field_table(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/GetOptionChain").respond(200, json=fixture("option_chain_success"))
    oc = client.option_chain(Exchange.NFO, "NIFTY", 24000, 200)
    assert json.loads(route.calls.last.request.content.decode()[6:]) == {
        "uid": "TEST123",
        "exch": "NFO",
        "tsym": "NIFTY",
        "strprc": "24000",
        "cnt": "200",
    }
    assert len(oc) == 4
    assert isinstance(oc[0].strike_price, Decimal) and oc[0].option_type == "CE"
    with pytest.raises(RequestValidationError):
        client.option_chain(Exchange.NFO, "NIFTY", 24000, 0)


@respx.mock
def test_option_chain_no_data_is_empty(client: TradeSmart) -> None:
    respx.post(f"{BASE}/GetOptionChain").respond(200, json=fixture("errors")["no_data"])
    assert client.option_chain(Exchange.NFO, "NIFTY", 24000, 4) == []


@respx.mock
def test_security_info_symname_alias(client: TradeSmart) -> None:
    respx.post(f"{BASE}/GetSecurityInfo").respond(200, json=fixture("security_info_success"))
    info = client.security_info(Exchange.NSE, "11536")
    assert info.symname == "TCS" and info.frzqty == 47035


@respx.mock
def test_security_info_symnam_variant(client: TradeSmart) -> None:
    body = {"stat": "Ok", "symnam": "INFY"}
    respx.post(f"{BASE}/GetSecurityInfo").respond(200, json=body)
    assert client.security_info(Exchange.NSE, "1594").symname == "INFY"


@respx.mock
async def test_async_market(async_client: AsyncTradeSmart) -> None:
    respx.post(f"{BASE}/GetQuotes").respond(200, json=fixture("get_quotes_success"))
    respx.post(f"{BASE}/SearchScrip").respond(200, json=fixture("search_scrip_success"))
    respx.post(f"{BASE}/GetOptionChain").respond(200, json=fixture("option_chain_success"))
    respx.post(f"{BASE}/GetSecurityInfo").respond(200, json=fixture("security_info_success"))
    assert (await async_client.quote("NSE", "11536")).last_price == Decimal("2099.00")
    assert len(await async_client.search_instruments("TCS")) == 3
    assert len(await async_client.option_chain("NFO", "NIFTY", "24000", 4)) == 4
    assert (await async_client.security_info("NSE", "11536")).symname == "TCS"
    with pytest.raises(RequestValidationError):
        await async_client.option_chain("NFO", "NIFTY", 24000, 0)
