"""The modify rig's product refusal belongs to the product-change call, not to whichever modify
reaches the offline broker next (the intermittent ``product`` step ``record``, 13-14 Nov 2026).

The matrix runs its cases in a thread pool. The rig refused a product change by arming
``broker.fail_next("ModifyOrder", ...)`` and then sending the modify; another case's modify that
reached the broker in between took the refusal, so the product change was acknowledged. This test
forces that interleaving: right after the refusal is armed, a second modify of another order is
started and given time to reach the broker.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from tests.integration.uat_modify_views_rig import PRODUCT_REFUSAL, modify_views_rig
from tradesmart import OrderRejectedError

ITC = {"exchange": "NSE", "tradingsymbol": "ITC-EQ", "quantity": 1}


def _place(client: Any) -> str:
    order_id: str = client.place_order(
        **ITC, transaction_type="BUY", product="MIS", order_type="LIMIT", price="360.00",
        validity="DAY",
    )  # fmt: skip
    return order_id


def test_a_concurrent_modify_never_takes_the_product_refusal(tmp_path: Path) -> None:
    with modify_views_rig(tmp_path) as rig:
        client = rig.session.client
        first, second = _place(client), _place(client)
        other: dict[str, Any] = {}
        done = threading.Event()

        def modify_second() -> None:
            try:
                other["id"] = client.modify_order(
                    second, **ITC, price="361.00", order_type="LIMIT", product="MIS"
                )
            except Exception as exc:  # recorded for the assertion below
                other["error"] = exc
            finally:
                done.set()

        armed = rig.broker.fail_next

        def arm_then_interleave(endpoint: str, drill: Any) -> None:
            armed(endpoint, drill)
            threading.Thread(target=modify_second).start()
            done.wait(0.5)  # time for the other modify to reach the broker, if it can

        rig.broker.fail_next = arm_then_interleave
        with pytest.raises(OrderRejectedError, match=PRODUCT_REFUSAL.split(":", 1)[1]):
            client.modify_order(first, **ITC, price="360.00", order_type="LIMIT", product="CNC")
        assert done.wait(10)
        assert "error" not in other, other
        assert other["id"] == second
