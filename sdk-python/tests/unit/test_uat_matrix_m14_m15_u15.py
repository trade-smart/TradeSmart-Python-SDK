"""Case tables and pure helpers of the UAT matrix families M14 (BSE and BFO canaries), M15 (MCX
in trade units) and the U15 ``algo_id`` probe, offline.

Synthetic contracts and quotes only; nothing here touches TradeSmart.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.live_orders.safety import Refusal
from tests.live_orders.uat_matrix import batch, cases, cases_algo, cases_bse, cases_mcx, family
from tests.live_orders.uat_matrix.contracts import Contract
from tests.live_orders.uat_matrix.pricing import PriceSnapshot
from tradesmart.models.base import IST

MATRIX_DIR = Path(cases.__file__).resolve().parent
BFO_CANARY_ID = "shapes-bfo-sensex-f-nrml-lmt-day-buy-canary"
BSE_CANARY_ID = "bse-bse-b-cnc-lmt-day-buy-canary"


def _mcx(role: str = "mcx-gold-f", *, lot: int = 100, tick: str = "1") -> Contract:
    return Contract(
        role=role,
        exchange="MCX",
        tradingsymbol=f"{role.split('-')[1].upper()}05DEC26",
        token="1",
        symbol=role.split("-")[1].upper(),
        kind="fut",
        lot_size=lot,
        tick_size=Decimal(tick),
        resolved_last_price=Decimal("76543"),
    )


def _snapshot(last: str = "76543", tick: str = "1") -> PriceSnapshot:
    return PriceSnapshot(
        "GOLD05DEC26",
        Decimal(last),
        Decimal(tick),
        lower_circuit=Decimal(last) * Decimal("0.8"),
        upper_circuit=Decimal(last) * Decimal("1.2"),
    )


# -- M14 ----------------------------------------------------------------------------------------


def test_m14_is_the_bse_canary_the_shared_bfo_canary_then_the_bse_grid() -> None:
    table = cases_bse.M14_CASES
    assert table[0].id == BSE_CANARY_ID and table[0].canary
    assert table[1] is cases.case(BFO_CANARY_ID)  # M1's canary, not a second one
    assert [c for c in table if c.canary] == table[:2]
    grid = table[2:]
    assert {c.family for c in grid} == {"bse"} and {c.role for c in grid} == {"bse-b"}
    assert len(grid) == len(cases.shape_grid("bse", "bse-b"))
    assert len({c.id for c in table}) == len(table)


def test_there_is_exactly_one_canary_per_canary_segment() -> None:
    canaries = [c for c in cases.CASES if c.canary]
    assert sorted(c.segment for c in canaries) == ["bfo", "bse"]
    assert cases_bse.BFO_CANARY in canaries and cases_bse.BSE_CANARY in canaries


def test_the_bse_smoke_runs_the_m1_smoke_shapes_and_eos() -> None:
    smoke = {c.id for c in cases_bse.M14_CASES if c.tier == cases.SMOKE}
    assert {BSE_CANARY_ID, BFO_CANARY_ID} <= smoke
    shapes = {
        (c.prctyp, c.ret, c.side)
        for c in cases_bse.M14_CASES
        if c.tier == cases.SMOKE and not c.canary
    }
    assert shapes == set(cases.SMOKE_SHAPES) | {("LMT", "EOS", "BUY")}
    assert "bse-bse-b-mis-lmt-eos-buy" in smoke


def test_eos_rests_on_bse_and_is_refused_on_nse() -> None:
    assert cases.case("bse-bse-b-mis-lmt-eos-buy").outcome == cases.REST
    assert cases.case("shapes-nse-b-mis-lmt-eos-buy").outcome == cases.REJECT


# -- M15 ----------------------------------------------------------------------------------------


def test_the_mcx_smoke_places_a_far_limit_buy_nrml_on_each_commodity() -> None:
    smoke = [c for c in cases_mcx.MCX_CASES if c.tier == cases.SMOKE]
    buys = {c.role for c in smoke if (c.product, c.prctyp, c.ret, c.side) == (
        "NRML", "LMT", "DAY", "BUY")}  # fmt: skip
    assert buys == set(cases.MCX_ROLES)
    assert len(smoke) <= 12


def test_mcx_cases_are_the_grid_and_two_rejection_probes_per_commodity() -> None:
    grid = [c for c in cases.CASES if c.family == "mcx"]
    probes = cases_mcx.PROBE_CASES
    assert [*grid, *probes] == cases_mcx.MCX_CASES
    assert {c.id for c in probes} == {
        f"mcx-{role}-nrml-lmt-day-buy-{probe}"
        for role in cases.MCX_ROLES
        for probe in ("offtick", "lot")
    }
    assert {c.outcome for c in probes} == {cases.REJECT}
    assert {c.family for c in cases_mcx.MCX_CASES} == {"mcx"}
    assert [c.id for c in probes if c.tier == cases.SMOKE] == [
        "mcx-mcx-crudeoil-f-nrml-lmt-day-buy-lot"
    ]


def test_the_mcx_lot_probe_sends_one_unit_more_than_a_lot() -> None:
    lot = next(c for c in cases_mcx.PROBE_CASES if c.id.endswith("gold-f-nrml-lmt-day-buy-lot"))
    plan = cases_mcx.build_plan(lot, _mcx(), _snapshot())
    assert plan["quantity"] == 101  # trade units: one lot of 100 plus one unit
    assert plan["price"] == Decimal("68888")


def test_the_mcx_lot_probe_skips_on_a_one_unit_lot_and_never_sends_one_unit() -> None:
    lot = next(c for c in cases_mcx.PROBE_CASES if "goldpetal" in c.id and c.id.endswith("-lot"))
    with pytest.raises(Refusal, match="lot size 1"):
        cases_mcx.build_plan(lot, _mcx("mcx-goldpetal-f", lot=1), _snapshot("7650"))


def test_the_mcx_offtick_probe_is_off_the_quote_tick() -> None:
    probe = next(
        c for c in cases_mcx.PROBE_CASES if c.id.endswith("gold-f-nrml-lmt-day-buy-offtick")
    )
    plan = cases_mcx.build_plan(probe, _mcx(), _snapshot())
    assert plan["price"] == Decimal("68888.01")
    assert plan["price"] % Decimal("1") != 0
    assert plan["quantity"] == 100
    with pytest.raises(Refusal, match="tick"):
        cases_mcx.build_plan(probe, _mcx(tick="0.01"), _snapshot(tick="0.01"))


def test_an_mcx_far_buy_is_priced_in_the_quote_units_on_the_quote_tick() -> None:
    """GOLD quotes per 10 g (``prcftr`` 100 is on the security info and the book row, not the
    quote): the order price is the quote's last price - 10 %, floored to the quote's tick, with
    no factor applied; the quantity is one lot in trade units."""
    grid = cases.case("mcx-mcx-gold-f-nrml-lmt-day-buy")
    plan = batch.default_plan(grid, _mcx(), _snapshot("76543", "1"))
    assert plan["price"] == Decimal("68888") and plan["quantity"] == 100
    goldm = _mcx("mcx-goldm-f", lot=10)
    plan = batch.default_plan(cases.case("mcx-mcx-goldm-f-nrml-sl-lmt-day-buy"), goldm,
                              _snapshot("76500", "1"))  # fmt: skip
    assert plan["quantity"] == 10
    assert all(plan[k] % 1 == 0 for k in ("price", "trigger_price"))
    assert plan["trigger_price"] > Decimal("76500") and plan["price"] >= plan["trigger_price"]


def test_mcx_items_record_units_on_every_case_and_plan_only_the_probes() -> None:
    items = cases_mcx.items(cases_mcx.MCX_CASES)
    assert [i.case for i in items] == cases_mcx.MCX_CASES
    assert all(i.after_settle is cases_mcx.record_units for i in items)
    assert all((i.plan is not None) == (i.case in cases_mcx.PROBE_CASES) for i in items)


def test_mcx_hours_are_the_mcx_window_not_the_nse_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADESMART_LIVE_ORDER_OUTSIDE_HOURS", raising=False)
    evening = datetime(2026, 11, 13, 20, 0, tzinfo=IST)
    assert cases_mcx.hours_refusal(evening) is None
    assert family.hours_refusal(evening) is not None
    assert cases_mcx.hours_refusal(datetime(2026, 11, 13, 23, 20, tzinfo=IST)) is not None
    assert cases_mcx.hours_refusal(datetime(2026, 11, 13, 9, 1, tzinfo=IST)) is not None
    assert cases_mcx.hours_refusal(datetime(2026, 11, 14, 12, 0, tzinfo=IST)) is not None


# -- U15 ----------------------------------------------------------------------------------------


def test_the_test_algo_id_is_synthetic() -> None:
    assert cases_algo.ALGO_ID == "TESTALGO1"
    assert not cases_algo.ALGO_ID.isdigit()  # never a real (numeric) exchange algo id


def test_algo_cases_are_a_few_far_limit_orders_recorded_only() -> None:
    table = cases_algo.ALGO_CASES
    assert 2 <= len(table) <= 4
    assert {c.family for c in table} == {"algo"}
    assert {c.prctyp for c in table} == {"LMT"} and {c.ret for c in table} == {"DAY"}
    assert {c.outcome for c in table} == {cases.RECORD}
    assert {c.segment for c in table} <= {"nse", "nfo"}  # never BSE-side (F26)
    assert all(c.id.startswith("algo-") for c in table)
    assert sum(c.tier == cases.SMOKE for c in table) == 1
    assert cases_algo.MODIFY_CASE in {c.id for c in table}


def test_an_algo_plan_is_the_default_plan_plus_the_test_algo_id() -> None:
    case = next(c for c in cases_algo.ALGO_CASES if c.role == "nfo-nifty-f")
    nifty = Contract(
        role="nfo-nifty-f", exchange="NFO", tradingsymbol="NIFTY25NOV26F", token="5",
        symbol="NIFTY", kind="fut", lot_size=65, tick_size=Decimal("0.1"),
        resolved_last_price=Decimal("26040"),
    )  # fmt: skip
    snap = PriceSnapshot("NIFTY25NOV26F", Decimal("26040"), Decimal("0.1"))
    plan = cases_algo.build_plan(case, nifty, snap)
    expected: dict[str, Any] = {**batch.default_plan(case, nifty, snap), "algo_id": "TESTALGO1"}
    assert plan == expected


def test_items_carry_the_algo_plan_and_hook() -> None:
    items = cases_algo.items(cases_algo.ALGO_CASES)
    assert all(i.plan is cases_algo.build_plan for i in items)
    assert all(i.after_settle is cases_algo.after_settle for i in items)


# -- the modules ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "marker"),
    [("test_bse", "m_bse"), ("test_mcx", "m_mcx"), ("test_z_algo_id", "m_algo")],
)
def test_family_modules_carry_their_markers(module: str, marker: str) -> None:
    import importlib

    mod = importlib.import_module(f"tests.live_orders.uat_matrix.{module}")
    names = [m.name for m in mod.pytestmark]
    assert names == ["live_orders", "uat_matrix", marker]


def test_the_algo_id_module_is_collected_last_in_the_package() -> None:
    """An order with a test algo id that sticks (as BSE orders with an algo id do, F26) trips its
    segment's breaker: the probe runs after every other family of the package."""
    modules = sorted(p.name for p in MATRIX_DIR.glob("test_*.py"))
    assert modules[-1] == "test_z_algo_id.py"
