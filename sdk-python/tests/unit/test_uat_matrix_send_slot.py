"""The per-SDK send slot: a serial SDK's writes are paced and re-checked inside one slot.

A driver serves one request at a time, so the pacer slot and the send must be one step: a call
that took its pacer slot and then queued behind a slow reply would go out back to back with the
calls queued with it (final review M-a). Every state-changing call of ``calls.py`` therefore
takes the SDK's send slot first, then the pacer's wait, the gate / STOP re-check and the write,
and gives the slot back right after the write (never held across the reads that follow).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from tests.live_orders.uat_matrix import calls
from tests.live_orders.uat_matrix.calls import CaseRef
from tests.live_orders.uat_matrix.guard import StopRun
from tests.live_orders.uat_matrix.sdk_under_test import PythonSdk
from tests.unit.test_uat_matrix_gtt import PLAN as GTT_PLAN
from tests.unit.test_uat_matrix_gtt import GttClient, _gtt, _modify_fields
from tests.unit.test_uat_matrix_safety import (
    PLAN,
    MatrixClient,
    _lines,
    _modify_kwargs,
    _order,
    _session,
)
from tradesmart import SlicedOrderResult

NSE = CaseRef("m1-nse-itc-cnc-lmt-day-buy", "nse")
GTT_CASE = CaseRef("gtt-nse-b-cnc-lmt-day-buy", "nse")
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
SLICED = SlicedOrderResult(
    requested_quantity=3,
    slice_quantities=(1, 1, 1),
    order_ids=("2001", "2002", "2003"),
    placed_quantity=3,
    unsent_quantity=0,
    complete=True,
    error=None,
)


class LogSlot:
    """A send slot that writes its entry and release to ``log`` (and may fire ``on_enter``
    once taken, as time passing while a call waited for it)."""

    def __init__(self, log: list[str], on_enter: Callable[[], None] | None = None) -> None:
        self.log = log
        self.on_enter = on_enter

    def __enter__(self) -> None:
        self.log.append("slot")
        if self.on_enter is not None:
            self.on_enter()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.log.append("release")


class SlotSdk(PythonSdk):
    """The Python SDK with a send slot of its own, standing in for a serial driver."""

    def __init__(self, client: Any, slot: Any) -> None:
        super().__init__(client)
        self.slot = slot

    def send_slot(self) -> Any:
        return self.slot


class LogPacer:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def wait(self, n: int = 1) -> None:
        self.log.append(f"wait:{n}")


def _rig(
    tmp_path: Path,
    client: MatrixClient,
    *,
    slot: Any = None,
    on_enter: Callable[[Any], None] | None = None,
) -> tuple[Any, list[str]]:
    """A session whose SDK is ``SlotSdk``; the log records the slot, the pacer wait, the gate
    check and every call of ``client`` (reads and writes)."""
    log: list[str] = []
    session = _session(client, tmp_path)
    fire = None if on_enter is None else (lambda: on_enter(session))
    session.sdk = SlotSdk(client, slot if slot is not None else LogSlot(log, fire))
    session.order_pacer = LogPacer(log)  # type: ignore[assignment]
    check_new = session.guard.check_new

    def logged_check(segment: str) -> None:
        log.append("check")
        check_new(segment)

    session.guard.check_new = logged_check  # type: ignore[method-assign]

    def on_call(name: str, args: Any) -> None:
        log.append(f"write:{name}" if name in WRITES else f"read:{name}")

    client.on_call = on_call
    return session, log


def _intents(session: Any) -> list[dict[str, Any]]:
    return [line for line in _lines(session) if line.get("type") == "intent"]


# -- every write: slot, pacer wait, re-check, write, release -----------------------------------


def _place(session: Any) -> None:
    session.guard.reserve(NSE.case_id, 1)
    calls.place(session, PLAN, NSE)


def _sliced(session: Any) -> None:
    session.guard.reserve(NSE.case_id, 3)
    calls.place_sliced(session, PLAN, NSE, max_slices=3)


def _placed(session: Any, log: list[str]) -> str:
    session.guard.reserve(NSE.case_id, 1)
    order_id = calls.place(session, PLAN, NSE).order_id
    assert order_id is not None
    log.clear()
    return order_id


def _modify(session: Any, order_id: str) -> None:
    before = _order(order_id, "OPEN", session.ledger.tag_of(order_id))
    calls.modify(session, order_id, NSE, before=before, **_modify_kwargs(), price="361.00")


def _cancel(session: Any, order_id: str) -> None:
    calls.cancel(session, order_id, NSE)


def _exit(session: Any, order_id: str) -> None:
    calls.exit(session, order_id, "CO", NSE)


def _gtt_rig(tmp_path: Path) -> tuple[Any, list[str], str]:
    client = GttClient()
    client.gtt_place_results = ["9001"]
    session, log = _rig(tmp_path, client)
    session.guard.reserve(GTT_CASE.case_id, 3)
    tag = calls.gtt_place(session, GTT_PLAN, GTT_CASE).tag
    log.clear()
    return session, log, tag


SEND = ["slot", "wait:1", "check"]  # then the write, then the release


def test_a_place_is_paced_and_rechecked_inside_the_slot(tmp_path: Path) -> None:
    session, log = _rig(tmp_path, MatrixClient())
    _place(session)
    assert log[log.index("slot") :] == [*SEND, "write:place_order", "release"]


def test_a_sliced_place_is_paced_by_its_slices_inside_the_slot(tmp_path: Path) -> None:
    session, log = _rig(tmp_path, MatrixClient(sliced=SLICED))
    _sliced(session)
    assert log[log.index("slot") :] == [
        "slot",
        "wait:3",
        "check",
        "write:place_sliced_order",
        "release",
    ]


def test_a_modify_is_paced_and_rechecked_inside_the_slot(tmp_path: Path) -> None:
    session, log = _rig(tmp_path, MatrixClient())
    order_id = _placed(session, log)
    _modify(session, order_id)
    assert log[log.index("slot") :] == [*SEND, "write:modify_order", "release"]


def test_a_cancel_and_an_exit_are_paced_inside_the_slot_and_have_no_recheck(
    tmp_path: Path,
) -> None:
    client = MatrixClient(place=["1001", "1002"])
    session, log = _rig(tmp_path, client)
    session.guard.reserve(NSE.case_id, 2)
    first = calls.place(session, PLAN, NSE).order_id
    second = calls.place(session, PLAN, NSE).order_id
    assert first is not None and second is not None
    log.clear()
    _cancel(session, first)
    assert log[log.index("slot") :] == ["slot", "wait:1", "write:cancel_order", "release"]
    log.clear()
    _exit(session, second)
    assert log[log.index("slot") :] == ["slot", "wait:1", "write:exit_order", "release"]


def test_gtt_writes_are_paced_inside_the_slot(tmp_path: Path) -> None:
    session, log, tag = _gtt_rig(tmp_path)
    calls.gtt_modify(session, "9001", GTT_CASE, before=_gtt("9001", tag), **_modify_fields())
    assert log[log.index("slot") :] == [*SEND, "write:modify_gtt", "release"]
    log.clear()
    calls.gtt_cancel(session, "9001", GTT_CASE)
    assert log[log.index("slot") :] == ["slot", "wait:1", "write:cancel_gtt", "release"]
    log.clear()
    calls.gtt_place(session, GTT_PLAN, GTT_CASE)
    assert log[log.index("slot") :] == [*SEND, "write:place_gtt", "release"]


def test_the_slot_is_given_back_before_the_lookup_after_an_ambiguous_write(
    tmp_path: Path,
) -> None:
    client = MatrixClient(place=[RuntimeError("the reply was lost")])
    session, log = _rig(tmp_path, client)
    _place(session)  # unknown outcome: the order book is read (never inside the slot)
    assert log.index("release") < log.index("read:orders")
    assert log.count("slot") == log.count("release") == 1


# -- a STOP file or a trip that arrives while a call waits for the slot ---------------------------


def _stop(session: Any) -> None:
    session.guard.stop_file.write_text("", encoding="utf-8")


def _trip(session: Any) -> None:
    session.guard.trip("nse", "three unexpected rejections")


EVENTS = [(_stop, StopRun, "STOP"), (_trip, calls.Refusal, "circuit breaker")]


@pytest.mark.parametrize(("event", "error", "match"), EVENTS)
def test_a_place_held_for_the_slot_is_not_sent_after_a_stop_or_trip(
    tmp_path: Path, event: Any, error: type[Exception], match: str
) -> None:
    client = MatrixClient()
    session, log = _rig(tmp_path, client, on_enter=event)
    session.guard.reserve(NSE.case_id, 1)
    with pytest.raises(error, match=match):
        calls.place(session, PLAN, NSE)
    assert client.count("place_order") == 0 and _intents(session) == []
    assert session.guard.pending == 0
    assert log.count("slot") == log.count("release") == 1


@pytest.mark.parametrize(("event", "error", "match"), EVENTS)
def test_a_sliced_place_held_for_the_slot_is_not_sent_after_a_stop_or_trip(
    tmp_path: Path, event: Any, error: type[Exception], match: str
) -> None:
    client = MatrixClient(sliced=SLICED)
    session, log = _rig(tmp_path, client, on_enter=event)
    session.guard.reserve(NSE.case_id, 3)
    with pytest.raises(error, match=match):
        calls.place_sliced(session, PLAN, NSE, max_slices=3)
    assert client.count("place_sliced_order") == 0 and _intents(session) == []
    assert log.count("slot") == log.count("release") == 1


@pytest.mark.parametrize(("event", "error", "match"), EVENTS)
def test_a_modify_held_for_the_slot_is_not_sent_after_a_stop_or_trip(
    tmp_path: Path, event: Any, error: type[Exception], match: str
) -> None:
    client = MatrixClient()
    fired = []
    session, log = _rig(tmp_path, client, on_enter=lambda s: fired and event(s))
    order_id = _placed(session, log)
    fired.append(True)  # the event comes with the modify's wait, not the place's
    intents = len(_intents(session))
    with pytest.raises(error, match=match):
        _modify(session, order_id)
    assert client.count("modify_order") == 0 and len(_intents(session)) == intents
    assert log.count("slot") == log.count("release") == 1


@pytest.mark.parametrize(("event", "error", "match"), EVENTS)
def test_gtt_writes_held_for_the_slot_are_not_sent_after_a_stop_or_trip(
    tmp_path: Path, event: Any, error: type[Exception], match: str
) -> None:
    client = GttClient()
    client.gtt_place_results = ["9001"]
    fired: list[bool] = []
    session, log = _rig(tmp_path, client, on_enter=lambda s: fired and event(s))
    session.guard.reserve(GTT_CASE.case_id, 2)
    tag = calls.gtt_place(session, GTT_PLAN, GTT_CASE).tag
    fired.append(True)
    intents = len(_intents(session))
    with pytest.raises(error, match=match):
        calls.gtt_modify(session, "9001", GTT_CASE, before=_gtt("9001", tag), **_modify_fields())
    with pytest.raises(error, match=match):
        calls.gtt_place(session, GTT_PLAN, GTT_CASE)
    assert client.count("modify_gtt") == 0 and client.count("place_gtt") == 1
    assert len(_intents(session)) == intents
    assert log.count("slot") == log.count("release")


def test_a_cancel_and_an_exit_held_for_the_slot_still_go_out_after_a_stop(
    tmp_path: Path,
) -> None:
    client = MatrixClient(place=["1001", "1002"])
    fired: list[bool] = []
    session, log = _rig(tmp_path, client, on_enter=lambda s: fired and _stop(s))
    session.guard.reserve(NSE.case_id, 2)
    first = calls.place(session, PLAN, NSE).order_id
    second = calls.place(session, PLAN, NSE).order_id
    assert first is not None and second is not None
    fired.append(True)
    assert calls.cancel(session, first, NSE).verdict == calls.ACKNOWLEDGED
    assert calls.exit(session, second, "CO", NSE).verdict == calls.ACKNOWLEDGED
    assert client.count("cancel_order") == 1 and client.count("exit_order") == 1


class WatchedLock:
    """A real lock that says when a caller is waiting for it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.waiting = threading.Event()

    def __enter__(self) -> None:
        self.waiting.set()
        self.lock.acquire()

    def __exit__(self, *exc: object) -> None:
        self.lock.release()


