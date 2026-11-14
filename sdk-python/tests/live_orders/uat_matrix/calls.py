"""The never-resend wrappers: the only place the UAT matrix makes a state-changing call.

One SDK call per intent (``place``, ``place_sliced``, ``modify``, ``cancel``, ``exit``, and the
GTT calls ``gtt_place``, ``gtt_modify``, ``gtt_cancel`` at the end of this file), never
repeated, whatever happens:

- the ledger line ``intent`` (with a unique call id) is written and flushed before the call,
  ``outcome`` (echoing it) after;
- a definitive answer (``APIError``: ``stat Not_Ok``) is ``REJECTED``; client-side validation
  (``RequestValidationError``) is ``NOT_SENT``;
- an ambiguous outcome (``OrderStatusUnknownError``, a timeout, a network error) is resolved by
  **reading** the order book (read-only calls may be repeated): a place by its tag (``FOUND``), a
  modify / cancel / exit by the order's row (``RESOLVED``); unresolved means ``CHECK THE
  ACCOUNT``: the segment's circuit breaker trips and that order gets no further state-changing
  call from this run (the teardown cancel included). An interrupted call (Ctrl-C) is the same;
- modify, cancel and exit act only on **this run's own orders** (placed by ``place`` /
  ``place_sliced``, never an ignored stuck one) and use the ledger's tag of the order, never a
  caller's; anything else is refused before any call;
- place and modify are refused once the run is stopped (STOP file, a foreign open order) or
  the segment's breaker tripped; cancel and exit always go out (they only reduce what is open);
- each place call claims its ``MAX_OPEN`` slots atomically before sending and releases them after;
- in a mock session (``session.mock``): a place with ``amo`` set and every GTT write are refused
  before any call (``refuse_amo``; ``mock_session.NO_AMO_GTT``), and before each place, sliced
  place and modify the switch is checked again (``check_mock``: after midnight IST the run
  stops); cancels and exits never ask, so the teardown still clears ours.

``calls.place`` sets the order's tag itself (``ledger.new_tag``), so the ledger can always find
it; a plan is the ``place_order`` keyword arguments without ``tag``, as a mapping or an object
with ``place_kwargs()`` (``safety.OrderPlan`` style), and ``CaseRef`` names the case and its
segment (the circuit-breaker key).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Protocol

from tests.live_orders import mock_session
from tests.live_orders.safety import CHECK_THE_ACCOUNT, Refusal, is_final, status_of
from tests.live_orders.uat_matrix.guard import StopRun
from tests.live_orders.uat_matrix.ledger import (
    GTT_CANCEL,
    GTT_MODIFY,
    GTT_PLACE,
    INTERRUPTED,
    NOT_SENT,
    PLACE_KINDS,
    is_ours,
    read_book,
)
from tests.live_orders.uat_matrix.ledger import (
    REJECTED as REJECTED,  # re-exported: calls.REJECTED is the verdict word
)
from tradesmart.enums import OrderType, Product, Validity
from tradesmart.errors import APIError, ConfigurationError, RateLimitError, RequestValidationError
from tradesmart.models.gtt import Gtt
from tradesmart.models.orders import Order

if TYPE_CHECKING:
    from tests.live_orders.uat_matrix.gate import MatrixSession

ACKNOWLEDGED = "acknowledged"

FOUND = "unknown, found in the book"
RESOLVED = "unknown, resolved by the book"
CHECK = CHECK_THE_ACCOUNT


@dataclass(frozen=True)
class CaseRef:
    """The case a call belongs to: its id (the ledger's) and its segment (the breaker key)."""

    case_id: str
    segment: str


class SupportsPlaceKwargs(Protocol):
    def place_kwargs(self) -> dict[str, Any]: ...


PlanLike = Mapping[str, Any] | SupportsPlaceKwargs


@dataclass
class OrderOutcome:
    """What one call did (one per intent; the ledger has the same as an ``outcome`` line)."""

    kind: str
    case_id: str
    tag: str
    verdict: str
    order_id: str | None = None
    order_ids: tuple[str, ...] = ()  # place_sliced: every slice seen
    exception: str | None = None
    error: dict[str, Any] | None = None
    status: str | None = None  # the order-book status seen while resolving
    call: str | None = None  # the ledger's call id of the intent

    @property
    def check_the_account(self) -> bool:
        return self.verdict == CHECK

    @property
    def sent(self) -> bool:
        return self.verdict != NOT_SENT


@dataclass
class CancelReport:
    """What ``cancel_ours`` did: ``untouched`` open orders are not ours (or ignored)."""

    cancelled: list[str] = field(default_factory=list)
    not_final: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)
    untouched: list[str] = field(default_factory=list)
    stuck: list[str] = field(default_factory=list)  # noted stuck earlier: not cancelled again
    already_cancelled: list[str] = field(default_factory=list)  # one CancelOrder per order
    pending: list[str] = field(default_factory=list)  # a call of this process in flight
    missing: list[str] = field(default_factory=list)  # ours, never listed in the book read
    book_unreadable: bool = False
    gtts: GttCleanup | None = None  # the teardown's GTT cancel (None: the run placed no GTT)


# -- helpers ----------------------------------------------------------------------------------


def _plan_kwargs(plan: PlanLike) -> dict[str, Any]:
    kwargs = dict(plan) if isinstance(plan, Mapping) else dict(plan.place_kwargs())
    if "tag" in kwargs:
        raise ValueError("the plan must not carry a tag: calls.place sets the tag itself")
    return kwargs


def _error(exc: BaseException) -> dict[str, Any]:
    data: dict[str, Any] = {"exception": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, APIError):
        raw = exc.raw if isinstance(exc.raw, Mapping) else {}
        data["stat"] = raw.get("stat")
        data["emsg"] = exc.emsg
        data["http_status"] = exc.http_status  # None: raised by the SDK, no server answer
    return data


def _settle(outcome: OrderOutcome, verdict: str | None, exc: BaseException) -> None:
    """Note what the call raised (and the verdict, when it is already decided)."""
    if verdict is not None:
        outcome.verdict = verdict
    outcome.exception = type(exc).__name__
    outcome.error = _error(exc)


def _id(order: Order) -> str:
    return (order.order_id or "").strip()


def _check_issued(session: MatrixSession, tag: str) -> None:
    if session.ledger.sent(tag):
        raise ValueError(f"tag {tag} was already sent in this run: never resent")
    if not session.ledger.issued(tag):
        raise ValueError(f"tag {tag} was not issued by this run's ledger (ledger.new_tag)")


def check_mock(session: MatrixSession) -> None:
    """Before every place, sliced place and modify (sync and async; never a cancel or exit): in
    a mock session the switch is checked again (``MatrixSession.mock_refusal``: the IST date
    may have rolled over); a failed check stops the run (``StopRun``: no new order, ours are
    cancelled by the teardown). Nothing outside mock mode."""
    reason = session.mock_refusal()
    if reason is not None:
        session.guard.stop(reason)
        raise StopRun(reason)


def refuse_amo(session: MatrixSession, kwargs: Mapping[str, Any]) -> None:
    """In a mock session a place with ``amo`` set is refused before any call (an AMO outlives
    the session: ``mock_session.NO_AMO_GTT``)."""
    if session.mock and kwargs.get("amo"):
        raise Refusal(mock_session.NO_AMO_GTT)


