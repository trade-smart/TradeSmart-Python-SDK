"""The UAT sliced-order live test's order run (pinned by tests/unit/test_uat_sliced_helpers.py).

``place_sliced_order`` sends several orders from one call, so it cannot go through
``safety.run_order`` (exactly one order per call). ``run_sliced`` keeps the same rules for it, in
UAT mode only (never on production, never in funded mode):

- the shape: a DAY LIMIT BUY well below the market (``safety.prepare_plan``'s price), split into
  slices of ``slice_lots`` lots, at most as many slices as the run cap and the test's own cap
  leave room for (both spent before the call; ``max_slices`` is set to that number, so the SDK
  refuses rather than send more);
- no other open order on the account before the call (the slices of the call are the only orders
  open at the same time);
- one ``place_sliced_order`` call, never resent; after an ambiguous outcome, or an interruption
  other than Ctrl-C, the slices are looked up in ``orders()`` by their shared tag (after an
  exception other than ``OrderStatusUnknownError`` every slice must be found, else "CHECK THE
  ACCOUNT");
- every slice still open is cancelled once in a ``finally`` block and must reach a final state,
  else the test fails with "CHECK THE ACCOUNT"; a fill fails with "CHECK THE ACCOUNT" too.

The slices share one tag (``sdkslice-<hex>``), outside the suite's ``sdklive-`` prefix: the
lifecycle tag check flags any ``sdklive-`` tag on two orders as a resend.
"""

from __future__ import annotations

import secrets
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from tests.live_orders import lifecycle, safety
from tests.live_orders.safety import (
    CHECK_THE_ACCOUNT,
    FUNDED_ENV,
    WELL_BELOW,
    CheckTheAccount,
    OrderPlan,
    OrderSession,
    Refusal,
    is_final,
    status_of,
)
from tradesmart import SlicedOrderResult
from tradesmart.errors import (
    ConfigurationError,
    OrderStatusUnknownError,
    RateLimitError,
    RequestValidationError,
)
from tradesmart.models.orders import Order

SLICED_LOTS = 2  # the order: two lots ...
SLICE_LOTS = 1  # ... in slices of one lot: two orders
SLICED_TAG_PREFIX = "sdkslice-"
UAT_ONLY = "the sliced-order test runs only in UAT mode (never on production or in funded mode)"


def new_sliced_tag() -> str:
    """The tag every slice of one sliced order carries."""
    return f"{SLICED_TAG_PREFIX}{secrets.token_hex(3)}"


def sliced_session(
    environ: Mapping[str, str], open_session: Callable[[], OrderSession]
) -> OrderSession:
    """The run's session, only in UAT mode and never with the funded-mode flag set (any value);
    ``open_session`` is not called when the environment already rules it out."""
    if FUNDED_ENV in environ:
        raise Refusal(f"{UAT_ONLY}; {FUNDED_ENV} is set: skipped")
    try:
        session = lifecycle.uat_session(environ, open_session)
    except Refusal as exc:
        raise Refusal(f"{UAT_ONLY}: {exc.reason}") from exc
    if session.funded:
        raise Refusal(f"{UAT_ONLY}; the session is in funded mode: skipped")
    return session


@dataclass
class SlicedOutcome:
    result: SlicedOrderResult | None = None  # what place_sliced_order returned (or the partial)
    exception: str | None = None  # what place_sliced_order raised, if anything
    order_ids: tuple[str, ...] = ()  # every order of the call: acknowledged, then found by tag
    open_ids: tuple[str, ...] = ()  # the orders still open after the settle time
    cancelled: tuple[str, ...] = ()  # the orders a cancel request was accepted for
    final_statuses: dict[str, str | None] = field(default_factory=dict)


