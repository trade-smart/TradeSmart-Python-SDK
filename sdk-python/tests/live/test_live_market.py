"""Live, read-only: market data per segment (nse / nfo / mcx). Opt-in; see README.md.

Every test takes the ``contract`` fixture (ids ``nse-eq``, ``nfo-fut``, ``nfo-opt``, ``mcx-fut``,
``mcx-opt``), found dynamically from the script master. Run one segment with ``-m mcx`` or
``-k mcx``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from tests.live._helpers import Contract, skip_if_unavailable_in_uat
from tradesmart import TradeSmart
from tradesmart.models.base import IST

pytestmark = pytest.mark.live


def test_search_instruments(
    shared_client: TradeSmart, contract: Contract, observe: Callable[..., object]
) -> None:
    matches = shared_client.search_instruments(contract.symbol)
    observe(
        segment=contract.exchange,
        text=contract.symbol,
        count=len(matches),
        first_raw_keys=sorted(matches[0].raw) if matches else None,
    )
    assert matches, f"search for {contract.symbol!r} found nothing"


def test_quote(
    shared_client: TradeSmart, contract: Contract, observe: Callable[..., object]
) -> None:
    quote = shared_client.quote(contract.exchange, contract.token)
    observe(
        segment=contract.exchange,
        tradingsymbol=contract.tradingsymbol,
        stat=quote.stat,
        last_price=quote.last_price,
        open_interest=quote.open_interest,
        feed_time=quote.feed_time,
        raw_keys=sorted(quote.raw),
    )
    assert quote.stat == "Ok"  # a closed market may leave last_price empty; that is fine


def test_security_info(
    shared_client: TradeSmart, contract: Contract, observe: Callable[..., object]
) -> None:
    info = shared_client.security_info(contract.exchange, contract.token)
    observe(
        segment=contract.exchange,
        tradingsymbol=contract.tradingsymbol,
        stat=info.stat,
        instname=info.instname,
        lot_size=info.lot_size,
        tick_size=info.tick_size,
        raw_keys=sorted(info.raw),
    )
    assert info.stat == "Ok"


def test_option_chain(
    shared_client: TradeSmart, option_contract: Contract, observe: Callable[..., object]
) -> None:
    assert option_contract.strike is not None
    chain = shared_client.option_chain(
        option_contract.exchange, option_contract.tradingsymbol, option_contract.strike, 5
    )
    observe(
        segment=option_contract.exchange,
        tradingsymbol=option_contract.tradingsymbol,
        strike=option_contract.strike,
        count=len(chain),
        option_types=sorted({c.option_type or "" for c in chain}),
        strikes=sorted({str(c.strike_price) for c in chain}),
    )
    assert chain, "option chain around an existing option came back empty"


def test_historical_data(
    shared_client: TradeSmart, contract: Contract, observe: Callable[..., object]
) -> None:
    end = datetime.now(IST)
    candles = shared_client.historical_data(
        contract.exchange, contract.token, end - timedelta(days=3), end, interval=5
    )
    observe(
        segment=contract.exchange,
        tradingsymbol=contract.tradingsymbol,
        count=len(candles),
        first_raw_keys=sorted(candles[0].raw) if candles else None,
        newest=candles[0].time if candles else None,
    )
    assert isinstance(candles, list)  # empty (closed market / no trades) is a pass


def test_daily_history(
    shared_client: TradeSmart, contract: Contract, observe: Callable[..., object]
) -> None:
    skip_if_unavailable_in_uat()
    today = datetime.now(IST).date()
    candles = shared_client.daily_history(
        contract.exchange, contract.tradingsymbol, today - timedelta(days=30), today
    )
    observe(
        segment=contract.exchange,
        tradingsymbol=contract.tradingsymbol,
        count=len(candles),
        first_raw_keys=sorted(candles[0].raw) if candles else None,
    )
    assert isinstance(candles, list)  # derivatives may have no daily history; that is a pass
