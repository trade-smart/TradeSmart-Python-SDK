"""Review fixes for `tradesmart.greeks`: stale-price status and Decimal/int inputs."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradesmart.greeks import (
    black76,
    black_scholes,
    greeks_from_price,
    implied_volatility,
)


def test_price_above_any_volatility_is_flagged_price_at_or_below_intrinsic() -> None:
    # Expiry day, 20 minutes left: a stale last price of 60 on a far-OTM call is above what
    # any volatility up to 500 % gives, so no IV exists and the same status applies.
    years = 20 / (365 * 24 * 60)
    result = greeks_from_price(60.0, 24000, 25200, years, 0.065, "CE")
    assert result.status == "price_at_or_below_intrinsic"
    assert result.iv is None
    assert result.price == 60.0


def test_decimal_and_int_inputs_equal_float_inputs() -> None:
    years = Decimal("0.1")
    assert black76(Decimal("100"), 100, years, Decimal("0.065"), Decimal("0.2"), "CE") == black76(
        100.0, 100.0, 0.1, 0.065, 0.2, "CE"
    )
    assert black_scholes(
        Decimal("100"), Decimal("100"), years, 0, Decimal("0.2"), "PE", Decimal("0.01")
    ) == black_scholes(100.0, 100.0, 0.1, 0.0, 0.2, "PE", 0.01)

    price = black76(100.0, 100.0, 0.1, 0.065, 0.2, "CE").price
    assert price is not None
    dec_price = Decimal(str(price))
    assert implied_volatility(
        dec_price, Decimal("100"), Decimal("100"), years, Decimal("0.065"), "CE"
    ) == implied_volatility(float(dec_price), 100.0, 100.0, 0.1, 0.065, "CE")
    assert greeks_from_price(
        dec_price,
        Decimal("100"),
        Decimal("100"),
        years,
        Decimal("0.065"),
        "CE",
        dividend_yield=Decimal("0"),
        tick_size=Decimal("0.05"),
    ) == greeks_from_price(float(dec_price), 100.0, 100.0, 0.1, 0.065, "CE", tick_size=0.05)


def test_carry_adjusted_forward_accepts_decimal_and_int() -> None:
    from tradesmart._option_contracts import carry_adjusted_forward

    expected = carry_adjusted_forward(24000.0, 0.065, 0.1, 0.05)
    got = carry_adjusted_forward(
        Decimal("24000"), Decimal("0.065"), Decimal("0.1"), Decimal("0.05")
    )
    assert got == expected
    assert carry_adjusted_forward(24000, 0, 1, 0) == 24000.0


@pytest.mark.parametrize("bad", [True, False])
def test_bool_is_rejected_as_a_number(bad: bool) -> None:
    with pytest.raises(ValueError, match="rate must be a finite number"):
        black76(100.0, 100.0, 0.1, bad, 0.2, "CE")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="strike must be a finite number"):
        greeks_from_price(1.0, 100.0, bad, 0.1, 0.065, "CE")  # type: ignore[arg-type]


def test_huge_int_is_a_value_error_not_overflow() -> None:
    with pytest.raises(ValueError, match="strike must be a finite number"):
        greeks_from_price(1.0, 100.0, 10**400, 0.1, 0.065, "CE")
    with pytest.raises(ValueError, match="rate must be a finite number"):
        black76(100.0, 100.0, 0.1, 10**400, 0.2, "CE")
