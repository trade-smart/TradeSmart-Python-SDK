"""Order lifecycle tests for a non-production test environment (UAT mode only; see README.md).

They skip unless UAT mode holds (``TRADESMART_ENV=uat`` and a UAT REST host), on top of every
gate of the order suite: both opt-ins, the account check, serial runs, the order cap (40 in UAT
mode, 2 per test), one open order at a time, one place call per order (never resent) and a
cancel in ``finally`` while an order is open. Every fill a test creates is closed by a SELL of
the same quantity at the last price; a close that does not fill fails with "CHECK THE ACCOUNT:
position left open". An order type UAT refuses with another message is recorded and skipped.
The helpers are pinned by ``tests/unit/test_uat_lifecycle_helpers.py``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import time
from collections.abc import Iterator
from decimal import Decimal
from typing import Any, NoReturn

import pytest

from tests.live_orders import lifecycle, safety
from tests.live_orders.safety import OrderBudget, OrderOutcome, OrderPlan, OrderSession, Refusal
from tradesmart import TradeSmartStream
from tradesmart.models.orders import Order

pytestmark = pytest.mark.live_orders

ALL_IDS = list(lifecycle.UAT_SEGMENTS)  # mcx (skipped in UAT), nse, bse, nfo, nfoopt, bfoopt
TRADED_IDS = [s for s in ALL_IDS if s != "mcx"]
BASE_IDS = ["nse", "bse", "nfo"]  # IOC and SL-MKT: cash and the future only
STREAM_START_SECONDS = 20.0
SUBSCRIBE_SETTLE_SECONDS = 1.0  # after subscribe_orders(), before the order is placed
STREAM_WAIT_SECONDS = 15.0  # for the stream's final update after the order book shows it
BOOK_WAIT_SECONDS = 10.0  # for a modified order, a trade or a position to show up
ALGO_CHECK = "ALGO_CHK"  # UAT: "Rejected : ALGO_CHK: MKT Order type not allowed for API order"


@pytest.fixture
def uat_session(request: pytest.FixtureRequest) -> OrderSession:
    """The order session in UAT mode; skips (before any API call) outside ``TRADESMART_ENV=uat``."""
    try:
        return lifecycle.uat_session(os.environ, lambda: request.getfixturevalue("order_session"))
    except Refusal as exc:
        pytest.skip(exc.reason)


@pytest.fixture
def uat_record(
    request: pytest.FixtureRequest, uat_session: OrderSession
) -> Iterator[dict[str, Any]]:
    """What the test observed; written as one JSON line when the test ends (pass or not).

    Also gives the test its own order cap (``lifecycle.PER_TEST_CAP``).
    """
    data: dict[str, Any] = {
        "test": request.node.name,
        "raw_bodies_recorded": uat_session.raw_bodies,
        "uat_mode": uat_session.uat,
        "lifecycle": True,
    }
    uat_session.test_budget = OrderBudget(lifecycle.PER_TEST_CAP)
    try:
        yield data
    finally:
        data["orders_sent_by_test"] = uat_session.test_budget.used
        uat_session.test_budget = None
        uat_session.recorder.write(data)


def _skip(record: dict[str, Any], reason: str) -> NoReturn:
    record["skipped"] = reason
    pytest.skip(reason)


def _plan(session: OrderSession, segment_id: str, record: dict[str, Any]) -> OrderPlan:
    """The segment's one-lot DAY LIMIT BUY 10 % below the market (the base of every case)."""
    record["segment"] = segment_id
    reason = lifecycle.uat_segment_skip_reason(segment_id)
    if reason is not None:
        _skip(record, reason)
    segment = lifecycle.UAT_SEGMENTS[segment_id]
    missing = lifecycle.script_master_reason(session.client, segment.exchange)
    if missing is not None:
        _skip(record, missing)
    try:
        if segment_id in lifecycle.OPTION_SEGMENT_IDS:
            return safety.prepare_plan(
                session.client,
                segment,
                record,
                uat=session.uat,
                any_hour=session.uat,
                pick=lifecycle.pick_option,
            )
        return safety.prepare_plan(
            session.client, segment, record, uat=session.uat, any_hour=session.uat
        )
    except Refusal as exc:
        _skip(record, exc.reason)


def _run(
    session: OrderSession, plan: OrderPlan, record: dict[str, Any], **kwargs: Any
) -> OrderOutcome:
    try:
        outcome = lifecycle.run_tracked(session, plan, record, **kwargs)
    except Refusal as exc:
        _skip(record, exc.reason)
    record["sdk_exception"] = outcome.exception
    record["final_status"] = outcome.final_status
    record["was_open"] = outcome.was_open
    record["cancelled_by_test"] = outcome.cancelled
    record["filled"] = outcome.filled
    return outcome


