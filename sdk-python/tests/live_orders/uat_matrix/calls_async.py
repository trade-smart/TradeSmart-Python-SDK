"""The async never-resend wrappers: ``calls.py``'s rules for an ``AsyncTradeSmart`` client.

The views family (M9) repeats its status paths through ``AsyncTradeSmart``; every state-changing
call it makes goes through here, and the rules are exactly those of ``calls.py`` (read that
module first), on the same session (one ``Ledger`` and one ``Guard`` for both clients):

- one SDK call per intent (``place``, ``modify``, ``cancel``, ``exit``), never repeated; the
  ledger line ``intent`` (unique call id) is written before the call, ``outcome`` after;
- ``APIError`` is ``REJECTED``; ``RequestValidationError`` is ``NOT_SENT``; any other exception
  is ambiguous: resolved only by **reading** the order book with the async client (a place by its
  tag, a modify / cancel / exit by the order's row), else CHECK THE ACCOUNT, the segment's
  breaker trips and the order gets no further state-changing call;
- an interruption anywhere from the intent to the outcome, the lookup included, is
  ``interrupted``: the outcome is written and the order and its tag are flagged
  (``calls.in_flight``). Under asyncio a Ctrl-C arrives as ``asyncio.CancelledError`` in the
  running task (``asyncio.run`` re-raises ``KeyboardInterrupt`` afterwards, and the matrix
  conftest then marks the session interrupted: its teardown cancels nothing); a timeout
  (``asyncio.wait_for``) is the same cancellation. No book read is made for an interrupted call
  (no blocking sync read inside the event loop), nor for any lookup once the session is
  interrupted;
- modify, cancel and exit act only on this run's own orders, by the ledger's tag; a flagged or
  ignored order, or one with a call in flight (its intent written, its outcome not yet), is
  refused before any call; one CancelOrder per order per session (shared with the sync
  wrappers: an order cancelled once by either is never cancelled again);
- place and modify are refused once the run is stopped or the segment tripped; cancel and exit
  always go out; a place claims its ``MAX_OPEN`` slot before sending and releases it after.

There is no sliced variant (the matrix sends sliced orders through the sync client only).
The helpers that carry no I/O are ``calls.py``'s own, so both wrappers decide alike.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from tests.live_orders.safety import Refusal, is_final, status_of
from tests.live_orders.uat_matrix import calls
from tests.live_orders.uat_matrix.calls import (
    ACKNOWLEDGED,
    FOUND,
    REJECTED,
    RESOLVED,
    CaseRef,
    OrderOutcome,
    PlanLike,
)
from tests.live_orders.uat_matrix.guard import MaxOpenReached, StopRun
from tests.live_orders.uat_matrix.ledger import NOT_SENT
from tradesmart.errors import APIError, RequestValidationError
from tradesmart.models.orders import Order

if TYPE_CHECKING:
    from tests.live_orders.uat_matrix.gate import MatrixSession

__all__ = [
    "ACKNOWLEDGED",
    "FOUND",
    "NOT_SENT",
    "REJECTED",
    "RESOLVED",
    "AsyncOrderClient",
    "MaxOpenReached",
    "StopRun",
    "attach_responses",
    "cancel",
    "exit",
    "modify",
    "place",
    "sleeper",
]


class AsyncOrderClient(Protocol):
    """The ``AsyncTradeSmart`` calls these wrappers make."""

    async def orders(self) -> list[Order]: ...

    async def cancel_order(self, order_id: str) -> str: ...

    # The SDK's keyword-only signatures: any callable returning the order id fits.
    @property
    def place_order(self) -> Callable[..., Awaitable[str]]: ...

    @property
    def modify_order(self) -> Callable[..., Awaitable[str]]: ...

    @property
    def exit_order(self) -> Callable[..., Awaitable[str]]: ...


Sleep = Callable[[float], Awaitable[None]]


def sleeper(session: MatrixSession) -> Sleep:
    """``asyncio.sleep`` for a real session; a test session's fake clock otherwise."""
    if session.sleep is time.sleep:
        return asyncio.sleep

    async def fake(seconds: float) -> None:
        session.sleep(seconds)
        await asyncio.sleep(0)  # still lets the stream's tasks run

    return fake


async def _book(client: AsyncOrderClient) -> list[Order] | None:
    try:
        return await client.orders()
    except Exception:  # a read; the caller's next read decides
        return None


async def _rows_with_tag(
    session: MatrixSession, client: AsyncOrderClient, tag: str
) -> tuple[list[Order], bool]:
    """Read the book (read-only) until a row carries ``tag`` or the reads run out; no read
    once the session is interrupted (Ctrl-C)."""
    sleep = sleeper(session)
    rows: list[Order] = []
    read_ok = False
    for index in range(session.lookup_attempts):
        if session.interrupted:
            break
        if index:
            await sleep(session.lookup_interval)
        book = await _book(client)
        if book is None:
            continue
        read_ok = True
        rows = [o for o in book if (o.tag or "").strip() == tag]
        if rows:
            break
    return rows, read_ok


