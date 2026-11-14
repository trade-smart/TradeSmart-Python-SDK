"""UAT matrix family M11: tags and encoding (inventory §3.2; ``cases_tags``).

Far orders on NSE ``M&M-EQ``: the trading symbol and the tag come back unchanged in the book and
the history (the echoed tag length is recorded; a tag cut short or dropped is recorded, a
changed symbol or an otherwise changed tag fails); every place tag of the run is unique, short
and of this run. Probe tags (the longest UAT keeps, spaces, ``&``, non-ASCII) are not run: the
never-resend calls send only the run's own tags (``cases_tags``). One batch per module run; UAT
only.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.live_orders.uat_matrix import batch, cases_tags, family, gate
from tests.live_orders.uat_matrix.batch import BatchItem, CaseResult, MatrixRun
from tests.live_orders.uat_matrix.cases import Case
from tests.live_orders.uat_matrix.ledger import LEDGER_NAME

pytestmark = [pytest.mark.live_orders, pytest.mark.uat_matrix, pytest.mark.m_tags]


def _build(run: MatrixRun, selected: list[Case]) -> list[BatchItem]:
    return cases_tags.items(selected)


@pytest.fixture(scope="module")
def results(request: pytest.FixtureRequest, matrix: gate.MatrixSession) -> Mapping[str, CaseResult]:
    return family.module_results(request, matrix, _build)


@pytest.mark.parametrize("case", family.params(cases_tags.TAG_CASES))
def test_symbol_and_tag_round_trip(case: Case, results: Mapping[str, CaseResult]) -> None:
    result = results.get(case.id)
    if result is None:
        pytest.fail(f"case {case.id} was not run by its module's batch")
    batch.report(cases_tags.judged(result))  # a tag cut short or dropped is recorded


@pytest.mark.smoke
def test_tags_are_unique_over_the_run(
    results: Mapping[str, CaseResult], matrix: gate.MatrixSession
) -> None:
    ledger = matrix.run_dir / LEDGER_NAME
    if not ledger.exists():
        pytest.skip("no order placed in this run")
    problems = cases_tags.tag_problems(ledger, matrix.run_id)
    assert not problems, problems