def _refusal_before_any_call(
    session: OrderSession, plan: OrderPlan, lots: int, slice_lots: int
) -> int:
    """The number of slices; refuses anything but a far DAY LIMIT BUY in UAT mode with room."""
    if session.funded or not session.uat:
        raise Refusal(UAT_ONLY)
    if not plan.is_production_shape:
        raise Refusal(
            "the sliced order is a DAY LIMIT BUY only; refusing "
            f"{plan.validity} {plan.order_type} {plan.transaction_type}"
        )
    if plan.last_price is None or plan.price > plan.last_price * WELL_BELOW:
        raise Refusal(
            f"the sliced order's price {plan.price} is not well below the last price "
            f"{plan.last_price}; refusing"
        )
    if not plan.tag.startswith(SLICED_TAG_PREFIX):
        raise Refusal(f"the slices need a {SLICED_TAG_PREFIX} tag (new_sliced_tag()); refusing")
    if lots < 1 or slice_lots < 1 or lots % slice_lots:
        raise Refusal(f"{lots} lots do not split into slices of {slice_lots} lots; refusing")
    slices = lots // slice_lots
    if session.test_budget is None:
        raise Refusal("no per-test order budget (session.test_budget); refusing")
    for budget, what in ((session.budget, "order cap"), (session.test_budget, "per-test cap")):
        if budget.limit - budget.used < slices:
            raise Refusal(
                f"{what} reached: {slices} slices need {slices} orders, "
                f"{budget.limit - budget.used} left"
            )
    return slices


def _refuse_open_orders(session: OrderSession) -> None:
    open_rows = [o for o in session.client.orders() if not is_final(status_of(o))]
    still_open = [o for o in open_rows if (o.order_id or "").strip() not in session.ignored_open]
    if still_open:
        ids = ", ".join(str(o.order_id) for o in still_open)
        raise Refusal(
            f"{CHECK_THE_ACCOUNT}: the account already has open order(s) {ids}; "
            "no sliced order while another order is open"
        )


def _describe(result: SlicedOrderResult) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "slice_quantities": list(result.slice_quantities),
        "order_ids": list(result.order_ids),
        "placed_quantity": result.placed_quantity,
        "unsent_quantity": result.unsent_quantity,
        "complete": result.complete,
    }
    if result.error is not None:
        entry["error"] = safety._error_record(result.error)
    return entry


@dataclass
class _Book:
    """What ``orders()`` showed for the orders of this call (by id and by the shared tag)."""

    tag: str
    known: list[str]
    rows: dict[str, Order] = field(default_factory=dict)  # order id -> the latest row
    seen: list[Order] = field(default_factory=list)  # every row seen (the fill check)
    read_ok: bool = False
    errors: list[str] = field(default_factory=list)

    def read(self, session: OrderSession) -> None:
        try:
            book = session.client.orders()
        except Exception as exc:
            self.errors.append(type(exc).__name__)
            return
        self.read_ok = True
        for order in book:
            order_id = (order.order_id or "").strip()
            if order_id and (order_id in self.known or (order.tag or "").strip() == self.tag):
                if order_id not in self.known:
                    self.known.append(order_id)
                self.rows[order_id] = order
                self.seen.append(order)

    def status(self, order_id: str) -> str | None:
        return status_of(self.rows.get(order_id))

    def all_final(self) -> bool:
        return bool(self.known) and all(is_final(self.status(i)) for i in self.known)


