"""UAT matrix family M15: MCX in trade units (``cases_mcx``).

CRUDEOIL, GOLD, GOLDM and GOLDPETAL futures: the M1 grid far from the market and the off-tick
and part-lot rejections, quantity in trade units (whole lots, lot size from the day's master,
U7; no quantity-1 probe); every accepted order records its quantity units. Placed at any hour
on UAT (``cases_mcx.hours_refusal``). One batch per module run; UAT only (``conftest``).
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.live_orders.uat_matrix import cases_mcx, family, gate
from tests.live_orders.uat_matrix.batch import CaseResult
from tests.live_orders.uat_matrix.cases import Case

pytestmark = [pytest.mark.live_orders, pytest.mark.uat_matrix, pytest.mark.m_mcx]


@pytest.fixture(scope="module")
def results(request: pytest.FixtureRequest, matrix: gate.MatrixSession) -> Mapping[str, CaseResult]:
    reason = cases_mcx.hours_refusal(uat=matrix.uat, mock=matrix.mock)
    if reason is not None:
        pytest.skip(reason)
    run = family.run_for(matrix)
    return family.run_cases(run, cases_mcx.items(family.selected_cases(request)))


@pytest.mark.parametrize("case", family.params(cases_mcx.MCX_CASES))
def test_mcx(case: Case, results: Mapping[str, CaseResult]) -> None:
    family.report(results, case)
