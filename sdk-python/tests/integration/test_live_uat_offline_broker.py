"""The UAT read tests (``tests/live/test_live_uat.py``) driven against the offline broker."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest

from tests.live import test_live_uat as uat
from tests.live._helpers import ContractResolver
from tradesmart import TradeSmart
from tradesmart.testing import OfflineBroker

EXPIRY = date.today() + timedelta(days=14)
HOLDING = {
    "stat": "Ok",
    "exch_tsym": [{"exch": "NSE", "tsym": "ITC-EQ", "token": "1660", "ti": "0.05", "ls": "1"}],
    "holdqty": "10",
    "dpqty": "10",
    "usedqty": "0",
    "upldprc": "380.00",
}
POSITION = {"exch": "NSE", "tsym": "ITC-EQ", "prd": "C", "netqty": "5", "netavgprc": "390.00"}


def _options(
    broker: OfflineBroker,
    exchange: str,
    name: str,
    base: int,
    step: int,
    future_symbol: str | None = None,
    option_symbol: str | None = None,
    expiry: date = EXPIRY,
) -> None:
    """``name`` future and CE/PE options; the script-master Symbols default to ``name``."""
    common: dict[str, Any] = {"lot_size": 65, "expiry": expiry}
    broker.add_instrument(
        exchange,
        f"{name}F",
        f"{name}FUT",
        instrument="FUTIDX",
        underlying=future_symbol or name,
        **common,
    )
    broker.set_quote(exchange, f"{name}F", lp=str(base + 40), ls="65")
    for i, strike in enumerate((base - step, base, base + step)):
        for side in ("CE", "PE"):
            token = f"{name}{i}{side}"
            broker.add_instrument(
                exchange,
                token,
                f"{name}{strike}{side}",
                instrument="OPTIDX",
                strike=strike,
                option_type=side,
                underlying=option_symbol or name,
                **common,
            )
            lp = "0.00" if strike == base else "150.00"  # the ATM strike has no trades on UAT
            broker.set_quote(exchange, token, lp=lp, ls="65")


@pytest.fixture
def client() -> Iterator[TradeSmart]:
    with OfflineBroker() as broker:
        broker.set_holdings([HOLDING])
        broker.set_positions([POSITION])
        broker.set_funds(cash="100000.00", payin="0.00")
        _options(broker, "NFO", "NIFTY", 26000, 100)
        # BFO (2026-11-12): SENSEX future Symbol BSXFUT, options BSXOPT. SENSEX 50 (SX50*)
        # expires earlier with strikes 50 off: taken only if the SENSEX lookup were too loose.
        _options(broker, "BFO", "SENSEX", 85000, 100, "BSXFUT", "BSXOPT")
        _options(
            broker, "BFO", "SENSEX50", 85050, 100, "SX50FUT", "SX50OPT", EXPIRY - timedelta(days=7)
        )
        built = broker.client()
        try:
            yield built
        finally:
            built.close()


def _observe(**data: Any) -> None:
    pass


def test_account_reads_pass(client: TradeSmart, caplog: pytest.LogCaptureFixture) -> None:
    uat.test_uat_holdings_are_parsed(client, _observe, caplog)
    uat.test_uat_positions_are_parsed(client, _observe, caplog)
    uat.test_uat_margins_are_consistent(client, _observe, caplog)


@pytest.mark.parametrize(
    ("holdings", "positions", "message"),
    [
        ([{**HOLDING, "upldprc": ""}], [POSITION], "upldprc"),
        ([], [POSITION], "holdings"),
    ],
    ids=["no-average-price", "no-holdings"],
)
def test_account_reads_fail_on_missing_data(
    caplog: pytest.LogCaptureFixture,
    holdings: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    message: str,
) -> None:
    with OfflineBroker() as broker:
        broker.set_holdings(holdings)
        broker.set_positions(positions)
        other = broker.client()
        try:
            with pytest.raises(AssertionError, match=message):
                uat.test_uat_holdings_are_parsed(other, _observe, caplog)
                uat.test_uat_positions_are_parsed(other, _observe, caplog)
        finally:
            other.close()


def test_positions_read_skips_on_an_empty_book(caplog: pytest.LogCaptureFixture) -> None:
    with OfflineBroker() as broker:
        broker.set_positions([])
        other = broker.client()
        try:
            with pytest.raises(pytest.skip.Exception, match="no position"):
                uat.test_uat_positions_are_parsed(other, _observe, caplog)
        finally:
            other.close()


def test_positions_read_passes_with_only_closed_positions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with OfflineBroker() as broker:
        broker.set_positions([{**POSITION, "netqty": "0"}])
        other = broker.client()
        try:
            uat.test_uat_positions_are_parsed(other, _observe, caplog)
        finally:
            other.close()


def test_option_reads_look_up_sensex_by_its_bfo_symbols() -> None:
    assert uat.OPTION_UNDERLYINGS == [
        ("NFO", "NIFTY", "NIFTY", "NIFTY"),
        ("BFO", "SENSEX", "BSXFUT", "BSXOPT"),
    ]


@pytest.mark.parametrize(
    ("exchange", "name", "future_symbol", "option_symbol"), uat.OPTION_UNDERLYINGS
)
def test_option_reads_take_the_nearest_strike_with_a_price(
    client: TradeSmart, exchange: str, name: str, future_symbol: str, option_symbol: str
) -> None:
    resolver = ContractResolver(client.instruments, lambda e, t: None, client.search_instruments)
    seen: dict[str, Any] = {}

    def run() -> None:
        uat.test_uat_option_reads_around_the_money(
            client,
            resolver,
            exchange,
            name,
            future_symbol,
            option_symbol,
            lambda **data: seen.update(data),
        )

    run()  # on BFO, option_greeks takes the BSXFUT future for a BSXOPT option (F28)
    assert seen["greeks"]["status"] in uat.GREEKS_STATUSES
    assert seen["greeks"]["underlying"] == seen["future"]
    assert seen["future"] == f"{name}FUT"  # never SENSEX50FUT (SX50FUT)
    assert seen["option"] == f"{name}{(26100 if name == 'NIFTY' else 85100)}CE"
    assert seen["chain"], "the reads before the Greeks ran"


def _bfo_reads(client: TradeSmart, resolver: ContractResolver) -> None:
    uat.test_uat_option_reads_around_the_money(
        client, resolver, "BFO", "SENSEX", "BSXFUT", "BSXOPT", _observe
    )


@pytest.mark.parametrize(
    ("keep", "message"),
    [
        ("FUTIDX", "no SENSEX CE of a future expiry in the BFO script master"),
        ("OPTIDX", "no SENSEX future in the BFO script master"),
        (None, "the BFO script master is unavailable"),
    ],
    ids=["no-options", "no-future", "no-file"],
)
def test_option_reads_skip_with_the_missing_bfo_data(
    client: TradeSmart, keep: str | None, message: str
) -> None:
    def rows_for(exchange: str) -> list[dict[str, str]]:
        if keep is None:
            raise ValueError("no BFO file")
        return [r for r in client.instruments(exchange) if r.get("Instrument") == keep]

    resolver = ContractResolver(rows_for, lambda e, t: None, client.search_instruments)
    with pytest.raises(pytest.skip.Exception, match=message):
        _bfo_reads(client, resolver)


def test_option_reads_never_take_sensex_50_for_sensex(client: TradeSmart) -> None:
    def rows_for(exchange: str) -> list[dict[str, str]]:
        return [r for r in client.instruments(exchange) if r.get("Symbol", "").startswith("SX50")]

    resolver = ContractResolver(rows_for, lambda e, t: None, client.search_instruments)
    with pytest.raises(pytest.skip.Exception, match="no SENSEX future in the BFO script master"):
        _bfo_reads(client, resolver)