def test_a_stop_file_created_while_a_call_waits_for_the_slot_refuses_it(tmp_path: Path) -> None:
    client = MatrixClient()
    slot = WatchedLock()
    session, _log = _rig(tmp_path, client, slot=slot)
    session.guard.reserve(NSE.case_id, 1)
    slot.lock.acquire()  # another write of this SDK is going out
    result: list[BaseException] = []

    def place() -> None:
        try:
            calls.place(session, PLAN, NSE)
        except BaseException as exc:
            result.append(exc)

    worker = threading.Thread(target=place, daemon=True)
    worker.start()
    assert slot.waiting.wait(5.0)  # the call is at the slot
    session.guard.stop_file.write_text("", encoding="utf-8")
    slot.lock.release()
    worker.join(5.0)
    assert not worker.is_alive()
    assert len(result) == 1 and isinstance(result[0], StopRun)
    assert client.count("place_order") == 0 and _intents(session) == []
    assert not slot.lock.locked()


# -- the slot is released whatever the call does --------------------------------------------------


def _raises(kind: str, session: Any, client: MatrixClient) -> None:
    if kind == "write":
        client.place_results = [KeyboardInterrupt()]
    elif kind == "intent":

        def broken(*args: Any, **kwargs: Any) -> None:
            raise OSError("the ledger cannot write")

        session.ledger.intent = broken
    elif kind == "check":
        session.guard.stop_file.write_text("", encoding="utf-8")


