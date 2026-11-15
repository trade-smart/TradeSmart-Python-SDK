"""Dry runs of the UAT matrix batch engine against the offline broker (no network).

``tests/live_orders/uat_matrix/batch.py`` drives real ``TradeSmart`` clients wired to a
``OfflineBroker`` that plays UAT as observed (``tests/integration/uat_like.py``: BUY at the last
price fills, SELL does not, off-tick / non-lot / beyond-circuit rejections, ALGO_CHK for MKT and
SL-MKT, BSE stays PENDING). The session is a real ``MatrixSession`` (ledger, guard, STOP file)
in ``tmp_path``; the engine's waits run on a fake clock, its pacing on the real one.
"""

from __future__ import annotations

import json
import signal
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.integration.uat_like import RMS, UatLikeMarket, hold_every_stock
from tests.live_orders.safety import Refusal
from tests.live_orders.uat_matrix import batch, calls, cases, gate
from tests.live_orders.uat_matrix.calls import CaseRef
from tests.live_orders.uat_matrix.cases import Case
from tests.live_orders.uat_matrix.expect import Profile
from tests.live_orders.uat_matrix.guard import STOP_REASON, Guard, StopRun
from tests.live_orders.uat_matrix.ledger import LEDGER_NAME, Ledger
from tests.live_orders.uat_matrix.pricing import PriceSnapshot
from tradesmart.errors import RateLimitError, RequestValidationError
from tradesmart.models.base import IST
from tradesmart.testing import OfflineBroker, drills

TODAY = date(2026, 11, 13)
MORNING = datetime(2026, 11, 13, 10, 0, tzinfo=IST)
EXPIRY = TODAY + timedelta(days=14)
RUN_ID = "1113test"


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Rig:
    broker: OfflineBroker
    market: UatLikeMarket
    session: gate.MatrixSession
    run: batch.MatrixRun
    root: Path

    def requests(self, endpoint: str) -> int:
        return sum(1 for r in self.broker.requests if r.endpoint == endpoint)

    def statuses(self) -> dict[str, str]:
        return {o.id: o.status for o in self.broker.orders}

    def records(self) -> list[dict[str, Any]]:
        path = self.session.run_dir / "records.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def ledger_lines(self) -> list[dict[str, Any]]:
        path = self.session.run_dir / LEDGER_NAME
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def stop_file(self) -> Path:
        return self.root / "STOP"


def _instruments(market: UatLikeMarket) -> None:
    market.add("NSE", "1660", "ITC-EQ", last="400.00", lower="320.00", upper="480.00",
               role="nse-b", instrument="EQ")  # fmt: skip
    market.add("BSE", "500875", "ITC", last="400.10", role="bse-b", instrument="A")
    market.add(
        "NFO", "500", "NIFTYFUT", last="26040.00", tick="0.10", lot=65, role="nfo-nifty-f",
        kind="fut", instrument="FUTIDX", expiry=EXPIRY, underlying="NIFTY",
    )  # fmt: skip
    market.add(
        "NFO", "601", "NIFTYC26000", last="180.00", lot=65, role="nfo-nifty-ce-atm", kind="opt",
        instrument="OPTIDX", expiry=EXPIRY, strike=26000, option_type="CE", underlying="NIFTY",
    )  # fmt: skip
    market.add(
        "MCX", "900", "CRUDEOIL19NOV26", last="9210", tick="1", lot=100, role="mcx-crudeoil-f",
        kind="fut", instrument="FUTCOM", expiry=EXPIRY, underlying="CRUDEOIL",
    )  # fmt: skip


def _rig(tmp_path: Path, *, max_open: int = 50, now: datetime = MORNING) -> Iterator[Rig]:
    t = FakeTime()
    with OfflineBroker("UAT01") as broker:
        market = UatLikeMarket(broker)
        _instruments(market)
        hold_every_stock(market)  # CNC SELL cases need holdings
        client = market.install(broker.client())
        run_dir = tmp_path / RUN_ID
        run_dir.mkdir()
        session = gate.MatrixSession(
            client=client,
            run_id=RUN_ID,
            trading_day=TODAY,
            run_dir=run_dir,
            ledger=Ledger(run_dir / LEDGER_NAME, RUN_ID, forbidden=["UAT01"]),
            guard=Guard(tmp_path / "STOP", max_open=max_open, clock=t.clock),
            sleep=t.sleep,
            clock=t.clock,
        )
        run = batch.open_run(
            session,
            contracts=market.contracts(TODAY),
            profile=Profile(None, None, None),
            now=lambda: now,
        )
        try:
            yield Rig(broker, market, session, run, tmp_path)
        finally:
            client.close()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    yield from _rig(tmp_path)


def _grid(role: str = "nse-b", family: str = "shapes") -> list[Case]:
    return cases.shape_grid(family, role)


def _one(role: str, prctyp: str = "LMT", ret: str = "DAY", side: str = "BUY") -> Case:
    product = "MIS" if role.startswith("nse") else "NRML"
    return next(
        c
        for c in _grid(role)
        if (c.product, c.prctyp, c.ret, c.side) == (product, prctyp, ret, side)
    )


# -- a 25-case batch -------------------------------------------------------------------------------


def test_a_25_case_batch_places_observes_cancels_and_records_one_line_each(rig: Rig) -> None:
    batch_cases = _grid()[:25]
    results = batch.run_batch(rig.run, batch_cases)
    assert [r.case_id for r in results] == [c.id for c in batch_cases]
    verdicts = {r.case_id: r.verdict for r in results}
    assert set(verdicts.values()) <= {batch.PASS, batch.RECORD}, verdicts
    assert verdicts["shapes-nse-b-cnc-lmt-day-buy"] == batch.PASS
    assert verdicts["shapes-nse-b-cnc-lmt-ioc-buy"] == batch.PASS  # cancelled by the exchange
    assert verdicts["shapes-nse-b-cnc-lmt-eos-buy"] == batch.PASS  # documented refusal
    assert verdicts["shapes-nse-b-cnc-mkt-day-buy"] == batch.PASS  # ALGO_CHK
    assert verdicts["shapes-nse-b-cnc-lmt-day-sell"] == batch.RECORD  # delivery SELL, no holding
    assert rig.requests("PlaceOrder") == 25  # one place call per case, never resent
    assert all(s in ("CANCELED", "REJECTED") for s in rig.statuses().values())
    resting = [o for o in rig.broker.orders if any(h.get("status") == "OPEN" or
               h.get("status") == "TRIGGER_PENDING" for h in o.history)]  # fmt: skip
    assert len(rig.market.cancels) == len(set(rig.market.cancels))  # one cancel per order
    assert {o.id for o in resting if o.status == "CANCELED"} >= set(rig.market.cancels)
    records = rig.records()
    assert sorted(r["case_id"] for r in records) == sorted(c.id for c in batch_cases)
    assert "UAT01" not in (rig.session.run_dir / "records.jsonl").read_text(encoding="utf-8")
    final = {line["case_id"] for line in rig.ledger_lines() if line["type"] == "verdict"}
    assert final == {c.id for c in batch_cases}  # a resumed run skips every one of them


