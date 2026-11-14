"""The UAT matrix family M15: MCX in trade units (marker ``m_mcx``).

Owner answer U7: MCX quantity is in **trade units, whole multiples of the lot size**, the lot
size from the day's script master (``Contract.quantity``); there is no quantity-1 probe.

- The M1 grid on the CRUDEOIL, GOLD, GOLDM and GOLDPETAL futures (``cases`` family ``mcx``):
  far LIMIT BUY / SELL NRML and MIS, stops, IOC and EOS (recorded: MCX has no row in the
  retention annexure), MKT / SL-MKT (F27). Smoke: the six smoke shapes on CRUDEOIL and the far
  LIMIT BUY NRML on GOLD, GOLDM and GOLDPETAL.
- The M3 rejections in units (``PROBE_CASES``): an off-tick price (the far BUY + 0.01 on the
  quote's tick) and one unit more than a lot (``lot size + 1`` units; a one-unit lot cannot
  make a part lot and skips). Smoke: the CRUDEOIL part lot.
- Every case records its quantity units when the order is accepted (``record_units``, no call:
  the plan's quantity, the lot size, the book row's ``qty``, ``ls`` and ``prcftr``).

**Units of price.** GOLD quotes per 10 g (``prcftr`` 100 on the security info), GOLDM per
10 g, GOLDPETAL per gram: the price factor scales value, not the price. Every price is the
quote's last price moved by the pricing rule and rounded to the quote's own tick
(``pricing.fresh_quote``), so price and tick are in the same unit; no factor is applied.

MCX trades 09:00-23:30 IST: outside UAT this family uses the MCX window (``hours_refusal``), not
the NSE one; on UAT it runs at any hour.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from tests.live_orders import safety
from tests.live_orders.safety import Refusal
from tests.live_orders.uat_matrix import batch, cases
from tests.live_orders.uat_matrix.batch import BatchItem, Placed
from tests.live_orders.uat_matrix.cases import FULL, REJECT, SMOKE, Case
from tests.live_orders.uat_matrix.contracts import Contract
from tests.live_orders.uat_matrix.pricing import PriceSnapshot
from tradesmart.models.base import IST

FAMILY = "mcx"
OFF_TICK = Decimal("0.01")  # the smallest price step a two-decimal price can take
PROBES = ("offtick", "lot")
SMOKE_PROBES = frozenset({("mcx-crudeoil-f", "lot")})


def _probe_cases() -> list[Case]:
    return [
        Case(
            id=f"{FAMILY}-{role}-nrml-lmt-day-buy-{probe}",
            family=FAMILY,
            role=role,
            product="NRML",
            prctyp="LMT",
            ret="DAY",
            side="BUY",
            orders=1,
            outcome=REJECT,
            tier=SMOKE if (role, probe) in SMOKE_PROBES else FULL,
        )
        for role in cases.MCX_ROLES
        for probe in PROBES
    ]


GRID_CASES: list[Case] = [c for c in cases.CASES if c.family == FAMILY]
PROBE_CASES: list[Case] = _probe_cases()
MCX_CASES: list[Case] = [*GRID_CASES, *PROBE_CASES]


def build_plan(case: Case, contract: Contract, snapshot: PriceSnapshot) -> dict[str, Any]:
    """The probe's ``place_order`` arguments: the far LIMIT BUY of one lot with one rule broken
    (``safety.Refusal`` when the probe cannot be built: the case skips)."""
    plan = batch.default_plan(case, contract, snapshot)
    if case.id.endswith("-offtick"):
        if snapshot.tick_size <= OFF_TICK:
            raise Refusal(f"tick {snapshot.tick_size}: no off-tick price with two decimals")
        plan["price"] = Decimal(str(plan["price"])) + OFF_TICK
    elif case.id.endswith("-lot"):
        if contract.lot_size < 2:
            raise Refusal(f"{contract.tradingsymbol}: lot size 1, no part-lot quantity")
        plan["quantity"] = contract.lot_size + 1  # trade units: one lot plus one unit
    else:
        raise ValueError(f"case {case.id} is not an MCX probe")
    return plan


def record_units(placed: Placed) -> None:
    """Note the accepted order's quantity units (no call): sent, lot size, lots, and what the
    book row says (``qty``, ``ls``, ``prcftr``)."""
    quantity = int(placed.plan["quantity"])
    lot = placed.contract.lot_size
    row = placed.row
    placed.notes["units"] = {
        "quantity_sent": quantity,
        "lot_size": lot,
        "lots": quantity // lot if quantity % lot == 0 else None,
        "book_quantity": None if row is None else row.quantity,
        "book_lot_size": None if row is None else row.lot_size,
        "prcftr": None if row is None else row.prcftr,
    }


def items(selected: Iterable[Case]) -> list[BatchItem]:
    """The batch items: every case notes its units; the probes carry their plan."""
    probes = set(PROBE_CASES)
    return [
        BatchItem(case, plan=build_plan if case in probes else None, after_settle=record_units)
        for case in selected
    ]


def hours_refusal(
    now: datetime | None = None, *, uat: bool = False, mock: bool = False
) -> str | None:
    """Why MCX orders must not be placed now (outside 09:05-23:15 IST, weekends), else
    ``None`` (``TRADESMART_LIVE_ORDER_OUTSIDE_HOURS=1`` lifts it, as in every live suite). On UAT
    (``uat``: the session's ``uat``) never: UAT tests run at any hour (owner, 2026-11-13); in a
    mock session (``mock``: the session's ``mock``) never: the owner starts the run once mock
    trading opens."""
    if uat or mock:
        return None
    moment = now if now is not None else datetime.now(IST)
    return safety.market_closed_reason(safety.SEGMENTS["mcx"], moment)
