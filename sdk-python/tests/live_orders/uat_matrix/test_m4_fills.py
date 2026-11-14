"""UAT matrix family M4, fills and closes (live, UAT only; see ``cases_fills.py``).

Places real orders on the TradeSmart UAT server, behind the matrix gate (``conftest.py``): UAT
mode, the UAT order opt-ins (smoke: the NFO-F round trip, 2 orders), ``TRADESMART_LIVE_ORDER_UAT_
MATRIX=1`` for the full tier, never next to funded mode. A BUY at the last price fills; its
closing SELL that does not fill is the non-strict xfail ``closing-sell`` and the position stays in
the run's position ledger (2 lots per instrument at most). Fill orders only before 15:30 IST.

The module's selected cases (``family.selected_cases``: not deselected, not skipped) run once, on
the session's run (``family.run_for``), at any hour on UAT (``family.hours_refusal``).
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.live_orders.uat_matrix import cases_fills, family, gate
from tests.live_orders.uat_matrix.batch import CaseResult
from tests.live_orders.uat_matrix.cases import Case

pytestmark = [pytest.mark.live_orders, pytest.mark.uat_matrix, pytest.mark.m_fills]

CASES = cases_fills.family_cases()


@pytest.fixture(scope="module")
def results(request: pytest.FixtureRequest, matrix: gate.MatrixSession) -> Mapping[str, CaseResult]:
    reason = family.hours_refusal(uat=matrix.uat, mock=matrix.mock)
    if reason is not None:
        pytest.skip(reason)
    return cases_fills.run_family(family.run_for(matrix), family.selected_cases(request))


@pytest.mark.parametrize("case", family.params(CASES))
def test_m4_fills(case: Case, results: Mapping[str, CaseResult]) -> None:
    family.report(results, case)
