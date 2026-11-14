"""Mocked unit tests pinning the GTT part of the UAT matrix safety core (Task 10, M13).

The never-resend GTT wrappers of ``uat_matrix/calls.py`` (``gtt_place``, ``gtt_modify``,
``gtt_cancel``), the ledger's GTT records (alert ids of this run, resume, adopting the order a
GTT of ours placed) and the teardown that cancels every pending GTT of ours once. Nothing here
touches the network: ``GttClient`` extends the safety tests' ``MatrixClient`` stand-in.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.live_orders.safety import CHECK_THE_ACCOUNT, OUTSIDE_HOURS_ENV, Refusal
from tests.live_orders.uat_matrix import calls, cases_gtt, gate
from tests.live_orders.uat_matrix.calls import CaseRef
from tests.live_orders.uat_matrix.guard import StopRun
from tests.live_orders.uat_matrix.ledger import (
    LEDGER_NAME,
    NOT_SENT,
    Ledger,
    check_open_orders,
    is_ours,
)
from tests.unit.test_uat_matrix_safety import (
    MatrixClient,
    _lines,
    _order,
    _rejected,
    _session,
    _unknown,
)
from tradesmart import NetworkError, RequestValidationError
from tradesmart.models.base import IST
from tradesmart.models.gtt import Gtt, TriggeredGtt
from tradesmart.models.orders import Order

GTT = CaseRef("gtt-nse-b-cnc-lmt-day-buy", "nse")
PLAN: dict[str, Any] = {
    "exchange": "NSE",
    "tradingsymbol": "ITC-EQ",
    "alert_type": "LTP_A_O",
    "trigger_value": Decimal("1200.00"),
    "quantity": 1,
    "transaction_type": "BUY",
    "product": "CNC",
    "order_type": "LIMIT",
    "price": Decimal("360.00"),
    "validity": "DAY",
    "alert_validity": "DAY",
}


def _gtt(alert_id: str, tag: str, trigger: str = "1200.00", **extra: Any) -> Gtt:
    return Gtt.model_validate(
        {
            "al_id": alert_id,
            "remarks": tag,
            "d": trigger,
            "ai_t": "LTP_A_O",
            "validity": "DAY",
            "tsym": "ITC-EQ",
            "exch": "NSE",
            "trantype": "B",
            "prctyp": "LMT",
            "prd": "C",
            "ret": "DAY",
            "qty": "1",
            "prc": "360.00",
            **extra,
        }
    )


def _gtt_order(order_id: str, status: str, **extra: str) -> Order:
    """The order book row of the order our GTT 9001 (``PLAN``) placed: its instrument and side,
    no tag, ``add_ord_id`` = the alert id (each overridable)."""
    fields = {"tag": "", "add_ord_id": "9001", "tsym": "ITC-EQ", "trantype": "B", **extra}
    tag = fields.pop("tag")
    return _order(order_id, status, tag=tag, **fields)


class Timeout(BaseException):
    """Like pytest-timeout's ``Failed`` (an ``OutcomeException``: a ``BaseException``)."""