async def _row_of(
    session: MatrixSession,
    client: AsyncOrderClient,
    order_id: str,
    done: Callable[[Order], bool],
) -> tuple[Order | None, bool]:
    """Read the book (read-only) until ``done(row)`` holds for the order's row; no read once
    the session is interrupted (Ctrl-C)."""
    sleep = sleeper(session)
    row: Order | None = None
    for index in range(session.lookup_attempts):
        if session.interrupted:
            break
        if index:
            await sleep(session.lookup_interval)
        book = await _book(client)
        if book is None:
            continue
        row = next((o for o in book if calls._id(o) == order_id), None)
        if row is not None and done(row):
            return row, True
    return row, False


# -- place ------------------------------------------------------------------------------------


async def place(
    session: MatrixSession,
    client: AsyncOrderClient,
    plan: PlanLike,
    case: CaseRef,
    *,
    tag: str | None = None,
) -> OrderOutcome:
    """One ``place_order`` call of the async client for ``plan`` (never resent)."""
    kwargs = calls._plan_kwargs(plan)
    calls.refuse_amo(session, kwargs)  # mock session: never an AMO
    calls.check_mock(session)  # mock session: the switch again (date rollover)
    if tag is not None:
        calls._check_issued(session, tag)
    calls._refuse_case(session, case)
    session.guard.check_send(case.segment)  # claims one MAX_OPEN slot
    opened: list[str] = []
    try:
        return await _place(session, client, kwargs, case, tag, opened)
    finally:
        session.guard.release(1, *opened)


async def _place(
    session: MatrixSession,
    client: AsyncOrderClient,
    kwargs: dict[str, Any],
    case: CaseRef,
    tag: str | None,
    opened: list[str],
) -> OrderOutcome:
    session.guard.spend(case.case_id)
    tag = tag if tag is not None else session.ledger.new_tag(case.case_id)
    outcome = OrderOutcome("place", case.case_id, tag, ACKNOWLEDGED)
    await session.order_pacer.wait_async(1, sleeper(session))  # shared with the sync calls
    session.guard.check_new(case.segment)  # again: a STOP file or a trip during the wait
    calls.check_mock(session)  # again, right before the intent
    mark = session.responses.mark()
    outcome.call = session.ledger.intent(
        tag, case.case_id, "place", segment=case.segment, plan=kwargs, client="async"
    )
    # An interruption anywhere below (the lookup included) writes ``interrupted`` and flags
    # the tag; no book read for it (a sync read would block the event loop).
    with calls.in_flight(session, outcome, case, lookup=False, opened=opened):
        try:
            order_id = await client.place_order(**kwargs, tag=tag)
        except RequestValidationError as exc:
            calls._settle(outcome, NOT_SENT, exc)
        except APIError as exc:  # stat Not_Ok: a definitive answer
            calls._settle(outcome, REJECTED, exc)
        except Exception as exc:  # ambiguous: look it up by tag, never resend
            calls._settle(outcome, None, exc)
            rows, read_ok = await _rows_with_tag(session, client, tag)
            if rows:
                outcome.verdict = FOUND
                outcome.order_id = calls._id(rows[0]) or None
                outcome.status = status_of(rows[0])
            else:
                why = "it is not in orders()" if read_ok else "the order book could not be read"
                calls._check_the_account(
                    session, outcome, case, f"order tag {tag}: place outcome unknown and {why}"
                )
        else:
            outcome.order_id = order_id
        if outcome.order_id is not None and not is_final(outcome.status):
            opened.append(outcome.order_id)
        return calls._finish(session, outcome, calls._response(session, "PlaceOrder", mark))


# -- modify / cancel / exit -------------------------------------------------------------------


