"""UAT matrix family M12, holdings SELL (live, UAT only; see ``cases_hold.py``).

Places real orders on the TradeSmart UAT server, behind the matrix gate (``conftest.py``): UAT
mode, the UAT order opt-ins (smoke: the far SELL), ``TRADESMART_LIVE_ORDER_UAT_MATRIX=1`` for the
full tier (the SELL of one held share at the last price, owner answer U10), never next to funded
mode. No holdings on the account: both cases skip with the reason, nothing sent. The filling SELL
sells an existing holding: it is recorded, never the ``closing-sell`` xfail.

The module's selected cases (``family.selected_cases``: not deselected, not skipped) run once, on
the session's run (``family.run_for``), at any hour on UAT (``family.hours_refusal``).
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.live_orders.uat_matrix import cases_hold, family, gate
from tests.live_orders.uat_matrix.batch import CaseResult
from tests.live_orders.uat_matrix.cases import Case

pytestmark = [pytest.mark.live_orders, pytest.mark.uat_matrix, pytest.mark.m_hold]

CASES = cases_hold.family_cases()


@pytest.fixture(scope="module")
def results(request: pytest.FixtureRequest, matrix: gate.MatrixSession) -> Mapping[str, CaseResult]:
    reason = family.hours_refusal(uat=matrix.uat, mock=matrix.mock)
    if reason is not None:
        pytest.skip(reason)
    return cases_hold.run_family(family.run_for(matrix), family.selected_cases(request))


@pytest.mark.parametrize("case", family.params(CASES))
def test_m12_hold(case: Case, results: Mapping[str, CaseResult]) -> None:
    family.report(results, case)
