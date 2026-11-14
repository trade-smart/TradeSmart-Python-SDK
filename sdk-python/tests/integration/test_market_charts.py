import json
from datetime import date, datetime
from decimal import Decimal

import pytest
import respx

from tests.conftest import BASE, fixture
from tradesmart import (
    Exchange,
    RequestValidationError,
    ResponseParseError,
    TradeSmart,
)
from tradesmart.endpoints.market import to_epoch
from tradesmart.models.base import IST


@respx.mock
def test_tpseries_payload_and_parse(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/TPSeries").respond(200, json=fixture("tpseries_success"))
    start = datetime(2025, 6, 20, 0, 0, tzinfo=IST)
    candles = client.historical_data(
        Exchange.BSE, "INFY", from_date=start, to_date=1750674066, interval=1
    )
    assert json.loads(route.calls.last.request.content.decode()[6:]) == {
        "uid": "TEST123",
        "exch": "BSE",
        "token": "INFY",
        "st": "1750357800",
        "et": "1750674066",
        "intrv": "1",
    }
    assert candles[0].time == datetime(2025, 6, 23, 15, 29, tzinfo=IST)
    assert isinstance(candles[0].intc, Decimal)
    assert candles[0].volume == 256409 and candles[0].intv == 665 and len(candles) == 5


@respx.mock
def test_tpseries_omits_unset_optionals(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/TPSeries").respond(200, json=fixture("tpseries_success"))
    client.historical_data(Exchange.NSE, "22")
    assert json.loads(route.calls.last.request.content.decode()[6:]) == {
        "uid": "TEST123",
        "exch": "NSE",
        "token": "22",
    }


def test_tpseries_bad_interval(client: TradeSmart) -> None:
    with pytest.raises(RequestValidationError):
        client.historical_data(Exchange.NSE, "22", interval=7)


def test_to_epoch_requires_aware_datetime() -> None:
    assert to_epoch(5) == "5"
    assert to_epoch(datetime(2025, 6, 20, tzinfo=IST)) == "1750357800"
    with pytest.raises(RequestValidationError):
        to_epoch(datetime(2025, 6, 20))


def test_to_epoch_date_is_midnight_ist() -> None:
    assert to_epoch(date(2025, 6, 20)) == "1750357800"


@respx.mock
def test_eod_decodes_string_elements_and_has_no_uid(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/EODChartData").respond(200, json=fixture("eod_chart_success"))
    rows = client.daily_history(Exchange.NSE, "SBIN-EQ", 1690934400, 1691107200)
    assert json.loads(route.calls.last.request.content.decode()[6:]) == {
        "sym": "NSE:SBIN-EQ",
        "from": "1690934400",
        "to": "1691107200",
    }
    assert rows[0].time == datetime(2023, 8, 4, tzinfo=IST)
    assert isinstance(rows[0].into, Decimal) and rows[0].intv == 52725616 and len(rows) == 3


@respx.mock
def test_eod_accepts_dict_elements(client: TradeSmart) -> None:
    respx.post(f"{BASE}/EODChartData").respond(
        200, json=[{"time": "04-AUG-2023", "intc": "573.30"}]
    )
    assert client.daily_history("NSE", "SBIN-EQ", 1, 2)[0].intc == Decimal("573.30")


@respx.mock
@pytest.mark.parametrize("body", [["not json"], [42], [["x"]]])
def test_eod_bad_elements_raise_parse_error(client: TradeSmart, body: list[object]) -> None:
    respx.post(f"{BASE}/EODChartData").respond(200, json=body)
    with pytest.raises(ResponseParseError):
        client.daily_history("NSE", "SBIN-EQ", 1, 2)


@respx.mock
def test_eod_empty_and_tpseries_no_data(client: TradeSmart) -> None:
    respx.post(f"{BASE}/EODChartData").respond(200, json=[])
    respx.post(f"{BASE}/TPSeries").respond(200, json=fixture("errors")["no_data"])
    assert client.daily_history("NSE", "SBIN-EQ", 1, 2) == []
    assert client.historical_data(Exchange.NSE, "22") == []
