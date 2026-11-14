"""Mocked unit tests pinning ``uat_matrix/calls_async.py``: the async never-resend wrappers.

They mirror the key safety tests of ``calls.py`` (``test_uat_matrix_safety.py``): one call per
intent, the ledger line before and after each call, an ambiguous outcome resolved only by
read-only book reads (by tag for a place, by the order's row for a modify / cancel / exit),
CHECK THE ACCOUNT plus the segment's breaker when it stays unresolved, refusals before any call
for an order that is not ours, flagged or already cancelled once, and the stop / breaker rules.
``AsyncFacade`` stands in for ``AsyncTradeSmart``: each coroutine delegates to the scripted
sync ``MatrixClient`` of the safety tests, so both clients share one call log.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.live_orders.safety import CHECK_THE_ACCOUNT, Refusal
from tests.live_orders.uat_matrix import calls, calls_async, gate
from tests.live_orders.uat_matrix.calls import CaseRef
from tests.unit.test_uat_matrix_safety import (
    PLAN,
    MatrixClient,
    _modify_kwargs,
    _order,
    _rejected,
    _session,
    _unknown,
)
from tradesmart import NetworkError, RequestValidationError
from tradesmart.models.orders import Order

NSE = CaseRef("m9-nse-b-mis-lmt-day-buy-open-async", "nse")


class AsyncFacade:
    """``AsyncTradeSmart`` stand-in: every coroutine runs the sync scripted client's method."""

    def __init__(self, sync: MatrixClient) -> None:
        self.sync = sync

    async def orders(self) -> list[Order]:
        return self.sync.orders()

    async def place_order(self, **kwargs: Any) -> str:
        return self.sync.place_order(**kwargs)

    async def modify_order(self, order_id: str, **kwargs: Any) -> str:
        return self.sync.modify_order(order_id, **kwargs)

    async def cancel_order(self, order_id: str) -> str:
        return self.sync.cancel_order(order_id)

    async def exit_order(self, order_id: str, product: str) -> str:
        return self.sync.exit_order(order_id, product)


def _rig(tmp_path: Path, **script: Any) -> tuple[gate.MatrixSession, MatrixClient, AsyncFacade]:
    client = MatrixClient(**script)
    session = _session(client, tmp_path)
    return session, client, AsyncFacade(client)


def _lines(session: gate.MatrixSession) -> list[dict[str, Any]]:
    text = session.ledger.path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


async def _placed(
    session: gate.MatrixSession, client: MatrixClient, aclient: AsyncFacade, order_id: str = "1001"
) -> str:
    client.place_results = [order_id]
    case = CaseRef(f"setup-{order_id}", "nse")
    session.guard.reserve(case.case_id, 1)
    outcome = await calls_async.place(session, aclient, PLAN, case)  # type: ignore[arg-type]
    assert outcome.order_id == order_id
    return outcome.tag


async def _close(
    session: gate.MatrixSession, aclient: AsyncFacade, kind: str, order_id: str
) -> calls.OrderOutcome:
    if kind == "modify":
        return await calls_async.modify(
            session,
            aclient,
            order_id,
            NSE,
            price=Decimal("1"),
            **_modify_kwargs(),  # type: ignore[arg-type]
        )
    if kind == "cancel":
        return await calls_async.cancel(session, aclient, order_id, NSE)  # type: ignore[arg-type]
    return await calls_async.exit(session, aclient, order_id, "CO", NSE)  # type: ignore[arg-type]


# -- the ledger around every call -------------------------------------------------------------


async def test_place_writes_the_intent_before_and_the_outcome_after(tmp_path: Path) -> None:
    seen: list[str] = []

    def on_call(name: str, args: Any) -> None:
        if name == "place_order":
            text = session.ledger.path.read_text(encoding="utf-8")
            seen.append(json.loads(text.splitlines()[-1])["type"])

    session, client, aclient = _rig(tmp_path, on_call=on_call)
    session.guard.reserve(NSE.case_id, 1)
    outcome = await calls_async.place(session, aclient, PLAN, NSE)  # type: ignore[arg-type]
    assert seen == ["intent"]
    last = _lines(session)[-1]
    assert last["type"] == "outcome" and last["order_id"] == "1001" == outcome.order_id
    assert outcome.verdict == calls.ACKNOWLEDGED and outcome.tag == client.sent_tags()[0]
    assert session.ledger.ours() == {"1001"} and session.guard.open_ids == {"1001"}
    assert session.guard.pending == 0