def _refuse_gtt(session: MatrixSession) -> None:
    """In a mock session every GTT write is refused before any call (``NO_AMO_GTT``)."""
    if session.mock:
        raise Refusal(mock_session.NO_AMO_GTT)


def _refuse_case(session: MatrixSession, case: CaseRef) -> None:
    reason = session.case_done(case.case_id)
    if reason is not None:
        raise Refusal(f"case {case.case_id}: {reason}")


def _our_tag(session: MatrixSession, order_id: str) -> str:
    """The ledger's tag of one of our orders; refuses any other order before any call."""
    if order_id in session.ignored_open:
        raise Refusal(
            f"order {order_id} is an ignored stuck order (TRADESMART_LIVE_ORDER_UAT_IGNORE_OPEN); "
            "it is never touched"
        )
    tag = session.ledger.tag_of(order_id)
    if tag is None:
        raise Refusal(
            f"order {order_id} is not an order of this run (not placed by it); refusing to "
            "send a state-changing call for it"
        )
    if order_id in session.ledger.flagged() or session.ledger.tag_flagged(tag):
        raise Refusal(
            f"{CHECK_THE_ACCOUNT}: order {order_id} had an unresolved call in this run; no "
            "further state-changing call is made for it"
        )
    if session.ledger.pending_order(order_id):
        raise Refusal(
            f"order {order_id} has a call in flight in this run (an intent without its outcome "
            "yet); no other call is made for it meanwhile"
        )
    return tag


@contextmanager
def in_flight(
    session: MatrixSession,
    outcome: OrderOutcome,
    case: CaseRef,
    *,
    lookup: bool,
    opened: list[str] | None = None,
) -> Iterator[None]:
    """Everything from a call's intent to its outcome. Whatever leaves it without the outcome
    written still writes one and flags the order and its tag: an interruption (Ctrl-C, a
    cancelled task) anywhere in it, the read-only lookup included, is ``interrupted``; an
    ordinary exception (a bug, an ``OSError`` of the ledger) is CHECK THE ACCOUNT and trips the
    segment; when even that cannot be written (or the CHECK step itself raises), the order and
    its tag are flagged in memory (as in ``gtt_in_flight``). The original exception is re-raised
    either way."""
    try:
        yield
    except BaseException as exc:
        call = outcome.call
        if call and session.ledger.call_pending(call):
            try:
                if isinstance(exc, Exception):
                    _unknown_after_intent(session, outcome, case, exc)
                else:
                    _interrupted(session, outcome, exc, lookup=lookup)
            except Exception:  # the ledger cannot write: no further call for it here
                _mark_unknown(session, outcome)
            if opened is not None:
                opened.extend(outcome.order_ids)
        raise


def _unknown_after_intent(
    session: MatrixSession, outcome: OrderOutcome, case: CaseRef, exc: Exception
) -> None:
    """An exception between the intent and the outcome: the call may have gone out and its
    result is unknown. CHECK THE ACCOUNT (outcome written, order and tag flagged, segment
    tripped); when even that outcome cannot be written, the flags are kept in memory."""
    what = (
        f"{outcome.kind} of tag {outcome.tag}: raised after its intent "
        f"({type(exc).__name__}: {exc}); its result is unknown"
    )
    _check_the_account(session, outcome, case, what)
    try:
        _finish(session, outcome, {"alert": f"{CHECK_THE_ACCOUNT}: {what}", "raised": _error(exc)})
    except Exception:  # the ledger cannot write: no further call for it in this process
        _mark_unknown(session, outcome)


def _response(session: MatrixSession, endpoint: str, mark: int) -> dict[str, Any]:
    seen = session.responses.last(endpoint, since=mark)
    return {} if seen is None else {"response": seen}


def _rows_with_tag(session: MatrixSession, tag: str, want: int = 1) -> tuple[list[Order], bool]:
    """Read the book (read-only) until ``want`` rows carry ``tag`` or the reads run out."""
    rows: list[Order] = []
    read_ok = False
    for index in range(session.lookup_attempts):
        if index:
            session.sleep(session.lookup_interval)
        try:
            book = session.client.orders()
        except Exception:  # noqa: S112 - a read; the next read decides
            continue
        read_ok = True
        rows = [o for o in book if (o.tag or "").strip() == tag]
        if len(rows) >= want:
            break
    return rows, read_ok


def _row_of(
    session: MatrixSession, order_id: str, done: Callable[[Order], bool]
) -> tuple[Order | None, bool]:
    """Read the book (read-only) until ``done(row)`` holds for the order's row."""
    row: Order | None = None
    for index in range(session.lookup_attempts):
        if index:
            session.sleep(session.lookup_interval)
        try:
            book = session.client.orders()
        except Exception:  # noqa: S112 - a read; the next read decides
            continue
        row = next((o for o in book if _id(o) == order_id), None)
        if row is not None and done(row):
            return row, True
    return row, False


def _finish(session: MatrixSession, outcome: OrderOutcome, extra: dict[str, Any]) -> OrderOutcome:
    detail: dict[str, Any] = dict(extra)
    if outcome.exception is not None:
        detail["error"] = outcome.error
    if outcome.status is not None:
        detail["status"] = outcome.status
    session.ledger.outcome(
        outcome.tag,
        outcome.order_id,
        outcome.verdict,
        kind=outcome.kind,
        call=outcome.call,
        order_ids=outcome.order_ids,
        **detail,
    )
    return outcome


def _check_the_account(
    session: MatrixSession, outcome: OrderOutcome, case: CaseRef, what: str
) -> None:
    outcome.verdict = CHECK
    session.guard.note_check_the_account(case.segment, what)


def _interrupted(
    session: MatrixSession, outcome: OrderOutcome, exc: BaseException, lookup: bool
) -> None:
    """Record an interrupted call (Ctrl-C, a test timeout): the order may exist; it is flagged
    (no further call, not even the teardown cancel). After Ctrl-C the session is marked
    ``interrupted``: its teardown cancels nothing and lists ours for the cleanup tool."""
    _settle(outcome, INTERRUPTED, exc)
    if isinstance(exc, KeyboardInterrupt):
        session.interrupted = True
    extra: dict[str, Any] = {"alert": f"{CHECK_THE_ACCOUNT}: interrupted during the call"}
    if lookup and not isinstance(exc, KeyboardInterrupt):
        book = read_book(session.client, attempts=1)
        if book is not None:
            ids = [_id(o) for o in book if (o.tag or "").strip() == outcome.tag and _id(o)]
            outcome.order_ids = tuple(dict.fromkeys([*outcome.order_ids, *ids]))
    try:
        _finish(session, outcome, extra)
    except Exception:  # the ledger cannot write: flagged in memory; the interruption stays
        _mark_unknown(session, outcome)


def _mark_unknown(session: MatrixSession, outcome: OrderOutcome) -> None:
    """The outcome could not be written: flag the tag and the order ids in memory."""
    session.ledger.mark_unknown(outcome.tag, [outcome.order_id, *outcome.order_ids])


# -- place ------------------------------------------------------------------------------------