def _poll(
    session: OrderSession,
    book: _Book,
    seconds: float,
    poll_interval: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    deadline = clock() + seconds
    while True:
        book.read(session)
        if book.all_final() or clock() >= deadline:
            return
        sleep(poll_interval)


def _cancel_open(
    session: OrderSession,
    book: _Book,
    entry: dict[str, Any],
    outcome: SlicedOutcome,
    *,
    seconds: float,
    poll_interval: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """One cancel request per order not yet final (never retried), then wait for final states."""
    to_cancel = [i for i in book.known if not is_final(book.status(i))]
    outcome.open_ids = tuple(to_cancel)
    cancels: dict[str, Any] = {}
    accepted: list[str] = []
    for order_id in to_cancel:
        mark = session.responses.mark()
        try:
            session.client.cancel_order(order_id)
        except Exception as exc:
            cancels[order_id] = {"outcome": "error", **safety._error_record(exc)}
        else:
            cancels[order_id] = {"outcome": "accepted"}
            accepted.append(order_id)
        safety._attach_response(cancels[order_id], session.responses, "CancelOrder", mark)
    outcome.cancelled = tuple(accepted)
    if cancels:
        entry["cancel"] = cancels
        _poll(session, book, seconds, poll_interval, sleep, clock)
    outcome.final_statuses = {i: book.status(i) for i in book.known}
    entry["final_statuses"] = outcome.final_statuses
    if book.errors:
        entry["order_book_errors"] = book.errors
    if not cancels:
        return
    if not book.read_ok:
        raise CheckTheAccount(
            f"{CHECK_THE_ACCOUNT}: orders {', '.join(to_cancel)} (tag {book.tag}): status "
            "unreadable after the cancel requests"
        )
    still = [
        f"{i} {book.status(i) or 'not visible'}" for i in to_cancel if not is_final(book.status(i))
    ]
    if still:
        raise CheckTheAccount(
            f"{CHECK_THE_ACCOUNT}: order(s) still open after one cancel request each "
            f"(tag {book.tag}): {', '.join(still)}"
        )


def _filled(session: OrderSession, book: _Book, entry: dict[str, Any]) -> list[str]:
    """The orders of this call with a fill: in any row seen, or listed once in ``trades()``."""
    filled = {
        (o.order_id or "").strip()
        for o in book.seen
        if status_of(o) == safety.FILLED or (safety.fill_of(o) or 0) > 0
    }
    try:
        trades = session.client.trades()
    except Exception as exc:
        entry["trades_error"] = safety._error_record(exc)
    else:
        filled |= {(t.order_id or "").strip() for t in trades} & set(book.known)
    return sorted(filled)


def run_sliced(
    session: OrderSession,
    plan: OrderPlan,
    record: dict[str, Any],
    *,
    lots: int = SLICED_LOTS,
    slice_lots: int = SLICE_LOTS,
    settle_seconds: float = 8.0,
    poll_interval: float = 1.0,
    cancel_settle_seconds: float = 15.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> SlicedOutcome:
    """Send ``plan`` (one lot) as ``lots`` lots in slices of ``slice_lots`` lots, once; observe
    the slices, cancel every one still open, and report what happened (see the module doc).

    A refusal before sending (here, or ``ValueError`` / ``RequestValidationError`` from the SDK
    with no order of the tag in the book) is a ``Refusal``; an ambiguous outcome whose unknown
    slice is not in ``orders()`` is "CHECK THE ACCOUNT" (after the visible slices are cancelled).
    """
    slices = _refusal_before_any_call(session, plan, lots, slice_lots)
    _refuse_open_orders(session)
    assert session.test_budget is not None  # checked above
    for _ in range(slices):
        session.budget.reserve()
        session.test_budget.reserve()
    kwargs: dict[str, Any] = {
        **plan.place_kwargs(),
        "quantity": lots * plan.quantity,
        "max_slice_quantity": slice_lots * plan.quantity,
        "max_slices": slices,
    }
    entry: dict[str, Any] = {
        "plan": plan.describe(),
        "quantity": kwargs["quantity"],
        "max_slice_quantity": kwargs["max_slice_quantity"],
        "max_slices": slices,
        "orders_reserved": slices,
    }
    record["sliced"] = entry
    outcome = SlicedOutcome()
    unknown = False
    not_sent: BaseException | None = None
    interrupted = False
    look_up_tag = False  # a non-Ctrl-C interruption: look for the tag before cancelling
    book = _Book(plan.tag, [])
    try:
        try:
            result = session.client.place_sliced_order(**kwargs)
        except OrderStatusUnknownError as exc:
            unknown = True
            outcome.result = exc.sliced_result
            entry["place"] = {"outcome": "unknown", **safety._error_record(exc)}
            outcome.exception = type(exc).__name__
        except (ValueError, RequestValidationError, ConfigurationError) as exc:  # refused, not sent
            not_sent = exc
            entry["place"] = {"outcome": "not sent", **safety._error_record(exc)}
            outcome.exception = type(exc).__name__
        except RateLimitError as exc:
            # The SDK limiter refused up front (no HTTP status; a slice refused part-way is
            # returned in result.error, never raised): nothing was sent.
            if exc.http_status is not None or getattr(exc, "sliced_result", None) is not None:
                unknown = True
                entry["place"] = {"outcome": "unknown", **safety._error_record(exc)}
            else:
                not_sent = exc
                entry["place"] = {"outcome": "not sent", **safety._error_record(exc)}
            outcome.exception = type(exc).__name__
        except Exception as exc:  # it may have sent slices
            unknown = True
            entry["place"] = {"outcome": "unknown", **safety._error_record(exc)}
            outcome.exception = type(exc).__name__
        except BaseException as exc:  # Ctrl-C, SystemExit, a test timeout: slices may exist
            interrupted = isinstance(exc, KeyboardInterrupt)
            look_up_tag = not interrupted
            partial = getattr(exc, "sliced_result", None)  # attached by the SDK (spec 5.7)
            if isinstance(partial, SlicedOrderResult):
                book.known.extend(partial.order_ids)
            alert = (
                f"{CHECK_THE_ACCOUNT}: orders with tag {plan.tag} may exist; the sliced order "
                f"was interrupted during placement ({type(exc).__name__})"
            )
            print(alert, file=sys.stderr, flush=True)
            entry["place"] = {"outcome": "interrupted", "exception": type(exc).__name__}
            entry["alert"] = alert
            raise
        else:
            outcome.result = result
            entry["place"] = {"outcome": "complete" if result.complete else "stopped"}
        if outcome.result is not None:
            entry["place"].update(_describe(outcome.result))
            book.known.extend(outcome.result.order_ids)
        acknowledged = list(book.known)
        if not_sent is not None:
            book.read(session)  # one look: nothing should carry the tag
        else:
            _poll(session, book, settle_seconds, poll_interval, sleep, clock)
        found = [i for i in book.known if i not in acknowledged]
        if found:
            entry["found_by_tag"] = found
        entry["order_book"] = {
            i: {"status": book.status(i), "rejreason": book.rows[i].reject_reason}
            for i in book.known
            if i in book.rows
        }
    finally:
        if look_up_tag:
            book.read(session)  # the slices of the tag, also those sent but not acknowledged
        if not interrupted:
            _cancel_open(
                session,
                book,
                entry,
                outcome,
                seconds=cancel_settle_seconds,
                poll_interval=poll_interval,
                sleep=sleep,
                clock=clock,
            )
    outcome.order_ids = tuple(book.known)
    filled = _filled(session, book, entry)
    if filled:
        entry["filled"] = filled
        raise CheckTheAccount(
            f"{CHECK_THE_ACCOUNT}: order(s) {', '.join(filled)} (tag {plan.tag}) were (partly) "
            "filled; the UAT position is left open"
        )
    if not_sent is not None and not book.known:
        raise Refusal(f"the SDK refused the sliced order before sending: {not_sent}")
    if unknown:
        # After OrderStatusUnknownError one more order than acknowledged must be visible; after
        # any other exception the number sent is unknown, so every slice must be.
        if outcome.result is None:
            missing = len(book.known) < slices
        else:
            missing = len(book.known) <= len(outcome.result.order_ids)
        if missing:
            raise CheckTheAccount(
                f"{CHECK_THE_ACCOUNT}: tag {plan.tag}: a slice's outcome is unknown and it is not "
                "in orders()"
            )
    return outcome
