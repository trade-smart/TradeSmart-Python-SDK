"""Read-only smoke tests against the real API. Opt-in; see README.md."""

from __future__ import annotations

import pytest

from tradesmart import Exchange, TradeSmart

pytestmark = pytest.mark.live


@pytest.mark.live
def test_user_details(live_client: TradeSmart) -> None:
    details = live_client.profile()
    assert details.raw.get("stat") == "Ok"


@pytest.mark.live
def test_limits(live_client: TradeSmart) -> None:
    limits = live_client.margins()
    assert limits.stat == "Ok"


@pytest.mark.live
def test_search(live_client: TradeSmart) -> None:
    assert live_client.search_instruments("INFY")


@pytest.mark.live
def test_quote_from_search(live_client: TradeSmart) -> None:
    matches = live_client.search_instruments("INFY")
    match = next(
        (
            m
            for m in matches
            if m.exchange == "NSE" and (m.tradingsymbol or "").endswith("-EQ") and m.token
        ),
        None,
    )
    if match is None or match.token is None:
        pytest.skip("no NSE -EQ match for INFY in search results")
    quote = live_client.quote(Exchange.NSE, match.token)
    assert quote.stat == "Ok"


@pytest.mark.live
def test_order_book_is_list(live_client: TradeSmart) -> None:
    assert isinstance(live_client.orders(), list)
