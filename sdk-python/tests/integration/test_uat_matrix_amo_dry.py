"""Dry run of the UAT matrix family M6 (after-market orders) on the offline broker.

The real family code (``cases_amo``: case table, AMO plans, the modify hook, the session
windows, the left-open check; ``family.run_cases``; the batch engine) runs end to end on a
UAT-like market where an AMO rests ``OPEN`` and an AMO IOC is refused. Every AMO is cancelled
and confirmed final before the run ends; one left open is CHECK THE ACCOUNT; one left behind by
an exception is cancelled by the session teardown (``calls.teardown``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests.integration.uat_family_rig import family_rig
from tests.integration.uat_like import UatLikeMarket
from tests.integration.uat_t7_rig import MCX_SYMBOL, add_mcx
from tests.live_orders.safety import CHECK_THE_ACCOUNT
from tests.live_orders.uat_matrix import batch, calls, cases, cases_amo, family, pricing
from tests.live_orders.uat_matrix import conftest as matrix_conftest
from tests.live_orders.uat_matrix.calls import CaseRef
from tests.live_orders.uat_matrix.cases import Case
from tradesmart.enums import Validity
from tradesmart.models.base import IST
from tradesmart.testing import drills

AMO_IOC = "Rejected : AMO not allowed for IOC orders"
NIGHT = datetime(2026, 11, 13, 6, 0, tzinfo=IST)  # every segment closed, MCX too
EVENING = datetime(2026, 11, 13, 20, 0, tzinfo=IST)  # NSE / NFO / BFO closed, MCX trading
MORNING = datetime(2026, 11, 13, 10, 0, tzinfo=IST)
AMO = cases_amo.AMO_CASES


@dataclass
class AmoMarket(UatLikeMarket):
    """UAT-like; an AMO IOC is refused in the place reply (the inventory's expectation)."""

    def _decide(self, place: Callable[..., str], kwargs: dict[str, Any]) -> str:
        ioc = Validity(str(kwargs.get("validity", "DAY"))) is Validity.IOC
        if kwargs.get("amo") and ioc:
            self.broker.fail_next("PlaceOrder", drills.reject(AMO_IOC))
            return place(**kwargs)
        return super()._decide(place, kwargs)


def expected(case: Case, now: datetime) -> str:
    if cases_amo.refusal(case, now) is not None:
        return batch.SKIP
    if case.canary or case.outcome == cases.REJECT:
        return batch.PASS
    return batch.RECORD


def _lmt_day(case: Case) -> bool:
    return (
        case.prctyp == "LMT" and case.ret == "DAY" and not case.canary
        and case is not cases_amo.INSIDE_PROBE
    )  # fmt: skip


def test_outside_every_session_each_amo_case_gets_its_verdict(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        add_mcx(rig)
        results = cases_amo.run_amo(rig.run, AMO, NIGHT)
        wrong = {
            c.id: (results[c.id].verdict, results[c.id].reason)
            for c in AMO
            if results[c.id].verdict != expected(c, NIGHT)
        }
        assert not wrong, wrong
        placed = rig.requests("PlaceOrder")
        assert len(placed) == len(AMO) - 1  # all but the inside probe, each once
        assert all(p["amo"] == "Yes" for p in placed)  # never a plain order outside hours
        canary = rig.run.contracts.contracts["bfo-sensex-f"].tradingsymbol
        assert placed[0]["tsym"] == canary  # the AMO canary first, no other canary added
        modified = rig.requests("ModifyOrder")
        assert len(modified) == sum(1 for c in AMO if _lmt_day(c))
        assert all(o.status in ("CANCELED", "REJECTED") for o in rig.broker.orders)
        assert len(rig.market.cancels) == len(set(rig.market.cancels))
        assert cases_amo.left_open(rig.session) == []
        assert cases_amo.left_open_failure(rig.session) is None


def test_the_modify_moves_the_price_one_tick_and_is_recorded(tmp_path: Path) -> None:
    case = next(c for c in AMO if c.role == "nse-b" and _lmt_day(c) and c.side == "BUY")
    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        result = cases_amo.run_amo(rig.run, [case], NIGHT)[case.id]
        placed, modified = rig.requests("PlaceOrder")[0], rig.requests("ModifyOrder")[0]
        assert float(modified["prc"]) == pytest.approx(float(placed["prc"]) + 0.05)
        assert result.record["notes"]["modify"]["verdict"] == calls.ACKNOWLEDGED


def test_at_twenty_hours_mcx_waits_for_its_session_to_end(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=AmoMarket, now=EVENING) as rig:
        add_mcx(rig)
        results = cases_amo.run_amo(rig.run, AMO, EVENING)
        for case in AMO:
            assert results[case.id].verdict == expected(case, EVENING), case.id
        mcx = [results[c.id] for c in AMO if c.segment == "mcx"]
        assert mcx and all(r.verdict == batch.SKIP and "MCX" in r.reason for r in mcx)
        assert all(p["tsym"] != MCX_SYMBOL for p in rig.requests("PlaceOrder"))


def test_inside_hours_only_the_inside_probe_runs(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=AmoMarket, now=MORNING) as rig:
        add_mcx(rig)
        results = cases_amo.run_amo(rig.run, AMO, MORNING)
        probe = cases_amo.INSIDE_PROBE
        assert results[probe.id].verdict == batch.RECORD
        others = [results[c.id] for c in AMO if c is not probe]
        assert all(r.verdict == batch.SKIP for r in others)
        placed = rig.requests("PlaceOrder")
        assert len(placed) == 1 and placed[0]["amo"] == "Yes"
        assert all(o.status == "CANCELED" for o in rig.broker.orders)


def test_selected_bfo_cases_get_the_amo_canary_not_a_plain_one(tmp_path: Path) -> None:
    bfo = [c for c in AMO if c.segment == "bfo" and not c.canary]
    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        results = cases_amo.run_amo(rig.run, bfo, NIGHT)
        placed = rig.requests("PlaceOrder")
        assert len(placed) == len(bfo) + 1
        assert all(p["amo"] == "Yes" for p in placed)
        assert {results[c.id].verdict for c in bfo} <= {batch.PASS, batch.RECORD}


def test_an_amo_left_by_an_exception_is_cancelled_at_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = next(c for c in AMO if c.role == "nfo-nifty-f" and _lmt_day(c))

    def broken(run: batch.MatrixRun, works: Any) -> None:
        raise RuntimeError("a bug in the batch cancel step")

    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        monkeypatch.setattr(batch, "_cancel_and_confirm", broken)
        with pytest.raises(RuntimeError, match="batch cancel"):
            cases_amo.run_amo(rig.run, [case], NIGHT)
        left = cases_amo.left_open(rig.session)
        assert left and len(left) == 1  # the AMO is still open after the exception
        monkeypatch.undo()
        report = calls.teardown(rig.session)
        assert report is not None and report.cancelled == left
        assert rig.requests("PlaceOrder")[0]["amo"] == "Yes"
        assert all(o.status == "CANCELED" for o in rig.broker.orders)
        assert cases_amo.left_open(rig.session) == []


def test_an_amo_placed_outside_a_batch_is_ours_and_cancelled_at_teardown(tmp_path: Path) -> None:
    case = next(c for c in AMO if c.role == "nse-b" and _lmt_day(c))
    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        contract = rig.run.contracts.contracts[case.role]
        snapshot = pricing.fresh_quote(rig.session.client, contract)
        rig.session.guard.reserve(case.id, 1)
        outcome = calls.place(
            rig.session, cases_amo.amo_plan(case, contract, snapshot), CaseRef(case.id, "nse")
        )
        assert cases_amo.left_open(rig.session) == [outcome.order_id]
        calls.teardown(rig.session)
        assert cases_amo.left_open(rig.session) == []


def test_an_amo_that_stays_open_is_check_the_account(tmp_path: Path) -> None:
    case = next(c for c in AMO if c.role == "nse-b" and _lmt_day(c))
    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT, stuck={"ITC-EQ"}) as rig:
        result = cases_amo.run_amo(rig.run, [case], NIGHT)[case.id]
        assert result.verdict == batch.STUCK
        assert rig.session.guard.tripped("nse") is not None
        left = cases_amo.left_open(rig.session)
        assert left == result.order_ids
        failure = cases_amo.left_open_failure(rig.session)
        assert failure is not None and CHECK_THE_ACCOUNT in failure and left[0] in failure
        assert "at the open" in failure
        notes = [line for line in rig.ledger_path().read_text().splitlines() if "amo_left_open"
                 in line]  # fmt: skip
        assert len(notes) == 1


# -- fix round 1: AMOs open at the end of the session; the AMO canary kept apart ---------------


class _Reporter:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_line(self, line: str, **markup: Any) -> None:
        self.lines.append(line)


def _interrupt_before_the_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(run: batch.MatrixRun, works: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(batch, "_settle", interrupted)


def test_ctrl_c_between_the_places_and_the_cancel_shows_the_open_amo_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lmt = [c for c in AMO if c.role in ("nse-b", "nfo-nifty-f") and _lmt_day(c)]
    monkeypatch.setattr(matrix_conftest, "NOTICES", [])
    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        _interrupt_before_the_cancel(monkeypatch)
        with pytest.raises(KeyboardInterrupt):
            cases_amo.run_amo(rig.run, lmt, NIGHT)
        assert rig.session.interrupted
        ids = [o.id for o in rig.broker.orders]
        assert len(ids) == len(lmt)
        assert matrix_conftest.end_session(rig.session) is None  # nothing cancelled
        assert rig.market.cancels == [] and rig.requests("CancelOrder") == []
        reporter = _Reporter()
        matrix_conftest.pytest_terminal_summary(reporter)
        text = "\n".join(reporter.lines)
        assert CHECK_THE_ACCOUNT in text and "09:00 IST" in text and "cleanup tool" in text
        assert all(order_id in text for order_id in ids)


def test_a_clean_session_end_shows_no_amo_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = next(c for c in AMO if c.role == "nse-b" and _lmt_day(c))
    monkeypatch.setattr(matrix_conftest, "NOTICES", [])
    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        cases_amo.run_amo(rig.run, [case], NIGHT)
        report = matrix_conftest.end_session(rig.session)
        assert report is not None and not report.not_final
        assert matrix_conftest.NOTICES == []


def test_a_stuck_amo_at_the_session_end_is_in_the_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = next(c for c in AMO if c.role == "nse-b" and _lmt_day(c))
    monkeypatch.setattr(matrix_conftest, "NOTICES", [])
    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT, stuck={"ITC-EQ"}) as rig:
        result = cases_amo.run_amo(rig.run, [case], NIGHT)[case.id]
        matrix_conftest.end_session(rig.session)
        assert len(matrix_conftest.NOTICES) == 1
        assert result.order_ids[0] in matrix_conftest.NOTICES[0]


def test_the_left_open_check_runs_when_the_module_batch_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = next(c for c in AMO if c.role == "nfo-nifty-f" and _lmt_day(c))

    def broken(run: batch.MatrixRun, works: Any) -> None:
        raise RuntimeError("a bug in the batch cancel step")

    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        monkeypatch.setattr(batch, "_cancel_and_confirm", broken)
        with (
            pytest.raises(pytest.fail.Exception, match=CHECK_THE_ACCOUNT) as failed,
            cases_amo.left_open_guard(rig.session),
        ):
            cases_amo.run_amo(rig.run, [case], NIGHT)
        assert isinstance(failed.value.__context__, RuntimeError)


def test_the_left_open_check_runs_after_the_module_tests(tmp_path: Path) -> None:
    case = next(c for c in AMO if c.role == "nse-b" and _lmt_day(c))
    with (
        family_rig(tmp_path, market_type=AmoMarket, now=NIGHT, stuck={"ITC-EQ"}) as rig,
        pytest.raises(pytest.fail.Exception, match="at the open"),
        cases_amo.left_open_guard(rig.session),
    ):
        cases_amo.run_amo(rig.run, [case], NIGHT)
    with (
        family_rig(tmp_path / "clean", market_type=AmoMarket, now=NIGHT) as clean,
        cases_amo.left_open_guard(clean.session),
    ):
        cases_amo.run_amo(clean.run, [case], NIGHT)


def test_the_amo_canary_never_unlocks_regular_bfo_orders(tmp_path: Path) -> None:
    bfo = next(c for c in AMO if c.segment == "bfo" and _lmt_day(c))
    shape = next(c for c in cases.CASES if c.family == "shapes" and c.segment == "bfo"
                 and not c.canary and c.prctyp == "LMT" and c.ret == "DAY")  # fmt: skip
    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        results = cases_amo.run_amo(rig.run, [bfo], NIGHT)
        assert results[bfo.id].verdict == batch.RECORD
        assert "bfo" not in rig.run.issues.canary_passed
        before = len(rig.requests("PlaceOrder"))
        rig.run.now = lambda: MORNING  # regular orders: inside market hours (checked per place)
        family.run_cases(rig.run, [shape])
        regular = rig.requests("PlaceOrder")[before:]
        assert regular[0]["amo"] == "No"  # the regular BFO canary runs first, a plain order
        assert len(regular) == 2


def test_a_later_amo_batch_reuses_the_amo_canary_and_adds_no_plain_one(tmp_path: Path) -> None:
    first, second = [c for c in AMO if c.segment == "bfo" and _lmt_day(c)][:2]
    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        cases_amo.run_amo(rig.run, [first], NIGHT)
        results = cases_amo.run_amo(rig.run, [second], NIGHT)
        assert results[second.id].verdict == batch.RECORD
        placed = rig.requests("PlaceOrder")
        assert len(placed) == 3 and all(p["amo"] == "Yes" for p in placed)
        assert "bfo" not in rig.run.issues.canary_passed


# -- fix round 2: resume, the AMO canary's own slot, confirmed AMOs, nits -------------------------


def _register_amo(monkeypatch: pytest.MonkeyPatch) -> None:
    """As if someone registered the AMO cases for resume (the registry is process-wide)."""
    monkeypatch.setattr(cases, "_REGISTERED", dict(cases._REGISTERED))
    cases.register(AMO)


def _bfo_shape() -> Case:
    return next(c for c in cases.CASES if c.family == "shapes" and c.segment == "bfo"
                and not c.canary and c.prctyp == "LMT" and c.ret == "DAY")  # fmt: skip


def test_a_resumed_run_never_lets_the_amo_canary_unlock_regular_bfo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_amo(monkeypatch)
    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        rig.session.ledger.verdict(cases_amo.CANARY.id, batch.PASS, reason="an earlier process")
        batch._restore(rig.run)  # what a resumed run's open_run does
        assert "bfo" not in rig.run.issues.canary_passed
        rig.run.now = lambda: MORNING  # regular orders: inside market hours (checked per place)
        family.run_cases(rig.run, [_bfo_shape()])
        placed = rig.requests("PlaceOrder")
        canary = next(c for c in cases.CASES if c.canary and c.segment == "bfo")
        assert len(placed) == 2 and placed[0]["amo"] == "No"  # the plain M1 canary first
        assert rig.ledger_path().read_text().count(canary.id) >= 1


def test_a_resumed_run_carries_the_amo_canary_result_over(tmp_path: Path) -> None:
    bfo = next(c for c in AMO if c.segment == "bfo" and _lmt_day(c))
    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        rig.session.ledger.verdict(cases_amo.CANARY.id, batch.PASS, reason="an earlier process")
        result = cases_amo.run_amo(rig.run, [bfo], NIGHT)[bfo.id]
        assert result.verdict == batch.RECORD, result.reason
        placed = rig.requests("PlaceOrder")
        assert len(placed) == 1 and placed[0]["amo"] == "Yes"  # no canary placed again
        assert "bfo" not in rig.run.issues.canary_passed


def test_an_amo_canary_that_did_not_pass_earlier_skips_the_bfo_amos_on_resume(
    tmp_path: Path,
) -> None:
    bfo = next(c for c in AMO if c.segment == "bfo" and _lmt_day(c))
    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        rig.session.ledger.verdict(cases_amo.CANARY.id, batch.STUCK, reason="an earlier process")
        result = cases_amo.run_amo(rig.run, [bfo], NIGHT)[bfo.id]
        assert result.verdict == batch.SKIP and "AMO canary" in result.reason
        assert rig.requests("PlaceOrder") == []


def test_the_ctrl_c_notice_leaves_out_amos_already_confirmed_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = next(c for c in AMO if c.role == "nse-b" and _lmt_day(c))
    second = next(c for c in AMO if c.role == "nfo-nifty-f" and _lmt_day(c))
    monkeypatch.setattr(matrix_conftest, "NOTICES", [])
    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        done = cases_amo.run_amo(rig.run, [first], NIGHT)[first.id]
        _interrupt_before_the_cancel(monkeypatch)
        with pytest.raises(KeyboardInterrupt):
            cases_amo.run_amo(rig.run, [second], NIGHT)
        matrix_conftest.end_session(rig.session)
        (shown,) = matrix_conftest.NOTICES
        notice = shown.splitlines()[-1]  # the AMO part (the general part lists every order)
        assert notice.startswith(cases_amo.NOTICE)
        still_open = [o.id for o in rig.broker.orders if o.status == "OPEN"]
        assert len(still_open) == 1 and still_open[0] in notice
        assert done.order_ids[0] not in notice
        assert "amo_confirmed_final" in rig.ledger_path().read_text()


def test_the_left_open_guard_reads_nothing_without_an_amo_in_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_read(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the book was read although the run placed no AMO")

    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        monkeypatch.setattr(cases_amo, "read_book", no_read)
        with cases_amo.left_open_guard(rig.session):
            pass


def test_a_notice_error_never_replaces_the_teardown_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_teardown(session: Any) -> None:
        raise RuntimeError("teardown broke")

    def broken_notice(session: Any, report: Any) -> None:
        raise ValueError("notice broke")

    monkeypatch.setattr(matrix_conftest, "NOTICES", [])
    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        monkeypatch.setattr(calls, "teardown", broken_teardown)
        monkeypatch.setattr(cases_amo, "open_amo_notice", broken_notice)
        with pytest.raises(RuntimeError, match="teardown broke"):
            matrix_conftest.end_session(rig.session)
        assert len(matrix_conftest.NOTICES) == 1
        assert CHECK_THE_ACCOUNT in matrix_conftest.NOTICES[0]
        assert "notice broke" in matrix_conftest.NOTICES[0]


def test_a_notice_error_after_a_clean_teardown_is_recorded_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_notice(session: Any, report: Any) -> None:
        raise ValueError("notice broke")

    monkeypatch.setattr(matrix_conftest, "NOTICES", [])
    with family_rig(tmp_path, market_type=AmoMarket, now=NIGHT) as rig:
        monkeypatch.setattr(cases_amo, "open_amo_notice", broken_notice)
        assert matrix_conftest.end_session(rig.session) is not None
        assert len(matrix_conftest.NOTICES) == 1 and "notice broke" in matrix_conftest.NOTICES[0]
