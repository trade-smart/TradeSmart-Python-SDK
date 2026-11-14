"""Price helpers."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from tradesmart.models.base import PriceInput, _parse_price


def _decimal(value: PriceInput, name: str) -> Decimal:
    try:
        result = _parse_price(value)
    except ValueError:
        result = None
    if result is None:
        raise ValueError(f"{name} must be a finite number")
    return result


def round_to_tick(price: PriceInput, tick_size: PriceInput) -> Decimal:
    """Round ``price`` to the nearest multiple of ``tick_size`` (half-way rounds up).

    Accepts ``Decimal``, ``int``, ``float`` or ``str``. The result is quantised to the
    tick size's decimal places, e.g. ``round_to_tick(1584.45 * 1.001, "0.05")`` is
    ``Decimal("1586.05")``. Raises ``ValueError`` if ``tick_size <= 0`` or an input is
    not a finite number. Floats are converted via ``str`` to avoid binary noise.
    """
    tick = _decimal(tick_size, "tick_size")
    if tick <= 0:
        raise ValueError("tick_size must be greater than 0")
    value = _decimal(price, "price")
    ticks = (value / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    exponent = tick.normalize().as_tuple().exponent  # an int: tick is finite
    places = exponent if isinstance(exponent, int) and exponent < 0 else 0
    return (ticks * tick).quantize(Decimal(1).scaleb(places))