def test_every_order_is_priced_from_a_fresh_quote_taken_just_before_it(rig: Rig) -> None:
    rig.market.set_last("ITC-EQ", "300.00")  # the contract still says 400: never used
    rig.market.instruments["ITC-EQ"].lower = Decimal("240.00")
    rig.market.set_last("ITC-EQ", "300.00")
    [result] = batch.run_batch(rig.run, [_one("nse-b")])
    assert result.verdict == batch.PASS
    [order] = rig.broker.orders
    assert order.price == Decimal("270.00")
    sequence = [r.endpoint for r in rig.broker.requests]
    assert sequence[sequence.index("PlaceOrder") - 1] == "GetQuotes"


def test_a_resumed_run_skips_the_cases_done_without_any_call(rig: Rig) -> None:
    case = _one("nse-b")
    batch.run_batch(rig.run, [case])
    before = rig.requests("PlaceOrder")
    [again] = batch.run_batch(rig.run, [case])
    assert again.verdict == batch.SKIP and "done in run" in again.reason
    assert rig.requests("PlaceOrder") == before


# -- rejections ------------------------------------------------------------------------------------


def _probe(base: Case, suffix: str) -> Case:
    return Case(
        id=f"reject-{suffix}", family="reject", role=base.role, product=base.product,
        prctyp="LMT", ret="DAY", side="BUY", orders=1, outcome=cases.REJECT,
    )  # fmt: skip


def test_off_tick_non_lot_and_beyond_circuit_orders_are_rejected(rig: Rig) -> None:
    nfo = _one("nfo-nifty-f")

    def off_tick(case: Case, contract: Any, snapshot: PriceSnapshot) -> dict[str, Any]:
        return {**batch.default_plan(case, contract, snapshot), "price": Decimal("23436.05")}

    def non_lot(case: Case, contract: Any, snapshot: PriceSnapshot) -> dict[str, Any]:
        return {**batch.default_plan(case, contract, snapshot), "quantity": 66}

    def beyond(case: Case, contract: Any, snapshot: PriceSnapshot) -> dict[str, Any]:
        return {**batch.default_plan(case, contract, snapshot), "price": Decimal("40000.00")}

    items = [
        batch.BatchItem(_probe(nfo, "tick"), plan=off_tick),
        batch.BatchItem(_probe(nfo, "lot"), plan=non_lot),
        batch.BatchItem(_probe(nfo, "circuit"), plan=beyond),
    ]
    results = batch.run_batch(rig.run, items)
    assert [r.verdict for r in results] == [batch.PASS] * 3
    assert ["tick size" in results[0].reason, "lot size" in results[1].reason] == [True, True]
    assert rig.requests("CancelOrder") == 0 and rig.session.guard.tripped("nfo") is None


def test_three_unexpected_rejections_in_a_row_trip_the_segment(rig: Rig) -> None:
    rig.market.reject_all = "Rejected : ORA:something unexpected"
    grid = [c for c in _grid("nfo-nifty-f") if c.outcome == cases.REST][:3]
    results = batch.run_batch(rig.run, grid)
    assert [r.verdict for r in results] == [batch.FAIL] * 3
    assert rig.session.guard.tripped("nfo") is not None
    rig.market.reject_all = None
    placed = rig.requests("PlaceOrder")
    [later] = batch.run_batch(rig.run, [_one("nfo-nifty-f", "SL-LMT")])
    assert later.verdict == batch.SKIP and "circuit breaker" in later.reason
    assert rig.requests("PlaceOrder") == placed


def test_an_rms_red_rejection_is_an_environment_limit_for_the_family(rig: Rig) -> None:
    rig.market.rms.add("NIFTYFUT")
    first = _one("nfo-nifty-f")
    [result] = batch.run_batch(rig.run, [first])
    assert result.verdict == batch.ENV_LIMIT and "RED:" in result.reason
    rig.market.rms.clear()
    placed = rig.requests("PlaceOrder")
    later = batch.run_batch(rig.run, [_one("nse-b"), _one("nfo-nifty-ce-atm")])
    assert [r.verdict for r in later] == [batch.SKIP, batch.SKIP]
    assert all("environment limit" in r.reason for r in later)
    assert rig.requests("PlaceOrder") == placed
    assert RMS.startswith("RED:")


def test_a_market_order_is_refused_with_algo_chk_and_passes(rig: Rig) -> None:
    results = batch.run_batch(rig.run, [_one("nse-b", "MKT"), _one("nse-b", "SL-MKT")])
    assert [r.verdict for r in results] == [batch.PASS, batch.PASS]
    assert all("ALGO_CHK" in r.reason for r in results)


def test_an_accepted_market_order_counts_as_a_fill_and_stops_the_market_cases(rig: Rig) -> None:
    rig.market.market = "accept"
    first, second = _one("nse-b", "MKT"), _one("nse-b", "MKT", side="SELL")
    results = batch.run_batch(rig.run, [first, second])
    assert results[0].verdict == batch.MARKET_FILL
    assert results[1].verdict == batch.SKIP and "market" in results[1].reason
    assert rig.run.positions.leftovers() == {"ITC-EQ": 1}
    assert rig.requests("PlaceOrder") == 1


# -- prices ----------------------------------------------------------------------------------------


