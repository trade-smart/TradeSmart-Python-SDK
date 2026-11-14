from __future__ import annotations

import math
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest

from tradesmart._option_contracts import (
    carry_adjusted_forward,
    expiry_instant,
    find_option,
    select_future,
    years_between,
)
from tradesmart.models.base import IST


def _row(
    ts: str,
    instrument: str,
    expiry: str,
    opt: str = "",
    strike: str = "0",
    sym: str = "NIFTY",
) -> dict[str, str]:
    return {
        "Exchange": "NFO",
        "Token": "t-" + ts,
        "LotSize": "75",
        "Symbol": sym,
        "TradingSymbol": ts,
        "Expiry": expiry,
        "Instrument": instrument,
        "OptionType": opt,
        "StrikePrice": strike,
        "TickSize": "0.05",
    }


ROWS = [
    _row("NIFTY13NOV25C25000", "OPTIDX", "13-NOV-2025", "CE", "25000"),
    _row("NIFTY27NOV25C25000", "OPTIDX", "27-NOV-2025", "CE", "25000"),
    _row("NIFTY27NOV25FUT", "FUTIDX", "27-NOV-2025"),
    _row("NIFTY25DEC25FUT", "FUTIDX", "25-DEC-2025"),
    _row("BANKNIFTY27NOV25FUT", "FUTIDX", "27-NOV-2025", sym="BANKNIFTY"),
]


def test_find_option_case_insensitive() -> None:
    o = find_option(ROWS, "nifty13nov25c25000")
    assert o.expiry == date(2025, 11, 13)
    assert o.strike == Decimal("25000")
    assert o.option_type == "CE"
    assert o.lot_size == 75
    assert o.tick_size == Decimal("0.05")
    assert o.symbol == "NIFTY"
    assert o.exchange == "NFO"


def test_find_option_rejects_future_and_unknown() -> None:
    with pytest.raises(ValueError, match="NIFTY27NOV25FUT"):
        find_option(ROWS, "NIFTY27NOV25FUT")
    with pytest.raises(ValueError, match="NOPE"):
        find_option(ROWS, "NOPE")


def test_select_future_same_expiry() -> None:
    f = select_future(ROWS, find_option(ROWS, "NIFTY27NOV25C25000"))
    assert f.tradingsymbol == "NIFTY27NOV25FUT"
    assert f.same_expiry is True


def test_select_future_weekly_uses_later_future() -> None:
    f = select_future(ROWS, find_option(ROWS, "NIFTY13NOV25C25000"))
    assert f.tradingsymbol == "NIFTY27NOV25FUT"
    assert f.same_expiry is False
    assert f.expiry == date(2025, 11, 27)


def test_select_future_falls_back_to_earlier() -> None:
    rows = [r for r in ROWS if r["TradingSymbol"] != "NIFTY25DEC25FUT"]
    rows.append(_row("NIFTY30DEC25C1", "OPTIDX", "30-DEC-2025", "CE", "1"))
    f = select_future(rows, find_option(rows, "NIFTY30DEC25C1"))
    assert f.tradingsymbol == "NIFTY27NOV25FUT"
    assert f.same_expiry is False


def test_select_future_none_raises() -> None:
    rows = [r for r in ROWS if r["Instrument"].startswith("OPT")]
    with pytest.raises(ValueError, match="NIFTY"):
        select_future(rows, find_option(rows, "NIFTY13NOV25C25000"))


def test_missing_optional_columns() -> None:
    r = _row("X1", "OPTSTK", "13-NOV-2025", "PE", "10.5", sym="X")
    del r["TickSize"], r["LotSize"]
    o = find_option([r], "X1")
    assert o.tick_size is None and o.lot_size is None
    assert o.strike == Decimal("10.5")


def test_expiry_instant_per_exchange() -> None:
    d = date(2025, 11, 13)
    assert expiry_instant("NFO", d) == datetime(2025, 11, 13, 15, 30, tzinfo=IST)
    assert expiry_instant("cds", d) == datetime(2025, 11, 13, 12, 30, tzinfo=IST)
    assert expiry_instant("MCX", d) == datetime(2025, 11, 13, 23, 30, tzinfo=IST)
    assert expiry_instant("NFO", d, time(10, 0)) == datetime(2025, 11, 13, 10, 0, tzinfo=IST)
    assert expiry_instant("XYZ", d, time(9, 0)).hour == 9
    with pytest.raises(ValueError, match="XYZ"):
        expiry_instant("XYZ", d)


