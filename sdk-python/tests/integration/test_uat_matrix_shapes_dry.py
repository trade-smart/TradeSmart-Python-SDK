"""Dry run of the UAT matrix family M1 (shape grid) on the offline broker (no network).

The real family code (``cases`` M1 table, ``family.run_cases``, the batch engine) runs end to
end on ``UatLikeMarket``; every case gets the verdict UAT is expected to give it.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tests.integration.uat_family_rig import family_rig
from tests.live_orders.uat_matrix import batch, cases, family
from tests.live_orders.uat_matrix.cases import Case
from tradesmart.models.base import IST

SHAPES = [c for c in cases.CASES if c.family == "shapes"]


def expected(case: Case) -> str:
    """The verdict on the UAT-like market: documented outcomes pass, undocumented record."""
    return batch.RECORD if case.outcome == cases.RECORD else batch.PASS


def test_the_full_m1_grid_gets_the_expected_verdict_per_case(tmp_path: Path) -> None:
    # Default 200 per minute on a simulated clock (D8): the grid places more than 200 orders.
    with family_rig(tmp_path) as rig:
        results = family.run_cases(rig.run, SHAPES)
        verdicts = {case.id: results[case.id].verdict for case in SHAPES}
        wrong = {
            case.id: (verdicts[case.id], results[case.id].reason)
            for case in SHAPES
            if verdicts[case.id] != expected(case)
        }
        assert not wrong, wrong
        placed = rig.requests("PlaceOrder")
        assert len(placed) == len(SHAPES)  # one place call per case, never resent
        tags = [p["remarks"] for p in placed]
        assert len(tags) == len(set(tags))
        assert all(o.status in ("CANCELED", "REJECTED") for o in rig.broker.orders)
        assert len(rig.market.cancels) == len(set(rig.market.cancels))  # one cancel per order
        assert sorted(r["case_id"] for r in rig.records()) == sorted(c.id for c in SHAPES)


def test_the_bfo_canary_runs_first_and_lets_bfo_run(tmp_path: Path) -> None:
    bfo = [c for c in SHAPES if c.segment == "bfo"]
    with family_rig(tmp_path) as rig:
        results = family.run_cases(rig.run, list(reversed(bfo)))
        canary = next(c for c in bfo if c.canary)
        first = rig.requests("PlaceOrder")[0]
        assert first["tsym"] == rig.run.contracts.contracts[canary.role].tradingsymbol
        assert results[canary.id].verdict == batch.PASS
        assert {r.verdict for r in results.values()} <= {batch.PASS, batch.RECORD}


def test_the_smoke_tier_is_six_shapes_per_segment_plus_the_canary(tmp_path: Path) -> None:
    smoke = cases.select(cases.SMOKE, SHAPES)
    assert len(smoke) == 3 * 6 + 1
    with family_rig(tmp_path) as rig:
        results = family.run_cases(rig.run, smoke)
        assert {r.verdict for r in results.values()} == {batch.PASS}
        assert len(rig.requests("PlaceOrder")) == len(smoke)


def test_a_case_runs_once_per_run_and_its_result_is_reused(tmp_path: Path) -> None:
    some = SHAPES[:5]
    with family_rig(tmp_path) as rig:
        first = family.run_cases(rig.run, some)
        again = family.run_cases(rig.run, some[2:])
        assert len(rig.requests("PlaceOrder")) == 5
        assert all(again[c.id] is first[c.id] for c in some[2:])


def test_another_run_does_not_reuse_results(tmp_path: Path) -> None:
    some = SHAPES[:2]
    with family_rig(tmp_path / "a") as rig:
        family.run_cases(rig.run, some)
    with family_rig(tmp_path / "b") as other:
        family.run_cases(other.run, some)
        assert len(other.requests("PlaceOrder")) == 2


def test_outside_market_hours_the_families_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADESMART_LIVE_ORDER_OUTSIDE_HOURS", raising=False)
    assert family.hours_refusal(datetime(2026, 11, 13, 10, 0, tzinfo=IST)) is None
    reason = family.hours_refusal(datetime(2026, 11, 13, 20, 0, tzinfo=IST))
    assert reason is not None and "closed" in reason
    weekend = family.hours_refusal(datetime(2026, 11, 14, 10, 0, tzinfo=IST))
    assert weekend is not None and "weekend" in weekend
