"""Live, read-only, UAT only: the test account's holdings, positions and funds, and the option
reads around the at-the-money strike (NFO NIFTY, BFO SENSEX). Opt-in; see README.md.

Every test skips (before any API call) unless ``TRADESMART_ENV=uat``: the UAT test account is
known to hold securities (its positions vary from day to day); a production account need not.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from tests._environment import is_uat
from tests.live._helpers import ContractResolver, nearest_future, options_by_distance
from tradesmart import TradeSmart

pytestmark = pytest.mark.live

PARSE_WARNING = "Could not parse field"
# Cash / collateral fields of "Limits" (documented, plus the undocumented aux_* ones).
CASH_FIELDS = (
    "cash",
    "payin",
    "daycash",
    "collateral",
    "cash_coll",
    "brkcollamt",
    "unclearedcash",
    "aux_daycash",
    "aux_brkcollamt",
    "aux_unclearedcash",
)
HOLDING_QUANTITIES = ("holdqty", "dpqty", "npoadqty", "brkcolqty", "unplgdqty", "benqty", "btstqty")
GREEKS_STATUSES = {"ok", "near_expiry", "expired", "price_at_or_below_intrinsic", "no_price"}
STRIKE_TRIES = 5  # strikes quoted (nearest first) to find one with a last price on UAT


@pytest.fixture
def uat_client(request: pytest.FixtureRequest) -> TradeSmart:
    """The shared client, only when ``TRADESMART_ENV=uat`` (skips before building it)."""
    if not is_uat(os.environ):
        pytest.skip("UAT only: set TRADESMART_ENV=uat")
    client: TradeSmart = request.getfixturevalue("shared_client")
    return client


def _parse_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if PARSE_WARNING in r.getMessage()]


def test_uat_holdings_are_parsed(
    uat_client: TradeSmart, observe: Callable[..., object], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="tradesmart"):
        holdings = uat_client.holdings()
    rows = [
        {
            "symbols": [i.tradingsymbol for i in h.instruments or []],
            "quantities": {name: getattr(h, name) for name in HOLDING_QUANTITIES},
            "upldprc": h.upldprc,
            "raw_keys": sorted(h.raw),
        }
        for h in holdings
    ]
    observe(count=len(holdings), holdings=rows, parse_warnings=_parse_warnings(caplog))
    assert not _parse_warnings(caplog)
    assert holdings, "the UAT test account holds securities, but holdings() is empty"
    for row in rows:
        assert row["symbols"] and all(row["symbols"]), f"a holding without a symbol: {row}"
        assert any(q is not None for q in row["quantities"].values()), f"no quantity: {row}"
        assert row["upldprc"] is not None, f"no average price (upldprc): {row}"
    assert any((q or 0) > 0 for row in rows for q in row["quantities"].values()), (
        "no holding has a quantity above 0"
    )


def test_uat_positions_are_parsed(
    uat_client: TradeSmart, observe: Callable[..., object], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="tradesmart"):
        positions = uat_client.positions()
    rows: list[dict[str, Any]] = [
        {
            "exchange": p.exchange,
            "tradingsymbol": p.tradingsymbol,
            "product": p.product,
            "net_quantity": p.net_quantity,
            "netavgprc": p.netavgprc,
            "realized_pnl": p.realized_pnl,
            "last_price": p.last_price,
            "raw_keys": sorted(p.raw),
        }
        for p in positions
    ]
    observe(count=len(positions), positions=rows, parse_warnings=_parse_warnings(caplog))
    assert not _parse_warnings(caplog)
    if not positions:
        # The UAT account's positions come and go (the 2026-11-11 NRML future was gone on
        # 2026-11-12): an empty book parsed without warnings is all there is to check.
        pytest.skip("no position on the UAT test account today (empty list parsed cleanly)")
    for row in rows:
        assert row["tradingsymbol"] and row["net_quantity"] is not None, f"unparsed: {row}"
        assert row["realized_pnl"] is not None, f"no realised P&L (rpnl): {row}"
    open_rows = [row for row in rows if row["net_quantity"]]
    for row in open_rows:
        assert row["netavgprc"] is not None, f"an open position without netavgprc: {row}"


def test_uat_margins_are_consistent(
    uat_client: TradeSmart, observe: Callable[..., object], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="tradesmart"):
        limits = uat_client.margins()
    cash = {name: getattr(limits, name) for name in CASH_FIELDS}
    observe(stat=limits.stat, cash=cash, raw_keys=sorted(limits.raw))
    assert limits.stat == "Ok"
    assert not _parse_warnings(caplog)
    present = {name: value for name, value in cash.items() if value is not None}
    assert present, "margins() has none of the cash fields"
    negative = {name: value for name, value in present.items() if value < 0}
    assert not negative, f"negative cash fields: {negative}"


# (exchange, name, future Symbol, option Symbol): the script-master Symbol of the index future
# and of its options. BFO (2026-11-12): SENSEX futures are BSXFUT, its options BSXOPT (SX50* is
# SENSEX 50); NFO NIFTY uses NIFTY for both.
OPTION_UNDERLYINGS = [
    ("NFO", "NIFTY", "NIFTY", "NIFTY"),
    ("BFO", "SENSEX", "BSXFUT", "BSXOPT"),
]


@pytest.mark.parametrize(
    ("exchange", "name", "future_symbol", "option_symbol"),
    OPTION_UNDERLYINGS,
    ids=["nfo", "bfo"],
)
def test_uat_option_reads_around_the_money(
    uat_client: TradeSmart,
    resolver: ContractResolver,
    exchange: str,
    name: str,
    future_symbol: str,
    option_symbol: str,
    observe: Callable[..., object],
) -> None:
    """The nearest-expiry CE closest to the future's price that has a last price on UAT."""
    try:
        rows = resolver.rows(exchange)
    except Exception as exc:
        pytest.skip(f"the {exchange} script master is unavailable ({type(exc).__name__}: {exc})")
    future = nearest_future(rows, exchange, future_symbol)
    if future is None:
        pytest.skip(f"no {name} future in the {exchange} script master")
    reference = uat_client.quote(exchange, future.token).last_price
    if reference is None or reference <= 0:
        pytest.skip(f"no last price for {future.tradingsymbol} on UAT")
    candidates = options_by_distance(rows, exchange, option_symbol, None, reference)
    if not candidates:
        pytest.skip(f"no {name} CE of a future expiry in the {exchange} script master")
    tried: list[tuple[str, Decimal | None]] = []
    option = None
    quote = None
    for candidate in candidates[:STRIKE_TRIES]:
        quote = uat_client.quote(exchange, candidate.token)
        tried.append((candidate.tradingsymbol, quote.last_price))
        if quote.last_price is not None and quote.last_price > 0:
            option = candidate
            break
    if option is None or quote is None:
        observe(segment=exchange, future=future.tradingsymbol, reference=reference, tried=tried)
        pytest.skip(f"no {name} CE near {reference} with a last price on UAT (tried {tried})")
    assert option.strike is not None

    info = uat_client.security_info(exchange, option.token)
    chain = uat_client.option_chain(exchange, option.tradingsymbol, option.strike, 5)
    # On BFO the SDK takes the underlying future from BSXFUT for a BSXOPT option (F28).
    greeks = uat_client.option_greeks(exchange, option.tradingsymbol)
    observe(
        segment=exchange,
        future=future.tradingsymbol,
        reference=reference,
        option=option.tradingsymbol,
        strike=option.strike,
        tried=tried,
        quote={"stat": quote.stat, "lp": quote.last_price, "ls": quote.lot_size},
        security_info={"stat": info.stat, "tsym": info.tradingsymbol, "strprc": info.strprc},
        chain=[(c.tradingsymbol, c.option_type, c.strike_price) for c in chain],
        greeks={
            "status": greeks.status,
            "iv": greeks.iv,
            "delta": greeks.delta,
            "underlying": greeks.underlying_tradingsymbol,
        },
    )
    assert quote.stat == "Ok" and quote.lot_size == option.lot_size
    assert info.stat == "Ok" and info.tradingsymbol == option.tradingsymbol
    assert any(c.strike_price == option.strike for c in chain), "the chain misses the strike"
    assert {c.option_type for c in chain} >= {"CE", "PE"}
    assert greeks.underlying_tradingsymbol is not None
    assert greeks.status in GREEKS_STATUSES
    if greeks.status == "ok":
        assert greeks.iv is not None and greeks.delta is not None
        assert 0 < greeks.delta < 1
