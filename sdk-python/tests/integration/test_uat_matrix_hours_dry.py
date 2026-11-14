"""Market hours are checked right before every place, not once per module (final review I2;
offline broker).

A clock that passes 15:15 IST in the middle of a batch makes the remaining cases skip with the
reason and nothing sent (no ledger verdict, so a later run inside hours places them). The same
rule applies to burst, sliced, the async views path and every GTT write (GTT keeps its stricter
refusal of ``TRADESMART_LIVE_ORDER_OUTSIDE_HOURS``). The AMO family keeps its own window; MCX
cases use the MCX window; ``TRADESMART_LIVE_ORDER_OUTSIDE_HOURS=1`` still lifts the rule for the
regular families (owner ruling).

These rigs build a session that is not UAT (``session.uat`` False), so they pin the rule that
stays for any session that is not UAT; on UAT there is no hours rule at all (owner, 2026-11-13:
``test_uat_matrix_anytime_dry.py``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from tests.integration.test_uat_matrix_gtt_dry import GttMarket
from tests.integration.uat_family_rig import family_rig
from tests.integration.uat_t7_rig import add_mcx, set_freeze
from tests.live_orders.safety import OUTSIDE_HOURS_ENV
from tests.live_orders.uat_matrix import batch, cases, cases_burst, cases_gtt, cases_sliced
from tradesmart.models.base import IST

BEFORE = datetime(2026, 11, 13, 15, 10, tzinfo=IST)
AFTER = datetime(2026, 11, 13, 15, 20, tzinfo=IST)
EVENING = datetime(2026, 11, 13, 20, 0, tzinfo=IST)


def _crossing(inside: int) -> Callable[[], datetime]:
    """A clock that reads 15:10 IST for its first ``inside`` reads, then 15:20 IST."""
    lock = threading.Lock()
    reads = [0]

    def now() -> datetime:
        with lock:
            reads[0] += 1
            return BEFORE if reads[0] <= inside else AFTER

    return now


def _resting(role: str, count: int) -> list[cases.Case]:
    table = [
        c
        for c in cases.CASES
        if c.role == role and c.prctyp == "LMT" and c.outcome == cases.REST and not c.canary
    ]
    return table[:count]


def test_a_clock_passing_1515_mid_batch_stops_new_places(tmp_path: Path) -> None:
    batch_cases = _resting("nse-b", 3)
    assert len(batch_cases) == 3
    with family_rig(tmp_path) as rig:
        rig.run.now = _crossing(inside=1)
        results = batch.run_batch(rig.run, batch_cases)
        assert len(rig.requests("PlaceOrder")) == 1
        skipped = [r for r in results if r.verdict == batch.SKIP]
        assert len(skipped) == 2
        assert all("closed" in r.reason for r in skipped)
        for result in skipped:  # no ledger verdict: a run inside hours places them later
            assert rig.session.ledger.done(result.case_id) is None


def test_outside_hours_skips_every_regular_case_with_nothing_sent(tmp_path: Path) -> None:
    with family_rig(tmp_path, now=AFTER) as rig:
        results = batch.run_batch(rig.run, _resting("nfo-nifty-f", 2))
        assert [r.verdict for r in results] == [batch.SKIP, batch.SKIP]
        assert rig.requests("PlaceOrder") == []


def test_the_outside_hours_opt_in_still_lifts_the_rule_for_regular_families(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OUTSIDE_HOURS_ENV, "1")
    with family_rig(tmp_path, now=AFTER) as rig:
        [result] = batch.run_batch(rig.run, _resting("nse-b", 1))
        assert result.verdict == batch.PASS
        assert len(rig.requests("PlaceOrder")) == 1


def test_the_hours_rule_per_case() -> None:
    nse = _resting("nse-b", 1)[0]
    mcx = next(c for c in cases.CASES if c.segment == "mcx" and c.prctyp == "LMT")
    amo = cases.Case("amo-nse-b-cnc-lmt-day-buy", "amo", "nse-b", "CNC", "LMT", "DAY", "BUY", 1,
                     cases.REST)  # fmt: skip
    assert batch.hours_refusal(nse, BEFORE) is None
    assert batch.hours_refusal(nse, AFTER) is not None
    assert batch.hours_refusal(mcx, EVENING) is None  # MCX trades in the evening
    assert batch.hours_refusal(nse, EVENING) is not None
    assert batch.hours_refusal(amo, EVENING) is None  # the AMO family has its own window


def test_a_sliced_case_after_1515_sends_nothing(tmp_path: Path) -> None:
    with family_rig(tmp_path, now=AFTER) as rig:
        add_mcx(rig)
        set_freeze(rig, "nfo-nifty-f", 1800)
        spec = next(s for s in cases_sliced.SPECS if s.case.segment == "nse")
        result = cases_sliced.sliced_results(rig.run, [spec.case])[spec.case.id]
        assert result.verdict == batch.SKIP and "closed" in result.reason
        assert rig.requests("PlaceOrder") == []


def test_a_burst_stops_placing_when_the_clock_passes_1515(tmp_path: Path) -> None:
    case = cases_burst.BURST_CASES[0]
    with family_rig(tmp_path) as rig:
        rig.run.now = _crossing(inside=3)
        result = cases_burst.run_burst(rig.run, [case])[case.id]
        placed = len(rig.requests("PlaceOrder"))
        assert 0 < placed < case.orders
        assert "closed" in result.reason


def test_a_gtt_place_after_1515_sends_nothing(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=GttMarket, now=AFTER) as rig:
        result = cases_gtt.run_case(rig.run, cases_gtt.FAR)
        assert result.verdict == batch.SKIP and "closed" in result.reason
        assert rig.requests("PlaceGTTOrder") == []
        assert rig.session.ledger.done(cases_gtt.FAR.id) is None


def test_a_gtt_modify_after_1515_is_not_sent_and_the_gtt_is_cancelled(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        rig.run.now = _crossing(inside=2)  # the case check and the place: inside hours
        result = cases_gtt.run_case(rig.run, cases_gtt.FAR)
        assert result.verdict == batch.SKIP and "closed" in result.reason
        assert len(rig.requests("PlaceGTTOrder")) == 1
        assert rig.requests("ModifyGTTOrder") == []
        assert len(rig.requests("CancelGTTOrder")) == 1


async def test_an_async_views_place_after_1515_sends_nothing(tmp_path: Path) -> None:
    from tests.integration.test_uat_matrix_views_dry import _cases, _run_async
    from tests.integration.uat_modify_views_rig import modify_views_rig
    from tests.live_orders.uat_matrix import cases_views

    with modify_views_rig(tmp_path, now=AFTER) as rig:
        [ioc] = [c for c in _cases("nse-b", asynchronous=True) if cases_views.path_of(c) == "ioc"]
        result = (await _run_async(rig, [ioc]))[ioc.id]
        assert result.verdict == batch.SKIP and "closed" in result.reason
        assert rig.count("PlaceOrder") == 0
        assert rig.session.ledger.done(ioc.id) is None
