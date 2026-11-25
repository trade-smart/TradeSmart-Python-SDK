"""Mocked unit tests pinning the OCO plumbing of the UAT matrix (OCO family, Task 1).

The never-resend OCO wrappers of ``uat_matrix/calls.py`` (``oco_place``, ``oco_modify``,
``oco_cancel``, ``find_oco``), the ledger's OCO kinds and the teardown that cancels every pending
OCO of ours once. Nothing here touches the network: ``OcoClient`` extends the GTT tests'
``GttClient`` stand-in.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.live_orders.safety import CHECK_THE_ACCOUNT, Refusal
from tests.live_orders.uat_matrix import calls
from tests.live_orders.uat_matrix.calls import CaseRef
from tests.live_orders.uat_matrix.guard import StopRun
from tests.live_orders.uat_matrix.ledger import LEDGER_NAME, OCO_PLACE, Ledger
from tests.live_orders.uat_matrix.sdk_under_test import PythonSdk
from tests.unit.test_uat_matrix_gtt import GttClient, _gtt
from tests.unit.test_uat_matrix_safety import _lines, _rejected, _session, _unknown
from tradesmart import NetworkError, OcoLeg
from tradesmart.models.gtt import Gtt, TriggeredGtt

CASE = CaseRef("oco-far-buy", "nse")
FIELDS: dict[str, Any] = {
    "exchange": "NSE",
    "tradingsymbol": "ITC-EQ",
    "quantity": 1,
    "product": "CNC",
    "validity": "DAY",
    "alert_validity": "DAY",
}
STOP = OcoLeg(trigger_value=Decimal("1320.00"), price=Decimal("1330.00"))
TARGET = OcoLeg(trigger_value=Decimal("1080.00"), price=Decimal("1090.00"))
STOP2 = OcoLeg(trigger_value=Decimal("1380.00"), price=Decimal("1390.00"))


class OcoClient(GttClient):
    """``GttClient`` plus the three OCO writes (scripted like the GTT ones)."""

    def __init__(
        self,
        *args: Any,
        oco_place: list[Any] | None = None,
        oco_modify: Any = None,
        oco_cancel: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.oco_place_results = oco_place if oco_place is not None else ["9001"]
        self.oco_modify_result = oco_modify
        self.oco_cancel_result = oco_cancel

    def place_oco(self, **kwargs: Any) -> str:
        self._record("place_oco", kwargs)
        results = self.oco_place_results
        result = results.pop(0) if len(results) > 1 else results[0]
        if isinstance(result, BaseException):
            raise result
        return str(result)

    def modify_oco(self, alert_id: str, **kwargs: Any) -> str:
        self._record("modify_oco", (alert_id, kwargs))
        if isinstance(self.oco_modify_result, BaseException):
            raise self.oco_modify_result
        return alert_id if self.oco_modify_result is None else str(self.oco_modify_result)

    def cancel_oco(self, alert_id: str) -> str:
        self._record("cancel_oco", alert_id)
        if isinstance(self.oco_cancel_result, BaseException):
            raise self.oco_cancel_result
        return alert_id

    def oco_tags(self) -> list[str]:
        return [args["tag"] for name, args in self.calls if name == "place_oco"]


def _oco_row(
    alert_id: str,
    *,
    symbol: str = "ITC-EQ",
    triggers: tuple[str, str] = ("1320.00", "1080.00"),
    legs: tuple[str, str] = ("", ""),
    prices: tuple[str, str] = ("1330.00", "1090.00"),
    remarks: str = "",
    triggered: bool = False,
) -> Gtt:
    """An OCO list row: top-level ``remarks`` EMPTY (F35 c), the tag only in the legs."""
    row: dict[str, Any] = {
        "al_id": alert_id,
        "ai_t": "LMT_BOS_O",
        "validity": "DAY",
        "tsym": symbol,
        "exch": "NSE",
        "remarks": remarks,
        "place_order_params": {
            "trantype": "B",
            "prctyp": "LMT",
            "qty": "1",
            "prc": prices[0],
            "remarks": legs[0],
        },
        "place_order_params_leg2": {
            "trantype": "B",
            "prctyp": "LMT",
            "qty": "1",
            "prc": prices[1],
            "remarks": legs[1],
        },
        "oivariable": [
            {"var_name": "x", "d": triggers[0]},
            {"var_name": "y", "d": triggers[1]},
        ],
    }
    return (TriggeredGtt if triggered else Gtt).model_validate(row)


def _oco_session(client: OcoClient, tmp_path: Path, count: int = 1) -> Any:
    session = _session(client, tmp_path)
    session.guard.reserve(CASE.case_id, count)
    return session


def _place(session: Any, client: OcoClient, alert_id: str = "9001") -> calls.OcoOutcome:
    client.oco_place_results = [alert_id]
    return calls.oco_place(
        session, CASE, stop_loss=STOP, target=TARGET, transaction_type="BUY", **FIELDS
    )


# -- oco_place ----------------------------------------------------------------------------------


def test_oco_place_spends_one_and_is_never_resent(tmp_path: Path) -> None:
    client = OcoClient(oco_place=[_unknown("PlaceOCOOrder")], pending=[[]], triggered=[[]])
    session = _oco_session(client, tmp_path)
    outcome = calls.oco_place(
        session, CASE, stop_loss=STOP, target=TARGET, transaction_type="BUY", **FIELDS
    )
    assert outcome.word == "unknown" and outcome.alert_id is None
    assert outcome.verdict == CHECK_THE_ACCOUNT
    assert client.count("place_oco") == 1  # never resent
    intent = next(x for x in _lines(session) if x["type"] == "intent")
    last = _lines(session)[-1]
    assert intent["kind"] == "oco_place" and intent["tag"] == outcome.tag
    assert last["type"] == "outcome" and last["call"] == intent["call"]
    assert last["verdict"] == CHECK_THE_ACCOUNT
    assert session.ledger.tag_flagged(outcome.tag)
    with pytest.raises(Refusal):  # the case's one declared order is spent
        calls.oco_place(
            session, CASE, stop_loss=STOP, target=TARGET, transaction_type="BUY", **FIELDS
        )
    assert client.count("place_oco") == 1


def test_oco_place_acknowledged_records_the_alert_id_under_the_ledger_tag(tmp_path: Path) -> None:
    client = OcoClient()
    session = _oco_session(client, tmp_path)
    outcome = _place(session, client)
    assert outcome.word == "accepted" and outcome.alert_id == "9001"
    place_call = next(args for name, args in client.calls if name == "place_oco")
    assert place_call["tag"] == outcome.tag == client.oco_tags()[0]
    assert place_call["stop_loss"] == STOP and place_call["transaction_type"] == "BUY"
    assert session.ledger.alert_tag("9001") == outcome.tag
    assert session.ledger.alert_is_oco("9001")
    assert Ledger(session.ledger.path, session.run_id).alert_is_oco("9001")  # survives a resume


def test_oco_place_rejected_and_not_sent_words(tmp_path: Path) -> None:
    client = OcoClient(oco_place=[_rejected("no")])
    session = _oco_session(client, tmp_path)
    assert (
        calls.oco_place(
            session, CASE, stop_loss=STOP, target=TARGET, transaction_type="BUY", **FIELDS
        ).word
        == "rejected"
    )
    from tradesmart import RequestValidationError

    client2 = OcoClient(oco_place=[RequestValidationError("bad")])
    session2 = _oco_session(client2, tmp_path / "b")
    out = calls.oco_place(
        session2, CASE, stop_loss=STOP, target=TARGET, transaction_type="BUY", **FIELDS
    )
    assert out.word == "not_sent" and not out.sent


def test_an_ambiguous_oco_place_is_found_by_leg_tag_never_resent(tmp_path: Path) -> None:
    client = OcoClient(
        oco_place=[_unknown("PlaceOCOOrder")],
        pending=[lambda c: [_oco_row("9007", legs=(c.oco_tags()[0], c.oco_tags()[0]))]],
    )
    session = _oco_session(client, tmp_path)
    outcome = calls.oco_place(
        session, CASE, stop_loss=STOP, target=TARGET, transaction_type="BUY", **FIELDS
    )
    assert outcome.verdict == calls.FOUND and outcome.alert_id == "9007"
    assert session.ledger.alerts() == {"9007"} and client.count("place_oco") == 1


def test_an_ambiguous_oco_place_that_fired_is_found_in_the_triggered_list(tmp_path: Path) -> None:
    client = OcoClient(
        oco_place=[_unknown("PlaceOCOOrder")],
        pending=[[]],
        triggered=[lambda c: [_oco_row("9008", legs=(c.oco_tags()[0], ""), triggered=True)]],
    )
    session = _oco_session(client, tmp_path)
    outcome = calls.oco_place(
        session, CASE, stop_loss=STOP, target=TARGET, transaction_type="BUY", **FIELDS
    )
    assert outcome.verdict == calls.FOUND and session.ledger.alerts() == {"9008"}


def test_an_interrupted_oco_place_is_recorded_flagged_and_reraised(tmp_path: Path) -> None:
    client = OcoClient(oco_place=[KeyboardInterrupt()])
    session = _oco_session(client, tmp_path)
    with pytest.raises(KeyboardInterrupt):
        calls.oco_place(
            session, CASE, stop_loss=STOP, target=TARGET, transaction_type="BUY", **FIELDS
        )
    last = _lines(session)[-1]
    assert last["verdict"] == "interrupted" and session.interrupted
    assert session.ledger.tag_flagged(last["tag"]) and client.count("place_oco") == 1


def test_oco_place_is_refused_in_a_mock_session_without_the_oco_switch(tmp_path: Path) -> None:
    client = OcoClient()
    session = _oco_session(client, tmp_path)
    session.mock = True
    session.mock_refusal = lambda: None
    with pytest.raises(Refusal):
        _place(session, client)
    assert client.count("place_oco") == 0
    session.mock_oco = True  # the owner's typed switch (set by the gate, Task 6)
    assert _place(session, client).word == "accepted"


def test_oco_place_refuses_a_foreign_tag(tmp_path: Path) -> None:
    client = OcoClient()
    session = _oco_session(client, tmp_path)
    with pytest.raises(ValueError, match="tag"):
        calls.oco_place(
            session,
            CASE,
            stop_loss=STOP,
            target=TARGET,
            transaction_type="BUY",
            tag="mine",
            **FIELDS,
        )
    assert client.count("place_oco") == 0


def test_oco_place_uses_a_tag_the_ledger_issued(tmp_path: Path) -> None:
    client = OcoClient()
    session = _oco_session(client, tmp_path)
    tag = session.ledger.new_tag(CASE.case_id)
    out = calls.oco_place(
        session, CASE, stop_loss=STOP, target=TARGET, transaction_type="BUY", tag=tag, **FIELDS
    )
    assert out.tag == tag and client.oco_tags() == [tag]


# -- lookups ------------------------------------------------------------------------------------


def test_oco_lookup_by_leg_tag_and_triggers(tmp_path: Path) -> None:
    triggers = (Decimal("1320.00"), Decimal("1080.00"))
    tag = "sdkuat-1120abcd-p001-1"
    by_leg = _oco_row("1", legs=(tag, tag))  # remarks (top level) empty
    by_one_leg = _oco_row("2", legs=("", tag), triggers=("1.00", "2.00"))
    by_triggers = _oco_row("3")  # no leg tags: symbol + both triggers
    swapped = _oco_row("4", triggers=("1080.00", "1320.00"))  # same two values, either slot
    other_symbol = _oco_row("5", symbol="TCS-EQ")
    other_trigger = _oco_row("6", triggers=("1320.00", "1000.00"))
    foreign_tag = _oco_row("7", legs=("sdkuat-other", "sdkuat-other"))  # a tag, not ours
    plain_gtt = _gtt("8", "by-hand")
    client = OcoClient(
        pending=[
            [
                by_leg,
                by_one_leg,
                by_triggers,
                swapped,
                other_symbol,
                other_trigger,
                foreign_tag,
                plain_gtt,
            ]
        ]
    )
    session = _oco_session(client, tmp_path)
    rows = calls.find_oco(session, tag, "ITC-EQ", triggers, [], exclude=frozenset())
    assert rows is not None
    assert [r.alert_id for r in rows] == ["1", "2", "3", "4"]
    by_id = calls.find_oco(session, "no-tag", "NONE", triggers, ["7"])
    assert by_id is not None and [r.alert_id for r in by_id] == ["7"]


def test_oco_lookup_by_symbol_and_triggers_is_off_unless_a_snapshot_is_given(
    tmp_path: Path,
) -> None:
    """Fail closed: without ``exclude`` (the untagged OCOs pending before the place) an
    untagged row is never matched by its symbol and triggers alone."""
    triggers = (Decimal("1320.00"), Decimal("1080.00"))
    client = OcoClient(pending=[[_oco_row("3")]])
    session = _oco_session(client, tmp_path)
    assert calls.find_oco(session, "sdkuat-1120abcd-p001-1", "ITC-EQ", triggers, []) == []


def test_oco_lookup_returns_none_when_the_read_fails(tmp_path: Path) -> None:
    client = OcoClient(pending=[NetworkError("down")])
    session = _oco_session(client, tmp_path)
    assert calls.find_oco(session, "t", "ITC-EQ", (Decimal(1), Decimal(2)), []) is None


# -- modify / cancel ----------------------------------------------------------------------------


def test_oco_modify_sends_the_ledger_tag_and_refuses_another_alert(tmp_path: Path) -> None:
    client = OcoClient()
    session = _oco_session(client, tmp_path)
    tag = _place(session, client).tag
    out = calls.oco_modify(
        session, CASE, "9001", stop_loss=STOP2, target=TARGET, transaction_type="BUY", **FIELDS
    )
    assert out.word == "accepted" and out.returned == "9001"
    name, (alert, sent) = next(c for c in client.calls if c[0] == "modify_oco")
    assert alert == "9001" and sent["tag"] == tag and sent["stop_loss"] == STOP2
    with pytest.raises(Refusal, match="not a GTT of this run"):
        calls.oco_modify(
            session, CASE, "777", stop_loss=STOP2, target=TARGET, transaction_type="BUY", **FIELDS
        )
    with pytest.raises(ValueError, match="tag"):
        calls.oco_modify(
            session,
            CASE,
            "9001",
            stop_loss=STOP2,
            target=TARGET,
            transaction_type="BUY",
            tag="mine",
            **FIELDS,
        )
    assert client.count("modify_oco") == 1


def test_an_ambiguous_oco_modify_is_resolved_only_when_the_row_shows_the_change(
    tmp_path: Path,
) -> None:
    client = OcoClient(oco_modify=_unknown("ModifyOCOOrder"))
    session = _oco_session(client, tmp_path)
    tag = _place(session, client).tag
    before = _oco_row("9001", legs=(tag, tag))
    applied = _oco_row(
        "9001",
        legs=(tag, tag),
        triggers=("1380.00", "1080.00"),
        prices=("1390.00", "1090.00"),
    )
    client.pending = [[applied]]
    out = calls.oco_modify(
        session,
        CASE,
        "9001",
        before=before,
        stop_loss=STOP2,
        target=TARGET,
        transaction_type="BUY",
        **FIELDS,
    )
    assert out.verdict == calls.RESOLVED and client.count("modify_oco") == 1
    client2 = OcoClient(oco_modify=_unknown("ModifyOCOOrder"), pending=[[before]])
    session2 = _oco_session(client2, tmp_path / "b")
    tag2 = _place(session2, client2).tag
    before2 = _oco_row("9001", legs=(tag2, tag2))
    client2.pending = [[before2]]
    out2 = calls.oco_modify(
        session2,
        CASE,
        "9001",
        before=before2,
        stop_loss=STOP2,
        target=TARGET,
        transaction_type="BUY",
        **FIELDS,
    )
    assert out2.verdict == CHECK_THE_ACCOUNT and session2.ledger.alert_flagged("9001")


def test_one_oco_cancel_per_alert_per_run(tmp_path: Path) -> None:
    client = OcoClient()
    session = _oco_session(client, tmp_path)
    _place(session, client)
    out = calls.oco_cancel(session, CASE, "9001")
    assert out.word == "accepted" and out.returned == "9001"
    with pytest.raises(Refusal, match="at most once"):
        calls.oco_cancel(session, CASE, "9001")
    with pytest.raises(Refusal, match="not a GTT of this run"):
        calls.oco_cancel(session, CASE, "777")
    assert client.count("cancel_oco") == 1
    kinds = [x["kind"] for x in _lines(session) if x["type"] == "intent"]
    assert kinds == ["oco_place", "oco_cancel"]


def test_an_ambiguous_oco_cancel_is_resolved_when_no_longer_pending(tmp_path: Path) -> None:
    client = OcoClient(oco_cancel=_unknown("CancelOCOOrder"), pending=[[]])
    session = _oco_session(client, tmp_path)
    _place(session, client)
    out = calls.oco_cancel(session, CASE, "9001")
    assert out.verdict == calls.RESOLVED and client.count("cancel_oco") == 1


def test_an_oco_place_whose_triggers_cannot_be_read_never_writes_an_intent(
    tmp_path: Path,
) -> None:
    """The lookup values (symbol, both triggers) are worked out before the ledger intent: a
    failure there refuses the place with nothing spent, written or sent, never a false CHECK
    THE ACCOUNT for a write that never went out."""
    client = OcoClient()
    session = _oco_session(client, tmp_path)
    bad = OcoLeg(trigger_value="not-a-number", price=Decimal("1330.00"))
    with pytest.raises(ValueError, match="trigger"):
        calls.oco_place(
            session, CASE, stop_loss=bad, target=TARGET, transaction_type="BUY", **FIELDS
        )
    assert client.count("place_oco") == 0
    lines = _lines(session)
    assert [x for x in lines if x["type"] in ("intent", "outcome")] == []
    assert CHECK_THE_ACCOUNT not in json.dumps(lines)
    assert session.guard.tripped("nse") is None
    assert _place(session, client).word == "accepted"  # the case's one place is still there


def test_oco_modify_and_cancel_refuse_a_plain_gtt_of_the_run(tmp_path: Path) -> None:
    """Only an alert the ledger knows as an OCO (placed by ``oco_place``) gets an OCO call: a
    plain GTT of the run is refused before any intent or send."""
    from tests.unit.test_uat_matrix_gtt import GTT, PLAN

    client = OcoClient(gtt_place=["5001"])
    session = _oco_session(client, tmp_path)
    session.guard.reserve(GTT.case_id, 1)
    assert calls.gtt_place(session, PLAN, GTT).alert_id == "5001"
    assert not session.ledger.alert_is_oco("5001")
    intents = len([x for x in _lines(session) if x["type"] == "intent"])
    with pytest.raises(Refusal, match="not an OCO"):
        calls.oco_modify(
            session, CASE, "5001", stop_loss=STOP2, target=TARGET, transaction_type="BUY", **FIELDS
        )
    with pytest.raises(Refusal, match="not an OCO"):
        calls.oco_cancel(session, CASE, "5001")
    assert client.count("modify_oco") == 0 and client.count("cancel_oco") == 0
    assert len([x for x in _lines(session) if x["type"] == "intent"]) == intents
    _place(session, client)  # an OCO of the run is still accepted
    assert calls.oco_cancel(session, CASE, "9001").word == "accepted"


def test_oco_cancel_not_sent_releases_the_one_cancel(tmp_path: Path) -> None:
    from tradesmart import RequestValidationError

    client = OcoClient(oco_cancel=RequestValidationError("bad"))
    session = _oco_session(client, tmp_path)
    _place(session, client)
    assert calls.oco_cancel(session, CASE, "9001").word == "not_sent"
    client.oco_cancel_result = None
    assert calls.oco_cancel(session, CASE, "9001").word == "accepted"


# -- ledger -------------------------------------------------------------------------------------


def test_an_oco_place_intent_without_its_outcome_blocks_the_case_on_resume(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_NAME
    ledger = Ledger(path, "1120abcd")
    tag = ledger.new_tag(CASE.case_id)
    ledger.intent(tag, CASE.case_id, OCO_PLACE, segment="nse")
    reopened = Ledger(path, "1120abcd")
    assert reopened.tag_flagged(tag) and reopened.unfinished(CASE.case_id)
    assert reopened.sent(tag) and reopened.segment_of(tag) == "nse"


# -- teardown -----------------------------------------------------------------------------------


def test_teardown_cancels_pending_ocos_once(tmp_path: Path) -> None:
    client = OcoClient()
    session = _oco_session(client, tmp_path)
    tag = _place(session, client).tag
    # 9002: an OCO of our tag the lookup found (no top-level remarks); 777: someone else's.
    client.pending = [
        [
            _oco_row("9001", legs=(tag, tag)),
            _oco_row("9002", legs=(tag, tag)),
            _oco_row("777", legs=("by-hand", "by-hand")),
        ],
        [_oco_row("777", legs=("by-hand", "by-hand"))],
    ]
    report = calls.teardown(session)
    assert report is not None and report.gtts is not None
    cancelled = sorted(args for name, args in client.calls if name == "cancel_oco")
    assert cancelled == ["9001", "9002"] and client.count("cancel_gtt") == 0
    assert sorted(report.gtts.cancelled) == ["9001", "9002"] and report.gtts.left == []
    assert report.gtts.untouched == ["777"]
    again = calls.teardown(session)
    assert again is not None and client.count("cancel_oco") == 2  # once each, ever
    note = next(x for x in reversed(_lines(session)) if x.get("type") == "teardown")
    assert note["gtts_left"] == []


def test_teardown_keeps_an_oco_that_stays_pending_as_left(tmp_path: Path) -> None:
    client = OcoClient()
    session = _oco_session(client, tmp_path)
    tag = _place(session, client).tag
    client.pending = [[_oco_row("9001", legs=(tag, tag))]]
    report = calls.teardown(session)
    assert report is not None and report.gtts is not None and report.gtts.left == ["9001"]
    assert any(x["type"] == "gtt_left" and CHECK_THE_ACCOUNT in x["alert"] for x in _lines(session))


def test_ctrl_c_teardown_sends_nothing(tmp_path: Path) -> None:
    client = OcoClient()
    session = _oco_session(client, tmp_path)
    _place(session, client)
    reads = client.count("pending_gtts")  # the place snapshot, before the teardown
    session.interrupted = True
    assert calls.teardown(session) is None
    assert client.count("cancel_oco") == 0 and client.count("pending_gtts") == reads
    note = _lines(session)[-1]
    assert note["type"] == "teardown" and note["alerts"] == ["9001"]


# -- the SDK seam -------------------------------------------------------------------------------


def test_python_sdk_forwards_oco_calls() -> None:
    seen: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    class Recorder:
        client_id = "TESTUAT1"

        def place_oco(self, *args: Any, **kwargs: Any) -> str:
            seen.append(("place_oco", args, kwargs))
            return "a1"

        def modify_oco(self, *args: Any, **kwargs: Any) -> str:
            seen.append(("modify_oco", args, kwargs))
            return "a2"

        def cancel_oco(self, *args: Any, **kwargs: Any) -> str:
            seen.append(("cancel_oco", args, kwargs))
            return "a3"

    sdk = PythonSdk(Recorder())  # type: ignore[arg-type]
    assert sdk.place_oco(exchange="NSE", tag="t", stop_loss=STOP) == "a1"
    assert sdk.modify_oco("9001", exchange="NSE", tag="t") == "a2"
    assert sdk.cancel_oco("9001") == "a3"
    assert seen == [
        ("place_oco", (), {"exchange": "NSE", "tag": "t", "stop_loss": STOP}),
        ("modify_oco", ("9001",), {"exchange": "NSE", "tag": "t"}),
        ("cancel_oco", ("9001",), {}),
    ]


# -- fix round 1: lookups never adopt an OCO the run did not place -------------------------------


def _unknown_place_client(pending: list[Any], triggered: list[Any] | None = None) -> OcoClient:
    return OcoClient(
        oco_place=[_unknown("PlaceOCOOrder")], pending=pending, triggered=triggered or [[]]
    )


def _place_unknown(session: Any) -> calls.OcoOutcome:
    return calls.oco_place(
        session, CASE, stop_loss=STOP, target=TARGET, transaction_type="BUY", **FIELDS
    )


def test_a_pre_existing_untagged_oco_is_neither_adopted_nor_cancelled(tmp_path: Path) -> None:
    pre = _oco_row("55")  # same symbol and triggers, no tag, pending before the place
    client = _unknown_place_client([[pre]])
    session = _oco_session(client, tmp_path)
    out = _place_unknown(session)
    assert out.verdict == CHECK_THE_ACCOUNT and out.alert_id is None
    assert session.ledger.alerts() == set()
    report = calls.teardown(session)
    assert report is not None and report.gtts is not None
    assert report.gtts.untouched == ["55"] and client.count("cancel_oco") == 0
    assert client.count("cancel_gtt") == 0


def test_a_new_untagged_oco_of_the_plan_is_adopted_by_symbol_and_triggers(tmp_path: Path) -> None:
    pre, new = _oco_row("55"), _oco_row("9100")
    client = _unknown_place_client([[pre], [pre, new]])
    session = _oco_session(client, tmp_path)
    out = _place_unknown(session)
    assert out.verdict == calls.FOUND and out.alert_id == "9100"
    assert session.ledger.alerts() == {"9100"} and client.count("place_oco") == 1


def test_two_new_untagged_candidates_are_check_the_account(tmp_path: Path) -> None:
    client = _unknown_place_client([[], [_oco_row("9100"), _oco_row("9101")]])
    session = _oco_session(client, tmp_path)
    out = _place_unknown(session)
    assert out.verdict == CHECK_THE_ACCOUNT and session.ledger.alerts() == set()
    assert session.ledger.tag_flagged(out.tag)


def test_a_new_untagged_oco_of_another_size_side_or_exchange_is_not_adopted(
    tmp_path: Path,
) -> None:
    def row(alert_id: str, qty: str = "1", side: str = "B", exch: str = "NSE") -> Gtt:
        base = _oco_row(alert_id)
        data = base.model_dump(by_alias=True)
        data["exch"] = exch
        for key in ("place_order_params", "place_order_params_leg2"):
            data[key]["qty"] = qty
            data[key]["trantype"] = side
        return Gtt.model_validate(data)

    client = _unknown_place_client(
        [[], [row("9100", qty="5"), row("9101", side="S"), row("9102", exch="BSE")]]
    )
    session = _oco_session(client, tmp_path)
    out = _place_unknown(session)
    assert out.verdict == CHECK_THE_ACCOUNT and session.ledger.alerts() == set()


def test_an_unreadable_snapshot_switches_the_symbol_fallback_off(tmp_path: Path) -> None:
    client = _unknown_place_client([NetworkError("down"), [_oco_row("9100")]])
    session = _oco_session(client, tmp_path)
    out = _place_unknown(session)
    assert out.verdict == CHECK_THE_ACCOUNT and session.ledger.alerts() == set()


def test_a_reopened_ledger_cancels_its_acknowledged_oco_once_with_cancel_oco(
    tmp_path: Path,
) -> None:
    client = OcoClient()
    first = _oco_session(client, tmp_path)
    tag = _place(first, client).tag
    later = _oco_session(client, tmp_path / "later")
    later.ledger = Ledger(first.ledger.path, first.run_id)  # the run, reopened
    client.pending = [[_oco_row("9001", legs=(tag, tag))], []]
    report = calls.teardown(later)
    assert report is not None and report.gtts is not None
    assert report.gtts.cancelled == ["9001"] and report.gtts.left == []
    assert client.count("cancel_oco") == 1 and client.count("cancel_gtt") == 0
    calls.teardown(later)
    assert client.count("cancel_oco") == 1  # once, ever


# -- fix round 1: mock checks and the plain fields -----------------------------------------------


def _mock_oco_session(client: OcoClient, tmp_path: Path, answers: list[str | None]) -> Any:
    session = _oco_session(client, tmp_path)
    session.mock = True
    session.mock_refusal = lambda: None
    session.mock_oco = True
    queue = list(answers)
    session.mock_refusal = lambda: queue.pop(0) if queue else None
    return session


@pytest.mark.parametrize("answers", [["rolled over"], [None, "rolled over"]])
def test_oco_place_rechecks_the_mock_switch_at_entry_and_before_the_intent(
    tmp_path: Path, answers: list[str | None]
) -> None:
    client = OcoClient()
    session = _mock_oco_session(client, tmp_path, answers)
    with pytest.raises(StopRun):
        _place(session, client)
    assert client.count("place_oco") == 0
    assert not any(x["type"] == "intent" for x in _lines(session))


@pytest.mark.parametrize("answers", [["rolled over"], [None, "rolled over"]])
def test_oco_modify_rechecks_the_mock_switch_and_cancel_never_asks(
    tmp_path: Path, answers: list[str | None]
) -> None:
    client = OcoClient()
    session = _mock_oco_session(client, tmp_path, [None, None])
    _place(session, client)
    queue = list(answers)
    session.mock_refusal = lambda: queue.pop(0) if queue else None
    with pytest.raises(StopRun):
        calls.oco_modify(
            session, CASE, "9001", stop_loss=STOP2, target=TARGET, transaction_type="BUY", **FIELDS
        )
    assert client.count("modify_oco") == 0
    session.mock_refusal = lambda: "rolled over"
    assert calls.oco_cancel(session, CASE, "9001").word == "accepted"  # a cancel never asks


@pytest.mark.parametrize(
    "bad",
    [
        {"alert_validity": "GTT"},
        {"alert_validity": None},
        {"bogus": 1},
        {"amo": True},
        {"order_type": "LIMIT"},
    ],
)
def test_oco_place_and_modify_refuse_bad_fields_before_spend_or_send(
    tmp_path: Path, bad: dict[str, Any]
) -> None:
    client = OcoClient()
    session = _oco_session(client, tmp_path)
    fields = {**FIELDS, **bad}
    with pytest.raises(ValueError):
        calls.oco_place(
            session, CASE, stop_loss=STOP, target=TARGET, transaction_type="BUY", **fields
        )
    assert client.count("place_oco") == 0
    assert _place(session, client).word == "accepted"  # the declared order was not spent
    with pytest.raises(ValueError):
        calls.oco_modify(
            session, CASE, "9001", stop_loss=STOP2, target=TARGET, transaction_type="BUY", **fields
        )
    assert client.count("modify_oco") == 0


def test_oco_alert_validity_is_required_and_day_is_normalised(tmp_path: Path) -> None:
    client = OcoClient()
    session = _oco_session(client, tmp_path)
    missing = {k: v for k, v in FIELDS.items() if k != "alert_validity"}
    with pytest.raises(ValueError, match="alert_validity"):
        calls.oco_place(
            session, CASE, stop_loss=STOP, target=TARGET, transaction_type="BUY", **missing
        )
    ok = calls.oco_place(
        session,
        CASE,
        stop_loss=STOP,
        target=TARGET,
        transaction_type="BUY",
        **{**FIELDS, "alert_validity": " day "},
    )
    assert ok.word == "accepted"
