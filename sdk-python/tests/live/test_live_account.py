"""Live, read-only: account data. An empty list is a pass. Opt-in; see README.md."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tradesmart import TradeSmart

pytestmark = [pytest.mark.live, pytest.mark.nse, pytest.mark.nfo, pytest.mark.mcx]


def _shape(items: list[Any]) -> dict[str, Any]:
    """What a list result looked like: its length, the model type and the raw keys seen."""
    keys = sorted({k for item in items for k in item.raw})
    return {
        "count": len(items),
        "model": type(items[0]).__name__ if items else None,
        "raw_keys": keys,
        "first_raw_keys": sorted(items[0].raw) if items else None,
    }


def test_margins_parse(live_client: TradeSmart, observe: Callable[..., object]) -> None:
    limits = live_client.margins()
    observe(stat=limits.stat, raw_keys=sorted(limits.raw), cash=limits.cash)
    assert limits.stat == "Ok"


def test_positions_parse(live_client: TradeSmart, observe: Callable[..., object]) -> None:
    positions = live_client.positions()
    observe(**_shape(positions))
    assert isinstance(positions, list)


def test_holdings_parse_on_empty_account(
    live_client: TradeSmart, observe: Callable[..., object]
) -> None:
    # F4: the shape of "Holdings" on an account without holdings (the SDK maps "no data" to []).
    holdings = live_client.holdings()
    observe(**_shape(holdings), product="CNC")
    assert isinstance(holdings, list)


def test_orders_parse(live_client: TradeSmart, observe: Callable[..., object]) -> None:
    orders = live_client.orders()
    observe(**_shape(orders))
    assert isinstance(orders, list)


def test_trades_parse(live_client: TradeSmart, observe: Callable[..., object]) -> None:
    trades = live_client.trades()
    observe(**_shape(trades))
    assert isinstance(trades, list)
