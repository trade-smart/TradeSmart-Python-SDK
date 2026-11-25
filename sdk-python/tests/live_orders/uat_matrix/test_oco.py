"""UAT matrix family M16: OCO orders (``cases_oco``; writes at any hour on UAT, as the GTT cases;
``cases_oco.write_refusal`` keeps the market-hours rule for any other session).

- ``oco-far-buy`` (smoke): a far BUY OCO on NSE ``ITC-EQ`` placed once, read back, its stop-loss
  modified once, cancelled, confirmed gone and never fired, then a modify on the cancelled id
  (expected ``OrderRejectedError``).
- ``oco-far-sell``, ``oco-far-sl-lmt`` (full): the mirror, and a stop-loss leg of ``SL-LMT``.
- ``oco-fires-buy``, ``oco-fires-sell`` (full): an OCO whose target trigger is already crossed;
  the one fired order is found by ``add_ord_id``, cancelled once and confirmed final, and which
  leg fired is recorded (both legs' orders, or one that cannot be cleaned up: CHECK THE ACCOUNT).
- ``oco-slots-reversed`` (full, the Python SDK only: skipped for a driver SDK): as
  ``oco-fires-buy`` sent through ``request()`` with the docs-example slot order; RECORD.

Every OCO write goes through ``calls`` (never resent); the session teardown cancels every
pending OCO of the run once and lists any left (CHECK THE ACCOUNT). A mock session runs this
family only with the owner's switch ``TRADESMART_LIVE_MOCK_OCO`` (exactly ``oco in mock session
<today's IST date>``), and then only the three far cases (the collection hook skips the rest);
after the module ``cases_oco.mock_cleanup`` cancels the run's own OCOs and the orders they
placed and confirms them gone / final (anything unconfirmed: CHECK THE ACCOUNT, an error here).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests.live_orders import mock_session
from tests.live_orders.uat_matrix import calls, cases_oco, family, gate
from tests.live_orders.uat_matrix.cases import Case

pytestmark = [pytest.mark.live_orders, pytest.mark.uat_matrix, pytest.mark.m_oco]


@pytest.fixture(scope="module", autouse=True)
def _mock_session_rules(matrix: gate.MatrixSession) -> Iterator[None]:
    """A mock session sends no OCO without the owner's switch (the collection hook skips the
    module already: belt); with it, the clean-up of E4 runs after the module's cases."""
    if matrix.mock and not matrix.mock_oco:
        pytest.skip(mock_session.NO_OCO)
    yield
    if matrix.mock:
        cases_oco.report(cases_oco.mock_cleanup(family.run_for(matrix)))


@pytest.mark.parametrize("case", family.params(cases_oco.OCO_CASES))
def test_oco_case(case: Case, matrix: gate.MatrixSession) -> None:
    if matrix.mock and case.id not in mock_session.MOCK_OCO_CASES:
        pytest.skip(calls.MOCK_NO_FIRE)  # E5 (the collection hook skips it already: belt)
    if case.id == cases_oco.SLOTS_REVERSED.id and matrix.sdk.name != calls.PYTHON:
        pytest.skip(f"{case.id}: the Python SDK only (sent through request())")
    reason = cases_oco.write_refusal(
        os.environ, uat=matrix.uat, mock=matrix.mock, mock_oco=matrix.mock_oco
    )
    if reason is not None:
        pytest.skip(reason)
    cases_oco.report(cases_oco.run_case(family.run_for(matrix), case))