@pytest.mark.parametrize(
    ("kind", "error"),
    [
        ("write", KeyboardInterrupt),
        ("intent", OSError),
        ("check", StopRun),
    ],
)
def test_the_slot_is_released_when_a_call_raises_inside_it(
    tmp_path: Path, kind: str, error: type[BaseException]
) -> None:
    client = MatrixClient()
    lock = WatchedLock()
    session, _log = _rig(tmp_path, client, slot=lock)
    session.order_pacer = LogPacer([])  # type: ignore[assignment]
    session.guard.reserve(NSE.case_id, 1)
    _raises(kind, session, client)
    with pytest.raises(error):
        calls.place(session, PLAN, NSE)
    assert not lock.lock.locked()  # the next call is not left waiting


def test_the_slot_is_released_when_a_cancel_raises(tmp_path: Path) -> None:
    client = MatrixClient()
    lock = WatchedLock()
    session, _log = _rig(tmp_path, client, slot=lock)
    session.order_pacer = LogPacer([])  # type: ignore[assignment]
    session.guard.reserve(NSE.case_id, 1)
    order_id = calls.place(session, PLAN, NSE).order_id
    assert order_id is not None
    client.cancel_result = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        calls.cancel(session, order_id, NSE)
    assert not lock.lock.locked()
