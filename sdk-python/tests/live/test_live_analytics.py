"""Live, read-only: Greeks and option analytics (nfo / mcx). Opt-in; see README.md.

Counts are kept small (``count=5``) to respect the API rate limits: ``option_chain_snapshot``
makes one ``quote()`` per contract.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from decimal import Decimal

import pytest

from tests.live._helpers import Contract
from tradesmart import Buildup, TradeSmart

pytestmark = pytest.mark.live

STATUSES = {"ok", "near_expiry", "expired", "price_at_or_below_intrinsic", "no_price"}
VALUE_FIELDS = (
    "price",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "option_price",
    "underlying_price",
    "forward",
)


def test_option_greeks(
    shared_client: TradeSmart, option_contract: Contract, observe: Callable[..., object]
) -> None:
    result = shared_client.option_greeks(option_contract.exchange, option_contract.tradingsymbol)
    observe(
        segment=option_contract.exchange,
        tradingsymbol=option_contract.tradingsymbol,
        status=result.status,
        model=result.model,
        underlying=result.underlying_tradingsymbol,
        same_expiry_future=result.same_expiry_future,
        values={name: getattr(result, name) for name in VALUE_FIELDS},
        years_to_expiry=result.years_to_expiry,
    )
    assert result.status in STATUSES
    for name in VALUE_FIELDS:
        value: float | None = getattr(result, name)
        assert value is None or math.isfinite(value), f"{name} is not finite: {value!r}"
    assert math.isfinite(result.years_to_expiry)
    if option_contract.exchange == "MCX":
        # Options on futures: Black-76 on the future itself, no carry adjustment.
        assert result.model == "black76"
        if result.forward is not None and result.underlying_price is not None:
            assert result.forward == pytest.approx(result.underlying_price)


def test_option_chain_snapshot_pcr_and_max_pain(
    shared_client: TradeSmart, option_contract: Contract, observe: Callable[..., object]
) -> None:
    assert option_contract.strike is not None
    snapshot = shared_client.option_chain_snapshot(
        option_contract.exchange, option_contract.tradingsymbol, option_contract.strike, count=5
    )
    pcr_oi: Decimal | None = snapshot.pcr()
    pcr_volume: Decimal | None = snapshot.pcr("volume")
    pain = snapshot.max_pain()
    observe(
        segment=option_contract.exchange,
        tradingsymbol=option_contract.tradingsymbol,
        rows=len(snapshot.rows),
        missing=list(snapshot.missing),
        pcr_oi=pcr_oi,
        pcr_volume=pcr_volume,
        max_pain=pain.strike if pain else None,
    )
    assert snapshot.rows or snapshot.missing, "the chain listed no contract at all"
    keys = [(r.strike, r.option_type) for r in snapshot.rows]
    assert keys == sorted(keys, key=lambda k: (k[0], k[1] != "CE")), "rows not sorted"
    for ratio in (pcr_oi, pcr_volume):
        assert ratio is None or (ratio.is_finite() and ratio >= 0)  # empty OI/volume: None
    if pain is not None:
        assert pain.strike in {r.strike for r in snapshot.rows}


def test_buildup(
    shared_client: TradeSmart, future_contract: Contract, observe: Callable[..., object]
) -> None:
    reading = shared_client.buildup(future_contract.exchange, future_contract.token)
    observe(
        segment=future_contract.exchange,
        tradingsymbol=future_contract.tradingsymbol,
        state=reading.state,
        price_change=reading.price_change,
        oi_change=reading.oi_change,
        basis=reading.basis,
    )
    assert isinstance(reading.state, Buildup)
    for change in (reading.price_change, reading.oi_change):
        assert change is None or change.is_finite()
