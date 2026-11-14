"""Final review minors of the UAT matrix (offline broker): breaker trips persisted from
every path, AMO LIMIT prices at least 10 % from the last price, a stuck AMO canary's segment
block restored on resume, ``calls.modify`` re-checking the STOP file and the breaker after its
book read, and the run-id lock file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.integration.uat_family_rig import family_rig


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


# -- breaker trips are persisted from every path --------------------------------------------------


def test_a_trip_outside_run_batch_is_in_the_ledger_at_once(tmp_path: Path) -> None:
    with family_rig(tmp_path) as rig:
        guard = rig.session.guard
        for _ in range(3):  # as a burst, sliced, async views or GTT path would
            guard.note_rejection("nfo", expected=False)
        trips = [x for x in _lines(rig.ledger_path()) if x["type"] == "trip"]
        assert [(t["segment"], "unexpected rejections" in t["reason"]) for t in trips] == [
            ("nfo", True)
        ]
        guard.note_check_the_account("nfo", "again")  # tripped already: no second line
        assert len([x for x in _lines(rig.ledger_path()) if x["type"] == "trip"]) == 1


def test_a_resumed_run_does_not_write_the_restored_trips_again(tmp_path: Path) -> None:
    from tests.live_orders.uat_matrix import batch

    with family_rig(tmp_path) as rig:
        rig.session.guard.note_stuck("bse", "9001")
        before = [x for x in _lines(rig.ledger_path()) if x["type"] == "trip"]
        assert len(before) == 1
        batch._restore(rig.run)  # what a resumed run's open_run does
        rig.run.session.guard.trip("bse", "restored")
        after = [x for x in _lines(rig.ledger_path()) if x["type"] == "trip"]
        assert after == before


# -- a stuck AMO canary blocks its segment after a resume too ------------------------------------


def test_a_resumed_run_keeps_a_stuck_amo_canary_blocking_its_segment(tmp_path: Path) -> None:
    from tests.live_orders.uat_matrix import batch, cases_amo

    with family_rig(tmp_path) as rig:
        rig.session.ledger.verdict(cases_amo.CANARY.id, batch.STUCK, reason="an earlier process")
        batch._restore(rig.run)  # what a resumed run's open_run does
        assert "bfo" in rig.run.issues.stuck_segments  # as in the process that saw it stuck
        assert "bfo" not in rig.run.issues.canary_passed


def test_a_resumed_run_never_lets_a_passed_amo_canary_unlock_its_segment(tmp_path: Path) -> None:
    from tests.live_orders.uat_matrix import batch, cases_amo

    with family_rig(tmp_path) as rig:
        rig.session.ledger.verdict(cases_amo.CANARY.id, batch.PASS, reason="an earlier process")
        batch._restore(rig.run)
        assert "bfo" not in rig.run.issues.canary_passed
        assert "bfo" not in rig.run.issues.stuck_segments