def _ended(outcome: OrderOutcome) -> str:
    return outcome.final_status or f"{outcome.exception} at place_order"


def _close_if_filled(
    session: OrderSession, plan: OrderPlan, outcome: OrderOutcome, record: dict[str, Any]
) -> None:
    """Sell back what the BUY filled (never more); "CHECK THE ACCOUNT" if that fails or the
    filled quantity is unknown (then nothing is sent)."""
    if not outcome.filled or outcome.order_id is None:
        return
    quantity = lifecycle.close_quantity(session, plan, outcome, record)
    lifecycle.close_position(session, plan, quantity, record, net_before=record["net_before"])


def _round_trip(plan: OrderPlan) -> OrderPlan:
    """The plan with the round-trip product, so an unexpected fill can be sold back the same day."""
    return dataclasses.replace(plan, product=lifecycle.ROUND_TRIP_PRODUCT[plan.segment_id])


def _refusal(record: dict[str, Any], outcome: OrderOutcome) -> str | None:
    """UAT's message when it refused the order (in the reply or as REJECTED), else ``None``."""
    if outcome.definitive_rejection and outcome.order_id is None:
        place = record.get("place", {})
        return str(place.get("emsg") or place.get("message") or outcome.exception)
    if lifecycle.normal_status(outcome.final_status) == "REJECTED":
        return str(record.get("order_book", {}).get("rejreason") or "REJECTED")
    return None


def _skip_if_refused(record: dict[str, Any], outcome: OrderOutcome, what: str) -> None:
    message = _refusal(record, outcome)
    if message is not None:
        _skip(record, f"UAT refused the {what} order: {message} (recorded)")


def _start_order_stream(
    session: OrderSession, updates: lifecycle.OrderUpdates, record: dict[str, Any]
) -> TradeSmartStream:
    """A stream subscribed to order updates; skips (no order sent yet) if it cannot start."""
    stream: TradeSmartStream | None = None
    try:
        stream = TradeSmartStream.from_client(session.client, on_order_update=updates.add)
        stream.start(timeout=STREAM_START_SECONDS)
        stream.subscribe_orders()
    except Exception as exc:
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
        _skip(record, f"the order stream did not start ({type(exc).__name__}); no order placed")
    record["stream"] = "subscribed to order updates"
    # The order subscription has no acknowledgement and the order goes over REST: give the
    # server a moment to register it, so the stream sees the order from its first update.
    time.sleep(SUBSCRIBE_SETTLE_SECONDS)
    return stream


def _book_shows(
    session: OrderSession, order_id: str, price: Decimal, trigger: Decimal | None = None
) -> dict[str, Any]:
    """Whether ``orders()`` shows the order's new price (and trigger), and what it showed last."""
    seen: list[Order | None] = []

    def shows_expected() -> bool:
        try:
            book = session.client.orders()
        except Exception:
            return False
        row = next((o for o in book if (o.order_id or "").strip() == order_id), None)
        seen.append(row)
        if row is None or row.price != price:
            return False
        return trigger is None or row.trigger_price == trigger

    matches = lifecycle.wait_until(shows_expected, BOOK_WAIT_SECONDS)
    last = seen[-1] if seen else None
    return {
        "matches": matches,
        "price": None if last is None or last.price is None else str(last.price),
        "trigger_price": (
            None if last is None or last.trigger_price is None else str(last.trigger_price)
        ),
    }


def _net(session: OrderSession, plan: OrderPlan) -> int | None:
    try:
        positions = session.client.positions()
    except Exception:
        return None
    return lifecycle.net_quantity(positions, plan.exchange, plan.tradingsymbol)


def _wait_net(session: OrderSession, plan: OrderPlan, expected: int) -> int | None:
    """Poll ``positions()`` until the instrument's net quantity is ``expected``; the last seen."""
    seen: list[int | None] = []

    def reached() -> bool:
        seen.append(_net(session, plan))
        return seen[-1] == expected

    lifecycle.wait_until(reached, BOOK_WAIT_SECONDS)
    return seen[-1] if seen else None


def _traded(session: OrderSession, order_ids: list[str]) -> bool:
    def listed() -> bool:
        try:
            trades = session.client.trades()
        except Exception:
            return False
        return all(lifecycle.has_trade(trades, order_id) for order_id in order_ids)

    return lifecycle.wait_until(listed, BOOK_WAIT_SECONDS)


