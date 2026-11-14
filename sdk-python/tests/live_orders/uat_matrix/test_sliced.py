"""UAT matrix family M7: sliced orders (inventory §3.2; ``cases_sliced``).

One ``place_sliced_order`` call per case (``calls.place_sliced``, never resent): 2 × 1 lot, the
freeze sizing (2 × the freeze slice + 1 lot, no ``max_slice_quantity``) on the NIFTY future, the
NIFTY ATM CE and the SENSEX ATM CE, IOC slices far (cancelled by the exchange), SELL slices far
above, disclosed quantity per slice, MCX in trade units. Every open slice is cancelled once and
confirmed. At any hour on UAT (owner, 2026-11-13); UAT only (``conftest``).
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.live_orders.uat_matrix import cases_sliced, family, gate
from tests.live_orders.uat_matrix.batch import CaseResult
from tests.live_orders.uat_matrix.cases import Case

pytestmark = [pytest.mark.live_orders, pytest.mark.uat_matrix, pytest.mark.m_sliced]


@pytest.fixture(scope="module")
def results(request: pytest.FixtureRequest, matrix: gate.MatrixSession) -> Mapping[str, CaseResult]:
    reason = family.hours_refusal(uat=matrix.uat, mock=matrix.mock)
    if reason is not None:
        pytest.skip(reason)
    return cases_sliced.sliced_results(family.run_for(matrix), family.selected_cases(request))


@pytest.mark.parametrize("case", family.params(cases_sliced.SLICED_CASES))
def test_sliced(case: Case, results: Mapping[str, CaseResult]) -> None:
    family.report(results, case)
