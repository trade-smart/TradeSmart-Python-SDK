"""UAT matrix family M14: the BSE and BFO canaries, then the BSE grid (``cases_bse``).

One canary per segment first (U9): a canary stuck ``PENDING`` (F26) skips its segment for the
run, naming the stuck order (cancelled once, never again); a canary that rests lets the
segment run. The BFO canary is M1's (one per session, whichever module runs first). Then the
M1 grid on BSE ``ITC`` (EOS rests on BSE). One batch per module run; UAT only (``conftest``).
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.live_orders.uat_matrix import cases_bse, family, gate
from tests.live_orders.uat_matrix.batch import CaseResult, MatrixRun
from tests.live_orders.uat_matrix.cases import Case

pytestmark = [pytest.mark.live_orders, pytest.mark.uat_matrix, pytest.mark.m_bse]


def _build(run: MatrixRun, selected: list[Case]) -> list[Case]:
    return selected


@pytest.fixture(scope="module")
def results(request: pytest.FixtureRequest, matrix: gate.MatrixSession) -> Mapping[str, CaseResult]:
    return family.module_results(request, matrix, _build)


@pytest.mark.parametrize("case", family.params(cases_bse.M14_CASES))
def test_bse_and_bfo(case: Case, results: Mapping[str, CaseResult]) -> None:
    family.report(results, case)
