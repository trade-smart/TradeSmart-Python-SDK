"""Live order tests on a small funded production account (funded mode only; see README.md).

They skip unless funded mode holds (``TRADESMART_LIVE_ORDER_FUNDED=1`` on the production REST
host, the balance ceiling and every gate of the order suite: both opt-ins, the account check,
serial runs, market hours). On top of the order cap (10 per run, 2 per test), one open order at
a time, one place call per order (never resent) and a cancel in ``finally`` while an order is
open, every order is held to the money budget (``funded.MoneyBudget``): at most 2,000 rupees per
order and 5,000 for the BUY orders of a run. Only LIMIT and SL-LMT orders are sent, product MIS,
1 share (cash) or 1 lot (options).

Stage 1 (the ``test_stage1_*`` tests, first in this file) never means to fill: LIMIT BUYs far
below the market (rested, modified, cancelled), an IOC far below (never rests) and a stop-limit
BUY above the market (trigger pending, modified, cancelled). An unexpected fill is sold back at
once and the test fails. Stage 2 (``test_stage2_*``, last) buys 1 share or 1 lot at the best
offer and sells the fill back at the best bid; it runs only with ``TRADESMART_LIVE_ORDER_FILLS=1``
and when no stage-1 test of the run failed (``funded.stage_two_reason``), and sends its BUY only
before 14:30 IST (``funded.stage_two_time_reason``: later, it skips and sends nothing). A BUY
the broker rejects fails the test (it is not a "did not fill" skip).

Any "CHECK THE ACCOUNT" stops the run: every later funded test fails at its start, before any
API call, until the owner has checked the account. The bodies are dry-run against the offline
offline broker by ``tests/integration/test_funded_live_offline_broker.py``.
"""

from __future__ import annotations

import dataclasses
import functools
import os
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any, NoReturn, TypeVar, cast

import pytest

from tests.live_orders import conftest as live_conftest
from tests.live_orders import funded, lifecycle, safety
from tests.live_orders import test_uat_lifecycle as uat
from tests.live_orders.safety import (
    CHECK_THE_ACCOUNT,
    CheckTheAccount,
    OrderBudget,
    OrderOutcome,
    OrderPlan,
    OrderSession,
    Refusal,
    ceil_to_tick,
)
from tradesmart.models.orders import Order

pytestmark = pytest.mark.live_orders

SEGMENT_IDS = list(funded.FUNDED_SEGMENTS)  # nse, bse, nfoopt, bfoopt
CASH_IDS = [s for s in SEGMENT_IDS if s not in funded.OPTION_IDS]  # IOC and SL-LMT: cash only
STREAM_WAIT_SECONDS = 15.0  # for the stream's final update after the order book shows it
# A stage-1 BUY (and its modified price) stays at most 97 % of the last price, also when it is
# raised to the lower circuit: otherwise the case is skipped and nothing is sent.
STAGE1_MAX_RATIO = Decimal("0.97")
FUNDED_ONLY = (
    f"funded live order tests run only in funded mode ({safety.FUNDED_ENV}=1 on the production "
    "server, with every other live order gate); skipped"
)
STOPPED = (
    f"{CHECK_THE_ACCOUNT}: an earlier test of this run left the account to be checked; no "
    "order sent (check the account, then start a new run)"
)

F = TypeVar("F", bound=Callable[..., None])


# -- the test's start and end (the fixture; the dry run calls them the same way) -----------


def _left_open(session: OrderSession) -> dict[str, int]:
    """What ``session.fills`` holds: bought by a test and not (surely) sent to be sold back."""
    return {symbol: quantity for symbol, quantity in session.fills.items() if quantity != 0}


def _stop_for_open_position(session: OrderSession, left: dict[str, int], when: str) -> NoReturn:
    session.stopped = True
    names = ", ".join(f"{symbol} ({quantity})" for symbol, quantity in sorted(left.items()))
    raise CheckTheAccount(
        f"{CHECK_THE_ACCOUNT}: position left open: {names} bought and not sold back {when}; "
        "the run is stopped and no further order is sent (close it by hand)"
    )


def begin_funded_test(session: OrderSession) -> None:
    """Before any call of a funded test: refuse once the run is stopped, and stop it when a fill
    of an earlier test is still held (never forgotten). Then give the test its own cap
    (``funded.FUNDED_PER_TEST_CAP``); ``session.fills`` is empty, so a SELL only closes a BUY of
    this test."""
    if session.stopped:
        raise CheckTheAccount(STOPPED)
    left = _left_open(session)
    if left:
        _stop_for_open_position(session, left, "by an earlier test")
    session.test_budget = OrderBudget(funded.FUNDED_PER_TEST_CAP)
    session.fills.clear()


