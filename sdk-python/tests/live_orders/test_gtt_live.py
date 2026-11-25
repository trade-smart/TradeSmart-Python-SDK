"""The GTT and OCO live tests, on UAT or on the zero-funds production account only (README.md).

It skips unless both order opt-ins are set in a serial run, and always in funded mode
(``TRADESMART_LIVE_ORDER_FUNDED`` set to any value): a GTT whose condition is already true when
placed fires at once and places a real order. On production the account check and the
zero-funds check run first (``safety.prepare_plan``) and it runs inside market hours only; UAT
mode needs neither and runs at any hour (owner 2026-11-13).

One GTT on NSE ``ITC-EQ``: ``LTP_A_O`` with the trigger at 3 x a fresh last price (refused
before sending if the quote has no last price or the condition would already be true), its
order a CNC LIMIT BUY of 1 share 10 % below the market, alert validity ``DAY``. It is read back
from ``pending_gtts()`` by its ``sdkgtt-`` tag, its trigger is modified once to 3.1 x, and it is
cancelled in ``finally``; a GTT left behind, or one that fired, is "CHECK THE ACCOUNT". Place,
modify and cancel each count against the run cap and the test's cap of 3. The run is
``gtt.run_gtt`` (pinned by ``tests/unit/test_live_orders_safety.py``; a dry run of this file on
the offline broker: ``tests/integration/test_gtt_live_offline_broker.py``). Two more tests read
``gtt_alert_types()`` and ``triggered_gtts()`` only.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterator
from typing import Any, NoReturn

import pytest

from tests.live_orders import gtt, lifecycle, safety
from tests.live_orders.safety import OrderBudget, OrderPlan, OrderSession, Refusal
from tradesmart.enums import GttAlertType, GttValidity

pytestmark = pytest.mark.live_orders


@pytest.fixture
def gtt_session(request: pytest.FixtureRequest) -> OrderSession:
    """The order session (UAT mode or production); skips before any API call in funded mode."""
    try:
        return gtt.gtt_session(os.environ, lambda: request.getfixturevalue("order_session"))
    except Refusal as exc:
        pytest.skip(exc.reason)


@pytest.fixture
def gtt_record(
    request: pytest.FixtureRequest, gtt_session: OrderSession
) -> Iterator[dict[str, Any]]:
    """What the test observed; written as one JSON line when the test ends (pass or not).

    Also gives the test its own cap of ``gtt.GTT_CALLS`` (place, modify, cancel).
    """
    data: dict[str, Any] = {
        "test": request.node.name,
        "raw_bodies_recorded": gtt_session.raw_bodies,
        "uat_mode": gtt_session.uat,
        "gtt_test": True,
    }
    gtt_session.test_budget = OrderBudget(gtt.GTT_CALLS)
    try:
        yield data
    finally:
        data["calls_reserved_by_test"] = gtt_session.test_budget.used
        gtt_session.test_budget = None
        gtt_session.recorder.write(data)


def _skip(record: dict[str, Any], reason: str) -> NoReturn:
    record["skipped"] = reason
    pytest.skip(reason)


def _plan(session: OrderSession, record: dict[str, Any]) -> OrderPlan:
    """1 share of ITC 10 % below the market (zero-funds check on production), a fresh GTT tag."""
    missing = lifecycle.script_master_reason(session.client, gtt.SEGMENT.exchange)
    if missing is not None:
        _skip(record, missing)
    try:
        plan = safety.prepare_plan(
            session.client, gtt.SEGMENT, record, uat=session.uat, any_hour=session.uat
        )
    except Refusal as exc:
        _skip(record, exc.reason)
    return dataclasses.replace(plan, tag=gtt.new_gtt_tag())


def test_far_gtt_buy_is_placed_read_back_modified_once_and_cancelled(
    gtt_session: OrderSession, gtt_record: dict[str, Any]
) -> None:
    session, record = gtt_session, gtt_record
    plan = _plan(session, record)
    try:
        outcome = gtt.run_gtt(session, plan, record)
    except Refusal as exc:
        _skip(record, exc.reason)
    if outcome.place == "rejected":
        _skip(record, f"the server rejected the GTT ({outcome.exception}); recorded")
    assert outcome.place == "accepted", (
        f"PlaceGTTOrder outcome {outcome.place} ({outcome.exception}); tag {plan.tag}: seen in "
        f"pending_gtts() as {outcome.seen or 'none'}, cancelled {outcome.cancelled or 'none'}"
    )
    row = outcome.placed_row
    assert row is not None, (
        f"the GTT (tag {plan.tag}) was never seen in pending_gtts() (cancelled by its id: "
        f"{outcome.cancelled or 'none'})"
        if not outcome.seen
        else f"the GTT (tag {plan.tag}) is not once in pending_gtts(): {outcome.seen}"
    )
    assert row.alert_type == GttAlertType.LTP_ABOVE, f"alert type {row.alert_type}"
    assert row.trigger_value == outcome.trigger, f"trigger {row.trigger_value}"
    assert row.alert_validity == GttValidity.DAY, f"alert validity {row.alert_validity}"
    assert row.quantity == plan.quantity and row.price == plan.price, (
        f"order {row.quantity} @ {row.price}"
    )
    if outcome.modify == "refused":
        _skip(record, f"modify not sent: {outcome.refusal} (the GTT was cancelled)")
    assert outcome.modify == "accepted", f"ModifyGTTOrder outcome {outcome.modify}"
    modified = outcome.modified_row
    assert modified is not None, f"the modified GTT (tag {plan.tag}) is not once in pending_gtts()"
    assert modified.trigger_value == outcome.modified_trigger, (
        f"modified trigger {modified.trigger_value}"
    )
    assert outcome.cancelled, "no cancel request was accepted"
    assert outcome.left_pending == []


def test_gtt_alert_types_are_a_list_of_words(
    gtt_session: OrderSession, gtt_record: dict[str, Any]
) -> None:
    """Read-only: the alert families enabled for the account (e.g. ``["ATP", "LTP"]``)."""
    families = gtt_session.client.gtt_alert_types()
    gtt_record["alert_types"] = families
    assert isinstance(families, list)
    assert all(isinstance(f, str) and f.strip() for f in families), families


def test_triggered_gtts_are_readable(gtt_session: OrderSession, gtt_record: dict[str, Any]) -> None:
    """Read-only: ``triggered_gtts()`` answers (an empty list when none fired)."""
    triggered = gtt_session.client.triggered_gtts()
    gtt_record["triggered_count"] = len(triggered)
    gtt_record["triggered_tags_of_this_suite"] = [
        g.tag for g in triggered if (g.tag or "").startswith(gtt.GTT_TAG_PREFIX)
    ]
    assert isinstance(triggered, list)


def test_oco_lifecycle(gtt_session: OrderSession, gtt_record: dict[str, Any]) -> None:
    """A BUY OCO of 1 ITC-EQ (CNC, LIMIT legs): stop-loss trigger 1.10 x and target trigger 0.90 x
    a fresh last price, placed, read back by alert id, its stop-loss modified once, cancelled; on
    UAT one more modify on the cancelled id must be rejected (``Invalid Oi``). On production it is
    three writes (no probe) and needs the whole run budget: run it alone with ``-k oco``."""
    session, record = gtt_session, gtt_record
    session.test_budget = OrderBudget(gtt.oco_calls_needed(session))
    plan = _plan(session, record)
    try:
        outcome = gtt.run_oco_lifecycle(session, plan, record)
    except Refusal as exc:
        _skip(record, exc.reason)
    if outcome.place == "rejected":
        _skip(record, f"the server rejected the OCO ({outcome.exception}); recorded")
    assert outcome.place == "accepted", (
        f"PlaceOCOOrder outcome {outcome.place} ({outcome.exception}); tag {plan.tag}: seen in "
        f"pending_gtts() as {outcome.seen or 'none'}, cancelled {outcome.cancelled or 'none'}"
    )
    row = outcome.placed_row
    assert row is not None, (
        f"the OCO (tag {plan.tag}) is not once in pending_gtts(): {outcome.seen}"
    )
    assert row.is_oco, f"alert type {row.alert_type}"
    assert row.stop_loss is not None and row.target is not None, "stop_loss / target not readable"
    assert row.stop_loss.trigger_value == outcome.stop_loss, f"stop-loss {row.stop_loss}"
    assert row.target.trigger_value == outcome.target, f"target {row.target}"
    if outcome.modify == "refused":
        _skip(record, f"modify not sent: {outcome.refusal} (the OCO was cancelled)")
    assert outcome.modify == "accepted", f"ModifyOCOOrder outcome {outcome.modify}"
    modified = outcome.modified_row
    assert modified is not None and modified.stop_loss is not None, "modified OCO not readable"
    assert modified.stop_loss.trigger_value == outcome.modified_stop_loss
    assert outcome.cancelled, "no cancel was accepted"
    assert outcome.left_pending == []
    if session.uat:
        assert outcome.probe == "rejected", f"modify on the cancelled id: {outcome.probe}"
