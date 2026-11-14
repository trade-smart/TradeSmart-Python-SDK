"""Dry run of the UAT matrix family M8 (burst and pacing, and the ``stress`` tier) offline.

The real family code (``cases_burst``) places far LIMIT BUYs on NSE-B back to back through
``calls.place`` (one family-level reservation), each place on the run's pacer (180 per minute)
and the SDK's own order limiter (10/s, 200/min), in waves that respect ``MAX_OPEN``, each wave
cancelled and confirmed before the next. The 50-order cases measure the SDK's 10/s in real time;
the stress run (650 orders at the 180/min cap) runs the pacer and the SDK limiter on the rig's
simulated clock (D8: the 200 default, no real waiting) and measures the ledger's fsync cost.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from tests.integration.uat_family_rig import family_rig
from tests.live_orders.uat_matrix import batch, cases_burst
from tradesmart.testing import drills

BURST, STRESS = cases_burst.BURST_CASES


def test_the_burst_places_fifty_orders_paced_and_cancels_each_once(tmp_path: Path) -> None:
    with family_rig(tmp_path, simulated_order_clock=False) as rig:
        result = cases_burst.run_burst(rig.run, [BURST])[BURST.id]
        assert result.verdict == batch.PASS, result.reason
        placed = rig.requests("PlaceOrder")
        assert len(placed) == 50 and len({p["remarks"] for p in placed}) == 50
        assert all(p["tsym"] == "ITC-EQ" and p["trantype"] == "B" for p in placed)
        assert Counter(rig.market.cancels) == Counter(result.order_ids)
        assert len(result.order_ids) == 50
        assert all(o.status == "CANCELED" for o in rig.broker.orders)
        assert rig.market.max_open_seen <= 50
        assert rig.session.guard.declared[BURST.id] == 50  # one family-level reservation
        assert rig.session.guard.used[BURST.id] == 50
        notes = result.record["notes"]
        assert notes["placed"] == 50 and notes["waves"] == 1
        # Return times may bunch by latency jitter; the start-to-return rule is the check.
        assert cases_burst.pacing_problems(notes["stamps"]) == []
        assert notes["seconds"] >= 4.0  # 50 sends at 10/s: the SDK limiter paced them
        assert notes["limiter_engaged"]["per_second"] is True
        assert "6/s" in notes["matrix_pacer"]  # live, the matrix order pacer caps the burst
        assert len(rig.requests("GetQuotes")) == 1  # one fresh quote per wave


def test_a_wave_quote_older_than_thirty_seconds_is_taken_again(tmp_path: Path) -> None:
    ticks = iter(range(0, 100_000, 31))  # every clock reading is 31 s after the last one

    def slow_clock() -> float:
        return float(next(ticks))

    with family_rig(tmp_path, simulated_order_clock=False) as rig:
        result = cases_burst.run_burst(rig.run, [BURST], clock=slow_clock)[BURST.id]
        assert result.verdict == batch.PASS, result.reason
        assert len(rig.requests("GetQuotes")) == 50  # a fresh quote before every place


def test_the_burst_respects_max_open_in_waves(tmp_path: Path) -> None:
    with family_rig(tmp_path, max_open=20, simulated_order_clock=False) as rig:
        result = cases_burst.run_burst(rig.run, [BURST])[BURST.id]
        assert result.verdict == batch.PASS, result.reason
        assert result.record["notes"]["waves"] == 3  # 20 + 20 + 10
        assert rig.market.max_open_seen <= 20
        assert len(rig.requests("PlaceOrder")) == 50


def test_an_rms_refusal_stops_the_burst_as_an_environment_limit(tmp_path: Path) -> None:
    with family_rig(tmp_path, rms={"ITC-EQ"}, simulated_order_clock=False) as rig:
        result = cases_burst.run_burst(rig.run, [BURST])[BURST.id]
        assert result.verdict == batch.ENV_LIMIT
        assert len(rig.requests("PlaceOrder")) == 1  # no blind repetition


def test_a_rate_rejection_fails_the_burst_and_stops_it(tmp_path: Path) -> None:
    with family_rig(tmp_path, simulated_order_clock=False) as rig:
        count: list[int] = []

        def tenth_rate_limited(kwargs: dict[str, Any]) -> None:
            count.append(1)
            if len(count) == 10:
                rig.broker.fail_next("PlaceOrder", drills.rate_limit())

        rig.market.on_place = tenth_rate_limited
        result = cases_burst.run_burst(rig.run, [BURST])[BURST.id]
        assert result.verdict == batch.FAIL and "rate" in result.reason
        assert len(rig.requests("PlaceOrder")) == 10
        assert all(o.status in ("CANCELED", "REJECTED") for o in rig.broker.orders)


def test_the_stop_file_mid_burst_stops_placing_and_cancels_what_was_placed(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, simulated_order_clock=False) as rig:
        count: list[int] = []

        def stop_after_ten(kwargs: dict[str, Any]) -> None:
            count.append(1)
            if len(count) == 10:
                (rig.root / "STOP").write_text("stop")

        rig.market.on_place = stop_after_ten
        result = cases_burst.run_burst(rig.run, [BURST])[BURST.id]
        assert result.verdict == batch.SKIP and "STOP" in result.reason
        assert "10 of 50" in result.reason
        assert len(rig.requests("PlaceOrder")) == 10
        assert all(o.status == "CANCELED" for o in rig.broker.orders)


def test_a_place_with_an_unknown_outcome_is_check_the_account(tmp_path: Path) -> None:
    with family_rig(tmp_path, simulated_order_clock=False) as rig:
        count: list[int] = []

        def fifth_times_out(kwargs: dict[str, Any]) -> None:
            count.append(1)
            if len(count) == 5:
                rig.broker.fail_next("PlaceOrder", drills.connect_error())

        rig.market.on_place = fifth_times_out
        result = cases_burst.run_burst(rig.run, [BURST])[BURST.id]
        assert result.verdict == batch.CHECK
        assert len(rig.requests("PlaceOrder")) <= 5
        assert rig.session.guard.tripped("nse") is not None


LPP = "17070: The Price is out of the current LPP range."
RATE = "Invalid Input :  Order Recieved 202 in a current minute exceeds Limit 200 for user"


def test_an_lpp_refusal_in_a_burst_is_an_environment_limit(tmp_path: Path) -> None:
    count: list[int] = []

    def tenth_out_of_band(kwargs: dict[str, Any]) -> str | None:
        count.append(1)
        return LPP if len(count) == 10 else None

    with family_rig(tmp_path) as rig:
        rig.market.band_refusal = tenth_out_of_band
        result = cases_burst.run_burst(rig.run, [BURST], clock=rig.session.clock)[BURST.id]
        assert result.verdict == batch.ENV_LIMIT, result.reason
        assert "environment limit (LPP band)" in result.reason and LPP in result.reason
        assert rig.session.guard.tripped("nse") is None  # neutral to the breaker
        assert all(o.status in ("CANCELED", "REJECTED") for o in rig.broker.orders)


def test_an_lpp_rejection_in_the_book_stops_the_burst_after_its_wave(tmp_path: Path) -> None:
    count: list[int] = []

    def fifth_out_of_band(kwargs: dict[str, Any]) -> str | None:
        count.append(1)
        return LPP if len(count) == 5 else None

    with family_rig(tmp_path, max_open=20) as rig:
        rig.market.band_refusal = fifth_out_of_band
        result = cases_burst.run_burst(rig.run, [BURST], clock=rig.session.clock)[BURST.id]
        assert result.verdict == batch.ENV_LIMIT, result.reason
        assert len(rig.requests("PlaceOrder")) == 20  # no next wave into the same band
        assert result.record["notes"]["waves"] == 1


def test_an_order_rate_refusal_stops_the_burst_as_an_environment_limit(tmp_path: Path) -> None:
    with family_rig(tmp_path) as rig:
        count: list[int] = []

        def tenth_over_the_minute(kwargs: dict[str, Any]) -> None:
            count.append(1)
            if len(count) == 10:
                rig.broker.fail_next("PlaceOrder", drills.reject(RATE))

        rig.market.on_place = tenth_over_the_minute
        result = cases_burst.run_burst(rig.run, [BURST], clock=rig.session.clock)[BURST.id]
        assert result.verdict == batch.ENV_LIMIT, result.reason
        assert "environment limit (order rate)" in result.reason and RATE in result.reason
        assert len(rig.requests("PlaceOrder")) == 10  # no blind repetition
        assert rig.session.guard.tripped("nse") is None
        assert all(o.status in ("CANCELED", "REJECTED") for o in rig.broker.orders)


def test_burst_places_keep_the_runs_per_minute_pace(tmp_path: Path) -> None:
    with family_rig(tmp_path, pace_per_minute=20) as rig:
        result = cases_burst.run_burst(rig.run, [BURST], clock=rig.session.clock)[BURST.id]
        assert result.verdict == batch.PASS, result.reason
        notes = result.record["notes"]
        assert notes["max_starts_per_minute"] <= 20  # the run's pacer, not only the SDK's 200
        assert notes["seconds"] >= 120.0  # 50 places at 20 per minute


def test_the_stress_tier_runs_at_the_runs_cap_and_measures_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_property: Any
) -> None:
    calls: list[float] = []
    fsync = os.fsync

    def timed(fd: int) -> None:
        start = time.perf_counter()
        fsync(fd)
        calls.append(time.perf_counter() - start)

    monkeypatch.setattr(os, "fsync", timed)  # the ledger fsyncs through os.fsync
    # D8: the SDK's 200 per minute default, the run's 180 pace, both on the rig's simulated clock.
    with family_rig(tmp_path) as rig:
        result = cases_burst.run_burst(rig.run, [STRESS], clock=rig.session.clock)[STRESS.id]
        notes = result.record["notes"]
        assert notes["placed"] == 650 and notes["waves"] == 13
        stamps = notes["stamps"]
        per_minute = cases_burst.max_in_window([s[0] for s in stamps], 60.0)
        assert notes["max_starts_per_minute"] == per_minute
        assert notes["limiter_engaged"] == cases_burst.limiter_engaged(stamps)
        # At the cap: the run's pacer held the places at 180 a minute (it waited), the SDK's
        # 200 per minute was never exceeded, and nothing was refused.
        assert per_minute == batch.ORDER_PLACES_PER_MINUTE
        assert notes["limiter_engaged"]["per_minute"] is True
        assert result.verdict == batch.PASS, result.reason
        assert f"at the run's {batch.ORDER_PLACES_PER_MINUTE}/min pace" in result.reason
        assert cases_burst.pacing_problems(stamps) == []
        assert stamps[-1][1] - stamps[0][0] >= 3 * 60.0  # 650 places at 180 a minute
        assert rig.market.max_open_seen <= 50
        assert len(rig.market.cancels) == len(set(rig.market.cancels)) == 650
        record_property("stress_verdict", result.verdict)
        record_property("stress_max_starts_per_minute", per_minute)
        print(f"stress: {result.verdict}, max {per_minute} starts per minute")  # noqa: T201
    assert len(calls) >= 4 * 650  # intent + outcome per place and per cancel
    per_call_ms = 1000 * sum(calls) / len(calls)
    record_property("ledger_fsync_calls", len(calls))
    record_property("ledger_fsync_ms_per_call", round(per_call_ms, 3))
    print(f"ledger fsync: {len(calls)} calls, {per_call_ms:.3f} ms per call")  # noqa: T201
    assert per_call_ms < 50.0
