"""Unit tests of the live-suite helpers (synthetic script-master rows, no network)."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.live._helpers import (
    CASES,
    Case,
    case_id,
    equity_from_search,
    find_equity,
    nearest_future,
    nearest_option,
    options_by_distance,
    parse_expiry,
    record,
    redact,
    resolve_contract,
)
from tradesmart.models.base import IST

TODAY = datetime(2026, 11, 10, 10, 0, tzinfo=IST)


def _row(
    ts: str,
    instrument: str,
    expiry: str,
    *,
    exch: str = "MCX",
    sym: str = "CRUDEOIL",
    opt: str = "",
    strike: str = "0",
    token: str | None = None,
) -> dict[str, str]:
    return {
        "Exchange": exch,
        "Token": token if token is not None else "t-" + ts,
        "LotSize": "100",
        "Symbol": sym,
        "TradingSymbol": ts,
        "Expiry": expiry,
        "Instrument": instrument,
        "OptionType": opt,
        "StrikePrice": strike,
    }


ROWS = [
    _row("CRUDEOIL18DEC26FUT", "FUTCOM", "18-DEC-2026"),
    _row("CRUDEOIL19NOV26FUT", "FUTCOM", "19-NOV-2026"),
    _row("CRUDEOIL19OCT26FUT", "FUTCOM", "19-OCT-2026"),  # expired
    _row("CO19NOV26C5000", "OPTFUT", "19-NOV-2026", opt="CE", strike="5000"),
    _row("CO19NOV26C5500", "OPTFUT", "19-NOV-2026", opt="CE", strike="5500"),
    _row("CO19NOV26C6000", "OPTFUT", "19-NOV-2026", opt="CE", strike="6000"),
    _row("CO19NOV26P5500", "OPTFUT", "19-NOV-2026", opt="PE", strike="5500"),
    _row("CO18DEC26C5500", "OPTFUT", "18-DEC-2026", opt="CE", strike="5500"),
    _row("CO19OCT26C5500", "OPTFUT", "19-OCT-2026", opt="CE", strike="5500"),  # expired
    _row("GOLD05DEC26FUT", "FUTCOM", "05-DEC-2026", sym="GOLD"),
    _row("NIFTY24NOV26FUT", "FUTIDX", "24-NOV-2026", exch="NFO", sym="NIFTY"),
]


def _no_price(_exchange: str, _token: str) -> Decimal | None:
    return None


def test_parse_expiry() -> None:
    assert parse_expiry("19-nov-2026") == date(2026, 11, 19)
    assert parse_expiry("") is None
    assert parse_expiry("soon") is None
    assert parse_expiry(None) is None


def test_nearest_future_skips_expired_and_other_exchanges() -> None:
    found = nearest_future(ROWS, "MCX", "CRUDEOIL", TODAY)
    assert found is not None
    assert found.tradingsymbol == "CRUDEOIL19NOV26FUT"
    assert found.expiry == date(2026, 11, 19)
    assert found.lot_size == 100
    assert nearest_future(ROWS, "MCX", "NIFTY", TODAY) is None
    assert nearest_future(ROWS, "NFO", "NIFTY", TODAY) is not None


def test_nearest_future_includes_expiry_day() -> None:
    found = nearest_future(ROWS, "MCX", "CRUDEOIL", datetime(2026, 11, 19, 10, 0, tzinfo=IST))
    assert found is not None and found.tradingsymbol == "CRUDEOIL19NOV26FUT"


def test_nearest_future_ignores_rows_without_token() -> None:
    rows = [_row("A", "FUTCOM", "19-NOV-2026", sym="X", token="")]
    assert nearest_future(rows, "MCX", "X", TODAY) is None


def test_nearest_option_nearest_expiry_and_strike() -> None:
    found = nearest_option(ROWS, "MCX", "CRUDEOIL", TODAY, Decimal("5600"), "CE")
    assert found is not None
    assert found.tradingsymbol == "CO19NOV26C5500"
    assert found.strike == Decimal("5500")
    assert found.option_type == "CE"
    assert found.kind == "opt"


def test_nearest_option_type_and_default_middle_strike() -> None:
    put = nearest_option(ROWS, "MCX", "CRUDEOIL", TODAY, None, "PE")
    assert put is not None and put.tradingsymbol == "CO19NOV26P5500"
    middle = nearest_option(ROWS, "MCX", "CRUDEOIL", TODAY)
    assert middle is not None and middle.strike == Decimal("5500")


def test_nearest_option_none() -> None:
    assert nearest_option(ROWS, "MCX", "GOLD", TODAY) is None


def test_options_by_distance_lists_the_nearest_expiry_closest_strike_first() -> None:
    found = options_by_distance(ROWS, "MCX", "CRUDEOIL", TODAY, Decimal("5800"), "CE")
    assert [c.tradingsymbol for c in found] == [
        "CO19NOV26C6000",
        "CO19NOV26C5500",
        "CO19NOV26C5000",
    ]
    assert options_by_distance(ROWS, "MCX", "GOLD", TODAY, Decimal("1"), "CE") == []


def test_options_by_distance_breaks_ties_by_the_lower_strike() -> None:
    found = options_by_distance(ROWS, "MCX", "CRUDEOIL", TODAY, Decimal("5250"), "CE")
    assert [c.strike for c in found][:2] == [Decimal("5000"), Decimal("5500")]


def test_find_equity_and_search_fallback() -> None:
    rows = [_row("INFY-EQ", "", "", exch="NSE", sym="INFY", token="1594")]
    found = find_equity(rows, "NSE", "infy")
    assert found is not None and found.token == "1594" and found.kind == "eq"
    assert find_equity(rows, "NSE", "TCS") is None

    match = SimpleNamespace(exchange="NSE", tradingsymbol="TCS-EQ", token="11536")
    other = SimpleNamespace(exchange="BSE", tradingsymbol="TCS-EQ", token="1")
    got = equity_from_search([other, match], "NSE", "TCS")
    assert got is not None and got.token == "11536"
    assert equity_from_search([other], "NSE", "TCS") is None


def test_resolve_contract_falls_back_to_next_symbol_and_quotes_future() -> None:
    quoted: list[tuple[str, str]] = []

    def last_price(exchange: str, token: str) -> Decimal | None:
        quoted.append((exchange, token))
        return Decimal("5990")

    case = Case("mcx", "MCX", "opt", ("MISSING", "CRUDEOIL"))
    found = resolve_contract(case, lambda _e: ROWS, last_price, lambda _t: [], TODAY)
    assert found is not None and found.tradingsymbol == "CO19NOV26C6000"
    # MISSING has no future (no quote); CRUDEOIL quotes its nearest future once.
    assert quoted == [("MCX", "t-CRUDEOIL19NOV26FUT")]


def test_resolve_contract_kinds() -> None:
    fut = resolve_contract(
        Case("mcx", "MCX", "fut", ("CRUDEOIL",)), lambda _e: ROWS, _no_price, lambda _t: [], TODAY
    )
    assert fut is not None and fut.kind == "fut"
    eq = resolve_contract(
        Case("nse", "NSE", "eq", ("INFY",)), lambda _e: [], _no_price, lambda _t: [], TODAY
    )
    assert eq is None
    opt = resolve_contract(
        Case("mcx", "MCX", "opt", ("CRUDEOIL",)), lambda _e: ROWS, _no_price, lambda _t: [], TODAY
    )
    assert opt is not None and opt.strike == Decimal("5500")  # no price: middle strike


def test_cases_cover_segments_with_unique_ids() -> None:
    ids = [case_id(c) for c in CASES]
    assert len(set(ids)) == len(ids)
    assert {c.segment for c in CASES} == {"nse", "nfo", "mcx"}
    assert {c.kind for c in CASES if c.segment == "mcx"} == {"fut", "opt"}


def test_redact_hides_secrets_recursively() -> None:
    data = {"ok": 1, "access_token": "x", "nested": {"secret_key": "y", "n": [{"checksum": "z"}]}}
    assert redact(data) == {
        "ok": 1,
        "access_token": "<redacted>",
        "nested": {"secret_key": "<redacted>", "n": [{"checksum": "<redacted>"}]},
    }


def test_redact_hides_account_ids_recursively() -> None:
    """No test account id is ever recorded (the results folder is local, but stays clean)."""
    data = {
        "client_id": "ZZ1",
        "user_id": "ZZ2",
        "Account_ID": "ZZ3",
        "raw": [{"uid": "ZZ4", "actid": "ZZ5", "token": "26000"}],
    }
    assert redact(data) == {
        "client_id": "<redacted>",
        "user_id": "<redacted>",
        "Account_ID": "<redacted>",
        "raw": [{"uid": "<redacted>", "actid": "<redacted>", "token": "26000"}],
    }


def test_record_appends_json_lines(tmp_path: Path) -> None:
    path = record("suite", "t1", results_dir=tmp_path, n=1, access_token="secret")
    record("suite", "t2", results_dir=tmp_path, when=date(2026, 1, 1))
    text = path.read_text(encoding="utf-8")
    lines = [json.loads(x) for x in text.splitlines()]
    assert [x["test"] for x in lines] == ["t1", "t2"]
    assert lines[0]["n"] == 1 and lines[0]["access_token"] == "<redacted>"
    assert lines[1]["when"] == "2026-01-01"
    assert "secret" not in text


def test_record_tags_each_line_with_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TRADESMART_ENV", raising=False)
    record("suite", "t1", results_dir=tmp_path)
    monkeypatch.setenv("TRADESMART_ENV", " UAT ")
    path = record("suite", "t2", results_dir=tmp_path, environment="spoofed")
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert [x["environment"] for x in lines] == ["production", "uat"]


@pytest.mark.parametrize(
    ("columns", "expected"),
    [
        ({"StrikePrice": "25300"}, Decimal("25300")),
        ({"Strike": "85200"}, Decimal("85200")),
        ({"StrikePrice": "25300", "Strike": "1"}, Decimal("25300")),
        ({"StrikePrice": "", "Strike": "85200"}, None),  # the StrikePrice key decides (10.4)
        ({}, None),
    ],
)
def test_option_strike_is_strikeprice_else_strike(
    columns: dict[str, str], expected: Decimal | None
) -> None:
    row = _row("SENSEX26N1985200CE", "OPTIDX", "19-NOV-2026", exch="BFO", sym="BSXOPT", opt="CE")
    del row["StrikePrice"]
    row.update(columns)
    from tests.live import _helpers

    found = _helpers._contract(row, "BFO", "opt")
    assert found is not None
    assert found.strike == expected
