"""UAT matrix family M8: burst and pacing, and the ``stress`` tier (inventory §3.2).

50 far LIMIT BUYs on NSE-B placed back to back under one family-level reservation, paced by the
run's pacer (180 places per minute), the matrix order pacer (6/s) and the SDK's own order limiter
(10/s, 200/min; never raised), in waves within ``MAX_OPEN``, each wave cancelled and confirmed;
the ``stress`` tier (``-m stress`` and ``TRADESMART_LIVE_ORDER_UAT_MATRIX=1``) places 650 at the
run's 180/min cap. At any hour on UAT (owner, 2026-11-13); UAT, or the production mock session
(``conftest``).
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.live_orders.uat_matrix import cases_burst, family, gate
from tests.live_orders.uat_matrix.batch import CaseResult
from tests.live_orders.uat_matrix.cases import Case

pytestmark = [pytest.mark.live_orders, pytest.mark.uat_matrix, pytest.mark.m_burst]


@pytest.fixture(scope="module")
def results(request: pytest.FixtureRequest, matrix: gate.MatrixSession) -> Mapping[str, CaseResult]:
    reason = family.hours_refusal(uat=matrix.uat, mock=matrix.mock)
    if reason is not None:
        pytest.skip(reason)
    return cases_burst.run_burst(family.run_for(matrix), family.selected_cases(request))


@pytest.mark.parametrize("case", family.params(cases_burst.BURST_CASES))
def test_burst(case: Case, results: Mapping[str, CaseResult]) -> None:
    family.report(results, case)