# -- LIMIT far from the market: open, modify, cancel; three views agree -------------------


@pytest.mark.parametrize("segment_id", ALL_IDS)
def test_open_order_is_modified_cancelled_and_all_views_agree(
    segment_id: str, uat_session: OrderSession, uat_record: dict[str, Any]
) -> None:
    session, record = uat_session, uat_record
    plan = _plan(session, segment_id, record)  # CNC on cash, NRML on derivatives
    updates = lifecycle.OrderUpdates()
    stream = _start_order_stream(session, updates, record)

    def modify(order: Order, rec: dict[str, Any]) -> None:
        assert plan.tick_size is not None and plan.last_price is not None
        new_price = safety.modify_price(
            plan.price, plan.tick_size, plan.lower_circuit, plan.last_price
        )
        order_id = str(order.order_id)
        safety.modify_once(session.client, plan, order_id, new_price, rec, session.responses)
        rec["modify"]["price_in_book"] = _book_shows(session, order_id, new_price)

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
    _close_if_filled(session, plan, outcome, record)
    if outcome.filled:
        _skip(record, f"the order filled ({_ended(outcome)}) and was closed: nothing to modify")
    if not outcome.was_open:
        _skip(record, f"no open order to modify: the order ended {_ended(outcome)} (recorded)")

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


# -- LIMIT at the last price: fills, then a SELL closes it --------------------------------


@pytest.mark.parametrize("segment_id", ALL_IDS)
def test_limit_at_last_price_fills_and_is_closed(
    segment_id: str, uat_session: OrderSession, uat_record: dict[str, Any]
) -> None:
    """UAT fills at exactly the last price. The account's net position ends as it began."""
    session, record = uat_session, uat_record
    plan = _plan(session, segment_id, record)
    try:
        price = lifecycle.at_last_price(
            plan.last_price,
            plan.tick_size,
            "BUY",
            lower_circuit=plan.lower_circuit,
            upper_circuit=plan.upper_circuit,
        )
    except Refusal as exc:
        _skip(record, exc.reason)
    product = lifecycle.ROUND_TRIP_PRODUCT[segment_id]
    plan = dataclasses.replace(plan, price=price, product=product)
    record["round_trip"] = {"product": product, "price": price}

    # run_tracked reads positions() into record["net_before"] first (unreadable: skipped).
    outcome = _run(
        session, plan, record, settle_seconds=lifecycle.FILL_WAIT_SECONDS, poll_interval=2.0
    )
    before: int = record["net_before"]
    if not outcome.filled or outcome.order_id is None:
        _skip(record, f"the order at the last price did not fill: it ended {_ended(outcome)}")
    buy_id = outcome.order_id
    # The larger of the final order-book row and the trade book (never more than ordered);
    # unknown: CHECK THE ACCOUNT and no SELL.
    bought = lifecycle.close_quantity(session, plan, outcome, record)
    record["bought"] = bought
    try:
        record["net_after_buy"] = _wait_net(session, plan, before + bought)
    finally:  # or CHECK THE ACCOUNT, also when positions() is not back at `before`
        close = lifecycle.close_position(session, plan, bought, record, net_before=before)
    sell_id = str(close.order_id)
    record["net_after_sell"] = _wait_net(session, plan, before)
    record["trades_listed"] = _traded(session, [buy_id, sell_id])
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


# -- LIMIT IOC away from the market: cancelled by the exchange, never left open -----------


@pytest.mark.parametrize("segment_id", BASE_IDS)
def test_ioc_limit_away_from_the_market_never_rests(
    segment_id: str, uat_session: OrderSession, uat_record: dict[str, Any]
) -> None:
    session, record = uat_session, uat_record
    plan = dataclasses.replace(_round_trip(_plan(session, segment_id, record)), validity="IOC")
    outcome = _run(session, plan, record)
    _close_if_filled(session, plan, outcome, record)
    assert not outcome.filled, f"the IOC order {outcome.order_id} filled (closed again)"
    _skip_if_refused(record, outcome, "IOC")
    assert not outcome.was_open, (
        f"the IOC order {outcome.order_id} was still open after the settle time "
        f"(cancelled by the test: {_ended(outcome)})"
    )
    assert lifecycle.normal_status(outcome.final_status) == "CANCELED", (
        f"the IOC order {outcome.order_id} ended {_ended(outcome)}"
    )


# -- SL-LMT BUY above the market: trigger pending, modify, cancel -------------------------


