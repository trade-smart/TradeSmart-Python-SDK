"""Dry run of the UAT matrix family M14 (BSE and BFO canaries, BSE grid) on the offline broker.

The real family code (``cases_bse``, ``family.run_cases``, the batch engine) runs end to end on a
UAT-like market with BSE ``ITC``: a BSE canary stuck ``PENDING`` (F26, as UAT behaved) skips
BSE for the run with the stuck order named, and the order gets one cancel only; a canary that
rests lets the BSE grid (EOS included) run; the BFO canary is M1's, placed once per session and
first among the BFO orders; a stuck BFO canary skips BFO only (one cancel, BSE unaffected).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.integration.uat_family_rig import Rig, family_rig
from tests.integration.uat_like import UatLikeMarket
from tests.live_orders.uat_matrix import batch, calls, cases, cases_bse, family
from tests.live_orders.uat_matrix.cases import Case

BSE_ITC = "ITC"
BFO_FUTURE = "BSXFUT27NOV26"  # the rig's SENSEX future: the BFO canary's instrument


@dataclass
class BseMarket(UatLikeMarket):
    """UAT-like with the BSE ``ITC`` role (BSE orders stay PENDING, F26)."""

    def __post_init__(self) -> None:
        self.add("BSE", "500875", BSE_ITC, last="400.00", lower="320.00", upper="480.00",
                 role="bse-b", instrument="EQ")  # fmt: skip


@dataclass
class FixedBseMarket(BseMarket):
    """BSE behaves like NSE (F26 fixed): orders rest and a cancel cancels."""

    def _sticks(self, exchange: str, tradingsymbol: str) -> bool:
        return tradingsymbol in self.stuck


def expected(case: Case) -> str:
    return batch.RECORD if case.outcome == cases.RECORD else batch.PASS


def _placed(rig: Rig, tradingsymbol: str) -> list[dict[str, Any]]:
    return [p for p in rig.requests("PlaceOrder") if p["tsym"] == tradingsymbol]


def test_a_stuck_bse_canary_skips_bse_and_names_the_stuck_order(tmp_path: Path) -> None:
    table = cases_bse.M14_CASES
    with family_rig(tmp_path, market_type=BseMarket) as rig:
        results = family.run_cases(rig.run, table)
        canary = results[cases_bse.BSE_CANARY.id]
        assert canary.verdict == batch.STUCK and canary.issue == "F26"
        stuck_id = canary.order_ids[0]
        assert stuck_id in canary.reason and "skipped for the run" in canary.reason
        assert len(_placed(rig, BSE_ITC)) == 1  # only the canary went out on BSE
        for case in table[2:]:
            result = results[case.id]
            assert result.verdict == batch.SKIP, (case.id, result.reason)
            assert "F26" in result.reason and stuck_id in result.reason
        assert rig.run.issues.stuck_orders == {stuck_id}
        assert rig.market.cancels.count(stuck_id) == 1
        assert results[cases_bse.BFO_CANARY.id].verdict == batch.PASS  # BFO is not BSE
        with pytest.raises(pytest.skip.Exception, match=stuck_id):
            batch.report(canary)
        calls.teardown(rig.session)
        assert rig.market.cancels.count(stuck_id) == 1  # never cancelled twice


def test_a_bse_canary_that_rests_lets_the_bse_grid_run_eos_included(tmp_path: Path) -> None:
    table = cases_bse.M14_CASES
    with family_rig(tmp_path, market_type=FixedBseMarket) as rig:
        results = family.run_cases(rig.run, table)
        wrong = {c.id: (results[c.id].verdict, results[c.id].reason) for c in table
                 if results[c.id].verdict != expected(c)}  # fmt: skip
        assert not wrong, wrong
        eos = results["bse-bse-b-mis-lmt-eos-buy"]
        assert eos.verdict == batch.PASS and eos.reason.startswith("OPEN")  # rested, then cancelled
        bse = _placed(rig, BSE_ITC)
        assert len(bse) == len(table) - 1  # every BSE case once, the BFO canary on BFO
        canary_tag = results[cases_bse.BSE_CANARY.id].record["calls"][0]["tag"]
        assert bse[0]["remarks"] == canary_tag  # the canary went first


def test_the_smoke_tier_runs_the_canaries_and_the_bse_smoke_shapes(tmp_path: Path) -> None:
    smoke = cases.select(cases.SMOKE, cases_bse.M14_CASES)
    with family_rig(tmp_path, market_type=FixedBseMarket) as rig:
        results = family.run_cases(rig.run, smoke)
        assert {r.verdict for r in results.values()} == {batch.PASS}
        assert len(rig.requests("PlaceOrder")) == len(smoke)


@pytest.mark.parametrize("m14_first", [True, False])
def test_one_bfo_canary_per_session_shared_with_m1(tmp_path: Path, m14_first: bool) -> None:
    m1_bfo = [c for c in cases.CASES if c.family == "shapes" and c.segment == "bfo"]
    m14 = cases_bse.M14_CASES
    canary = cases_bse.BFO_CANARY
    with family_rig(tmp_path, market_type=FixedBseMarket) as rig:
        first, second = (m14, m1_bfo) if m14_first else (m1_bfo, m14)
        one = family.run_cases(rig.run, first)
        two = family.run_cases(rig.run, second)
        assert one[canary.id] is two[canary.id]
        bfo = [p for p in rig.requests("PlaceOrder") if p["exch"] == "BFO"]
        assert len(bfo) == len(m1_bfo)  # the canary is in M1's list: placed once
        # The canary went first: M1's SENSEX future shapes share its trading symbol, so the
        # first BFO order is told apart by the canary's own tag.
        canary_tag = one[canary.id].record["calls"][0]["tag"]
        assert bfo[0]["remarks"] == canary_tag
        assert [p["exch"] for p in rig.requests("PlaceOrder")].count("BSE") == len(m14) - 1


def test_a_stuck_bfo_canary_skips_bfo_and_leaves_bse_running(tmp_path: Path) -> None:
    m1_bfo = [c for c in cases.CASES if c.family == "shapes" and c.segment == "bfo"]
    m14 = cases_bse.M14_CASES
    canary = cases_bse.BFO_CANARY
    with family_rig(tmp_path, market_type=FixedBseMarket, stuck={BFO_FUTURE}) as rig:
        results = {**family.run_cases(rig.run, m14), **family.run_cases(rig.run, m1_bfo)}
        stuck = results[canary.id]
        assert stuck.verdict == batch.STUCK and stuck.issue == "F26"
        stuck_id = stuck.order_ids[0]
        assert stuck_id in stuck.reason and "BFO is skipped for the run" in stuck.reason
        for case in m1_bfo:
            if case is canary:
                continue
            result = results[case.id]
            assert result.verdict == batch.SKIP, (case.id, result.reason)
            assert "F26" in result.reason and stuck_id in result.reason
        bfo = [p for p in rig.requests("PlaceOrder") if p["exch"] == "BFO"]
        assert len(bfo) == 1  # only the canary went out on BFO
        assert rig.run.issues.stuck_orders == {stuck_id}
        calls.teardown(rig.session)
        assert rig.market.cancels.count(stuck_id) == 1  # one cancel in total, teardown included
        # BSE is not BFO: its canary rests and its grid runs.
        for case in m14:
            if case is canary:
                continue
            assert results[case.id].verdict == expected(case), (case.id, results[case.id].reason)
        assert rig.session.guard.tripped("bse") is None
        assert rig.session.guard.tripped("bfo") is not None
