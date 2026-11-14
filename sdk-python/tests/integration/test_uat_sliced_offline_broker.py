"""A dry run of the UAT sliced-order live test body against the offline broker (no network).

The live test in ``tests/live_orders/test_uat_sliced.py`` runs only against UAT. Here its body
runs unchanged with a UAT-mode session on an ``OfflineBroker`` client and the real
``place_sliced_order``, so a mistake in the test code shows up before the live run. Market hours
are lifted and the waits run on a fake clock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.live_orders import lifecycle, safety, sliced
from tests.live_orders import test_uat_sliced as uat
from tests.live_orders.safety import OrderBudget, OrderSession, Recorder
from tradesmart.testing import OfflineBroker, drills

SYMBOL = "NIFTYFUT"
LOT = 65


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Rig:
    broker: OfflineBroker
    session: OrderSession
    place_calls: list[int]

    def run(self) -> dict[str, Any]:
        record: dict[str, Any] = {"test": "sliced"}
        self.session.test_budget = OrderBudget(lifecycle.PER_TEST_CAP)
        try:
            uat.test_far_limit_buy_of_two_lots_goes_out_as_two_slices_and_both_are_cancelled(
                self.session, record
            )
        finally:
            self.session.test_budget = None
        return record

    def requests(self, endpoint: str) -> list[dict[str, Any]]:
        return [r.payload for r in self.broker.requests if r.endpoint == endpoint]


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Rig]:
    monkeypatch.setattr(safety, "market_closed_reason", lambda segment, now: None)
    fake = FakeClock()
    real_run = sliced.run_sliced

    def run_on_fake_time(*args: Any, **kwargs: Any) -> Any:
        kwargs.update(sleep=fake.sleep, clock=fake.clock)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(sliced, "run_sliced", run_on_fake_time)
    with OfflineBroker("UAT01") as broker:
        broker.add_instrument(
            "NFO", "500", SYMBOL, lot_size=LOT, tick_size="0.10", instrument="FUTIDX",
            expiry=date.today() + timedelta(days=14), underlying="NIFTY", frzqty=1801,
        )  # fmt: skip
        broker.set_quote(
            "NFO", "500", lp="26040.00", lc="23436.00", uc="28644.00", ti="0.10", ls="65"
        )
        client = broker.client()
        calls: list[int] = []
        original: Callable[..., str] = client.place_order

        def counted(**kwargs: Any) -> str:
            calls.append(kwargs["quantity"])
            return original(**kwargs)

        monkeypatch.setattr(client, "place_order", counted)
        session = OrderSession(
            client,
            budget=OrderBudget(safety.UAT_MAX_ORDERS_PER_RUN),
            recorder=Recorder(tmp_path / "live_orders.jsonl"),
            uat=True,
        )
        try:
            yield Rig(broker, session, calls)
        finally:
            client.close()


def test_two_slices_rest_and_are_both_cancelled(rig: Rig) -> None:
    record = rig.run()
    placed = rig.requests("PlaceOrder")
    assert [int(p["qty"]) for p in placed] == [LOT, LOT]
    assert {p["prctyp"] for p in placed} == {"LMT"} and {p["trantype"] for p in placed} == {"B"}
    assert len({p["remarks"] for p in placed}) == 1  # one shared tag
    assert placed[0]["remarks"].startswith(sliced.SLICED_TAG_PREFIX)
    assert len(rig.requests("CancelOrder")) == 2
    assert [o.status for o in rig.broker.orders] == ["CANCELED", "CANCELED"]
    assert rig.session.budget.used == 2
    assert record["sliced"]["place"]["outcome"] == "complete"
    assert record["sliced"]["max_slice_quantity"] == LOT


def test_a_rejected_second_slice_fails_the_test_after_cancelling_the_first(rig: Rig) -> None:
    original = rig.session.client.place_order

    def reject_second(**kwargs: Any) -> str:
        if len(rig.place_calls) == 1:  # the second slice
            rig.broker.fail_next("PlaceOrder", drills.reject("Rejected : RMS: margin"))
        result: str = original(**kwargs)
        return result

    rig.session.client.place_order = reject_second  # type: ignore[method-assign]
    with pytest.raises(AssertionError, match="stopped after 1 of 2 slices"):
        rig.run()
    assert len(rig.requests("PlaceOrder")) == 2
    assert [o.status for o in rig.broker.orders] == ["CANCELED"]


def test_slices_rejected_by_the_environment_skip_the_test(rig: Rig) -> None:
    rig.broker.default_place = "reject"
    with pytest.raises(pytest.skip.Exception, match="rejected every slice"):
        rig.run()
    assert len(rig.requests("PlaceOrder")) == 2 and rig.requests("CancelOrder") == []


def test_the_sdk_refusing_for_no_minute_room_is_a_refusal_not_an_unknown_outcome(
    rig: Rig,
) -> None:
    # The test body turns the "not sent" Refusal into a skip; an unknown outcome would fail it.
    rig.session.client._transport.order_room_wait = lambda n: 99.0  # type: ignore[attr-defined]
    with pytest.raises(pytest.skip.Exception, match="refused the sliced order before sending"):
        rig.run()
    assert rig.requests("PlaceOrder") == []
