"""Dry run of the UAT matrix family M2 (modify variants) on the offline broker.

``cases_modify.run_cases`` is the code the live test runs; here it drives a ``UatLikeMarket``
(``uat_modify_views_rig.py``) through the real batch engine, ledger, guard and order stream. Per
instrument one far LIMIT BUY and one stop BUY rest, and each is modified in sequence through
``Placed.modify``: price by a tick both ways, quantity up and down by a lot, the trigger, price
and trigger, the order type, the product (refused), the tag and the validity (recorded); then
one cancel and a modify of the cancelled order (refused). Each accepted modify must show in the
book, as the newest history entry and as a stream update.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.integration.test_mock_session_dry import SAT_11, make_mock
from tests.integration.uat_modify_views_rig import PRODUCT_REFUSAL, modify_views_rig
from tests.integration.uat_modify_views_rig import ModifyViewsRig as Rig
from tests.live_orders.uat_matrix import batch, calls, cases, cases_modify, family
from tests.live_orders.uat_matrix.cases import Case
from tradesmart.testing import drills

FAST = cases_modify.Timing(stream_wait=2.0)
LMT_STEPS = [
    "price-down", "price-up", "qty-up", "qty-down", "product", "tag", "to-sl-lmt",
    "validity-ioc", "cancel", "modify-after-cancel",
]  # fmt: skip
SL_STEPS = [
    "price-up", "price-down", "qty-up", "qty-down", "trigger", "price-trigger", "product", "tag",
    "to-lmt", "validity-ioc", "cancel", "modify-after-cancel",
]  # fmt: skip
EXPECTED_RESULTS = {
    "product": "pass",  # refused, as expected
    "tag": "record",
    "validity-ioc": "record",
}


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    with modify_views_rig(tmp_path) as built:
        yield built


def _case(role: str, prctyp: str = "LMT") -> Case:
    return next(c for c in cases_modify.CASES if c.role == role and c.prctyp == prctyp)


def _steps(result: batch.CaseResult) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = result.record["notes"]["steps"]
    return steps


def test_the_case_table_covers_the_instruments_with_one_lmt_and_one_stop_each() -> None:
    roles = {c.role for c in cases_modify.CASES}
    assert {"nse-b", "nse-t", "nse-e", "nfo-nifty-f", "nfo-banknifty-f", "nfo-stk-f",
            "nfo-nifty-ce-itm1", "nfo-nifty-pe-otm1", "nfo-banknifty-ce-atm",
            "nfo-banknifty-pe-itm1", "nfo-stk-ce-atm", "bfo-sensex-f",
            "bfo-sensex-ce-atm"} <= roles  # fmt: skip
    for role in roles:
        assert sorted(c.prctyp for c in cases_modify.CASES if c.role == role) == ["LMT", "SL-LMT"]
    ids = [c.id for c in cases_modify.CASES]
    assert len(ids) == len(set(ids)) and all(i.startswith("modify-") for i in ids)
    assert _case("nse-b").id == "modify-nse-b-mis-lmt-day-buy"
    assert _case("nfo-nifty-f", "SL-LMT").id == "modify-nfo-nifty-f-nrml-sl-lmt-day-buy"
    smoke = {c.id for c in cases_modify.CASES if c.tier == cases.SMOKE}
    assert smoke == {"modify-nse-b-mis-lmt-day-buy", "modify-nfo-nifty-f-nrml-lmt-day-buy",
                     "modify-bfo-sensex-f-nrml-lmt-day-buy"}  # fmt: skip
    assert all(c.orders == 1 and c.side == "BUY" and c.ret == "DAY" for c in cases_modify.CASES)


def test_every_modify_variant_on_the_uat_like_broker(rig: Rig) -> None:
    results = cases_modify.run_cases(rig.run, cases_modify.CASES, timing=FAST)
    contracts = rig.run.contracts
    ran = [c for c in cases_modify.CASES if contracts.get(c.role) is not None]
    assert len(ran) == 26  # every modify role has an instrument in the rig, LMT and stop each
    for case in cases_modify.CASES:
        result = results[case.id]
        if case not in ran:
            assert result.verdict == batch.SKIP and "no contract" in result.reason
            continue
        assert result.verdict == batch.PASS, (case.id, result.reason, _steps(result))
        steps = _steps(result)
        names = [s["step"] for s in steps]
        assert names == (LMT_STEPS if case.prctyp == "LMT" else SL_STEPS), case.id
        for step in steps:
            want = EXPECTED_RESULTS.get(step["step"], "pass")
            assert step["result"] == want, (case.id, step)
        accepted = [s for s in steps if s["expect"] == "accept" and s["step"] != "cancel"]
        assert all(s["stream_lag"] is not None for s in accepted), (case.id, accepted)
        assert all(s["history"] == "ok" and s["book"] == "ok" for s in accepted)
        product = next(s for s in steps if s["step"] == "product")
        assert product["verdict"] == calls.REJECTED and PRODUCT_REFUSAL in product["words"]
        after = next(s for s in steps if s["step"] == "modify-after-cancel")
        assert after["verdict"] == calls.REJECTED and "not open" in after["words"]
        kinds = [c["kind"] for c in result.record["calls"]]
        modifies = len(names) - 1  # every step but the cancel is one modify call
        assert kinds == ["place", *["modify"] * (modifies - 1), "cancel", "modify"], case.id
        [order_id] = result.order_ids
        assert rig.statuses()[order_id] == "CANCELED"
    assert len(rig.market.cancels) == len(set(rig.market.cancels)) == len(ran) + 1  # + canary
    canary = family.results_of(rig.run).get("shapes-bfo-sensex-f-nrml-lmt-day-buy-canary")
    assert canary is not None and canary.verdict == batch.PASS  # BFO waits for its canary


def test_the_records_carry_the_views_and_the_raw_bodies(rig: Rig) -> None:
    results = cases_modify.run_cases(rig.run, [_case("nse-b")], timing=FAST)
    record = results[_case("nse-b").id].record
    [order_id] = results[_case("nse-b").id].order_ids
    assert record["views"]["orders"][order_id]["history_count"] >= 10
    assert record["views"]["orders"][order_id]["stream_lag"] is not None
    assert record["views"]["mismatches"] == [], record["views"]["mismatches"]
    kinds = [c["kind"] for c in record["raw_calls"]]
    assert kinds.count("modify") == 9 and kinds.count("cancel") == 1
    first = next(c for c in record["raw_calls"] if c["kind"] == "modify")
    assert "changes" in first["request"] and first["verdict"] == calls.ACKNOWLEDGED


def test_a_modify_acknowledged_but_not_applied_fails_the_case(rig: Rig) -> None:
    rig.script[1] = "lie"  # price-down: acknowledged, the book never changes
    [result] = cases_modify.run_cases(rig.run, [_case("nse-b")], timing=FAST).values()
    assert result.verdict == batch.FAIL and "price-down" in result.reason
    first = _steps(result)[0]
    assert first["result"] == "fail" and any("book" in p for p in first["problems"])
    assert [s["step"] for s in _steps(result)] == LMT_STEPS  # the rest still ran, from the book
    assert rig.statuses()[result.order_ids[0]] == "CANCELED"


def test_an_ambiguous_modify_is_check_the_account_and_never_repeated(rig: Rig) -> None:
    rig.broker.fail_next("ModifyOrder", drills.connect_error())  # nothing goes out
    [result] = cases_modify.run_cases(rig.run, [_case("nse-b")], timing=FAST).values()
    assert result.verdict == batch.CHECK
    assert [s["step"] for s in _steps(result)] == ["price-down"]  # the sequence stops
    assert rig.modify_calls == 1 and rig.count("CancelOrder") == 0
    [order_id] = result.order_ids
    assert rig.statuses()[order_id] == "OPEN"  # never touched again: a human checks it
    assert rig.session.guard.tripped("nse") is not None
    assert order_id in rig.session.ledger.flagged()


def test_an_unexpected_refusal_fails_the_step_and_the_sequence_goes_on(rig: Rig) -> None:
    rig.script[3] = "reject:Rejected : ORA:something unexpected"  # qty-up
    [result] = cases_modify.run_cases(rig.run, [_case("nse-b")], timing=FAST).values()
    assert result.verdict == batch.FAIL and "qty-up" in result.reason
    steps = {s["step"]: s for s in _steps(result)}
    assert steps["qty-up"]["result"] == "fail" and steps["qty-up"]["verdict"] == calls.REJECTED
    assert steps["qty-down"]["result"] == "skipped"  # nothing to take back
    assert steps["to-sl-lmt"]["result"] == "pass"


def test_an_rms_refusal_of_a_modify_is_an_environment_limit_not_a_failure(rig: Rig) -> None:
    rig.script[3] = "reject:RED:RULE:{Turnover limit} Current: INR 2,00,000 Set: INR 1,00,000"
    [result] = cases_modify.run_cases(rig.run, [_case("nfo-nifty-f")], timing=FAST).values()
    assert result.verdict == batch.PASS, result.reason
    steps = {s["step"]: s for s in _steps(result)}
    assert steps["qty-up"]["result"] == "environment limit"
    assert steps["qty-down"]["result"] == "skipped"


def test_a_product_change_that_is_accepted_is_recorded(rig: Rig) -> None:
    rig.refuse_product = False
    [result] = cases_modify.run_cases(rig.run, [_case("nse-b", "SL-LMT")], timing=FAST).values()
    assert result.verdict == batch.PASS
    product = next(s for s in _steps(result) if s["step"] == "product")
    assert product["result"] == "record" and product["verdict"] == calls.ACKNOWLEDGED


def test_the_stop_file_mid_sequence_sends_no_further_modify(rig: Rig) -> None:
    rig.script[3] = "stop"  # the STOP file appears while qty-up goes out
    [result] = cases_modify.run_cases(rig.run, [_case("nse-b")], timing=FAST).values()
    assert result.verdict == batch.SKIP and "STOP" in result.reason
    assert rig.modify_calls == 3
    [order_id] = result.order_ids
    assert rig.statuses()[order_id] == "CANCELED"  # cancelled once all the same
    assert rig.count_for("CancelOrder", order_id) == 1


def test_modify_cases_skip_when_the_bfo_canary_is_not_confirmed(rig: Rig) -> None:
    rig.market.stuck.add("BSXFUT27NOV26")  # the BFO canary stays PENDING (F26)
    results = cases_modify.run_cases(rig.run, [_case("bfo-sensex-f")], timing=FAST)
    canary = family.results_of(rig.run)["shapes-bfo-sensex-f-nrml-lmt-day-buy-canary"]
    assert canary.verdict == batch.STUCK
    skipped = results[_case("bfo-sensex-f").id]
    assert skipped.verdict == batch.SKIP and "F26" in skipped.reason
    assert rig.modify_calls == 0


def test_after_ctrl_c_the_sequence_sends_nothing_more_not_even_its_cancel(rig: Rig) -> None:
    rig.script[8] = "interrupt"  # the session is interrupted during the last modify step
    [result] = cases_modify.run_cases(rig.run, [_case("nse-b")], timing=FAST).values()
    assert rig.modify_calls == 8
    assert rig.count("CancelOrder") == 0 and rig.market.cancels == []
    [order_id] = result.order_ids
    assert rig.statuses()[order_id] == "TRIGGER_PENDING"  # left open for the cleanup tool


# == Fix round 1 ===============================================================================


def test_modify_refusals_are_recorded_per_case_and_never_trip_the_breaker(rig: Rig) -> None:
    for call in (1, 2, 3):  # price-down, price-up, qty-up refused unexpectedly
        rig.script[call] = "reject:Rejected : ORA:something unexpected"
    [result] = cases_modify.run_cases(rig.run, [_case("nse-b")], timing=FAST).values()
    assert result.verdict == batch.FAIL
    assert rig.session.guard.tripped("nse") is None  # modify answers are not place outcomes
    refusals = result.record["notes"]["modify_refusals"]
    assert [r["step"] for r in refusals] == [
        "price-down",
        "price-up",
        "qty-up",
        "product",
        "modify-after-cancel",
    ]
    assert refusals[0]["result"] == "fail" and refusals[-1]["result"] == "pass"
    assert all(r["words"] for r in refusals)


def test_a_stop_order_at_the_upper_circuit_steps_its_price_down_instead(rig: Rig) -> None:
    inst = rig.market.instruments["ITC-EQ"]
    inst.upper = inst.last * 104 / 100  # the stop BUY's limit (4 % above) sits on the circuit
    rig.market.set_last("ITC-EQ", str(inst.last))
    [result] = cases_modify.run_cases(rig.run, [_case("nse-b", "SL-LMT")], timing=FAST).values()
    assert result.verdict == batch.PASS, (result.reason, _steps(result))
    steps = {s["step"]: s for s in _steps(result)}
    upper = inst.upper
    assert steps["price-up"]["changes"]["price"] == upper - inst.tick
    assert "upper circuit" in steps["price-up"]["note"]
    for name in ("price-up", "price-down", "price-trigger"):
        price = steps[name].get("changes", {}).get("price")
        assert price is None or price <= upper, (name, price)
    trigger = steps["price-trigger"]
    assert trigger["result"] in ("pass", "skipped"), trigger


# -- modify of a cancelled order: an Ok reply is only an acknowledgement; the history decides ---

AFTER_CANCEL_CALL = 9  # the 8 modifies of the LIMIT sequence, the cancel, then this modify


@pytest.fixture(params=["uat", "mock", "production"])
def after_cancel_rig(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Rig]:
    if request.param == "mock":
        with modify_views_rig(tmp_path, now=SAT_11) as built:
            make_mock(built.session, monkeypatch)
            yield built
    else:
        with modify_views_rig(tmp_path, uat=request.param == "uat") as built:
            yield built


def _after(rig: Rig, action: str) -> tuple[batch.CaseResult, dict[str, Any]]:
    rig.script[AFTER_CANCEL_CALL] = action
    [result] = cases_modify.run_cases(rig.run, [_case("nse-b")], timing=FAST).values()
    after = next(s for s in _steps(result) if s["step"] == "modify-after-cancel")
    assert after["verdict"] == calls.ACKNOWLEDGED  # the reply was Ok in every case
    return result, after


def test_an_acknowledged_modify_of_a_cancelled_order_refused_in_the_history_passes(
    after_cancel_rig: Rig,
) -> None:
    result, after = _after(after_cancel_rig, "ack-reject:ORA: order is cancelled")
    assert result.verdict == batch.PASS, (result.reason, after)
    assert after["result"] == "pass" and "order is cancelled" in after["history_words"]
    refusals = result.record["notes"]["modify_refusals"]
    assert any(r["step"] == "modify-after-cancel" for r in refusals)


def test_an_acknowledged_modify_of_a_cancelled_order_with_no_history_entry_is_ignored(
    after_cancel_rig: Rig,
) -> None:
    result, after = _after(after_cancel_rig, "lie")
    assert result.verdict == batch.PASS, (result.reason, after)
    assert after["result"] == "pass" and after["note"] == "modify_after_cancel_ignored"


def test_an_acknowledged_modify_that_changed_the_cancelled_order_fails(
    after_cancel_rig: Rig,
) -> None:
    result, after = _after(after_cancel_rig, "ack-apply")
    assert after["result"] == "fail"
    assert result.verdict == batch.FAIL
    assert "modify-after-cancel: the cancelled order changed" in result.reason


def test_an_unreadable_history_after_the_modify_is_recorded_never_a_pass(
    after_cancel_rig: Rig,
) -> None:
    result, after = _after(after_cancel_rig, "ack-unreadable")
    assert after["result"] == "record" and after["note"] == "modify_after_cancel_unverified"
    assert result.verdict == batch.PASS, (result.reason, after)  # recorded: not a failure
