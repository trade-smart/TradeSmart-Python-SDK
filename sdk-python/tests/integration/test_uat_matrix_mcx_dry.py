"""Dry run of the UAT matrix family M15 (MCX in trade units) on the offline broker.

The real family code (``cases_mcx``: the grid, the off-tick and part-lot probes, the units
note; ``family.run_cases``; the batch engine) runs end to end on a UAT-like market with
CRUDEOIL, GOLD, GOLDM and GOLDPETAL futures (quantity in trade units, lot sizes from the day's
master, prices per the quote's unit and tick): every case gets its expected verdict, every order
is whole lots on the quote's tick, and each accepted order records its quantity units.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from tests.integration.uat_family_rig import EXPIRY, family_rig
from tests.integration.uat_like import UatLikeMarket
from tests.live_orders.uat_matrix import batch, cases, cases_mcx, family
from tests.live_orders.uat_matrix.cases import Case

# (role, trading symbol, last price, lot size in trade units); tick 1.00 like UAT's MCX quotes.
COMMODITIES = (
    ("mcx-crudeoil-f", "CRUDEOIL19NOV26", "9210", 100),
    ("mcx-gold-f", "GOLD05DEC26", "76543", 100),  # per 10 g
    ("mcx-goldm-f", "GOLDM05DEC26", "76500", 10),
    ("mcx-goldpetal-f", "GOLDPETAL28NOV26", "7650", 1),  # a one-unit lot
)
LOTS = {symbol: lot for _, symbol, _, lot in COMMODITIES}
LAST = {symbol: Decimal(last) for _, symbol, last, _ in COMMODITIES}


@dataclass
class McxMarket(UatLikeMarket):
    """UAT-like with the four MCX futures of M15."""

    def __post_init__(self) -> None:
        for token, (role, symbol, last, lot) in enumerate(COMMODITIES, start=900):
            self.add("MCX", str(token), symbol, last=last, tick="1.00", lot=lot, role=role,
                     kind="fut", instrument="FUTCOM", expiry=EXPIRY,
                     underlying=role.split("-")[1].upper())  # fmt: skip


def expected(case: Case) -> str:
    if case.id == "mcx-mcx-goldpetal-f-nrml-lmt-day-buy-lot":
        return batch.SKIP  # lot size 1: no part-lot quantity
    return batch.RECORD if case.outcome == cases.RECORD else batch.PASS


def test_every_mcx_case_gets_the_expected_verdict(tmp_path: Path) -> None:
    table = cases_mcx.MCX_CASES
    with family_rig(tmp_path, market_type=McxMarket) as rig:
        results = family.run_cases(rig.run, cases_mcx.items(table))
        wrong = {c.id: (results[c.id].verdict, results[c.id].reason) for c in table
                 if results[c.id].verdict != expected(c)}  # fmt: skip
        assert not wrong, wrong
        lot_skip = results["mcx-mcx-goldpetal-f-nrml-lmt-day-buy-lot"]
        assert "lot size 1" in lot_skip.reason
        placed = rig.requests("PlaceOrder")
        assert len(placed) == len(table) - 1  # one call per case, never resent
        assert all(o.status in ("CANCELED", "REJECTED") for o in rig.broker.orders)


def test_every_mcx_order_is_whole_lots_in_trade_units_on_the_quote_tick(tmp_path: Path) -> None:
    grid = [c for c in cases_mcx.MCX_CASES if c not in cases_mcx.PROBE_CASES]
    with family_rig(tmp_path, market_type=McxMarket) as rig:
        family.run_cases(rig.run, cases_mcx.items(grid))
        placed = rig.requests("PlaceOrder")
        assert placed
        for payload in placed:
            symbol, lot = payload["tsym"], LOTS[payload["tsym"]]
            assert int(payload["qty"]) == lot, (
                payload
            )  # one lot, in units (never 1 unit by default)
            for key in ("prc", "trgprc"):
                if key in payload and payload[key] not in ("", "0", None):
                    price = Decimal(str(payload[key]))
                    assert price % 1 == 0, payload  # on the 1.00 tick, no factor applied
                    assert abs(price - LAST[symbol]) <= LAST[symbol] * Decimal("0.2"), payload


def test_accepted_mcx_orders_record_their_quantity_units(tmp_path: Path) -> None:
    buys = [c for c in cases_mcx.MCX_CASES if c.id.endswith("-nrml-lmt-day-buy")]
    assert len(buys) == 4
    with family_rig(tmp_path, market_type=McxMarket) as rig:
        family.run_cases(rig.run, cases_mcx.items(buys))
        records = {r["case_id"]: r for r in rig.records()}
        for case in buys:
            units = records[case.id]["notes"]["units"]
            lot = rig.run.contracts.contracts[case.role].lot_size
            assert units["quantity_sent"] == lot and units["lot_size"] == lot
            assert units["lots"] == 1 and units["book_quantity"] == lot
            assert "prcftr" in units and "book_lot_size" in units


def test_a_rejected_mcx_order_records_no_units_note(tmp_path: Path) -> None:
    offtick = [
        c for c in cases_mcx.PROBE_CASES if c.id.endswith("crudeoil-f-nrml-lmt-day-buy-offtick")
    ]
    with family_rig(tmp_path, market_type=McxMarket) as rig:
        results = family.run_cases(rig.run, cases_mcx.items(offtick))
        assert results[offtick[0].id].verdict == batch.PASS
        record = rig.records()[0]
        assert "units" not in record["notes"]
        assert int(record["plan"]["quantity"]) == 100