def test_no_last_price_at_placement_skips_and_never_sends_a_priceless_order(rig: Rig) -> None:
    rig.market.set_last("ITC-EQ", "0")
    [result] = batch.run_batch(rig.run, [_one("nse-b")])
    assert result.verdict == batch.SKIP and "last price" in result.reason
    assert rig.requests("PlaceOrder") == 0


def test_an_unavailable_role_skips_with_its_reason(rig: Rig) -> None:
    [result] = batch.run_batch(rig.run, [_one("nse-t")])
    assert result.verdict == batch.SKIP and "nse-t" in result.reason
    assert rig.requests("PlaceOrder") == 0


# -- stuck orders and the BSE canary ---------------------------------------------------------------


def test_a_stuck_order_trips_the_segment_breaker(rig: Rig) -> None:
    rig.market.stuck.add("NIFTYFUT")
    [result] = batch.run_batch(rig.run, [_one("nfo-nifty-f")])
    assert result.verdict == batch.STUCK
    assert "stuck" in (rig.session.guard.tripped("nfo") or "")
    assert rig.market.cancels == [result.order_ids[0]]  # one cancel, never repeated
    assert any(line["type"] == "stuck" for line in rig.ledger_lines())
    placed = rig.requests("PlaceOrder")
    [later] = batch.run_batch(rig.run, [_one("nfo-nifty-ce-atm")])
    assert later.verdict == batch.SKIP and "circuit breaker" in later.reason
    assert rig.requests("PlaceOrder") == placed


def test_the_bse_canary_goes_first_and_a_stuck_one_skips_the_segment(rig: Rig) -> None:
    canary = next(c for c in cases.CASES if c.canary and c.segment == "bse")
    grid = [c for c in cases.shape_grid("bse", "bse-b") if c.outcome == cases.REST][:2]
    results = batch.run_batch(rig.run, [*grid, canary])
    by_id = {r.case_id: r for r in results}
    assert by_id[canary.id].verdict == batch.STUCK and "F26" in by_id[canary.id].reason
    assert all(by_id[c.id].verdict == batch.SKIP for c in grid)
    assert all("F26" in by_id[c.id].reason for c in grid)
    assert rig.requests("PlaceOrder") == 1
    with pytest.raises(pytest.skip.Exception, match="F26"):
        batch.report(by_id[canary.id])


def test_a_bse_canary_that_rests_lets_the_segment_run_in_the_same_batch(rig: Rig) -> None:
    canary = next(c for c in cases.CASES if c.canary and c.segment == "bse")
    grid = [c for c in cases.shape_grid("bse", "bse-b") if c.outcome == cases.REST][:2]
    original = rig.market._sticks
    rig.market._sticks = lambda exchange, symbol: False  # type: ignore[method-assign]
    try:
        results = batch.run_batch(rig.run, [*grid, canary])
    finally:
        rig.market._sticks = original  # type: ignore[method-assign]
    assert [r.verdict for r in results] == [batch.PASS, batch.PASS, batch.PASS]
    assert rig.requests("PlaceOrder") == 3


# -- fills -----------------------------------------------------------------------------------------


def _fill_case(role: str = "nfo-nifty-f", product: str = "NRML") -> Case:
    return next(
        c for c in cases.CASES if c.family == "fills" and c.role == role and c.product == product
        and c.outcome == cases.FILL_CLOSE
    )  # fmt: skip


def test_a_fill_whose_closing_sell_does_not_fill_is_an_xfail_and_keeps_the_position(
    rig: Rig,
) -> None:
    [result] = batch.run_batch(rig.run, [_fill_case()])
    assert result.verdict == batch.XFAIL and result.issue == "closing-sell"
    assert rig.run.positions.leftovers() == {"NIFTYFUT": 65}
    buy, sell = rig.broker.orders
    assert (buy.side, buy.status, sell.side, sell.status) == ("B", "COMPLETE", "S", "CANCELED")
    assert buy.price == sell.price == Decimal("26040.00")
    with pytest.raises(pytest.xfail.Exception, match="closing SELL"):
        batch.report(result)


def test_a_closing_sell_whose_cancel_is_unknown_is_check_the_account(rig: Rig) -> None:
    rig.broker.fail_next("CancelOrder", drills.connect_error())
    [result] = batch.run_batch(rig.run, [_fill_case()])
    assert result.verdict == batch.CHECK and "cancel" in result.reason
    assert rig.run.positions.leftovers() == {"NIFTYFUT": 65}  # the fill is still recorded
    assert len(rig.market.cancels) == 1


def test_a_closing_sell_that_fills_is_an_xpass(rig: Rig) -> None:
    rig.market.fill_sells = True
    [result] = batch.run_batch(rig.run, [_fill_case()])
    assert result.verdict == batch.XPASS
    assert rig.run.positions.leftovers() == {}


def test_the_position_ceiling_stops_a_third_lot_on_one_instrument(rig: Rig) -> None:
    first, second = _fill_case(), _fill_case(product="MIS")
    results = batch.run_batch(rig.run, [first, second])
    assert [r.verdict for r in results] == [batch.XFAIL, batch.XFAIL]
    assert rig.run.positions.leftovers() == {"NIFTYFUT": 130}
    third = Case(
        id="fills-extra", family="fills", role="nfo-nifty-f", product="MIS", prctyp="LMT",
        ret="DAY", side="BUY", orders=2, outcome=cases.FILL_CLOSE,
    )  # fmt: skip
    placed = rig.requests("PlaceOrder")
    [result] = batch.run_batch(rig.run, [third])
    assert result.verdict == batch.SKIP and "ceiling" in result.reason
    assert rig.requests("PlaceOrder") == placed


def test_fill_cases_skip_at_1530_ist_and_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADESMART_LIVE_ORDER_OUTSIDE_HOURS", "1")  # the cut-off, not the hours
    late = datetime(2026, 11, 13, 15, 30, tzinfo=IST)
    for rig in _rig(tmp_path, now=late):
        [result] = batch.run_batch(rig.run, [_fill_case()])
        assert result.verdict == batch.SKIP and "before 15:30" in result.reason
        assert rig.requests("PlaceOrder") == 0


# -- MAX_OPEN --------------------------------------------------------------------------------------


