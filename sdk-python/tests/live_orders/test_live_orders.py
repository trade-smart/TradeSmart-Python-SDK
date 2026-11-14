"""Live order tests on the owner-approved zero-funds test account. Opt-in; see README.md.

Each order is a one-lot DAY LIMIT BUY well below the market and is expected to be rejected
(no funds). The safety rules live in ``safety.py`` and are pinned by
``tests/unit/test_live_orders_safety.py``.
"""

from __future__ import annotations

from typing import Any, NoReturn

import pytest

from tests.live_orders import safety
from tests.live_orders.safety import OrderOutcome, OrderPlan, OrderSession, Refusal
from tradesmart import OrderRejectedError
from tradesmart.models.orders import Order

pytestmark = pytest.mark.live_orders

SEGMENT_IDS = list(safety.SEGMENTS)  # "mcx", "nse", "nfo": `-k mcx` runs only MCX
FUNDED_SKIP = (
    "production-mode order tests do not run in funded mode (the account holds money: only "
    "test_funded_live.py sends orders there); skipped"
)


def _skip(record: dict[str, Any], reason: str) -> NoReturn:
    record["skipped"] = reason
    pytest.skip(reason)


def _production_only(session: OrderSession, record: dict[str, Any]) -> None:
    """Skip (before any API call) when the session is in funded mode."""
    if session.funded:
        _skip(record, FUNDED_SKIP)


def _plan(session: OrderSession, segment_id: str, record: dict[str, Any]) -> OrderPlan:
    record["segment"] = segment_id
    try:
        return safety.prepare_plan(
            session.client,
            safety.SEGMENTS[segment_id],
            record,
            uat=session.uat,
            any_hour=session.uat,  # UAT mode: any hour; production keeps its hours
        )
    except Refusal as exc:
        _skip(record, exc.reason)


def _run(
    session: OrderSession, plan: OrderPlan, record: dict[str, Any], **kwargs: Any
) -> OrderOutcome:
    try:
        outcome = safety.run_order(
            session.client,
            plan,
            session.budget,
            record,
            responses=session.responses,
            recorder=session.recorder,
            uat=session.uat,
            ignore_open=session.ignored_open,
            **kwargs,
        )
    except Refusal as exc:
        _skip(record, exc.reason)
    record["sdk_exception"] = outcome.exception
    record["final_status"] = outcome.final_status
    record["was_open"] = outcome.was_open
    record["cancelled_by_test"] = outcome.cancelled
    if outcome.definitive_rejection:
        record["error_mapping"] = "place_order raised " + str(outcome.exception)
    elif outcome.exception is not None:
        record["error_mapping"] = f"place_order raised {outcome.exception} (ambiguous)"
    else:
        record["error_mapping"] = f"place_order acknowledged; order book {outcome.final_status}"
    if outcome.final_status == "REJECTED" and outcome.order_id is not None:
        session.rejected[plan.segment_id] = outcome.order_id
    return outcome


@pytest.mark.parametrize("segment_id", SEGMENT_IDS)
def test_limit_buy_is_rejected_or_cancelled(
    segment_id: str, order_session: OrderSession, record: dict[str, Any]
) -> None:
    _production_only(order_session, record)
    plan = _plan(order_session, segment_id, record)
    outcome = _run(order_session, plan, record)
    assert outcome.settled, (
        f"order {outcome.order_id} ended as {outcome.final_status}; expected REJECTED or CANCELED"
    )


@pytest.mark.parametrize("segment_id", SEGMENT_IDS)
def test_modify_of_an_open_order(
    segment_id: str, order_session: OrderSession, record: dict[str, Any]
) -> None:
    """F9: the real ModifyOrder answer, if an order stays open long enough to modify it."""
    _production_only(order_session, record)
    plan = _plan(order_session, segment_id, record)

    def modify(order: Order, rec: dict[str, Any]) -> None:
        assert plan.tick_size is not None and plan.last_price is not None
        new_price = safety.modify_price(
            plan.price, plan.tick_size, plan.lower_circuit, plan.last_price
        )
        safety.modify_once(
            order_session.client,
            plan,
            str(order.order_id),
            new_price,
            rec,
            order_session.responses,
        )

    outcome = _run(order_session, plan, record, on_open=modify)
    assert outcome.settled, (
        f"order {outcome.order_id} ended as {outcome.final_status}; expected REJECTED or CANCELED"
    )
    if not outcome.was_open:
        ended = outcome.final_status or f"{outcome.exception} at place_order"
        _skip(record, f"no open order to modify: the order ended {ended} (recorded)")


@pytest.mark.parametrize("segment_id", SEGMENT_IDS)
def test_cancel_of_a_rejected_order_is_definitive(
    segment_id: str, order_session: OrderSession, record: dict[str, Any]
) -> None:
    """Cancelling an already rejected order never changes it (no new order, never filled).

    Observed live 2026-11-10: MCX refuses it in the reply (``Not_Ok`` → ``OrderRejectedError``);
    NSE/NFO answer ``stat: Ok`` and refuse it afterwards (the order stays REJECTED, its reason
    becomes "SAF:order is not open to cancel"). Both are definitive; either is recorded.
    """
    record["segment"] = segment_id
    _production_only(order_session, record)
    order_id = order_session.rejected.get(segment_id)
    if order_id is None:
        order_id = safety.rejected_order_of_suite(order_session.client.orders(), segment_id)
    if order_id is None:
        _skip(record, f"no rejected order of this suite for {segment_id.upper()} today")
    error = safety.cancel_rejected_once(
        order_session.client, order_id, record, order_session.responses
    )
    record["sdk_exception"] = None if error is None else type(error).__name__
    if error is not None:
        assert isinstance(error, OrderRejectedError), (
            f"expected a definitive OrderRejectedError, got {type(error).__name__}: {error}"
        )
    record["cancel_refused"] = "in reply" if error is not None else "after acknowledgement"
    assert record.get("status_after_cancel") == "REJECTED", (
        f"CHECK THE ACCOUNT: order {order_id} is {record.get('status_after_cancel')!r} after "
        "cancelling it; it was REJECTED before"
    )
