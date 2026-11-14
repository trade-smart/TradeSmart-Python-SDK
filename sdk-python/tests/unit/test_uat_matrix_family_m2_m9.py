"""The M2 / M9 family modules on ``family.py`` (Task 5 fix round 1): the market-hours gate, the
shared run's results (``family.results_of``) and no direct ``run_batch`` in the family code."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.live_orders.uat_matrix import cases_modify, cases_views, family
from tests.live_orders.uat_matrix.batch import CaseResult

MATRIX = Path(__file__).resolve().parents[1] / "live_orders" / "uat_matrix"


@pytest.mark.parametrize("name", ["test_modify.py", "test_views.py"])
def test_the_family_modules_check_the_hours_rule_on_the_shared_run(name: str) -> None:
    source = (MATRIX / name).read_text(encoding="utf-8")
    assert "family.hours_refusal(uat=matrix.uat, mock=matrix.mock)" in source
    assert "pytest.skip(reason)" in source
    assert "family.run_for(matrix)" in source and "family.selected_cases(request)" in source
    assert "batch.open_run" not in source and "family.report(results, case)" in source


@pytest.mark.parametrize("name", ["cases_modify.py", "cases_views.py"])
def test_the_family_code_runs_batches_through_family_only(name: str) -> None:
    source = (MATRIX / name).read_text(encoding="utf-8")
    assert "run_batch(" not in source and "family.run_cases(" in source


class _Run:
    """Stands in for a ``MatrixRun`` as a cache key (``family`` keys its caches per run)."""


def test_results_of_is_the_runs_result_cache_used_by_run_cases() -> None:
    run = _Run()
    case = cases_views.ASYNC_CASES[0]
    done = CaseResult(case.id, "pass", "cached")
    family.results_of(run)[case.id] = done  # type: ignore[arg-type]
    assert family.results_of(run) is family.results_of(run)  # type: ignore[arg-type]
    assert family.run_cases(run, [case]) == {case.id: done}  # type: ignore[arg-type]  # no batch


def test_the_item_builders_return_batch_items_with_their_hooks() -> None:
    modify_items = cases_modify.items(cases_modify.CASES[:2])
    assert [i.case for i in modify_items] == cases_modify.CASES[:2]
    assert all(isinstance(i.after_settle, cases_modify.ModifySequence) for i in modify_items)
    view_items = {cases_views.path_of(i.case): i for i in cases_views.items(cases_views.CASES[:5])}
    assert view_items["reject"].plan is cases_views.off_tick_plan
    assert isinstance(view_items["open"].after_settle, cases_modify.ModifySequence)
    assert view_items["fill"].after_settle is None and view_items["fill"].plan is None


def test_the_off_tick_view_plan_of_an_option_is_four_percent_below_plus_half_a_tick() -> None:
    from decimal import Decimal

    from tests.live_orders.uat_matrix.contracts import Contract
    from tests.live_orders.uat_matrix.pricing import PriceSnapshot

    case = next(c for c in cases_views.CASES if c.role == "nfo-nifty-ce-atm")
    option = Contract(
        role=case.role, exchange="NFO", tradingsymbol="X", token="1", symbol="NIFTY", kind="opt",
        lot_size=75, tick_size=Decimal("0.05"), resolved_last_price=Decimal("100"),
    )  # fmt: skip
    snap = PriceSnapshot("X", Decimal("100"), Decimal("0.05"), Decimal("1"), Decimal("500"))
    plan = cases_views.off_tick_plan(case, option, snap)
    assert plan["price"] == Decimal("96.00") + Decimal("0.025")
