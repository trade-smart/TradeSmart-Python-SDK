"""SENSEX (BFO) options end to end on the offline broker, through the real sync and async clients.

The BFO script master (production, 2026-11-12, F28) names the strike column ``Strike`` and files
SENSEX futures under ``Symbol`` ``BSXFUT`` and its options under ``BSXOPT``.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from tradesmart import AsyncTradeSmart, TradeSmart
from tradesmart.analytics import BuildupReading, OptionChainSnapshot
from tradesmart.greeks import OptionGreeks
from tradesmart.models.base import IST
from tradesmart.testing._rest import AsyncOfflineTransport, OfflineTransport, offline_config
from tradesmart.testing._state import BrokerState

WS_URL = "ws://127.0.0.1:9/NorenWSAPI/"
FUTURE = "SENSEX26NOVFUT"
OPTION = "SENSEX26N1985200CE"
# Relative expiries keep the option live whenever the suite runs: a weekly option before the
# monthly future, as on 2026-11-12 (19 and 26 November).
OPTION_EXPIRY = datetime.now(IST).date() + timedelta(days=7)
FUTURE_EXPIRY = OPTION_EXPIRY + timedelta(days=7)


def _sensex(state: BrokerState) -> None:
    state.add_instrument(
        "BFO",
        "700",
        FUTURE,
        lot_size=20,
        instrument="FUTIDX",
        expiry=FUTURE_EXPIRY,
        underlying="BSXFUT",
    )
    # SENSEX 50 futures share the exchange and expiry but are another family.
    state.add_instrument(
        "BFO",
        "900",
        "SENSEX5026NOVFUT",
        lot_size=25,
        instrument="FUTIDX",
        expiry=FUTURE_EXPIRY,
        underlying="SX50FUT",
    )
    for token, strike, kind in (("801", 85200, "CE"), ("802", 85200, "PE")):
        state.add_instrument(
            "BFO",
            token,
            f"SENSEX26N19{strike}{kind}",
            lot_size=20,
            instrument="OPTIDX",
            expiry=OPTION_EXPIRY,
            strike=strike,
            option_type=kind,
            underlying="BSXOPT",
        )
    state.set_quote("BFO", "700", last_price="85310.00", close="85000.00")
    state.set_quote("BFO", "900", last_price="27000.00")
    state.set_quote("BFO", "801", last_price="410.00", close="400.00", oi="1200")
    state.set_quote("BFO", "802", last_price="300.00", close="310.00", oi="900")


@pytest.fixture(params=["sync", "async"])
async def call(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Any]:
    """Calls a method on a real client of the parametrised kind, wired to a SENSEX broker."""
    monkeypatch.setenv("HOME", str(tmp_path))  # the script-master cache lives under HOME
    state = BrokerState()
    _sensex(state)
    kwargs: dict[str, Any] = {
        "access_token": "offline-token-test",
        "app_key": "offline-app-key",
        "secret_key": "offline-secret-key",
        "config": offline_config(WS_URL),
    }
    http: Any
    client: TradeSmart | AsyncTradeSmart
    if request.param == "sync":
        http = httpx.Client(transport=OfflineTransport(state))
        client = TradeSmart("OFFLINE01", http_client=http, **kwargs)
    else:
        http = httpx.AsyncClient(transport=AsyncOfflineTransport(state))
        client = AsyncTradeSmart("OFFLINE01", http_client=http, **kwargs)

    async def _call(method: str, *args: Any, **kw: Any) -> Any:
        result = getattr(client, method)(*args, **kw)
        return await result if inspect.isawaitable(result) else result

    yield _call
    closed = http.close() if isinstance(http, httpx.Client) else http.aclose()
    if inspect.isawaitable(closed):
        await closed


async def test_option_greeks_on_a_sensex_option(call: Any) -> None:
    greeks = await call("option_greeks", "BFO", OPTION)
    assert isinstance(greeks, OptionGreeks)
    assert greeks.underlying_tradingsymbol == FUTURE
    assert greeks.same_expiry_future is False
    assert greeks.underlying_price == pytest.approx(85310.0)
    assert greeks.status == "ok"


async def test_option_chain_snapshot_and_buildup_on_sensex(call: Any) -> None:
    snapshot = await call("option_chain_snapshot", "BFO", OPTION, Decimal("85200"), 1)
    assert isinstance(snapshot, OptionChainSnapshot)
    assert snapshot.tradingsymbol == OPTION
    assert {row.tradingsymbol for row in snapshot.rows} == {OPTION, "SENSEX26N1985200PE"}
    reading = await call("buildup", "BFO", "801")
    assert isinstance(reading, BuildupReading)
    assert reading.price_change == Decimal("10.00")