def test_never_more_than_max_open_orders_of_the_run_are_open(tmp_path: Path) -> None:
    for rig in _rig(tmp_path, max_open=3):
        grid = [c for c in _grid() if c.outcome in (cases.REST, cases.TRIGGER_PENDING)][:7]
        results = batch.run_batch(rig.run, grid)
        assert [r.verdict for r in results] == [batch.PASS] * 7
        assert rig.market.max_open_seen <= 3
        assert rig.requests("PlaceOrder") == 7


# -- Review Focus 1: a foreign open order ----------------------------------------------------------


def test_a_foreign_open_order_stops_the_run_before_the_next_batch(rig: Rig) -> None:
    rig.session.guard.reserve("manual-ours", 1)
    plan = {
        "exchange": "NSE",
        "tradingsymbol": "ITC-EQ",
        "quantity": 1,
        "transaction_type": "BUY",
        "product": "MIS",
        "order_type": "LMT",
        "price": Decimal("360.00"),
        "validity": "DAY",
    }
    ours = calls.place(rig.session, plan, CaseRef("manual-ours", "nse"))
    assert ours.order_id is not None
    hand = rig.broker.client()  # someone trading by hand: not through the matrix
    foreign = hand.place_order(
        exchange="NSE", tradingsymbol="ITC-EQ", quantity=1, transaction_type="BUY",
        product="CNC", order_type="LMT", price="350.00",
    )  # fmt: skip
    rig.broker.accept(foreign)
    placed = rig.requests("PlaceOrder")
    results = batch.run_batch(rig.run, _grid()[:3])
    assert all(r.verdict == batch.SKIP and "not in this run's ledger" in r.reason for r in results)
    assert rig.requests("PlaceOrder") == placed  # nothing new was placed
    statuses = rig.statuses()
    assert statuses[ours.order_id] == "CANCELED" and statuses[foreign] == "OPEN"
    assert foreign not in rig.market.cancels
    assert rig.session.guard.stopped()
    hand.close()


# -- Review Focus 2: the STOP file mid-batch -------------------------------------------------------


def test_the_stop_file_mid_batch_stops_placing_and_cancels_the_batch(rig: Rig) -> None:
    sent: list[str] = []

    def on_place(kwargs: dict[str, Any]) -> None:
        sent.append(kwargs["tag"])
        if len(sent) == 3:
            rig.stop_file().write_text("stop", encoding="utf-8")

    rig.market.on_place = on_place
    rig.run.workers = 1  # deterministic: one place at a time
    grid = [c for c in _grid() if c.outcome in (cases.REST, cases.TRIGGER_PENDING)][:6]
    assert len(grid) == 6
    results = batch.run_batch(rig.run, grid)
    assert rig.requests("PlaceOrder") == 3  # no place call after the STOP file
    assert all(s == "CANCELED" for s in rig.statuses().values())  # the batch, cancelled
    assert [r.verdict for r in results[3:]] == [batch.SKIP] * 3
    assert all(r.reason == STOP_REASON for r in results[3:])
    assert all(r.verdict == batch.SKIP and STOP_REASON in r.reason for r in results[:3])
    assert len(rig.market.cancels) == len(set(rig.market.cancels)) == 3
    [later] = batch.run_batch(rig.run, [_one("nfo-nifty-f")])
    assert later.verdict == batch.SKIP and later.reason == STOP_REASON


# -- Review Focus 5: a cancel that times out -------------------------------------------------------


def test_a_cancel_that_times_out_is_resolved_by_the_book_and_never_repeated(rig: Rig) -> None:
    rig.broker.fail_next("CancelOrder", drills.timeout_after_send())  # it takes effect
    [result] = batch.run_batch(rig.run, [_one("nse-b")])
    assert result.verdict == batch.PASS and result.status == "CANCELED"
    assert rig.requests("CancelOrder") == 1
    cancel = next(c for c in result.record["calls"] if c["kind"] == "cancel")
    assert cancel["verdict"] == calls.RESOLVED


def test_a_cancel_that_times_out_without_effect_is_check_the_account(rig: Rig) -> None:
    rig.broker.fail_next("CancelOrder", drills.connect_error())  # nothing went out
    [result] = batch.run_batch(rig.run, [_one("nse-b")])
    assert rig.requests("CancelOrder") == 0 and len(rig.market.cancels) == 1  # one attempt
    cancel = next(c for c in result.record["calls"] if c["kind"] == "cancel")
    assert cancel["verdict"] == calls.CHECK
    assert result.verdict == batch.CHECK and "cancel" in result.reason
    assert rig.session.guard.tripped("nse") is not None
    [order] = rig.broker.orders
    assert order.status == "OPEN"  # never cancelled again: left for a human (CHECK THE ACCOUNT)


# -- family hooks ----------------------------------------------------------------------------------


def test_a_family_hook_modifies_a_resting_order_with_the_batch_row(rig: Rig) -> None:
    seen: list[str | None] = []

    def nudge(ctx: batch.Placed) -> None:
        assert ctx.row is not None
        seen.append(str(ctx.row.status))
        assert ctx.row.price is not None
        outcome = ctx.modify(price=ctx.row.price - ctx.contract.tick_size)  # the rest: the plan's
        ctx.notes["modify"] = outcome.verdict

    book_reads = rig.requests("OrderBook")
    [result] = batch.run_batch(rig.run, [batch.BatchItem(_one("nse-b"), after_settle=nudge)])
    assert result.verdict == batch.PASS and seen == ["OPEN"]
    [order] = rig.broker.orders
    assert order.price == Decimal("359.95") and order.status == "CANCELED"
    assert rig.records()[-1]["notes"] == {"modify": calls.ACKNOWLEDGED}
    assert rig.requests("ModifyOrder") == 1
    assert rig.requests("OrderBook") - book_reads <= 5  # batch polls only: no read per modify


# -- the stream ------------------------------------------------------------------------------------


def test_order_updates_from_the_stream_are_recorded_and_cross_checked(rig: Rig) -> None:
    import time as real_time

    from tests.live_orders.uat_matrix import views
    from tradesmart import TradeSmartStream

    watcher = views.OrderWatcher()
    stream = TradeSmartStream.from_client(
        rig.session.client, on_order_update=watcher.on_order_update
    )
    stream.start(timeout=5)
    stream.subscribe_orders()
    try:
        real_time.sleep(0.2)
        rig.run.watcher = watcher
        [result] = batch.run_batch(rig.run, [_one("nse-b")])
        deadline = real_time.monotonic() + 5
        while watcher.latest_status(result.order_ids[0]) != "CANCELED":
            assert real_time.monotonic() < deadline, watcher.updates(result.order_ids[0])
            real_time.sleep(0.05)
    finally:
        stream.stop()
    record = rig.records()[-1]
    assert "stream" in record["orders"][result.order_ids[0]]


