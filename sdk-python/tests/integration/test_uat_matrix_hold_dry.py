"""Dry run of the UAT matrix family M12 (holdings SELL) on the UAT-like offline broker.

Drives ``uat_matrix/cases_hold.py`` end to end (the code the live ``test_m12_hold.py`` runs):
holdings read first, a CNC SELL of one held share far above the market (rests, cancelled), and
the owner-approved (U10) CNC SELL of one share at the last price, recorded, never an xfail.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.integration.uat_fill_rig import LATE, TODAY, Rig, make_rig
from tests.live_orders.uat_matrix import batch, cases, cases_hold, family, family_tools
from tradesmart.errors import TradeSmartError

FAR = "hold-nse-hold-cnc-lmt-day-sell"
FILL = "hold-nse-hold-cnc-lmt-day-sell-fill"
ITC = {"exch": "NSE", "tsym": "ITC-EQ", "token": "1660", "ls": "1", "ti": "0.05", "lp": "400.00"}


def _holding(qty: str = "5", *listings: dict[str, Any]) -> dict[str, Any]:
    return {"exch_tsym": list(listings) or [ITC], "holdqty": qty, "dpqty": "0", "usedqty": "0"}


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    for rig in make_rig(tmp_path):
        rig.broker.set_holdings([_holding()])
        yield rig


def test_the_family_lists_the_m12_cases_and_its_smoke_tier() -> None:
    family = cases_hold.family_cases()
    assert [c.id for c in family] == [FAR, FILL]
    assert [c.tier for c in family] == [cases.SMOKE, cases.FULL]
    assert all((c.product, c.side, c.prctyp, c.orders) == ("CNC", "SELL", "LMT", 1) for c in family)


def test_the_far_sell_rests_and_the_fill_sell_is_recorded_not_xfailed(rig: Rig) -> None:
    results = cases_hold.run_family(rig.run, cases_hold.family_cases())
    assert results[FAR].verdict == batch.PASS and results[FAR].status == "CANCELED"
    fill = results[FILL]
    assert fill.verdict == batch.RECORD and fill.issue != "closing-sell"
    batch.report(fill)  # passes: a holdings SELL is never the closing-sell xfail
    far_order, fill_order = rig.broker.orders
    assert far_order.side == fill_order.side == "S" and far_order.product == "C"
    assert far_order.price == Decimal("440.00") and fill_order.price == Decimal("400.00")
    assert far_order.quantity == fill_order.quantity == 1
    assert fill_order.status == "CANCELED"  # UAT-like: the SELL did not fill; cancelled once
    holdings = rig.record(FILL, "holdings")
    assert holdings["tradingsymbol"] == "ITC-EQ" and holdings["before"] == 5
    assert holdings["after"] == 5 and holdings["sold"] == 0
    assert rig.run.positions.leftovers() == {}
    assert rig.requests("PlaceOrder") == 2


def test_a_filling_holdings_sell_is_recorded_with_the_position_change(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path, fill_sells=True):
        rig.broker.set_holdings([_holding()])
        results = cases_hold.run_family(rig.run, cases_hold.family_cases()[1:])
        assert results[FILL].verdict == batch.RECORD and results[FILL].status == "COMPLETE"
        holdings = rig.record(FILL, "holdings")
        assert holdings["sold"] == 1 and holdings["before"] == 5
        # A holdings sale is not a position of the run: no leftover, nothing to net with M4.
        assert rig.run.positions.net("ITC-EQ") == 0
        assert rig.run.positions.leftovers() == {}


def test_a_holdings_sale_does_not_net_against_a_run_position(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path, fill_sells=True):
        rig.broker.set_holdings([_holding()])
        rig.run.positions.add_fill("ITC-EQ", 2, lot_size=1, case_id="m4-mis", order_id="1")
        cases_hold.run_family(rig.run, cases_hold.family_cases()[1:])
        assert rig.run.positions.net("ITC-EQ") == 2  # the M4 leftover stays as it was


def test_a_holding_whose_lot_is_not_one_share_is_never_sold(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path, fill_sells=True):
        rig.market.add("NSE", "9999", "SMEX-EQ", last="100.00", lot=5, instrument="EQ")
        listing = {"exch": "NSE", "tsym": "SMEX-EQ", "token": "9999", "ls": "5", "ti": "0.05",
                   "lp": "100.00"}  # fmt: skip
        rig.broker.set_holdings([_holding("2", listing)])
        results = cases_hold.run_family(rig.run, cases_hold.family_cases())
        assert {r.verdict for r in results.values()} == {batch.SKIP}
        assert all("no holdings to sell" in r.reason and "SMEX-EQ (lot size 5)" in r.reason
                   for r in results.values())  # fmt: skip
        assert rig.requests("PlaceOrder") == 0


def test_every_holdings_order_is_one_share(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path, fill_sells=True):
        rig.broker.set_holdings([_holding()])
        cases_hold.run_family(rig.run, cases_hold.family_cases())
        assert [p["qty"] for p in rig.payloads("PlaceOrder")] == ["1", "1"]


def test_the_fill_sell_skips_when_the_holdings_cannot_be_read_before_it(rig: Rig) -> None:
    client: Any = rig.session.client
    read = client.holdings
    calls = 0

    def holdings(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise TradeSmartError("holdings unavailable")
        return read(*args)

    client.holdings = holdings
    results = cases_hold.run_family(rig.run, cases_hold.family_cases())
    assert results[FAR].verdict == batch.PASS
    assert results[FILL].verdict == batch.SKIP and "holdings" in results[FILL].reason
    assert rig.requests("PlaceOrder") == 1


def test_a_resumed_run_reports_a_done_case_even_without_holdings_now(rig: Rig) -> None:
    first = cases_hold.run_family(rig.run, cases_hold.family_cases()[:1])
    assert first[FAR].verdict == batch.PASS
    rig.broker.set_holdings([])  # sold out since: the holdings pre-check would refuse
    again = batch.open_run(rig.session, contracts=rig.market.contracts(TODAY),
                           profile=rig.run.profile)  # fmt: skip
    resumed = cases_hold.run_family(again, cases_hold.family_cases()[:1])
    assert resumed[FAR].verdict == batch.SKIP and "done in run" in resumed[FAR].reason
    assert rig.requests("PlaceOrder") == 1
    assert rig.session.ledger.done(FAR) == batch.PASS  # the earlier verdict stays


def test_a_skip_never_hides_an_earlier_verdict_of_the_run(rig: Rig) -> None:
    [far] = cases_hold.family_cases()[:1]
    assert cases_hold.run_family(rig.run, [far])[FAR].verdict == batch.PASS
    family_tools.skipped(rig.run, far, "a later refusal")
    assert family.run_cases(rig.run, [far])[FAR].verdict == batch.PASS


def test_a_resumed_run_keeps_the_holdings_family_rms_limit(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path, rms={"ITC-EQ"}):
        rig.broker.set_holdings([_holding()])
        results = cases_hold.run_family(rig.run, cases_hold.family_cases()[:1])
        assert results[FAR].verdict == batch.ENV_LIMIT
        again = batch.open_run(rig.session, contracts=rig.market.contracts(TODAY),
                               profile=rig.run.profile)  # fmt: skip
        assert "hold" in again.issues.rms_families


def test_no_holdings_skips_the_family_without_sending(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path):
        results = cases_hold.run_family(rig.run, cases_hold.family_cases())
        assert {r.verdict for r in results.values()} == {batch.SKIP}
        assert all("no holdings" in r.reason for r in results.values())
        assert rig.requests("PlaceOrder") == 0 and rig.requests("Holdings") == 1


def test_a_holding_without_an_nse_listing_or_quantity_is_not_used(tmp_path: Path) -> None:
    bse = {"exch": "BSE", "tsym": "ITC", "token": "500875", "ls": "1", "ti": "0.05", "lp": "400"}
    for rig in make_rig(tmp_path):
        rig.broker.set_holdings([_holding("5", bse), _holding("0")])
        results = cases_hold.run_family(rig.run, cases_hold.family_cases())
        assert {r.verdict for r in results.values()} == {batch.SKIP}
        assert rig.requests("PlaceOrder") == 0


def test_the_fill_sell_skips_at_1530_ist_and_the_far_sell_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADESMART_LIVE_ORDER_OUTSIDE_HOURS", "1")  # the cut-off, not the hours
    for rig in make_rig(tmp_path, now=LATE):
        rig.broker.set_holdings([_holding()])
        results = cases_hold.run_family(rig.run, cases_hold.family_cases())
        assert results[FAR].verdict == batch.PASS
        assert results[FILL].verdict == batch.SKIP and "before 15:30" in results[FILL].reason
        assert rig.requests("PlaceOrder") == 1


def test_the_held_contract_is_priced_from_a_fresh_quote(rig: Rig) -> None:
    rig.market.set_last("ITC-EQ", "300.00")  # the holdings row still says 400
    rig.market.instruments["ITC-EQ"].lower = Decimal("240.00")
    rig.market.set_last("ITC-EQ", "300.00")
    cases_hold.run_family(rig.run, cases_hold.family_cases()[:1])
    [order] = rig.broker.orders
    assert order.price == Decimal("330.00")