def end_funded_test(session: OrderSession, record: dict[str, Any]) -> None:
    """After a funded test, whatever its outcome: record it; a fill it did not sell back stops
    the run ("CHECK THE ACCOUNT: position left open")."""
    budget = session.test_budget
    session.test_budget = None
    record["orders_sent_by_test"] = 0 if budget is None else budget.used
    record["money_committed"] = None if session.money is None else str(session.money.committed)
    left = _left_open(session)
    if left:
        record["left_open"] = dict(left)
        session.stopped = True
    record["stopped"] = session.stopped
    if left:
        _stop_for_open_position(session, left, "in this test")


@pytest.fixture
def funded_session(request: pytest.FixtureRequest) -> OrderSession:
    """The order session in funded mode; skips (before any API call) without the funded opt-in."""
    if os.environ.get(safety.FUNDED_ENV) != "1":
        pytest.skip(FUNDED_ONLY)
    session = cast(OrderSession, request.getfixturevalue("order_session"))
    if not session.funded:
        pytest.skip(FUNDED_ONLY)
    return session


@pytest.fixture
def funded_record(
    request: pytest.FixtureRequest, funded_session: OrderSession
) -> Iterator[dict[str, Any]]:
    """What the test observed; written as one JSON line when the test ends (pass or not).

    Fails the test at its start once the run is stopped (CHECK THE ACCOUNT). A stage-1 test that
    failed (setup or call, from the kept reports) marks the session: stage 2 must not run.
    """
    session = funded_session
    data: dict[str, Any] = {
        "test": request.node.name,
        "raw_bodies_recorded": session.raw_bodies,
        "uat_mode": session.uat,
        "funded_mode": session.funded,
    }
    try:
        begin_funded_test(session)
    except CheckTheAccount:
        session.recorder.write({**data, "refused": STOPPED})
        raise
    try:
        yield data
    finally:
        try:
            end_funded_test(session, data)  # a fill not sold back: CHECK THE ACCOUNT, stopped
        finally:
            if live_conftest.is_stage1_item(request.node) and live_conftest.stage_one_failed(
                request.node
            ):
                session.stage1_failed = True
            session.recorder.write(data)


def _stops_the_run(test: F) -> F:
    """A "CHECK THE ACCOUNT" raised by the test stops the run (``session.stopped``)."""

    @functools.wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        try:
            test(*args, **kwargs)
        except CheckTheAccount:
            for value in (*args, *kwargs.values()):
                if isinstance(value, OrderSession):
                    value.stopped = True
            raise

    return cast(F, run)


# -- helpers -----------------------------------------------------------------------------


def _skip(record: dict[str, Any], reason: str) -> NoReturn:
    record["skipped"] = reason
    pytest.skip(reason)


def _plan(session: OrderSession, segment_id: str, record: dict[str, Any]) -> OrderPlan:
    """The segment's MIS LIMIT BUY of 1 share or 1 lot (read-only calls only; the price is set
    by each test)."""
    record["segment"] = segment_id
    segment = funded.FUNDED_SEGMENTS[segment_id]
    if session.max_balance is None:
        _skip(record, "funded mode without a balance ceiling; no order placed")
    missing = lifecycle.script_master_reason(session.client, segment.exchange)
    if missing is not None:
        _skip(record, missing)
    try:
        if segment_id in funded.OPTION_IDS:
            plan = safety.prepare_plan(
                session.client,
                segment,
                record,
                funded_ceiling=session.max_balance,
                pick=funded.pick_option_by_premium,
            )
        else:
            plan = safety.prepare_plan(
                session.client, segment, record, funded_ceiling=session.max_balance
            )
    except Refusal as exc:
        _skip(record, exc.reason)
    if segment_id not in funded.OPTION_IDS and plan.quantity != 1:
        _skip(record, f"{plan.tradingsymbol}: lot size {plan.quantity}, not 1 share; no order")
    return plan


