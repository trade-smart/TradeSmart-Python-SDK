"""Review fixes: ``option_greeks()`` validates rate/dividend_yield first and accepts Decimal."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import respx

from tests.integration.test_option_greeks_client import (
    AnyClient,
    _clock,  # noqa: F401  (autouse fixture)
    any_client,  # noqa: F401  (fixture)
    call,
    mock_quotes,
    patch_instruments,
)


@respx.mock
@pytest.mark.parametrize(
    ("kwargs", "name"),
    [
        ({"rate": float("nan")}, "rate"),
        ({"rate": float("inf")}, "rate"),
        ({"rate": "abc"}, "rate"),
        ({"dividend_yield": float("nan")}, "dividend_yield"),
        ({"dividend_yield": None}, "dividend_yield"),
    ],
)
async def test_option_greeks_bad_rate_sends_no_request(
    any_client: AnyClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
    name: str,
) -> None:
    patch_instruments(monkeypatch, any_client)
    route = mock_quotes({"1001": 24100, "2001": 120})

    with pytest.raises(ValueError, match=name):
        await call(any_client, "option_greeks", "NFO", "NIFTY26NOV26C24000", **kwargs)

    assert route.call_count == 0


@respx.mock
async def test_option_greeks_accepts_decimal_numbers(
    any_client: AnyClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_instruments(monkeypatch, any_client)
    mock_quotes({})
    given: dict[str, Any] = {"option_price": 120.5, "underlying_price": 24100}
    ref = await call(
        any_client,
        "option_greeks",
        "NFO",
        "NIFTY26NOV26C24000",
        rate=0.065,
        dividend_yield=0.01,
        **given,
    )
    g = await call(
        any_client,
        "option_greeks",
        "NFO",
        "NIFTY26NOV26C24000",
        rate=Decimal("0.065"),
        dividend_yield=Decimal("0.01"),
        option_price=Decimal("120.5"),
        underlying_price=Decimal("24100"),
    )
    assert g == ref
    assert isinstance(g.rate, float) and isinstance(g.dividend_yield, float)
