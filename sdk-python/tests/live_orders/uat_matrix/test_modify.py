"""UAT matrix family M2: modify variants (live, UAT only; see ``cases_modify.py``).

One batch per pytest run for the selected cases, on the session's shared run
(``family.run_for``), at any hour on UAT (owner, 2026-11-13): per instrument a far LIMIT BUY and
a stop BUY rest and are modified in sequence (price, quantity, trigger, type, product, tag,
validity), then cancelled once and modified once more (a definitive refusal expected). Every
call goes through ``calls`` (never resent); the order stream runs for the whole batch. Opt-ins
and tiers: ``gate.py``; the dry run is ``tests/integration/test_uat_matrix_modify_dry.py``.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.live_orders.uat_matrix import cases_modify, family, gate, views
from tests.live_orders.uat_matrix.batch import CaseResult
from tests.live_orders.uat_matrix.cases import Case

pytestmark = [pytest.mark.live_orders, pytest.mark.uat_matrix, pytest.mark.m_modify]


@pytest.fixture(scope="module")
def results(request: pytest.FixtureRequest, matrix: gate.MatrixSession) -> Mapping[str, CaseResult]:
    reason = family.hours_refusal(uat=matrix.uat, mock=matrix.mock)
    if reason is not None:
        pytest.skip(reason)
    selected = family.selected_cases(request)
    if not selected:
        return {}
    run = family.run_for(matrix)
    with views.order_stream(matrix.client) as watcher:
        run.watcher = watcher
        try:
            return cases_modify.run_cases(run, selected)
        finally:
            run.watcher = None


@pytest.mark.parametrize("case", family.params(cases_modify.CASES))
def test_modify_variants(case: Case, results: Mapping[str, CaseResult]) -> None:
    family.report(results, case)
