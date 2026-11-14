"""UAT matrix family M1: the order shape grid (inventory §3.2; ``cases`` family ``shapes``).

Every allowed product × {LMT, SL-LMT} × {DAY, IOC, EOS} × {BUY, SELL} per instrument, far from the
market (non-filling), plus MKT and SL-MKT once per product × side (the engine's market path:
ALGO_CHK passes, F27); the BFO canary first (F26, U9). One batch per module run
(``family.module_results``); each test reports its case. UAT only (``conftest``).
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.live_orders.uat_matrix import cases, family, gate
from tests.live_orders.uat_matrix.batch import CaseResult, MatrixRun
from tests.live_orders.uat_matrix.cases import Case

pytestmark = [pytest.mark.live_orders, pytest.mark.uat_matrix, pytest.mark.m_shapes]

SHAPES = [c for c in cases.CASES if c.family == "shapes"]


def _build(run: MatrixRun, selected: list[Case]) -> list[Case]:
    return selected


@pytest.fixture(scope="module")
def results(request: pytest.FixtureRequest, matrix: gate.MatrixSession) -> Mapping[str, CaseResult]:
    return family.module_results(request, matrix, _build)


@pytest.mark.parametrize("case", family.params(SHAPES))
def test_shape(case: Case, results: Mapping[str, CaseResult]) -> None:
    family.report(results, case)