def stage1_prices(
    last_price: Decimal | None,
    tick: Decimal | None,
    lower_circuit: Decimal | None,
    ratio: Decimal,
) -> tuple[Decimal, Decimal]:
    """``(price, modified price)`` of a stage-1 BUY far below the market.

    The price is ``funded.far_buy_price`` (the last price times ``ratio``, floored to the tick),
    raised to the lower circuit (rounded up to the tick) when ``lower_circuit`` is above 0 (an
    order below it would be rejected). Both prices stay far: at most ``STAGE1_MAX_RATIO`` (97 %)
    of the last price and at least one tick below it; ``Refusal`` (nothing is sent) when the
    price is not. The modified price is one tick lower while that stays above 0 and within the
    lower circuit, else one tick higher while that is still far; ``Refusal`` when neither fits.
    """
    if last_price is None or tick is None:
        raise Refusal(f"no last price or tick size (got {last_price}, {tick}); no order placed")
    price = funded.far_buy_price(last_price, tick, ratio)
    circuit = lower_circuit is not None and lower_circuit > 0
    if circuit and lower_circuit is not None and price < lower_circuit:
        price = ceil_to_tick(lower_circuit, tick)

    def far(value: Decimal) -> bool:
        return value <= last_price * STAGE1_MAX_RATIO and value + tick <= last_price

    if not far(price):
        raise Refusal(
            f"the lowest allowed price {price} (lower circuit {lower_circuit}) is not at least "
            f"3 % below the last price {last_price} (and one tick); no order placed"
        )
    down = price - tick
    if down > 0 and (not circuit or (lower_circuit is not None and down >= lower_circuit)):
        return price, down
    up = price + tick
    if far(up):
        return price, up
    raise Refusal(
        f"no second price far below the last price {last_price} for the modify (price {price}, "
        f"lower circuit {lower_circuit}); no order placed"
    )


def _far_prices(plan: OrderPlan, record: dict[str, Any]) -> tuple[Decimal, Decimal]:
    kind = "option" if plan.segment_id in funded.OPTION_IDS else "cash"
    try:
        price, modified = stage1_prices(
            plan.last_price, plan.tick_size, plan.lower_circuit, funded.STAGE1_RATIO[kind]
        )
    except Refusal as exc:
        _skip(record, exc.reason)
    record["stage1_prices"] = {"price": price, "modified": modified, "ratio": kind}
    return price, modified


def _run(
    session: OrderSession, plan: OrderPlan, record: dict[str, Any], **kwargs: Any
) -> OrderOutcome:
    """``lifecycle.run_tracked``; a refusal skips the test (nothing was sent), except one that
    says CHECK THE ACCOUNT (an order already open in the account): that stops the run."""
    try:
        outcome = lifecycle.run_tracked(session, plan, record, **kwargs)
    except Refusal as exc:
        if CHECK_THE_ACCOUNT in exc.reason:
            raise CheckTheAccount(exc.reason) from exc
        _skip(record, exc.reason)
    record["sdk_exception"] = outcome.exception
    record["final_status"] = outcome.final_status
    record["was_open"] = outcome.was_open
    record["cancelled_by_test"] = outcome.cancelled
    record["filled"] = outcome.filled
    return outcome


def _skip_from_the_cut_off(record: dict[str, Any]) -> None:
    """No stage-2 BUY from 14:30 IST (``funded.stage_two_time_reason``): skipped, nothing sent."""
    reason = funded.stage_two_time_reason(funded.now_ist())
    if reason is not None:
        _skip(record, reason)


def _ended(outcome: OrderOutcome) -> str:
    return outcome.final_status or f"{outcome.exception} at place_order"


def _close_unexpected_fill(
    session: OrderSession, plan: OrderPlan, outcome: OrderOutcome, record: dict[str, Any]
) -> None:
    """A stage-1 order must not fill: sell back what it filled (at the best bid), then fail."""
    if not outcome.filled:
        return
    if outcome.order_id is not None:
        quantity = lifecycle.close_quantity(session, plan, outcome, record)
        lifecycle.close_position(
            session, plan, quantity, record, net_before=record["net_before"], use_depth=True
        )
    raise AssertionError(
        f"stage 1: order {outcome.order_id} ({plan.tradingsymbol}) filled unexpectedly "
        f"({_ended(outcome)}); the fill was sold back"
    )


def _require_open(outcome: OrderOutcome, record: dict[str, Any], what: str) -> None:
    if not outcome.was_open:
        place = record.get("place", {})
        why = record.get("order_book", {}).get("rejreason") or place.get("emsg") or ""
        raise AssertionError(
            f"the {what} order {outcome.order_id} was never open: it ended {_ended(outcome)}"
            + (f" ({why})" if why else "")
        )


# -- stage 1: LIMIT far below the market: open, modify, cancel; three views agree ----------


