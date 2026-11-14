from decimal import Decimal

import pytest

import tradesmart
from tradesmart import round_to_tick


def test_exported() -> None:
    assert "round_to_tick" in tradesmart.__all__


def test_float_times_factor_rounds_to_nearest_tick() -> None:
    assert round_to_tick(1584.45 * 1.001, "0.05") == Decimal("1586.05")


@pytest.mark.parametrize(
    "price,tick,expected",
    [
        (Decimal("100.02"), Decimal("0.05"), "100.00"),
        ("100.03", "0.05", "100.05"),
        ("100.025", "0.05", "100.05"),  # half-way rounds up
        (100, "0.05", "100.00"),
        (1234, 5, "1235"),
        (1232.4, 5, "1230"),
        ("99.999", "0.01", "100.00"),
        (Decimal("1584.45"), Decimal("0.05"), "1584.45"),
        ("10.3", "0.25", "10.25"),
    ],
)
def test_rounds_to_nearest_tick_and_quantises(price, tick, expected) -> None:
    result = round_to_tick(price, tick)
    assert isinstance(result, Decimal)
    assert str(result) == expected


@pytest.mark.parametrize("tick", [0, "0", -0.05, "-1"])
def test_non_positive_tick_is_value_error(tick) -> None:
    with pytest.raises(ValueError, match="tick_size"):
        round_to_tick("100", tick)


@pytest.mark.parametrize("price,tick", [("abc", "0.05"), ("100", "x"), ("nan", "0.05"), (True, 1)])
def test_invalid_input_is_value_error(price, tick) -> None:
    with pytest.raises(ValueError):
        round_to_tick(price, tick)