def test_years_between_negative_after_close() -> None:
    a = datetime(2025, 11, 13, 16, 0, tzinfo=IST)
    b = datetime(2025, 11, 13, 15, 30, tzinfo=IST)
    assert years_between(a, b) < 0
    c = datetime(2026, 11, 13, 15, 30, tzinfo=IST)
    assert years_between(b, c) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("StrikePrice", ""),
        ("StrikePrice", "abc"),
        ("Expiry", "32-FOO-2025"),
        ("Expiry", ""),
        ("OptionType", "XX"),
        ("OptionType", ""),
        ("Symbol", ""),
    ],
)
def test_find_option_malformed_value_raises_value_error(field: str, value: str) -> None:
    row = _row("NIFTY13NOV25C25000", "OPTIDX", "13-NOV-2025", "CE", "25000")
    row[field] = value
    with pytest.raises(ValueError, match="malformed instrument row for NIFTY13NOV25C25000"):
        find_option([row], "NIFTY13NOV25C25000")


@pytest.mark.parametrize("key", ["StrikePrice", "Expiry", "OptionType", "Symbol"])
def test_find_option_missing_key_raises_value_error(key: str) -> None:
    row = _row("NIFTY13NOV25C25000", "OPTIDX", "13-NOV-2025", "CE", "25000")
    del row[key]
    with pytest.raises(ValueError, match="malformed instrument row for NIFTY13NOV25C25000"):
        find_option([row], "NIFTY13NOV25C25000")


def test_select_future_skips_malformed_future_rows() -> None:
    bad = _row("NIFTY13NOV25FUT", "FUTIDX", "garbage")
    missing = _row("NIFTY14NOV25FUT", "FUTIDX", "14-NOV-2025")
    del missing["Expiry"]
    rows = [bad, missing, *ROWS]
    f = select_future(rows, find_option(rows, "NIFTY13NOV25C25000"))
    assert f.tradingsymbol == "NIFTY27NOV25FUT"


def test_select_future_only_malformed_futures_raises() -> None:
    rows = [
        _row("NIFTY13NOV25C25000", "OPTIDX", "13-NOV-2025", "CE", "25000"),
        _row("NIFTY27NOV25FUT", "FUTIDX", ""),
    ]
    with pytest.raises(ValueError, match="NIFTY"):
        select_future(rows, find_option(rows, "NIFTY13NOV25C25000"))


def test_select_future_ignores_prefix_symbols() -> None:
    nxt = _row("NIFTYNXT5020NOVFUT", "FUTIDX", "20-NOV-2025", sym="NIFTYNXT50")
    rows = [nxt, *ROWS]
    f = select_future(rows, find_option(rows, "NIFTY13NOV25C25000"))
    assert f.tradingsymbol == "NIFTY27NOV25FUT"


def test_select_future_ignores_other_exchange() -> None:
    bfo = _row("NIFTYBFO13NOVFUT", "FUTIDX", "13-NOV-2025")
    bfo["Exchange"] = "BFO"
    rows = [bfo, *ROWS]
    f = select_future(rows, find_option(rows, "NIFTY13NOV25C25000"))
    assert f.tradingsymbol == "NIFTY27NOV25FUT"
    assert f.same_expiry is False


def test_select_future_blank_option_symbol_raises() -> None:
    from tradesmart._option_contracts import OptionContractInfo

    opt = OptionContractInfo("NFO", "X", "1", "", date(2025, 11, 13), "CE", Decimal(1), None, None)
    blank = _row("BLANKFUT", "FUTIDX", "13-NOV-2025", sym="")
    with pytest.raises(ValueError, match="symbol"):
        select_future([blank], opt)


def test_carry_adjusted_forward() -> None:
    assert carry_adjusted_forward(100.0, 0.065, 0.1, 0.05) == pytest.approx(
        100 * math.exp(-0.00325)
    )


def test_select_future_skips_future_without_tradingsymbol() -> None:
    nobody = _row("X", "FUTIDX", "13-NOV-2025")
    del nobody["TradingSymbol"]
    blank = _row("   ", "FUTIDX", "13-NOV-2025")
    rows = [nobody, blank, *ROWS]
    f = select_future(rows, find_option(rows, "NIFTY13NOV25C25000"))
    assert f.tradingsymbol == "NIFTY27NOV25FUT"


