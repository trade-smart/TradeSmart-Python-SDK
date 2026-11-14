"""UAT matrix family M13: GTT orders (``cases_gtt``; UAT only; writes at any hour on UAT,
owner 2026-11-13; ``cases_gtt.write_refusal`` keeps the market-hours rule for any other session).

- ``gtt-nse-b-cnc-lmt-day-buy`` (smoke): the GTT design spec §5 test under the matrix ledger: a
  far ``LTP_A_O`` GTT on NSE ``ITC-EQ`` placed once, read back by tag, its trigger modified once,
  cancelled, confirmed gone and never fired.
- ``gtt-nse-b-cnc-lmt-day-buy-fires`` (full): an ``LTP_B_O`` GTT with the trigger above the last
  price fires at once; its far order (``add_ord_id`` = the alert id) is cancelled once.
- ``gtt_alert_types()`` and ``triggered_gtts()``: read-only (smoke).

Every GTT write goes through ``calls`` (never resent); the session teardown cancels every
pending GTT of the run once and lists any left (CHECK THE ACCOUNT).
"""

from __future__ import annotations

import os

import pytest

from tests.live_orders import mock_session
from tests.live_orders.uat_matrix import cases_gtt, family, gate
from tests.live_orders.uat_matrix.cases import Case

pytestmark = [pytest.mark.live_orders, pytest.mark.uat_matrix, pytest.mark.m_gtt]


@pytest.fixture(scope="module", autouse=True)
def _never_in_a_mock_session(matrix: gate.MatrixSession) -> None:
    """A mock session sends no GTT, and reads none either (the collection hook skips the module
    already: belt)."""
    if matrix.mock:
        pytest.skip(mock_session.NO_AMO_GTT)


@pytest.mark.parametrize("case", family.params(cases_gtt.GTT_CASES))
def test_gtt_case(case: Case, matrix: gate.MatrixSession) -> None:
    reason = cases_gtt.write_refusal(os.environ, uat=matrix.uat, mock=matrix.mock)
    if reason is not None:
        pytest.skip(reason)
    cases_gtt.report(cases_gtt.run_case(family.run_for(matrix), case))


@pytest.mark.smoke
def test_gtt_alert_types_are_a_list_of_words(matrix: gate.MatrixSession) -> None:
    cases_gtt.report(cases_gtt.read_alert_types(family.run_for(matrix)))


@pytest.mark.smoke
def test_triggered_gtts_are_readable(matrix: gate.MatrixSession) -> None:
    cases_gtt.report(cases_gtt.read_triggered(family.run_for(matrix)))