# == Fix round 1: review regressions ==============================================================


def _reopen(rig: Rig) -> batch.MatrixRun:
    """The same run resumed by a new process: a new ledger, guard and run on the same folder."""
    t = FakeTime()
    run_dir = rig.session.run_dir
    session = gate.MatrixSession(
        client=rig.session.client,
        run_id=RUN_ID,
        trading_day=TODAY,
        run_dir=run_dir,
        ledger=Ledger(run_dir / LEDGER_NAME, RUN_ID, forbidden=["UAT01"]),
        guard=Guard(rig.root / "STOP", clock=t.clock),
        resumed=True,
        sleep=t.sleep,
        clock=t.clock,
    )
    return batch.open_run(
        session,
        contracts=rig.market.contracts(TODAY),
        profile=Profile(None, None, None),
        now=lambda: MORNING,
    )


def _spy_rejections(rig: Rig) -> list[tuple[str, bool]]:
    seen: list[tuple[str, bool]] = []
    original = rig.session.guard.note_rejection

    def spy(segment: str, *, expected: bool) -> None:
        seen.append((segment, expected))
        original(segment, expected=expected)

    rig.session.guard.note_rejection = spy  # type: ignore[method-assign]
    return seen


def _client_rate_limit() -> RateLimitError:
    return RateLimitError(
        "client-side rate limit for place_order", endpoint="PlaceOrder", http_status=None
    )


def _ledger_verdicts(rig: Rig) -> list[dict[str, Any]]:
    return [line for line in rig.ledger_lines() if line["type"] == "verdict"]


# -- C1: Ctrl-C mid-round --------------------------------------------------------------------------


@pytest.mark.parametrize("workers", [1, 4])
def test_ctrl_c_mid_round_stops_placing_and_sends_nothing_more(rig: Rig, workers: int) -> None:
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("SIGINT reaches the main thread only")
    if not hasattr(signal, "pthread_kill"):
        pytest.skip("no signal.pthread_kill on this OS (Windows): Ctrl+C cannot be simulated here")
    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("SIGINT is handled elsewhere here: it would not raise KeyboardInterrupt")
    grid = [c for c in _grid() if c.outcome in (cases.REST, cases.TRIGGER_PENDING)][:6]
    sent: list[int] = []

    def on_place(kwargs: dict[str, Any]) -> None:
        sent.append(1)
        if len(sent) == 1:
            signal.pthread_kill(threading.main_thread().ident or 0, signal.SIGINT)
            time.sleep(0.3)  # the main thread takes the interrupt while this place is in flight

    rig.market.on_place = on_place
    rig.run.workers = workers
    with pytest.raises(KeyboardInterrupt):
        batch.run_batch(rig.run, grid)
    after = rig.requests("PlaceOrder")
    time.sleep(0.5)
    assert rig.requests("PlaceOrder") == after <= workers  # only the in-flight places
    assert rig.session.interrupted and rig.session.guard.stopped()
    with pytest.raises(StopRun):
        rig.session.guard.check_send("nse")
    assert rig.requests("CancelOrder") == 0  # an interrupted run sends no more calls


# -- I1: worker exceptions -------------------------------------------------------------------------


def test_a_worker_exception_leaves_the_other_cases_cancelled_recorded_and_judged(
    rig: Rig,
) -> None:
    grid = [c for c in _grid() if c.outcome == cases.REST][:3]

    def bad(case: Case, contract: Any, snapshot: PriceSnapshot) -> dict[str, Any]:
        raise RuntimeError("plan bug")

    items = [
        batch.BatchItem(grid[0]),
        batch.BatchItem(grid[1], plan=bad),
        batch.BatchItem(grid[2]),
    ]
    rig.run.workers = 1
    results = batch.run_batch(rig.run, items)
    assert [r.verdict for r in results] == [batch.PASS, batch.FAIL, batch.PASS]
    assert "plan bug" in results[1].reason
    assert len(rig.statuses()) == 2 and all(s == "CANCELED" for s in rig.statuses().values())
    assert len(rig.records()) == 3


