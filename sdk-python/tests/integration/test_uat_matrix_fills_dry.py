"""Dry run of the UAT matrix family M4 (fills and closes) on the UAT-like offline broker.

Drives ``uat_matrix/cases_fills.py`` end to end (the code the live ``test_m4_fills.py`` runs)
against ``OfflineBroker`` + ``UatLikeMarket``: a BUY at the last price fills, a SELL does not.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.integration.uat_fill_rig import LATE, Rig, make_rig
from tests.integration.uat_like import Instrument, UatLikeMarket
from tests.live_orders.uat_matrix import batch, cases, cases_fills, family_tools
from tests.live_orders.uat_matrix.batch import BatchItem
from tests.live_orders.uat_matrix.cases import Case
from tradesmart.enums import OrderType
from tradesmart.errors import ConfigurationError
from tradesmart.testing import drills

PROBE = "fills-nfo-banknifty-f-mis-lmt-day-buy-partial"
CLOSE = "fills-nfo-banknifty-f-mis-lmt-day-sell-partial-close"
BFO_FILL = "fills-bfo-sensex-ce-atm-mis-lmt-day-buy"
BFO_CANARY = "shapes-bfo-sensex-f-nrml-lmt-day-buy-canary"


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    yield from make_rig(tmp_path)


def _cases(*ids: str) -> list[Case]:
    by_id = {c.id: c for c in cases_fills.family_cases()}
    return [by_id[i] for i in ids]


def test_the_family_lists_the_m4_cases_and_its_smoke_tier() -> None:
    family = cases_fills.family_cases()
    ids = [c.id for c in family]
    assert len(ids) == len(set(ids))
    assert all(c.family == "fills" for c in family)
    assert {c.role for c in family if c.outcome == cases.FILL_CLOSE} == {
        "nse-b", "nfo-nifty-f", "nfo-nifty-ce-atm", "nfo-stk-f", "bfo-sensex-ce-atm",
    }  # fmt: skip
    assert {c.role for c in family if c.outcome == cases.SHORT_COVER} == {
        "nfo-nifty-f", "nfo-nifty-ce-atm", "nse-b",
    }  # fmt: skip
    assert PROBE in ids and CLOSE in ids
    assert [c.id for c in family if c.tier == cases.SMOKE] == ["fills-nfo-nifty-f-nrml-lmt-day-buy"]
    assert sum(c.orders for c in family if c.tier == cases.SMOKE) == 2  # the NFO-F round trip


def test_the_whole_family_runs_with_the_expected_verdicts(rig: Rig) -> None:
    family = cases_fills.family_cases()
    results = cases_fills.run_family(rig.run, family)
    verdicts = {case_id: r.verdict for case_id, r in results.items()}
    assert set(verdicts) == {c.id for c in family}
    for case in family:
        if case.outcome == cases.FILL_CLOSE:
            assert verdicts[case.id] == batch.XFAIL, (case.id, results[case.id].reason)
            assert results[case.id].issue == "closing-sell"
        elif case.outcome == cases.SHORT_COVER:
            # UAT-like: a SELL at the last price does not fill, so there is nothing to cover.
            assert verdicts[case.id] == batch.SKIP, (case.id, results[case.id].reason)
    assert verdicts[PROBE] == batch.RECORD and verdicts[CLOSE] == batch.RECORD
    # Every fill stays in the run's position ledger, within 2 lots per instrument.
    leftovers = rig.run.positions.leftovers()
    assert leftovers["NIFTYFUT"] == 130 and leftovers["BANKNIFTYFUT"] == 60
    assert leftovers["ITC-EQ"] == 2 and leftovers["BSXC85000"] == 20
    # The shared BFO canary ran first (BFO was undecided), once.
    assert rig.session.ledger.done(BFO_CANARY) == batch.PASS
    canary_places = [
        line for line in rig.ledger_lines()
        if line["type"] == "intent" and line.get("kind") == "place"
        and line.get("case_id") == BFO_CANARY
    ]  # fmt: skip
    assert len(canary_places) == 1
    # Every place is within the declared order counts, never resent.
    declared = sum(c.orders for c in family) + 1  # + the canary
    assert rig.requests("PlaceOrder") <= declared
    assert len(rig.market.cancels) == len(set(rig.market.cancels))
    assert all(o.status in ("COMPLETE", "CANCELED") for o in rig.broker.orders)
    assert "UAT01" not in (rig.session.run_dir / "records.jsonl").read_text(encoding="utf-8")


def test_shorts_are_recorded_when_a_sell_fills(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path, fill_sells=True):
        shorts = [c for c in cases_fills.family_cases() if c.outcome == cases.SHORT_COVER]
        results = cases_fills.run_family(rig.run, shorts)
        assert {r.verdict for r in results.values()} == {batch.RECORD}
        assert rig.run.positions.leftovers() == {}


def test_the_partial_probe_is_sized_to_the_position_ceiling_and_recorded(rig: Rig) -> None:
    results = cases_fills.run_family(rig.run, _cases(PROBE, CLOSE))
    assert results[PROBE].verdict == batch.RECORD
    probe = rig.record(PROBE)
    assert probe["plan"]["quantity"] == 60  # 2 lots of 30, not the 10 lots asked for
    assert Decimal(probe["plan"]["price"]) == Decimal("52000")
    assert probe["plan"]["transaction_type"] == "BUY"
    sizing = rig.record(PROBE, "sizing")
    assert sizing["asked_lots"] == 10 and sizing["lots"] == 2 and sizing["ceiling_lots"] == 2
    assert "ceiling" in sizing["reason"]
    close = rig.record(CLOSE)
    assert close["plan"]["quantity"] == 60 and close["plan"]["transaction_type"] == "SELL"
    assert rig.run.positions.net("BANKNIFTYFUT") == 60  # the SELL did not fill (UAT-like)


def test_the_partial_probe_is_sized_to_what_is_left_under_the_ceiling(rig: Rig) -> None:
    contract = rig.run.contracts.get("nfo-banknifty-f")
    assert contract is not None
    rig.run.positions.add_fill("BANKNIFTYFUT", 30, lot_size=30, case_id="earlier", order_id="1")
    cases_fills.run_family(rig.run, _cases(PROBE))
    assert rig.record(PROBE)["plan"]["quantity"] == 30
    assert rig.record(PROBE, "sizing")["lots"] == 1


def test_the_partial_probe_sends_nothing_without_room_under_the_ceiling(rig: Rig) -> None:
    rig.run.positions.add_fill("BANKNIFTYFUT", 60, lot_size=30, case_id="earlier", order_id="1")
    results = cases_fills.run_family(rig.run, _cases(PROBE, CLOSE))
    assert results[PROBE].verdict == batch.SKIP and "ceiling" in results[PROBE].reason
    assert results[CLOSE].verdict == batch.SKIP and "nothing to close" in results[CLOSE].reason
    assert rig.requests("PlaceOrder") == 0


class PartialMarket(UatLikeMarket):
    """Fills one whole lot (30) of a BUY at the last price on BANKNIFTYFUT; the rest stays
    OPEN (``stick``: and its cancel is then acknowledged but never applied)."""

    stick = False

    def _match(
        self, order_id: str, inst: Instrument, kwargs: dict[str, Any], order_type: OrderType
    ) -> None:
        if inst.tradingsymbol == "BANKNIFTYFUT" and str(kwargs["transaction_type"]) == "BUY":
            self.broker.fill(order_id, inst.last, inst.lot)
            if self.stick:
                self.stuck.add(inst.tradingsymbol)
            return
        super()._match(order_id, inst, kwargs, order_type)


class StuckPartialMarket(PartialMarket):
    stick = True


def test_partial_fill_progress_is_recorded_and_the_rest_cancelled(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path, market_class=PartialMarket):
        results = cases_fills.run_family(rig.run, _cases(PROBE, CLOSE))
        assert results[PROBE].verdict == batch.RECORD
        assert results[CLOSE].verdict == batch.RECORD  # the SELL did not fill: recorded
        probe = rig.record(PROBE)
        progress = probe["notes"]["fill_progress"]
        assert progress and progress[0]["filled"] == 30 and progress[0]["status"] == "OPEN"
        [buy, sell] = rig.broker.orders
        assert (buy.status, buy.filled_quantity) == ("CANCELED", 30)  # the rest cancelled once
        assert rig.market.cancels.count(buy.id) == 1
        assert rig.run.positions.net("BANKNIFTYFUT") == 30
        assert sell.quantity == 30 and sell.side == "S"  # the close sells what filled
        assert sell.status == "CANCELED"  # it did not fill (UAT-like): cancelled once


def test_a_stuck_partly_filled_order_keeps_its_fill_in_the_position_ledger(
    tmp_path: Path,
) -> None:
    for rig in make_rig(tmp_path, market_class=StuckPartialMarket):
        results = cases_fills.run_family(rig.run, _cases(PROBE, CLOSE))
        assert results[PROBE].verdict == batch.STUCK
        assert rig.run.positions.net("BANKNIFTYFUT") == 30  # the real fill is never lost
        assert results[CLOSE].verdict == batch.SKIP  # the NFO breaker tripped
        assert "circuit breaker" in results[CLOSE].reason
        assert rig.requests("PlaceOrder") == 1


def _stop_file_writer(path: Path) -> Any:
    """An ``on_place`` callback that writes the STOP file (the next place is refused)."""

    def stop(kwargs: dict[str, Any]) -> None:
        path.write_text("stop", encoding="utf-8")

    return stop


def test_a_stop_mid_batch_keeps_the_fill_of_a_stuck_partly_filled_order(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path, market_class=StuckPartialMarket):
        rig.run.workers = 1  # the probe is placed, then the STOP file stops the next place
        rig.market.on_place = _stop_file_writer(rig.root / "STOP")
        probe = BatchItem(
            _cases(PROBE)[0], plan=family_tools.fill_plan(rig.run, side="BUY", lots=2)
        )
        later = cases.shape_grid("shapes", "nse-b")[0]
        first, second = batch.run_batch(rig.run, [probe, later])
        assert first.verdict == batch.STUCK and second.verdict == batch.SKIP, first.reason
        assert rig.run.positions.net("BANKNIFTYFUT") == 30
        assert rig.requests("PlaceOrder") == 1


def test_an_engine_error_after_a_partial_fill_keeps_the_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(run: Any, work: Any, outcome: Any) -> None:
        raise RuntimeError("engine bug after the place")

    monkeypatch.setattr(batch, "_early_rms", broken)
    for rig in make_rig(tmp_path, market_class=PartialMarket):
        results = cases_fills.run_family(rig.run, _cases(PROBE))
        assert results[PROBE].verdict == batch.FAIL and "engine bug" in results[PROBE].reason
        assert rig.run.positions.net("BANKNIFTYFUT") == 30
        [buy] = rig.broker.orders
        assert buy.status == "CANCELED"  # the rest is still cancelled once


def test_a_partly_filled_order_whose_cancel_is_unknown_keeps_its_fill(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path, market_class=PartialMarket):
        rig.broker.fail_next("CancelOrder", drills.connect_error())
        results = cases_fills.run_family(rig.run, _cases(PROBE))
        assert results[PROBE].verdict == batch.CHECK and "cancel" in results[PROBE].reason
        assert rig.run.positions.net("BANKNIFTYFUT") == 30  # the real fill is never lost


def test_a_session_error_in_the_read_only_views_keeps_the_verdicts(rig: Rig) -> None:
    client: Any = rig.session.client
    read = client.positions
    reads = 0

    def positions() -> Any:
        nonlocal reads
        reads += 1
        if reads > 1:  # after the batch
            raise ConfigurationError("session expired")
        return read()

    client.positions = positions
    smoke = [c for c in cases_fills.family_cases() if c.tier == cases.SMOKE]
    results = cases_fills.run_family(rig.run, smoke)
    assert [r.verdict for r in results.values()] == [batch.XFAIL]
    views = rig.record(smoke[0].id, "views")
    after = views["positions"]["NIFTYFUT/NRML"]["after"]
    assert isinstance(after, str) and "ConfigurationError" in after


def test_each_fill_records_its_trades_and_the_positions_change(rig: Rig) -> None:
    smoke = [c for c in cases_fills.family_cases() if c.tier == cases.SMOKE]
    cases_fills.run_family(rig.run, smoke)
    [case] = smoke
    views = rig.record(case.id, "views")
    [trade] = views["trades"]
    assert trade["trantype"] == "B" and trade["tsym"] == "NIFTYFUT"
    delta = views["positions"]["NIFTYFUT/NRML"]
    assert delta["before"] is None and delta["after"]["netqty"] == "65"
    assert rig.requests("TradeBook") >= 1 and rig.requests("PositionBook") >= 2


class RestingMarket(UatLikeMarket):
    """A BUY at the last price on BANKNIFTYFUT rests OPEN (it fills later, in the test)."""

    def _match(
        self, order_id: str, inst: Instrument, kwargs: dict[str, Any], order_type: OrderType
    ) -> None:
        if inst.tradingsymbol == "BANKNIFTYFUT" and str(kwargs["transaction_type"]) == "BUY":
            return
        super()._match(order_id, inst, kwargs, order_type)


def _fill_on_the_sixth_read(rig: Rig) -> None:
    """The open BUY fills a few order-book reads into the fill wait."""
    client: Any = rig.session.client
    read = client.orders
    reads = 0

    def orders() -> Any:
        nonlocal reads
        reads += 1
        buys = [o for o in rig.broker.orders if o.side == "B" and o.status == "OPEN"]
        if reads == 6 and buys:
            rig.broker.fill(buys[0].id, Decimal("52000.00"))
        return read()

    client.orders = orders


def test_the_fill_wait_ends_as_soon_as_the_order_fills(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path, market_class=RestingMarket):
        _fill_on_the_sixth_read(rig)
        start = rig.session.clock()
        results = cases_fills.run_family(rig.run, _cases(PROBE))
        assert results[PROBE].verdict == batch.RECORD
        progress = rig.record(PROBE)["notes"]["fill_progress"]
        assert [(p["status"], p["filled"]) for p in progress] == [("OPEN", 0), ("COMPLETE", 60)]
        assert rig.session.clock() - start < 10  # not the whole fill wait (60 s)
        assert rig.market.cancels == []  # filled: nothing to cancel


def test_the_partial_probe_skips_at_1530_ist_without_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADESMART_LIVE_ORDER_OUTSIDE_HOURS", "1")  # the cut-off, not the hours
    for rig in make_rig(tmp_path, now=LATE):
        results = cases_fills.run_family(rig.run, _cases(PROBE, CLOSE))
        assert results[PROBE].verdict == batch.SKIP and "before 15:30" in results[PROBE].reason
        assert results[CLOSE].verdict == batch.SKIP
        assert rig.requests("PlaceOrder") == 0


def test_the_bfo_fill_skips_the_canary_when_bfo_is_already_confirmed(rig: Rig) -> None:
    rig.run.issues.canary_ok("bfo")
    results = cases_fills.run_family(rig.run, _cases(BFO_FILL))
    assert results[BFO_FILL].verdict == batch.XFAIL
    assert rig.session.ledger.done(BFO_CANARY) is None  # not sent by this family
    assert rig.requests("PlaceOrder") == 2


def test_a_stuck_bfo_canary_skips_the_bfo_fill(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path, stuck={"BSXFUT"}):
        results = cases_fills.run_family(rig.run, _cases(BFO_FILL))
        assert results[BFO_FILL].verdict == batch.SKIP and "F26" in results[BFO_FILL].reason
        assert rig.requests("PlaceOrder") == 1  # the canary only


def test_the_smoke_round_trip_is_two_orders(rig: Rig) -> None:
    smoke = [c for c in cases_fills.family_cases() if c.tier == cases.SMOKE]
    results = cases_fills.run_family(rig.run, smoke)
    assert [r.verdict for r in results.values()] == [batch.XFAIL]
    assert rig.requests("PlaceOrder") == 2
    buy, sell = rig.broker.orders
    assert buy.price == sell.price == Decimal("26040.00")
