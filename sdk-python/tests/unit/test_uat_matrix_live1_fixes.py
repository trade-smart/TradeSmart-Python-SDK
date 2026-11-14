"""Mocked unit tests for the fixes after the first live UAT smoke run of the matrix (2026-11-13).

- One order-call pacer per session (``order_pacer.OrderPacer``): every state-changing call of
  the matrix (place, sliced place weighted by its slices, modify, cancel, exit, the GTT writes)
  stays at most 6 in any rolling second (TradeSmart's ALGO_CHK counts them all against 10/s).
- A quote read refused for rate (``exceeds Limit``) is read again (read-only) after 0.5 s, 1 s,
  2 s; any other error is unchanged.
- On UAT the F&O / MCX lot comes from the script master when ``quote().ls`` disagrees (UAT
  quotes report ``ls`` 1), with the mismatch recorded.
- The UAT matrix fill cut-off is 15:30 IST.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.live_orders.safety import Refusal
from tests.live_orders.uat_matrix import (
    batch,
    calls,
    calls_async,
    cases_views,
    contracts,
    gate,
    pricing,
)
from tests.live_orders.uat_matrix.calls import CaseRef
from tests.live_orders.uat_matrix.contracts import Contract
from tests.live_orders.uat_matrix.guard import StopRun
from tests.live_orders.uat_matrix.order_pacer import ORDER_CALLS_PER_SECOND, OrderPacer
from tests.unit.test_uat_matrix_calls_async import AsyncFacade
from tests.unit.test_uat_matrix_gtt import PLAN as GTT_PLAN
from tests.unit.test_uat_matrix_gtt import GttClient, _gtt, _modify_fields
from tests.unit.test_uat_matrix_safety import (
    MATRIX_ENV,
    PLAN,
    FakeTime,
    MatrixClient,
    _lines,
    _modify_kwargs,
    _order,
    _session,
)
from tradesmart import APIError, SlicedOrderResult
from tradesmart.models.base import IST
from tradesmart.models.market import Quote

NSE = CaseRef("m1-nse-itc-cnc-lmt-day-buy", "nse")
WRITES = frozenset(
    {
        "place_order",
        "place_sliced_order",
        "modify_order",
        "cancel_order",
        "exit_order",
        "place_gtt",
        "modify_gtt",
        "cancel_gtt",
    }
)
RATE = (
    "GetQuotes failed: Invalid Input :  Order Recieved 22 in a current second exceeds Limit 10 "
    "for user"
)


def _max_in_any_second(stamps: list[float]) -> int:
    ordered = sorted(stamps)
    return max(
        (sum(1 for s in ordered if start <= s < start + 1.0 - 1e-9) for start in ordered),
        default=0,
    )


# -- 1. the order-call pacer ---------------------------------------------------------------------


def test_the_pacer_allows_six_calls_in_any_rolling_second() -> None:
    t = FakeTime()
    pacer = OrderPacer(clock=t.clock, sleep=t.sleep)
    stamps: list[float] = []
    for _ in range(30):
        pacer.wait()
        stamps.append(t.clock())
    assert ORDER_CALLS_PER_SECOND == 6
    assert _max_in_any_second(stamps) == 6
    assert stamps[5] == 0.0  # the first six go out at once


def test_the_pacer_counts_a_weighted_call_as_that_many_calls() -> None:
    t = FakeTime()
    pacer = OrderPacer(clock=t.clock, sleep=t.sleep)
    for _ in range(4):
        pacer.wait()
    pacer.wait(3)  # 4 + 3 > 6: waits for the window to clear
    assert t.clock() >= 1.0


def test_the_async_pacer_sleeps_through_the_given_coroutine() -> None:
    t = FakeTime()
    pacer = OrderPacer(clock=t.clock, sleep=t.sleep)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        t.sleep(seconds)

    async def burst() -> list[float]:
        stamps = []
        for _ in range(12):
            await pacer.wait_async(1, fake_sleep)
            stamps.append(t.clock())
        return stamps

    stamps = asyncio.run(burst())
    assert _max_in_any_second(stamps) == 6 and slept


def _paced_session(tmp_path: Path, client: MatrixClient) -> tuple[gate.MatrixSession, list[float]]:
    session = _session(client, tmp_path)
    stamps: list[float] = []

    def on_call(name: str, args: Any) -> None:
        if name in WRITES:
            stamps.append(session.clock())

    client.on_call = on_call
    session.guard.reserve(NSE.case_id, 6)
    return session, stamps


def test_places_modifies_and_cancels_share_one_window_of_six(tmp_path: Path) -> None:
    client = MatrixClient(place=[str(1001 + i) for i in range(6)])
    session, stamps = _paced_session(tmp_path, client)
    ids = [calls.place(session, PLAN, NSE).order_id for _ in range(6)]
    for order_id in ids[:4]:
        tag = session.ledger.tag_of(order_id)
        before = _order(order_id, "OPEN", tag)
        calls.modify(session, order_id, NSE, before=before, **_modify_kwargs(), price="361.00")
    for order_id in ids[4:]:
        calls.cancel(session, order_id, NSE)
    assert len(stamps) == 12
    assert _max_in_any_second(stamps) <= 6


def test_async_places_modifies_and_cancels_share_the_same_window(tmp_path: Path) -> None:
    client = MatrixClient(place=[str(1001 + i) for i in range(6)])
    session, stamps = _paced_session(tmp_path, client)
    aclient = AsyncFacade(client)

    async def go() -> None:
        ids = [(await calls_async.place(session, aclient, PLAN, NSE)).order_id for _ in range(6)]
        for order_id in ids[:4]:
            assert order_id is not None
            tag = session.ledger.tag_of(order_id)
            before = _order(order_id, "OPEN", tag)
            await calls_async.modify(
                session, aclient, order_id, NSE, before=before, **_modify_kwargs(), price="361.00"
            )
        for order_id in ids[4:]:
            assert order_id is not None
            await calls_async.cancel(session, aclient, order_id, NSE)

    asyncio.run(go())
    assert len(stamps) == 12
    assert _max_in_any_second(stamps) <= 6


class SpyPacer:
    def __init__(self) -> None:
        self.weights: list[int] = []

    def wait(self, n: int = 1) -> None:
        self.weights.append(n)


def test_exit_and_a_sliced_place_go_through_the_pacer_with_their_weight(tmp_path: Path) -> None:
    sliced = SlicedOrderResult(
        requested_quantity=3,
        slice_quantities=(1, 1, 1),
        order_ids=("2001", "2002", "2003"),
        placed_quantity=3,
        unsent_quantity=0,
        complete=True,
        error=None,
    )
    client = MatrixClient(sliced=sliced)
    session = _session(client, tmp_path)
    spy = SpyPacer()
    session.order_pacer = spy  # type: ignore[assignment]
    session.guard.reserve(NSE.case_id, 3)
    outcome = calls.place_sliced(session, PLAN, NSE, max_slices=3)
    assert spy.weights == [3]
    calls.exit(session, outcome.order_ids[0], "CO", NSE)
    assert spy.weights == [3, 1]


def test_the_gtt_writes_go_through_the_pacer(tmp_path: Path) -> None:
    client = GttClient()
    client.gtt_place_results = ["9001"]
    session = _session(client, tmp_path)
    session.guard.reserve("gtt-nse-b-cnc-lmt-day-buy", 1)
    spy = SpyPacer()
    session.order_pacer = spy  # type: ignore[assignment]
    case = CaseRef("gtt-nse-b-cnc-lmt-day-buy", "nse")
    tag = calls.gtt_place(session, GTT_PLAN, case).tag
    calls.gtt_modify(session, "9001", case, before=_gtt("9001", tag), **_modify_fields())
    calls.gtt_cancel(session, "9001", case)
    assert spy.weights == [1, 1, 1]


def test_the_session_pacer_runs_on_the_sessions_clock(tmp_path: Path) -> None:
    session = _session(MatrixClient(), tmp_path)
    assert isinstance(session.order_pacer, OrderPacer)
    assert session.order_pacer.clock == session.clock
    assert session.order_pacer.sleep == session.sleep


# -- 2. a quote read refused for rate is read again ---------------------------------------------


def _quote(ls: str = "1", lp: str = "24000.00") -> Quote:
    return Quote.model_validate(
        {"stat": "Ok", "exch": "NFO", "tsym": "NIFTY25NOVFUT", "lp": lp, "ti": "0.10", "ls": ls}
    )


def _contract(lot: int = 65) -> Contract:
    return Contract(
        role="nfo-nifty-f",
        exchange="NFO",
        tradingsymbol="NIFTY25NOVFUT",
        token="111",
        symbol="NIFTY",
        kind="fut",
        lot_size=lot,
        tick_size=Decimal("0.10"),
        resolved_last_price=Decimal("24000.00"),
    )


def _rate_error(message: str = RATE) -> APIError:
    return APIError(message, endpoint="GetQuotes", http_status=200, emsg=message)


class QuoteClient:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.reads = 0

    def quote(self, exchange: str, token: str) -> Quote:
        self.reads += 1
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, Quote)
        return answer

    def instruments(self, exchange: str) -> list[dict[str, str]]:
        return []


def test_a_rate_refused_quote_is_read_again_after_a_short_wait() -> None:
    slept: list[float] = []
    client = QuoteClient(_rate_error(), _rate_error(), _quote(ls="65"))
    snap = pricing.fresh_quote(client, _contract(), sleep=slept.append)
    assert snap.last_price == Decimal("24000.00")
    assert slept == [0.5, 1.0] and client.reads == 3


def test_a_quote_refused_for_rate_four_times_is_a_refusal_after_three_retries() -> None:
    slept: list[float] = []
    client = QuoteClient(*[_rate_error() for _ in range(4)])
    with pytest.raises(Refusal, match="exceeds Limit"):
        pricing.fresh_quote(client, _contract(), sleep=slept.append)
    assert slept == [0.5, 1.0, 2.0] and client.reads == 4


def test_any_other_quote_error_is_not_read_again() -> None:
    slept: list[float] = []
    client = QuoteClient(_rate_error("GetQuotes failed: Invalid Token"))
    with pytest.raises(Refusal, match="Invalid Token"):
        pricing.fresh_quote(client, _contract(), sleep=slept.append)
    assert slept == [] and client.reads == 1


def test_contract_resolution_reads_a_rate_refused_quote_again() -> None:
    slept: list[float] = []
    client = QuoteClient(_rate_error(), _quote(ls="65"))
    market = contracts._Market(client, sleep=slept.append)
    quote = market.quote("NFO", "111")
    assert quote is not None and quote.last_price == Decimal("24000.00")
    assert slept == [0.5] and client.reads == 2


def test_contract_resolution_keeps_other_quote_errors_as_no_data() -> None:
    slept: list[float] = []
    client = QuoteClient(_rate_error("GetQuotes failed: no data"))
    assert contracts._Market(client, sleep=slept.append).quote("NFO", "111") is None
    assert slept == [] and client.reads == 1


# -- 3. on UAT the lot comes from the script master ----------------------------------------------


def test_on_uat_a_quote_lot_of_one_takes_the_master_lot_and_notes_it() -> None:
    snap = pricing.fresh_quote(QuoteClient(_quote(ls="1")), _contract(65), uat=True)
    assert snap.lot_mismatch == "quote ls 1, script-master lot 65 (UAT: the master lot is used)"


def test_outside_uat_a_lot_mismatch_still_refuses() -> None:
    with pytest.raises(Refusal, match="lot size 1 in a fresh quote, 65 at resolution"):
        pricing.fresh_quote(QuoteClient(_quote(ls="1")), _contract(65))


def test_a_matching_lot_notes_nothing_on_uat() -> None:
    snap = pricing.fresh_quote(QuoteClient(_quote(ls="65")), _contract(65), uat=True)
    assert snap.lot_mismatch is None


def _row(lot: str = "65") -> dict[str, str]:
    return {
        "Exchange": "NFO",
        "Token": "111",
        "LotSize": lot,
        "Symbol": "NIFTY",
        "TradingSymbol": "NIFTY25NOVFUT",
        "Expiry": "25-NOV-2025",
        "Instrument": "FUTIDX",
        "OptionType": "XX",
        "StrikePrice": "0",
        "TickSize": "0.10",
    }


def test_resolution_on_uat_takes_the_master_lot_and_keeps_the_quote_ls() -> None:
    built = contracts._build("nfo-nifty-f", "NFO", "fut", _row(), _quote(ls="1"), uat=True)
    assert isinstance(built, Contract)
    assert built.lot_size == 65 and built.quote_lot_size == 1
    assert Contract.from_dict(built.to_dict()) == built


def test_resolution_outside_uat_still_refuses_a_lot_mismatch() -> None:
    built = contracts._build("nfo-nifty-f", "NFO", "fut", _row(), _quote(ls="1"))
    assert isinstance(built, contracts.Unavailable)
    assert built.kind == contracts.LOT_SIZE_MISMATCH


def test_resolution_on_uat_still_refuses_a_master_row_without_a_lot() -> None:
    built = contracts._build("nfo-nifty-f", "NFO", "fut", _row(lot=""), _quote(ls="1"), uat=True)
    assert isinstance(built, contracts.Unavailable)
    assert built.kind == contracts.INCOMPLETE_ROW


def test_the_matrix_session_is_uat_from_the_environment_check(tmp_path: Path) -> None:
    session = _session(MatrixClient(), tmp_path, environ=dict(MATRIX_ENV))
    assert session.uat is True


def test_a_session_built_directly_is_not_uat_by_default(tmp_path: Path) -> None:
    session = _session(MatrixClient(), tmp_path)
    plain = gate.MatrixSession(
        client=session.client,
        run_id=session.run_id,
        trading_day=date(2026, 11, 13),
        run_dir=session.run_dir,
        ledger=session.ledger,
        guard=session.guard,
    )
    assert plain.uat is False


# -- 4. the UAT matrix fill cut-off is 15:30 IST -------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "minute", "allowed"), [(14, 30, True), (15, 14, True), (15, 29, True), (15, 30, False)]
)
def test_the_uat_matrix_fill_cutoff_is_half_past_three(
    hour: int, minute: int, allowed: bool
) -> None:
    now = datetime(2026, 11, 13, hour, minute, tzinfo=IST)
    assert (batch.fill_cutoff_reason(now) is None) is allowed
    assert batch.FILL_CUTOFF.hour == 15 and batch.FILL_CUTOFF.minute == 30


def test_the_async_views_quote_read_retries_a_rate_refusal(tmp_path: Path) -> None:
    session = _session(MatrixClient(), tmp_path)
    sync = QuoteClient(_rate_error(), _rate_error(), _quote(ls="65"))

    class AsyncQuotes:
        async def quote(self, exchange: str, token: str) -> Quote:
            return sync.quote(exchange, token)

    a = SimpleNamespace(run=SimpleNamespace(session=session), client=AsyncQuotes())
    start = session.clock()
    quote = asyncio.run(cases_views._read_quote(a, _contract()))  # type: ignore[arg-type]
    assert quote.last_price == Decimal("24000.00") and sync.reads == 3
    assert session.clock() - start == pytest.approx(1.5)  # 0.5 s + 1 s on the session's clock


# -- the STOP file and the breaker are checked again after the pacer wait ------------------------


class EventDuringWait:
    """A pacer whose wait lets ``event`` happen (another thread trips the breaker, or the STOP
    file appears) while the call is held, as a real wait of up to a second can."""

    def __init__(self, event: Any) -> None:
        self.event = event
        self.waits = 0

    def wait(self, n: int = 1) -> None:
        self.waits += 1
        self.event()

    async def wait_async(self, n: int, sleep: Any) -> None:
        self.wait(n)


def _trip(session: gate.MatrixSession) -> Any:
    return lambda: session.guard.trip("nse", "three unexpected rejections")


def _stop_file(session: gate.MatrixSession) -> Any:
    return lambda: session.guard.stop_file.write_text("", encoding="utf-8")


EVENTS = [(_trip, Refusal, "circuit breaker"), (_stop_file, StopRun, "STOP")]


def _intents(session: gate.MatrixSession) -> list[dict[str, Any]]:
    return [line for line in _lines(session) if line.get("type") == "intent"]


@pytest.mark.parametrize(("event", "error", "match"), EVENTS)
def test_a_place_held_by_the_pacer_is_not_sent_after_a_trip_or_stop(
    tmp_path: Path, event: Any, error: type[Exception], match: str
) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    session.order_pacer = EventDuringWait(event(session))  # type: ignore[assignment]
    with pytest.raises(error, match=match):
        calls.place(session, PLAN, NSE)
    assert client.count("place_order") == 0 and _intents(session) == []
    assert session.guard.pending == 0  # the MAX_OPEN slot is released


@pytest.mark.parametrize(("event", "error", "match"), EVENTS)
def test_a_sliced_place_held_by_the_pacer_is_not_sent_after_a_trip_or_stop(
    tmp_path: Path, event: Any, error: type[Exception], match: str
) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 3)
    session.order_pacer = EventDuringWait(event(session))  # type: ignore[assignment]
    with pytest.raises(error, match=match):
        calls.place_sliced(session, PLAN, NSE, max_slices=3)
    assert client.count("place_sliced_order") == 0 and _intents(session) == []
    assert session.guard.pending == 0


@pytest.mark.parametrize(("event", "error", "match"), EVENTS)
def test_a_modify_held_by_the_pacer_is_not_sent_after_a_trip_or_stop(
    tmp_path: Path, event: Any, error: type[Exception], match: str
) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    order_id = calls.place(session, PLAN, NSE).order_id
    assert order_id is not None
    before = _order(order_id, "OPEN", session.ledger.tag_of(order_id))
    intents = len(_intents(session))
    session.order_pacer = EventDuringWait(event(session))  # type: ignore[assignment]
    with pytest.raises(error, match=match):
        calls.modify(session, order_id, NSE, before=before, **_modify_kwargs(), price="361.00")
    assert client.count("modify_order") == 0 and len(_intents(session)) == intents


def test_a_cancel_and_an_exit_held_by_the_pacer_still_go_out_after_a_stop(tmp_path: Path) -> None:
    client = MatrixClient(place=["1001", "1002"])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 2)
    first = calls.place(session, PLAN, NSE).order_id
    second = calls.place(session, PLAN, NSE).order_id
    assert first is not None and second is not None
    session.order_pacer = EventDuringWait(_stop_file(session))  # type: ignore[assignment]
    session.guard.trip("nse", "tripped as well")
    assert calls.cancel(session, first, NSE).verdict == calls.ACKNOWLEDGED
    assert calls.exit(session, second, "CO", NSE).verdict == calls.ACKNOWLEDGED
    assert client.count("cancel_order") == 1 and client.count("exit_order") == 1


@pytest.mark.parametrize(("event", "error", "match"), EVENTS)
def test_gtt_writes_held_by_the_pacer_are_not_sent_after_a_trip_or_stop(
    tmp_path: Path, event: Any, error: type[Exception], match: str
) -> None:
    client = GttClient()
    client.gtt_place_results = ["9001"]
    session = _session(client, tmp_path)
    case = CaseRef("gtt-nse-b-cnc-lmt-day-buy", "nse")
    session.guard.reserve(case.case_id, 2)
    tag = calls.gtt_place(session, GTT_PLAN, case).tag
    session.order_pacer = EventDuringWait(event(session))  # type: ignore[assignment]
    intents = len(_intents(session))
    with pytest.raises(error, match=match):
        calls.gtt_modify(session, "9001", case, before=_gtt("9001", tag), **_modify_fields())
    with pytest.raises(error, match=match):
        calls.gtt_place(session, GTT_PLAN, case)
    assert client.count("modify_gtt") == 0 and client.count("place_gtt") == 1
    assert len(_intents(session)) == intents


@pytest.mark.parametrize(("event", "error", "match"), EVENTS)
def test_async_place_and_modify_held_by_the_pacer_are_not_sent_after_a_trip_or_stop(
    tmp_path: Path, event: Any, error: type[Exception], match: str
) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 2)
    aclient = AsyncFacade(client)
    order_id = calls.place(session, PLAN, NSE).order_id
    assert order_id is not None
    before = _order(order_id, "OPEN", session.ledger.tag_of(order_id))
    intents = len(_intents(session))
    session.order_pacer = EventDuringWait(event(session))  # type: ignore[assignment]

    async def modify() -> None:
        await calls_async.modify(
            session, aclient, order_id, NSE, before=before, **_modify_kwargs(), price="361.00"
        )

    with pytest.raises(error, match=match):
        asyncio.run(calls_async.place(session, aclient, PLAN, NSE))
    with pytest.raises(error, match=match):
        asyncio.run(modify())
    assert client.count("place_order") == 1 and client.count("modify_order") == 0
    assert len(_intents(session)) == intents and session.guard.pending == 0


def test_async_cancel_held_by_the_pacer_still_goes_out_after_a_stop(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    order_id = calls.place(session, PLAN, NSE).order_id
    assert order_id is not None
    session.order_pacer = EventDuringWait(_stop_file(session))  # type: ignore[assignment]
    outcome = asyncio.run(calls_async.cancel(session, AsyncFacade(client), order_id, NSE))
    assert outcome.verdict == calls.ACKNOWLEDGED and client.count("cancel_order") == 1