def test_select_future_only_future_without_tradingsymbol_raises() -> None:
    nobody = _row("X", "FUTIDX", "27-NOV-2025")
    del nobody["TradingSymbol"]
    rows = [_row("NIFTY27NOV25C25000", "OPTIDX", "27-NOV-2025", "CE", "25000"), nobody]
    with pytest.raises(ValueError, match="no underlying future"):
        select_future(rows, find_option(rows, "NIFTY27NOV25C25000"))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "sNaN", "abc"])
def test_find_option_non_finite_lot_size_raises_value_error(value: str) -> None:
    row = _row("NIFTY13NOV25C25000", "OPTIDX", "13-NOV-2025", "CE", "25000")
    row["LotSize"] = value
    with pytest.raises(ValueError, match="malformed instrument row for NIFTY13NOV25C25000"):
        find_option([row], "NIFTY13NOV25C25000")


def test_find_option_missing_option_type_says_missing() -> None:
    row = _row("NIFTY13NOV25C25000", "OPTIDX", "13-NOV-2025", "CE", "25000")
    del row["OptionType"]
    with pytest.raises(ValueError, match="missing OptionType"):
        find_option([row], "NIFTY13NOV25C25000")


@pytest.mark.parametrize("blank", ["", "   "])
def test_find_option_blank_tradingsymbol_raises(blank: str) -> None:
    with pytest.raises(ValueError, match="tradingsymbol is required"):
        find_option(ROWS, blank)


def test_find_option_ignores_rows_without_tradingsymbol() -> None:
    nobody = _row("X", "OPTIDX", "13-NOV-2025", "CE", "25000")
    del nobody["TradingSymbol"]
    with pytest.raises(ValueError, match="not found"):
        find_option([nobody], "NIFTY13NOV25C25000")


# --- final review C1: indexed lookups behave exactly like the row scans ------------------


def _tie_rows() -> list[dict[str, str]]:
    first = _row("NIFTY27NOV25FUT", "FUTIDX", "27-NOV-2025")
    second = _row("NIFTY27NOV25FUT2", "FUTIDX", "27-NOV-2025")
    return [ROWS[1], first, second]


def _malformed_option() -> list[dict[str, str]]:
    row = _row("NIFTY13NOV25C25000", "OPTIDX", "garbage", "CE", "25000")
    return [row, *ROWS]


def _no_tradingsymbol_rows() -> list[dict[str, str]]:
    nobody = _row("X", "FUTIDX", "13-NOV-2025")
    del nobody["TradingSymbol"]
    blank = _row("   ", "FUTIDX", "13-NOV-2025")
    return [nobody, blank, *ROWS]


def _other_exchange_rows() -> list[dict[str, str]]:
    bfo = _row("NIFTYBFO13NOVFUT", "FUTIDX", "13-NOV-2025")
    bfo["Exchange"] = "bfo"
    nfo_lower = _row("NIFTY13NOV25FUT", "FUTIDX", "13-NOV-2025", sym="nifty")
    nfo_lower["Exchange"] = " nfo "
    return [bfo, nfo_lower, *ROWS]


def _malformed_futures() -> list[dict[str, str]]:
    bad = _row("NIFTY13NOV25FUT", "FUTIDX", "garbage")
    missing = _row("NIFTY14NOV25FUT", "FUTIDX", "14-NOV-2025")
    del missing["Expiry"]
    return [bad, missing, *ROWS]


def _future_named_like_option() -> list[dict[str, str]]:
    # A FUT row with the option's tradingsymbol comes first; the OPT row must still be found.
    clash = _row("NIFTY13NOV25C25000", "FUTIDX", "13-NOV-2025")
    return [clash, *ROWS]


INDEX_CASES = [
    (ROWS, "NIFTY13NOV25C25000"),
    (ROWS, " nifty27nov25c25000 "),
    (ROWS, "NIFTY27NOV25FUT"),
    (ROWS, "NOPE"),
    (ROWS, "   "),
    (_tie_rows(), "NIFTY27NOV25C25000"),
    (_malformed_option(), "NIFTY13NOV25C25000"),
    (_no_tradingsymbol_rows(), "NIFTY13NOV25C25000"),
    (_other_exchange_rows(), "NIFTY13NOV25C25000"),
    (_malformed_futures(), "NIFTY13NOV25C25000"),
    (_future_named_like_option(), "NIFTY13NOV25C25000"),
    ([ROWS[0]], "NIFTY13NOV25C25000"),
]