def place(
    session: MatrixSession, plan: PlanLike, case: CaseRef, *, tag: str | None = None
) -> OrderOutcome:
    """One ``place_order`` call for ``plan`` (never resent)."""
    kwargs = _plan_kwargs(plan)
    refuse_amo(session, kwargs)  # mock session: never an AMO
    check_mock(session)  # mock session: the switch again (date rollover)
    if tag is not None:
        _check_issued(session, tag)  # a programming error is refused before anything else
    _refuse_case(session, case)
    session.guard.check_send(case.segment)  # claims one MAX_OPEN slot
    opened: list[str] = []
    try:
        return _place(session, kwargs, case, tag, opened)
    finally:
        session.guard.release(1, *opened)


def _place(
    session: MatrixSession,
    kwargs: dict[str, Any],
    case: CaseRef,
    tag: str | None,
    opened: list[str],
) -> OrderOutcome:
    session.guard.spend(case.case_id)
    tag = tag if tag is not None else session.ledger.new_tag(case.case_id)
    outcome = OrderOutcome("place", case.case_id, tag, ACKNOWLEDGED)
    session.order_pacer.wait()  # every state-changing call: 6/s (ALGO_CHK counts all)
    session.guard.check_new(case.segment)  # again: a STOP file or a trip during the wait
    check_mock(session)  # again, right before the intent
    mark = session.responses.mark()
    outcome.call = session.ledger.intent(
        tag, case.case_id, "place", segment=case.segment, plan=kwargs
    )
    with in_flight(session, outcome, case, lookup=True, opened=opened):
        try:
            order_id = session.client.place_order(**kwargs, tag=tag)
        except RequestValidationError as exc:
            _settle(outcome, NOT_SENT, exc)
        except APIError as exc:  # stat Not_Ok: a definitive answer
            _settle(outcome, REJECTED, exc)
        except Exception as exc:  # ambiguous: it may exist; look it up by tag, never resend
            _settle(outcome, None, exc)
            rows, read_ok = _rows_with_tag(session, tag)
            if rows:
                outcome.verdict = FOUND
                outcome.order_id = _id(rows[0]) or None
                outcome.status = status_of(rows[0])
            else:
                why = "it is not in orders()" if read_ok else "the order book could not be read"
                _check_the_account(
                    session, outcome, case, f"order tag {tag}: place outcome unknown and {why}"
                )
        except BaseException as exc:
            _interrupted(session, outcome, exc, lookup=True)
            opened.extend(outcome.order_ids)
            raise
        else:
            outcome.order_id = order_id
        if outcome.order_id is not None and not is_final(outcome.status):
            opened.append(outcome.order_id)
        return _finish(session, outcome, _response(session, "PlaceOrder", mark))


_BEFORE_SENDING = (RequestValidationError, ValueError, ConfigurationError)


def _refused_before_sending(exc: BaseException) -> bool:
    """The SDK refused the call before any slice went out.

    Its own up-front checks, and the client-side rate limiter (a `RateLimitError` with no HTTP
    status: place_sliced_order raises it only before the first slice; a slice refused part-way
    is returned in `result.error`, never raised).
    """
    if isinstance(exc, RateLimitError):
        return exc.http_status is None
    return isinstance(exc, _BEFORE_SENDING)


def place_sliced(
    session: MatrixSession,
    plan: PlanLike,
    case: CaseRef,
    *,
    max_slices: int,
    max_slice_quantity: int | None = None,
    tag: str | None = None,
) -> OrderOutcome:
    """One ``place_sliced_order`` call (all slices share one tag; never resent).

    The case spends ``max_slices`` from its declared count, and claims ``max_slices``
    ``MAX_OPEN`` slots, before anything is sent.
    """
    kwargs = _plan_kwargs(plan)
    refuse_amo(session, kwargs)  # mock session: never an AMO
    check_mock(session)  # mock session: the switch again (date rollover)
    if tag is not None:
        _check_issued(session, tag)
    _refuse_case(session, case)
    session.guard.check_send(case.segment, max_slices)
    opened: list[str] = []
    try:
        return _place_sliced(session, kwargs, case, max_slices, max_slice_quantity, tag, opened)
    finally:
        session.guard.release(max_slices, *opened)


def _place_sliced(
    session: MatrixSession,
    kwargs: dict[str, Any],
    case: CaseRef,
    max_slices: int,
    max_slice_quantity: int | None,
    tag: str | None,
    opened: list[str],
) -> OrderOutcome:
    session.guard.spend(case.case_id, max_slices)
    tag = tag if tag is not None else session.ledger.new_tag(case.case_id)
    outcome = OrderOutcome("place_sliced", case.case_id, tag, ACKNOWLEDGED)
    session.order_pacer.wait(max_slices)  # the SDK sends up to max_slices calls
    session.guard.check_new(case.segment)  # again: a STOP file or a trip during the wait
    check_mock(session)  # again, right before the intent
    outcome.call = session.ledger.intent(
        tag,
        case.case_id,
        "place_sliced",
        segment=case.segment,
        plan=kwargs,
        max_slices=max_slices,
        max_slice_quantity=max_slice_quantity,
    )
    with in_flight(session, outcome, case, lookup=True, opened=opened):
        try:
            result = session.client.place_sliced_order(
                **kwargs, tag=tag, max_slices=max_slices, max_slice_quantity=max_slice_quantity
            )
        except Exception as exc:  # never resend; the book decides what exists
            _settle(outcome, None, exc)
            partial = getattr(exc, "sliced_result", None)
            known = tuple(partial.order_ids) if partial is not None else ()
            # Documented as raised before any slice is sent, unless a partial result says so.
            before_sending = _refused_before_sending(exc) and partial is None
            outcome.order_ids = known  # kept if the lookup is interrupted
            rows, read_ok = _rows_with_tag(session, tag, want=len(known) + 1)
            seen = tuple(dict.fromkeys([*known, *(_id(o) for o in rows if _id(o))]))
            outcome.order_ids = seen
            if len(seen) > len(known):
                outcome.verdict = FOUND
            elif before_sending and read_ok:
                outcome.verdict = NOT_SENT
            else:
                why = "it is not in orders()" if read_ok else "the order book could not be read"
                _check_the_account(
                    session, outcome, case, f"order tag {tag}: a slice's outcome unknown and {why}"
                )
        except BaseException as exc:
            partial = getattr(exc, "sliced_result", None)
            if partial is not None:
                outcome.order_ids = tuple(partial.order_ids)
            _interrupted(session, outcome, exc, lookup=True)
            opened.extend(outcome.order_ids)
            raise
        else:
            outcome.order_ids = tuple(result.order_ids)
            if not result.complete:
                outcome.verdict = REJECTED
                if result.error is not None:
                    _settle(outcome, None, result.error)
        opened.extend(outcome.order_ids)
        return _finish(session, outcome, {})


# -- modify / cancel / exit -------------------------------------------------------------------


def _same(name: str, a: Any, b: Any) -> bool:
    """Whether two values of a modify field mean the same (wire words and enum names alike)."""
    if a is None or b is None:
        return a is None and b is None
    try:
        if name == "order_type":
            return OrderType(str(a)) == OrderType(str(b))
        if name == "product":
            return Product(str(a)) == Product(str(b))
        if name == "validity":
            return Validity(str(a)) == Validity(str(b))
        if name == "quantity":
            return int(a) == int(b)
        return Decimal(str(a)) == Decimal(str(b))
    except (ValueError, InvalidOperation):
        return str(a).strip().upper() == str(b).strip().upper()


