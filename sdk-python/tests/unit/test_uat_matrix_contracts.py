"""The UAT matrix's contracts, pricing, case table and expectation oracle, offline.

Synthetic script-master rows and quotes only (``tests/live_orders/uat_matrix``); nothing here
touches TradeSmart.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.live_orders import safety
from tests.live_orders.uat_matrix import cases, contracts, expect, pricing
from tests.live_orders.uat_matrix.cases import Case
from tests.live_orders.uat_matrix.contracts import Contract, Contracts
from tests.live_orders.uat_matrix.expect import KnownIssues, Profile
from tradesmart.errors import APIError, AuthenticationError, ConfigurationError
from tradesmart.models.market import Quote
from tradesmart.models.user import UserDetails

TODAY = date(2026, 11, 13)


# -- a synthetic market ---------------------------------------------------------------------


def _row(exchange: str, token: str, tradingsymbol: str, **extra: str) -> dict[str, str]:
    row = {
        "Exchange": exchange,
        "Token": token,
        "TradingSymbol": tradingsymbol,
        "LotSize": "1",
        "TickSize": "0.05",
    }
    row.update(extra)
    return row


def _future(
    exchange: str, token: str, symbol: str, expiry: str, lot: str, instrument: str = "FUTIDX"
) -> dict[str, str]:
    return _row(
        exchange,
        token,
        f"{symbol}{expiry.replace('-', '')}F",
        Instrument=instrument,
        Symbol=symbol,
        Expiry=expiry,
        LotSize=lot,
    )


def _options(
    exchange: str,
    symbol: str,
    expiry: str,
    lot: str,
    strikes: range,
    *,
    first_token: int,
    instrument: str = "OPTIDX",
    strike_column: str = "StrikePrice",
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    token = first_token
    for option_type in ("CE", "PE"):
        for strike in strikes:
            row = _row(
                exchange,
                str(token),
                f"{symbol}{strike}{option_type}",
                Instrument=instrument,
                Symbol=symbol,
                Expiry=expiry,
                LotSize=lot,
                OptionType=option_type,
            )
            row[strike_column] = f"{strike}.00"
            rows.append(row)
            token += 1
    return rows


def _quote(lp: str, ls: str = "1", ti: str = "0.05", **extra: str) -> dict[str, str]:
    return {"lp": lp, "ls": ls, "ti": ti, **extra}


class FakeMarket:
    """``instruments`` and ``quote`` of a client, from synthetic rows and quotes."""

    def __init__(
        self,
        masters: dict[str, list[dict[str, str]]],
        quotes: dict[tuple[str, str], dict[str, str]],
        *,
        broken: frozenset[str] = frozenset(),
    ) -> None:
        self.masters = masters
        self.quotes = quotes
        self.broken = broken
        self.calls: list[tuple[str, ...]] = []

    def instruments(self, exchange: str) -> list[dict[str, str]]:
        self.calls.append(("instruments", exchange))
        if exchange in self.broken:
            raise APIError("script master failed", endpoint="ScripMaster", http_status=500)
        return [dict(r) for r in self.masters.get(exchange, [])]

    def quote(self, exchange: str, token: str) -> Quote:
        self.calls.append(("quote", exchange, token))
        data = self.quotes.get((exchange, token))
        if data is None:
            raise APIError("no data", endpoint="GetQuotes", http_status=200, emsg="no data")
        return Quote.model_validate(data)

    def quoted(self, exchange: str) -> list[str]:
        return [c[2] for c in self.calls if c[0] == "quote" and c[1] == exchange]


NEAR = "27-NOV-2026"
FAR = "30-DEC-2026"


def _market() -> FakeMarket:
    """Every role of the plan resolvable; NIFTY at 24,010, strikes 23,800-24,200 step 50."""
    nse = [
        _row("NSE", "1660", "ITC-EQ"),
        _row("NSE", "11915", "YESBANK-EQ", TickSize="0.01"),
        _row("NSE", "14366", "IDEA-EQ", TickSize="0.01"),
        _row("NSE", "2031", "M&M-EQ"),
    ]
    bse = [_row("BSE", "500875", "ITC")]
    nfo = [
        _future("NFO", "100", "NIFTY", NEAR, "75"),
        _future("NFO", "101", "NIFTY", FAR, "75"),
        _future("NFO", "102", "BANKNIFTY", NEAR, "35"),
        _future("NFO", "103", "RELIANCE", NEAR, "500", instrument="FUTSTK"),
        _future("NFO", "104", "HDFCBANK", NEAR, "550", instrument="FUTSTK"),
        *_options("NFO", "NIFTY", NEAR, "75", range(23800, 24250, 50), first_token=1000),
        *_options("NFO", "BANKNIFTY", NEAR, "35", range(51800, 52250, 100), first_token=2000),
        *_options(
            "NFO",
            "HDFCBANK",
            NEAR,
            "550",
            range(1700, 1800, 20),
            first_token=3000,
            instrument="OPTSTK",
        ),
    ]
    bfo = [
        _future("BFO", "800", "BSXFUT", NEAR, "20"),
        *_options(
            "BFO",
            "BSXOPT",
            NEAR,
            "20",
            range(79800, 80300, 100),
            first_token=8000,
            strike_column="Strike",
        ),
    ]
    mcx = [
        _future("MCX", "900", "CRUDEOIL", "18-NOV-2026", "100", instrument="FUTCOM"),
        _future("MCX", "901", "GOLD", "04-DEC-2026", "1", instrument="FUTCOM"),
        _future("MCX", "902", "GOLDM", "04-DEC-2026", "100", instrument="FUTCOM"),
        _future("MCX", "903", "GOLDPETAL", "30-NOV-2026", "1", instrument="FUTCOM"),
    ]
    quotes: dict[tuple[str, str], dict[str, str]] = {
        ("NSE", "1660"): _quote("410.15", lc="369.15", uc="451.15"),
        ("NSE", "11915"): _quote("19.87", ti="0.01"),
        ("NSE", "14366"): _quote("7.12", ti="0.01"),
        ("NSE", "2031"): _quote("2950.40"),
        ("BSE", "500875"): _quote("410.20"),
        ("NFO", "100"): _quote("24010", ls="75"),
        ("NFO", "101"): _quote("24100", ls="75"),
        ("NFO", "102"): _quote("52010", ls="35"),
        ("NFO", "103"): _quote("0", ls="500"),  # RELIANCE future: no trade today
        ("NFO", "104"): _quote("1745", ls="550"),
        ("BFO", "800"): _quote("80020", ls="20"),
        ("MCX", "900"): _quote("9210", ls="100", ti="1", lc="8500", uc="9900"),
        ("MCX", "901"): _quote("76000", ls="1", ti="1"),
        ("MCX", "902"): _quote("76010", ls="100", ti="1"),
        ("MCX", "903"): _quote("7600", ls="1", ti="1"),
    }
    for row in nfo + bfo:
        if row.get("OptionType"):
            quotes[(row["Exchange"], row["Token"])] = _quote("120.5", ls=row["LotSize"])
    return FakeMarket({"NSE": nse, "BSE": bse, "NFO": nfo, "BFO": bfo, "MCX": mcx}, quotes)


def _token(market: FakeMarket, exchange: str, tradingsymbol: str) -> str:
    for row in market.masters[exchange]:
        if row["TradingSymbol"] == tradingsymbol:
            return row["Token"]
    raise AssertionError(tradingsymbol)


def _no_price(market: FakeMarket, exchange: str, *tradingsymbols: str) -> None:
    for tradingsymbol in tradingsymbols:
        market.quotes[(exchange, _token(market, exchange, tradingsymbol))]["lp"] = "0"


# -- contracts: futures -----------------------------------------------------------------------


def _reason(resolved: Contracts, role: str) -> str:
    return resolved.unavailable[role].reason


def _kind(resolved: Contracts, role: str) -> str:
    return resolved.unavailable[role].kind


def test_every_role_is_resolved_or_has_a_reason() -> None:
    resolved = contracts.resolve_contracts(_market(), TODAY)
    assert set(resolved.contracts) | set(resolved.unavailable) == set(contracts.ROLES)
    assert not set(resolved.contracts) & set(resolved.unavailable)
    assert resolved.day == TODAY


def test_the_full_synthetic_market_resolves_every_role() -> None:
    resolved = contracts.resolve_contracts(_market(), TODAY)
    assert resolved.unavailable == {}
    assert all(c.resolved_last_price > 0 and c.tick_size > 0 for c in resolved.contracts.values())


def test_future_expiry_is_strictly_after_today() -> None:
    market = _market()
    market.masters["NFO"].append(_future("NFO", "99", "NIFTY", "13-NOV-2026", "75"))  # today
    market.masters["NFO"].append(_future("NFO", "98", "NIFTY", "06-NOV-2026", "75"))  # past
    market.quotes[("NFO", "99")] = _quote("24000", ls="75")
    market.quotes[("NFO", "98")] = _quote("24000", ls="75")
    nifty = contracts.resolve_contracts(market, TODAY).contracts["nfo-nifty-f"]
    assert nifty.token == "100"
    assert nifty.expiry == date(2026, 11, 27)
    assert nifty.kind == "fut"


def test_a_future_expiring_today_only_leaves_the_role_unavailable() -> None:
    market = _market()
    market.masters["MCX"] = [
        _future("MCX", "900", "CRUDEOIL", "13-NOV-2026", "100", instrument="FUTCOM"),
    ]
    resolved = contracts.resolve_contracts(market, TODAY)
    assert "mcx-crudeoil-f" not in resolved.contracts
    assert "CRUDEOIL" in _reason(resolved, "mcx-crudeoil-f")
    assert _kind(resolved, "mcx-crudeoil-f") == contracts.NOT_IN_MASTER


def test_bfo_sensex_uses_bsxfut_for_the_future_and_bsxopt_with_the_strike_column() -> None:
    resolved = contracts.resolve_contracts(_market(), TODAY)
    future = resolved.contracts["bfo-sensex-f"]
    assert (future.exchange, future.symbol, future.token) == ("BFO", "BSXFUT", "800")
    ce = resolved.contracts["bfo-sensex-ce-atm"]
    pe = resolved.contracts["bfo-sensex-pe-atm"]
    assert ce.symbol == pe.symbol == "BSXOPT"
    assert ce.strike == pe.strike == Decimal("80000")  # nearest to the future's 80,020
    assert (ce.option_type, pe.option_type) == ("CE", "PE")
    assert ce.lot_size == 20


def test_mcx_roles_are_futcom_in_trade_units_of_whole_lots() -> None:
    resolved = contracts.resolve_contracts(_market(), TODAY)
    crude = resolved.contracts["mcx-crudeoil-f"]
    assert crude.lot_size == 100
    assert crude.quantity() == 100  # one lot is 100 units, never 1
    assert crude.quantity(2) == 200
    assert resolved.contracts["mcx-gold-f"].quantity() == 1
    assert resolved.contracts["mcx-goldm-f"].quantity(3) == 300
    assert resolved.contracts["mcx-goldpetal-f"].lot_size == 1  # from the day's master
    for role in ("mcx-crudeoil-f", "mcx-gold-f", "mcx-goldm-f", "mcx-goldpetal-f"):
        assert resolved.contracts[role].exchange == "MCX"
        assert resolved.contracts[role].kind == "fut"


def test_quantity_refuses_a_non_positive_lot_count() -> None:
    crude = contracts.resolve_contracts(_market(), TODAY).contracts["mcx-crudeoil-f"]
    with pytest.raises(ValueError, match="lots"):
        crude.quantity(0)


def test_a_lot_size_the_quote_disagrees_with_leaves_the_role_unavailable() -> None:
    market = _market()
    market.quotes[("MCX", "900")]["ls"] = "1"
    resolved = contracts.resolve_contracts(market, TODAY)
    assert "mcx-crudeoil-f" not in resolved.contracts
    assert "lot size" in _reason(resolved, "mcx-crudeoil-f")
    assert _kind(resolved, "mcx-crudeoil-f") == contracts.LOT_SIZE_MISMATCH


def test_tick_comes_from_the_quote_else_from_the_master_and_circuits_from_the_quote() -> None:
    market = _market()
    market.quotes[("NSE", "1660")]["ti"] = "0.10"
    market.quotes[("NSE", "2031")].pop("ti")
    resolved = contracts.resolve_contracts(market, TODAY)
    itc = resolved.contracts["nse-b"]
    assert itc.tick_size == Decimal("0.10")
    assert (itc.resolved_lower_circuit, itc.resolved_upper_circuit) == (
        Decimal("369.15"),
        Decimal("451.15"),
    )
    assert itc.resolved_last_price == Decimal("410.15")
    assert resolved.contracts["nse-e"].tick_size == Decimal("0.05")  # master
    assert resolved.contracts["nse-e"].resolved_lower_circuit is None


def test_a_contract_has_no_field_a_price_could_be_taken_from_by_mistake() -> None:
    itc = contracts.resolve_contracts(_market(), TODAY).contracts["nse-b"]
    for name in ("last_price", "lower_circuit", "upper_circuit", "lc", "uc"):
        assert not hasattr(itc, name), name


def test_no_tick_anywhere_leaves_the_role_unavailable() -> None:
    market = _market()
    market.quotes[("NSE", "2031")].pop("ti")
    for row in market.masters["NSE"]:
        if row["TradingSymbol"] == "M&M-EQ":
            row["TickSize"] = ""
    resolved = contracts.resolve_contracts(market, TODAY)
    assert "tick" in _reason(resolved, "nse-e")
    assert _kind(resolved, "nse-e") == contracts.NO_TICK


# -- contracts: cash candidates (Review Focus 4) ------------------------------------------------


def test_cash_roles_take_the_exact_trading_symbols() -> None:
    resolved = contracts.resolve_contracts(_market(), TODAY)
    assert resolved.contracts["nse-b"].tradingsymbol == "ITC-EQ"
    assert resolved.contracts["nse-e"].tradingsymbol == "M&M-EQ"  # sent as is; encoding is M11
    assert resolved.contracts["bse-b"].tradingsymbol == "ITC"
    assert resolved.contracts["nse-b"].quantity() == 1


def test_low_tick_role_falls_back_to_the_next_candidate_with_a_last_price() -> None:
    market = _market()
    _no_price(market, "NSE", "YESBANK-EQ")
    resolved = contracts.resolve_contracts(market, TODAY)
    assert resolved.contracts["nse-t"].tradingsymbol == "IDEA-EQ"
    assert resolved.contracts["nse-t"].tick_size == Decimal("0.01")


def test_a_role_without_any_priced_candidate_is_skipped_with_the_reason() -> None:
    market = _market()
    _no_price(market, "NSE", "YESBANK-EQ", "IDEA-EQ")
    resolved = contracts.resolve_contracts(market, TODAY)
    assert "nse-t" not in resolved.contracts
    reason = _reason(resolved, "nse-t")
    assert "no last price" in reason
    assert "YESBANK-EQ" in reason and "IDEA-EQ" in reason
    assert _kind(resolved, "nse-t") == contracts.NO_LAST_PRICE


def test_a_quote_error_counts_as_no_last_price() -> None:
    market = _market()
    del market.quotes[("NSE", "1660")]
    resolved = contracts.resolve_contracts(market, TODAY)
    assert "no last price" in _reason(resolved, "nse-b")
    assert _kind(resolved, "nse-b") == contracts.NO_LAST_PRICE


def test_a_contract_never_exists_without_a_positive_last_price() -> None:
    with pytest.raises(ValueError, match="last price"):
        Contract(
            role="nse-b",
            exchange="NSE",
            tradingsymbol="ITC-EQ",
            token="1660",
            symbol="",
            kind="eq",
            lot_size=1,
            tick_size=Decimal("0.05"),
            resolved_last_price=Decimal(0),
        )


def test_a_failing_script_master_leaves_that_exchange_unavailable_only() -> None:
    market = _market()
    market.broken = frozenset({"MCX"})
    resolved = contracts.resolve_contracts(market, TODAY)
    for role in ("mcx-crudeoil-f", "mcx-gold-f", "mcx-goldm-f", "mcx-goldpetal-f"):
        assert "MCX script master" in _reason(resolved, role)
        assert _kind(resolved, role) == contracts.MASTER_UNAVAILABLE
    assert "nfo-nifty-f" in resolved.contracts
    assert market.calls.count(("instruments", "MCX")) == 1  # read once per exchange


class ExpiredSession(FakeMarket):
    """The session expired: every call answers with an authentication error."""

    def __init__(self, error: Exception, *, on: str) -> None:
        super().__init__(_market().masters, _market().quotes)
        self.error = error
        self.on = on

    def instruments(self, exchange: str) -> list[dict[str, str]]:
        if self.on == "instruments":
            raise self.error
        return super().instruments(exchange)

    def quote(self, exchange: str, token: str) -> Quote:
        if self.on == "quote":
            raise self.error
        return super().quote(exchange, token)


@pytest.mark.parametrize("on", ["instruments", "quote"])
@pytest.mark.parametrize(
    "error",
    [
        AuthenticationError("Session Expired", endpoint="X", http_status=401),
        ConfigurationError("no access token"),
    ],
)
def test_an_expired_session_is_an_error_not_a_data_gap(error: Exception, on: str) -> None:
    with pytest.raises(type(error)):
        contracts.resolve_contracts(ExpiredSession(error, on=on), TODAY)


def test_each_script_master_is_read_once_per_resolution() -> None:
    market = _market()
    contracts.resolve_contracts(market, TODAY)
    reads = [c for c in market.calls if c[0] == "instruments"]
    assert sorted(reads) == sorted({c for c in reads})


# -- contracts: stock future and option -------------------------------------------------------


def test_stock_future_falls_back_to_the_next_candidate_and_its_option_follows() -> None:
    resolved = contracts.resolve_contracts(_market(), TODAY)
    future = resolved.contracts["nfo-stk-f"]
    assert future.symbol == "HDFCBANK"  # RELIANCE has no last price
    assert future.lot_size == 550
    option = resolved.contracts["nfo-stk-ce-atm"]
    assert option.symbol == "HDFCBANK"
    assert option.strike == Decimal("1740")  # nearest to 1,745 (1740 and 1760: 1740 nearer)
    assert option.option_type == "CE"


# -- contracts: option ladders ----------------------------------------------------------------


def _strike(resolved: Contracts, role: str) -> Decimal | None:
    contract = resolved.contracts.get(role)
    return None if contract is None else contract.strike


def test_the_ladder_picks_atm_itm_and_otm_strikes_from_the_futures_last_price() -> None:
    resolved = contracts.resolve_contracts(_market(), TODAY)
    # NIFTY future 24,010: ATM 24,000; a CE is in the money below, a PE above.
    assert _strike(resolved, "nfo-nifty-ce-atm") == Decimal("24000")
    assert _strike(resolved, "nfo-nifty-ce-itm1") == Decimal("23950")
    assert _strike(resolved, "nfo-nifty-ce-itm2") == Decimal("23900")
    assert _strike(resolved, "nfo-nifty-ce-otm1") == Decimal("24050")
    assert _strike(resolved, "nfo-nifty-ce-otm2") == Decimal("24100")
    assert _strike(resolved, "nfo-nifty-pe-itm1") == Decimal("24050")
    assert _strike(resolved, "nfo-nifty-pe-otm1") == Decimal("23950")
    assert _strike(resolved, "nfo-banknifty-pe-atm") == Decimal("52000")
    assert resolved.contracts["nfo-nifty-ce-atm"].expiry == date(2026, 11, 27)
    assert resolved.contracts["nfo-nifty-ce-atm"].lot_size == 75


def test_the_ladder_records_each_strikes_distance_from_the_money() -> None:
    resolved = contracts.resolve_contracts(_market(), TODAY)
    steps = {
        rung: resolved.contracts[f"nfo-nifty-ce-{rung}"].strikes_from_atm
        for rung in ("atm", "itm1", "itm2", "otm1", "otm2")
    }
    assert steps == {"atm": 0, "itm1": -1, "itm2": -2, "otm1": 1, "otm2": 2}
    assert resolved.contracts["nfo-nifty-pe-itm1"].strikes_from_atm == 1
    assert resolved.contracts["nfo-nifty-ce-atm"].resolved_reference_price == Decimal("24010")
    assert resolved.contracts["nfo-nifty-f"].strikes_from_atm is None


def test_the_ladder_skips_strikes_without_a_last_price() -> None:
    market = _market()
    _no_price(market, "NFO", "NIFTY24000CE", "NIFTY23950CE")
    resolved = contracts.resolve_contracts(market, TODAY)
    # ATM falls to the next nearest strike with a price (24,050); the rungs never share a strike.
    assert _strike(resolved, "nfo-nifty-ce-atm") == Decimal("24050")
    assert _strike(resolved, "nfo-nifty-ce-itm1") == Decimal("23900")
    assert _strike(resolved, "nfo-nifty-ce-itm2") == Decimal("23850")
    assert _strike(resolved, "nfo-nifty-ce-otm1") == Decimal("24100")
    assert _strike(resolved, "nfo-nifty-ce-otm2") == Decimal("24150")
    strikes = [
        _strike(resolved, f"nfo-nifty-ce-{rung}")
        for rung in ("atm", "itm1", "itm2", "otm1", "otm2")
    ]
    assert len(set(strikes)) == 5
    # The recorded distance is the strike actually used, not the rung's name.
    assert resolved.contracts["nfo-nifty-ce-atm"].strikes_from_atm == 1
    assert resolved.contracts["nfo-nifty-ce-itm1"].strikes_from_atm == -2


def _unpriced_nifty_ladder() -> tuple[FakeMarket, list[dict[str, str]]]:
    """NIFTY CE/PE 23,000-24,950 step 50 (40 strikes each), none with a last price."""
    market = _market()
    nifty_options = [
        r for r in market.masters["NFO"] if r.get("Symbol") == "NIFTY" and r.get("OptionType")
    ]
    many = _options("NFO", "NIFTY", NEAR, "75", range(23000, 25000, 50), first_token=5000)
    market.masters["NFO"] = [r for r in market.masters["NFO"] if r not in nifty_options] + many
    for row in many:
        market.quotes[("NFO", row["Token"])] = _quote("0", ls="75")
    return market, many


def test_the_atm_rung_quotes_exactly_ten_strikes_nearest_first_when_none_has_a_price() -> None:
    market, many = _unpriced_nifty_ladder()
    resolved = contracts.resolve_contracts(market, TODAY)
    reason = _reason(resolved, "nfo-nifty-ce-atm")
    assert "no last price" in reason
    assert _kind(resolved, "nfo-nifty-ce-atm") == contracts.NO_LAST_PRICE
    nearest_ten = [24000, 24050, 23950, 24100, 23900, 24150, 23850, 24200, 23800, 24250]
    tried = reason.split("tried ", 1)[1].rstrip(")").split(", ")
    assert tried == [f"NIFTY{strike}CE" for strike in nearest_ten]
    ce_tokens = {r["Token"] for r in many if r["OptionType"] == "CE"}
    first_quotes = [t for t in market.quoted("NFO") if t in ce_tokens][:11]
    by_token = {r["Token"]: r["TradingSymbol"] for r in many}
    assert [by_token[t] for t in first_quotes[:10]] == tried
    assert by_token[first_quotes[10]] == "NIFTY23750CE"  # itm1 goes on below the ATM's ten


def test_every_rung_tries_at_most_ten_strikes_and_quotes_each_once() -> None:
    market, many = _unpriced_nifty_ladder()
    resolved = contracts.resolve_contracts(market, TODAY)
    for option_type in ("ce", "pe"):
        for rung in contracts.LADDER_RUNGS:
            reason = _reason(resolved, f"nfo-nifty-{option_type}-{rung}")
            tried = reason.split("tried ", 1)[1].rstrip(")").split(", ")
            assert 1 <= len(tried) <= contracts.STRIKE_TRIES, (option_type, rung)
    quoted = [t for t in market.quoted("NFO") if t in {r["Token"] for r in many}]
    assert len(quoted) == len(set(quoted))  # a strike is quoted once per resolution


def test_options_lose_their_ladder_when_the_future_has_no_last_price() -> None:
    market = _market()
    market.quotes[("NFO", "102")]["lp"] = "0"
    resolved = contracts.resolve_contracts(market, TODAY)
    assert "nfo-banknifty-f" in resolved.unavailable
    assert "nfo-banknifty-f" in _reason(resolved, "nfo-banknifty-ce-atm")
    assert _kind(resolved, "nfo-banknifty-ce-atm") == contracts.NO_UNDERLYING
    banknifty_tokens = {
        r["Token"]
        for r in market.masters["NFO"]
        if r.get("Symbol") == "BANKNIFTY" and r.get("OptionType")
    }
    assert not banknifty_tokens & set(market.quoted("NFO"))  # never quoted without a reference


def test_option_expiry_is_strictly_after_today() -> None:
    market = _market()
    market.masters["NFO"] += _options(
        "NFO", "NIFTY", "13-NOV-2026", "75", range(23800, 24250, 50), first_token=7000
    )
    for row in market.masters["NFO"]:
        if row["Token"].startswith("7") and len(row["Token"]) == 4:
            market.quotes[("NFO", row["Token"])] = _quote("50", ls="75")
    resolved = contracts.resolve_contracts(market, TODAY)
    assert resolved.contracts["nfo-nifty-ce-atm"].expiry == date(2026, 11, 27)


# -- contracts.json: written per run, reused on resume, refused on another day ------------------


class NoCalls:
    def instruments(self, exchange: str) -> list[dict[str, str]]:
        raise AssertionError("a resumed run must not read the script master again")

    def quote(self, exchange: str, token: str) -> Quote:
        raise AssertionError("a resumed run must not quote again")


def test_contracts_are_written_to_the_run_folder_and_reused_on_resume(tmp_path: Path) -> None:
    market = _market()
    _no_price(market, "NSE", "YESBANK-EQ", "IDEA-EQ")
    first = contracts.resolve_contracts(market, TODAY, run_dir=tmp_path / "run1")
    path = tmp_path / "run1" / "contracts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["day"] == "2026-11-13"
    assert data["contracts"]["mcx-crudeoil-f"]["lot_size"] == 100
    assert data["contracts"]["nse-b"]["resolved_last_price"] == "410.15"
    assert data["unavailable"]["nse-t"]["kind"] == contracts.NO_LAST_PRICE
    again = contracts.resolve_contracts(NoCalls(), TODAY, run_dir=tmp_path / "run1")
    assert again == first


def test_contracts_of_another_trading_day_are_refused(tmp_path: Path) -> None:
    contracts.resolve_contracts(_market(), TODAY, run_dir=tmp_path)
    with pytest.raises(safety.Refusal, match="2026-11-13"):
        contracts.resolve_contracts(NoCalls(), date(2026, 11, 16), run_dir=tmp_path)


def test_a_contracts_file_of_an_unknown_version_is_refused(tmp_path: Path) -> None:
    (tmp_path / "contracts.json").write_text(
        json.dumps({"version": 99, "day": "2026-11-13", "contracts": {}, "unavailable": {}}),
        encoding="utf-8",
    )
    with pytest.raises(safety.Refusal, match="version"):
        contracts.resolve_contracts(NoCalls(), TODAY, run_dir=tmp_path)


def test_a_contracts_file_that_does_not_cover_every_role_is_refused(tmp_path: Path) -> None:
    contracts.resolve_contracts(_market(), TODAY, run_dir=tmp_path)
    path = tmp_path / "contracts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["contracts"]["mcx-goldm-f"]
    data["contracts"]["nfo-finnifty-f"] = data["contracts"]["nfo-nifty-f"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(safety.Refusal, match="mcx-goldm-f") as refused:
        contracts.resolve_contracts(NoCalls(), TODAY, run_dir=tmp_path)
    assert "nfo-finnifty-f" in refused.value.reason


def test_a_contracts_file_with_every_role_unavailable_is_resolved_again(tmp_path: Path) -> None:
    closed = FakeMarket(_market().masters, {})  # before the open: no quote answers
    early = contracts.resolve_contracts(closed, TODAY, run_dir=tmp_path)
    assert early.contracts == {}
    market = _market()
    later = contracts.resolve_contracts(market, TODAY, run_dir=tmp_path)
    assert later.unavailable == {}
    assert any(c[0] == "quote" for c in market.calls)
    saved = json.loads((tmp_path / "contracts.json").read_text(encoding="utf-8"))
    assert saved["unavailable"] == {}
    assert contracts.resolve_contracts(NoCalls(), TODAY, run_dir=tmp_path) == later


def test_the_contracts_file_holds_no_account_ids(tmp_path: Path) -> None:
    contracts.resolve_contracts(_market(), TODAY, run_dir=tmp_path)
    text = (tmp_path / "contracts.json").read_text(encoding="utf-8")
    for key in ("client_id", "user_id", "account_id", "uid", "actid"):
        assert f'"{key}"' not in text


# -- pricing: only from a quote taken just before placing ------------------------------------

D = Decimal


def _snap(
    last: str, tick: str = "0.05", lc: str | None = None, uc: str | None = None
) -> pricing.PriceSnapshot:
    return pricing.PriceSnapshot(
        tradingsymbol="X",
        last_price=D(last),
        tick_size=D(tick),
        lower_circuit=None if lc is None else D(lc),
        upper_circuit=None if uc is None else D(uc),
    )


def test_far_buy_is_ten_percent_below_floored_to_the_tick() -> None:
    assert pricing.far_buy(_snap("101.23")) == D("91.10")


def test_far_buy_is_clamped_to_the_lower_circuit_rounded_up() -> None:
    assert pricing.far_buy(_snap("100", lc="94.97")) == D("95.00")


def test_far_buy_refuses_a_price_less_than_one_percent_below_the_last() -> None:
    with pytest.raises(safety.Refusal, match="well below"):
        pricing.far_buy(_snap("100", lc="99.5"))


def test_far_sell_is_ten_percent_above_ceiled_to_the_tick() -> None:
    assert pricing.far_sell(_snap("101.23")) == D("111.40")


def test_far_sell_is_clamped_to_the_upper_circuit_rounded_down() -> None:
    assert pricing.far_sell(_snap("100", uc="105.03")) == D("105.00")


def test_far_sell_refuses_a_price_less_than_one_percent_above_the_last() -> None:
    with pytest.raises(safety.Refusal, match="well above"):
        pricing.far_sell(_snap("100", uc="100.5"))


@pytest.mark.parametrize(("last", "tick"), [("0", "0.05"), ("-1", "0.05"), ("100", "0")])
def test_a_snapshot_never_exists_without_a_positive_price_and_tick(last: str, tick: str) -> None:
    with pytest.raises(ValueError):
        _snap(last, tick)


def test_stop_buy_triggers_above_the_market() -> None:
    assert pricing.stop_buy(_snap("100")) == (D("103.00"), D("104.00"))
    assert pricing.stop_buy(_snap("100"), option=True) == (D("110.00"), D("112.00"))


def test_stop_buy_refuses_when_the_upper_circuit_is_below_the_trigger() -> None:
    with pytest.raises(safety.Refusal):
        pricing.stop_buy(_snap("100", uc="102"))


def test_stop_sell_triggers_below_the_market_with_its_limit_below_the_trigger() -> None:
    assert pricing.stop_sell(_snap("100")) == (D("97.00"), D("96.00"))
    assert pricing.stop_sell(_snap("100"), option=True) == (D("90.00"), D("88.00"))
    assert pricing.stop_sell(_snap("100", lc="96.42")) == (D("97.00"), D("96.45"))


def test_stop_sell_refuses_when_the_lower_circuit_is_above_the_trigger() -> None:
    with pytest.raises(safety.Refusal):
        pricing.stop_sell(_snap("100", lc="97.5"))


def test_at_last_rounds_toward_a_fill() -> None:
    assert pricing.at_last(_snap("100.03"), "BUY") == D("100.05")
    assert pricing.at_last(_snap("100.03"), "SELL") == D("100.00")


@pytest.mark.parametrize(
    "price",
    [
        lambda x: pricing.far_buy(x),
        lambda x: pricing.far_sell(x),
        lambda x: pricing.stop_buy(x),
        lambda x: pricing.stop_sell(x),
        lambda x: pricing.at_last(x, "BUY"),
    ],
)
def test_pricing_cannot_be_called_with_a_contract(price: Any) -> None:
    itc = contracts.resolve_contracts(_market(), TODAY).contracts["nse-b"]
    with pytest.raises(TypeError, match="fresh_quote"):
        price(itc)


def test_a_fresh_quote_prices_the_order_not_the_resolution_snapshot() -> None:
    market = _market()
    resolved = contracts.resolve_contracts(market, TODAY)
    option = resolved.contracts["nfo-nifty-ce-atm"]
    assert option.resolved_last_price == D("120.5")
    # Later the same day the premium has fallen to 40: a "far" BUY priced from the resolution
    # snapshot (120.5 - 10 % = 108.45) would be far above the market and fill.
    market.quotes[("NFO", option.token)] = _quote("40", ls="75", lc="0.05", uc="400")
    snapshot = pricing.fresh_quote(market, option)
    assert snapshot.last_price == D("40")
    assert (snapshot.lower_circuit, snapshot.upper_circuit) == (D("0.05"), D("400"))
    assert pricing.far_buy(snapshot) == D("36.00")
    assert market.calls[-1] == ("quote", "NFO", option.token)


def test_a_fresh_quote_takes_the_tick_from_the_quote_else_from_the_contract() -> None:
    market = _market()
    itc = contracts.resolve_contracts(market, TODAY).contracts["nse-b"]
    market.quotes[("NSE", "1660")] = _quote("400", ti="0.10")
    assert pricing.fresh_quote(market, itc).tick_size == D("0.10")
    market.quotes[("NSE", "1660")].pop("ti")
    assert pricing.fresh_quote(market, itc).tick_size == itc.tick_size


@pytest.mark.parametrize("quote", [None, {"lp": "0"}, {"lp": "-1"}, {"ti": "0.05"}])
def test_a_fresh_quote_without_a_last_price_refuses(quote: dict[str, str] | None) -> None:
    market = _market()
    itc = contracts.resolve_contracts(market, TODAY).contracts["nse-b"]
    if quote is None:
        del market.quotes[("NSE", "1660")]
    else:
        market.quotes[("NSE", "1660")] = quote
    with pytest.raises(safety.Refusal, match="ITC-EQ"):
        pricing.fresh_quote(market, itc)


def test_a_fresh_quote_with_another_lot_size_refuses() -> None:
    market = _market()
    crude = contracts.resolve_contracts(market, TODAY).contracts["mcx-crudeoil-f"]
    market.quotes[("MCX", "900")]["ls"] = "10"
    with pytest.raises(safety.Refusal, match="lot size"):
        pricing.fresh_quote(market, crude)


def test_a_fresh_quote_lets_an_expired_session_through() -> None:
    itc = contracts.resolve_contracts(_market(), TODAY).contracts["nse-b"]
    expired = ExpiredSession(
        AuthenticationError("Session Expired", endpoint="GetQuotes", http_status=401),
        on="quote",
    )
    with pytest.raises(AuthenticationError):
        pricing.fresh_quote(expired, itc)


# -- the case table ---------------------------------------------------------------------------

PRODUCTS = {"CNC", "MIS", "NRML", "CO", "BO", "MTF"}
PRICE_TYPES = {"LMT", "SL-LMT", "MKT", "SL-MKT"}


def test_case_ids_are_unique_and_every_case_is_well_formed() -> None:
    ids = [case.id for case in cases.CASES]
    assert len(ids) == len(set(ids))
    for case in cases.CASES:
        assert case.role in contracts.ROLES, case.id
        assert case.product in PRODUCTS, case.id
        assert case.prctyp in PRICE_TYPES, case.id
        assert case.ret in {"DAY", "IOC", "EOS"}, case.id
        assert case.side in {"BUY", "SELL"}, case.id
        assert case.orders >= 1, case.id
        assert case.outcome in cases.OUTCOMES, case.id
        assert case.tier in cases.TIERS, case.id
        assert case.id == case.id.lower() and " " not in case.id


def test_a_case_knows_its_segment_and_exchange() -> None:
    case = cases.case("shapes-bfo-sensex-f-nrml-lmt-eos-buy")
    assert (case.segment, case.exchange) == ("bfo", "BFO")
    assert case.outcome == cases.REST
    with pytest.raises(KeyError):
        cases.case("no-such-case")


def test_the_shape_grid_covers_products_types_validities_and_sides() -> None:
    grid = [c for c in cases.CASES if c.family == "shapes" and c.role == "nfo-nifty-f"]
    combos = {(c.product, c.prctyp, c.ret, c.side) for c in grid}
    for product in ("NRML", "MIS"):
        for prctyp in ("LMT", "SL-LMT"):
            for ret in ("DAY", "IOC", "EOS"):
                for side in ("BUY", "SELL"):
                    assert (product, prctyp, ret, side) in combos
        for prctyp in ("MKT", "SL-MKT"):
            for side in ("BUY", "SELL"):
                assert (product, prctyp, "DAY", side) in combos
    cash = {c.product for c in cases.CASES if c.family == "shapes" and c.role == "nse-b"}
    assert {"CNC", "MIS", "MTF"} <= cash
    assert all(c.orders == 1 for c in grid)


def test_documented_outcomes_of_the_shapes() -> None:
    assert cases.case("shapes-nse-b-mis-lmt-day-buy").outcome == cases.REST
    assert cases.case("shapes-nse-b-mis-lmt-ioc-buy").outcome == cases.EXCHANGE_CANCEL
    assert cases.case("shapes-nse-b-mis-sl-lmt-day-sell").outcome == cases.TRIGGER_PENDING
    assert cases.case("shapes-nse-b-mis-lmt-eos-buy").outcome == cases.REJECT  # EOS: BSE/BFO only
    assert cases.case("shapes-nfo-nifty-f-nrml-lmt-eos-sell").outcome == cases.REJECT
    assert cases.case("shapes-bfo-sensex-f-nrml-sl-lmt-eos-buy").outcome == cases.TRIGGER_PENDING
    assert cases.case("shapes-nse-b-mis-mkt-day-buy").outcome == cases.REJECT
    assert cases.case("shapes-nse-b-cnc-lmt-day-sell").outcome == cases.RECORD  # no holdings
    assert cases.case("shapes-nse-b-mtf-lmt-day-sell").outcome == cases.RECORD  # no MTF holding
    assert cases.case("shapes-nse-b-mtf-lmt-day-buy").outcome == cases.REST
    assert cases.case("shapes-nse-b-mis-sl-lmt-ioc-buy").outcome == cases.RECORD


def test_mcx_validities_are_predicted_only_where_known() -> None:
    # The retention annexure lists no MCX row: only DAY (accepted on UAT MCX) is predicted.
    assert cases.case("mcx-mcx-crudeoil-f-nrml-lmt-day-buy").outcome == cases.REST
    assert cases.case("mcx-mcx-crudeoil-f-nrml-sl-lmt-day-sell").outcome == cases.TRIGGER_PENDING
    assert cases.case("mcx-mcx-crudeoil-f-nrml-lmt-ioc-buy").outcome == cases.RECORD
    assert cases.case("mcx-mcx-gold-f-mis-lmt-eos-sell").outcome == cases.RECORD
    assert cases.case("mcx-mcx-gold-f-mis-mkt-day-sell").outcome == cases.REJECT  # F27


def test_mcx_cases_cover_the_four_commodities() -> None:
    roles = {c.role for c in cases.CASES if c.family == "mcx"}
    assert roles == {"mcx-crudeoil-f", "mcx-gold-f", "mcx-goldm-f", "mcx-goldpetal-f"}


def test_bse_and_bfo_each_have_one_canary() -> None:
    canaries = [c for c in cases.CASES if c.canary]
    assert sorted(c.segment for c in canaries) == ["bfo", "bse"]
    for canary in canaries:
        assert (canary.prctyp, canary.ret, canary.side, canary.orders) == ("LMT", "DAY", "BUY", 1)
        assert canary.tier == "smoke"


def test_fill_cases_declare_the_buy_and_its_closing_sell() -> None:
    fills = [c for c in cases.CASES if c.outcome == cases.FILL_CLOSE]
    assert {c.role for c in fills} >= {
        "nse-b",
        "nfo-nifty-f",
        "nfo-nifty-ce-atm",
        "bfo-sensex-ce-atm",
    }
    assert all(c.orders == 2 and c.side == "BUY" and c.prctyp == "LMT" for c in fills)
    shorts = [c for c in cases.CASES if c.outcome == cases.SHORT_COVER]
    assert shorts and all(c.side == "SELL" and c.product == "MIS" and c.orders == 2 for c in shorts)


def test_every_segment_has_smoke_cases_and_smoke_is_small() -> None:
    smoke = [c for c in cases.CASES if c.tier == "smoke"]
    assert {c.segment for c in smoke} == {"nse", "bse", "nfo", "bfo", "mcx"}
    assert sum(c.orders for c in smoke) <= 80
    assert cases.select("smoke") == smoke
    assert set(cases.select("full")) >= set(smoke)
    assert len(cases.select("full")) == len(cases.CASES)


# -- the expectation oracle -------------------------------------------------------------------


def _details(**overrides: Any) -> UserDetails:
    data: dict[str, Any] = {
        "exarr": ["NSE", "NFO", "BSE", "BFO", "MCX"],
        "orarr": ["MKT", "LMT", "SL-LMT", "SL-MKT", "DS"],
        "prarr": [
            {"prd": "C", "s_prdt_ali": "CNC", "exch": ["NSE", "BSE"]},
            {"prd": "I", "s_prdt_ali": "MIS", "exch": ["NSE", "BSE", "NFO", "BFO", "MCX"]},
            {"prd": "M", "s_prdt_ali": "NRML", "exch": ["NFO", "BFO", "MCX"]},
            {"prd": "H", "s_prdt_ali": "CO", "exch": ["NSE"]},
        ],
    }
    data.update(overrides)
    return UserDetails.model_validate(data)


PROFILE = Profile.from_details(_details())


def _issues(**kwargs: Any) -> KnownIssues:
    issues = KnownIssues(**kwargs)
    issues.canary_ok("bse")
    issues.canary_ok("bfo")
    return issues


def test_profile_is_read_from_user_details() -> None:
    assert PROFILE.exchanges == frozenset({"NSE", "NFO", "BSE", "BFO", "MCX"})
    assert "SL-LMT" in PROFILE.price_types
    assert PROFILE.product_enabled("CNC", "NSE") is True
    assert PROFILE.product_enabled("CNC", "NFO") is False
    assert PROFILE.product_enabled("MTF", "NSE") is False
    assert PROFILE.product_enabled("CO", "NSE") is True


def test_an_enabled_resting_shape_is_expected_to_be_accepted() -> None:
    result = expect.expectation(
        cases.case("shapes-nfo-nifty-f-nrml-lmt-day-buy"), PROFILE, _issues()
    )
    assert result.verdict == expect.ACCEPT
    assert result.issue is None


def test_a_product_not_enabled_on_the_exchange_is_expected_to_be_rejected() -> None:
    result = expect.expectation(cases.case("shapes-nse-b-mtf-lmt-day-buy"), PROFILE, _issues())
    assert result.verdict == expect.REJECT
    assert "MTF" in result.reason and "prarr" in result.reason


def test_an_exchange_not_enabled_is_expected_to_be_rejected() -> None:
    profile = Profile.from_details(_details(exarr=["NSE", "NFO"]))
    result = expect.expectation(
        cases.case("mcx-mcx-crudeoil-f-nrml-lmt-day-buy"), profile, _issues()
    )
    assert result.verdict == expect.REJECT
    assert "exarr" in result.reason


def test_a_price_type_not_enabled_is_expected_to_be_rejected() -> None:
    profile = Profile.from_details(_details(orarr=["LMT", "MKT"]))
    result = expect.expectation(cases.case("shapes-nse-b-mis-sl-lmt-day-buy"), profile, _issues())
    assert result.verdict == expect.REJECT
    assert "orarr" in result.reason


def test_an_unknown_profile_list_does_not_predict_a_rejection() -> None:
    profile = Profile.from_details(_details(prarr=[], exarr=None))
    result = expect.expectation(cases.case("shapes-nse-b-mtf-lmt-day-buy"), profile, _issues())
    assert result.verdict == expect.ACCEPT


def test_f27_market_orders_pass_when_refused_by_algo_chk() -> None:
    for case_id in ("shapes-nse-b-mis-mkt-day-buy", "shapes-nfo-nifty-f-nrml-sl-mkt-day-sell"):
        result = expect.expectation(cases.case(case_id), PROFILE, _issues())
        assert (result.verdict, result.issue, result.words) == (expect.REJECT, "F27", "ALGO_CHK")
        assert result.accepts_rejection("Rejected : ALGO_CHK: SL-MKT Order type not allowed")
        assert not result.accepts_rejection("RED:RULE:{Turnover limit}")
        assert not result.accepts_rejection(None)


def test_f27_another_refusal_message_is_recorded_not_unexpected() -> None:
    result = expect.expectation(cases.case("shapes-nse-b-mis-mkt-day-buy"), PROFILE, _issues())
    assert result.other_rejection == expect.RECORD
    assert result.judge_rejection("Rejected : ALGO_CHK: MKT Order type not allowed") == (
        expect.EXPECTED
    )
    assert result.judge_rejection("Order type not permitted for this product") == expect.RECORD
    assert result.judge_rejection(None) == expect.RECORD


def test_documented_rejections_take_any_message() -> None:
    result = expect.expectation(cases.case("shapes-nse-b-mis-lmt-eos-buy"), PROFILE, _issues())
    assert result.verdict == expect.REJECT
    assert result.accepts_rejection("EOS not allowed")
    assert result.judge_rejection("EOS not allowed") == expect.EXPECTED
    accepted = expect.expectation(cases.case("shapes-nse-b-mis-lmt-day-buy"), PROFILE, _issues())
    assert not accepted.accepts_rejection("anything")
    assert accepted.judge_rejection("anything") == expect.UNEXPECTED


def test_f26_bse_and_bfo_wait_for_their_canary() -> None:
    issues = KnownIssues()
    canary = next(c for c in cases.CASES if c.canary and c.segment == "bse")
    other = cases.case("bse-bse-b-cnc-lmt-day-buy")
    first = expect.expectation(canary, PROFILE, issues)
    assert (first.verdict, first.issue) == (expect.RECORD, "F26")
    waiting = expect.expectation(other, PROFILE, issues)
    assert (waiting.verdict, waiting.issue) == (expect.SKIP, "F26")
    assert "canary" in waiting.reason
    assert expect.expectation(
        cases.case("shapes-nse-b-mis-lmt-day-buy"), PROFILE, issues
    ).verdict == (expect.ACCEPT)
    issues.canary_ok("bse")
    assert expect.expectation(other, PROFILE, issues).verdict == expect.ACCEPT


def test_f26_a_stuck_canary_skips_the_segment_for_the_run() -> None:
    issues = KnownIssues()
    issues.canary_stuck("bfo", "26111300001")
    for case in [c for c in cases.CASES if c.segment == "bfo"]:
        result = expect.expectation(case, PROFILE, issues)
        assert (result.verdict, result.issue) == (expect.SKIP, "F26"), case.id
        assert "26111300001" in result.reason
    assert issues.stuck_orders == {"26111300001"}


def test_rms_red_rejection_skips_the_rest_of_that_family_only() -> None:
    assert expect.is_rms_limit("RED:RULE:{Turnover limit} Current: INR 14,81,980.50")
    assert not expect.is_rms_limit("ORA:Quantity 1 is not a multiple of lot size 100.00")
    assert not expect.is_rms_limit(None)
    issues = _issues()
    issues.rms_limit("fills", "RED:RULE:{Turnover limit} Set: INR 1,00,000.00")
    fill = expect.expectation(cases.case("fills-nfo-nifty-f-nrml-lmt-day-buy"), PROFILE, issues)
    assert (fill.verdict, fill.issue) == (expect.SKIP, "RMS")
    assert "environment limit" in fill.reason and "Turnover" in fill.reason
    shape = expect.expectation(cases.case("shapes-nfo-nifty-f-nrml-lmt-day-buy"), PROFILE, issues)
    assert shape.verdict == expect.ACCEPT


def test_rms_limit_ignores_a_message_that_is_not_an_rms_rejection() -> None:
    issues = _issues()
    with pytest.raises(ValueError, match="RED:"):
        issues.rms_limit("fills", "ALGO_CHK")


def test_a_role_without_a_contract_skips_with_the_reason() -> None:
    market = _market()
    _no_price(market, "NSE", "YESBANK-EQ", "IDEA-EQ")
    resolved = contracts.resolve_contracts(market, TODAY)
    issues = _issues()
    issues.add_unavailable(resolved)
    result = expect.expectation(cases.case("shapes-nse-t-mis-lmt-day-buy"), PROFILE, issues)
    assert (result.verdict, result.issue) == (expect.SKIP, contracts.NO_LAST_PRICE)
    assert "IDEA-EQ" in result.reason


def test_each_unavailability_reason_keeps_its_own_issue_key() -> None:
    market = _market()
    market.broken = frozenset({"MCX"})
    market.quotes[("NFO", "102")]["lp"] = "0"
    resolved = contracts.resolve_contracts(market, TODAY)
    issues = _issues()
    issues.add_unavailable(resolved)
    mcx = expect.expectation(cases.case("mcx-mcx-gold-f-nrml-lmt-day-buy"), PROFILE, issues)
    assert (mcx.verdict, mcx.issue) == (expect.SKIP, contracts.MASTER_UNAVAILABLE)
    future = expect.expectation(
        cases.case("shapes-nfo-banknifty-f-nrml-lmt-day-buy"), PROFILE, issues
    )
    assert future.issue == contracts.NO_LAST_PRICE


def test_closing_sell_a_fill_is_accepted_and_its_closing_sell_is_a_non_strict_xfail() -> None:
    result = expect.expectation(
        cases.case("fills-nfo-nifty-f-nrml-lmt-day-buy"), PROFILE, _issues()
    )
    assert result.verdict == expect.ACCEPT
    assert result.close is not None
    assert (result.close.verdict, result.close.issue, result.close.strict) == (
        expect.XFAIL,
        "closing-sell",
        False,
    )


def test_undocumented_behaviour_is_recorded_until_pinned() -> None:
    case = cases.case("shapes-nse-b-cnc-lmt-day-sell")
    assert expect.expectation(case, PROFILE, _issues()).verdict == expect.RECORD
    pinned = {case.id: {"verdict": "reject", "words": "RMS"}}
    result = expect.expectation(case, PROFILE, _issues(pinned=pinned))
    assert (result.verdict, result.words, result.issue) == (expect.REJECT, "RMS", "pinned")
    accept = expect.expectation(case, PROFILE, _issues(pinned={case.id: {"verdict": "accept"}}))
    assert accept.verdict == expect.ACCEPT


def test_a_pin_never_overrides_a_documented_expectation() -> None:
    case = cases.case("shapes-nse-b-mis-lmt-day-buy")
    result = expect.expectation(case, PROFILE, _issues(pinned={case.id: {"verdict": "reject"}}))
    assert result.verdict == expect.ACCEPT


def test_the_known_issue_table_names_every_issue() -> None:
    assert set(expect.KNOWN_ISSUES) == {
        "F26",
        "F27",
        "closing-sell",
        "RMS",
        "record",
        *contracts.UNAVAILABLE_KINDS,
    }
    assert len(contracts.UNAVAILABLE_KINDS) == 7
    for kind in contracts.UNAVAILABLE_KINDS:
        assert expect.KNOWN_ISSUES[kind].verdict == expect.SKIP
    for key, issue in expect.KNOWN_ISSUES.items():
        assert issue.key == key
        assert issue.verdict in {expect.SKIP, expect.XFAIL, expect.RECORD, expect.REJECT}
        assert issue.description


# -- uat_expectations.json --------------------------------------------------------------------


def test_the_expectations_file_starts_as_an_empty_versioned_skeleton() -> None:
    data = json.loads(expect.EXPECTATIONS_FILE.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["cases"] == {}
    assert expect.load_pinned() == {}


@pytest.mark.parametrize(
    "case_id",
    [
        "fills-nfo-banknifty-f-mis-lmt-day-buy-partial",
        "fills-nfo-banknifty-f-mis-lmt-day-sell-partial-close",
        "cobo-nse-t-co-lmt-day-buy-fill",
        "cobo-nse-t-bo-lmt-day-buy-fill",
        "hold-nse-hold-cnc-lmt-day-sell-fill",
    ],
)
def test_a_recorded_fill_case_is_never_pinned_accept(tmp_path: Path, case_id: str) -> None:
    # U13 ruling: whether these fill depends on the market of the moment; only a refusal pins.
    path = tmp_path / "x.json"
    path.write_text(
        json.dumps({"version": 1, "cases": {case_id: {"verdict": "accept"}}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="recorded fill"):
        expect.load_pinned(path)
    path.write_text(
        json.dumps({"version": 1, "cases": {case_id: {"verdict": "reject", "words": "RED:"}}}),
        encoding="utf-8",
    )
    assert expect.load_pinned(path) == {case_id: {"verdict": "reject", "words": "RED:"}}


def test_pinned_expectations_are_validated(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"version": 2, "cases": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        expect.load_pinned(path)
    path.write_text(
        json.dumps({"version": 1, "cases": {"a": {"verdict": "maybe"}}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="verdict"):
        expect.load_pinned(path)
    path.write_text(
        json.dumps({"version": 1, "cases": {"a": {"verdict": "reject", "words": "X"}}}),
        encoding="utf-8",
    )
    pinned: Mapping[str, Mapping[str, str]] = expect.load_pinned(path)
    assert pinned == {"a": {"verdict": "reject", "words": "X"}}


def test_cases_are_plain_frozen_data() -> None:
    case = cases.CASES[0]
    assert isinstance(case, Case)
    with pytest.raises(AttributeError):
        case.id = "x"  # type: ignore[misc]


# -- option far prices: 4 % from the market (the exchange's LPP band) ---------------------------


def test_far_buy_option_is_four_percent_below() -> None:
    snap = _snap("100.00", lc="1.00")
    assert pricing.far_buy(snap, option=True) == D("96.00")
    assert pricing.far_buy(snap) == D("90.00")  # non-option unchanged


def test_far_sell_option_is_four_percent_above() -> None:
    snap = _snap("100.00", uc="500.00")
    assert pricing.far_sell(snap, option=True) == D("104.00")
    assert pricing.far_sell(snap) == D("110.00")


def test_far_option_prices_keep_the_circuit_clamp_and_the_one_percent_refusal() -> None:
    assert pricing.far_buy(_snap("100", lc="97.03"), option=True) == D("97.05")
    assert pricing.far_sell(_snap("100", uc="102.97"), option=True) == D("102.95")
    with pytest.raises(safety.Refusal):
        pricing.far_buy(_snap("100", lc="99.5"), option=True)
    with pytest.raises(safety.Refusal):
        pricing.far_sell(_snap("100", uc="100.5"), option=True)


def test_is_lpp_refusal() -> None:
    assert expect.is_lpp_refusal("17070: The Price is out of the current LPP range.")
    assert not expect.is_lpp_refusal("RED:RULE:{Check circuit limit ...}")
    assert not expect.is_lpp_refusal(None)


def test_is_order_rate_refusal() -> None:
    message = "Invalid Input :  Order Recieved 202 in a current minute exceeds Limit 200 for user"
    assert expect.is_order_rate_refusal(message)
    assert not expect.is_order_rate_refusal("exceeds Limit 10 per second")
    assert not expect.is_order_rate_refusal("RED:RULE:{Turnover limit}")
    assert not expect.is_order_rate_refusal(None)