def test_an_engine_error_after_the_place_still_cancels_the_batch(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = batch._settle

    def broken_settle(run: batch.MatrixRun, works: Any) -> None:
        original(run, works)
        raise RuntimeError("engine bug")

    monkeypatch.setattr(batch, "_settle", broken_settle)
    with pytest.raises(RuntimeError, match="engine bug"):
        batch.run_batch(rig.run, [_one("nse-b"), _one("nfo-nifty-f")])
    assert len(rig.statuses()) == 2 and all(s == "CANCELED" for s in rig.statuses().values())


def test_a_session_error_from_the_quote_stops_the_run(rig: Rig) -> None:
    rig.broker.fail_next("GetQuotes", drills.session_expired())
    rig.run.workers = 1
    grid = [c for c in _grid() if c.outcome == cases.REST][:3]
    results = batch.run_batch(rig.run, grid)
    assert results[0].verdict == batch.FAIL and "Authentication" in results[0].reason
    assert rig.session.guard.stopped()
    assert [r.verdict for r in results[1:]] == [batch.SKIP, batch.SKIP]
    assert rig.requests("PlaceOrder") == 0


# -- I2 / I3: the closing leg and stuck fills ------------------------------------------------------


def test_a_closing_sell_rejected_is_a_failure_counted_for_the_breaker(rig: Rig) -> None:
    def on_place(kwargs: dict[str, Any]) -> None:
        if str(kwargs["transaction_type"]).upper() == "SELL":
            rig.broker.fail_next("PlaceOrder", drills.reject("Rejected : ORA:unexpected thing"))

    rig.market.on_place = on_place
    seen = _spy_rejections(rig)
    [result] = batch.run_batch(rig.run, [_fill_case()])
    assert result.verdict == batch.FAIL and "unexpected thing" in result.reason
    assert result.issue != "closing-sell"
    assert seen[-1] == ("nfo", False)
    assert rig.run.positions.leftovers() == {"NIFTYFUT": 65}


def test_a_closing_sell_not_sent_is_a_skip_and_keeps_the_position(rig: Rig) -> None:
    def on_place(kwargs: dict[str, Any]) -> None:
        if str(kwargs["transaction_type"]).upper() == "BUY":
            rig.session.guard.trip("nfo", "another instrument's order stuck")

    rig.market.on_place = on_place
    [result] = batch.run_batch(rig.run, [_fill_case()])
    assert result.verdict == batch.SKIP and result.reason.startswith("close not sent:")
    assert "circuit breaker" in result.reason
    assert rig.run.positions.leftovers() == {"NIFTYFUT": 65}
    assert rig.requests("PlaceOrder") == 1


def test_a_stuck_fill_is_stuck_and_fails(rig: Rig) -> None:
    rig.market.stuck.add("NIFTYFUT")
    [result] = batch.run_batch(rig.run, [_fill_case()])
    assert result.verdict == batch.STUCK and "cancelled" not in result.reason
    assert rig.session.guard.tripped("nfo") is not None
    with pytest.raises(pytest.fail.Exception):
        batch.report(result)


# -- I4: family hooks that fail --------------------------------------------------------------------


def test_a_hook_error_fails_the_case(rig: Rig) -> None:
    def hook(ctx: batch.Placed) -> None:
        raise AssertionError("family check failed")

    [result] = batch.run_batch(rig.run, [batch.BatchItem(_one("nse-b"), after_settle=hook)])
    assert result.verdict == batch.FAIL and "family check failed" in result.reason
    assert rig.statuses() and all(s == "CANCELED" for s in rig.statuses().values())


def test_a_hook_refusal_skips_the_case(rig: Rig) -> None:
    def hook(ctx: batch.Placed) -> None:
        raise Refusal("no room for this variant")

    [result] = batch.run_batch(rig.run, [batch.BatchItem(_one("nse-b"), after_settle=hook)])
    assert result.verdict == batch.SKIP and "no room for this variant" in result.reason
    assert _ledger_verdicts(rig)  # it sent an order: a resume does not place it again


# -- I5: a stopped round keeps what it knows -------------------------------------------------------


def test_a_stop_with_a_check_place_is_check_the_account(rig: Rig) -> None:
    grid = [c for c in _grid() if c.outcome == cases.REST][:2]
    sent: list[int] = []

    def on_place(kwargs: dict[str, Any]) -> None:
        sent.append(1)
        if len(sent) == 1:
            rig.broker.fail_next("PlaceOrder", drills.connect_error())
            rig.stop_file().write_text("stop", encoding="utf-8")

    rig.market.on_place = on_place
    rig.run.workers = 1
    results = batch.run_batch(rig.run, grid)
    assert results[0].verdict == batch.CHECK
    assert results[1].verdict == batch.SKIP and results[1].reason == STOP_REASON


def test_a_stop_keeps_an_rms_rejection_and_says_cancelled_only_when_confirmed(rig: Rig) -> None:
    rig.market.rms.add("NIFTYFUT")
    rig.market.stuck.add("NIFTYC26000")
    sent: list[str] = []

    def on_place(kwargs: dict[str, Any]) -> None:
        sent.append(kwargs["tradingsymbol"])
        if len(sent) == 3:
            rig.stop_file().write_text("stop", encoding="utf-8")

    rig.market.on_place = on_place
    rig.run.workers = 1
    rms = _probe(_one("nfo-nifty-f"), "rms")  # its own family: the RMS limit stays there
    items = [rms, _one("nfo-nifty-ce-atm"), _one("nse-b"), _one("mcx-crudeoil-f")]
    results = batch.run_batch(rig.run, items)
    assert results[0].verdict == batch.ENV_LIMIT
    assert results[1].verdict == batch.STUCK and "cancelled" not in results[1].reason
    assert results[2].verdict == batch.SKIP and results[2].reason.endswith("; cancelled")
    assert results[3].verdict == batch.SKIP and results[3].reason == STOP_REASON


# -- concurrency: places that overlap --------------------------------------------------------------


def test_workers_place_in_parallel_and_keep_every_rule(tmp_path: Path) -> None:
    for rig in _rig(tmp_path, max_open=5):
        rig.market.rendezvous = 2  # the first two places meet outside the policy lock (no sleep)
        rig.run.workers = 4
        grid = [c for c in _grid() if c.outcome in (cases.REST, cases.TRIGGER_PENDING)][:7]
        grid += [c for c in _grid("nfo-nifty-f") if c.outcome == cases.REST][:3]
        results = batch.run_batch(rig.run, grid)
        assert [r.verdict for r in results] == [batch.PASS] * 10
        assert rig.market.max_overlap >= 2
        assert rig.market.max_open_seen <= 5
        assert rig.requests("PlaceOrder") == 10
        assert len(rig.market.cancels) == len(set(rig.market.cancels)) == 10


# -- resume: tripped segments stay closed ----------------------------------------------------------


def test_a_resumed_run_keeps_a_tripped_segment_closed(rig: Rig) -> None:
    rig.market.reject_all = "Rejected : ORA:something unexpected"
    grid = [c for c in _grid("nfo-nifty-f") if c.outcome == cases.REST][:3]
    batch.run_batch(rig.run, grid)
    assert rig.session.guard.tripped("nfo") is not None
    rig.market.reject_all = None
    resumed = _reopen(rig)
    assert resumed.session.guard.tripped("nfo") is not None
    calls_before = len(rig.broker.requests)
    [result] = batch.run_batch(resumed, [_one("nfo-nifty-f", "SL-LMT")])
    assert result.verdict == batch.SKIP and "circuit breaker" in result.reason
    sent = [r.endpoint for r in rig.broker.requests[calls_before:]]
    assert "PlaceOrder" not in sent and "GetQuotes" not in sent


def test_a_resumed_run_keeps_a_stuck_segment_closed(rig: Rig) -> None:
    rig.market.stuck.add("NIFTYFUT")
    batch.run_batch(rig.run, [_one("nfo-nifty-f")])
    resumed = _reopen(rig)
    assert resumed.session.guard.tripped("nfo") is not None
    [result] = batch.run_batch(resumed, [_one("nfo-nifty-ce-atm")])
    assert result.verdict == batch.SKIP and "circuit breaker" in result.reason


# -- M1: pacing and the client-side rate limit -----------------------------------------------------


def test_the_quote_comes_before_the_pacing_stamp_and_the_place(rig: Rig) -> None:
    events: list[str] = []
    quote = rig.session.client.quote
    place = rig.session.client.place_order

    class Recording(batch.Pacer):
        def wait(self) -> None:
            events.append("pace")
            super().wait()

    def quoted(*args: Any, **kwargs: Any) -> Any:
        events.append("quote")
        return quote(*args, **kwargs)

    def placed(**kwargs: Any) -> str:
        events.append("place")
        return place(**kwargs)

    rig.run.pacer = Recording()
    rig.session.client.quote = quoted  # type: ignore[method-assign]
    rig.session.client.place_order = placed  # type: ignore[method-assign]
    batch.run_batch(rig.run, [_one("nse-b")])
    assert events == ["quote", "pace", "place"]


def test_a_client_side_rate_limit_skips_and_a_resume_retries_it(rig: Rig) -> None:
    place = rig.session.client.place_order
    refused: list[int] = []

    def limited(**kwargs: Any) -> str:
        if not refused:
            refused.append(1)
            raise _client_rate_limit()
        return place(**kwargs)

    rig.session.client.place_order = limited  # type: ignore[method-assign]
    case = _one("nse-b")
    [result] = batch.run_batch(rig.run, [case])
    assert result.verdict == batch.SKIP and "rate limit" in result.reason
    assert _ledger_verdicts(rig) == []
    resumed = _reopen(rig)
    assert resumed.session.case_done(case.id) is None
    [again] = batch.run_batch(resumed, [case])
    assert again.verdict == batch.PASS


# -- M2: the fill path's client-side refusals ------------------------------------------------------


def test_a_fill_refused_client_side_is_not_counted_for_the_breaker(rig: Rig) -> None:
    def refused(**kwargs: Any) -> str:
        raise RequestValidationError("quantity must be a whole lot")

    rig.session.client.place_order = refused  # type: ignore[method-assign]
    seen = _spy_rejections(rig)
    [result] = batch.run_batch(rig.run, [_fill_case()])
    assert result.verdict == batch.FAIL and "nothing sent" in result.reason
    assert seen == [("nfo", True)]


def test_a_fill_hit_by_the_client_rate_limit_skips_without_a_verdict(rig: Rig) -> None:
    def limited(**kwargs: Any) -> str:
        raise _client_rate_limit()

    rig.session.client.place_order = limited  # type: ignore[method-assign]
    seen = _spy_rejections(rig)
    [result] = batch.run_batch(rig.run, [_fill_case()])
    assert result.verdict == batch.SKIP and "rate limit" in result.reason
    assert seen == []
    assert _ledger_verdicts(rig) == []


# -- M3: an accepted market order is a fill: cut-off and ceiling first -----------------------------


def test_market_cases_skip_at_1530_before_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADESMART_LIVE_ORDER_OUTSIDE_HOURS", "1")  # the cut-off, not the hours
    late = datetime(2026, 11, 13, 15, 30, tzinfo=IST)
    for rig in _rig(tmp_path, now=late):
        rig.market.market = "accept"
        [result] = batch.run_batch(rig.run, [_one("nse-b", "MKT")])
        assert result.verdict == batch.SKIP and "before 15:30" in result.reason
        assert rig.requests("PlaceOrder") == 0


def test_market_cases_skip_past_the_position_ceiling(rig: Rig) -> None:
    rig.market.market = "accept"
    rig.run.positions.add_fill("ITC-EQ", 2, lot_size=1, case_id="earlier", order_id="x")
    [result] = batch.run_batch(rig.run, [_one("nse-b", "MKT")])
    assert result.verdict == batch.SKIP and "ceiling" in result.reason
    assert rig.requests("PlaceOrder") == 0


# -- M5: the foreign-order check before each fill case ---------------------------------------------


def test_a_foreign_order_between_fill_cases_stops_the_next_one(rig: Rig) -> None:
    hand = rig.broker.client()
    sent: list[str] = []

    def on_place(kwargs: dict[str, Any]) -> None:
        sent.append(str(kwargs["transaction_type"]).upper())
        if sent == ["BUY", "SELL"]:  # the first case's close: then someone trades by hand
            foreign = hand.place_order(
                exchange="NSE", tradingsymbol="ITC-EQ", quantity=1, transaction_type="BUY",
                product="CNC", order_type="LMT", price="350.00",
            )  # fmt: skip
            rig.broker.accept(foreign)

    rig.market.on_place = on_place
    first, second = _fill_case(), _fill_case(product="MIS")
    results = batch.run_batch(rig.run, [first, second])
    assert results[0].verdict == batch.XFAIL
    assert results[1].verdict == batch.SKIP and "not in this run's ledger" in results[1].reason
    assert rig.requests("PlaceOrder") == 3  # the first case's BUY and SELL, and the foreign one
    hand.close()


# -- M6: a batch cancel that was never sent --------------------------------------------------------


def test_a_batch_cancel_not_sent_leaves_the_teardown_its_one_cancel(rig: Rig) -> None:
    cancel = rig.session.client.cancel_order
    refused: list[int] = []

    def once(order_id: str) -> str:
        if not refused:
            refused.append(1)
            raise RequestValidationError("not now")
        return cancel(order_id)

    rig.session.client.cancel_order = once  # type: ignore[method-assign]
    [result] = batch.run_batch(rig.run, [_one("nse-b")])
    cancel_call = next(c for c in result.record["calls"] if c["kind"] == "cancel")
    assert cancel_call["verdict"] == "not sent"
    assert rig.session.guard.tripped("nse") is None
    assert not any(line["type"] == "stuck" for line in rig.ledger_lines())
    report = calls.cancel_ours(rig.session)  # the teardown
    assert report.cancelled == result.order_ids
    assert all(s == "CANCELED" for s in rig.statuses().values())


# == Fix round 2 ===================================================================================


def _refuse_cancels(rig: Rig) -> None:
    def refused(order_id: str) -> str:
        raise RequestValidationError("not now")

    rig.session.client.cancel_order = refused  # type: ignore[method-assign]


# -- m2: a place intent left pending by a killed process ----------------------------------------


def test_a_place_intent_left_pending_by_a_killed_process_trips_its_segment(rig: Rig) -> None:
    ledger = rig.session.ledger
    tag = ledger.new_tag("killed-case")
    ledger.intent(tag, "killed-case", "place", segment="nfo", plan={})  # no outcome: killed
    resumed = _reopen(rig)
    assert resumed.session.guard.tripped("nfo") is not None
    [result] = batch.run_batch(resumed, [_one("nfo-nifty-f")])
    assert result.verdict == batch.SKIP and "circuit breaker" in result.reason
    assert rig.requests("PlaceOrder") == 0


# -- m3 / m4: a fill leg that is not final and not cancelled ---------------------------------------


def test_after_ctrl_c_a_fill_leg_sends_no_cancel(rig: Rig) -> None:
    rig.market.stuck.add("NIFTYFUT")

    def on_place(kwargs: dict[str, Any]) -> None:
        rig.session.interrupted = True  # Ctrl-C reached the main thread meanwhile

    rig.market.on_place = on_place
    [result] = batch.run_batch(rig.run, [_fill_case()])
    assert rig.market.cancels == [] and rig.requests("CancelOrder") == 0
    assert result.verdict == batch.CHECK and "cancelled" not in result.reason


def test_an_opening_leg_not_final_whose_cancel_was_not_sent_is_check(rig: Rig) -> None:
    rig.market.stuck.add("NIFTYFUT")  # the BUY stays PENDING
    _refuse_cancels(rig)
    [result] = batch.run_batch(rig.run, [_fill_case()])
    assert result.verdict == batch.CHECK and "cancelled" not in result.reason
    assert rig.session.guard.tripped("nfo") is None  # not stuck: its one cancel is still unused


def test_a_closing_leg_not_final_whose_cancel_was_not_sent_is_check(rig: Rig) -> None:
    _refuse_cancels(rig)  # the BUY fills (no cancel); the SELL rests and its cancel is refused
    [result] = batch.run_batch(rig.run, [_fill_case()])
    assert result.verdict == batch.CHECK and result.issue != "closing-sell"
    assert rig.run.positions.leftovers() == {"NIFTYFUT": 65}


# -- m5: STUCK before an error; a stopped round honours an error -----------------------------------


def test_a_stuck_order_is_stuck_even_when_the_case_also_raised(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any) -> None:
        raise RuntimeError("engine bug after the place")

    monkeypatch.setattr(batch, "_early_rms", boom)
    rig.market.stuck.add("NIFTYFUT")
    [result] = batch.run_batch(rig.run, [_one("nfo-nifty-f")])
    assert result.verdict == batch.STUCK


def test_a_stopped_round_keeps_an_error_after_the_place(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any) -> None:
        raise RuntimeError("engine bug after the place")

    def on_place(kwargs: dict[str, Any]) -> None:
        rig.stop_file().write_text("stop", encoding="utf-8")

    monkeypatch.setattr(batch, "_early_rms", boom)
    rig.market.on_place = on_place
    rig.run.workers = 1
    grid = [c for c in _grid() if c.outcome == cases.REST][:2]
    results = batch.run_batch(rig.run, grid)
    assert results[0].verdict == batch.FAIL and "engine bug" in results[0].reason
    assert results[1].verdict == batch.SKIP and results[1].reason == STOP_REASON


# -- m6: one failing cancel never drops the others -------------------------------------------------


def test_a_cancel_worker_exception_does_not_drop_the_other_cancels(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = calls.cancel
    raised: list[str] = []

    def flaky(session: Any, order_id: str, case: Any) -> Any:
        if not raised:
            raised.append(order_id)
            raise RuntimeError("cancel worker bug")
        return real(session, order_id, case)

    monkeypatch.setattr(calls, "cancel", flaky)
    rig.run.workers = 1
    results = batch.run_batch(rig.run, [_one("nse-b"), _one("nfo-nifty-f")])
    by_order = {r.order_ids[0]: r for r in results}
    failed = by_order[raised[0]]
    assert failed.verdict == batch.FAIL and "cancel worker bug" in failed.reason
    other = next(r for r in results if r is not failed)
    assert other.verdict == batch.PASS and rig.statuses()[other.order_ids[0]] == "CANCELED"


# -- m7: an exception after the place intent was written -------------------------------------------


def test_an_exception_after_the_place_intent_is_check_the_account(rig: Rig) -> None:
    ledger = rig.session.ledger
    original = ledger.outcome

    def broken(tag: str, order_id: Any, verdict: str, **kwargs: Any) -> None:
        if kwargs.get("kind") == "place":
            raise OSError("disk full while writing the outcome")
        original(tag, order_id, verdict, **kwargs)

    ledger.outcome = broken  # type: ignore[method-assign]
    [result] = batch.run_batch(rig.run, [_one("nse-b")])
    assert result.verdict == batch.CHECK and "raised before any call" not in result.reason
    assert rig.session.guard.tripped("nse") is not None


# -- UAT quotes report ls 1 for F&O (live smoke run, 2026-11-13) -----------------------------------


def test_on_uat_a_future_whose_quote_says_lot_1_is_placed_with_the_master_lot(rig: Rig) -> None:
    rig.session.uat = True
    rig.broker.set_quote("NFO", "500", ls="1")
    [result] = batch.run_batch(rig.run, [_one("nfo-nifty-f")])
    assert result.verdict == batch.PASS, result.reason
    [order] = rig.broker.orders
    assert order.quantity == 65
    [record] = rig.records()
    assert record["notes"]["lot_size_mismatch"] == (
        "quote ls 1, script-master lot 65 (UAT: the master lot is used)"
    )


def test_outside_uat_a_future_whose_quote_says_lot_1_is_still_refused(rig: Rig) -> None:
    rig.broker.set_quote("NFO", "500", ls="1")
    [result] = batch.run_batch(rig.run, [_one("nfo-nifty-f")])
    assert result.verdict == batch.SKIP and "lot size 1 in a fresh quote" in result.reason
    assert rig.requests("PlaceOrder") == 0