_MODIFY_FIELDS = ("price", "trigger_price", "quantity", "order_type", "product", "validity")


def _applied(row: Order, before: Order | None, changes: Mapping[str, Any]) -> bool:
    """The book row shows every requested field, and at least one of them differs from the
    row read before the modify (else the row cannot tell an applied modify from none)."""
    if before is None:
        return False
    asked = [name for name in _MODIFY_FIELDS if changes.get(name) is not None]
    if not asked:
        return False
    if not all(_same(name, getattr(row, name), changes[name]) for name in asked):
        return False
    return any(not _same(name, getattr(before, name), changes[name]) for name in asked)


def modify(
    session: MatrixSession,
    order_id: str,
    case: CaseRef,
    *,
    before: Order | None = None,
    **changes: Any,
) -> OrderOutcome:
    """One ``modify_order`` call (``changes`` are its keyword arguments); never repeated.

    Only for one of our orders. ``before`` is its order-book row before the modify (read here
    when not given): an ambiguous outcome is resolved only when the book then shows a change.
    A ``tag`` change must use a tag issued by ``ledger.new_tag``.
    """
    check_mock(session)  # mock session: the switch again (date rollover), before any read
    tag = _our_tag(session, order_id)
    new_tag = changes.get("tag")
    if new_tag is not None:
        _check_issued(session, str(new_tag))
    session.guard.check_new(case.segment)
    if before is None:
        book = read_book(session.client, attempts=1)
        before = next((o for o in book or [] if _id(o) == order_id), None)
        session.guard.check_new(case.segment)  # again: the STOP file or a trip during the read
    outcome = OrderOutcome("modify", case.case_id, tag, ACKNOWLEDGED, order_id=order_id)
    session.order_pacer.wait()  # every state-changing call: 6/s (ALGO_CHK counts all)
    session.guard.check_new(case.segment)  # again: a STOP file or a trip during the wait
    check_mock(session)  # again, right before the intent
    mark = session.responses.mark()
    outcome.call = session.ledger.intent(
        tag, case.case_id, "modify", segment=case.segment, order_id=order_id, changes=changes
    )
    with in_flight(session, outcome, case, lookup=False):
        try:
            session.client.modify_order(order_id, **changes)
        except RequestValidationError as exc:
            _settle(outcome, NOT_SENT, exc)
        except APIError as exc:
            _settle(outcome, REJECTED, exc)
        except Exception as exc:  # ambiguous: resolved by reading the book, never repeated
            _settle(outcome, None, exc)
            row, resolved = _row_of(
                session,
                order_id,
                lambda r: is_final(status_of(r)) or _applied(r, before, changes),
            )
            outcome.status = status_of(row)
            if resolved:
                outcome.verdict = RESOLVED
            else:
                what = f"order {order_id}: modify outcome unknown"
                _check_the_account(session, outcome, case, what)
        except BaseException as exc:
            _interrupted(session, outcome, exc, lookup=False)
            raise
        result = _finish(session, outcome, _response(session, "ModifyOrder", mark))
    if new_tag is not None and outcome.verdict in (ACKNOWLEDGED, RESOLVED):
        session.ledger.retag(order_id, str(new_tag))
    return result


def _close_call(
    session: MatrixSession,
    kind: str,
    endpoint: str,
    order_id: str,
    case: CaseRef,
    send: Callable[[], object],
    **detail: Any,
) -> OrderOutcome:
    """A cancel or an exit of one of our orders: one call; an ambiguous one is resolved when
    the book shows the order final, else CHECK THE ACCOUNT. Allowed when stopped or tripped."""
    tag = _our_tag(session, order_id)
    if kind == "cancel" and session.ledger.cancel_sent(order_id):
        raise Refusal(
            f"order {order_id} was already cancelled once in this run; a CancelOrder is sent "
            "at most once per order"
        )
    session.guard.stopped()  # noted (sticky); a cancel or exit still goes out
    outcome = OrderOutcome(kind, case.case_id, tag, ACKNOWLEDGED, order_id=order_id)
    session.order_pacer.wait()  # every state-changing call: 6/s (ALGO_CHK counts all)
    mark = session.responses.mark()
    outcome.call = session.ledger.intent(
        tag, case.case_id, kind, segment=case.segment, order_id=order_id, **detail
    )
    with in_flight(session, outcome, case, lookup=False):
        try:
            send()
        except RequestValidationError as exc:
            _settle(outcome, NOT_SENT, exc)
        except APIError as exc:
            _settle(outcome, REJECTED, exc)
        except Exception as exc:  # ambiguous: resolved by reading the book, never repeated
            _settle(outcome, None, exc)
            row, resolved = _row_of(session, order_id, lambda r: is_final(status_of(r)))
            outcome.status = status_of(row)
            if resolved:
                outcome.verdict = RESOLVED
                session.guard.closed(order_id)
            else:
                what = f"order {order_id}: {kind} outcome unknown"
                _check_the_account(session, outcome, case, what)
        except BaseException as exc:
            _interrupted(session, outcome, exc, lookup=False)
            raise
        return _finish(session, outcome, _response(session, endpoint, mark))


def cancel(session: MatrixSession, order_id: str, case: CaseRef) -> OrderOutcome:
    """One ``cancel_order`` call for one of our orders (never repeated for this intent)."""

    def send() -> object:
        return session.client.cancel_order(order_id)

    return _close_call(session, "cancel", "CancelOrder", order_id, case, send)


def exit(  # the matrix name for exit_order (ExitSNOOrder)
    session: MatrixSession, order_id: str, product: str, case: CaseRef
) -> OrderOutcome:
    """One ``exit_order`` call for one of our cover / bracket orders (never repeated)."""

    def send() -> object:
        return session.client.exit_order(order_id, product)

    return _close_call(session, "exit", "ExitSNOOrder", order_id, case, send, product=product)


# -- open orders of the run, stop, teardown ---------------------------------------------------


def open_ours(session: MatrixSession) -> tuple[set[str], set[str]]:
    """One book read (read-only): (our non-final order ids, every order id listed); for
    ``Guard.wait_for_room``. Unreadable: the guard's own open set and nothing listed."""
    book = read_book(
        session.client, attempts=1, interval=session.lookup_interval, sleep=session.sleep
    )
    if book is None:
        return set(session.guard.open_ids), set()
    ours = {
        _id(o) for o in book if _id(o) and not is_final(status_of(o)) and is_ours(o, session.ledger)
    }
    return ours, {_id(o) for o in book if _id(o)}


