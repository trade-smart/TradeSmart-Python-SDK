"""Dry run of the UAT matrix family M10 (order margin, read-only) on the offline broker.

The real family code (``cases_margin`` and ``tests/live/test_live_uat_margin.check_margin``)
runs end to end against a fake ``GetOrderMargin`` reply: every M1 shape, cover and bracket
orders with ``blprc``, market orders (recorded) and invalid products (``Not_Ok``, recorded).
Nothing is placed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tests.integration.uat_family_rig import ACCOUNT, add_instruments, contracts_of
from tests.integration.uat_like import UatLikeMarket
from tests.live.test_live_uat_margin import MarginRun, check_margin
from tests.live_orders.uat_matrix import batch, cases_margin
from tests.live_orders.uat_matrix.expect import Profile
from tradesmart import TradeSmart
from tradesmart.enums import Exchange, Product
from tradesmart.testing import OfflineBroker, drills

OK_REPLY = {
    "stat": "Ok",
    "remarks": "Order Success",
    "cash": "0.00",
    "marginused": "120.50",
    "ordermargin": "120.50",
    "marginusedprev": "0.00",
}
INVALID = "Invalid Product"


def _invalid(kwargs: dict[str, Any]) -> bool:
    exchange = Exchange(str(kwargs["exchange"]))
    product = Product(str(kwargs["product"]))
    return exchange in (Exchange.NFO, Exchange.BFO) and product in (Product.CNC, Product.MTF)


def expected(case: cases_margin.MarginCase) -> str:
    """With no profile: documented LMT / SL-LMT shapes pass; market types, a delivery SELL on
    cash, cover / bracket orders (the profile does not enable them) and invalid products are
    recorded."""
    recorded = (
        case.invalid
        or case.prctyp in ("MKT", "SL-MKT")
        or case.product in ("CO", "BO")
        or (case.segment == "nse" and case.product in ("CNC", "MTF") and case.side == "SELL")
    )
    return batch.RECORD if recorded else batch.PASS


@pytest.fixture
def rig() -> Iterator[tuple[OfflineBroker, TradeSmart, MarginRun]]:
    with OfflineBroker(ACCOUNT) as broker:
        market = UatLikeMarket(broker)
        add_instruments(market)
        client = broker.client()
        broker.set_reply("GetOrderMargin", OK_REPLY)
        real = client.order_margin

        def margin(**kwargs: Any) -> Any:
            if _invalid(kwargs):
                broker.fail_next("GetOrderMargin", drills.reject(INVALID))
            return real(**kwargs)

        client.order_margin = margin  # type: ignore[method-assign]
        run = MarginRun(client, contracts_of(market), Profile(None, None, None))
        try:
            yield broker, client, run
        finally:
            client.close()


def test_every_margin_case_gets_its_expected_verdict(
    rig: tuple[OfflineBroker, TradeSmart, MarginRun],
) -> None:
    broker, _, run = rig
    results = {c.id: check_margin(run, c) for c in cases_margin.MARGIN_CASES}
    for case in cases_margin.MARGIN_CASES:
        verdict = results[case.id].verdict
        assert verdict == expected(case), (case.id, verdict, results[case.id].reason)
    assert sum(1 for r in results.values() if r.verdict == batch.PASS) > 150
    invalid = [c for c in cases_margin.MARGIN_CASES if c.invalid]
    for case in invalid:
        assert INVALID in results[case.id].reason
        assert results[case.id].record["error"]["emsg"] == INVALID
    endpoints = {r.endpoint for r in broker.requests}
    assert endpoints == {"GetOrderMargin", "GetQuotes"}  # read-only: nothing placed
    roles = {c.role for c in cases_margin.MARGIN_CASES}
    assert sum(1 for r in broker.requests if r.endpoint == "GetQuotes") == len(roles)


def test_the_margin_request_carries_the_shape(
    rig: tuple[OfflineBroker, TradeSmart, MarginRun],
) -> None:
    broker, _, run = rig
    case = next(c for c in cases_margin.MARGIN_CASES if c.id == "margin-nse-b-co-lmt-buy")
    result = check_margin(run, case)
    [request] = [r.payload for r in broker.requests if r.endpoint == "GetOrderMargin"]
    assert (request["exch"], request["tsym"], request["prd"], request["trantype"]) == (
        "NSE",
        "ITC-EQ",
        "H",
        "B",
    )
    assert float(request["prc"]) > 0 and float(request["blprc"]) > 0
    assert "ret" not in request
    assert result.record["kwargs"]["product"] == "CO"
    assert result.record["margin"]["ordermargin"] == "120.50"


def test_a_role_without_a_contract_or_a_price_skips(
    rig: tuple[OfflineBroker, TradeSmart, MarginRun],
) -> None:
    broker, _, run = rig
    case = next(c for c in cases_margin.MARGIN_CASES if c.role == "nse-t")
    del run.contracts.contracts["nse-t"]
    result = check_margin(run, case)
    assert result.verdict == batch.SKIP and "nse-t" in result.reason
    other = next(c for c in cases_margin.MARGIN_CASES if c.role == "nse-b")
    broker.set_quote("NSE", "1660", lp="0")
    assert check_margin(run, other).verdict == batch.SKIP
    assert not [r for r in broker.requests if r.endpoint == "GetOrderMargin"]


def test_a_failed_margin_read_fails_an_ok_case(
    rig: tuple[OfflineBroker, TradeSmart, MarginRun],
) -> None:
    broker, _, run = rig
    case = next(c for c in cases_margin.MARGIN_CASES if c.id == "margin-nse-b-mis-lmt-buy")
    broker.fail_next("GetOrderMargin", drills.reject("Margin not available"))
    result = check_margin(run, case)
    assert result.verdict == batch.FAIL and "Margin not available" in result.reason