def _scan_outcome(rows: list[dict[str, str]], tradingsymbol: str) -> object:
    """The reference: find_option / select_future scanning every row."""
    try:
        option = find_option(rows, tradingsymbol)
        return (option, select_future(rows, option))
    except ValueError as exc:
        return ("ValueError", str(exc))


def _index_outcome(rows: list[dict[str, str]], tradingsymbol: str) -> object:
    """The path option_greeks takes: plan_option_greeks on a prebuilt ContractIndex."""
    from tradesmart._option_contracts import build_contract_index, plan_option_greeks

    try:
        plan = plan_option_greeks(
            build_contract_index(rows),
            "NFO",
            tradingsymbol,
            underlying_price=None,
            spot_price=None,
        )
        return (plan.option, plan.future)
    except ValueError as exc:
        return ("ValueError", str(exc))


@pytest.mark.parametrize(("rows", "tradingsymbol"), INDEX_CASES)
def test_contract_index_lookup_matches_row_scan(
    rows: list[dict[str, str]], tradingsymbol: str
) -> None:
    assert _index_outcome(rows, tradingsymbol) == _scan_outcome(rows, tradingsymbol)


def test_contract_index_tie_takes_the_first_row() -> None:
    outcome = _index_outcome(_tie_rows(), "NIFTY27NOV25C25000")
    assert outcome[1].tradingsymbol == "NIFTY27NOV25FUT"  # type: ignore[index]


def test_contract_index_keeps_only_option_and_future_rows() -> None:
    from tradesmart._option_contracts import build_contract_index

    equity = _row("NIFTY-EQ", "EQ", "")
    index = build_contract_index([equity, *ROWS])

    assert sum(len(v) for v in index.options.values()) == 2
    assert sum(len(v) for v in index.futures.values()) == 3


# --- final review C2: MCX options use their future's price without carry -----------------


def _plan_with_later_future(exchange: str) -> object:
    from tradesmart._option_contracts import GreeksPlan, OptionContractInfo, UnderlyingFuture

    option = OptionContractInfo(
        exchange,
        "OPT",
        "1",
        "SYM",
        date(2026, 11, 16),
        "CE",
        Decimal(6000),
        Decimal("1"),
        100,
    )
    future = UnderlyingFuture("FUT", "2", date(2026, 11, 19), same_expiry=False)
    return GreeksPlan(exchange, option, future, "black76")


def test_mcx_option_forward_is_the_future_price(monkeypatch: pytest.MonkeyPatch) -> None:
    from tradesmart import _option_contracts as oc

    monkeypatch.setattr(oc, "_now", lambda: datetime(2026, 11, 12, 10, 0, tzinfo=IST))
    g = oc.option_greeks_result(
        _plan_with_later_future("MCX"),  # type: ignore[arg-type]
        option_price=150,
        underlying_price=6050,
        rate=0.065,
        dividend_yield=0.0,
        expiry_time=None,
    )

    assert g.forward == 6050.0
    assert g.same_expiry_future is False
    assert g.underlying_price == 6050.0


def test_nfo_option_on_later_future_is_still_carry_adjusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tradesmart import _option_contracts as oc

    monkeypatch.setattr(oc, "_now", lambda: datetime(2026, 11, 12, 10, 0, tzinfo=IST))
    g = oc.option_greeks_result(
        _plan_with_later_future("NFO"),  # type: ignore[arg-type]
        option_price=150,
        underlying_price=6050,
        rate=0.065,
        dividend_yield=0.0,
        expiry_time=None,
    )

    assert g.forward is not None and g.forward < 6050.0


# --- final review C7: expiry_time must be naive (IST) -------------------------------------