def cancel_ours(session: MatrixSession) -> CancelReport:
    """Cancel every open order of this run, then confirm each reaches a final state.

    One CancelOrder per order for the whole session (a batch cancel, ``stop_run`` and the
    teardown together): an order already cancelled once is only confirmed again
    (``already_cancelled``), one noted stuck is skipped (``stuck``), and a book row listed twice
    is handled once. Never touches an order that is not ours, nor an ignored (stuck on UAT) one,
    nor one with an unknown outcome (CHECK THE ACCOUNT, interrupted, or an intent without its
    outcome). An order still open after ``confirm_seconds`` is stuck: its segment's breaker trips
    and a ``stuck`` line goes to the ledger.
    """
    report = CancelReport()
    ledger = session.ledger
    book = read_book(
        session.client,
        attempts=session.lookup_attempts,
        interval=session.lookup_interval,
        sleep=session.sleep,
    )
    if book is None:
        report.book_unreadable = True
        ledger.note("cleanup", book_unreadable=True)
        return report
    listed = {_id(row) for row in book if _id(row)}
    report.missing = sorted(ledger.ours() - listed)  # acknowledged, never in the book
    sent: dict[str, str] = {}  # order id -> segment, waiting for a final state
    seen: set[str] = set()
    for row in book:
        order_id, row_tag = _id(row), (row.tag or "").strip()
        if not order_id or order_id in seen or is_final(status_of(row)):
            continue
        seen.add(order_id)  # a row listed twice is handled once
        if not is_ours(row, ledger) or order_id in session.ignored_open:
            report.untouched.append(order_id)
            continue
        if order_id in ledger.flagged() or ledger.tag_flagged(row_tag):
            report.flagged.append(order_id)
            continue
        if ledger.pending_order(order_id) or ledger.pending_tag(row_tag, kinds=PLACE_KINDS):
            report.pending.append(order_id)  # a call of this process is in flight: not now
            continue
        if not ledger.adopt(row):  # ours by id, or listed under one of our place tags
            report.untouched.append(order_id)
            continue
        tag = ledger.tag_of(order_id)
        if tag is None or ledger.tag_flagged(tag):
            report.flagged.append(order_id)
            continue
        segment = ledger.segment_of(tag) or "unknown"
        if ledger.stuck(order_id):  # cancelled once and never final: a human looks at it
            report.stuck.append(order_id)
            continue
        if ledger.cancel_sent(order_id):  # cancelled once already (a batch, a stop): confirm
            report.already_cancelled.append(order_id)
            sent[order_id] = segment
            continue
        case = CaseRef(ledger.case_of(tag) or "cleanup", segment)
        try:
            outcome = cancel(session, order_id, case)
        except Refusal:
            report.flagged.append(order_id)
            continue
        if outcome.check_the_account:
            report.flagged.append(order_id)
            continue
        if outcome.verdict in (ACKNOWLEDGED, RESOLVED):
            report.cancelled.append(order_id)
        sent[order_id] = case.segment
    _confirm(session, sent, report)
    return report


def _confirm(session: MatrixSession, sent: dict[str, str], report: CancelReport) -> None:
    waiting = dict(sent)
    deadline = session.clock() + session.confirm_seconds
    while waiting:
        book = read_book(session.client, attempts=1)
        if book is not None:
            for row in book:
                if _id(row) in waiting and is_final(status_of(row)):
                    del waiting[_id(row)]
                    session.guard.closed(_id(row))
        if not waiting or session.clock() >= deadline:
            break
        session.sleep(session.poll_interval)
    for order_id, segment in waiting.items():
        report.not_final.append(order_id)
        session.guard.note_stuck(segment, order_id)
        session.ledger.note("stuck", order_id=order_id, segment=segment)


def stop_run(session: MatrixSession, stop: StopRun) -> CancelReport:
    """Stop the run (every remaining item skips with the reason) and cancel ours once."""
    # Orders only: a GTT of the run is cancelled by its own case (cases_gtt) and, if still
    # pending, once at the session's teardown (cancel_our_gtts); a stop sends no new GTT call.
    session.guard.stop(stop.reason)
    session.ledger.note("stop", reason=stop.reason, foreign=list(stop.foreign))
    report = cancel_ours(session)
    session.ledger.note(
        "stop_report",
        cancelled=report.cancelled,
        not_final=report.not_final,
        flagged=report.flagged,
        pending=report.pending,
        missing=report.missing,
        untouched=report.untouched,
        book_unreadable=report.book_unreadable,
    )
    return report


def teardown(session: MatrixSession) -> CancelReport | None:
    """The session's end: cancel every pending GTT of ours once (``cancel_our_gtts``, only when
    the run placed a GTT), then cancel our orders once and confirm (an order a GTT of ours
    placed included); after Ctrl-C cancel nothing and list ours (orders, tags, alert ids) for
    the cleanup tool (``None``), as an interrupted run must not send more calls."""
    ledger = session.ledger
    if session.interrupted:
        ledger.note(
            "teardown",
            skipped="interrupted (Ctrl-C): nothing cancelled; run the cleanup tool",
            ours=sorted(ledger.ours()),
            flagged=sorted(ledger.flagged()),
            tags=sorted(ledger.tags()),
            flagged_tags=sorted(ledger.flagged_tags()),
            alerts=sorted(ledger.alerts()),
            gtt_tags=sorted(ledger.gtt_tags()),
        )
        return None
    gtts = cancel_our_gtts(session) if ledger.gtt_tags() else None
    report = cancel_ours(session)
    report.gtts = gtts
    extra: dict[str, Any] = {}
    if gtts is not None:
        extra = {
            "gtts_cancelled": gtts.cancelled,
            "gtts_left": gtts.left,
            "gtts_flagged": gtts.flagged,
            "gtts_unreadable": gtts.unreadable,
        }
    ledger.note(
        "teardown",
        cancelled=report.cancelled,
        not_final=report.not_final,
        flagged=report.flagged,
        pending=report.pending,
        missing=report.missing,
        untouched=report.untouched,
        book_unreadable=report.book_unreadable,
        **extra,
    )
    return report


# -- GTTs (family M13) --------------------------------------------------------------------------
#
# The same rules as the order wrappers above, for PlaceGTTOrder / ModifyGTTOrder /
# CancelGTTOrder: one call per intent, never repeated; the ledger ``intent`` (kind ``gtt_place``
# / ``gtt_modify`` / ``gtt_cancel``) before and the ``outcome`` (with the alert id) after; an
# ``APIError`` is REJECTED, ``RequestValidationError`` NOT_SENT; an ambiguous outcome is resolved
# by **reading** ``pending_gtts()`` (and, for a place, ``triggered_gtts()``: a GTT whose condition
# is already true fires at once) by the GTT's tag, else CHECK THE ACCOUNT and the segment's
# breaker trips. A GTT is ours only from our own place outcome or a lookup by our GTT tag; modify
# and cancel refuse any other alert id, and one with an unknown outcome; one cancel per alert id
# per run; the GTT's tag is always the ledger's (``remarks``), never a caller's.

GTT_FACTOR_FIELDS = ("trigger_value", "price", "trigger_price", "quantity")


@dataclass
class GttOutcome:
    """What one GTT call did (one per intent; the ledger has the same as an ``outcome`` line)."""

    kind: str  # gtt_place / gtt_modify / gtt_cancel
    case_id: str
    tag: str
    verdict: str
    alert_id: str | None = None  # the GTT's alert id (place: as acknowledged or looked up)
    returned: str | None = None  # modify / cancel: the alert id the server acknowledged
    exception: str | None = None
    error: dict[str, Any] | None = None
    call: str | None = None

    @property
    def check_the_account(self) -> bool:
        return self.verdict == CHECK

    @property
    def sent(self) -> bool:
        return self.verdict != NOT_SENT


