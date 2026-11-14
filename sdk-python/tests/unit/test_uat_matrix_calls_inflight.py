"""``calls.py`` with a call in flight (Task 5 fix round 1, C1): an interruption during the
read-only lookup after an ambiguous outcome still writes the call's outcome (``interrupted``)
and flags the order, and no other call is made for an order or tag whose intent has no outcome
yet in this process (``Ledger.pending_order`` / ``pending_tag``)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.live_orders.safety import CHECK_THE_ACCOUNT, Refusal
from tests.live_orders.uat_matrix import calls, gate
from tests.live_orders.uat_matrix.calls import CaseRef
from tests.unit.test_uat_matrix_safety import (
    PLAN,
    MatrixClient,
    _modify_kwargs,
    _order,
    _session,
    _unknown,
)

NSE = CaseRef("inflight-nse", "nse")


def _lines(session: gate.MatrixSession) -> list[dict[str, Any]]:
    return [json.loads(x) for x in session.ledger.path.read_text(encoding="utf-8").splitlines()]


def _ctrl_c_on_sleep(session: gate.MatrixSession) -> None:
    def sleep(seconds: float) -> None:
        raise KeyboardInterrupt

    session.sleep = sleep


def _placed(session: gate.MatrixSession) -> str:
    session.guard.reserve("setup", 1)
    outcome = calls.place(session, PLAN, CaseRef("setup", "nse"))
    assert outcome.order_id == "1001"
    return outcome.tag


def test_ctrl_c_during_the_place_lookup_writes_the_outcome_and_flags_the_tag(
    tmp_path: Path,
) -> None:
    client = MatrixClient(place=[_unknown()])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    _ctrl_c_on_sleep(session)  # the first lookup read finds nothing; Ctrl-C while waiting
    with pytest.raises(KeyboardInterrupt):
        calls.place(session, PLAN, NSE)
    last = _lines(session)[-1]
    assert last["type"] == "outcome" and last["verdict"] == calls.INTERRUPTED
    assert session.ledger.tag_flagged(last["tag"]) and session.interrupted
    assert session.guard.pending == 0 and client.count("place_order") == 1


@pytest.mark.parametrize("kind", ["modify", "cancel"])
def test_ctrl_c_during_a_modify_or_cancel_lookup_flags_the_order(tmp_path: Path, kind: str) -> None:
    client = MatrixClient(modify=_unknown("ModifyOrder"), cancel=_unknown("CancelOrder"))
    session = _session(client, tmp_path)
    tag = _placed(session)
    row = _order("1001", "OPEN", tag=tag, prc="360.00", qty="1", prctyp="LMT", prd="C")
    client.books = [[row]]
    _ctrl_c_on_sleep(session)
    with pytest.raises(KeyboardInterrupt):
        if kind == "modify":
            calls.modify(
                session, "1001", NSE, before=row, price=Decimal("359.95"), **_modify_kwargs()
            )
        else:
            calls.cancel(session, "1001", NSE)
    last = _lines(session)[-1]
    assert last["type"] == "outcome" and last["verdict"] == calls.INTERRUPTED
    assert "1001" in session.ledger.flagged()


def test_cancel_ours_refuses_an_order_whose_place_is_in_flight(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    tag = session.ledger.new_tag(NSE.case_id)
    session.ledger.intent(tag, NSE.case_id, "place", segment="nse", plan={})  # no outcome yet
    assert session.ledger.pending_tag(tag)
    client.books = [[_order("9001", "OPEN", tag=tag)]]
    report = calls.cancel_ours(session)
    assert report.pending == ["9001"] and report.cancelled == []
    assert client.count("cancel_order") == 0


def test_no_call_for_an_order_with_a_call_in_flight(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    tag = _placed(session)
    call = session.ledger.intent(tag, NSE.case_id, "modify", segment="nse", order_id="1001")
    assert session.ledger.pending_order("1001")
    with pytest.raises(Refusal, match="in flight"):
        calls.cancel(session, "1001", NSE)
    client.books = [[_order("1001", "OPEN", tag=tag)]]
    assert calls.cancel_ours(session).pending == ["1001"]
    assert client.count("cancel_order") == 0
    session.ledger.outcome(tag, "1001", calls.ACKNOWLEDGED, kind="modify", call=call)
    assert not session.ledger.pending_order("1001") and not session.ledger.pending_tag(tag)
    assert calls.cancel(session, "1001", NSE).verdict == calls.ACKNOWLEDGED


# -- fix round 2: an ordinary exception after the intent ----------------------------------------


def _bug_before_finish(session: gate.MatrixSession) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("a bug after the call")

    session.responses.last = broken  # type: ignore[method-assign]


def test_an_exception_after_the_place_intent_is_one_check_outcome_and_flagged(
    tmp_path: Path,
) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    _bug_before_finish(session)
    with pytest.raises(RuntimeError, match="a bug"):
        calls.place(session, PLAN, NSE)
    outcomes = [x for x in _lines(session) if x["type"] == "outcome"]
    assert len(outcomes) == 1 and outcomes[0]["verdict"] == CHECK_THE_ACCOUNT
    assert "RuntimeError" in outcomes[0]["detail"]["raised"]["exception"]
    tag = outcomes[0]["tag"]
    assert "1001" in session.ledger.flagged() and session.ledger.tag_flagged(tag)
    assert session.guard.tripped("nse") is not None and session.ledger.pending_place(NSE.case_id)
    client.books = [[_order("1001", "OPEN", tag=tag)]]
    report = calls.teardown(session)
    assert report is not None and report.flagged == ["1001"] and report.pending == []
    assert client.count("cancel_order") == 0
    note = _lines(session)[-1]
    assert note["type"] == "teardown" and note["flagged"] == ["1001"] and note["pending"] == []


@pytest.mark.parametrize("kind", ["modify", "cancel"])
def test_an_exception_after_a_modify_or_cancel_intent_flags_the_order(
    tmp_path: Path, kind: str
) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    tag = _placed(session)
    _bug_before_finish(session)
    with pytest.raises(RuntimeError):
        if kind == "modify":
            calls.modify(session, "1001", NSE, price=Decimal("359.95"), **_modify_kwargs())
        else:
            calls.cancel(session, "1001", NSE)
    last = _lines(session)[-1]
    assert last["type"] == "outcome" and last["verdict"] == CHECK_THE_ACCOUNT
    assert "1001" in session.ledger.flagged()
    client.books = [[_order("1001", "OPEN", tag=tag)]]
    assert calls.cancel_ours(session).flagged == ["1001"]
    assert client.count("cancel_order") == int(kind == "cancel")


def test_a_ledger_that_cannot_write_the_outcome_still_flags_in_memory(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    tags: list[str] = []
    original = session.ledger.intent

    def intent(tag: str, *args: Any, **kwargs: Any) -> str:
        tags.append(tag)
        return original(tag, *args, **kwargs)

    def disk_full(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    session.ledger.intent = intent  # type: ignore[method-assign]
    session.ledger.outcome = disk_full  # type: ignore[method-assign]
    with pytest.raises(OSError, match="disk full"):
        calls.place(session, PLAN, NSE)
    assert session.ledger.tag_flagged(tags[0]) and "1001" in session.ledger.flagged()
    assert session.guard.tripped("nse") is not None
    assert session.ledger.pending_place(NSE.case_id)  # the intent still has no outcome
    client.books = [[_order("1001", "OPEN", tag=tags[0])]]
    report = calls.cancel_ours(session)
    assert report.flagged == ["1001"] and client.count("cancel_order") == 0


def test_stop_run_and_teardown_notes_list_the_orders_in_flight(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    tag = session.ledger.new_tag(NSE.case_id)
    session.ledger.intent(tag, NSE.case_id, "place", segment="nse", plan={})  # in flight
    client.books = [[_order("9001", "OPEN", tag=tag)]]
    calls.stop_run(session, calls.StopRun("stopped by the STOP file"))
    stop_notes = [x for x in _lines(session) if x["type"] in ("stop", "stop_report")]
    assert stop_notes[-1]["type"] == "stop_report" and stop_notes[-1]["pending"] == ["9001"]
    calls.teardown(session)
    assert _lines(session)[-1]["pending"] == ["9001"]
    assert client.count("cancel_order") == 0


def test_an_interruption_with_a_ledger_that_cannot_write_keeps_ctrl_c_and_flags(
    tmp_path: Path,
) -> None:
    client = MatrixClient(place=[KeyboardInterrupt()])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    tags: list[str] = []
    original = session.ledger.intent

    def intent(tag: str, *args: Any, **kwargs: Any) -> str:
        tags.append(tag)
        return original(tag, *args, **kwargs)

    def disk_full(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    session.ledger.intent = intent  # type: ignore[method-assign]
    session.ledger.outcome = disk_full  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):  # the interruption, not the ledger's OSError
        calls.place(session, PLAN, NSE)
    assert session.ledger.tag_flagged(tags[0])  # flagged in memory: no further call for it


def test_a_check_step_that_raises_still_flags_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    tags: list[str] = []
    original = session.ledger.intent

    def intent(tag: str, *args: Any, **kwargs: Any) -> str:
        tags.append(tag)
        return original(tag, *args, **kwargs)

    def disk_full(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    def broken_check(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the CHECK step broke")

    session.ledger.intent = intent  # type: ignore[method-assign]
    session.ledger.outcome = disk_full  # type: ignore[method-assign]
    monkeypatch.setattr(calls, "_check_the_account", broken_check)
    with pytest.raises(OSError, match="disk full"):  # the original exception is raised
        calls.place(session, PLAN, NSE)
    assert session.ledger.tag_flagged(tags[0]) and "1001" in session.ledger.flagged()


def test_modify_rechecks_the_stop_file_after_its_book_read(tmp_path: Path) -> None:
    from tests.live_orders.uat_matrix.guard import StopRun

    client = MatrixClient()
    session = _session(client, tmp_path)
    tag = _placed(session)
    client.books = [[_order("1001", "OPEN", tag=tag)]]

    def stop_during_the_read(name: str, args: Any) -> None:
        if name == "orders":
            session.guard.stop_file.write_text("stop", encoding="utf-8")

    client.on_call = stop_during_the_read
    with pytest.raises(StopRun):
        calls.modify(session, "1001", NSE, price=Decimal("361.00"))
    assert client.count("modify_order") == 0


def test_modify_rechecks_the_breaker_after_its_book_read(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    tag = _placed(session)
    client.books = [[_order("1001", "OPEN", tag=tag)]]

    def trip_during_the_read(name: str, args: Any) -> None:
        if name == "orders":
            session.guard.trip("nse", "tripped by another worker meanwhile")

    client.on_call = trip_during_the_read
    with pytest.raises(Refusal, match="circuit breaker"):
        calls.modify(session, "1001", NSE, price=Decimal("361.00"))
    assert client.count("modify_order") == 0
