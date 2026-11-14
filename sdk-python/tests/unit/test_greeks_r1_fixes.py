"""Round-1 review fixes: carry with a yield, price checks in option_greeks, no raw maths errors."""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from tradesmart import _option_contracts as oc
from tradesmart.greeks import black76, black_scholes, greeks_from_price, implied_volatility
from tradesmart.models.base import IST

# --- G1: the carry uses (rate - dividend_yield) -------------------------------------------


def test_carry_uses_rate_minus_dividend_yield() -> None:
    forward = oc.carry_adjusted_forward(24200.0, 0.065, 0.2, 0.05, dividend_yield=0.013)
    assert forward == 24200.0 * math.exp(-(0.065 - 0.013) * (0.2 - 0.05))


def test_carry_default_dividend_yield_keeps_the_rate_only_formula() -> None:
    assert oc.carry_adjusted_forward(24200.0, 0.065, 0.2, 0.05) == 24200.0 * math.exp(
        -0.065 * (0.2 - 0.05)
    )
    assert oc.carry_adjusted_forward(
        24200.0, 0.065, 0.2, 0.05, dividend_yield=0.0
    ) == oc.carry_adjusted_forward(24200.0, 0.065, 0.2, 0.05)


def test_carry_with_yield_equal_to_rate_is_the_future_price() -> None:
    assert oc.carry_adjusted_forward(88.5, 0.065, 0.3, 0.01, dividend_yield=0.065) == 88.5


def test_carry_dividend_yield_accepts_decimal_and_rejects_bool() -> None:
    assert oc.carry_adjusted_forward(
        100, 0.065, 1, 0, dividend_yield=Decimal("0.045")
    ) == oc.carry_adjusted_forward(100.0, 0.065, 1.0, 0.0, dividend_yield=0.045)
    with pytest.raises(ValueError, match="dividend_yield"):
        oc.carry_adjusted_forward(100.0, 0.065, 1.0, 0.0, dividend_yield=True)  # type: ignore[arg-type]


def _plan(exchange: str, option_expiry: date, future_expiry: date) -> oc.GreeksPlan:
    option = oc.OptionContractInfo(
        exchange=exchange,
        tradingsymbol="OPT",
        token="2",
        symbol="SYM",
        expiry=option_expiry,
        option_type="CE",
        strike=Decimal("100"),
        tick_size=Decimal("0.0025"),
        lot_size=1,
    )
    future = oc.UnderlyingFuture("FUT", "1", future_expiry, future_expiry == option_expiry)
    return oc.GreeksPlan(exchange, option, future, "black76")


def test_option_forward_on_a_currency_future_uses_the_foreign_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 11, 12, 10, 0, tzinfo=IST)
    monkeypatch.setattr(oc, "_now", lambda: now)
    plan = _plan("CDS", date(2026, 11, 26), date(2026, 12, 29))

    g = oc.option_greeks_result(
        plan,
        option_price=0.5,
        underlying_price=88.5,
        rate=0.065,
        dividend_yield=0.045,
        expiry_time=None,
    )

    to = oc.years_between(now, datetime(2026, 11, 26, 12, 30, tzinfo=IST))
    tf = oc.years_between(now, datetime(2026, 12, 29, 12, 30, tzinfo=IST))
    assert g.forward == 88.5 * math.exp(-(0.065 - 0.045) * (tf - to))
    assert g.dividend_yield == 0.045


def test_option_forward_on_mcx_ignores_the_dividend_yield(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "_now", lambda: datetime(2026, 11, 12, 10, 0, tzinfo=IST))
    plan = _plan("MCX", date(2026, 11, 19), date(2026, 12, 19))

    g = oc.option_greeks_result(
        plan,
        option_price=100,
        underlying_price=6500,
        rate=0.065,
        dividend_yield=0.03,
        expiry_time=None,
    )

    assert g.forward == 6500.0


# --- Minor 1: option_greeks prices follow the numeric rules of greeks_from_price -----------


@pytest.mark.parametrize("name", ["option_price", "underlying_price", "spot_price"])
@pytest.mark.parametrize("bad", [True, 10**400, "12.5", object()])
def test_check_prices_rejects_non_numbers_naming_the_parameter(name: str, bad: Any) -> None:
    kwargs: dict[str, Any] = {"option_price": None, "underlying_price": None, "spot_price": None}
    kwargs[name] = bad
    with pytest.raises(ValueError, match=name):
        oc.check_prices(**kwargs)


def test_check_prices_accepts_numbers_none_and_non_finite() -> None:
    oc.check_prices(option_price=Decimal("12.5"), underlying_price=24100, spot_price=None)
    oc.check_prices(option_price=float("nan"), underlying_price=float("inf"), spot_price=1.5)


def test_option_greeks_result_rejects_a_huge_int_price(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "_now", lambda: datetime(2026, 11, 12, 10, 0, tzinfo=IST))
    plan = _plan("NFO", date(2026, 11, 26), date(2026, 11, 26))
    with pytest.raises(ValueError, match="option_price"):
        oc.option_greeks_result(
            plan,
            option_price=10**400,
            underlying_price=100,
            rate=0.065,
            dividend_yield=0.0,
            expiry_time=None,
        )


# --- Minor 4: underflowing inputs give None, never a raw ZeroDivisionError -----------------


def test_black76_with_underflowing_underlying_and_volatility() -> None:
    g = black76(1e-300, 1, 1, 0.065, 1e-300, "CE")
    assert g.status == "ok"
    assert g.gamma is None or math.isfinite(g.gamma)


def test_models_with_the_smallest_volatility() -> None:
    for result in (
        black76(100, 100, 0.01, 0.065, 5e-324, "CE"),
        black76(100, 90, 0.01, 0.065, 5e-324, "PE"),
        black_scholes(100, 110, 0.01, 0.065, 5e-324, "CE", 0.01),
    ):
        assert result.status == "ok"
        for value in (result.price, result.delta, result.gamma, result.theta, result.vega):
            assert value is None or math.isfinite(value)


def test_greeks_from_price_with_a_denormal_underlying() -> None:
    g = greeks_from_price(1e-321, 1e-320, 1e-320, 0.1, 0.065, "CE")
    assert g.status in ("ok", "near_expiry", "price_at_or_below_intrinsic")
    iv = implied_volatility(1e-321, 1e-320, 1e-320, 0.1, 0.065, "CE")
    assert iv is None or math.isfinite(iv)
