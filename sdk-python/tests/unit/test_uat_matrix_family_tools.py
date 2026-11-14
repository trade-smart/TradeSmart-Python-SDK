"""The M4 / M5 / M12 live modules' shape and their case tables, offline: module markers, the
shared session run (``family``), the resume registry, the session-wide ``nse-hold`` role."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.live_orders.uat_matrix import cases, cases_cobo, cases_fills, cases_hold, contracts

MATRIX_DIR = Path(__file__).resolve().parents[1] / "live_orders" / "uat_matrix"
MODULES = {
    "test_m4_fills.py": ("m_fills", cases_fills.family_cases),
    "test_m5_cobo.py": ("m_cobo", cases_cobo.family_cases),
    "test_m12_hold.py": ("m_hold", cases_hold.family_cases),
}


@pytest.mark.parametrize("name", sorted(MODULES))
def test_each_family_module_runs_on_the_session_run_with_the_hours_rule(name: str) -> None:
    marker, family_cases = MODULES[name]
    source = (MATRIX_DIR / name).read_text(encoding="utf-8")
    assert (
        f"pytestmark = [pytest.mark.live_orders, pytest.mark.uat_matrix, pytest.mark.{marker}]"
        in source
    )
    for needle in (
        "family.hours_refusal(uat=matrix.uat, mock=matrix.mock)",
        "family.run_for(matrix)",
        "family.params(CASES)",
        "family.selected_cases(request)",
        "family.report(results, case)",
    ):
        assert needle in source, (name, needle)
    assert "open_run" not in source and "run_batch" not in source
    ids = [c.id for c in family_cases()]
    assert len(ids) == len(set(ids)) and all(i == i.lower() and " " not in i for i in ids)


@pytest.mark.parametrize("name", ["cases_fills.py", "cases_cobo.py", "cases_hold.py"])
def test_family_modules_place_only_through_the_session_run(name: str) -> None:
    source = (MATRIX_DIR / name).read_text(encoding="utf-8")
    assert "batch.run_batch(" not in source and "family.run_cases(" in source


def test_family_case_ids_are_unique_across_the_matrix_tables() -> None:
    ids = [c.id for c in cases.CASES]
    ids += [c.id for c in cases_cobo.family_cases()] + [c.id for c in cases_hold.family_cases()]
    assert len(ids) == len(set(ids))


def test_the_resume_registry_has_the_cobo_and_hold_cases_once() -> None:
    registered = cases.registered()
    ids = [c.id for c in registered]
    assert len(ids) == len(set(ids))
    assert set(ids) >= {c.id for c in cases.CASES}
    assert set(ids) >= {c.id for c in cases_cobo.family_cases()}
    assert set(ids) >= {c.id for c in cases_hold.family_cases()}
    cases.register(cases_cobo.family_cases())  # registering again adds nothing
    assert [c.id for c in cases.registered()] == ids


def test_the_held_stock_role_is_no_resolved_role() -> None:
    """``nse-hold`` lives on the session's run (set by M12 only): no family resolves it."""
    assert cases_hold.ROLE not in contracts.ROLES
    users = [c for c in cases.registered() if c.role == cases_hold.ROLE]
    assert {c.family for c in users} == {"hold"}
