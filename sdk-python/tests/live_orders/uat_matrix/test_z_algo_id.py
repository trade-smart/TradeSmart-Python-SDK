"""UAT matrix probe U15: far LIMIT orders with a synthetic test ``algo_id`` (``cases_algo``).

Recorded only (accepted, or the refusal words); one modify carries the test id too. The SDK's
per-call ``algo_id`` is used; ``ClientConfig.algo_id`` and the order rate limit are untouched.
The module name sorts last in the package on purpose: an order with the test id that sticks
(as BSE-side orders with an algo id do, F26) trips its segment's breaker, so every other family
runs first. One batch per module run; UAT only (``conftest``).
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.live_orders.uat_matrix import cases_algo, family, gate
from tests.live_orders.uat_matrix.batch import BatchItem, CaseResult, MatrixRun
from tests.live_orders.uat_matrix.cases import Case

pytestmark = [pytest.mark.live_orders, pytest.mark.uat_matrix, pytest.mark.m_algo]


def _build(run: MatrixRun, selected: list[Case]) -> list[BatchItem]:
    return cases_algo.items(selected)


@pytest.fixture(scope="module")
def results(request: pytest.FixtureRequest, matrix: gate.MatrixSession) -> Mapping[str, CaseResult]:
    return family.module_results(request, matrix, _build)


@pytest.mark.parametrize("case", family.params(cases_algo.ALGO_CASES))
def test_algo_id(case: Case, results: Mapping[str, CaseResult]) -> None:
    family.report(results, case)