@dataclass
class GttCleanup:
    """What ``cancel_our_gtts`` did; ``left`` lists our GTTs still pending (or never confirmed
    gone): CHECK THE ACCOUNT for them."""

    cancelled: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)  # unknown outcome earlier: not touched
    untouched: list[str] = field(default_factory=list)  # pending GTTs that are not ours
    unreadable: bool = False


def _gtt_settle(outcome: GttOutcome, verdict: str | None, exc: BaseException) -> None:
    if verdict is not None:
        outcome.verdict = verdict
    outcome.exception = type(exc).__name__
    outcome.error = _error(exc)


def _gtt_finish(
    session: MatrixSession, outcome: GttOutcome, extra: Mapping[str, Any]
) -> GttOutcome:
    detail: dict[str, Any] = dict(extra)
    if outcome.exception is not None:
        detail["error"] = outcome.error
    if outcome.returned is not None:
        detail["returned"] = outcome.returned
    session.ledger.outcome(
        outcome.tag,
        None,
        outcome.verdict,
        kind=outcome.kind,
        call=outcome.call,
        alert_id=outcome.alert_id,
        **detail,
    )
    return outcome


def _gtt_id(gtt: Any) -> str:
    return (getattr(gtt, "alert_id", None) or "").strip()


def read_gtts(session: MatrixSession, *, triggered: bool = False) -> list[Gtt] | None:
    """``pending_gtts()`` (or ``triggered_gtts()``), read once (read-only); ``None`` when
    unreadable."""
    try:
        if triggered:
            return list(session.client.triggered_gtts())
        return list(session.client.pending_gtts())
    except Exception:  # a read; the caller decides
        return None


def gtts_with_tag(
    session: MatrixSession, tag: str, *, attempts: int | None = None
) -> tuple[list[Gtt], bool]:
    """Read ``pending_gtts()`` (read-only) until a row carries ``tag`` or the reads run out:
    (the rows of ``tag``, whether any read succeeded)."""
    rows: list[Gtt] = []
    read_ok = False
    for index in range(session.lookup_attempts if attempts is None else attempts):
        if index:
            session.sleep(session.lookup_interval)
        pending = read_gtts(session)
        if pending is None:
            continue
        read_ok = True
        rows = [g for g in pending if (g.tag or "").strip() == tag]
        if rows:
            break
    return rows, read_ok


def _our_alert(session: MatrixSession, alert_id: str) -> str:
    """The ledger's GTT tag of one of our alert ids; refuses any other before any call."""
    tag = session.ledger.alert_tag(alert_id)
    if tag is None:
        raise Refusal(
            f"alert {alert_id} is not a GTT of this run (not placed or looked up by it); "
            "refusing to send a GTT call for it"
        )
    if session.ledger.alert_flagged(alert_id):
        raise Refusal(
            f"{CHECK_THE_ACCOUNT}: GTT {alert_id} had an unknown outcome in this run; no further "
            "GTT call is made for it"
        )
    if session.ledger.pending_tag(tag):
        raise Refusal(
            f"{CHECK_THE_ACCOUNT}: GTT {alert_id} (tag {tag}) has a call in flight (an intent "
            "without its outcome); no further GTT call is made for it"
        )
    return tag


def _look_up_or_interrupt(
    session: MatrixSession, outcome: GttOutcome, case: CaseRef, look_up: Callable[[], None]
) -> dict[str, Any]:
    """Run the read-only lookup of an ambiguous GTT call; returns notes for its outcome.

    A failure inside the lookup itself (an ``Exception``: a bug, the reads already swallow
    their own errors) leaves the outcome unresolved: CHECK THE ACCOUNT (the segment's breaker
    trips), the error noted as ``lookup_error``. An interruption during it (Ctrl-C, a test
    timeout: any other ``BaseException``) still writes the call's outcome (``interrupted``:
    the tag and alert id are flagged) before it propagates."""
    try:
        look_up()
    except Exception as exc:
        what = f"GTT tag {outcome.tag}: {outcome.kind} outcome unknown (the lookup failed)"
        _check_the_account_gtt(session, outcome, case, what)
        return {"lookup_error": _error(exc)}
    except BaseException as exc:
        call_error = outcome.error
        _gtt_interrupted(
            session,
            outcome,
            exc,
            {"call_error": call_error, "alert": f"{CHECK_THE_ACCOUNT}: interrupted while "
             f"looking up the outcome of the {outcome.kind}"},
        )  # fmt: skip
        raise
    return {}


def gtt_place(session: MatrixSession, plan: Mapping[str, Any], case: CaseRef) -> GttOutcome:
    """One ``place_gtt`` call for ``plan`` (its keywords without ``tag``; never resent).

    Refused when the case is done, the run is stopped or the segment tripped; it spends one of
    the case's declared count. The tag is the ledger's (``ledger.new_tag``). Never in a mock
    session (``NO_AMO_GTT``; refused before any call).
    """
    _refuse_gtt(session)
    kwargs = dict(plan)
    if "tag" in kwargs:
        raise ValueError("the plan must not carry a tag: calls.gtt_place sets the tag itself")
    _refuse_case(session, case)
    session.guard.check_new(case.segment)
    session.guard.spend(case.case_id)
    tag = session.ledger.new_tag(case.case_id)
    outcome = GttOutcome(GTT_PLACE, case.case_id, tag, ACKNOWLEDGED)
    session.order_pacer.wait()  # every state-changing call: 6/s (ALGO_CHK counts all)
    session.guard.check_new(case.segment)  # again: a STOP file or a trip during the wait
    mark = session.responses.mark()
    outcome.call = session.ledger.intent(
        tag, case.case_id, GTT_PLACE, segment=case.segment, plan=kwargs
    )
    with gtt_in_flight(session, outcome, case):
        result = _send_gtt_place(session, kwargs, case, outcome, mark)
    return result


def _send_gtt_place(
    session: MatrixSession,
    kwargs: dict[str, Any],
    case: CaseRef,
    outcome: GttOutcome,
    mark: int,
) -> GttOutcome:
    """``gtt_place`` from its intent to its outcome (inside ``gtt_in_flight``)."""
    tag = outcome.tag
    extra: dict[str, Any] = {}
    try:
        alert_id = session.client.place_gtt(**kwargs, tag=tag)
    except RequestValidationError as exc:
        _gtt_settle(outcome, NOT_SENT, exc)
    except APIError as exc:  # stat Not_Ok: a definitive answer
        _gtt_settle(outcome, REJECTED, exc)
    except Exception as exc:  # ambiguous: it may exist (or have fired); look it up by tag
        _gtt_settle(outcome, None, exc)

        def look_up() -> None:
            found, where, read_ok = _look_up_gtt(session, tag)
            if found is not None:
                outcome.verdict = FOUND
                outcome.alert_id = found
                extra["found_in"] = where
            else:
                why = "it is in neither GTT list" if read_ok else "the GTT lists could not be read"
                _check_the_account_gtt(
                    session, outcome, case, f"GTT tag {tag}: place outcome unknown and {why}"
                )

        extra.update(_look_up_or_interrupt(session, outcome, case, look_up))
    except BaseException as exc:
        # The outcome is always written (``finally``), whatever happens during the one read
        # below; it keeps the place's own exception as its error.
        _gtt_settle(outcome, INTERRUPTED, exc)
        extra["alert"] = f"{CHECK_THE_ACCOUNT}: interrupted during the GTT place"
        if isinstance(exc, KeyboardInterrupt):
            session.interrupted = True
        try:
            if not isinstance(exc, KeyboardInterrupt):  # one read to record what exists
                rows, _ = gtts_with_tag(session, tag, attempts=1)
                outcome.alert_id = next((_gtt_id(g) for g in rows if _gtt_id(g)), None)
        except BaseException as during:  # interrupted again (Ctrl-C) during that read
            if isinstance(during, KeyboardInterrupt):
                session.interrupted = True
            extra["lookup_interrupted"] = type(during).__name__
            raise
        finally:
            _gtt_finish(session, outcome, extra)
        raise
    else:
        outcome.alert_id = (alert_id or "").strip() or None
    extra.update(_response(session, "PlaceGTTOrder", mark))
    return _gtt_finish(session, outcome, extra)


