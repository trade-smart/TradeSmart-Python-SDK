"""UAT matrix family M6: after-market orders (inventory §3.2, U12; ``cases_amo``).

Outside market hours only (each segment's own session; MCX trades until 23:55 IST in winter, so
MCX AMOs wait until 00:10): far ``amo=True`` orders per segment, placed, modified, cancelled; one
AMO probe inside hours. Every AMO is cancelled and confirmed final before the run ends: the
``results`` fixture checks it after the module's tests, and also when the batch raised (an AMO
still open is CHECK THE ACCOUNT: it would reach the exchange at the open); after Ctrl-C the
session end lists the run's AMOs (``conftest.end_session``). One batch per module run; UAT only.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

import pytest

from tests.live_orders import mock_session
from tests.live_orders.uat_matrix import cases_amo, family, gate
from tests.live_orders.uat_matrix.batch import CaseResult
from tests.live_orders.uat_matrix.cases import Case

pytestmark = [pytest.mark.live_orders, pytest.mark.uat_matrix, pytest.mark.m_amo]


@pytest.fixture(scope="module", autouse=True)
def _never_in_a_mock_session(matrix: gate.MatrixSession) -> None:
    """A mock session sends no AMO (the collection hook skips the module already: belt)."""
    if matrix.mock:
        pytest.skip(mock_session.NO_AMO_GTT)


@pytest.fixture(scope="module")
def results(
    request: pytest.FixtureRequest, matrix: gate.MatrixSession
) -> Iterator[Mapping[str, CaseResult]]:
    with cases_amo.left_open_guard(matrix):
        yield cases_amo.module_results(request, matrix)


@pytest.mark.parametrize("case", family.params(cases_amo.AMO_CASES))
def test_amo(case: Case, results: Mapping[str, CaseResult]) -> None:
    family.report(results, case)