def test_expiry_instant_rejects_an_aware_expiry_time() -> None:

    with pytest.raises(ValueError, match="expiry_time must be a naive time in IST"):
        expiry_instant("NFO", date(2025, 11, 13), time(10, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="expiry_time must be a naive time in IST"):
        expiry_instant("NFO", date(2025, 11, 13), time(10, 0, tzinfo=IST))


# --- F28: BFO option contracts (Strike column, OPT -> FUT underlying symbol) --------------


def _bfo(
    ts: str,
    instrument: str,
    expiry: str,
    sym: str,
    opt: str = "",
    strike: str = "0",
    exchange: str = "BFO",
) -> dict[str, str]:
    """A row in the BFO layout: the strike column is ``Strike`` (no ``StrikePrice`` key)."""
    return {
        "Exchange": exchange,
        "Token": "t-" + ts,
        "LotSize": "20",
        "Symbol": sym,
        "TradingSymbol": ts,
        "Expiry": expiry,
        "Instrument": instrument,
        "OptionType": opt,
        "Strike": strike,
        "TickSize": "0.05",
    }


BSX_ROWS = [
    _bfo("SENSEX26N1985200CE", "OPTIDX", "19-NOV-2026", "BSXOPT", "CE", "85200"),
    _bfo("SENSEX26NOVFUT", "FUTIDX", "26-NOV-2026", "BSXFUT"),
    _bfo("SENSEX5026NOVFUT", "FUTIDX", "26-NOV-2026", "SX50FUT"),
]


def test_future_symbols() -> None:
    from tradesmart._option_contracts import future_symbols

    assert future_symbols("BSXOPT") == ("BSXOPT", "BSXFUT")
    assert future_symbols(" sx50opt ") == ("SX50OPT", "SX50FUT")
    assert future_symbols("NIFTY") == ("NIFTY",)
    assert future_symbols("OPT") == ("OPT",)


def test_find_option_reads_the_bfo_strike_column() -> None:
    option = find_option(BSX_ROWS, "SENSEX26N1985200CE")
    assert option.strike == Decimal("85200")
    assert option.symbol == "BSXOPT"
    assert option.lot_size == 20


def test_find_option_prefers_the_strikeprice_column() -> None:
    row = _row("X1", "OPTSTK", "13-NOV-2025", "CE", "100", sym="X")
    row["Strike"] = "200"
    assert find_option([row], "X1").strike == Decimal("100")
    row["StrikePrice"] = ""
    with pytest.raises(ValueError, match="malformed instrument row for X1: invalid StrikePrice"):
        find_option([row], "X1")


def test_find_option_without_any_strike_column_is_malformed() -> None:
    row = _row("X1", "OPTSTK", "13-NOV-2025", "CE", "100", sym="X")
    del row["StrikePrice"]
    with pytest.raises(ValueError, match="malformed instrument row for X1: missing StrikePrice"):
        find_option([row], "X1")


def test_find_option_invalid_bfo_strike_names_the_strike_column() -> None:
    row = _bfo("S1", "OPTIDX", "19-NOV-2026", "BSXOPT", "CE", "abc")
    with pytest.raises(ValueError, match="invalid Strike 'abc'"):
        find_option([row], "S1")


def test_select_future_falls_back_from_opt_to_fut() -> None:
    option = find_option(BSX_ROWS, "SENSEX26N1985200CE")
    future = select_future(BSX_ROWS, option)
    assert future.tradingsymbol == "SENSEX26NOVFUT"
    assert future.same_expiry is False
    assert future.expiry == date(2026, 11, 26)


def test_select_future_fallback_is_case_and_space_insensitive() -> None:
    rows = [
        _bfo("S1", "OPTIDX", "19-NOV-2026", " bsxopt ", "CE", "85200"),
        _bfo("SENSEX26NOVFUT", "FUTIDX", "26-NOV-2026", "BSXFUT"),
    ]
    assert select_future(rows, find_option(rows, "S1")).tradingsymbol == "SENSEX26NOVFUT"


def test_select_future_never_takes_another_family() -> None:
    rows = [
        _bfo("SX50C1", "OPTIDX", "19-NOV-2026", "SX50OPT", "CE", "100"),
        _bfo("SENSEX26NOVFUT", "FUTIDX", "26-NOV-2026", "BSXFUT"),
    ]
    with pytest.raises(ValueError, match="no underlying future found for symbol SX50OPT"):
        select_future(rows, find_option(rows, "SX50C1"))


def test_select_future_prefers_the_own_symbol() -> None:
    rows = [
        _bfo("XC1", "OPTIDX", "19-NOV-2026", "XOPT", "CE", "100"),
        _bfo("XFUTROW", "FUTIDX", "19-NOV-2026", "XFUT"),
        _bfo("XOPTROW", "FUTIDX", "26-NOV-2026", "XOPT"),
    ]
    future = select_future(rows, find_option(rows, "XC1"))
    assert future.tradingsymbol == "XOPTROW"


def test_select_future_falls_back_when_own_symbol_rows_are_unusable() -> None:
    rows = [
        _bfo("XC1", "OPTIDX", "19-NOV-2026", "XOPT", "CE", "100"),
        _bfo("XOPTROW", "FUTIDX", "garbage", "XOPT"),
        _bfo("XFUTROW", "FUTIDX", "19-NOV-2026", "XFUT"),
    ]
    assert select_future(rows, find_option(rows, "XC1")).tradingsymbol == "XFUTROW"


def test_select_future_fallback_ignores_other_exchanges() -> None:
    rows = [
        BSX_ROWS[0],
        _bfo("SENSEX26NOVFUT", "FUTIDX", "26-NOV-2026", "BSXFUT", exchange="NFO"),
    ]
    with pytest.raises(ValueError, match="no underlying future found for symbol BSXOPT"):
        select_future(rows, find_option(rows, "SENSEX26N1985200CE"))


def test_select_future_error_names_the_symbols_tried() -> None:
    rows = [BSX_ROWS[0]]
    with pytest.raises(ValueError, match="BSXOPT.*BSXFUT"):
        select_future(rows, find_option(rows, "SENSEX26N1985200CE"))


def test_select_future_symbol_opt_alone_has_no_fallback() -> None:
    rows = [
        _bfo("O1", "OPTIDX", "19-NOV-2026", "OPT", "CE", "100"),
        _bfo("FUTROW", "FUTIDX", "26-NOV-2026", "FUT"),
    ]
    with pytest.raises(ValueError, match="no underlying future found for symbol OPT"):
        select_future(rows, find_option(rows, "O1"))


@pytest.mark.parametrize(
    "rows",
    [
        BSX_ROWS,
        [BSX_ROWS[1], BSX_ROWS[2], BSX_ROWS[0]],
        [
            _bfo("XC1", "OPTIDX", "19-NOV-2026", "XOPT", "CE", "100"),
            _bfo("XFUTROW", "FUTIDX", "19-NOV-2026", "XFUT"),
            _bfo("XOPTROW", "FUTIDX", "26-NOV-2026", "XOPT"),
        ],
        [
            _bfo("XC1", "OPTIDX", "19-NOV-2026", "XOPT", "CE", "100"),
            _bfo("XOPTROW", "FUTIDX", "garbage", "XOPT"),
            _bfo("XFUTROW", "FUTIDX", "19-NOV-2026", "XFUT"),
        ],
    ],
)
def test_index_and_scan_agree_on_the_fallback(rows: list[dict[str, str]]) -> None:
    from tradesmart._option_contracts import build_contract_index

    option_row = next(r for r in rows if r["Instrument"].startswith("OPT"))
    option = find_option(rows, option_row["TradingSymbol"])
    index = build_contract_index(rows)
    assert select_future(index.future_rows(option), option) == select_future(rows, option)


def test_select_future_error_upper_cases_the_symbols() -> None:
    rows = [_bfo("S1", "OPTIDX", "19-NOV-2026", " bsxopt ", "CE", "85200")]
    with pytest.raises(ValueError) as caught:
        select_future(rows, find_option(rows, "S1"))
    expected = "no underlying future found for symbol BSXOPT (tried BSXOPT, BSXFUT)"
    assert str(caught.value) == expected
    nifty = [_row("N1", "OPTIDX", "13-NOV-2025", "CE", "1", sym="nifty")]
    with pytest.raises(ValueError) as caught:
        select_future(nifty, find_option(nifty, "N1"))
    assert str(caught.value) == "no underlying future found for symbol NIFTY"


# --- sliced orders: any instrument by trading symbol (spec 8.4) ----------------------------


def test_instrument_row_finds_any_instrument_trimmed_and_case_insensitive() -> None:
    from tradesmart._option_contracts import build_contract_index

    equity = _row(" itc-eq ", "EQ", "")
    future = _row("NIFTY26NOVFUT", "FUTIDX", "26-NOV-2026")
    option = _row("NIFTY26N2524000CE", "OPTIDX", "25-NOV-2026", "CE", "24000")
    index = build_contract_index([equity, future, option])

    assert index.instrument_row("ITC-EQ") is equity
    assert index.instrument_row("  nifty26novfut ") is future
    assert index.instrument_row("nifty26n2524000ce") is option
    assert index.instrument_row("NIFTY26DECFUT") is None
    assert index.instrument_row("   ") is None


def test_instrument_row_first_row_wins_and_blank_names_are_skipped() -> None:
    from tradesmart._option_contracts import build_contract_index

    first = _row("NIFTY26NOVFUT", "FUTIDX", "26-NOV-2026")
    second = {**first, "Token": "other"}
    blank = _row("  ", "EQ", "")
    index = build_contract_index([blank, first, second])

    assert index.instrument_row("NIFTY26NOVFUT") is first
    assert index.instrument_row("") is None
