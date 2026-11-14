"""UAT matrix family M3: rejection paths (inventory §3.2; ``cases_reject``).

Each case breaks one rule (off-tick, part lot, beyond a circuit, a stop limit below its trigger,
a product or validity the segment does not take, an unknown or expired symbol, disclosed
quantity) and keeps the rest non-filling. A refusal passes; an undocumented answer is recorded;
a probe accepted by mistake is cancelled once and recorded "accepted", never a pass. One batch
per module run; UAT only (``conftest``).
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.live_orders.uat_matrix import cases_reject, family, gate
from tests.live_orders.uat_matrix.batch import BatchItem, CaseResult, MatrixRun
from tests.live_orders.uat_matrix.cases import Case

pytestmark = [pytest.mark.live_orders, pytest.mark.uat_matrix, pytest.mark.m_reject]


def _build(run: MatrixRun, selected: list[Case]) -> list[BatchItem]:
    session = run.session
    expired = cases_reject.expired_symbols(
        session.client, run.contracts, selected, session.trading_day
    )
    return cases_reject.items(selected, expired=expired)


@pytest.fixture(scope="module")
def results(request: pytest.FixtureRequest, matrix: gate.MatrixSession) -> Mapping[str, CaseResult]:
    return family.module_results(request, matrix, _build)


@pytest.mark.parametrize("case", family.params(cases_reject.REJECT_CASES))
def test_rejection_path(case: Case, results: Mapping[str, CaseResult]) -> None:
    family.report(results, case)