def _look_up_gtt(session: MatrixSession, tag: str) -> tuple[str | None, str | None, bool]:
    """(alert id, list, any read succeeded) of the GTT carrying ``tag``: ``pending_gtts()``
    up to ``lookup_attempts`` times, then ``triggered_gtts()`` once (it may have fired)."""
    rows, read_ok = gtts_with_tag(session, tag)
    found = next((_gtt_id(g) for g in rows if _gtt_id(g)), None)
    if found is not None:
        return found, "pending", True
    fired = read_gtts(session, triggered=True)
    if fired is not None:
        read_ok = True
        found = next(
            (_gtt_id(g) for g in fired if (g.tag or "").strip() == tag and _gtt_id(g)), None
        )
        if found is not None:
            return found, "triggered", True
    return None, None, read_ok


def _check_the_account_gtt(
    session: MatrixSession, outcome: GttOutcome, case: CaseRef, what: str
) -> None:
    outcome.verdict = CHECK
    session.guard.note_check_the_account(case.segment, what)


def _same_value(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (ValueError, InvalidOperation):
        return str(a).strip() == str(b).strip()


def _gtt_applied(row: Gtt, before: Gtt | None, changes: Mapping[str, Any]) -> bool:
    """The pending row shows every requested value, and at least one differs from the row read
    before the modify (else it cannot tell an applied modify from none)."""
    if before is None:
        return False
    asked = [name for name in GTT_FACTOR_FIELDS if changes.get(name) is not None]
    if not asked:
        return False
    if not all(_same_value(getattr(row, name), changes[name]) for name in asked):
        return False
    return any(not _same_value(getattr(before, name), changes[name]) for name in asked)


def gtt_modify(
    session: MatrixSession,
    alert_id: str,
    case: CaseRef,
    *,
    before: Gtt | None = None,
    **fields: Any,
) -> GttOutcome:
    """One ``modify_gtt`` call of one of our GTTs (``fields``: every ``modify_gtt`` keyword but
    ``tag``, which is the ledger's); never repeated. ``before`` is its pending row before the
    modify (read here when not given): an ambiguous outcome is resolved only when the pending
    row then shows the change. Refused when stopped or tripped, and in a mock session."""
    _refuse_gtt(session)
    if "tag" in fields:
        raise ValueError("calls.gtt_modify sends the ledger's tag of the GTT, never a caller's tag")
    tag = _our_alert(session, alert_id)
    session.guard.check_new(case.segment)
    if before is None:
        pending = read_gtts(session)
        before = next((g for g in pending or [] if _gtt_id(g) == alert_id), None)
    outcome = GttOutcome(GTT_MODIFY, case.case_id, tag, ACKNOWLEDGED, alert_id=alert_id)
    session.order_pacer.wait()  # every state-changing call: 6/s (ALGO_CHK counts all)
    session.guard.check_new(case.segment)  # again: a STOP file or a trip during the wait
    mark = session.responses.mark()
    outcome.call = session.ledger.intent(
        tag, case.case_id, GTT_MODIFY, segment=case.segment, alert_id=alert_id, changes=fields
    )
    with gtt_in_flight(session, outcome, case):
        result = _send_gtt_modify(session, alert_id, case, outcome, mark, before, fields)
    return result


def _send_gtt_modify(
    session: MatrixSession,
    alert_id: str,
    case: CaseRef,
    outcome: GttOutcome,
    mark: int,
    before: Gtt | None,
    fields: dict[str, Any],
) -> GttOutcome:
    """``gtt_modify`` from its intent to its outcome (inside ``gtt_in_flight``)."""
    tag = outcome.tag
    notes: dict[str, Any] = {}
    try:
        returned = session.client.modify_gtt(alert_id, **fields, tag=tag)
    except RequestValidationError as exc:
        _gtt_settle(outcome, NOT_SENT, exc)
    except APIError as exc:
        _gtt_settle(outcome, REJECTED, exc)
    except Exception as exc:  # ambiguous: resolved by reading the pending list, never repeated
        _gtt_settle(outcome, None, exc)

        def look_up() -> None:
            if _gtt_resolved(session, lambda rows: any(
                _gtt_id(g) == alert_id and _gtt_applied(g, before, fields) for g in rows
            )):  # fmt: skip
                outcome.verdict = RESOLVED
            else:
                what = f"GTT {alert_id}: modify outcome unknown"
                _check_the_account_gtt(session, outcome, case, what)

        notes.update(_look_up_or_interrupt(session, outcome, case, look_up))
    except BaseException as exc:
        _gtt_interrupted(session, outcome, exc)
        raise
    else:
        outcome.returned = (returned or "").strip() or None
    extra = {**_response(session, "ModifyGTTOrder", mark), **notes}
    if outcome.returned is not None and outcome.returned != alert_id:
        extra["returned_differs"] = True
    return _gtt_finish(session, outcome, extra)


def _gtt_resolved(session: MatrixSession, done: Callable[[list[Gtt]], bool]) -> bool:
    """Read ``pending_gtts()`` (read-only) up to ``lookup_attempts`` times until ``done``."""
    for index in range(session.lookup_attempts):
        if index:
            session.sleep(session.lookup_interval)
        pending = read_gtts(session)
        if pending is not None and done(pending):
            return True
    return False


def _gtt_interrupted(
    session: MatrixSession,
    outcome: GttOutcome,
    exc: BaseException,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Write the ``interrupted`` outcome (the ledger flags the tag and alert id); after Ctrl-C
    the session is marked ``interrupted`` (its teardown sends nothing)."""
    _gtt_settle(outcome, INTERRUPTED, exc)
    if isinstance(exc, KeyboardInterrupt):
        session.interrupted = True
    detail = {"alert": f"{CHECK_THE_ACCOUNT}: interrupted during the {outcome.kind}"}
    detail.update(extra or {})
    _gtt_finish(session, outcome, detail)


@contextmanager
def gtt_in_flight(session: MatrixSession, outcome: GttOutcome, case: CaseRef) -> Iterator[None]:
    """The GTT counterpart of ``in_flight``: everything from a GTT call's intent to its outcome.
    Whatever leaves it with the intent still pending writes an outcome and flags the GTT's tag
    and alert id: an interruption is ``interrupted``; an ordinary exception (a bug, an
    ``OSError`` of the ledger) is CHECK THE ACCOUNT and trips the segment; when even that
    outcome cannot be written, the tag is flagged in memory. The exception is re-raised."""
    try:
        yield
    except BaseException as exc:
        call = outcome.call
        if call and session.ledger.call_pending(call):
            try:
                if isinstance(exc, Exception):
                    what = (
                        f"{outcome.kind} of GTT tag {outcome.tag}: raised after its intent "
                        f"({type(exc).__name__}: {exc}); its result is unknown"
                    )
                    _check_the_account_gtt(session, outcome, case, what)
                    _gtt_finish(
                        session,
                        outcome,
                        {"alert": f"{CHECK_THE_ACCOUNT}: {what}", "raised": _error(exc)},
                    )
                else:
                    _gtt_interrupted(session, outcome, exc)
            except Exception:  # the ledger cannot write: no further call for it here
                session.ledger.mark_unknown(outcome.tag)
        raise


def gtt_cancel(session: MatrixSession, alert_id: str, case: CaseRef) -> GttOutcome:
    """One ``cancel_gtt`` call for one of our GTTs: at most one per alert id per run (a NOT_SENT
    one excepted); allowed when stopped or tripped (it only removes risk). An ambiguous outcome
    is resolved when ``pending_gtts()`` no longer lists the alert id, else CHECK THE ACCOUNT.
    Refused in a mock session (it places no GTT, so it has none to cancel)."""
    _refuse_gtt(session)
    tag = _our_alert(session, alert_id)
    if session.ledger.gtt_cancel_sent(alert_id):
        raise Refusal(
            f"GTT {alert_id} was already cancelled once in this run; a CancelGTTOrder is sent "
            "at most once per GTT"
        )
    session.guard.stopped()  # noted (sticky); a cancel still goes out
    outcome = GttOutcome(GTT_CANCEL, case.case_id, tag, ACKNOWLEDGED, alert_id=alert_id)
    session.order_pacer.wait()  # every state-changing call: 6/s (ALGO_CHK counts all)
    mark = session.responses.mark()
    outcome.call = session.ledger.intent(
        tag, case.case_id, GTT_CANCEL, segment=case.segment, alert_id=alert_id
    )
    with gtt_in_flight(session, outcome, case):
        result = _send_gtt_cancel(session, alert_id, case, outcome, mark)
    return result


def _send_gtt_cancel(
    session: MatrixSession, alert_id: str, case: CaseRef, outcome: GttOutcome, mark: int
) -> GttOutcome:
    """``gtt_cancel`` from its intent to its outcome (inside ``gtt_in_flight``)."""
    notes: dict[str, Any] = {}
    try:
        returned = session.client.cancel_gtt(alert_id)
    except RequestValidationError as exc:
        _gtt_settle(outcome, NOT_SENT, exc)
    except APIError as exc:
        _gtt_settle(outcome, REJECTED, exc)
    except Exception as exc:  # ambiguous: resolved when the GTT is no longer pending
        _gtt_settle(outcome, None, exc)

        def look_up() -> None:
            if _gtt_resolved(session, lambda rows: all(_gtt_id(g) != alert_id for g in rows)):
                outcome.verdict = RESOLVED
            else:
                what = f"GTT {alert_id}: cancel outcome unknown"
                _check_the_account_gtt(session, outcome, case, what)

        notes.update(_look_up_or_interrupt(session, outcome, case, look_up))
    except BaseException as exc:
        _gtt_interrupted(session, outcome, exc)
        raise
    else:
        outcome.returned = (returned or "").strip() or None
    extra = {**_response(session, "CancelGTTOrder", mark), **notes}
    if outcome.returned is not None and outcome.returned != alert_id:
        extra["returned_differs"] = True
    return _gtt_finish(session, outcome, extra)


def cancel_our_gtts(session: MatrixSession) -> GttCleanup:
    """Cancel every pending GTT of this run once, then list ours still pending (``left``).

    Ours: an alert id of this run, or a pending row carrying one of this run's GTT tags (a
    lookup: adopted first). Never touches another GTT, one with an unknown outcome or a call in
    flight (``flagged``) nor one already cancelled once (still pending: ``left``). An unreadable
    list cancels nothing and lists every alert id of ours as ``left``. Every one left (a
    flagged one still pending included) gets a ``gtt_left`` line (CHECK THE ACCOUNT) in the
    ledger.

    No hour check here (ruling): a cancel only removes risk, so the teardown cancels our GTTs
    outside market hours too, and while ``TRADESMART_LIVE_ORDER_OUTSIDE_HOURS`` is set, which
    refuses every new GTT write (``cases_gtt.write_refusal``).
    """
    report = GttCleanup()
    ledger = session.ledger
    pending = _read_gtts_retrying(session)
    if pending is None:
        report.unreadable = True
        report.left = sorted(ledger.alerts())
        _note_left(session, report.left, "pending_gtts() could not be read at the teardown")
        return report
    tags = ledger.gtt_tags()
    for row in pending:
        alert_id, row_tag = _gtt_id(row), (row.tag or "").strip()
        if not alert_id:
            continue
        if ledger.alert_tag(alert_id) is None and row_tag in tags and ledger.pending_tag(row_tag):
            report.flagged.append(alert_id)  # a call of that tag is in flight: never touched
            continue
        if ledger.alert_tag(alert_id) is None and not (
            row_tag in tags and ledger.adopt_gtt(alert_id, row_tag)
        ):
            report.untouched.append(alert_id)
            continue
        if ledger.alert_flagged(alert_id):
            report.flagged.append(alert_id)
            continue
        if ledger.gtt_cancel_sent(alert_id):
            continue  # cancelled once already: confirmed below
        tag = ledger.alert_tag(alert_id) or row_tag
        case = CaseRef(ledger.case_of(tag) or "cleanup", ledger.segment_of(tag) or "unknown")
        try:
            outcome = gtt_cancel(session, alert_id, case)
        except Refusal:
            report.flagged.append(alert_id)
            continue
        if outcome.verdict in (ACKNOWLEDGED, RESOLVED):
            report.cancelled.append(alert_id)
    after = _read_gtts_retrying(session)
    ours = ledger.alerts() | set(report.flagged)  # flagged: ours, or of a tag in flight
    if after is None:
        report.unreadable = True
        report.left = sorted(ours)
        _note_left(session, report.left, "pending_gtts() could not be read after the cancel")
        return report
    report.left = sorted({_gtt_id(g) for g in after} & ours)
    _note_left(session, report.left, "still pending after the teardown")
    return report


def _read_gtts_retrying(session: MatrixSession) -> list[Gtt] | None:
    for index in range(session.lookup_attempts):
        if index:
            session.sleep(session.lookup_interval)
        pending = read_gtts(session)
        if pending is not None:
            return pending
    return None


def _note_left(session: MatrixSession, alert_ids: list[str], why: str) -> None:
    for alert_id in alert_ids:
        session.ledger.note(
            "gtt_left",
            alert_id=alert_id,
            tag=session.ledger.alert_tag(alert_id),
            alert=f"{CHECK_THE_ACCOUNT}: GTT {alert_id} of this run: {why}",
        )