async def modify(
    session: MatrixSession,
    client: AsyncOrderClient,
    order_id: str,
    case: CaseRef,
    *,
    before: Order | None = None,
    **changes: Any,
) -> OrderOutcome:
    """One ``modify_order`` call of the async client (never repeated); see ``calls.modify``."""
    calls.check_mock(session)  # mock session: the switch again (date rollover), before any read
    calls._our_tag(session, order_id)
    new_tag = changes.get("tag")
    if new_tag is not None:
        calls._check_issued(session, str(new_tag))
    session.guard.check_new(case.segment)
    if before is None:
        book = await _book(client)
        before = next((o for o in book or [] if calls._id(o) == order_id), None)
    # The read above awaited: the order, the STOP file and the breaker are checked again
    # right before the intent.
    tag = calls._our_tag(session, order_id)
    session.guard.check_new(case.segment)
    outcome = OrderOutcome("modify", case.case_id, tag, ACKNOWLEDGED, order_id=order_id)
    await session.order_pacer.wait_async(1, sleeper(session))  # shared with the sync calls
    session.guard.check_new(case.segment)  # again: a STOP file or a trip during the wait
    calls.check_mock(session)  # again, right before the intent
    mark = session.responses.mark()
    outcome.call = session.ledger.intent(
        tag,
        case.case_id,
        "modify",
        segment=case.segment,
        order_id=order_id,
        changes=changes,
        client="async",
    )
    with calls.in_flight(session, outcome, case, lookup=False):
        try:
            await client.modify_order(order_id, **changes)
        except RequestValidationError as exc:
            calls._settle(outcome, NOT_SENT, exc)
        except APIError as exc:
            calls._settle(outcome, REJECTED, exc)
        except Exception as exc:  # ambiguous: resolved by reading the book, never repeated
            calls._settle(outcome, None, exc)
            seen = before
            row, resolved = await _row_of(
                session,
                client,
                order_id,
                lambda r: is_final(status_of(r)) or calls._applied(r, seen, changes),
            )
            outcome.status = status_of(row)
            if resolved:
                outcome.verdict = RESOLVED
            else:
                calls._check_the_account(
                    session, outcome, case, f"order {order_id}: modify outcome unknown"
                )
        result = calls._finish(session, outcome, calls._response(session, "ModifyOrder", mark))
    if new_tag is not None and outcome.verdict in (ACKNOWLEDGED, RESOLVED):
        session.ledger.retag(order_id, str(new_tag))
    return result


async def _close_call(
    session: MatrixSession,
    client: AsyncOrderClient,
    kind: str,
    endpoint: str,
    order_id: str,
    case: CaseRef,
    send: Callable[[], Awaitable[object]],
    **detail: Any,
) -> OrderOutcome:
    """A cancel or an exit of one of our orders: one call; see ``calls._close_call``."""
    tag = calls._our_tag(session, order_id)
    if kind == "cancel" and session.ledger.cancel_sent(order_id):
        raise Refusal(
            f"order {order_id} was already cancelled once in this run; a CancelOrder is sent "
            "at most once per order"
        )
    session.guard.stopped()  # noted (sticky); a cancel or exit still goes out
    outcome = OrderOutcome(kind, case.case_id, tag, ACKNOWLEDGED, order_id=order_id)
    await session.order_pacer.wait_async(1, sleeper(session))  # shared with the sync calls
    mark = session.responses.mark()
    outcome.call = session.ledger.intent(
        tag,
        case.case_id,
        kind,
        segment=case.segment,
        order_id=order_id,
        client="async",
        **detail,
    )
    with calls.in_flight(session, outcome, case, lookup=False):
        try:
            await send()
        except RequestValidationError as exc:
            calls._settle(outcome, NOT_SENT, exc)
        except APIError as exc:
            calls._settle(outcome, REJECTED, exc)
        except Exception as exc:  # ambiguous: resolved by reading the book, never repeated
            calls._settle(outcome, None, exc)
            row, resolved = await _row_of(
                session, client, order_id, lambda r: is_final(status_of(r))
            )
            outcome.status = status_of(row)
            if resolved:
                outcome.verdict = RESOLVED
                session.guard.closed(order_id)
            else:
                calls._check_the_account(
                    session, outcome, case, f"order {order_id}: {kind} outcome unknown"
                )
        return calls._finish(session, outcome, calls._response(session, endpoint, mark))


async def cancel(
    session: MatrixSession, client: AsyncOrderClient, order_id: str, case: CaseRef
) -> OrderOutcome:
    """One ``cancel_order`` call of the async client for one of our orders (never repeated)."""

    async def send() -> object:
        return await client.cancel_order(order_id)

    return await _close_call(session, client, "cancel", "CancelOrder", order_id, case, send)


async def exit(  # the matrix name for exit_order (ExitSNOOrder)
    session: MatrixSession,
    client: AsyncOrderClient,
    order_id: str,
    product: str,
    case: CaseRef,
) -> OrderOutcome:
    """One ``exit_order`` call of the async client for one of our cover / bracket orders."""

    async def send() -> object:
        return await client.exit_order(order_id, product)

    return await _close_call(
        session, client, "exit", "ExitSNOOrder", order_id, case, send, product=product
    )


# -- raw bodies -------------------------------------------------------------------------------


def attach_responses(session: MatrixSession, client: object) -> bool:
    """Record the async client's raw order-endpoint bodies in the session's ``ResponseLog``
    (test-only: the SDK has no public hook for this); ``False`` when there is no HTTP client."""
    transport: Any = getattr(client, "_transport", None)
    http_client = getattr(transport, "http_client", None)
    if not isinstance(http_client, httpx.AsyncClient):
        return False

    async def hook(response: httpx.Response) -> None:
        session.responses.hook(response)

    hooks = http_client.event_hooks
    http_client.event_hooks = {
        "request": list(hooks.get("request", [])),
        "response": [*hooks.get("response", []), hook],
    }
    return True