@pytest.mark.parametrize("kind", ["modify", "cancel", "exit"])
async def test_modify_cancel_exit_write_the_ledger_around_one_call(
    tmp_path: Path, kind: str
) -> None:
    seen: list[str] = []

    def on_call(name: str, args: Any) -> None:
        if name in ("modify_order", "cancel_order", "exit_order"):
            text = session.ledger.path.read_text(encoding="utf-8")
            seen.append(json.loads(text.splitlines()[-1])["type"])

    session, client, aclient = _rig(tmp_path, on_call=on_call)
    tag = await _placed(session, client, aclient)
    outcome = await _close(session, aclient, kind, "1001")
    assert seen == ["intent"] and outcome.verdict == calls.ACKNOWLEDGED
    last, intent = _lines(session)[-1], _lines(session)[-2]
    assert last["kind"] == kind and intent["tag"] == tag and last["call"] == intent["call"]


# -- definitive answers -----------------------------------------------------------------------


async def test_a_rejected_place_is_definitive_and_never_resent(tmp_path: Path) -> None:
    session, client, aclient = _rig(tmp_path, place=[_rejected()])
    session.guard.reserve(NSE.case_id, 1)
    outcome = await calls_async.place(session, aclient, PLAN, NSE)  # type: ignore[arg-type]
    assert outcome.verdict == calls.REJECTED and outcome.order_id is None
    assert client.count("place_order") == 1
    assert session.guard.pending == 0 and session.guard.open_count == 0


async def test_a_place_refused_client_side_is_not_sent(tmp_path: Path) -> None:
    session, _, aclient = _rig(tmp_path, place=[RequestValidationError("bad price")])
    session.guard.reserve(NSE.case_id, 1)
    outcome = await calls_async.place(session, aclient, PLAN, NSE)  # type: ignore[arg-type]
    assert outcome.verdict == calls.NOT_SENT and outcome.sent is False


async def test_a_rejected_modify_is_definitive(tmp_path: Path) -> None:
    session, client, aclient = _rig(tmp_path)
    await _placed(session, client, aclient)
    client.modify_result = _rejected("SAF: order is not open")
    outcome = await _close(session, aclient, "modify", "1001")
    assert outcome.verdict == calls.REJECTED and client.count("modify_order") == 1


# -- ambiguous outcomes: read the book, never resend ------------------------------------------


@pytest.mark.parametrize("error", [_unknown(), NetworkError("reset")])
async def test_an_ambiguous_place_is_found_by_tag(tmp_path: Path, error: Exception) -> None:
    def book(c: MatrixClient) -> list[Order]:
        return [_order("1009", "OPEN", tag=c.sent_tags()[0])]

    session, client, aclient = _rig(tmp_path, place=[error])
    session.guard.reserve(NSE.case_id, 1)
    client.books = [book]
    outcome = await calls_async.place(session, aclient, PLAN, NSE)  # type: ignore[arg-type]
    assert client.count("place_order") == 1
    assert outcome.verdict == calls.FOUND and outcome.order_id == "1009"
    assert session.ledger.ours() == {"1009"}


async def test_an_ambiguous_place_not_found_is_check_the_account_and_trips(
    tmp_path: Path,
) -> None:
    session, client, aclient = _rig(tmp_path, place=[_unknown()])
    session.guard.reserve(NSE.case_id, 2)
    reads = client.count("orders")
    outcome = await calls_async.place(session, aclient, PLAN, NSE)  # type: ignore[arg-type]
    assert client.count("place_order") == 1
    assert client.count("orders") - reads == session.lookup_attempts  # read-only lookups
    assert outcome.verdict == CHECK_THE_ACCOUNT
    reason = session.guard.tripped("nse")
    assert reason is not None and outcome.tag in reason
    with pytest.raises(Refusal, match="circuit breaker"):
        await calls_async.place(session, aclient, PLAN, NSE)  # type: ignore[arg-type]
    assert client.count("place_order") == 1