@pytest.mark.parametrize("segment_id", SEGMENT_IDS)
@_stops_the_run
def test_stage1_far_limit_open_modified_cancelled(
    segment_id: str, funded_session: OrderSession, funded_record: dict[str, Any]
) -> None:
    session, record = funded_session, funded_record
    plan = _plan(session, segment_id, record)
    price, new_price = _far_prices(plan, record)
    plan = dataclasses.replace(plan, price=price)
    updates = lifecycle.OrderUpdates()
    stream = uat._start_order_stream(session, updates, record)

    def modify(order: Order, rec: dict[str, Any]) -> None:
        order_id = str(order.order_id)
        safety.modify_once(session.client, plan, order_id, new_price, rec, session.responses)
        rec["modify"]["price_in_book"] = uat._book_shows(session, order_id, new_price)

    order_id: str | None = None
    try:
        outcome = _run(session, plan, record, on_open=modify)
        order_id = outcome.order_id
        if order_id is not None:
            lifecycle.wait_until(
                lambda: any(safety.is_final(s) for s in updates.statuses(order_id)),
                STREAM_WAIT_SECONDS,
            )
    finally:
        stream.stop()
    stream_statuses = [] if order_id is None else updates.statuses(order_id)
    record["stream_statuses"] = stream_statuses
    _close_unexpected_fill(session, plan, outcome, record)
    _require_open(outcome, record, "far LIMIT")

    modify_entry = record["modify"]
    assert modify_entry["outcome"] == "acknowledged", f"modify_order: {modify_entry}"
    in_book = modify_entry["price_in_book"]
    assert in_book["matches"], (
        f"orders() shows price {in_book['price']} after the modify, "
        f"expected {modify_entry['new_price']}"
    )
    assert outcome.cancelled and lifecycle.normal_status(outcome.final_status) == "CANCELED", (
        f"order {order_id} ended {_ended(outcome)} after one cancel request"
    )
    history = [entry["status"] for entry in record.get("history", [])]
    problems = lifecycle.states_agree(outcome.final_status, history, stream_statuses)
    record["views_agree"] = problems or "yes"
    assert not problems, "; ".join(problems)


# -- stage 1: IOC LIMIT far below the market: cancelled, never left open -------------------


@pytest.mark.parametrize("segment_id", CASH_IDS)
@_stops_the_run
def test_stage1_ioc_far_never_rests(
    segment_id: str, funded_session: OrderSession, funded_record: dict[str, Any]
) -> None:
    session, record = funded_session, funded_record
    plan = _plan(session, segment_id, record)
    price, _ = _far_prices(plan, record)
    plan = dataclasses.replace(plan, price=price, validity="IOC")
    outcome = _run(session, plan, record)
    _close_unexpected_fill(session, plan, outcome, record)
    assert not outcome.was_open, (
        f"the IOC order {outcome.order_id} was still open after the settle time "
        f"(cancelled by the test: {_ended(outcome)})"
    )
    assert lifecycle.normal_status(outcome.final_status) == "CANCELED", (
        f"the IOC order {outcome.order_id} ended {_ended(outcome)}"
    )


# -- stage 1: SL-LMT BUY above the market (not hit): trigger pending, modify, cancel -------


@pytest.mark.parametrize("segment_id", CASH_IDS)
@_stops_the_run
def test_stage1_stop_limit_not_hit(
    segment_id: str, funded_session: OrderSession, funded_record: dict[str, Any]
) -> None:
    session, record = funded_session, funded_record
    plan = _plan(session, segment_id, record)
    try:  # trigger +3 %, limit +4 %, within the upper circuit (else skipped, nothing sent)
        trigger, price = lifecycle.stop_buy_prices(
            plan.last_price,
            plan.tick_size,
            plan.upper_circuit,
            ratios=lifecycle.stop_ratios(segment_id),
        )
    except Refusal as exc:
        _skip(record, exc.reason)
    plan = dataclasses.replace(plan, order_type="SL-LMT", trigger_price=trigger, price=price)

    def modify(order: Order, rec: dict[str, Any]) -> None:
        assert plan.tick_size is not None
        rec["status_while_open"] = lifecycle.normal_status(order.status)
        new_trigger, new_price = lifecycle.stop_modify(
            trigger, price, plan.tick_size, plan.upper_circuit
        )
        order_id = str(order.order_id)
        safety.modify_once(
            session.client,
            plan,
            order_id,
            new_price,
            rec,
            session.responses,
            trigger_price=new_trigger,
        )
        rec["modify"]["in_book"] = uat._book_shows(session, order_id, new_price, new_trigger)

    outcome = _run(session, plan, record, on_open=modify)
    _close_unexpected_fill(session, plan, outcome, record)
    _require_open(outcome, record, "SL-LMT")

    assert record.get("status_while_open") == "TRIGGER_PENDING", (
        f"the stop order was {record.get('status_while_open')} while open"
    )
    modify_entry = record["modify"]
    assert modify_entry["outcome"] == "acknowledged", f"modify_order: {modify_entry}"
    assert modify_entry["in_book"]["matches"], (
        f"orders() shows {modify_entry['in_book']} after the modify, expected price "
        f"{modify_entry['new_price']} and trigger {modify_entry['new_trigger_price']}"
    )
    assert outcome.cancelled and lifecycle.normal_status(outcome.final_status) == "CANCELED", (
        f"order {outcome.order_id} ended {_ended(outcome)} after one cancel request"
    )


