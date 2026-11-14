"""Unit tests for the views family's additions to ``uat_matrix/views.py`` and ``record.py``.

``cross_check`` gains a field-level mode (``full=True``: trigger, fills, average price, type,
side, validity, product, tag, exchange order id, reject reason; history newest first; the trade
average) and a positions view (the day buy / sell quantity and amount of the order's product);
``views_record`` assembles one case's views (history, trades, positions delta, stream lag,
mismatches); ``RecordViews`` reads them for a case record; ``Recorder`` takes an ``enrich`` hook;
``record.calls_from_ledger`` pairs a case's intents and outcomes (the raw request and response
bodies) from the ledger. No network: data is built from wire dictionaries.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.live_orders.uat_matrix import record, views
from tests.live_orders.uat_matrix.ledger import Ledger
from tradesmart.models.orders import Order, OrderHistoryEntry, Trade
from tradesmart.models.portfolio import Position
from tradesmart.models.stream import OrderUpdate

ROW = {
    "norenordno": "1",
    "exch": "NSE",
    "tsym": "ITC-EQ",
    "status": "COMPLETE",
    "qty": "1",
    "prc": "400.00",
    "prd": "I",
    "trantype": "B",
    "prctyp": "LMT",
    "ret": "DAY",
    "remarks": "sdkuat-1113test-001-1",
    "exchordid": "111",
    "fillshares": "1",
    "avgprc": "400.00",
}


def _row(**changes: str) -> Order:
    return Order.model_validate({**ROW, **changes})


def _hist(norentm: str, **changes: str) -> OrderHistoryEntry:
    return OrderHistoryEntry.model_validate({**ROW, "norentm": norentm, **changes})


def _om(**changes: str) -> OrderUpdate:
    wire = {k: v for k, v in ROW.items() if k != "prd"}
    return OrderUpdate.model_validate({"t": "om", **wire, "pcode": "I", **changes})


def _trade(qty: str = "1", price: str = "400.00", order_id: str = "1") -> Trade:
    return Trade.model_validate(
        {
            "norenordno": order_id,
            "exch": "NSE",
            "tsym": "ITC-EQ",
            "prd": "I",
            "trantype": "B",
            "flqty": qty,
            "flprc": price,
            "fillshares": qty,
        }  # fmt: skip
    )


def _position(buy: str = "1", buy_amount: str = "400.00", sell: str = "0") -> Position:
    return Position.model_validate(
        {
            "exch": "NSE",
            "tsym": "ITC-EQ",
            "prd": "I",
            "daybuyqty": buy,
            "daysellqty": sell,
            "daybuyamt": buy_amount,
            "daysellamt": "0.00",
        }  # fmt: skip
    )


# -- cross_check: the field-level mode ---------------------------------------------------------


def test_full_cross_check_agrees_when_every_view_shows_the_book() -> None:
    history = [_hist("10:00:02 13-11-2026"), _hist("10:00:01 13-11-2026", status="OPEN")]
    mismatches = views.cross_check(
        _row(), history=history, updates=[_om()], trades=[_trade()], full=True
    )
    assert mismatches == []


def test_full_cross_check_names_each_field_that_differs() -> None:
    history = [_hist("10:00:02 13-11-2026", trgprc="1.00", remarks="other")]
    updates = [_om(ret="IOC", pcode="M", exchordid="999", rejreason="nope")]
    row = _row(rejreason="RMS: margin")
    mismatches = views.cross_check(row, history=history, updates=updates, full=True)
    text = " | ".join(mismatches)
    assert "history: tag" in text
    for field in ("validity", "product", "exchange_order_id", "reject_reason"):
        assert f"stream: {field}" in text, text
    assert views.cross_check(row, history=history, updates=updates) == []  # basic mode


def test_full_cross_check_wants_history_newest_first() -> None:
    oldest_first = [_hist("10:00:01 13-11-2026"), _hist("10:00:02 13-11-2026")]
    mismatches = views.cross_check(_row(), history=oldest_first, full=True)
    assert any("newest first" in m for m in mismatches)


def test_full_cross_check_compares_the_trade_average_with_the_book() -> None:
    trades = [_trade(price="401.00")]
    assert any("average" in m for m in views.cross_check(_row(), trades=trades, full=True))


def test_a_view_without_the_field_says_nothing_about_it() -> None:
    updates = [OrderUpdate.model_validate({"t": "om", "norenordno": "1", "status": "COMPLETE"})]
    assert views.cross_check(_row(), updates=updates, full=True) == []


# -- cross_check: the positions view -----------------------------------------------------------


def test_the_positions_delta_shows_the_orders_fill_on_its_own_side() -> None:
    delta = views.PositionDelta(buy_quantity=1, buy_amount=Decimal("400.00"))
    assert views.cross_check(_row(), trades=[_trade()], positions=delta) == []
    sold = _row(trantype="S")
    delta = views.PositionDelta(sell_quantity=1, sell_amount=Decimal("400.00"))
    assert views.cross_check(sold, positions=delta) == []


def test_a_positions_delta_that_misses_the_fill_is_a_mismatch() -> None:
    mismatches = views.cross_check(_row(), positions=views.PositionDelta())
    assert any("positions: day buy quantity 0" in m for m in mismatches), mismatches
    wrong = views.PositionDelta(buy_quantity=1, buy_amount=Decimal("390.00"))
    assert any("day buy amount" in m for m in views.cross_check(_row(), positions=wrong))


def test_an_unfilled_order_wants_no_position_change() -> None:
    row = _row(status="CANCELED", fillshares="0", avgprc="0.00")
    assert views.cross_check(row, positions=views.PositionDelta()) == []
    assert views.cross_check(row, positions=views.PositionDelta(buy_quantity=1))


def test_position_deltas_are_per_exchange_symbol_and_product() -> None:
    before = views.position_totals([_position(buy="2", buy_amount="800.00")])
    after = views.position_totals([_position(buy="3", buy_amount="1200.00")])
    key = views.position_key("NSE", "ITC-EQ", "MIS")
    assert key == views.position_key("nse", "ITC-EQ", "I")
    delta = views.position_delta(before, after, key)
    assert delta == views.PositionDelta(buy_quantity=1, buy_amount=Decimal("400.00"))
    assert views.position_delta({}, after, key).buy_quantity == 3
    assert views.position_delta(before, {}, views.position_key("NSE", "X", "CNC")) == (
        views.PositionDelta()
    )


# -- the stream lag ----------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_wait_for_status_returns_the_lag_or_none() -> None:
    watcher = views.OrderWatcher()
    t = _Clock()
    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        calls["n"] += 1
        t.sleep(seconds)
        if calls["n"] == 3:
            watcher.on_order_update(_om())

    lag = views.wait_for_status(
        watcher, "1", "COMPLETE", timeout=5.0, clock=t.clock, sleep=sleep, interval=0.5
    )
    assert lag == 1.5
    missing = views.wait_for_status(
        watcher, "2", "COMPLETE", timeout=2.0, clock=t.clock, sleep=t.sleep, interval=0.5
    )
    assert missing is None


def test_wait_for_update_wants_a_new_update_that_matches() -> None:
    watcher = views.OrderWatcher()
    watcher.on_order_update(_om(prc="400.00"))  # before the modify: not counted
    t = _Clock()
    arrivals = iter([_om(prc="399.00", status="OPEN"), _om(prc="399.95", status="OPEN")])

    def sleep(seconds: float) -> None:
        t.sleep(seconds)
        watcher.on_order_update(next(arrivals))

    def match(update: OrderUpdate) -> bool:
        return views.field_value(update, "price") == views.normalize("price", "399.95")

    lag, update = views.wait_for_update(
        watcher, "1", match, after=1, timeout=5.0, clock=t.clock, sleep=sleep, interval=1.0
    )
    assert lag == 2.0 and update is not None and update.price == Decimal("399.95")
    lag, update = views.wait_for_update(
        watcher, "1", match, after=3, timeout=2.0, clock=t.clock, sleep=t.sleep, interval=1.0
    )
    assert (lag, update) == (None, None)


def test_field_value_reads_the_product_of_an_order_update_from_pcode() -> None:
    assert views.field_value(_om(), "product") == "MIS"
    assert views.field_value(_row(), "product") == "MIS"
    assert views.normalize("order_type", "SL-LMT") == views.field_value(
        _row(prctyp="SL-LMT"), "order_type"
    )
    assert views.normalize("trigger_price", "") is None
    assert views.normalize("price", "27081.60") == views.normalize("price", Decimal("27081.6"))
    assert views.normalize("quantity", "65") == views.normalize("quantity", 65)


# -- views_record: one case's views ------------------------------------------------------------


def test_views_record_collects_every_view_and_the_mismatches() -> None:
    row = _row()
    history = {"1": [_hist("10:00:02 13-11-2026")]}
    updates = {"1": [_om()]}
    deltas = {views.position_key("NSE", "ITC-EQ", "MIS"): views.PositionDelta()}
    data = views.views_record(
        {"1": row}, history=history, trades=[_trade()], updates=updates, deltas=deltas,
        lags={"1": 0.25},
    )  # fmt: skip
    entry = data["orders"]["1"]
    assert entry["stream_lag"] == 0.25 and len(entry["history"]) == 1
    assert entry["trades"][0]["flqty"] == "1" and entry["stream_reports"] == [None]
    assert any("positions" in m for m in entry["mismatches"])  # the fill is not in the delta
    assert data["mismatches"] == [f"order 1: {m}" for m in entry["mismatches"]]
    assert data["positions"]["NSE|ITC-EQ|MIS"]["buy_quantity"] == 0


def test_views_record_without_a_stream_update_is_a_mismatch_when_one_is_wanted() -> None:
    data = views.views_record(
        {"1": _row()}, history={}, trades=[_trade()], updates={"1": []}, deltas={},
        lags={"1": None}, want_stream=True,
    )  # fmt: skip
    assert any("no update" in m for m in data["mismatches"])


# -- RecordViews: the reads for one case record ------------------------------------------------


class _Reader:
    def __init__(self) -> None:
        self.positions_rows = [_position(buy="0", buy_amount="0.00")]
        self.reads: list[str] = []

    def order_history(self, order_id: str) -> list[OrderHistoryEntry]:
        self.reads.append(f"history {order_id}")
        return [_hist("10:00:02 13-11-2026")]

    def trades(self) -> list[Trade]:
        self.reads.append("trades")
        return [_trade()]

    def positions(self) -> list[Position]:
        self.reads.append("positions")
        return self.positions_rows


def _case_record(ledger: Ledger | None = None) -> dict[str, Any]:
    return {
        "case_id": "views-nse-b-mis-lmt-day-buy-fill",
        "contract": {"exchange": "NSE", "tradingsymbol": "ITC-EQ"},
        "calls": [],
        "orders": {"1": {"settled": dict(ROW), "final": dict(ROW)}},
    }


def test_record_views_reads_history_trades_and_the_positions_delta(tmp_path: Path) -> None:
    reader = _Reader()
    watcher = views.OrderWatcher()
    watcher.on_order_update(_om())
    enrich = views.RecordViews(
        reader,
        tmp_path / "ledger.jsonl",
        watcher=watcher,
        stream_wait=0.0,  # type: ignore[arg-type]
    )
    assert reader.reads == ["positions"]  # the baseline
    reader.positions_rows = [_position(buy="1", buy_amount="400.00")]
    data = enrich(_case_record())
    assert data["views"]["mismatches"] == [], data["views"]
    assert data["views"]["positions"]["NSE|ITC-EQ|MIS"] == {
        "buy_quantity": 1, "buy_amount": "400.00", "sell_quantity": 0, "sell_amount": "0.00",
    }  # fmt: skip
    assert reader.reads == ["positions", "history 1", "trades", "positions"]
    again = enrich(_case_record())  # the baseline moved: no new fill since the last record
    assert any("positions" in m for m in again["views"]["mismatches"])


def test_record_views_adds_the_raw_calls_from_the_ledger(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", "1113test")
    call = ledger.intent("tagA", "case-a", "place", segment="nse", plan={"price": "1"})
    ledger.outcome("tagA", "1", "acknowledged", kind="place", call=call,
                   response={"http_status": 200, "body": {"stat": "Ok"}})  # fmt: skip
    reader = _Reader()
    enrich = views.RecordViews(reader, ledger.path, stream_wait=0.0)  # type: ignore[arg-type]
    case = _case_record()
    case["calls"] = [{"call": call, "kind": "place"}]
    data = enrich(case)
    [raw] = data["raw_calls"]
    assert raw["request"] == {"plan": {"price": "1"}}
    assert raw["response"] == {"http_status": 200, "body": {"stat": "Ok"}}
    assert raw["verdict"] == "acknowledged" and raw["kind"] == "place"


def test_record_views_never_breaks_on_a_case_that_sent_nothing(tmp_path: Path) -> None:
    enrich = views.RecordViews(_Reader(), tmp_path / "none.jsonl", stream_wait=0.0)  # type: ignore[arg-type]
    data = enrich({"case_id": "x", "orders": {}, "calls": [], "contract": None})
    assert data["views"]["mismatches"] == [] and data["raw_calls"] == []


# -- record.py ---------------------------------------------------------------------------------


def test_calls_from_ledger_pairs_intents_with_their_outcomes(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", "1113test")
    first = ledger.intent("tagA", "a", "place", segment="nse", plan={"qty": 1})
    ledger.outcome("tagA", "1", "acknowledged", kind="place", call=first)
    second = ledger.intent("tagA", "a", "cancel", segment="nse", order_id="1")
    other = ledger.intent("tagB", "b", "place", segment="nse", plan={})
    ledger.outcome("tagB", None, "rejected", kind="place", call=other)
    pairs = record.calls_from_ledger(ledger.path, [first, second])
    assert [p["call"] for p in pairs] == [first, second]
    assert pairs[0]["request"] == {"plan": {"qty": 1}} and pairs[0]["verdict"] == "acknowledged"
    assert pairs[1]["verdict"] is None  # no outcome line (yet)
    assert record.calls_from_ledger(tmp_path / "missing.jsonl", [first]) == []


def test_recorder_enrich_adds_keys_before_redaction(tmp_path: Path) -> None:
    def enrich(data: Mapping[str, Any]) -> dict[str, Any]:
        return {"views": {"note": f"{data['case_id']} by TEST01"}, "client_id": "TEST01"}

    recorder = record.Recorder(tmp_path, run_id="r", forbidden=["TEST01"], enrich=enrich)
    written = recorder.write({"case_id": "c"})
    assert written["views"] == {"note": "[redacted]"} and "client_id" not in written
    line = json.loads((tmp_path / record.RECORDS_NAME).read_text(encoding="utf-8"))
    assert line["views"] == {"note": "[redacted]"}


def test_recorder_enrich_errors_are_recorded_not_raised(tmp_path: Path) -> None:
    def broken(data: Mapping[str, Any]) -> dict[str, Any]:
        raise RuntimeError("book unreadable")

    recorder = record.Recorder(tmp_path, run_id="r")
    recorder.enrich = broken
    written = recorder.write({"case_id": "c"})
    assert written["views_error"] == "RuntimeError: book unreadable"


def test_recorder_without_enrich_writes_as_before(tmp_path: Path) -> None:
    recorder = record.Recorder(tmp_path, run_id="r")
    assert recorder.enrich is None
    written = recorder.write({"case_id": "c"})
    assert set(written) == {"case_id", "run_id", "at"}


def test_recorder_enrich_runs_one_case_at_a_time(tmp_path: Path) -> None:
    inside = {"now": 0, "most": 0}
    lock = threading.Lock()

    def slow(data: Mapping[str, Any]) -> dict[str, Any]:
        with lock:
            inside["now"] += 1
            inside["most"] = max(inside["most"], inside["now"])
        threading.Event().wait(0.01)
        with lock:
            inside["now"] -= 1
        return {}

    recorder = record.Recorder(tmp_path, run_id="r", enrich=slow)
    threads = [
        threading.Thread(target=recorder.write, args=({"case_id": str(i)},)) for i in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert inside["most"] == 1 and len(recorder.read()) == 6
