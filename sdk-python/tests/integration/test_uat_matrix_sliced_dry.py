"""Dry run of the UAT matrix family M7 (sliced orders) on the offline broker.

The real family code (``cases_sliced``: specs, sizing from the freeze quantity, one
``calls.place_sliced`` per case, observation, one cancel per open slice, judging) runs end to end
on ``UatLikeMarket`` and the SDK's real ``place_sliced_order``. Every slice is placed once, in
order; a partly sent sliced order keeps its placed slices (cancelled once) and is never placed
again; an RMS ``RED:`` refusal is an environment limit for the rest of the family.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from tests.integration.uat_family_rig import Rig, family_rig
from tests.integration.uat_t7_rig import add_mcx, set_freeze
from tests.live_orders.uat_matrix import batch, cases, cases_sliced
from tests.live_orders.uat_matrix.cases import Case
from tradesmart.testing import drills

SLICED = cases_sliced.SLICED_CASES
FREEZE = {"nfo-nifty-f": 1800, "nfo-nifty-ce-atm": 1800, "bfo-sensex-ce-atm": 1000}


def _prepare(rig: Rig) -> None:
    add_mcx(rig)
    for role, frzqty in FREEZE.items():
        set_freeze(rig, role, frzqty)


def expected(case: Case) -> str:
    return batch.RECORD if case.outcome == cases.RECORD else batch.PASS


def _by_tag(rig: Rig) -> dict[str, list[dict[str, Any]]]:
    tags: dict[str, list[dict[str, Any]]] = {}
    for request in rig.requests("PlaceOrder"):
        tags.setdefault(str(request["remarks"]), []).append(request)
    return tags


def test_every_sliced_case_gets_its_verdict_and_each_slice_goes_out_once(tmp_path: Path) -> None:
    with family_rig(tmp_path) as rig:
        _prepare(rig)
        results = cases_sliced.sliced_results(rig.run, SLICED)
        wrong = {
            c.id: (results[c.id].verdict, results[c.id].reason)
            for c in SLICED
            if results[c.id].verdict != expected(c)
        }
        assert not wrong, wrong
        canary = next(c for c in cases.CASES if c.canary and c.segment == "bfo")
        bfo = rig.run.contracts.contracts[canary.role].tradingsymbol
        placed = rig.requests("PlaceOrder")
        assert placed[0]["tsym"] != bfo  # NSE first; the BFO canary only before the BFO case
        assert len(placed) == sum(c.orders for c in SLICED) + 1  # + the BFO canary
        by_tag = _by_tag(rig)
        for spec in cases_sliced.SPECS:
            sent = next(
                rows for rows in by_tag.values()
                if rows[0]["tsym"] == rig.run.contracts.contracts[spec.case.role].tradingsymbol
                and rows[0]["trantype"] == spec.case.side[0] and rows[0]["ret"] == spec.case.ret
                and ("dscqty" in rows[0]) == bool(spec.disclosed)
                and len(rows) == spec.case.orders
            )  # fmt: skip
            planned = results[spec.case.id].record["notes"]["slices_planned"]
            assert [int(r["qty"]) for r in sent] == planned, spec.case.id
            if spec.disclosed:
                assert all(r["dscqty"] == str(spec.disclosed) for r in sent)
        assert all(o.status in ("CANCELED", "REJECTED") for o in rig.broker.orders)
        cancels = Counter(rig.market.cancels)
        assert all(n == 1 for n in cancels.values())


def test_freeze_sizing_sends_three_slices_without_a_cap(tmp_path: Path) -> None:
    spec = next(s for s in cases_sliced.SPECS if s.sizing == cases_sliced.FREEZE
                and s.case.role == "nfo-nifty-f")  # fmt: skip
    with family_rig(tmp_path) as rig:
        _prepare(rig)
        result = cases_sliced.sliced_results(rig.run, [spec.case])[spec.case.id]
        assert result.verdict == batch.PASS, result.reason
        quantities = [int(r["qty"]) for r in rig.requests("PlaceOrder")]
        assert quantities == [1755, 1755, 65]  # 27 lots of 65 under a freeze of 1800
        intent = next(
            line for line in rig.ledger_path().read_text().splitlines()
            if '"place_sliced"' in line and '"intent"' in line
        )  # fmt: skip
        assert '"max_slice_quantity": null' in intent and '"max_slices": 3' in intent


def test_ioc_slices_are_cancelled_by_the_exchange_not_by_the_run(tmp_path: Path) -> None:
    case = next(c for c in SLICED if c.ret == "IOC")
    with family_rig(tmp_path) as rig:
        result = cases_sliced.sliced_results(rig.run, [case])[case.id]
        assert result.verdict == batch.PASS, result.reason
        assert len(result.order_ids) == case.orders
        assert rig.market.cancels == []


def test_without_a_freeze_quantity_the_freeze_case_skips_before_sending(tmp_path: Path) -> None:
    spec = next(s for s in cases_sliced.SPECS if s.sizing == cases_sliced.FREEZE
                and s.case.role == "nfo-nifty-ce-atm")  # fmt: skip
    with family_rig(tmp_path) as rig:
        result = cases_sliced.sliced_results(rig.run, [spec.case])[spec.case.id]
        assert result.verdict == batch.SKIP and "freeze" in result.reason
        assert rig.requests("PlaceOrder") == []


def test_an_rms_refusal_is_an_environment_limit_for_the_rest_of_the_family(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path) as rig:
        _prepare(rig)
        nifty = rig.run.contracts.contracts["nfo-nifty-f"].tradingsymbol
        rig.market.rms.add(nifty)
        results = cases_sliced.sliced_results(rig.run, SLICED)
        verdicts = [results[c.id].verdict for c in SLICED]
        first = next(i for i, c in enumerate(SLICED) if c.role == "nfo-nifty-f")
        assert verdicts[first] == batch.ENV_LIMIT
        assert all(v == batch.SKIP for v in verdicts[first + 1 :])
        assert all("environment limit" in results[c.id].reason for c in SLICED[first + 1 :])
        assert all(v in (batch.PASS, batch.RECORD) for v in verdicts[:first])


def test_a_partly_sent_sliced_order_cancels_its_slice_and_is_never_placed_again(
    tmp_path: Path,
) -> None:
    case = next(c for c in SLICED if c.id.endswith("-2x1lot") and c.role == "nfo-nifty-f")
    with family_rig(tmp_path) as rig:
        seen: list[int] = []

        def second_refused(kwargs: dict[str, Any]) -> None:
            seen.append(1)
            if len(seen) == 2:
                rig.broker.fail_next("PlaceOrder", drills.reject("Rejected : ORA:something"))

        rig.market.on_place = second_refused
        result = cases_sliced.sliced_results(rig.run, [case])[case.id]
        assert result.verdict == batch.FAIL and "something" in result.reason
        assert len(result.order_ids) == 1
        assert rig.market.cancels == result.order_ids  # the placed slice, cancelled once
        assert len(rig.requests("PlaceOrder")) == 2
        assert rig.session.case_done(case.id) is not None  # a resume never places it again
        again = cases_sliced.sliced_results(rig.run, [case])[case.id]
        assert again is result and len(rig.requests("PlaceOrder")) == 2


def test_the_stop_file_before_a_case_sends_nothing(tmp_path: Path) -> None:
    with family_rig(tmp_path) as rig:
        (rig.root / "STOP").write_text("stop")
        results = cases_sliced.sliced_results(rig.run, SLICED[:2])
        assert {r.verdict for r in results.values()} == {batch.SKIP}
        assert rig.requests("PlaceOrder") == []


def test_no_max_open_room_for_the_slices_skips_the_case_before_sending(tmp_path: Path) -> None:
    case = next(c for c in SLICED if c.orders == 3 and c.role == "nfo-nifty-f")
    with family_rig(tmp_path, max_open=2) as rig:
        result = cases_sliced.sliced_results(rig.run, [case])[case.id]
        assert result.verdict == batch.SKIP and result.reason.startswith("MAX_OPEN"), result.reason
        assert rig.requests("PlaceOrder") == []


def test_the_option_freeze_cases_rest_four_percent_below_the_last_price(tmp_path: Path) -> None:
    spec = next(s for s in cases_sliced.SPECS if s.sizing == cases_sliced.FREEZE
                and s.case.role == "nfo-nifty-ce-atm")  # fmt: skip
    with family_rig(tmp_path) as rig:
        _prepare(rig)
        result = cases_sliced.sliced_results(rig.run, [spec.case])[spec.case.id]
        assert result.verdict == batch.PASS, result.reason
        prices = {float(r["prc"]) for r in rig.requests("PlaceOrder")}
        assert prices == {144.0}  # last 150.00 less 4 % (option, LPP band)


def test_the_sdk_refusing_for_no_minute_room_is_not_sent_not_check_the_account(
    tmp_path: Path,
) -> None:
    # The SDK raises RateLimitError before the first slice when the minute has no room: nothing
    # went out, so the matrix files NOT_SENT ("the SDK refused it, nothing sent"), never
    # CHECK THE ACCOUNT.
    case = next(c for c in SLICED if c.orders == 3 and c.role == "nfo-nifty-f")
    with family_rig(tmp_path) as rig:
        rig.run.session.client._transport.order_room_wait = lambda n: 99.0
        result = cases_sliced.sliced_results(rig.run, [case])[case.id]
        assert result.verdict == batch.FAIL, (result.verdict, result.reason)
        assert "nothing sent" in result.reason and "CHECK THE ACCOUNT" not in result.reason
        assert rig.requests("PlaceOrder") == []
