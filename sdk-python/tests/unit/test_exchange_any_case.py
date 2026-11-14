"""The exchange is accepted in any case and always sent upper-case (review 2026-11-10 1d)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tradesmart import Exchange
from tradesmart.endpoints.base import Call
from tradesmart.endpoints.market import (
    eod_symbol,
    option_chain_call,
    quote_call,
    security_info_call,
    time_price_series_call,
)
from tradesmart.endpoints.orders import modify_order_call, order_margin_call, place_order_call
from tradesmart.errors import RequestValidationError
from tradesmart.instruments import script_master_url
from tradesmart.stream.protocol import format_keys

SPELLINGS = ["nse", "Nse", " NSE ", "NSE", Exchange.NSE]

BUILDERS: dict[str, Callable[[Any], Call[Any]]] = {
    "quote": lambda exch: quote_call("TEST123", exch, "22"),
    "security_info": lambda exch: security_info_call("TEST123", exch, "22"),
    "option_chain": lambda exch: option_chain_call("TEST123", exch, "NIFTY", "24000", 5),
    "historical_data": lambda exch: time_price_series_call("TEST123", exch, "22"),
    "place_order": lambda exch: place_order_call(
        "TEST123",
        exchange=exch,
        tradingsymbol="ACC-EQ",
        quantity=1,
        transaction_type="BUY",
        product="MIS",
        order_type="LIMIT",
        price="100",
    ),
    "modify_order": lambda exch: modify_order_call(
        "TEST123",
        order_id="1",
        exchange=exch,
        tradingsymbol="ACC-EQ",
        quantity=1,
        price="100",
        order_type="LIMIT",
        product="MIS",
    ),
    "order_margin": lambda exch: order_margin_call(
        "TEST123",
        exchange=exch,
        tradingsymbol="ACC-EQ",
        quantity=1,
        price="100",
        product="MIS",
        transaction_type="BUY",
        order_type="LIMIT",
    ),
}


@pytest.mark.parametrize("spelling", SPELLINGS, ids=repr)
def test_exchange_enum_resolves_case_insensitively(spelling) -> None:
    assert Exchange(spelling) is Exchange.NSE


def test_exchange_enum_still_rejects_unknown_codes() -> None:
    for bad in ("XYZ", "", "N SE"):
        with pytest.raises(ValueError):
            Exchange(bad)


@pytest.mark.parametrize("spelling", SPELLINGS, ids=repr)
@pytest.mark.parametrize("builder", sorted(BUILDERS))
def test_every_request_builder_sends_the_upper_case_code(builder, spelling) -> None:
    assert BUILDERS[builder](spelling).payload["exch"] == "NSE"


@pytest.mark.parametrize("builder", sorted(BUILDERS))
def test_every_request_builder_rejects_an_unknown_exchange(builder) -> None:
    with pytest.raises(RequestValidationError, match="exch"):
        BUILDERS[builder]("XYZ")


@pytest.mark.parametrize("spelling", SPELLINGS, ids=repr)
def test_eod_symbol_uses_the_upper_case_code(spelling) -> None:
    assert eod_symbol(spelling, "SBIN-EQ") == "NSE:SBIN-EQ"


def test_eod_symbol_rejects_an_unknown_exchange() -> None:
    with pytest.raises(RequestValidationError, match="exch"):
        eod_symbol("XYZ", "SBIN-EQ")


def test_stream_keys_carry_the_upper_case_code() -> None:
    assert format_keys(["nse|22", ("bse", "500325"), "Nfo|35001", (Exchange.MCX, "1")]) == [
        "NSE|22",
        "BSE|500325",
        "NFO|35001",
        "MCX|1",
    ]
    assert format_keys("nse|22") == ["NSE|22"]
    assert format_keys(("nse", "22")) == ["NSE|22"]


def test_stream_keys_in_any_case_are_de_duplicated() -> None:
    assert format_keys(["nse|22", "NSE|22", ("Nse", "22")]) == ["NSE|22"]


def test_stream_keys_still_reject_an_unknown_exchange() -> None:
    with pytest.raises(RequestValidationError, match="unknown exchange"):
        format_keys(["xyz|22"])


@pytest.mark.parametrize("spelling", ["NSE", "nse", " Nse ", Exchange.NSE])
def test_script_master_url_uses_the_upper_case_code(spelling: Any) -> None:
    url = script_master_url(spelling, base_url="https://example.test/")
    assert url == "https://example.test/NSE_symbols.txt.zip"


@pytest.mark.parametrize("bad", ["xyz", "", 7])
def test_script_master_url_rejects_an_unknown_exchange(bad: Any) -> None:
    allowed = ", ".join(e.value for e in Exchange)
    with pytest.raises(ValueError, match=f"^unknown exchange {bad!r}; allowed: {allowed}$"):
        script_master_url(bad, base_url="https://example.test")
