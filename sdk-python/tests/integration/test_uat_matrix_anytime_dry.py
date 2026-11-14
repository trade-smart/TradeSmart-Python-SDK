"""On UAT the matrix places at any hour (owner, 2026-11-13; offline broker).

"In the UAT environment we don't need to limit ourselves to 3:15 pm": a UAT session
(``session.uat``) has no market-hours refusal and no 15:30 fill cut-off, in the batch engine,
burst, sliced, the async views path, the fill families and the GTT writes (the GTT family's
``TRADESMART_LIVE_ORDER_OUTSIDE_HOURS`` refusal included). A case the closed market refuses or
leaves unfilled is judged as on any day (recorded), never skipped up front. The AMO family keeps
its own windows. A session that is not UAT keeps every hours rule
(``test_uat_matrix_hours_dry.py``).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tests.integration.test_uat_matrix_gtt_dry import GttMarket
from tests.integration.uat_family_rig import family_rig
from tests.integration.uat_fill_rig import make_rig
from tests.integration.uat_t7_rig import add_mcx, set_freeze
from tests.live_orders.safety import OUTSIDE_HOURS_ENV
from tests.live_orders.uat_matrix import (
    batch,
    cases,
    cases_amo,
    cases_burst,
    cases_fills,
    cases_gtt,
    cases_sliced,
)
from tradesmart.models.base import IST

EVENING = datetime(2026, 11, 13, 20, 0, tzinfo=IST)  # a Friday, after the close
SATURDAY = datetime(2026, 11, 14, 10, 0, tzinfo=IST)
MORNING = datetime(2026, 11, 13, 10, 0, tzinfo=IST)
FILL = "fills-nfo-nifty-f-nrml-lmt-day-buy"


def _resting(role: str, count: int) -> list[cases.Case]:
    table = [
        c
        for c in cases.CASES
        if c.role == role and c.prctyp == "LMT" and c.outcome == cases.REST and not c.canary
    ]
    return table[:count]


def _not_for_hours(result: batch.CaseResult) -> None:
    assert "closed" not in result.reason and "15:30" not in result.reason, result.reason


@pytest.mark.parametrize("now", [EVENING, SATURDAY], ids=["20:00", "saturday"])
def test_a_uat_batch_places_outside_market_hours(tmp_path: Path, now: datetime) -> None:
    with family_rig(tmp_path, now=now, uat=True) as rig:
        results = batch.run_batch(rig.run, _resting("nse-b", 2))
        assert len(rig.requests("PlaceOrder")) == 2
        for result in results:
            _not_for_hours(result)
            assert result.verdict != batch.SKIP, result.reason


def test_a_session_that_is_not_uat_still_skips_outside_market_hours(tmp_path: Path) -> None:
    with family_rig(tmp_path, now=EVENING) as rig:
        results = batch.run_batch(rig.run, _resting("nse-b", 1))
        assert [r.verdict for r in results] == [batch.SKIP]
        assert rig.requests("PlaceOrder") == []


def test_a_uat_mcx_case_places_at_night(tmp_path: Path) -> None:
    night = datetime(2026, 11, 14, 2, 0, tzinfo=IST)
    mcx = [c for c in cases.CASES if c.segment == "mcx" and c.prctyp == "LMT"][:1]
    assert mcx
    with family_rig(tmp_path, now=night, uat=True) as rig:
        add_mcx(rig)
        [result] = batch.run_batch(rig.run, mcx)
        _not_for_hours(result)
        assert len(rig.requests("PlaceOrder")) == 1


def test_a_uat_burst_places_every_order_in_the_evening(tmp_path: Path) -> None:
    case = cases_burst.BURST_CASES[0]
    with family_rig(tmp_path, now=EVENING, uat=True) as rig:
        result = cases_burst.run_burst(rig.run, [case])[case.id]
        _not_for_hours(result)
        assert len(rig.requests("PlaceOrder")) == case.orders


def test_a_uat_sliced_case_places_in_the_evening(tmp_path: Path) -> None:
    with family_rig(tmp_path, now=EVENING, uat=True) as rig:
        add_mcx(rig)
        set_freeze(rig, "nfo-nifty-f", 1800)
        spec = next(s for s in cases_sliced.SPECS if s.case.segment == "nse")
        result = cases_sliced.sliced_results(rig.run, [spec.case])[spec.case.id]
        _not_for_hours(result)
        assert rig.requests("PlaceOrder") != []


def test_a_uat_gtt_is_written_in_the_evening_even_with_the_outside_hours_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OUTSIDE_HOURS_ENV, "1")
    with family_rig(tmp_path, market_type=GttMarket, now=EVENING, uat=True) as rig:
        result = cases_gtt.run_case(rig.run, cases_gtt.FAR)
        _not_for_hours(result)
        assert OUTSIDE_HOURS_ENV not in result.reason
        assert len(rig.requests("PlaceGTTOrder")) == 1
        assert len(rig.requests("ModifyGTTOrder")) == 1


async def test_a_uat_async_views_place_in_the_evening_is_sent(tmp_path: Path) -> None:
    from tests.integration.test_uat_matrix_views_dry import _cases, _run_async
    from tests.integration.uat_modify_views_rig import modify_views_rig
    from tests.live_orders.uat_matrix import cases_views

    with modify_views_rig(tmp_path, now=EVENING, uat=True) as rig:
        [ioc] = [c for c in _cases("nse-b", asynchronous=True) if cases_views.path_of(c) == "ioc"]
        result = (await _run_async(rig, [ioc]))[ioc.id]
        _not_for_hours(result)
        assert rig.count("PlaceOrder") == 1


def test_a_uat_fill_case_runs_after_the_1530_cut_off(tmp_path: Path) -> None:
    [case] = [c for c in cases_fills.family_cases() if c.id == FILL]
    for rig in make_rig(tmp_path, now=EVENING, uat=True):
        result = cases_fills.run_family(rig.run, [case])[case.id]
        _not_for_hours(result)
        assert rig.requests("PlaceOrder") >= 1


def test_the_amo_family_keeps_its_own_window_on_uat(tmp_path: Path) -> None:
    amo = next(
        c for c in cases_amo.AMO_CASES if c.segment == "nse" and c.id != cases_amo.INSIDE_PROBE.id
    )
    with family_rig(tmp_path, now=MORNING, uat=True) as rig:  # inside NSE hours
        result = cases_amo.run_amo(rig.run, [amo], MORNING)[amo.id]
        assert result.verdict == batch.SKIP
        assert rig.requests("PlaceOrder") == []