@pytest.mark.parametrize("segment_id", TRADED_IDS)
def test_stop_limit_buy_is_trigger_pending_modified_and_cancelled(
    segment_id: str, uat_session: OrderSession, uat_record: dict[str, Any]
) -> None:
    session, record = uat_session, uat_record
    plan = _round_trip(_plan(session, segment_id, record))
    try:
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
        rec["modify"]["in_book"] = _book_shows(session, order_id, new_price, new_trigger)

    outcome = _run(session, plan, record, on_open=modify)
    _close_if_filled(session, plan, outcome, record)
    if outcome.filled:
        _skip(record, f"the stop order triggered and filled ({_ended(outcome)}); closed again")
    _skip_if_refused(record, outcome, "SL-LMT")
    if not outcome.was_open:
        _skip(record, f"the stop order was never pending: it ended {_ended(outcome)} (recorded)")

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


# -- SL-MKT BUY above the market: refused by the API order check, or recorded -------------


@pytest.mark.parametrize("segment_id", BASE_IDS)
def test_stop_market_buy_is_refused_or_recorded(
    segment_id: str, uat_session: OrderSession, uat_record: dict[str, Any]
) -> None:
    session, record = uat_session, uat_record
    plan = _round_trip(_plan(session, segment_id, record))
    try:  # the trigger stays within the upper circuit (else skipped, nothing sent)
        trigger, _ = lifecycle.stop_buy_prices(
            plan.last_price,
            plan.tick_size,
            plan.upper_circuit,
            ratios=lifecycle.stop_ratios(segment_id),
        )
    except Refusal as exc:
        _skip(record, exc.reason)
    plan = dataclasses.replace(plan, order_type="SL-MKT", trigger_price=trigger)
    outcome = _run(session, plan, record)
    _close_if_filled(session, plan, outcome, record)
    message = _refusal(record, outcome)
    record["refused_with"] = message
    if message is None:
        record["sl_mkt"] = f"accepted; ended {_ended(outcome)}"
        assert safety.is_final(outcome.final_status), (
            f"the SL-MKT order {outcome.order_id} ended {_ended(outcome)}"
        )
        return
    if ALGO_CHECK not in message:
        _skip(record, f"UAT refused the SL-MKT order with another message: {message}")
    record["sl_mkt"] = "refused by the API order check"


# -- MARKET: refused by the API order check at the place call ------------------------------


@pytest.mark.parametrize("segment_id", TRADED_IDS)
def test_market_order_is_refused_by_the_api_order_check(
    segment_id: str, uat_session: OrderSession, uat_record: dict[str, Any]
) -> None:
    session, record = uat_session, uat_record
    plan = dataclasses.replace(_round_trip(_plan(session, segment_id, record)), order_type="MARKET")
    outcome = _run(session, plan, record)
    _close_if_filled(session, plan, outcome, record)
    message = _refusal(record, outcome)
    record["refused_with"] = message
    assert message is not None, (
        f"MARKET order {outcome.order_id} was accepted ({_ended(outcome)}); expected the "
        f"{ALGO_CHECK} rejection"
    )
    if ALGO_CHECK not in message:
        _skip(record, f"UAT refused the MARKET order with another message: {message}")
    in_reply = outcome.exception == "OrderRejectedError" and outcome.order_id is None
    in_book = lifecycle.normal_status(outcome.final_status) == "REJECTED" and not outcome.filled
    record["refused_in"] = "place reply" if in_reply else "order book" if in_book else None
    assert in_reply or in_book, (
        f"expected OrderRejectedError from place_order or a REJECTED order, got "
        f"{outcome.exception} and order {outcome.order_id} ({_ended(outcome)})"
    )


# -- every order's tag is unique and found in orders() -------------------------------------


@pytest.mark.parametrize("segment_id", ALL_IDS)
def test_every_order_tag_is_unique_and_in_the_order_book(
    segment_id: str, uat_session: OrderSession, uat_record: dict[str, Any]
) -> None:
    """Places nothing: checks the orders sent by the lifecycle tests above in this run."""
    session, record = uat_session, uat_record
    record["segment"] = segment_id
    placed = [p for p in session.placed if p.segment_id == segment_id]
    if not placed:
        _skip(record, f"no lifecycle order for {segment_id.upper()} in this run")
    record["tags"] = [p.tag for p in placed]
    problems = lifecycle.tag_problems(placed, session.client.orders())
    record["tag_problems"] = problems
    assert not problems, "; ".join(problems)
