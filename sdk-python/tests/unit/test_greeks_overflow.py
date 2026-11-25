"""An absurd but finite rate or dividend yield is a ``ValueError``, never ``OverflowError``.

Spec 10.3 ("Rate and yield range"): the discount factors ``exp(-rate * years)`` and
``exp(-dividend_yield * years)`` and the carry factor ``exp(-(rate - dividend_yield) * (Tf - To))``
must fit a float.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from tradesmart import _option_contracts as oc
from tradesmart.greeks import black76, black_scholes, greeks_from_price, implied_volatility
from tradesmart.models.base import IST

_RATE = "rate is out of range: exp(-rate * years) overflows a float"
_YIELD = "dividend_yield is out of range: exp(-dividend_yield * years) overflows a float"
_CARRY = (
    "rate - dividend_yield is out of range: "
    "exp(-(rate - dividend_yield) * (future_years - option_years)) overflows a float"
)

_CASES: list[tuple[str, Callable[[], Any], str]] = [
    ("black76_rate", lambda: black76(100, 100, 0.25, -1e6, 0.2, "CE"), _RATE),
    ("black76_long_years", lambda: black76(100, 100, 1e6, -0.01, 0.2, "PE"), _RATE),
    ("bsm_rate", lambda: black_scholes(100, 100, 0.25, -1e6, 0.2, "CE"), _RATE),
    ("bsm_yield", lambda: black_scholes(100, 100, 0.25, 0.05, 0.2, "PE", -1e6), _YIELD),
    ("iv_black76_rate", lambda: implied_volatility(5, 100, 100, 0.25, -1e6, "CE"), _RATE),
    (
        "iv_bsm_yield",
        lambda: implied_volatility(5, 100, 100, 0.25, 0.05, "CE", model="bsm", dividend_yield=-1e6),
        _YIELD,
    ),
    ("gfp_black76_rate", lambda: greeks_from_price(5, 100, 100, 0.25, -1e6, "CE"), _RATE),
    (
        "gfp_bsm_yield",
        lambda: greeks_from_price(5, 100, 100, 0.25, 0.05, "CE", model="bsm", dividend_yield=-1e6),
        _YIELD,
    ),
    ("carry_rate", lambda: oc.carry_adjusted_forward(24200, 1e6, 0.1, 0.2), _CARRY),
    ("carry_negative_rate", lambda: oc.carry_adjusted_forward(24200, -1e6, 0.2, 0.1), _CARRY),
    (
        "carry_yield",
        lambda: oc.carry_adjusted_forward(24200, 0.065, 0.2, 0.1, dividend_yield=1e6),
        _CARRY,
    ),
]


@pytest.mark.parametrize(("fn", "message"), [c[1:] for c in _CASES], ids=[c[0] for c in _CASES])
def test_overflowing_factor_is_a_value_error(fn: Callable[[], Any], message: str) -> None:
    with pytest.raises(ValueError, match="^" + re.escape(message)):
        fn()


def test_message_shows_the_values() -> None:
    with pytest.raises(ValueError) as info:
        black_scholes(100, 100, 0.25, 0.05, 0.2, "CE", dividend_yield=-1e6)
    assert str(info.value) == (
        "dividend_yield is out of range: exp(-dividend_yield * years) overflows a float "
        "(dividend_yield=-1000000.0, years=0.25)"
    )
    with pytest.raises(ValueError) as info:
        oc.carry_adjusted_forward(24200, -1e6, 0.2, 0.1)
    assert str(info.value) == (
        "rate - dividend_yield is out of range: "
        "exp(-(rate - dividend_yield) * (future_years - option_years)) overflows a float "
        "(rate=-1000000.0, dividend_yield=0.0, future_years=0.2, option_years=0.1)"
    )


def test_underflowing_factor_is_still_a_result() -> None:
    """Only overflow is refused: a factor that underflows to 0 still gives a (finite) result."""
    assert black76(100, 100, 0.25, 1e6, 0.2, "CE").price == 0.0
    assert oc.carry_adjusted_forward(24200, 1e6, 0.2, 0.1) == 0.0


def test_expired_option_never_computes_the_factor() -> None:
    assert black76(100, 90, 0.0, -1e6, 0.2, "CE").status == "expired"
    assert greeks_from_price(5, 100, 90, -1.0, -1e6, "CE").status == "expired"


def test_option_greeks_result_raises_value_error_for_an_overflowing_carry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oc, "_now", lambda: datetime(2026, 11, 12, 10, 0, tzinfo=IST))
    option = oc.OptionContractInfo(
        exchange="NFO",
        token="2",
        tradingsymbol="OPT",
        symbol="SYM",
        expiry=date(2026, 11, 26),
        strike=Decimal("24000"),
        option_type="CE",
        tick_size=None,
        lot_size=1,
    )
    future = oc.UnderlyingFuture("FUT", "1", date(2026, 12, 29), False)
    plan = oc.GreeksPlan("NFO", option, future, "black76")

    with pytest.raises(ValueError, match=r"^rate - dividend_yield is out of range"):
        oc.option_greeks_result(
            plan,
            option_price=100,
            underlying_price=24200,
            rate=-1e6,
            dividend_yield=0.0,
            expiry_time=None,
        )
