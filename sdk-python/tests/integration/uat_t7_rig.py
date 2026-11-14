"""Additions to the family dry-run rig for M6 (AMO), M7 (sliced) and M8 (burst) (test helper).

``add_mcx(rig)`` adds the MCX CRUDEOIL future (role ``mcx-crudeoil-f``, lot 100 units) to the
market and the run's contracts; ``set_freeze(rig, role, frzqty)`` gives an instrument a freeze
quantity (``security_info().frzqty``, what ``place_sliced_order`` slices by).
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

from tests.integration.uat_family_rig import EXPIRY, Rig

MCX_ROLE = "mcx-crudeoil-f"
MCX_SYMBOL = "CRUDEOIL18DEC26FUT"


def add_mcx(rig: Rig) -> None:
    rig.market.add(
        "MCX", "900", MCX_SYMBOL, last="5500.00", tick="1.00", lot=100, role=MCX_ROLE,
        kind="fut", instrument="FUTCOM", expiry=EXPIRY + timedelta(days=21),
        underlying="CRUDEOIL",
    )  # fmt: skip
    contract = rig.market.contracts(rig.session.trading_day).contracts[MCX_ROLE]
    rig.run.contracts.contracts[MCX_ROLE] = dataclasses.replace(contract, symbol="CRUDEOIL")


def set_freeze(rig: Rig, role: str, frzqty: int) -> None:
    """The instrument of ``role`` again, its row unchanged plus ``frzqty``."""
    contract = rig.run.contracts.contracts[role]
    inst = rig.market.instruments[contract.tradingsymbol]
    # Read the stored row (test helper only), add it back through the public add_instrument.
    row: dict[str, Any] = dict(
        rig.broker._state.find_instrument(inst.exchange, inst.tradingsymbol) or {}
    )
    row["frzqty"] = str(frzqty)
    extra = {k: v for k, v in row.items() if k not in ("exch", "token", "tsym")}
    rig.broker.add_instrument(
        inst.exchange, inst.token, inst.tradingsymbol, lot_size=inst.lot,
        tick_size=str(inst.tick), **extra,
    )  # fmt: skip
