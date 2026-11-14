"""Dry run of the UAT matrix family M5 (cover / bracket orders, exit_order) on the offline broker.

Drives ``uat_matrix/cases_cobo.py`` end to end (the code the live ``test_m5_cobo.py`` runs)
against ``OfflineBroker`` + ``UatLikeMarket``. The broker models no SNO legs: ``LegMarket`` adds one
(a stop SELL of the same product) when a cover or bracket BUY fills, tagged like its entry or
not, to drive the exit path.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.integration.uat_fill_rig import LATE, TODAY, Rig, make_rig
from tests.integration.uat_like import Instrument, UatLikeMarket
from tests.live_orders.uat_matrix import batch, calls, cases, cases_cobo
from tests.live_orders.uat_matrix.calls import CaseRef
from tests.live_orders.uat_matrix.cases import Case
from tests.live_orders.uat_matrix.expect import Profile
from tradesmart import TradeSmart
from tradesmart.enums import OrderType
from tradesmart.testing import drills

MARGIN_REPLY = {
    "stat": "Ok",
    "remarks": "Order Success",
    "cash": "0.00",
    "marginused": "0.00",
    "ordermargin": "120.00",
    "marginusedprev": "0.00",
}
CO_PROBE = "cobo-nse-b-co-lmt-day-buy"
BO_PROBE = "cobo-nse-b-bo-lmt-day-buy"
CO_FILL = "cobo-nse-t-co-lmt-day-buy-fill"
BO_FILL = "cobo-nse-t-bo-lmt-day-buy-fill"
EXIT_REGULAR = "cobo-nse-b-mis-lmt-day-buy-exit"
ALL_EXCHANGES = frozenset({"NSE", "NFO", "BSE", "BFO", "MCX"})


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    for rig in make_rig(tmp_path, profile=None):
        rig.broker.set_reply("GetOrderMargin", MARGIN_REPLY)
        yield rig


def _cases(*ids: str) -> list[Case]:
    by_id = {c.id: c for c in cases_cobo.family_cases()}
    return [by_id[i] for i in ids]


def _refuse(rig: Rig, product: str, message: str = "Rejected : product not enabled") -> None:
    def on_place(kwargs: dict[str, Any]) -> None:
        if str(kwargs["product"]) == product:
            rig.broker.fail_next("PlaceOrder", drills.reject(message))

    rig.market.on_place = on_place


def test_the_family_lists_the_m5_cases_and_its_smoke_tier() -> None:
    family = cases_cobo.family_cases()
    ids = [c.id for c in family]
    assert len(ids) == len(set(ids)) == 21
    assert all(c.family == "cobo" and c.id.startswith("cobo-") for c in family)
    assert sorted(c.id for c in family if c.tier == cases.SMOKE) == [BO_PROBE, CO_PROBE]
    roles = {c.role for c in family if c.product in ("CO", "BO") and "fill" not in c.id}
    assert roles == {"nse-b", "nfo-nifty-f", "nfo-nifty-ce-atm"}
    assert {c.prctyp for c in family if c.product == "CO"} == {"LMT", "SL-LMT", "MKT"}
    assert all(c.orders == 1 for c in family)


def test_the_whole_family_runs_with_the_expected_verdicts(rig: Rig) -> None:
    family = cases_cobo.family_cases()
    results = cases_cobo.run_family(rig.run, family)
    assert list(results) == [c.id for c in family]
    for case in family:
        result = results[case.id]
        if case.id in (CO_FILL, BO_FILL, EXIT_REGULAR):
            assert result.verdict == batch.RECORD, (case.id, result.reason)
        else:  # resting, trigger pending, ALGO_CHK for MKT
            assert result.verdict == batch.PASS, (case.id, result.reason)
    # Every order is final: nothing of the run is left open.
    assert all(o.status in ("COMPLETE", "CANCELED") for o in rig.broker.orders)
    assert len(rig.market.cancels) == len(set(rig.market.cancels))


def test_cover_and_bracket_orders_carry_their_legs(rig: Rig) -> None:
    cases_cobo.run_family(rig.run, _cases(CO_PROBE, BO_PROBE, "cobo-nse-b-bo-mkt-day-buy"))
    places = rig.payloads("PlaceOrder")
    co = next(p for p in places if p["prd"] == "H")
    bo = [p for p in places if p["prd"] == "B"]
    assert Decimal(co["blprc"]) == Decimal("8.00") and "bpprc" not in co  # 2 % of 400, points
    limit = next(p for p in bo if p["prctyp"] == "LMT")
    assert Decimal(limit["blprc"]) == Decimal(limit["bpprc"]) == Decimal("8.00")
    assert Decimal(limit["trailprc"]) == Decimal("2.00")
    market = next(p for p in bo if p["prctyp"] == "MKT")
    assert Decimal(market["mkt_protection"]) == Decimal("5")


def test_the_cover_probe_modifies_price_then_book_loss_and_reads_its_margin(rig: Rig) -> None:
    [result] = cases_cobo.run_family(rig.run, _cases(CO_PROBE)).values()
    assert result.verdict == batch.PASS
    modifies = rig.payloads("ModifyOrder")
    assert len(modifies) == 2
    first, second = modifies
    assert Decimal(first["prc"]) == Decimal("360.05") and Decimal(first["blprc"]) == Decimal("8")
    assert Decimal(second["prc"]) == Decimal("360.05")
    assert Decimal(second["blprc"]) == Decimal("8.05")
    notes = rig.record(CO_PROBE)["notes"]
    assert [m["verdict"] for m in notes["modifies"]] == ["acknowledged", "acknowledged"]
    margin = notes["order_margin"]
    assert margin["ordermargin"] == "120.00"
    [request] = rig.payloads("GetOrderMargin")
    assert request["norenordno"] == result.order_ids[0] and request["prd"] == "H"
    assert len(rig.market.cancels) == 1  # cancelled once by the engine


def test_the_bracket_probe_modifies_each_leg(rig: Rig) -> None:
    cases_cobo.run_family(rig.run, _cases(BO_PROBE))
    modifies = rig.payloads("ModifyOrder")
    legs = [tuple(Decimal(m[k]) for k in ("blprc", "bpprc", "trailprc")) for m in modifies]
    assert legs == [
        (Decimal("8.05"), Decimal("8"), Decimal("2")),
        (Decimal("8.05"), Decimal("8.05"), Decimal("2")),
        (Decimal("8.05"), Decimal("8.05"), Decimal("2.05")),
    ]
    assert all(Decimal(m["prc"]) == Decimal("360") for m in modifies)  # the price is kept


def test_a_refused_modify_turns_the_probe_into_a_record(rig: Rig) -> None:
    rig.broker.fail_next("ModifyOrder", drills.reject("Rejected : modify not allowed"))
    [result] = cases_cobo.run_family(rig.run, _cases(CO_PROBE)).values()
    assert result.verdict == batch.RECORD and "modify not allowed" in result.reason
    assert rig.session.ledger.done(CO_PROBE) == batch.RECORD
    assert rig.record(CO_PROBE, "modifies")["engine_verdict"] == batch.PASS


def test_exit_order_on_a_regular_order_is_recorded(rig: Rig) -> None:
    [result] = cases_cobo.run_family(rig.run, _cases(EXIT_REGULAR)).values()
    assert result.verdict == batch.RECORD
    [exit_call] = [c for c in rig.record(EXIT_REGULAR)["calls"] if c["kind"] == "exit"]
    assert exit_call["verdict"] == "rejected"
    assert "not a cover or bracket" in rig.record(EXIT_REGULAR)["notes"]["exit"]["message"]
    [payload] = rig.payloads("ExitSNOOrder")
    assert payload["prd"] == "H"
    assert rig.broker.orders[0].status == "CANCELED"  # then cancelled once by the engine


def test_a_product_not_enabled_runs_one_probe_and_skips_the_rest(tmp_path: Path) -> None:
    products = {name: ALL_EXCHANGES for name in ("CNC", "MIS", "NRML", "BO")}
    profile = Profile(ALL_EXCHANGES, frozenset({"LMT", "SL-LMT", "MKT", "SL-MKT"}), products)
    for rig in make_rig(tmp_path, profile=profile):
        _refuse(rig, "CO")
        family = cases_cobo.family_cases()
        results = cases_cobo.run_family(rig.run, family)
        probes = {"cobo-nse-b-co-lmt-day-buy", "cobo-nfo-nifty-f-co-lmt-day-buy"}
        for case in family:
            verdict, reason = results[case.id].verdict, results[case.id].reason
            if case.product != "CO":
                continue
            if case.id in probes:  # the first probe per exchange
                assert verdict == batch.PASS and "refused as expected" in reason, case.id
            else:
                assert verdict == batch.SKIP and "not enabled" in reason, (case.id, reason)
        co_places = [p for p in rig.payloads("PlaceOrder") if p["prd"] == "H"]
        assert sorted(p["exch"] for p in co_places) == ["NFO", "NSE"]  # one probe per exchange
        assert results[BO_PROBE].verdict == batch.PASS
        assert results[BO_FILL].verdict == batch.RECORD


def test_a_refused_probe_skips_the_rest_of_its_product_on_that_role(rig: Rig) -> None:
    rig.run.profile = Profile(None, None, None)
    _refuse(rig, "BO", "Rejected : BO not allowed")
    ids = (BO_PROBE, "cobo-nse-b-bo-sl-lmt-day-buy", "cobo-nse-b-bo-mkt-day-buy", BO_FILL)
    results = cases_cobo.run_family(rig.run, _cases(*ids))
    assert results[BO_PROBE].verdict == batch.FAIL  # an unexpected refusal
    for case_id in ids[1:]:  # NSE bracket orders, the fill on another NSE stock included
        assert results[case_id].verdict == batch.SKIP
        assert "BO not allowed" in results[case_id].reason
    assert rig.requests("PlaceOrder") == 1


class LegMarket(UatLikeMarket):
    """When a cover / bracket BUY fills, the broker shows its SNO leg: a stop SELL of the same
    product, tagged like the entry (``tag_legs``) or not."""

    tag_legs = True
    legs_on_fill = True
    leg_client: TradeSmart | None = None
    legs: list[str]

    def _match(
        self, order_id: str, inst: Instrument, kwargs: dict[str, Any], order_type: OrderType
    ) -> None:
        super()._match(order_id, inst, kwargs, order_type)
        order = next(o for o in self.broker.orders if o.id == order_id)
        if not self.legs_on_fill or order.product not in ("H", "B") or order.status != "COMPLETE":
            return
        assert self.leg_client is not None
        legs = {k: kwargs[k] for k in ("book_loss_price", "book_profit_price") if k in kwargs}
        leg = self.leg_client.place_order(
            exchange=inst.exchange, tradingsymbol=inst.tradingsymbol, quantity=order.quantity,
            transaction_type="SELL", product=order.product, order_type="SL-LMT",
            trigger_price=inst.last - 1, price=inst.last - 2,
            tag=kwargs.get("tag") if self.tag_legs else None, **legs,
        )  # fmt: skip
        self.broker.accept(leg)
        self.legs = [*getattr(self, "legs", []), leg]


def _leg_rig(tmp_path: Path, *, tagged: bool, **options: Any) -> Iterator[Rig]:
    for rig in make_rig(tmp_path, profile=None, market_class=LegMarket, **options):
        market = rig.market
        assert isinstance(market, LegMarket)
        market.tag_legs = tagged
        market.leg_client = rig.broker.client()
        try:
            yield rig
        finally:
            market.leg_client.close()


def test_a_filled_cover_order_exits_its_sno_leg(tmp_path: Path) -> None:
    for rig in _leg_rig(tmp_path, tagged=True):
        [result] = cases_cobo.run_family(rig.run, _cases(CO_FILL)).values()
        assert isinstance(rig.market, LegMarket)
        [leg] = rig.market.legs
        assert result.verdict == batch.RECORD and leg in result.reason
        [payload] = rig.payloads("ExitSNOOrder")
        assert payload["norenordno"] == leg and payload["prd"] == "H"
        exit_record = rig.record(CO_FILL, "exit")
        assert exit_record["exit_order_id"] == leg and exit_record["legs_open_after"] == []
        assert rig.run.positions.net("YESBANK-EQ") == 1  # the leg did not fill: kept
        assert all(o.status in ("COMPLETE", "CANCELED") for o in rig.broker.orders)


def test_a_filled_bracket_order_exits_its_sno_leg(tmp_path: Path) -> None:
    for rig in _leg_rig(tmp_path, tagged=True):
        [result] = cases_cobo.run_family(rig.run, _cases(BO_FILL)).values()
        assert result.verdict == batch.RECORD
        [payload] = rig.payloads("ExitSNOOrder")
        assert payload["prd"] == "B"


def test_a_filled_cover_order_without_a_leg_in_the_book_exits_by_its_entry(rig: Rig) -> None:
    [result] = cases_cobo.run_family(rig.run, _cases(CO_FILL)).values()
    assert result.verdict == batch.RECORD and "no SNO leg" in result.reason
    [payload] = rig.payloads("ExitSNOOrder")
    assert payload["norenordno"] == result.order_ids[0]
    assert rig.record(CO_FILL, "exit")["exit"]["verdict"] == "rejected"


def test_an_sno_leg_not_tagged_as_ours_is_check_the_account(tmp_path: Path) -> None:
    for rig in _leg_rig(tmp_path, tagged=False):
        [result] = cases_cobo.run_family(rig.run, _cases(CO_FILL)).values()
        assert result.verdict == batch.CHECK
        assert "not an order of this run" in result.reason
        assert "the open-order invariant stops the run" in result.reason
        [payload] = rig.payloads("ExitSNOOrder")
        assert payload["norenordno"] == result.order_ids[0]  # never a call for the leg
        assert rig.session.ledger.done(CO_FILL) == batch.CHECK
        assert rig.session.guard.tripped("nse") is not None  # CHECK THE ACCOUNT trips NSE
        assert rig.market.cancels == []  # the leg is never touched


def test_an_accepted_market_cover_order_counts_as_a_fill_and_is_exited(tmp_path: Path) -> None:
    for rig in _leg_rig(tmp_path, tagged=True, market="accept"):
        case_id = "cobo-nse-b-co-mkt-day-buy"
        [result] = cases_cobo.run_family(rig.run, _cases(case_id)).values()
        assert result.verdict == batch.MARKET_FILL
        assert isinstance(rig.market, LegMarket)
        [leg] = rig.market.legs
        [payload] = rig.payloads("ExitSNOOrder")
        assert payload["norenordno"] == leg
        assert rig.record(case_id, "exit")["legs_open_after"] == []
        assert all(o.status in ("COMPLETE", "CANCELED") for o in rig.broker.orders)


def test_a_fill_order_is_sent_even_when_the_circuit_band_is_narrow(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path, profile=None):
        inst = rig.market.instruments["YESBANK-EQ"]
        inst.lower, inst.upper = Decimal("19.90"), Decimal("20.10")  # no far price exists
        rig.market.set_last("YESBANK-EQ", "20.00")
        [result] = cases_cobo.run_family(rig.run, _cases(CO_FILL)).values()
        assert result.verdict == batch.RECORD, result.reason
        [order] = rig.broker.orders
        assert order.price == Decimal("20.00") and order.status == "COMPLETE"


def test_the_exit_keeps_a_failing_engine_verdict(tmp_path: Path) -> None:
    for rig in _leg_rig(tmp_path, tagged=True):
        rig.run.issues.pinned = {CO_FILL: {"verdict": "accept"}}  # a fill is then a failure
        [result] = cases_cobo.run_family(rig.run, _cases(CO_FILL)).values()
        assert result.verdict == batch.FAIL and "filled" in result.reason
        assert isinstance(rig.market, LegMarket)
        [leg] = rig.market.legs
        [payload] = rig.payloads("ExitSNOOrder")
        assert payload["norenordno"] == leg  # the exit still runs, as cleanup
        exit_record = rig.record(CO_FILL, "exit")
        assert exit_record["verdict"] == batch.FAIL and exit_record["engine_verdict"] == batch.FAIL
        assert rig.session.ledger.done(CO_FILL) == batch.FAIL
        with pytest.raises(pytest.fail.Exception):
            batch.report(result)


def _legs_after_the_exit(rig: Rig, *, tagged: bool, apply_exit: bool = True) -> None:
    """The SNO leg shows in the book only once exit_order is called (not after the fill)."""
    market = rig.market
    assert isinstance(market, LegMarket) and market.leg_client is not None
    client: Any = rig.session.client
    original = client.exit_order
    leg_client = market.leg_client

    def exit_order(order_id: str, product: str) -> Any:
        entry = next(o for o in rig.broker.orders if o.id == order_id)
        leg = leg_client.place_order(
            exchange=entry.exchange, tradingsymbol=entry.symbol, quantity=entry.quantity,
            transaction_type="SELL", product="CO", order_type="SL-LMT",
            trigger_price=Decimal("19.00"), price=Decimal("18.00"),
            book_loss_price=Decimal("0.40"),
            tag=str(entry.payload.get("remarks")) if tagged else None,
        )  # fmt: skip
        rig.broker.accept(leg)
        market.legs = [*getattr(market, "legs", []), leg]
        return original(order_id, product) if apply_exit else order_id

    client.exit_order = exit_order


@pytest.mark.parametrize("tagged", [True, False], ids=["ours", "foreign"])
def test_a_leg_that_appears_only_after_the_exit_is_followed(tmp_path: Path, tagged: bool) -> None:
    for rig in _leg_rig(tmp_path, tagged=tagged):
        assert isinstance(rig.market, LegMarket)
        rig.market.legs_on_fill = False
        _legs_after_the_exit(rig, tagged=tagged)
        [result] = cases_cobo.run_family(rig.run, _cases(CO_FILL)).values()
        [leg] = rig.market.legs
        exit_record = rig.record(CO_FILL, "exit")
        assert leg in exit_record["legs"]
        if tagged:  # ours: cancelled once, then final
            assert result.verdict == batch.RECORD, result.reason
            assert rig.market.cancels == [leg]
            assert exit_record["legs_open_after"] == []
        else:  # not an order of this run: never touched, CHECK THE ACCOUNT
            assert result.verdict == batch.CHECK and leg in result.reason
            assert rig.market.cancels == []
            assert rig.session.guard.tripped("nse") is not None


def test_an_unknown_cancel_of_a_leg_is_check_the_account(tmp_path: Path) -> None:
    for rig in _leg_rig(tmp_path, tagged=True):
        client: Any = rig.session.client
        client.exit_order = lambda order_id, product: order_id  # acknowledged, no effect
        rig.broker.fail_next("CancelOrder", drills.connect_error())
        [result] = cases_cobo.run_family(rig.run, _cases(CO_FILL)).values()
        assert result.verdict == batch.CHECK and "unknown outcome" in result.reason
        assert isinstance(rig.market, LegMarket)
        [leg] = rig.market.legs
        assert rig.market.cancels == [leg]  # one cancel, never repeated


def test_only_the_entrys_own_legs_are_exited(tmp_path: Path) -> None:
    for rig in _leg_rig(tmp_path, tagged=True):
        rig.session.guard.reserve("other-co", 1)
        plan = {
            "exchange": "NSE", "tradingsymbol": "YESBANK-EQ", "quantity": 1,
            "transaction_type": "BUY", "product": "CO", "order_type": "LMT",
            "price": Decimal("18.00"), "validity": "DAY", "book_loss_price": Decimal("0.40"),
        }  # fmt: skip
        other = calls.place(rig.session, plan, CaseRef("other-co", "nse"))
        assert other.order_id is not None
        [result] = cases_cobo.run_family(rig.run, _cases(CO_FILL)).values()
        assert result.verdict == batch.RECORD, result.reason
        assert isinstance(rig.market, LegMarket)
        [leg] = rig.market.legs
        exit_record = rig.record(CO_FILL, "exit")
        assert exit_record["legs"] == [leg]  # our other CO order is not a leg of this entry
        [detail] = exit_record["leg_rows"]
        assert detail["order_id"] == leg and "snonum" in detail and "sno_fillid" in detail
        assert other.order_id not in rig.market.cancels
        statuses = {o.id: o.status for o in rig.broker.orders}
        assert statuses[other.order_id] == "OPEN"


def test_running_the_family_again_on_the_run_sends_no_second_exit(tmp_path: Path) -> None:
    for rig in _leg_rig(tmp_path, tagged=True):
        first = cases_cobo.run_family(rig.run, _cases(CO_FILL))[CO_FILL]
        again = cases_cobo.run_family(rig.run, _cases(CO_FILL))[CO_FILL]
        assert again.verdict == first.verdict == batch.RECORD and again.reason == first.reason
        places = [line for line in rig.ledger_lines() if line["type"] == "intent"
                  and line.get("kind") == "place"]  # fmt: skip
        assert len(places) == 1 and rig.requests("ExitSNOOrder") == 1


def test_a_resumed_run_keeps_a_cobo_market_fill(tmp_path: Path) -> None:
    for rig in _leg_rig(tmp_path, tagged=True, market="accept"):
        [result] = cases_cobo.run_family(rig.run, _cases("cobo-nse-b-co-mkt-day-buy")).values()
        assert result.verdict == batch.MARKET_FILL
        again = batch.open_run(
            rig.session, contracts=rig.market.contracts(TODAY), profile=rig.run.profile
        )
        assert again.market_filled is not None


def test_fill_cases_skip_at_1530_ist_without_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADESMART_LIVE_ORDER_OUTSIDE_HOURS", "1")  # the cut-off, not the hours
    for rig in make_rig(tmp_path, now=LATE, profile=None):
        results = cases_cobo.run_family(rig.run, _cases(CO_FILL, BO_FILL))
        assert {r.verdict for r in results.values()} == {batch.SKIP}
        assert all("before 15:30" in r.reason for r in results.values())
        assert rig.requests("PlaceOrder") == 0 and rig.requests("ExitSNOOrder") == 0
