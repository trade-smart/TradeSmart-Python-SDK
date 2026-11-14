"""The sliced-order live test for a non-production test environment (UAT mode only; README.md).

It skips unless UAT mode holds (``TRADESMART_ENV=uat`` and a UAT REST host, the funded-mode flag
unset), on top of every gate of the order suite: both opt-ins, the account check and serial
runs. One ``place_sliced_order`` call sends a LIMIT BUY of 2 lots of the nearest NFO NIFTY
future, 10 % below the market (never below the lower circuit), with ``max_slice_quantity`` = 1
lot: two orders, counted against the run cap and the test's own cap of 2. Both are cancelled in
``finally``. The order run is ``sliced.run_sliced`` (pinned by
``tests/unit/test_uat_sliced_helpers.py``; a dry run of this body on the offline broker:
``tests/integration/test_uat_sliced_offline_broker.py``).
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterator
from typing import Any, NoReturn

import pytest

from tests.live_orders import lifecycle, safety, sliced
from tests.live_orders.safety import OrderBudget, OrderPlan, OrderSession, Refusal

pytestmark = pytest.mark.live_orders

SEGMENT_ID = "nfo"  # the NIFTY index future (FUTIDX), nearest expiry after today


@pytest.fixture
def sliced_session(request: pytest.FixtureRequest) -> OrderSession:
    """The order session in UAT mode; skips (before any API call) outside it or in funded mode."""
    try:
        return sliced.sliced_session(os.environ, lambda: request.getfixturevalue("order_session"))
    except Refusal as exc:
        pytest.skip(exc.reason)


@pytest.fixture
def sliced_record(
    request: pytest.FixtureRequest, sliced_session: OrderSession
) -> Iterator[dict[str, Any]]:
    """What the test observed; written as one JSON line when the test ends (pass or not).

    Also gives the test its own order cap (``lifecycle.PER_TEST_CAP``: the two slices).
    """
    data: dict[str, Any] = {
        "test": request.node.name,
        "raw_bodies_recorded": sliced_session.raw_bodies,
        "uat_mode": sliced_session.uat,
        "sliced_order": True,
    }
    sliced_session.test_budget = OrderBudget(lifecycle.PER_TEST_CAP)
    try:
        yield data
    finally:
        data["orders_reserved_by_test"] = sliced_session.test_budget.used
        sliced_session.test_budget = None
        sliced_session.recorder.write(data)


def _skip(record: dict[str, Any], reason: str) -> NoReturn:
    record["skipped"] = reason
    pytest.skip(reason)


def _plan(session: OrderSession, record: dict[str, Any]) -> OrderPlan:
    """One lot of the NIFTY future, 10 % below the market, with a fresh sliced-order tag."""
    record["segment"] = SEGMENT_ID
    segment = lifecycle.UAT_SEGMENTS[SEGMENT_ID]
    missing = lifecycle.script_master_reason(session.client, segment.exchange)
    if missing is not None:
        _skip(record, missing)
    try:
        plan = safety.prepare_plan(
            session.client, segment, record, uat=session.uat, any_hour=session.uat
        )
    except Refusal as exc:
        _skip(record, exc.reason)
    return dataclasses.replace(plan, tag=sliced.new_sliced_tag())


def test_far_limit_buy_of_two_lots_goes_out_as_two_slices_and_both_are_cancelled(
    sliced_session: OrderSession, sliced_record: dict[str, Any]
) -> None:
    session, record = sliced_session, sliced_record
    plan = _plan(session, record)
    try:
        outcome = sliced.run_sliced(session, plan, record)
    except Refusal as exc:
        _skip(record, exc.reason)
    result = outcome.result
    assert result is not None, f"place_sliced_order raised {outcome.exception}"
    if not result.order_ids and result.error is not None:
        _skip(record, f"UAT refused the first slice: {result.error} (recorded)")
    statuses = outcome.final_statuses
    if statuses and all(lifecycle.normal_status(s) == "REJECTED" for s in statuses.values()):
        _skip(record, f"UAT rejected every slice: {statuses} (recorded)")

    lot = plan.quantity
    assert result.slice_quantities == (lot, lot), f"planned slices {result.slice_quantities}"
    assert result.complete, f"stopped after {len(result.order_ids)} of 2 slices: {result.error}"
    assert len(set(result.order_ids)) == 2, f"order ids {result.order_ids}"
    assert result.placed_quantity == 2 * lot and result.unsent_quantity == 0
    assert outcome.order_ids == result.order_ids, (
        f"orders() shows other orders with tag {plan.tag}: {outcome.order_ids}"
    )
    ended = {i: lifecycle.normal_status(s) for i, s in statuses.items()}
    assert set(ended.values()) == {"CANCELED"}, f"the slices ended {ended}"
    assert set(outcome.cancelled) == set(result.order_ids), f"cancelled {outcome.cancelled}"
