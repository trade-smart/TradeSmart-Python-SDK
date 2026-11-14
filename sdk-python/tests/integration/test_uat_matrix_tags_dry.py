"""Dry run of the UAT matrix family M11 (tags and encoding) on the offline broker.

The real family code (``cases_tags``: the ``M&M-EQ`` cases and their round-trip hook, the run's
tag check; ``family.run_cases``; the batch engine) runs end to end: the far orders on the
``&`` symbol rest, their trading symbol and tag come back unchanged in the book and the
history, the echoed tag length is recorded; a tag cut short or dropped is recorded (the remarks
length is an open question), a changed symbol or an otherwise changed tag fails the case.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.integration.uat_family_rig import RUN_ID, Rig, family_rig
from tests.live_orders.uat_matrix import batch, cases_tags, family
from tests.live_orders.uat_matrix.batch import CaseResult
from tradesmart.models.orders import Order


def test_the_ampersand_symbol_and_the_tag_round_trip(tmp_path: Path) -> None:
    with family_rig(tmp_path) as rig:
        results = family.run_cases(rig.run, cases_tags.items(cases_tags.TAG_CASES))
        assert {c.id: results[c.id].verdict for c in cases_tags.TAG_CASES} == {
            c.id: batch.PASS for c in cases_tags.TAG_CASES
        }
        sent = rig.requests("PlaceOrder")
        assert {p["tsym"] for p in sent} == {"M&M-EQ"}
        for record in rig.records():
            trip = record["notes"]["round_trip"]
            assert trip["tsym_book"] == trip["tsym_history"] == "M&M-EQ"
            assert trip["remarks_book"] == trip["remarks_sent"]
            assert trip["remarks_echoed_length"] == trip["remarks_sent_length"] == 21
        assert cases_tags.tag_problems(rig.ledger_path(), RUN_ID) == []
        assert all(o.status == "CANCELED" for o in rig.broker.orders)


def _smoke_with_book(rig: Rig, change: Callable[[Order], dict[str, Any]]) -> CaseResult:
    """The smoke M&M case with the book rows changed by ``change`` (the batch row the hook
    checks); its judged result (``cases_tags.judged``)."""
    client = rig.session.client
    real = client.orders

    def mangled() -> list[Order]:
        return [o.model_copy(update=change(o)) for o in real()]

    client.orders = mangled  # type: ignore[method-assign]
    smoke = [c for c in cases_tags.TAG_CASES if c.tier == "smoke"]
    [result] = family.run_cases(rig.run, cases_tags.items(smoke)).values()
    assert all(o.status == "CANCELED" for o in rig.broker.orders)  # still cancelled
    return cases_tags.judged(result)


def test_a_tag_cut_short_in_the_book_is_recorded(tmp_path: Path) -> None:
    with family_rig(tmp_path) as rig:
        result = _smoke_with_book(rig, lambda o: {"tag": (o.tag or "")[:10]})
        assert result.verdict == batch.RECORD
        assert "truncated" in result.reason and "10 of 21" in result.reason
        trip = rig.records()[0]["notes"]["round_trip"]
        assert trip["remarks_echoed_length"] == 10 and trip["remarks_finding"] == "truncated"


def test_a_tag_dropped_from_the_book_is_recorded(tmp_path: Path) -> None:
    with family_rig(tmp_path) as rig:
        result = _smoke_with_book(rig, lambda o: {"tag": None})
        assert result.verdict == batch.RECORD and "dropped" in result.reason


def test_a_tag_changed_otherwise_fails_the_case(tmp_path: Path) -> None:
    with family_rig(tmp_path) as rig:
        result = _smoke_with_book(rig, lambda o: {"tag": "sdkuat-other-tag-0001"})
        assert result.verdict == batch.FAIL and "remarks" in result.reason


def test_a_changed_symbol_in_the_book_fails_the_case(tmp_path: Path) -> None:
    with family_rig(tmp_path) as rig:
        result = _smoke_with_book(rig, lambda o: {"tradingsymbol": "M"})
        assert result.verdict == batch.FAIL and "tsym" in result.reason


def test_a_changed_symbol_in_the_history_fails_the_case(tmp_path: Path) -> None:
    with family_rig(tmp_path) as rig:
        client = rig.session.client
        real = client.order_history

        def mangled(order_id: str) -> list[Any]:
            return [e.model_copy(update={"tradingsymbol": "M"}) for e in real(order_id)]

        client.order_history = mangled  # type: ignore[method-assign]
        smoke = [c for c in cases_tags.TAG_CASES if c.tier == "smoke"]
        [result] = family.run_cases(rig.run, cases_tags.items(smoke)).values()
        result = cases_tags.judged(result)
        assert result.verdict == batch.FAIL and "tsym" in result.reason


def test_a_passing_case_stays_a_pass_when_judged(tmp_path: Path) -> None:
    with family_rig(tmp_path) as rig:
        smoke = [c for c in cases_tags.TAG_CASES if c.tier == "smoke"]
        [result] = family.run_cases(rig.run, cases_tags.items(smoke)).values()
        assert cases_tags.judged(result) is result and result.verdict == batch.PASS


def test_the_tag_check_with_nothing_placed_finds_no_problem(tmp_path: Path) -> None:
    with family_rig(tmp_path) as rig:
        assert not rig.ledger_path().exists()  # the ledger is written on its first line only
        assert cases_tags.tag_problems(rig.ledger_path(), RUN_ID) == []
