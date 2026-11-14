"""The UAT matrix probe U15: orders with a test ``algo_id``, recorded only (marker ``m_algo``).

Owner answer U15: a few far LIMIT orders sent with a **synthetic** test ``algo_id``
(``ALGO_ID``, never a real one) through the SDK's per-call ``algo_id`` of ``place_order``, and
one ``modify_order`` with it; the answer is recorded (accepted, or the refusal words), never
judged. ``ClientConfig.algo_id`` stays unset and ``order_rate_limit_per_second`` is never raised:
the test id is a probe, not an approved algo.

NSE and NFO only: a BSE-side order with an algo id stays ``PENDING`` (F26). An NSE or NFO order
with the test id could stick the same way and trip its segment's breaker, so the module runs last
in the package (``test_z_algo_id.py``).
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from tests.live_orders.uat_matrix import batch
from tests.live_orders.uat_matrix.batch import BatchItem, Placed
from tests.live_orders.uat_matrix.cases import FULL, RECORD, SMOKE, Case
from tests.live_orders.uat_matrix.contracts import Contract
from tests.live_orders.uat_matrix.pricing import PriceSnapshot

FAMILY = "algo"
ALGO_ID = "TESTALGO1"  # synthetic: not a real (numeric) exchange algo id
# (role, product, side, tier); the NIFTY future also sends one modify with the test id.
_ROWS = (
    ("nse-b", "MIS", "BUY", FULL),
    ("nfo-nifty-f", "NRML", "BUY", SMOKE),
    ("nfo-nifty-ce-atm", "NRML", "SELL", FULL),
)
ALGO_CASES: list[Case] = [
    Case(
        id=f"{FAMILY}-{role}-{product}-lmt-day-{side}".lower(),
        family=FAMILY,
        role=role,
        product=product,
        prctyp="LMT",
        ret="DAY",
        side=side,
        orders=1,
        outcome=RECORD,
        tier=tier,
    )
    for role, product, side, tier in _ROWS
]
MODIFY_CASE = "algo-nfo-nifty-f-nrml-lmt-day-buy"


def build_plan(case: Case, contract: Contract, snapshot: PriceSnapshot) -> dict[str, Any]:
    """The far LIMIT order of ``case`` with the test ``algo_id`` for this call only."""
    return {**batch.default_plan(case, contract, snapshot), "algo_id": ALGO_ID}


def after_settle(placed: Placed) -> None:
    """Record the echoed ``algo_id``; on ``MODIFY_CASE`` one modify with the test id (the price
    one tick nearer the market, still far from it), recorded whatever the answer."""
    row = placed.row
    note: dict[str, Any] = {
        "sent": ALGO_ID,
        "echoed": None if row is None else row.algo_id,
    }
    placed.notes["algo"] = note
    if placed.case.id != MODIFY_CASE:
        return
    tick = placed.snapshot.tick_size
    nearer = Decimal(str(placed.plan["price"])) + (tick if placed.case.side == "BUY" else -tick)
    outcome = placed.modify(price=nearer, algo_id=ALGO_ID)
    note["modify"] = outcome.verdict
    if outcome.error:
        note["modify_error"] = outcome.error.get("emsg") or outcome.error.get("message")


def items(selected: Iterable[Case]) -> list[BatchItem]:
    return [BatchItem(case, plan=build_plan, after_settle=after_settle) for case in selected]
