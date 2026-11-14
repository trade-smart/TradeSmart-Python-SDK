"""Unit tests of the UAT matrix batch engine helpers (no network, no offline broker).

``tests/live_orders/uat_matrix/``: ``positions`` (the run's position ledger and its per-instrument
ceiling), ``record`` (one JSON line per case, account ids redacted), ``views`` (book / history /
trades / stream cross-checks and the order-update collector) and the pure parts of ``batch``
(pacing, plans priced from a fresh snapshot only, the 15:30 IST fill cut-off, re-resolving
price-related unavailable roles on a resume, and the pytest outcome of a case result). The
batch runs themselves are in ``tests/integration/test_uat_matrix_offline_broker.py``.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.live_orders.safety import Refusal
from tests.live_orders.uat_matrix import batch, cases, contracts, gate, positions, record, views
from tests.live_orders.uat_matrix.cases import Case
from tests.live_orders.uat_matrix.contracts import Contract, Contracts, Unavailable
from tests.live_orders.uat_matrix.ledger import Ledger
from tests.live_orders.uat_matrix.pricing import PriceSnapshot
from tradesmart.models.base import IST
from tradesmart.models.market import Quote
from tradesmart.models.orders import Order, OrderHistoryEntry, Trade
from tradesmart.models.stream import OrderUpdate

TODAY = date(2026, 11, 13)


def _contract(role: str = "nse-b", **extra: Any) -> Contract:
    data: dict[str, Any] = {
        "role": role,
        "exchange": "NSE",
        "tradingsymbol": "ITC-EQ",
        "token": "1660",
        "symbol": "",
        "kind": "eq",
        "lot_size": 1,
        "tick_size": Decimal("0.05"),
        "resolved_last_price": Decimal("400"),
    }
    data.update(extra)
    return Contract(**data)


def _snapshot(
    last: str = "400", tick: str = "0.05", lc: str = "300", uc: str = "500"
) -> PriceSnapshot:
    return PriceSnapshot("ITC-EQ", Decimal(last), Decimal(tick), Decimal(lc), Decimal(uc))


def _case(**fields: Any) -> Case:
    data: dict[str, Any] = {
        "id": "shapes-nse-b-mis-lmt-day-buy",
        "family": "shapes",
        "role": "nse-b",
        "product": "MIS",
        "prctyp": "LMT",
        "ret": "DAY",
        "side": "BUY",
        "orders": 1,
        "outcome": cases.REST,
    }
    data.update(fields)
    return Case(**data)


# -- positions -------------------------------------------------------------------------------------


def test_a_fill_and_a_close_that_does_not_fill_leave_the_position(tmp_path: Path) -> None:
    book = positions.PositionLedger(tmp_path / "positions.jsonl")
    book.add_fill("NIFTYFUT", 65, lot_size=65, case_id="c1", order_id="1")
    book.close_attempt("NIFTYFUT", 0, case_id="c1", order_id="2")
    assert book.leftovers() == {"NIFTYFUT": 65}
    book.close_attempt("NIFTYFUT", 65, case_id="c1", order_id="3")
    assert book.leftovers() == {}


def test_a_short_fill_is_negative(tmp_path: Path) -> None:
    book = positions.PositionLedger(tmp_path / "positions.jsonl")
    book.add_fill("ITC-EQ", -1, lot_size=1, case_id="c", order_id="1")
    assert book.leftovers() == {"ITC-EQ": -1}
    book.close_attempt("ITC-EQ", 1, case_id="c", order_id="2")  # the cover BUY filled
    assert book.leftovers() == {}


def test_the_per_instrument_ceiling_is_two_lots(tmp_path: Path) -> None:
    book = positions.PositionLedger(tmp_path / "positions.jsonl")
    assert positions.CEILING_LOTS == 2
    assert book.room("NIFTYFUT", 65, lot_size=65)
    book.add_fill("NIFTYFUT", 65, lot_size=65, case_id="a", order_id="1")
    assert book.room("NIFTYFUT", 65, lot_size=65)
    book.add_fill("NIFTYFUT", 65, lot_size=65, case_id="b", order_id="2")
    assert not book.room("NIFTYFUT", 65, lot_size=65)
    assert book.room("NIFTYFUT", -195, lot_size=65)  # back to a short of 1 lot: within it
    assert not book.room("BANKNIFTYFUT", -45, lot_size=15)  # a short of 3 lots: past it
    assert book.room("ITC-EQ", 1, lot_size=1)  # per instrument
    with pytest.raises(Refusal, match="ceiling"):
        book.check_room("NIFTYFUT", 65, lot_size=65)


def test_the_position_ledger_is_replayed_on_resume_without_account_ids(tmp_path: Path) -> None:
    path = tmp_path / "positions.jsonl"
    book = positions.PositionLedger(path, forbidden=["TEST01"])
    book.add_fill("NIFTYFUT", 65, lot_size=65, case_id="c", order_id="1", note="TEST01 x")
    again = positions.PositionLedger(path)
    assert again.leftovers() == {"NIFTYFUT": 65}
    text = path.read_text(encoding="utf-8")
    assert "TEST01" not in text
    assert all(json.loads(line)["type"] in ("fill", "close") for line in text.splitlines())


def test_fills_on_one_instrument_go_one_at_a_time(tmp_path: Path) -> None:
    book = positions.PositionLedger(tmp_path / "positions.jsonl")
    inside: list[int] = []
    peak: list[int] = []

    def work() -> None:
        with book.one_at_a_time("NIFTYFUT"):
            inside.append(1)
            peak.append(len(inside))
            threading.Event().wait(0.01)
            inside.pop()

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max(peak) == 1


# -- record ----------------------------------------------------------------------------------------


def test_one_json_line_per_case_with_account_ids_redacted(tmp_path: Path) -> None:
    recorder = record.Recorder(tmp_path, run_id="1113abcd", forbidden=["TEST01"])
    recorder.write({"case_id": "a", "row": {"uid": "TEST01", "actid": "TEST01", "tsym": "ITC-EQ"}})
    recorder.write({"case_id": "b", "note": "account TEST01 said no", "client_id": "TEST01"})
    lines = recorder.read()
    assert [line["case_id"] for line in lines] == ["a", "b"]
    assert lines[0]["row"] == {"tsym": "ITC-EQ"} and lines[0]["run_id"] == "1113abcd"
    assert "client_id" not in lines[1] and lines[1]["note"] == "[redacted]"
    assert "TEST01" not in (tmp_path / record.RECORDS_NAME).read_text(encoding="utf-8")


def test_records_from_threads_never_interleave(tmp_path: Path) -> None:
    recorder = record.Recorder(tmp_path, run_id="r")
    threads = [
        threading.Thread(target=recorder.write, args=({"case_id": str(i), "pad": "x" * 5000},))
        for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(int(line["case_id"]) for line in recorder.read()) == list(range(20))


# -- views -----------------------------------------------------------------------------------------


def _row(**wire: str) -> Order:
    return Order.model_validate({"norenordno": "1", "remarks": "t", "qty": "1", **wire})


def test_book_and_stream_that_agree_have_no_mismatch() -> None:
    row = _row(status="OPEN", prc="360.00")
    update = OrderUpdate.model_validate(
        {"t": "om", "norenordno": "1", "status": "OPEN", "prc": "360.00", "qty": "1"}
    )
    assert views.cross_check(row, updates=[update]) == []


def test_a_stream_status_other_than_the_book_is_a_mismatch() -> None:
    row = _row(status="CANCELED")
    update = OrderUpdate.model_validate({"t": "om", "norenordno": "1", "status": "OPEN"})
    assert any("stream" in m and "CANCELED" in m for m in views.cross_check(row, updates=[update]))


def test_history_newest_first_must_match_the_book() -> None:
    row = _row(status="COMPLETE", fillshares="1")
    newest = OrderHistoryEntry.model_validate({"norenordno": "1", "status": "COMPLETE"})
    older = OrderHistoryEntry.model_validate({"norenordno": "1", "status": "OPEN"})
    assert views.cross_check(row, history=[newest, older]) == []
    assert views.cross_check(row, history=[older, newest])  # oldest first: a mismatch


def test_trades_must_add_up_to_the_filled_quantity() -> None:
    row = _row(status="COMPLETE", fillshares="65", qty="65")
    trade = Trade.model_validate({"norenordno": "1", "flqty": "40", "fillshares": "40"})
    other = Trade.model_validate({"norenordno": "2", "flqty": "25"})
    assert views.filled_by_trades([trade, other], "1") == 40
    assert any("trades" in m for m in views.cross_check(row, trades=[trade, other]))


def test_the_order_watcher_keeps_updates_per_order() -> None:
    watcher = views.OrderWatcher()
    watcher.on_order_update(
        OrderUpdate.model_validate({"t": "om", "norenordno": "1", "status": "OPEN"})
    )
    watcher.on_order_update(
        OrderUpdate.model_validate({"t": "om", "norenordno": "2", "status": "OPEN"})
    )
    watcher.on_order_update(
        OrderUpdate.model_validate({"t": "om", "norenordno": "1", "status": "CANCELED"})
    )
    assert [str(u.status) for u in watcher.updates("1")] == ["OPEN", "CANCELED"]
    assert watcher.latest_status("1") == "CANCELED" and watcher.latest_status("9") is None


# -- pacing ----------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_the_pacer_stays_under_the_sdk_order_limits() -> None:
    clock = Clock()
    pacer = batch.Pacer(clock=clock.clock, sleep=clock.sleep)
    stamps = []
    for _ in range(30):
        pacer.wait()
        stamps.append(clock.now)
    assert (batch.PACE_PER_SECOND, batch.ORDER_PLACES_PER_MINUTE) == (9, 180)
    assert all(sum(1 for s in stamps if start <= s < start + 1.0) <= 9 for start in stamps)


def test_the_pacer_keeps_the_minute_window() -> None:
    clock = Clock()
    pacer = batch.Pacer(per_second=1000, per_minute=5, clock=clock.clock, sleep=clock.sleep)
    for _ in range(6):
        pacer.wait()
    assert clock.now >= 60.0


def test_the_181st_place_in_a_minute_waits_for_the_first_to_expire() -> None:
    clock = Clock()
    pacer = batch.Pacer(clock=clock.clock, sleep=clock.sleep)
    for _ in range(180):
        pacer.wait()
    assert clock.now < 60.0  # 180 places at 9/s take 20 s
    pacer.wait()
    assert clock.now >= 60.0 - 1e-6  # the first stamp (t=0) expires at 60 s


def test_modifies_do_not_count_in_the_minute_window() -> None:
    clock = Clock()
    pacer = batch.Pacer(clock=clock.clock, sleep=clock.sleep)
    for _ in range(180):
        pacer.wait()
    start = clock.now
    for _ in range(50):
        pacer.wait(place=False)  # D3: only the per-second window
    assert clock.now - start < 10.0  # 50 at 9/s, no minute wait
    pacer.wait()  # a place still waits for the minute window
    assert clock.now >= 60.0 - 1e-6


def test_non_place_calls_still_hold_the_per_second_window() -> None:
    clock = Clock()
    pacer = batch.Pacer(clock=clock.clock, sleep=clock.sleep)
    stamps = []
    for _ in range(30):
        pacer.wait(place=False)
        stamps.append(clock.now)
    assert all(sum(1 for s in stamps if start <= s < start + 1.0) <= 9 for start in stamps)


# -- plans -----------------------------------------------------------------------------------------


def test_a_resting_buy_is_priced_far_below_from_the_snapshot() -> None:
    plan = batch.default_plan(_case(), _contract(), _snapshot())
    assert plan == {
        "exchange": "NSE",
        "tradingsymbol": "ITC-EQ",
        "quantity": 1,
        "transaction_type": "BUY",
        "product": "MIS",
        "order_type": "LMT",
        "validity": "DAY",
        "price": Decimal("360.00"),
    }
    assert "tag" not in plan


def test_a_resting_sell_is_priced_far_above() -> None:
    plan = batch.default_plan(_case(side="SELL"), _contract(), _snapshot())
    assert plan["price"] == Decimal("440.00")


def test_stop_orders_get_a_trigger_away_from_the_market() -> None:
    buy = batch.default_plan(_case(prctyp="SL-LMT"), _contract(), _snapshot())
    sell = batch.default_plan(_case(prctyp="SL-LMT", side="SELL"), _contract(), _snapshot())
    assert buy["trigger_price"] > Decimal("400") and buy["price"] >= buy["trigger_price"]
    assert sell["trigger_price"] < Decimal("400") and sell["price"] <= sell["trigger_price"]


def test_market_orders_carry_no_price_and_stop_market_a_trigger_only() -> None:
    market = batch.default_plan(_case(prctyp="MKT"), _contract(), _snapshot())
    stop = batch.default_plan(_case(prctyp="SL-MKT"), _contract(), _snapshot())
    assert "price" not in market and "trigger_price" not in market
    assert "price" not in stop and stop["trigger_price"] > Decimal("400")


def test_a_fill_case_is_priced_at_the_last_price() -> None:
    fill = _case(id="fills-x", family="fills", outcome=cases.FILL_CLOSE, orders=2)
    plan = batch.default_plan(fill, _contract(), _snapshot(last="401.03", tick="0.05"))
    assert plan["price"] == Decimal("401.05")  # rounded toward a fill
    assert batch.default_plan(fill, _contract(), _snapshot(), side="SELL")["price"] == Decimal(
        "400.00"
    )


def test_an_mcx_quantity_is_in_trade_units() -> None:
    crude = _contract(
        role="mcx-crudeoil-f", exchange="MCX", tradingsymbol="CRUDEOIL19NOV26", kind="fut",
        lot_size=100, tick_size=Decimal("1"), resolved_last_price=Decimal("9200"),
    )  # fmt: skip
    plan = batch.default_plan(_case(role="mcx-crudeoil-f", product="NRML"), crude, _snapshot())
    assert plan["quantity"] == 100


def test_a_plan_is_never_priced_from_a_contract() -> None:
    with pytest.raises(TypeError, match="fresh_quote"):
        batch.default_plan(_case(), _contract(), _contract())  # type: ignore[arg-type]


# -- the fill cut-off ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "minute", "allowed"), [(9, 20, True), (15, 29, True), (15, 30, False), (15, 45, False)]
)
def test_fill_cases_run_before_1530_ist(hour: int, minute: int, allowed: bool) -> None:
    now = datetime(2026, 11, 13, hour, minute, tzinfo=IST)
    assert (batch.fill_cutoff_reason(now) is None) is allowed


def test_the_fill_cut_off_reads_ist_whatever_the_clock_zone() -> None:
    utc_0855 = datetime(2026, 11, 13, 8, 55, tzinfo=IST) - timedelta(hours=5, minutes=30)
    assert batch.fill_cutoff_reason(utc_0855.astimezone(IST)) is None
    late = datetime(2026, 11, 13, 9, 30).replace(tzinfo=IST) + timedelta(hours=6)
    assert batch.fill_cutoff_reason(late) is not None


# -- resume: re-resolve roles without a last price -------------------------------------------------


class Market:
    """``instruments`` / ``quote`` stand-in (read-only calls only)."""

    def __init__(self, rows: dict[str, list[dict[str, str]]], quotes: dict[str, Quote]) -> None:
        self.rows = rows
        self.quotes = quotes
        self.calls: list[tuple[str, str]] = []

    def instruments(self, exchange: str) -> list[dict[str, str]]:
        self.calls.append(("instruments", exchange))
        return self.rows.get(exchange, [])

    def quote(self, exchange: str, token: str) -> Quote:
        self.calls.append(("quote", token))
        return self.quotes.get(token) or Quote.model_validate({"stat": "Ok", "lp": "0"})


def _all_unavailable(kind: str = contracts.NO_LAST_PRICE) -> Contracts:
    return Contracts(
        day=TODAY,
        unavailable={
            role: Unavailable(kind, f"{role}: before the open") for role in contracts.ROLES
        },
    )


def test_a_resume_resolves_again_the_roles_without_a_last_price(tmp_path: Path) -> None:
    saved = _all_unavailable()
    saved.contracts["nse-t"] = _contract(role="nse-t", tradingsymbol="YESBANK-EQ", token="11915")
    del saved.unavailable["nse-t"]
    saved.unavailable["mcx-gold-f"] = Unavailable(contracts.NOT_IN_MASTER, "not listed")
    rows = {"NSE": [{"Exchange": "NSE", "Token": "1660", "LotSize": "1", "TradingSymbol": "ITC-EQ",
                     "TickSize": "0.05", "Instrument": "EQ", "Symbol": "ITC"}]}  # fmt: skip
    quotes = {"1660": Quote.model_validate({"stat": "Ok", "lp": "401.00", "ti": "0.05", "ls": "1"})}
    market = Market(rows, quotes)
    fresh = batch.refresh_unavailable(market, saved, TODAY, run_dir=tmp_path)
    itc = fresh.get("nse-b")
    assert itc is not None and itc.tradingsymbol == "ITC-EQ"
    assert "nse-b" not in fresh.unavailable
    assert fresh.get("nse-t") == saved.get("nse-t")  # a resolved role is kept as it was
    assert fresh.unavailable["mcx-gold-f"].kind == contracts.NOT_IN_MASTER  # not price-related
    assert ("quote", "11915") not in market.calls  # never re-quoted
    written = json.loads((tmp_path / contracts.CONTRACTS_FILE).read_text(encoding="utf-8"))
    assert written["contracts"]["nse-b"]["tradingsymbol"] == "ITC-EQ"


def test_a_resume_with_nothing_price_related_makes_no_call(tmp_path: Path) -> None:
    saved = _all_unavailable(contracts.NOT_IN_MASTER)
    market = Market({}, {})
    assert batch.refresh_unavailable(market, saved, TODAY, run_dir=tmp_path) is saved
    assert market.calls == []


def test_a_role_still_without_a_last_price_stays_unavailable(tmp_path: Path) -> None:
    saved = _all_unavailable()
    rows = {"NSE": [{"Exchange": "NSE", "Token": "1660", "LotSize": "1", "TradingSymbol": "ITC-EQ",
                     "TickSize": "0.05", "Instrument": "EQ", "Symbol": "ITC"}]}  # fmt: skip
    fresh = batch.refresh_unavailable(Market(rows, {}), saved, TODAY, run_dir=tmp_path)
    assert fresh.unavailable["nse-b"].kind == contracts.NO_LAST_PRICE
    assert fresh.get("nse-b") is None


# -- the pytest outcome of a result ----------------------------------------------------------------


def _result(verdict: str, *, canary: bool = False, issue: str | None = None) -> batch.CaseResult:
    return batch.CaseResult(case_id="c", verdict=verdict, reason="why", canary=canary, issue=issue)


@pytest.mark.parametrize("verdict", [batch.PASS, batch.RECORD, batch.XPASS, batch.MARKET_FILL])
def test_passing_verdicts_report_nothing(verdict: str) -> None:
    batch.report(_result(verdict))


@pytest.mark.parametrize(
    ("verdict", "outcome"),
    [
        (batch.SKIP, pytest.skip.Exception),
        (batch.ENV_LIMIT, pytest.skip.Exception),
        (batch.XFAIL, pytest.fail.Exception),  # an xfail without the known issue
        (batch.FAIL, pytest.fail.Exception),
        (batch.CHECK, pytest.fail.Exception),
        (batch.STUCK, pytest.fail.Exception),
    ],
)
def test_other_verdicts_report_through_pytest(verdict: str, outcome: type[BaseException]) -> None:
    with pytest.raises(outcome, match="why") as info:
        batch.report(_result(verdict))
    if outcome is not pytest.xfail.Exception:  # an xfail is a kind of fail: not that one
        assert not isinstance(info.value, pytest.xfail.Exception)


def test_a_stuck_canary_is_the_known_issue_not_a_failure() -> None:
    with pytest.raises(pytest.skip.Exception, match="why"):
        batch.report(_result(batch.STUCK, canary=True))


def test_only_the_closing_sell_known_issue_xfails() -> None:
    with pytest.raises(pytest.xfail.Exception, match="why"):
        batch.report(_result(batch.XFAIL, issue="closing-sell"))
    with pytest.raises(pytest.fail.Exception, match="why") as info:
        batch.report(_result(batch.XFAIL, issue="F26"))
    assert not isinstance(info.value, pytest.xfail.Exception)


# -- compose: the matrix variables reach the containers --------------------------------------------


def test_compose_passes_the_matrix_variables_through() -> None:
    """Without them the full / stress tiers and a resume cannot run inside Docker. The bare
    ``- NAME`` form passes a variable only when it is set on the host."""
    compose = Path(__file__).resolve().parents[2] / "compose.yaml"
    lines = [line.strip() for line in compose.read_text(encoding="utf-8").splitlines()]
    for name in (gate.MATRIX_ENV, gate.RUN_ID_ENV):
        assert f"- {name}" in lines, name
        assert not any(line.startswith(f"- {name}=") for line in lines), name


# -- the ledger on resume: a place that was never sent ---------------------------------------------


def _place_record(ledger: Ledger, case_id: str, verdict: str, error: dict[str, Any]) -> None:
    tag = ledger.new_tag(case_id)
    call = ledger.intent(tag, case_id, "place", segment="nse", plan={})
    ledger.outcome(tag, None, verdict, kind="place", call=call, error=error)


def test_a_place_refused_by_the_client_rate_limit_is_retried_on_resume(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path, "1113abcd")
    limit = {
        "exception": "RateLimitError",
        "message": "client-side rate limit for place_order",
        "http_status": None,  # raised by the SDK itself: no server answer
    }
    _place_record(ledger, "a", "rejected", limit)
    _place_record(ledger, "b", "not sent", {"exception": "RequestValidationError"})
    server = {"exception": "OrderRejectedError", "message": "RMS: no", "emsg": "RMS: no"}
    _place_record(ledger, "c", "rejected", server)
    again = Ledger(path, "1113abcd")
    assert not again.unfinished("a") and not again.unfinished("b")  # nothing went out
    assert again.unfinished("c")  # the broker answered: never placed again without a verdict


# -- fix round 2: a partly sent or unknown place is never "unsent" ------------------------------


def _resumed_session(client: Any, tmp_path: Path, run_id: str) -> Any:
    from tests.unit.test_uat_matrix_safety import MATRIX_ENV, _session

    (tmp_path / run_id / gate.LOCK_NAME).unlink(missing_ok=True)  # the first process ended
    return _session(client, tmp_path, environ={**MATRIX_ENV, "TRADESMART_UAT_RUN_ID": run_id})


def _client_limit(http_status: int | None = None, text: str = "client-side rate limit") -> Any:
    from tradesmart.errors import RateLimitError

    return RateLimitError(f"{text} for orders", endpoint="PlaceOrder", http_status=http_status)


def test_a_partly_sent_sliced_order_is_never_placed_again_on_resume(tmp_path: Path) -> None:
    from tests.live_orders.uat_matrix import calls
    from tests.unit.test_uat_matrix_safety import NSE, PLAN, MatrixClient, _session
    from tradesmart._slicing import SliceProgress

    progress = SliceProgress(3, (1, 1, 1))
    progress.order_ids.append("1")  # slice 1 went out; slice 2 hit the SDK's own limiter
    session = _session(MatrixClient(sliced=progress.stopped(_client_limit())), tmp_path)
    session.guard.reserve(NSE.case_id, 3)
    outcome = calls.place_sliced(session, PLAN, NSE, max_slices=3, max_slice_quantity=1)
    assert outcome.order_ids == ("1",)
    assert not batch.outcome_unsent(outcome)
    again = MatrixClient()
    resumed = _resumed_session(again, tmp_path, session.run_id)
    assert resumed.ledger.unfinished(NSE.case_id)
    with pytest.raises(Refusal, match="without a verdict"):
        calls.place_sliced(resumed, PLAN, NSE, max_slices=3, max_slice_quantity=1)
    assert again.count("place_sliced_order") == 0 and again.count("place_order") == 0


def test_a_raised_client_rate_limit_means_nothing_was_sent(tmp_path: Path) -> None:
    from tests.live_orders.uat_matrix import calls
    from tests.unit.test_uat_matrix_safety import NSE, PLAN, MatrixClient, _session

    session = _session(MatrixClient(sliced=_client_limit()), tmp_path)  # raised directly
    session.guard.reserve(NSE.case_id, 2)
    outcome = calls.place_sliced(session, PLAN, NSE, max_slices=2, max_slice_quantity=1)
    # place_sliced_order raises the limiter's refusal only before the first slice (a refusal
    # part-way comes back in result.error), so a raised one is NOT_SENT, not CHECK THE ACCOUNT.
    assert outcome.verdict == calls.NOT_SENT
    assert batch.outcome_unsent(outcome)


def test_a_server_429_carrying_the_phrase_still_blocks(tmp_path: Path) -> None:
    from tests.live_orders.uat_matrix import calls
    from tests.unit.test_uat_matrix_safety import NSE, PLAN, MatrixClient, _session

    client = MatrixClient(place=[_client_limit(http_status=429)])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    outcome = calls.place(session, PLAN, NSE)
    assert outcome.verdict == calls.REJECTED and outcome.error is not None
    assert outcome.error["http_status"] == 429
    assert not batch.outcome_unsent(outcome)
    assert _resumed_session(MatrixClient(), tmp_path, session.run_id).ledger.unfinished(NSE.case_id)


def test_a_single_place_refused_by_the_client_limit_is_unsent(tmp_path: Path) -> None:
    from tests.live_orders.uat_matrix import calls
    from tests.unit.test_uat_matrix_safety import NSE, PLAN, MatrixClient, _session

    session = _session(MatrixClient(place=[_client_limit()]), tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    outcome = calls.place(session, PLAN, NSE)
    assert outcome.error is not None and outcome.error["http_status"] is None
    assert batch.outcome_unsent(outcome)
    assert not _resumed_session(MatrixClient(), tmp_path, session.run_id).ledger.unfinished(
        NSE.case_id
    )


@pytest.mark.parametrize(
    ("verdict", "kind", "order_id", "order_ids", "error", "unsent"),
    [
        ("not sent", "place", None, (), {}, True),
        ("not sent", "place_sliced", None, ("1",), {}, False),  # a slice is known: sent
        (
            "rejected",
            "place",
            None,
            (),
            {
                "exception": "RateLimitError",
                "message": "client-side rate limit",
                "http_status": None,
            },
            True,
        ),  # fmt: skip
        (
            "rejected",
            "place",
            "9",
            (),
            {
                "exception": "RateLimitError",
                "message": "client-side rate limit",
                "http_status": None,
            },
            False,
        ),  # fmt: skip
        (
            "rejected",
            "place",
            None,
            (),
            {"exception": "RateLimitError", "message": "client-side rate limit"},
            False,
        ),  # no http_status recorded  # fmt: skip
        (
            "rejected",
            "place",
            None,
            (),
            {
                "exception": "RateLimitError",
                "message": "client-side rate limit",
                "http_status": 429,
            },
            False,
        ),  # fmt: skip
        (
            "rejected",
            "modify",
            None,
            (),
            {
                "exception": "RateLimitError",
                "message": "client-side rate limit",
                "http_status": None,
            },
            False,
        ),  # fmt: skip
        (
            "CHECK THE ACCOUNT",
            "place",
            None,
            (),
            {
                "exception": "RateLimitError",
                "message": "client-side rate limit",
                "http_status": None,
            },
            False,
        ),  # fmt: skip
        ("interrupted", "place", None, (), {}, False),
    ],
)
def test_nothing_sent_needs_no_ids_no_unknown_and_no_server_answer(
    verdict: str,
    kind: str,
    order_id: str | None,
    order_ids: tuple[str, ...],
    error: dict[str, Any],
    unsent: bool,
) -> None:
    from tests.live_orders.uat_matrix.ledger import nothing_sent

    found = nothing_sent(verdict, error, kind=kind, order_id=order_id, order_ids=order_ids)
    assert found is unsent


def test_an_option_resting_order_is_priced_four_percent_from_the_market() -> None:
    option = _contract("nfo-nifty-pe-atm", kind="opt", exchange="NFO")
    buy = batch.default_plan(_case(role=option.role), option, _snapshot())
    sell = batch.default_plan(_case(role=option.role, side="SELL"), option, _snapshot())
    assert buy["price"] == Decimal("384.00") and sell["price"] == Decimal("416.00")
