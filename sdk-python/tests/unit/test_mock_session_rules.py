"""The matrix rules in mock-session mode (``MatrixSession.mock``; offline, no network).

In a mock session (the production server during an exchange mock trading session) the matrix:

- has no market-hours check and no 15:30 fill cut-off (the owner starts it once mock trading
  opens), in every family and both MCX and NSE windows;
- keeps the per-instrument position ceiling;
- judges as production does: the UAT-only recording rules are off;
- sends no AMO and no GTT (both are stored past the session): the AMO and GTT families skip,
  and the never-resend wrappers refuse ``amo=True`` and every GTT write with no SDK call;
- checks the switch again before every place, sliced place and modify (sync and async): once
  the date rolls over the run stops, while cancels still go out (teardown clears ours).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.live_orders import mock_session
from tests.live_orders.safety import Refusal
from tests.live_orders.uat_matrix import (
    batch,
    calls,
    calls_async,
    cases,
    cases_gtt,
    cases_mcx,
    family,
    gate,
)
from tests.live_orders.uat_matrix.calls import CaseRef
from tests.live_orders.uat_matrix.guard import StopRun
from tests.live_orders.uat_matrix.positions import PositionLedger
from tests.unit.test_mock_session import MOCK_ENV, SAT_MORNING, SATURDAY, SUNDAY
from tests.unit.test_uat_matrix_safety import (
    PLAN,
    PRODUCTION_URL,
    FakeTime,
    MatrixClient,
    _modify_kwargs,
    _order,
)
from tradesmart.models.base import IST

SAT_11 = datetime(2026, 11, 14, 11, 0, tzinfo=IST)
SAT_16 = datetime(2026, 11, 14, 16, 0, tzinfo=IST)
NSE = CaseRef("m1-nse-b-cnc-lmt-day-buy", "nse")
NO_AMO_GTT = "AMO and GTT are not sent in a mock session: they are stored past it"
SHAPE = next(c for c in cases.CASES if c.family == "shapes" and c.segment == "nse")
MCX = next(c for c in cases.CASES if c.segment == "mcx")


def _mock(tmp_path: Path, client: MatrixClient | None = None) -> tuple[gate.MatrixSession, Any]:
    """A real mock-session ``MatrixSession`` on the production URL, its IST date Saturday."""
    client = client if client is not None else MatrixClient(base_url=PRODUCTION_URL)
    t = FakeTime()
    session = gate.matrix_session(
        MOCK_ENV,
        client,  # type: ignore[arg-type]
        root=tmp_path,
        now=SAT_MORNING,
        sleep=t.sleep,
        clock=t.clock,
    )
    session.today = lambda: SATURDAY
    assert session.mock and not session.uat
    return session, client


def _placed(session: gate.MatrixSession, client: MatrixClient, order_id: str = "1001") -> str:
    client.place_results = [order_id]
    case = CaseRef(f"setup-{order_id}", "nse")
    session.guard.reserve(case.case_id, 1)
    outcome = calls.place(session, PLAN, case)
    assert outcome.order_id == order_id
    return order_id


def _sdk_calls(client: MatrixClient) -> list[str]:
    return [name for name, _ in client.calls]


# -- the reason ---------------------------------------------------------------------------------


def test_the_reason_is_the_owners_words() -> None:
    assert mock_session.NO_AMO_GTT == NO_AMO_GTT


# -- hours: off in mock mode --------------------------------------------------------------------


@pytest.mark.parametrize("now", [SAT_11, SAT_16], ids=["sat-11:00", "sat-16:00"])
def test_no_hours_check_in_mock_mode(now: datetime) -> None:
    assert family.hours_refusal(now, mock=True) is None
    assert cases_mcx.hours_refusal(now, mock=True) is None
    assert batch.hours_refusal(SHAPE, now, mock=True) is None
    assert batch.hours_refusal(MCX, now, mock=True) is None


def test_hours_still_apply_outside_uat_and_mock_mode() -> None:
    assert family.hours_refusal(SAT_11) is not None
    assert cases_mcx.hours_refusal(SAT_11) is not None
    assert batch.hours_refusal(SHAPE, SAT_11) is not None


def test_no_fill_cut_off_in_mock_mode() -> None:
    assert batch.fill_cutoff_reason(SAT_16, mock=True) is None
    assert batch.fill_cutoff_reason(SAT_16) is not None  # outside UAT and mock: unchanged


def test_check_hours_reads_the_sessions_mock_flag() -> None:
    run = SimpleNamespace(now=lambda: SAT_16, session=SimpleNamespace(uat=False, mock=True))
    batch.check_hours(run, SHAPE)  # type: ignore[arg-type]
    plain = SimpleNamespace(now=lambda: SAT_16, session=SimpleNamespace(uat=False, mock=False))
    with pytest.raises(Refusal):
        batch.check_hours(plain, SHAPE)  # type: ignore[arg-type]


# -- the position ceiling: on in mock mode ------------------------------------------------------


def test_the_position_ceiling_applies_in_mock_mode(tmp_path: Path) -> None:
    positions = PositionLedger(tmp_path / "positions.jsonl")
    positions.add_fill("ITC-EQ", 2, lot_size=1, case_id="earlier", order_id=None)
    contract = SimpleNamespace(tradingsymbol="ITC-EQ", lot_size=1)
    mock_run = SimpleNamespace(session=SimpleNamespace(uat=False, mock=True), positions=positions)
    reason = batch.ceiling_refusal(mock_run, contract)  # type: ignore[arg-type]
    assert reason is not None and "ceiling" in reason
    plain = SimpleNamespace(session=SimpleNamespace(uat=False, mock=False), positions=positions)
    assert batch.ceiling_refusal(plain, contract) is None  # type: ignore[arg-type]


# -- UAT-only recording rules: off in mock mode --------------------------------------------------


def test_uat_recording_rules_are_off_in_mock_mode(tmp_path: Path) -> None:
    session, _ = _mock(tmp_path)
    assert session.uat_quote_pacer() is None  # no UAT quote pacing
    assert not batch.uat_stop_open(session.uat, SHAPE, "TRIGGER_PENDING", "OPEN")
    run = SimpleNamespace(session=session)
    work = SimpleNamespace(case=SHAPE)
    assert batch.note_uat_fill(run, work, 1) is False  # type: ignore[arg-type]


# -- GTT and AMO: refused in mock mode ------------------------------------------------------------


def test_the_gtt_write_rule_refuses_in_mock_mode() -> None:
    assert cases_gtt.write_refusal({}, SAT_11, mock=True) == NO_AMO_GTT


@pytest.mark.parametrize("marker", ["m_amo", "m_gtt"])
def test_the_amo_and_gtt_families_skip_at_collection_in_mock_mode(marker: str) -> None:
    assert gate.mock_family_refusal(MOCK_ENV, [marker, "smoke"]) == NO_AMO_GTT
    assert gate.mock_family_refusal(MOCK_ENV, ["m_shapes", "smoke"]) is None
    uat = {k: v for k, v in MOCK_ENV.items() if k not in (mock_session.SESSION_ENV,
                                                          mock_session.CONFIRM_ENV)}  # fmt: skip
    assert gate.mock_family_refusal(uat, [marker]) is None


def test_a_place_with_amo_is_refused_with_no_call(tmp_path: Path) -> None:
    session, client = _mock(tmp_path)
    before = _sdk_calls(client)
    session.guard.reserve(NSE.case_id, 1)
    with pytest.raises(Refusal, match=NO_AMO_GTT):
        calls.place(session, {**PLAN, "amo": True}, NSE)
    assert _sdk_calls(client) == before
    assert session.guard.open_count == 0 and session.guard.pending == 0
    assert not session.ledger.ours()


def test_a_place_without_amo_goes_out_in_mock_mode(tmp_path: Path) -> None:
    session, client = _mock(tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    outcome = calls.place(session, PLAN, NSE)
    assert outcome.order_id == "1001" and client.count("place_order") == 1


def test_a_sliced_place_with_amo_is_refused_with_no_call(tmp_path: Path) -> None:
    session, client = _mock(tmp_path)
    before = _sdk_calls(client)
    session.guard.reserve(NSE.case_id, 2)
    with pytest.raises(Refusal, match=NO_AMO_GTT):
        calls.place_sliced(session, {**PLAN, "amo": True}, NSE, max_slices=2)
    assert _sdk_calls(client) == before


def test_an_async_place_with_amo_is_refused_with_no_call(tmp_path: Path) -> None:
    from tests.unit.test_uat_matrix_calls_async import AsyncFacade

    session, client = _mock(tmp_path)
    before = _sdk_calls(client)
    session.guard.reserve(NSE.case_id, 1)
    with pytest.raises(Refusal, match=NO_AMO_GTT):
        asyncio.run(
            calls_async.place(session, AsyncFacade(client), {**PLAN, "amo": True}, NSE)  # type: ignore[arg-type]
        )
    assert _sdk_calls(client) == before


def test_every_gtt_write_is_refused_with_no_call(tmp_path: Path) -> None:
    session, client = _mock(tmp_path)
    before = _sdk_calls(client)
    plan = {"exchange": "NSE", "tradingsymbol": "ITC-EQ", "quantity": 1}
    session.guard.reserve(NSE.case_id, 1)
    with pytest.raises(Refusal, match=NO_AMO_GTT):
        calls.gtt_place(session, plan, NSE)
    with pytest.raises(Refusal, match=NO_AMO_GTT):
        calls.gtt_modify(session, "26111400001", NSE, trigger_value=Decimal("500"))
    with pytest.raises(Refusal, match=NO_AMO_GTT):
        calls.gtt_cancel(session, "26111400001", NSE)
    assert _sdk_calls(client) == before
    assert not session.ledger.gtt_tags()


# -- the date rolls over: no new place or modify, cancels still go out ----------------------------


def test_after_midnight_a_place_is_refused_and_the_run_stops(tmp_path: Path) -> None:
    session, client = _mock(tmp_path)
    session.today = lambda: SUNDAY
    session.guard.reserve(NSE.case_id, 1)
    with pytest.raises(StopRun) as raised:
        calls.place(session, PLAN, NSE)
    assert "today" in raised.value.reason
    assert client.count("place_order") == 0
    assert session.guard.stopped()


def test_after_midnight_a_sliced_place_is_refused(tmp_path: Path) -> None:
    session, client = _mock(tmp_path)
    session.today = lambda: SUNDAY
    session.guard.reserve(NSE.case_id, 2)
    with pytest.raises(StopRun):
        calls.place_sliced(session, PLAN, NSE, max_slices=2)
    assert client.count("place_sliced_order") == 0


def test_after_midnight_a_modify_is_refused_and_the_cancel_still_goes_out(tmp_path: Path) -> None:
    session, client = _mock(tmp_path)
    order_id = _placed(session, client)
    client.books = [[_order(order_id, "OPEN")], [_order(order_id, "CANCELED")]]
    session.today = lambda: SUNDAY
    reads = client.count("orders")
    with pytest.raises(StopRun):
        calls.modify(session, order_id, NSE, **_modify_kwargs())
    assert client.count("modify_order") == 0
    assert client.count("orders") == reads  # refused before the book read too
    outcome = calls.cancel(session, order_id, NSE)
    assert client.count("cancel_order") == 1 and outcome.sent


def test_after_midnight_async_place_and_modify_are_refused(tmp_path: Path) -> None:
    from tests.unit.test_uat_matrix_calls_async import AsyncFacade

    session, client = _mock(tmp_path)
    order_id = _placed(session, client)
    session.today = lambda: SUNDAY
    facade = AsyncFacade(client)
    other = CaseRef("m9-nse-b-mis-lmt-day-buy-open-async", "nse")
    session.guard.reserve(other.case_id, 1)
    with pytest.raises(StopRun):
        asyncio.run(calls_async.place(session, facade, PLAN, other))  # type: ignore[arg-type]
    with pytest.raises(StopRun):
        asyncio.run(
            calls_async.modify(session, facade, order_id, NSE, **_modify_kwargs())  # type: ignore[arg-type]
        )
    assert client.count("place_order") == 1 and client.count("modify_order") == 0


def test_a_stale_switch_date_refuses_even_on_the_same_weekend(tmp_path: Path) -> None:
    session, client = _mock(tmp_path)
    session.mock_date = date(2026, 11, 7)  # an earlier weekend's switch, still in the run
    session.guard.reserve(NSE.case_id, 1)
    with pytest.raises(StopRun):
        calls.place(session, PLAN, NSE)
    assert client.count("place_order") == 0


def test_outside_mock_mode_the_wrappers_ask_nothing_new(tmp_path: Path) -> None:
    from tests.unit.test_uat_matrix_safety import _session

    client = MatrixClient()
    session = _session(client, tmp_path)  # a UAT session
    session.today = lambda: SUNDAY  # never read outside mock mode
    session.guard.reserve(NSE.case_id, 1)
    assert calls.place(session, PLAN, NSE).order_id == "1001"


# -- compose: the switch reaches the container --------------------------------------------------


def test_compose_passes_the_mock_session_switch_through() -> None:
    """Without the pass-through the switch never reaches ``docker compose run`` (the run then
    fails closed: production without the switch is refused). The bare ``- NAME`` form passes a
    variable only when it is set on the host."""
    compose = Path(__file__).resolve().parents[2] / "compose.yaml"
    lines = [line.strip() for line in compose.read_text(encoding="utf-8").splitlines()]
    for name in (mock_session.SESSION_ENV, mock_session.CONFIRM_ENV):
        assert f"- {name}" in lines, name
        assert not any(line.startswith(f"- {name}=") for line in lines), name


def test_a_family_module_batch_runs_outside_hours_in_mock_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.live_orders import safety

    monkeypatch.setattr(safety, "market_closed_reason", lambda segment, now: "market is closed")
    ran: list[Any] = []
    monkeypatch.setattr(family, "run_for", lambda session: "run")
    monkeypatch.setattr(family, "selected_cases", lambda request: [])
    monkeypatch.setattr(family, "run_cases", lambda run, items: ran.append(items) or {})
    session: Any = SimpleNamespace(uat=False, mock=True)
    try:
        results = family.module_results(object(), session, lambda run, chosen: chosen)
    except pytest.skip.Exception as skipped:  # a skip here would hide the failure
        pytest.fail(f"skipped in mock mode: {skipped}")
    assert results == {}
    assert ran == [[]]