async def test_an_ambiguous_modify_is_resolved_only_by_a_visible_change(tmp_path: Path) -> None:
    session, client, aclient = _rig(tmp_path, modify=_unknown("ModifyOrder"))
    tag = await _placed(session, client, aclient)
    row = _order("1001", "OPEN", tag=tag, prc="360.00", qty="1", prctyp="LMT", prd="C")
    after = _order("1001", "OPEN", tag=tag, prc="359.95", qty="1", prctyp="LMT", prd="C")
    client.books = [[after]]
    outcome = await calls_async.modify(
        session,
        aclient,
        "1001",
        NSE,
        before=row,
        price=Decimal("359.95"),
        **_modify_kwargs(),  # type: ignore[arg-type]
    )
    assert outcome.verdict == calls.RESOLVED and client.count("modify_order") == 1
    client.books = [[after]]
    outcome = await calls_async.modify(
        session,
        aclient,
        "1001",
        NSE,
        before=after,
        price=Decimal("359.90"),
        **_modify_kwargs(),  # type: ignore[arg-type]
    )
    assert outcome.verdict == CHECK_THE_ACCOUNT and "1001" in session.ledger.flagged()
    assert session.guard.tripped("nse") is not None
    assert client.count("modify_order") == 2  # one per intent; the second order is flagged now
    with pytest.raises(Refusal, match=CHECK_THE_ACCOUNT):
        await calls_async.cancel(session, aclient, "1001", NSE)  # type: ignore[arg-type]
    assert client.count("cancel_order") == 0


async def test_a_modify_without_before_reads_the_row_first(tmp_path: Path) -> None:
    session, client, aclient = _rig(tmp_path, modify=_unknown("ModifyOrder"))
    tag = await _placed(session, client, aclient)
    before = _order("1001", "OPEN", tag=tag, prc="360.00", qty="1", prctyp="LMT", prd="C")
    after = _order("1001", "OPEN", tag=tag, prc="359.95", qty="1", prctyp="LMT", prd="C")
    client.books = [[before], [after]]
    outcome = await _close_price(session, aclient, Decimal("359.95"))
    assert outcome.verdict == calls.RESOLVED


