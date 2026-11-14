"""Dry run of the UAT matrix probe U15 (``algo_id``, recorded only) on the offline broker.

The real family code (``cases_algo``, ``family.run_cases``, the batch engine) runs end to end on
a UAT-like market: each far LIMIT order carries the synthetic test ``algo_id`` per call (and one
modify carries it too), the answer is recorded (accepted or the refusal words), nothing else of
the run sends an ``algo_id`` and the client's order rate limit is never raised.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.integration.uat_family_rig import family_rig
from tests.integration.uat_like import UatLikeMarket
from tests.live_orders.uat_matrix import batch, cases, cases_algo, family
from tradesmart.testing import drills

ALGO_REFUSAL = "Rejected : RMS:Invalid algo id"


@dataclass
class AlgoRefusingMarket(UatLikeMarket):
    """UAT-like, but a place call with an ``algo_id`` is refused in the reply."""

    def _decide(self, place: Callable[..., str], kwargs: dict[str, Any]) -> str:
        if kwargs.get("algo_id"):
            self.broker.fail_next("PlaceOrder", drills.reject(ALGO_REFUSAL))
        return super()._decide(place, kwargs)


def test_algo_orders_carry_the_test_algo_id_and_are_recorded(tmp_path: Path) -> None:
    table = cases_algo.ALGO_CASES
    with family_rig(tmp_path) as rig:
        results = family.run_cases(rig.run, cases_algo.items(table))
        assert {results[c.id].verdict for c in table} == {batch.RECORD}
        assert all(results[c.id].status == "CANCELED" for c in table)
        placed = rig.requests("PlaceOrder")
        assert len(placed) == len(table)
        assert {p.get("algo_id") for p in placed} == {cases_algo.ALGO_ID}
        records = {r["case_id"]: r for r in rig.records()}
        for case in table:
            note = records[case.id]["notes"]["algo"]
            assert note["sent"] == cases_algo.ALGO_ID
            assert "echoed" in note


def test_one_modify_carries_the_test_algo_id_and_its_answer_is_recorded(tmp_path: Path) -> None:
    with family_rig(tmp_path) as rig:
        results = family.run_cases(rig.run, cases_algo.items(cases_algo.ALGO_CASES))
        modifies = rig.requests("ModifyOrder")
        assert len(modifies) == 1
        assert modifies[0].get("algo_id") == cases_algo.ALGO_ID
        record = {r["case_id"]: r for r in rig.records()}[cases_algo.MODIFY_CASE]
        assert record["notes"]["algo"]["modify"] == "acknowledged"
        assert results[cases_algo.MODIFY_CASE].verdict == batch.RECORD
        assert len(rig.market.cancels) == len(cases_algo.ALGO_CASES)  # one cancel each


def test_a_refused_algo_id_is_recorded_with_the_words(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=AlgoRefusingMarket) as rig:
        results = family.run_cases(rig.run, cases_algo.items(cases_algo.ALGO_CASES))
        for case in cases_algo.ALGO_CASES:
            result = results[case.id]
            assert result.verdict == batch.RECORD and ALGO_REFUSAL in result.reason
        assert rig.requests("ModifyOrder") == []
        assert rig.session.guard.tripped("nse") is None and rig.session.guard.tripped("nfo") is None


def test_no_other_case_sends_an_algo_id_and_the_rate_limit_stays(tmp_path: Path) -> None:
    shapes = [c for c in cases.CASES if c.family == "shapes"][:4]
    with family_rig(tmp_path) as rig:
        family.run_cases(rig.run, shapes)
        family.run_cases(rig.run, cases_algo.items(cases_algo.ALGO_CASES))
        placed = rig.requests("PlaceOrder")
        assert [p.get("algo_id") for p in placed[: len(shapes)]] == [None] * len(shapes)
        config = rig.session.client.config
        assert config.algo_id is None and config.order_rate_limit_per_second <= 10