class GttClient(MatrixClient):
    """``MatrixClient`` plus the GTT methods: each ``pending_gtts()`` / ``triggered_gtts()``
    pops the next list (the last repeats); a list may be a callable of the client, or an
    exception to raise."""

    def __init__(
        self,
        *args: Any,
        gtt_place: list[Any] | None = None,
        gtt_modify: Any = None,
        gtt_cancel: Any = None,
        pending: list[Any] | None = None,
        triggered: list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.gtt_place_results = gtt_place if gtt_place is not None else ["9001"]
        self.gtt_modify_result = gtt_modify
        self.gtt_cancel_result = gtt_cancel
        self.pending = pending if pending is not None else [[]]
        self.triggered = triggered if triggered is not None else [[]]

    @staticmethod
    def _next(source: list[Any], client: GttClient) -> Any:
        value = source.pop(0) if len(source) > 1 else source[0]
        if callable(value):
            value = value(client)
        if isinstance(value, BaseException):
            raise value
        return value

    def place_gtt(self, **kwargs: Any) -> str:
        self._record("place_gtt", kwargs)
        results = self.gtt_place_results
        result = results.pop(0) if len(results) > 1 else results[0]
        if isinstance(result, BaseException):
            raise result
        return str(result)

    def modify_gtt(self, alert_id: str, **kwargs: Any) -> str:
        self._record("modify_gtt", (alert_id, kwargs))
        if isinstance(self.gtt_modify_result, BaseException):
            raise self.gtt_modify_result
        return alert_id if self.gtt_modify_result is None else str(self.gtt_modify_result)

    def cancel_gtt(self, alert_id: str) -> str:
        self._record("cancel_gtt", alert_id)
        if isinstance(self.gtt_cancel_result, BaseException):
            raise self.gtt_cancel_result
        return alert_id

    def pending_gtts(self) -> list[Gtt]:
        self._record("pending_gtts", None)
        result: list[Gtt] = self._next(self.pending, self)
        return result

    def triggered_gtts(self) -> list[TriggeredGtt]:
        self._record("triggered_gtts", None)
        result: list[TriggeredGtt] = self._next(self.triggered, self)
        return result

    def gtt_tags(self) -> list[str]:
        return [args["tag"] for name, args in self.calls if name == "place_gtt"]


def _gtt_session(client: GttClient, tmp_path: Path) -> gate.MatrixSession:
    session = _session(client, tmp_path)
    session.guard.reserve(GTT.case_id, 1)
    return session


def _placed_gtt(session: gate.MatrixSession, client: GttClient, alert_id: str = "9001") -> str:
    client.gtt_place_results = [alert_id]
    outcome = calls.gtt_place(session, PLAN, GTT)
    assert outcome.alert_id == alert_id
    return outcome.tag


def _modify_fields(trigger: str = "1240.00") -> dict[str, Any]:
    fields = {k: v for k, v in PLAN.items()}
    fields["trigger_value"] = Decimal(trigger)
    return fields


# -- gtt_place ---------------------------------------------------------------------------------


def test_gtt_place_writes_the_intent_before_and_the_alert_id_after(tmp_path: Path) -> None:
    seen: list[str] = []

    def on_call(name: str, args: Any) -> None:
        if name == "place_gtt":
            text = session.ledger.path.read_text(encoding="utf-8")
            seen.append(json.loads(text.splitlines()[-1])["type"])

    client = GttClient(on_call=on_call)
    session = _gtt_session(client, tmp_path)
    outcome = calls.gtt_place(session, PLAN, GTT)
    assert seen == ["intent"]
    intent, last = _lines(session)[-2], _lines(session)[-1]
    assert intent["kind"] == "gtt_place" and intent["tag"] == outcome.tag == client.gtt_tags()[0]
    assert intent["segment"] == "nse" and intent["case_id"] == GTT.case_id
    assert last["type"] == "outcome" and last["call"] == intent["call"]
    assert last["alert_id"] == "9001" == outcome.alert_id and last["verdict"] == calls.ACKNOWLEDGED
    assert outcome.tag.startswith(f"sdkuat-{session.run_id}-")
    assert session.ledger.alerts() == {"9001"} and session.ledger.alert_tag("9001") == outcome.tag
    assert client.count("place_gtt") == 1


def test_gtt_place_refuses_a_plan_with_a_tag(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    with pytest.raises(ValueError, match="tag"):
        calls.gtt_place(session, {**PLAN, "tag": "mine"}, GTT)
    assert client.count("place_gtt") == 0


def test_gtt_place_spends_the_declared_count(tmp_path: Path) -> None:
    client = GttClient(gtt_place=["9001", "9002"])
    session = _gtt_session(client, tmp_path)
    calls.gtt_place(session, PLAN, GTT)
    with pytest.raises(Refusal, match="declared order count"):
        calls.gtt_place(session, PLAN, GTT)
    undeclared = CaseRef("gtt-undeclared", "nse")
    with pytest.raises(Refusal, match="no declared order count"):
        calls.gtt_place(session, PLAN, undeclared)
    assert client.count("place_gtt") == 1


def test_gtt_place_is_refused_when_stopped_or_tripped(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    session.guard.trip("nse", "test")
    with pytest.raises(Refusal, match="circuit breaker"):
        calls.gtt_place(session, PLAN, GTT)
    session.guard.stop("by hand")
    with pytest.raises(StopRun):
        calls.gtt_place(session, PLAN, GTT)
    assert client.count("place_gtt") == 0


def test_gtt_place_rejected_is_definitive_and_nothing_is_ours(tmp_path: Path) -> None:
    client = GttClient(gtt_place=[_rejected("GTT not allowed")])
    session = _gtt_session(client, tmp_path)
    outcome = calls.gtt_place(session, PLAN, GTT)
    assert outcome.verdict == calls.REJECTED and outcome.alert_id is None
    assert session.ledger.alerts() == set() and client.count("pending_gtts") == 0


def test_gtt_place_refused_by_the_sdk_is_not_sent(tmp_path: Path) -> None:
    client = GttClient(gtt_place=[RequestValidationError("bad trigger")])
    session = _gtt_session(client, tmp_path)
    outcome = calls.gtt_place(session, PLAN, GTT)
    assert outcome.verdict == NOT_SENT and not outcome.sent
    reopened = Ledger(session.ledger.path, session.run_id)
    assert reopened.unfinished(GTT.case_id) is False  # nothing went out: a resume may place it


def test_an_ambiguous_gtt_place_is_found_by_tag_and_never_resent(tmp_path: Path) -> None:
    client = GttClient(
        gtt_place=[_unknown("PlaceGTTOrder")],
        pending=[[], lambda c: [_gtt("9007", c.gtt_tags()[0]), _gtt("777", "by-hand")]],
    )
    session = _gtt_session(client, tmp_path)
    outcome = calls.gtt_place(session, PLAN, GTT)
    assert outcome.verdict == calls.FOUND and outcome.alert_id == "9007"
    assert session.ledger.alerts() == {"9007"}  # never the foreign GTT
    assert client.count("place_gtt") == 1 and client.count("pending_gtts") == 2


def test_an_ambiguous_gtt_place_that_fired_is_found_in_the_triggered_list(tmp_path: Path) -> None:
    client = GttClient(
        gtt_place=[_unknown("PlaceGTTOrder")],
        triggered=[lambda c: [TriggeredGtt.model_validate({"al_id": "9008",
                                                            "remarks": c.gtt_tags()[0]})]],
    )  # fmt: skip
    session = _gtt_session(client, tmp_path)
    outcome = calls.gtt_place(session, PLAN, GTT)
    assert outcome.verdict == calls.FOUND and session.ledger.alerts() == {"9008"}
    assert client.count("place_gtt") == 1


@pytest.mark.parametrize("unreadable", [False, True])
def test_an_ambiguous_gtt_place_not_found_is_check_the_account_and_trips(
    tmp_path: Path, unreadable: bool
) -> None:
    failing: Any = NetworkError("down") if unreadable else []
    client = GttClient(
        gtt_place=[_unknown("PlaceGTTOrder")], pending=[failing], triggered=[failing]
    )
    session = _gtt_session(client, tmp_path)
    outcome = calls.gtt_place(session, PLAN, GTT)
    assert outcome.verdict == CHECK_THE_ACCOUNT and outcome.check_the_account
    assert session.guard.tripped("nse") is not None
    assert session.ledger.tag_flagged(outcome.tag)
    assert client.count("place_gtt") == 1
    assert client.count("pending_gtts") == session.lookup_attempts


def test_an_interrupted_gtt_place_is_recorded_flagged_and_reraised(tmp_path: Path) -> None:
    client = GttClient(gtt_place=[KeyboardInterrupt()])
    session = _gtt_session(client, tmp_path)
    with pytest.raises(KeyboardInterrupt):
        calls.gtt_place(session, PLAN, GTT)
    last = _lines(session)[-1]
    assert last["type"] == "outcome" and last["verdict"] == "interrupted"
    assert session.interrupted and session.ledger.tag_flagged(last["tag"])
    assert client.count("place_gtt") == 1


# -- the ledger: resume, adoption ---------------------------------------------------------------


def test_a_gtt_place_intent_without_its_outcome_blocks_the_case_on_resume(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_NAME
    ledger = Ledger(path, "1113abcd")
    tag = ledger.new_tag(GTT.case_id)
    ledger.intent(tag, GTT.case_id, "gtt_place", segment="nse")
    reopened = Ledger(path, "1113abcd")
    assert reopened.tag_flagged(tag) and reopened.unfinished(GTT.case_id)
    assert reopened.segment_of(tag) == "nse" and reopened.case_of(tag) == GTT.case_id
    assert reopened.sent(tag)


def test_a_gtt_place_with_its_outcome_is_still_unfinished_without_a_verdict(
    tmp_path: Path,
) -> None:
    path = tmp_path / LEDGER_NAME
    ledger = Ledger(path, "1113abcd")
    tag = ledger.new_tag(GTT.case_id)
    call = ledger.intent(tag, GTT.case_id, "gtt_place", segment="nse")
    ledger.outcome(tag, None, calls.ACKNOWLEDGED, kind="gtt_place", call=call, alert_id="9001")
    reopened = Ledger(path, "1113abcd")
    assert reopened.alerts() == {"9001"} and not reopened.tag_flagged(tag)
    assert reopened.unfinished(GTT.case_id)  # an earlier process placed and wrote no verdict
    reopened.verdict(GTT.case_id, "pass")
    assert Ledger(path, "1113abcd").unfinished(GTT.case_id) is False


def test_adopt_takes_an_order_placed_by_one_of_our_gtts(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)
    row = _gtt_order("5001", "OPEN")
    assert is_ours(row, session.ledger)
    assert session.ledger.adopt(row) is True
    assert session.ledger.ours() == {"5001"} and session.ledger.tag_of("5001") == tag
    adopt = _lines(session)[-1]
    assert adopt["type"] == "adopt" and adopt["alert_id"] == "9001" and adopt["order_id"] == "5001"
    reopened = Ledger(session.ledger.path, session.run_id)
    assert reopened.tag_of("5001") == tag and reopened.segment_of(tag) == "nse"


def test_a_foreign_order_is_never_adopted_and_still_stops_the_run(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)
    lines = len(_lines(session))
    foreign = [
        _order("5002", "OPEN", tag="by-hand", add_ord_id="1234"),  # another GTT's order
        _order("5003", "OPEN", tag=tag),  # carries our GTT tag but no link to our alert
        _order("5004", "OPEN", tag="", add_ord_id=""),
    ]
    for row in foreign:
        assert not is_ours(row, session.ledger)
        assert session.ledger.adopt(row) is False
    assert session.ledger.ours() == set() and len(_lines(session)) == lines
    client.books = [foreign]
    with pytest.raises(StopRun, match="5002"):
        check_open_orders(client, session.ledger, frozenset(), sleep=session.sleep)  # type: ignore[arg-type]


def test_an_open_order_of_our_gtt_passes_the_open_order_check(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    _placed_gtt(session, client)
    client.books = [[_gtt_order("5001", "OPEN")]]
    check_open_orders(client, session.ledger, frozenset(), sleep=session.sleep)  # type: ignore[arg-type]


def test_an_adopted_gtt_order_is_cancelled_once_through_calls_cancel(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    _placed_gtt(session, client)
    assert session.ledger.adopt(_gtt_order("5001", "OPEN"))
    outcome = calls.cancel(session, "5001", GTT)
    assert outcome.verdict == calls.ACKNOWLEDGED
    with pytest.raises(Refusal, match="already cancelled once"):
        calls.cancel(session, "5001", GTT)
    assert client.count("cancel_order") == 1


# -- gtt_modify -------------------------------------------------------------------------------


def test_gtt_modify_sends_every_field_with_the_ledgers_tag_once(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)
    before = _gtt("9001", tag)
    outcome = calls.gtt_modify(session, "9001", GTT, before=before, **_modify_fields())
    assert outcome.verdict == calls.ACKNOWLEDGED and outcome.kind == "gtt_modify"
    [(alert_id, sent)] = [args for name, args in client.calls if name == "modify_gtt"]
    assert alert_id == "9001" and sent["tag"] == tag
    assert sent["validity"] == "DAY" and sent["alert_validity"] == "DAY"
    intent = _lines(session)[-2]
    assert intent["kind"] == "gtt_modify" and intent["alert_id"] == "9001"


def test_gtt_modify_refuses_an_alert_not_ours_or_a_caller_tag(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    _placed_gtt(session, client)
    with pytest.raises(Refusal, match="not a GTT of this run"):
        calls.gtt_modify(session, "777", GTT, **_modify_fields())
    with pytest.raises(ValueError, match="tag"):
        calls.gtt_modify(session, "9001", GTT, tag="mine", **_modify_fields())
    assert client.count("modify_gtt") == 0


def test_an_ambiguous_gtt_modify_is_resolved_by_the_pending_row(tmp_path: Path) -> None:
    client = GttClient(gtt_modify=_unknown("ModifyGTTOrder"))
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)
    client.pending = [[_gtt("9001", tag, "1200.00")], [_gtt("9001", tag, "1240.00")]]
    outcome = calls.gtt_modify(
        session, "9001", GTT, before=_gtt("9001", tag), **_modify_fields("1240.00")
    )
    assert outcome.verdict == calls.RESOLVED and client.count("modify_gtt") == 1


def test_an_ambiguous_gtt_modify_not_seen_is_check_the_account_and_blocks_the_alert(
    tmp_path: Path,
) -> None:
    client = GttClient(gtt_modify=_unknown("ModifyGTTOrder"))
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)
    client.pending = [[_gtt("9001", tag, "1200.00")]]
    outcome = calls.gtt_modify(
        session, "9001", GTT, before=_gtt("9001", tag), **_modify_fields("1240.00")
    )
    assert outcome.verdict == CHECK_THE_ACCOUNT and session.guard.tripped("nse")
    assert session.ledger.alert_flagged("9001")
    with pytest.raises(Refusal, match=CHECK_THE_ACCOUNT):
        calls.gtt_cancel(session, "9001", GTT)
    assert client.count("modify_gtt") == 1 and client.count("cancel_gtt") == 0


# -- gtt_cancel -------------------------------------------------------------------------------


def test_gtt_cancel_is_sent_once_per_alert(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    _placed_gtt(session, client)
    session.guard.stop("by hand")  # a cancel still goes out
    outcome = calls.gtt_cancel(session, "9001", GTT)
    assert outcome.verdict == calls.ACKNOWLEDGED and outcome.kind == "gtt_cancel"
    with pytest.raises(Refusal, match="once"):
        calls.gtt_cancel(session, "9001", GTT)
    with pytest.raises(Refusal, match="not a GTT of this run"):
        calls.gtt_cancel(session, "777", GTT)
    assert client.count("cancel_gtt") == 1
    assert Ledger(session.ledger.path, session.run_id).gtt_cancel_sent("9001")


def test_a_gtt_cancel_never_sent_keeps_its_one_cancel(tmp_path: Path) -> None:
    client = GttClient(gtt_cancel=RequestValidationError("bad id"))
    session = _gtt_session(client, tmp_path)
    _placed_gtt(session, client)
    assert calls.gtt_cancel(session, "9001", GTT).verdict == NOT_SENT
    assert session.ledger.gtt_cancel_sent("9001") is False
    client.gtt_cancel_result = None
    assert calls.gtt_cancel(session, "9001", GTT).verdict == calls.ACKNOWLEDGED


@pytest.mark.parametrize(
    ("listed", "verdict"), [(False, calls.RESOLVED), (True, CHECK_THE_ACCOUNT)]
)
def test_an_ambiguous_gtt_cancel_is_resolved_only_when_the_gtt_is_gone(
    tmp_path: Path, listed: bool, verdict: str
) -> None:
    client = GttClient(gtt_cancel=_unknown("CancelGTTOrder"))
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)
    client.pending = [[_gtt("9001", tag)] if listed else []]
    outcome = calls.gtt_cancel(session, "9001", GTT)
    assert outcome.verdict == verdict and client.count("cancel_gtt") == 1
    assert (session.guard.tripped("nse") is not None) is listed


# -- teardown ---------------------------------------------------------------------------------


def test_a_run_without_gtts_reads_no_gtt_list_at_teardown(tmp_path: Path) -> None:
    client = GttClient()
    session = _session(client, tmp_path)
    report = calls.teardown(session)
    assert report is not None and report.gtts is None
    assert client.count("pending_gtts") == 0


def test_the_teardown_cancels_every_pending_gtt_of_ours_once(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)
    # 9002: an unacknowledged GTT of our tag (found by the lookup); 777: someone else's.
    client.pending = [
        [_gtt("9001", tag), _gtt("9002", tag), _gtt("777", "by-hand")],
        [_gtt("777", "by-hand")],
    ]
    report = calls.teardown(session)
    assert report is not None and report.gtts is not None
    assert sorted(args for name, args in client.calls if name == "cancel_gtt") == ["9001", "9002"]
    assert sorted(report.gtts.cancelled) == ["9001", "9002"] and report.gtts.left == []
    note = _lines(session)[-1]
    assert note["type"] == "teardown" and note["gtts_left"] == []


def test_a_gtt_still_pending_after_its_cancel_is_listed(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)
    client.pending = [[_gtt("9001", tag)]]
    report = calls.teardown(session)
    assert report is not None and report.gtts is not None and report.gtts.left == ["9001"]
    assert client.count("cancel_gtt") == 1
    assert _lines(session)[-1]["gtts_left"] == ["9001"]
    assert any(x["type"] == "gtt_left" and CHECK_THE_ACCOUNT in x["alert"] for x in _lines(session))


def test_the_teardown_skips_a_gtt_already_cancelled_or_unknown(tmp_path: Path) -> None:
    client = GttClient(gtt_place=["9001", "9002"])
    session = _session(client, tmp_path)
    session.guard.reserve(GTT.case_id, 2)
    a = calls.gtt_place(session, PLAN, GTT)
    calls.gtt_cancel(session, "9001", GTT)
    client.gtt_modify_result = _unknown("ModifyGTTOrder")
    client.pending = [[_gtt("9002", a.tag, "1200.00")]]  # the modify is never seen applied
    calls.gtt_place(session, PLAN, GTT)
    tag_b = session.ledger.alert_tag("9002")
    assert tag_b is not None
    calls.gtt_modify(session, "9002", GTT, before=_gtt("9002", tag_b), **_modify_fields())
    cancels = client.count("cancel_gtt")
    client.pending = [[_gtt("9001", a.tag), _gtt("9002", tag_b)]]
    report = calls.teardown(session)
    assert report is not None and report.gtts is not None
    assert client.count("cancel_gtt") == cancels  # 9001 cancelled once already; 9002 unknown
    assert report.gtts.flagged == ["9002"] and sorted(report.gtts.left) == ["9001", "9002"]


def test_an_unreadable_gtt_list_at_teardown_is_listed(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    _placed_gtt(session, client)
    client.pending = [NetworkError("down")]
    report = calls.teardown(session)
    assert report is not None and report.gtts is not None and report.gtts.unreadable
    assert report.gtts.left == ["9001"] and client.count("cancel_gtt") == 0


def test_the_teardown_cancels_an_open_order_of_our_gtt(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    _placed_gtt(session, client)
    client.books = [
        [_gtt_order("5001", "OPEN"), _order("5002", "OPEN", add_ord_id="1234")],
        [_gtt_order("5001", "CANCELED"), _order("5002", "OPEN", add_ord_id="1234")],
    ]
    report = calls.teardown(session)
    assert report is not None and report.cancelled == ["5001"] and report.untouched == ["5002"]
    assert [args for name, args in client.calls if name == "cancel_order"] == ["5001"]


def test_the_teardown_after_ctrl_c_sends_nothing_and_lists_our_gtts(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    _placed_gtt(session, client)
    session.interrupted = True
    assert calls.teardown(session) is None
    assert client.count("cancel_gtt") == 0 and client.count("pending_gtts") == 0
    note = _lines(session)[-1]
    assert note["type"] == "teardown" and note["alerts"] == ["9001"]


# -- fix round 1 ------------------------------------------------------------------------------


@pytest.mark.parametrize("interruption", [Timeout(), KeyboardInterrupt()])
def test_an_interruption_during_the_ambiguous_place_lookup_still_writes_the_outcome(
    tmp_path: Path, interruption: BaseException
) -> None:
    client = GttClient(gtt_place=[_unknown("PlaceGTTOrder")], pending=[interruption])
    session = _gtt_session(client, tmp_path)
    with pytest.raises(type(interruption)):
        calls.gtt_place(session, PLAN, GTT)
    tag = client.gtt_tags()[0]
    last = _lines(session)[-1]
    assert last["type"] == "outcome" and last["kind"] == "gtt_place"
    assert last["verdict"] == "interrupted" and last["tag"] == tag
    assert session.ledger.tag_flagged(tag) and not session.ledger.pending_tag(tag)
    assert session.interrupted is isinstance(interruption, KeyboardInterrupt)
    if not session.interrupted:  # the teardown never cancels a GTT whose place is unknown
        client.pending = [[_gtt("9001", tag)]]
        report = calls.teardown(session)
        assert report is not None and report.gtts is not None
        assert client.count("cancel_gtt") == 0 and report.gtts.flagged == ["9001"]
        assert report.gtts.left == ["9001"]


def test_an_interruption_during_the_ambiguous_modify_lookup_still_writes_the_outcome(
    tmp_path: Path,
) -> None:
    client = GttClient(gtt_modify=_unknown("ModifyGTTOrder"))
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)
    client.pending = [Timeout()]
    with pytest.raises(Timeout):
        calls.gtt_modify(session, "9001", GTT, before=_gtt("9001", tag), **_modify_fields())
    last = _lines(session)[-1]
    assert last["type"] == "outcome" and last["kind"] == "gtt_modify"
    assert last["verdict"] == "interrupted" and session.ledger.alert_flagged("9001")
    with pytest.raises(Refusal, match=CHECK_THE_ACCOUNT):
        calls.gtt_cancel(session, "9001", GTT)
    assert client.count("cancel_gtt") == 0


def test_an_interruption_during_the_ambiguous_cancel_lookup_still_writes_the_outcome(
    tmp_path: Path,
) -> None:
    client = GttClient(gtt_cancel=_unknown("CancelGTTOrder"))
    session = _gtt_session(client, tmp_path)
    _placed_gtt(session, client)
    client.pending = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        calls.gtt_cancel(session, "9001", GTT)
    last = _lines(session)[-1]
    assert last["type"] == "outcome" and last["kind"] == "gtt_cancel"
    assert last["verdict"] == "interrupted" and last["alert_id"] == "9001"
    assert session.ledger.alert_flagged("9001") and session.interrupted
    assert Ledger(session.ledger.path, session.run_id).alert_flagged("9001")


@pytest.mark.parametrize("kind", ["place", "cancel", "gtt_place", "gtt_cancel"])
def test_pending_tag_and_pending_order_see_an_intent_without_its_outcome(
    tmp_path: Path, kind: str
) -> None:
    ledger = Ledger(tmp_path / LEDGER_NAME, "1113abcd")
    tag = ledger.new_tag(GTT.case_id)
    order_id = "1001" if kind == "cancel" else None
    call = ledger.intent(tag, GTT.case_id, kind, segment="nse", order_id=order_id)
    assert ledger.pending_tag(tag) and not ledger.pending_tag("other")
    assert ledger.pending_order("1001") is (kind == "cancel")
    ledger.outcome(tag, order_id, calls.ACKNOWLEDGED, kind=kind, call=call)
    assert not ledger.pending_tag(tag) and not ledger.pending_order("1001")


def test_a_gtt_with_a_call_in_flight_is_refused_everywhere(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)
    session.ledger.intent(tag, GTT.case_id, "gtt_modify", segment="nse", alert_id="9001")
    with pytest.raises(Refusal, match="in flight"):
        calls.gtt_modify(session, "9001", GTT, **_modify_fields())
    with pytest.raises(Refusal, match="in flight"):
        calls.gtt_cancel(session, "9001", GTT)
    assert session.ledger.adopt_gtt("9002", tag) is False
    client.pending = [[_gtt("9001", tag), _gtt("9002", tag)]]
    report = calls.teardown(session)
    assert report is not None and report.gtts is not None
    assert sorted(report.gtts.flagged) == ["9001", "9002"]
    assert client.count("cancel_gtt") == 0 and client.count("modify_gtt") == 0
    # both are still pending and never confirmed gone: listed for CHECK THE ACCOUNT
    assert report.gtts.left == ["9001", "9002"]
    left = [x["alert_id"] for x in _lines(session) if x["type"] == "gtt_left"]
    assert sorted(left) == ["9001", "9002"]


@pytest.mark.parametrize(
    "row",
    [
        # an iceberg leg (its AQU id in add_ord_id) or a by-hand order colliding with our id
        _order("7777", "OPEN", tag="by-hand-iceberg", add_ord_id="9001", tsym="RELIANCE-EQ",
               trantype="B", qty="50"),
        _gtt_order("7778", "OPEN", tag="by-hand"),  # our instrument and side, another tag
        _gtt_order("7779", "OPEN", tsym="RELIANCE-EQ"),  # blank tag, another symbol
        _gtt_order("7780", "OPEN", trantype="S"),  # blank tag, the other side
        _gtt_order("7781", "OPEN", exch="BSE"),  # blank tag, another exchange
    ],
    ids=["iceberg-leg", "other-tag", "other-symbol", "other-side", "other-exchange"],
)  # fmt: skip
def test_an_order_with_our_alert_id_in_add_ord_id_but_not_our_gtts_shape_is_never_ours(
    tmp_path: Path, row: Order
) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    _placed_gtt(session, client)  # our alert id 9001
    assert not is_ours(row, session.ledger)
    assert session.ledger.adopt(row) is False and session.ledger.ours() == set()
    client.books = [[row]]
    report = calls.cancel_ours(session)
    assert client.count("cancel_order") == 0 and report.untouched == [row.order_id]


def test_the_order_of_our_gtt_may_carry_the_gtts_tag(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)
    row = _gtt_order("5001", "OPEN", tag=tag)
    assert is_ours(row, session.ledger) and session.ledger.adopt(row)
    reopened = Ledger(session.ledger.path, session.run_id)
    assert reopened.tag_of("5001") == tag
    other = _gtt_order("5002", "OPEN", tsym="RELIANCE-EQ")
    assert not is_ours(other, reopened) and reopened.adopt(other) is False


def _uat_remarks(token: str) -> str:
    """The remarks UAT writes on the order a GTT placed (live, 13 Nov 2026):
    ``<ai_t>: <tag>: <text>: <tag>``."""
    return f"LTP_B_O: {token}: Ltp 284.25 is below 298.55: {token}"


def test_the_order_of_our_gtt_with_uat_remarks_carrying_our_tag_is_ours(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)
    row = _gtt_order("5001", "OPEN", tag=_uat_remarks(tag))
    assert session.ledger.gtt_order_tag(row) == tag
    assert is_ours(row, session.ledger) and session.ledger.adopt(row)
    assert Ledger(session.ledger.path, session.run_id).tag_of("5001") == tag


@pytest.mark.parametrize(
    "remarks",
    [
        lambda tag: _uat_remarks(f"{tag}0"),  # e.g. ...-005-10 is not ...-005-1
        lambda tag: _uat_remarks(tag[:-1]),  # a shorter tag that is a prefix of ours
        lambda tag: _uat_remarks("sdkuat-00000000-999-1"),  # another tag token
        lambda tag: f"LTP_B_O: x{tag}: text: {tag}y",  # our tag only as a substring
        lambda tag: f"by hand {tag}",  # our tag inside a token, not a token
    ],
    ids=["longer-tag", "shorter-tag", "other-tag", "substring", "inside-a-token"],
)
def test_uat_remarks_without_our_tag_as_a_token_are_not_ours(
    tmp_path: Path, remarks: Callable[[str], str]
) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)
    row = _gtt_order("5001", "OPEN", tag=remarks(tag))
    assert session.ledger.gtt_order_tag(row) is None
    assert not is_ours(row, session.ledger) and session.ledger.adopt(row) is False


def test_uat_remarks_with_our_tag_but_a_foreign_parent_id_are_not_ours(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)  # our alert id 9001
    row = _gtt_order("5001", "OPEN", tag=_uat_remarks(tag), add_ord_id="9002")
    assert session.ledger.gtt_order_tag(row) is None
    assert not is_ours(row, session.ledger) and session.ledger.adopt(row) is False
    blank = _gtt_order("5002", "OPEN", tag=_uat_remarks(tag), add_ord_id="")
    assert session.ledger.gtt_order_tag(blank) is None and not is_ours(blank, session.ledger)


def test_uat_remarks_with_our_tag_but_not_our_gtts_shape_are_not_ours(tmp_path: Path) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)
    for row in (
        _gtt_order("5001", "OPEN", tag=_uat_remarks(tag), tsym="RELIANCE-EQ"),
        _gtt_order("5002", "OPEN", tag=_uat_remarks(tag), trantype="S"),
        _gtt_order("5003", "OPEN", tag=_uat_remarks(tag), exch="BSE"),
    ):
        assert session.ledger.gtt_order_tag(row) is None
        assert not is_ours(row, session.ledger) and session.ledger.adopt(row) is False


@pytest.mark.parametrize("value", ["1", "0", "", "yes"])
def test_gtt_writes_are_refused_whenever_the_outside_hours_opt_in_is_set(value: str) -> None:
    morning = datetime(2026, 11, 13, 10, 0, tzinfo=IST)  # inside market hours
    reason = cases_gtt.write_refusal({OUTSIDE_HOURS_ENV: value}, morning)
    assert reason is not None and OUTSIDE_HOURS_ENV in reason


def test_gtt_writes_run_inside_market_hours_only() -> None:
    assert cases_gtt.write_refusal({}, datetime(2026, 11, 13, 10, 0, tzinfo=IST)) is None
    evening = cases_gtt.write_refusal({}, datetime(2026, 11, 13, 18, 0, tzinfo=IST))
    sunday = cases_gtt.write_refusal({}, datetime(2026, 11, 15, 10, 0, tzinfo=IST))
    assert evening is not None and sunday is not None


def test_the_gtt_module_skips_its_writes_through_write_refusal() -> None:
    source = (Path(__file__).parents[1] / "live_orders" / "uat_matrix" / "test_gtt.py").read_text(
        encoding="utf-8"
    )
    assert "cases_gtt.write_refusal(os.environ, uat=matrix.uat, mock=matrix.mock)" in source
    assert "family.hours_refusal(" not in source  # the GTT rule is stricter outside UAT


# -- fix round 2 ------------------------------------------------------------------------------


def test_ctrl_c_during_the_record_read_after_an_interrupted_place_still_writes_one_outcome(
    tmp_path: Path,
) -> None:
    client = GttClient(gtt_place=[Timeout()], pending=[KeyboardInterrupt()])
    session = _gtt_session(client, tmp_path)
    with pytest.raises(KeyboardInterrupt):
        calls.gtt_place(session, PLAN, GTT)
    tag = client.gtt_tags()[0]
    outcomes = [x for x in _lines(session) if x["type"] == "outcome"]
    assert [(x["kind"], x["verdict"]) for x in outcomes] == [("gtt_place", "interrupted")]
    assert outcomes[0]["detail"]["error"]["exception"] == "Timeout"  # the place's own error
    assert outcomes[0]["detail"]["lookup_interrupted"] == "KeyboardInterrupt"
    assert session.ledger.tag_flagged(tag) and not session.ledger.pending_tag(tag)
    assert session.interrupted is True
    assert client.count("place_gtt") == 1 and client.count("pending_gtts") == 1


@pytest.mark.parametrize("wrapper", ["modify", "cancel"])
def test_ctrl_c_after_a_timeout_in_modify_or_cancel_writes_one_outcome(
    tmp_path: Path, wrapper: str
) -> None:
    client = GttClient(gtt_modify=Timeout(), gtt_cancel=Timeout())
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)
    with pytest.raises(Timeout):
        if wrapper == "modify":
            calls.gtt_modify(session, "9001", GTT, before=_gtt("9001", tag), **_modify_fields())
        else:
            calls.gtt_cancel(session, "9001", GTT)
    outcomes = [x for x in _lines(session) if x["type"] == "outcome"]
    assert outcomes[-1]["verdict"] == "interrupted" and len(outcomes) == 2  # place + this one
    assert client.count("pending_gtts") == 0  # no read after an interrupted modify / cancel
    assert session.ledger.alert_flagged("9001") and not session.ledger.pending_tag(tag)


@pytest.mark.parametrize("wrapper", ["place", "modify", "cancel"])
def test_a_bug_in_the_lookup_is_check_the_account_never_interrupted(
    tmp_path: Path, wrapper: str
) -> None:
    client = GttClient(
        gtt_place=["9001", _unknown("PlaceGTTOrder")],
        gtt_modify=_unknown("ModifyGTTOrder"),
        gtt_cancel=_unknown("CancelGTTOrder"),
    )
    session = _session(client, tmp_path)
    session.guard.reserve(GTT.case_id, 2)
    tag = calls.gtt_place(session, PLAN, GTT).tag

    def bug(_: float) -> None:  # the bug is in the lookup itself (between two reads)
        raise RuntimeError("bug in the lookup")

    session.sleep = bug
    session.lookup_attempts = 2
    client.pending = [[_gtt("9001", tag, "1200.00")]]  # listed, never applied: a second read
    client.triggered = [[]]
    if wrapper == "place":
        outcome = calls.gtt_place(session, PLAN, GTT)
    elif wrapper == "modify":
        outcome = calls.gtt_modify(
            session, "9001", GTT, before=_gtt("9001", tag), **_modify_fields()
        )
    else:
        outcome = calls.gtt_cancel(session, "9001", GTT)
    assert outcome.verdict == CHECK_THE_ACCOUNT and session.guard.tripped("nse") is not None
    last = _lines(session)[-1]
    assert last["type"] == "outcome" and last["verdict"] == CHECK_THE_ACCOUNT
    assert "bug in the lookup" in last["detail"]["lookup_error"]["message"]
    assert session.interrupted is False


def test_the_teardown_cancels_our_gtts_outside_hours_and_with_the_outside_hours_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel only removes risk: the GTT cleanup ignores the hours and the opt-in."""
    monkeypatch.setenv(OUTSIDE_HOURS_ENV, "1")
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client)
    assert cases_gtt.write_refusal({OUTSIDE_HOURS_ENV: "1"}) is not None  # no new GTT ...
    client.pending = [[_gtt("9001", tag)], []]
    report = calls.teardown(session)  # ... but ours is still cancelled
    assert report is not None and report.gtts is not None
    assert report.gtts.cancelled == ["9001"] and client.count("cancel_gtt") == 1


# -- merge with main: gtt_in_flight -------------------------------------------------------------


@pytest.mark.parametrize("wrapper", ["place", "modify", "cancel"])
def test_an_ordinary_exception_after_a_gtt_intent_is_check_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrapper: str
) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    tag = _placed_gtt(session, client) if wrapper != "place" else None

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("a bug after the call")

    monkeypatch.setattr(
        session.responses, "last", broken
    )  # read after the call, before the outcome
    with pytest.raises(RuntimeError):
        if wrapper == "place":
            calls.gtt_place(session, PLAN, GTT)
        elif wrapper == "modify":
            calls.gtt_modify(
                session, "9001", GTT, before=_gtt("9001", str(tag)), **_modify_fields()
            )
        else:
            calls.gtt_cancel(session, "9001", GTT)
    last = _lines(session)[-1]
    assert last["type"] == "outcome" and last["verdict"] == CHECK_THE_ACCOUNT
    assert "a bug after the call" in last["detail"]["raised"]["message"]
    flagged_tag = client.gtt_tags()[-1]
    assert session.ledger.tag_flagged(flagged_tag) and not session.ledger.pending_tag(flagged_tag)
    assert session.guard.tripped("nse") is not None


def test_a_gtt_place_whose_outcome_cannot_be_written_is_never_cancelled_and_is_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = GttClient()
    session = _gtt_session(client, tmp_path)
    real = session.ledger.outcome

    def full(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(session.ledger, "outcome", full)
    with pytest.raises(OSError):
        calls.gtt_place(session, PLAN, GTT)
    monkeypatch.setattr(session.ledger, "outcome", real)
    tag = client.gtt_tags()[0]
    assert session.ledger.tag_flagged(tag) and session.guard.tripped("nse") is not None
    client.pending = [[_gtt("9001", tag)]]
    report = calls.teardown(session)
    assert report is not None and report.gtts is not None
    assert client.count("cancel_gtt") == 0 and report.gtts.left == ["9001"]