async def _close_price(
    session: gate.MatrixSession, aclient: AsyncFacade, price: Decimal
) -> calls.OrderOutcome:
    return await calls_async.modify(
        session,
        aclient,
        "1001",
        NSE,
        price=price,
        **_modify_kwargs(),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(("status", "verdict"), [("CANCELED", calls.RESOLVED),
                                                  ("OPEN", CHECK_THE_ACCOUNT)])  # fmt: skip
async def test_an_ambiguous_cancel_is_resolved_by_a_final_row_else_check(
    tmp_path: Path, status: str, verdict: str
) -> None:
    session, client, aclient = _rig(tmp_path, cancel=_unknown("CancelOrder"))
    tag = await _placed(session, client, aclient)
    client.books = [[_order("1001", status, tag=tag)]]
    outcome = await calls_async.cancel(session, aclient, "1001", NSE)  # type: ignore[arg-type]
    assert outcome.verdict == verdict and client.count("cancel_order") == 1
    assert (session.guard.tripped("nse") is not None) is (verdict == CHECK_THE_ACCOUNT)


async def test_an_ambiguous_exit_is_resolved_or_check_the_account(tmp_path: Path) -> None:
    session, client, aclient = _rig(tmp_path, exit_=_unknown("ExitSNOOrder"))
    tag1 = await _placed(session, client, aclient, "1001")
    tag2 = await _placed(session, client, aclient, "1002")
    client.books = [[_order("1001", "COMPLETE", tag=tag1)]]
    assert (await calls_async.exit(session, aclient, "1001", "CO", NSE)).verdict == calls.RESOLVED  # type: ignore[arg-type]
    client.books = [[_order("1002", "OPEN", tag=tag2)]]
    second = await calls_async.exit(session, aclient, "1002", "CO", NSE)  # type: ignore[arg-type]
    assert second.verdict == CHECK_THE_ACCOUNT and client.count("exit_order") == 2


# -- only our own orders; one cancel per order -------------------------------------------------


@pytest.mark.parametrize("kind", ["modify", "cancel", "exit"])
async def test_a_foreign_order_is_refused_with_no_call(tmp_path: Path, kind: str) -> None:
    session, client, aclient = _rig(tmp_path)
    before = len(client.calls)
    with pytest.raises(Refusal, match="not an order of this run"):
        await _close(session, aclient, kind, "555")
    assert client.calls[before:] == []


async def test_a_second_cancel_of_one_order_is_refused(tmp_path: Path) -> None:
    session, client, aclient = _rig(tmp_path)
    await _placed(session, client, aclient)
    await calls_async.cancel(session, aclient, "1001", NSE)  # type: ignore[arg-type]
    with pytest.raises(Refusal, match="already cancelled once"):
        await calls_async.cancel(session, aclient, "1001", NSE)  # type: ignore[arg-type]
    with pytest.raises(Refusal, match="already cancelled once"):  # the sync wrapper agrees
        calls.cancel(session, "1001", NSE)
    assert client.count("cancel_order") == 1


async def test_a_cancel_never_sent_keeps_the_orders_one_cancel(tmp_path: Path) -> None:
    session, client, aclient = _rig(tmp_path, cancel=RequestValidationError("bad id"))
    await _placed(session, client, aclient)
    first = await calls_async.cancel(session, aclient, "1001", NSE)  # type: ignore[arg-type]
    assert first.verdict == calls.NOT_SENT and session.ledger.cancel_sent("1001") is False
    client.cancel_result = None
    second = await calls_async.cancel(session, aclient, "1001", NSE)  # type: ignore[arg-type]
    assert second.verdict == calls.ACKNOWLEDGED and session.ledger.cancel_sent("1001") is True


async def test_a_modify_tag_change_needs_a_tag_issued_by_the_ledger(tmp_path: Path) -> None:
    session, client, aclient = _rig(tmp_path)
    await _placed(session, client, aclient)
    with pytest.raises(ValueError, match="not issued"):
        await calls_async.modify(
            session,
            aclient,
            "1001",
            NSE,
            price=Decimal("1"),
            tag="free-text",
            **_modify_kwargs(),  # type: ignore[arg-type]
        )
    assert client.count("modify_order") == 0
    new = session.ledger.new_tag(NSE.case_id)
    await calls_async.modify(
        session,
        aclient,
        "1001",
        NSE,
        price=Decimal("1"),
        tag=new,
        **_modify_kwargs(),  # type: ignore[arg-type]
    )
    assert session.ledger.tag_of("1001") == new


# -- stop, breakers, declared counts, MAX_OPEN -------------------------------------------------


async def test_a_stopped_run_refuses_place_and_modify_but_cancels(tmp_path: Path) -> None:
    session, client, aclient = _rig(tmp_path)
    await _placed(session, client, aclient, "1001")
    session.guard.reserve(NSE.case_id, 1)
    (tmp_path / "STOP").write_text("", encoding="utf-8")
    with pytest.raises(calls.StopRun):
        await calls_async.place(session, aclient, PLAN, NSE)  # type: ignore[arg-type]
    with pytest.raises(calls.StopRun):
        await _close(session, aclient, "modify", "1001")
    await calls_async.cancel(session, aclient, "1001", NSE)  # type: ignore[arg-type]
    assert client.count("place_order") == 1 and client.count("modify_order") == 0
    assert client.count("cancel_order") == 1


async def test_a_case_cannot_exceed_its_declared_count(tmp_path: Path) -> None:
    session, client, aclient = _rig(tmp_path, place=["1001", "1002"])
    session.guard.reserve(NSE.case_id, 1)
    await calls_async.place(session, aclient, PLAN, NSE)  # type: ignore[arg-type]
    with pytest.raises(Refusal, match="declared"):
        await calls_async.place(session, aclient, PLAN, NSE)  # type: ignore[arg-type]
    assert client.count("place_order") == 1 and session.guard.pending == 0


async def test_max_open_reached_refuses_before_sending(tmp_path: Path) -> None:
    session, client, aclient = _rig(tmp_path)
    session.guard.max_open = 1
    await _placed(session, client, aclient, "1001")
    session.guard.reserve(NSE.case_id, 1)
    with pytest.raises(calls_async.MaxOpenReached):
        await calls_async.place(session, aclient, PLAN, NSE)  # type: ignore[arg-type]
    assert client.count("place_order") == 1 and session.guard.pending == 0


async def test_a_plan_with_a_tag_is_refused(tmp_path: Path) -> None:
    session, client, aclient = _rig(tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    with pytest.raises(ValueError, match="tag"):
        await calls_async.place(session, aclient, {**PLAN, "tag": "mine"}, NSE)  # type: ignore[arg-type]
    assert client.count("place_order") == 0


# -- interruption -----------------------------------------------------------------------------


async def test_a_cancelled_task_mid_place_is_interrupted_and_flagged(tmp_path: Path) -> None:
    session, client, aclient = _rig(tmp_path, place=[asyncio.CancelledError()])
    session.guard.reserve(NSE.case_id, 1)
    with pytest.raises(asyncio.CancelledError):
        await calls_async.place(session, aclient, PLAN, NSE)  # type: ignore[arg-type]
    last = _lines(session)[-1]
    assert last["verdict"] == calls.INTERRUPTED
    assert session.ledger.tag_flagged(last["tag"]) and session.guard.pending == 0


async def test_ctrl_c_mid_cancel_marks_the_session_interrupted(tmp_path: Path) -> None:
    session, client, aclient = _rig(tmp_path, cancel=KeyboardInterrupt())
    tag = await _placed(session, client, aclient)
    client.books = [[_order("1001", "OPEN", tag=tag)]]
    with pytest.raises(KeyboardInterrupt):
        await calls_async.cancel(session, aclient, "1001", NSE)  # type: ignore[arg-type]
    assert session.interrupted is True and "1001" in session.ledger.flagged()
    report = calls.cancel_ours(session)
    assert report.flagged == ["1001"] and client.count("cancel_order") == 1


# -- the raw bodies of the async client --------------------------------------------------------


async def test_attach_responses_records_the_async_clients_order_bodies(tmp_path: Path) -> None:
    session, _, _ = _rig(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"stat": "Ok", "norenordno": "1001"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    class Holder:
        class _transport:  # noqa: N801 - the SDK's private attribute name
            http_client = http

    assert calls_async.attach_responses(session, Holder()) is True  # type: ignore[arg-type]
    mark = session.responses.mark()
    response = await http.post("https://uat.example.test/NorenWClientAPIv2/PlaceOrder")
    await response.aread()
    seen = session.responses.last("PlaceOrder", since=mark)
    assert seen is not None and seen["body"]["norenordno"] == "1001"
    await http.aclose()
    assert calls_async.attach_responses(session, object()) is False  # type: ignore[arg-type]


# == Fix round 1: in-flight calls under real task cancellation and concurrency ================


class SlowFacade(AsyncFacade):
    """Order calls that take real loop time (tasks interleave); ``orders()`` may block."""

    def __init__(
        self,
        sync: MatrixClient,
        *,
        place_delay: float = 0.0,
        cancel_delay: float = 0.0,
        block_orders: bool = False,
        on_orders: Any = None,
    ) -> None:
        super().__init__(sync)
        self.place_delay = place_delay
        self.cancel_delay = cancel_delay
        self.block_orders = block_orders
        self.on_orders = on_orders
        self.blocked = asyncio.Event()

    async def orders(self) -> list[Order]:
        if self.on_orders is not None:
            self.on_orders()
        if self.block_orders:
            self.blocked.set()
            await asyncio.sleep(3600)
        return self.sync.orders()

    async def place_order(self, **kwargs: Any) -> str:
        await asyncio.sleep(self.place_delay)
        return self.sync.place_order(**kwargs)

    async def cancel_order(self, order_id: str) -> str:
        await asyncio.sleep(self.cancel_delay)
        return self.sync.cancel_order(order_id)


async def _cancel_while_blocked(aclient: SlowFacade, coro: Any) -> None:
    task = asyncio.create_task(coro)
    await asyncio.wait_for(aclient.blocked.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_task_cancelled_during_the_place_lookup_is_interrupted_and_flagged(
    tmp_path: Path,
) -> None:
    session, client, _ = _rig(tmp_path, place=[_unknown()])
    aclient = SlowFacade(client, block_orders=True)
    session.guard.reserve(NSE.case_id, 1)
    reads = client.count("orders")
    await _cancel_while_blocked(aclient, calls_async.place(session, aclient, PLAN, NSE))  # type: ignore[arg-type]
    last = _lines(session)[-1]
    assert last["type"] == "outcome" and last["verdict"] == calls.INTERRUPTED
    tag = last["tag"]
    assert session.ledger.tag_flagged(tag) and session.guard.pending == 0
    assert client.count("orders") == reads  # no blocking sync book read inside the loop
    client.books = [[_order("9001", "OPEN", tag=tag)]]
    report = calls.cancel_ours(session)
    assert report.flagged == ["9001"] and client.count("cancel_order") == 0


@pytest.mark.parametrize("kind", ["modify", "cancel"])
async def test_a_task_cancelled_during_a_modify_or_cancel_lookup_flags_the_order(
    tmp_path: Path, kind: str
) -> None:
    session, client, _ = _rig(
        tmp_path, modify=_unknown("ModifyOrder"), cancel=_unknown("CancelOrder")
    )
    tag = await _placed(session, client, AsyncFacade(client))
    aclient = SlowFacade(client, block_orders=True)
    row = _order("1001", "OPEN", tag=tag, prc="360.00", qty="1", prctyp="LMT", prd="C")
    if kind == "modify":
        coro = calls_async.modify(
            session,
            aclient,
            "1001",
            NSE,
            before=row,
            price=Decimal("359.95"),
            **_modify_kwargs(),  # type: ignore[arg-type]
        )
    else:
        coro = calls_async.cancel(session, aclient, "1001", NSE)  # type: ignore[arg-type]
    await _cancel_while_blocked(aclient, coro)
    last = _lines(session)[-1]
    assert last["type"] == "outcome" and last["verdict"] == calls.INTERRUPTED
    assert "1001" in session.ledger.flagged()
    client.books = [[row]]
    report = calls.cancel_ours(session)
    assert report.flagged == ["1001"] and client.count("cancel_order") == int(kind == "cancel")


async def test_a_wait_for_timeout_mid_place_is_interrupted_with_no_slot_left(
    tmp_path: Path,
) -> None:
    session, client, _ = _rig(tmp_path)
    aclient = SlowFacade(client, place_delay=10.0)
    session.guard.reserve(NSE.case_id, 1)
    reads = client.count("orders")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(calls_async.place(session, aclient, PLAN, NSE), timeout=0.05)  # type: ignore[arg-type]
    last = _lines(session)[-1]
    assert last["verdict"] == calls.INTERRUPTED and session.ledger.tag_flagged(last["tag"])
    assert session.guard.pending == 0 and client.count("orders") == reads
    client.books = [[_order("9001", "OPEN", tag=last["tag"])]]  # the place did go out
    report = calls.teardown(session)
    assert report is not None and report.flagged == ["9001"]
    assert client.count("cancel_order") == 0  # the teardown skips the flagged order


async def test_an_exception_after_the_async_intent_is_check_and_flagged(tmp_path: Path) -> None:
    session, client, aclient = _rig(tmp_path)
    session.guard.reserve(NSE.case_id, 1)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("a bug after the call")

    session.responses.last = broken  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="a bug"):
        await calls_async.place(session, aclient, PLAN, NSE)  # type: ignore[arg-type]
    outcomes = [x for x in _lines(session) if x["type"] == "outcome"]
    assert len(outcomes) == 1 and outcomes[0]["verdict"] == CHECK_THE_ACCOUNT
    assert "1001" in session.ledger.flagged() and session.guard.tripped("nse") is not None
    assert not session.ledger.pending_order("1001") and session.guard.pending == 0
    client.books = [[_order("1001", "OPEN", tag=outcomes[0]["tag"])]]
    report = calls.teardown(session)
    assert report is not None and report.flagged == ["1001"] and report.pending == []
    assert client.count("cancel_order") == 0


@pytest.mark.parametrize("kind", ["modify", "cancel"])
async def test_an_exception_after_an_async_modify_or_cancel_intent_flags_the_order(
    tmp_path: Path, kind: str
) -> None:
    session, client, aclient = _rig(tmp_path)
    tag = await _placed(session, client, aclient)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("a bug after the call")

    session.responses.last = broken  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await _close(session, aclient, kind, "1001")
    last = _lines(session)[-1]
    assert last["type"] == "outcome" and last["verdict"] == CHECK_THE_ACCOUNT
    assert "1001" in session.ledger.flagged()
    client.books = [[_order("1001", "OPEN", tag=tag)]]
    assert calls.cancel_ours(session).flagged == ["1001"]
    assert client.count("cancel_order") == int(kind == "cancel")


async def test_gathered_places_never_pass_max_open(tmp_path: Path) -> None:
    session, client, _ = _rig(tmp_path, place=["1001", "1002", "1003", "1004", "1005"])
    session.guard.max_open = 2
    aclient = SlowFacade(client, place_delay=0.05)
    refs = [CaseRef(f"gather-{i}", "nse") for i in range(5)]
    for ref in refs:
        session.guard.reserve(ref.case_id, 1)
    results = await asyncio.gather(
        *(calls_async.place(session, aclient, PLAN, r) for r in refs),  # type: ignore[arg-type]
        return_exceptions=True,
    )
    assert client.count("place_order") == 2
    assert sum(isinstance(r, calls_async.MaxOpenReached) for r in results) == 3
    assert session.guard.pending == 0 and len(session.guard.open_ids) == 2


async def test_two_concurrent_cancels_of_one_order_send_one(tmp_path: Path) -> None:
    session, client, _ = _rig(tmp_path)
    tag = await _placed(session, client, AsyncFacade(client))
    client.books = [[_order("1001", "OPEN", tag=tag)]]
    aclient = SlowFacade(client, cancel_delay=0.05)
    results = await asyncio.gather(
        calls_async.cancel(session, aclient, "1001", NSE),  # type: ignore[arg-type]
        calls_async.cancel(session, aclient, "1001", NSE),  # type: ignore[arg-type]
        return_exceptions=True,
    )
    assert client.count("cancel_order") == 1
    assert sorted(type(r).__name__ for r in results) == ["OrderOutcome", "Refusal"]


async def test_an_order_with_a_call_in_flight_gets_no_other_call(tmp_path: Path) -> None:
    session, client, _ = _rig(tmp_path, modify=_unknown("ModifyOrder"))
    tag = await _placed(session, client, AsyncFacade(client))
    aclient = SlowFacade(client, block_orders=True)
    row = _order("1001", "OPEN", tag=tag, prc="360.00", qty="1", prctyp="LMT", prd="C")
    task = asyncio.create_task(
        calls_async.modify(
            session,
            aclient,
            "1001",
            NSE,
            before=row,
            price=Decimal("359.95"),
            **_modify_kwargs(),  # type: ignore[arg-type]
        )
    )
    await asyncio.wait_for(aclient.blocked.wait(), 5)  # the modify's lookup is in flight
    assert session.ledger.pending_order("1001") and session.ledger.pending_tag(tag)
    with pytest.raises(Refusal, match="in flight"):
        await calls_async.cancel(session, AsyncFacade(client), "1001", NSE)  # type: ignore[arg-type]
    client.books = [[row]]
    report = calls.cancel_ours(session)
    assert report.pending == ["1001"] and client.count("cancel_order") == 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_stop_during_the_before_read_sends_no_modify(tmp_path: Path) -> None:
    session, client, _ = _rig(tmp_path)
    tag = await _placed(session, client, AsyncFacade(client))
    client.books = [[_order("1001", "OPEN", tag=tag, prc="360.00", qty="1", prctyp="LMT")]]

    def stop() -> None:
        (tmp_path / "STOP").write_text("", encoding="utf-8")

    aclient = SlowFacade(client, on_orders=stop)
    intents = len([x for x in _lines(session) if x["type"] == "intent"])
    with pytest.raises(calls.StopRun):
        await calls_async.modify(
            session,
            aclient,
            "1001",
            NSE,
            price=Decimal("1"),
            **_modify_kwargs(),  # type: ignore[arg-type]
        )
    assert client.count("modify_order") == 0
    assert len([x for x in _lines(session) if x["type"] == "intent"]) == intents


async def test_no_book_read_after_ctrl_c_an_ambiguous_place_is_check(tmp_path: Path) -> None:
    session, client, aclient = _rig(tmp_path, place=[_unknown()])
    session.guard.reserve(NSE.case_id, 1)
    session.interrupted = True
    reads = client.count("orders")
    outcome = await calls_async.place(session, aclient, PLAN, NSE)  # type: ignore[arg-type]
    assert outcome.verdict == CHECK_THE_ACCOUNT and client.count("orders") == reads