# -- stage 2: a tiny BUY at the best offer fills, a SELL at the best bid closes it ---------


@pytest.mark.parametrize("segment_id", SEGMENT_IDS)
@_stops_the_run
def test_stage2_tiny_fill_closed(
    segment_id: str,
    funded_session: OrderSession,
    funded_record: dict[str, Any],
    request: pytest.FixtureRequest,
) -> None:
    """The account's net position ends as it began; a close that does not fill stops the run."""
    session, record = funded_session, funded_record
    reason = funded.stage_two_reason(
        session, os.environ, live_conftest.stage1_failures(request.config)
    )
    if reason is not None:
        _skip(record, reason)
    _skip_from_the_cut_off(record)
    plan = _plan(session, segment_id, record)
    try:
        quote = session.client.quote(plan.exchange, plan.token)
    except Exception as exc:  # no price, no order
        _skip(record, f"quote() failed ({type(exc).__name__}); no order placed")
    tick = quote.tick_size if quote.tick_size is not None and quote.tick_size > 0 else None
    try:
        price = lifecycle.at_last_price(
            lifecycle.reference_price(quote, "BUY", use_depth=True),
            tick or plan.tick_size,
            "BUY",
            lower_circuit=quote.lc,
            upper_circuit=quote.uc,
        )
    except Refusal as exc:
        _skip(record, exc.reason)
    plan = dataclasses.replace(plan, price=price, last_price=quote.last_price)
    record["tiny_buy"] = {
        "price": price,
        "best_offer": quote.sp1,
        "best_bid": quote.bp1,
        "last_price": quote.last_price,
    }

    _skip_from_the_cut_off(record)  # read again just before the BUY
    # run_tracked reads positions() into record["net_before"] first (unreadable: skipped).
    outcome = _run(
        session, plan, record, settle_seconds=lifecycle.FILL_WAIT_SECONDS, poll_interval=2.0
    )
    before: int = record["net_before"]
    if outcome.definitive_rejection or lifecycle.normal_status(outcome.final_status) == "REJECTED":
        place = record.get("place", {})
        why = record.get("order_book", {}).get("rejreason") or place.get("emsg") or ""
        raise AssertionError(  # a failure of the live run, never a "did not fill" skip
            f"the tiny BUY {outcome.order_id} ({plan.tradingsymbol}) was rejected: it ended "
            f"{_ended(outcome)}" + (f" ({why})" if why else "")
        )
    if not outcome.filled or outcome.order_id is None:
        _skip(
            record,
            f"the tiny BUY did not fill within {lifecycle.FILL_WAIT_SECONDS:.0f} s: it ended "
            f"{_ended(outcome)} (recorded)",
        )
    buy_id = outcome.order_id
    bought = lifecycle.close_quantity(session, plan, outcome, record)  # unknown: CHECK, no SELL
    record["bought"] = bought
    try:
        record["net_after_buy"] = uat._wait_net(session, plan, before + bought)
    finally:  # or CHECK THE ACCOUNT, also when positions() is not back at `before`
        close = lifecycle.close_position(
            session, plan, bought, record, net_before=before, use_depth=True
        )
    sell_id = str(close.order_id)
    record["net_after_sell"] = uat._wait_net(session, plan, before)
    record["trades_listed"] = uat._traded(session, [buy_id, sell_id])
    after_buy, after_sell = record["net_after_buy"], record["net_after_sell"]
    record["net_change_after_buy"] = None if after_buy is None else after_buy - before
    record["net_change_after_sell"] = (
        None if after_buy is None or after_sell is None else after_sell - after_buy
    )

    assert lifecycle.normal_status(outcome.final_status) == "COMPLETE", (
        f"order {buy_id} filled but ended {_ended(outcome)}"
    )
    assert record["net_change_after_buy"] == bought, (
        f"positions() net quantity of {plan.tradingsymbol} changed by "
        f"{record['net_change_after_buy']} after the buy, expected +{bought} (the filled quantity)"
    )
    assert record["net_change_after_sell"] == -bought, (
        f"positions() net quantity of {plan.tradingsymbol} changed by "
        f"{record['net_change_after_sell']} after the closing sell, expected -{bought}"
    )
    assert record["trades_listed"], f"trades() does not list both orders {buy_id} and {sell_id}"
