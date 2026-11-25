"""A dry run of the GTT live test body against the offline broker (no network).

The live test in ``tests/live_orders/test_gtt_live.py`` runs only on UAT or on the zero-funds
production account. Here its body runs unchanged on an ``OfflineBroker`` client with the real GTT
methods, in a UAT-mode session and in a production session, so a mistake in the test code shows
up before the live run. Market hours are lifted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.live_orders import gtt, safety
from tests.live_orders import test_gtt_live as live
from tests.live_orders.safety import CheckTheAccount, OrderBudget, OrderSession, Recorder
from tradesmart import OcoLeg
from tradesmart.models.gtt import TriggeredGtt
from tradesmart.models.market import Quote
from tradesmart.models.orders import Order
from tradesmart.testing import OfflineBroker, drills

TOKEN = "1660"
WRITES = (
    "PlaceGTTOrder",
    "ModifyGTTOrder",
    "CancelGTTOrder",
    "PlaceOCOOrder",
    "ModifyOCOOrder",
    "CancelOCOOrder",
)
OCO_WRITES = ["PlaceOCOOrder", "ModifyOCOOrder", "CancelOCOOrder"]


@dataclass
class Rig:
    broker: OfflineBroker
    session: OrderSession
    pauses: list[float]  # the pauses between the look-ups by tag (no real sleep)

    def run(self) -> dict[str, Any]:
        record: dict[str, Any] = {"test": "gtt"}
        self.session.test_budget = OrderBudget(gtt.GTT_CALLS)
        try:
            live.test_far_gtt_buy_is_placed_read_back_modified_once_and_cancelled(
                self.session, record
            )
        finally:
            self.session.test_budget = None
        return record

    def run_oco(self, record: dict[str, Any] | None = None) -> dict[str, Any]:
        record = {"test": "oco"} if record is None else record
        self.session.test_budget = OrderBudget(gtt.GTT_CALLS)  # the test sets its own
        try:
            live.test_oco_lifecycle(self.session, record)
        finally:
            self.session.test_budget = None
        return record

    def requests(self, endpoint: str) -> list[dict[str, Any]]:
        return [r.payload for r in self.broker.requests if r.endpoint == endpoint]

    def writes(self) -> list[str]:
        return [r.endpoint for r in self.broker.requests if r.endpoint in WRITES]


def _make_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, uat: bool) -> Iterator[Rig]:
    monkeypatch.setattr(safety, "market_closed_reason", lambda segment, now: None)
    pauses: list[float] = []
    real_run = gtt.run_gtt

    def run_without_waiting(*args: Any, **kwargs: Any) -> Any:
        kwargs.update(sleep=pauses.append)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(gtt, "run_gtt", run_without_waiting)
    with OfflineBroker("UAT01" if uat else "PROD01") as broker:
        broker.add_instrument("NSE", TOKEN, "ITC-EQ", lot_size=1, tick_size="0.05")
        broker.set_quote("NSE", TOKEN, lp="400.00", lc="320.00", uc="480.00", ti="0.05", ls="1")
        client = broker.client()
        limit = safety.UAT_MAX_ORDERS_PER_RUN if uat else safety.MAX_ORDERS_PER_RUN
        session = OrderSession(
            client,
            budget=OrderBudget(limit),
            recorder=Recorder(tmp_path / "live_orders.jsonl"),
            uat=uat,
        )
        try:
            yield Rig(broker, session, pauses)
        finally:
            client.close()


@pytest.fixture(params=[True, False], ids=["uat", "zero-funds-production"])
def rig(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Rig]:
    yield from _make_rig(monkeypatch, tmp_path, uat=request.param)


@pytest.fixture
def prod_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Rig]:
    yield from _make_rig(monkeypatch, tmp_path, uat=False)


def test_the_gtt_is_placed_read_back_modified_once_and_cancelled(rig: Rig) -> None:
    record = rig.run()
    assert rig.writes() == ["PlaceGTTOrder", "ModifyGTTOrder", "CancelGTTOrder"]
    (placed,) = rig.requests("PlaceGTTOrder")
    assert placed["ai_t"] == "LTP_A_O" and placed["validity"] == "DAY"
    assert Decimal(placed["d"]) == Decimal("1200.00")  # 3 x the fresh last price
    assert (placed["trantype"], placed["prctyp"], placed["prd"], placed["ret"]) == (
        "B",
        "LMT",
        "C",
        "DAY",
    )
    assert Decimal(placed["prc"]) == Decimal("360.00") and placed["qty"] == "1"
    assert placed["remarks"].startswith("sdkgtt-") and placed["tsym"] == "ITC-EQ"
    (modified,) = rig.requests("ModifyGTTOrder")
    assert Decimal(modified["d"]) == Decimal("1240.00")  # 3.1 x the fresh last price
    assert modified["remarks"] == placed["remarks"] and modified["ai_t"] == "LTP_A_O"
    assert rig.session.client.pending_gtts() == []
    assert rig.session.budget.used == 3  # place, modify and cancel each count
    assert record["gtt"]["place"]["outcome"] == "accepted"
    assert record["gtt"]["modify"]["outcome"] == "accepted"
    assert record["gtt"]["left_pending"] == []


def test_the_record_never_carries_the_account_id(rig: Rig) -> None:
    record = rig.run()
    text = repr(record)
    assert rig.broker.client_id not in text


def test_production_with_funds_is_skipped_before_any_gtt_call(prod_rig: Rig) -> None:
    prod_rig.broker.set_funds(cash="1000")
    with pytest.raises(pytest.skip.Exception, match="zero-funds"):
        prod_rig.run()
    assert prod_rig.writes() == []


def test_a_funded_session_is_refused_before_any_gtt_call(prod_rig: Rig) -> None:
    prod_rig.session.funded = True
    with pytest.raises(pytest.skip.Exception, match="never in funded mode"):
        prod_rig.run()
    assert prod_rig.writes() == []
    assert prod_rig.requests("GetPendingGTTOrder") == []


def _quotes_after_the_first(rig: Rig, fields: dict[str, str]) -> None:
    client = rig.session.client
    real: Callable[..., Quote] = client.quote
    calls = 0

    def quote(exchange: str, token: str) -> Quote:
        nonlocal calls
        calls += 1
        if calls == 1:  # the plan's quote
            return real(exchange, token)
        return Quote.model_validate({"stat": "Ok", **fields})

    client.quote = quote  # type: ignore[method-assign]


def test_a_missing_fresh_quote_skips_before_sending(rig: Rig) -> None:
    _quotes_after_the_first(rig, {"ti": "0.05"})  # no lp
    with pytest.raises(pytest.skip.Exception, match="no fresh last price"):
        rig.run()
    assert rig.writes() == [] and rig.session.budget.used == 0


def test_a_condition_already_true_skips_before_sending(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gtt, "PLACE_FACTOR", Decimal("1"))
    with pytest.raises(pytest.skip.Exception, match="fire at once"):
        rig.run()
    assert rig.writes() == [] and rig.session.budget.used == 0


def test_no_room_for_three_calls_skips_before_sending(prod_rig: Rig) -> None:
    prod_rig.session.budget.used = 1  # another order test of the run spent one of the three
    with pytest.raises(pytest.skip.Exception, match="cap"):
        prod_rig.run()
    assert prod_rig.writes() == [] and prod_rig.session.budget.used == 1


def test_a_missing_quote_before_the_modify_cancels_without_modifying(rig: Rig) -> None:
    client = rig.session.client
    real = client.quote
    calls = 0

    def quote(exchange: str, token: str) -> Quote:
        nonlocal calls
        calls += 1
        if calls <= 2:  # the plan's quote and the place's fresh quote
            return real(exchange, token)
        return Quote.model_validate({"stat": "Ok", "ti": "0.05"})

    client.quote = quote  # type: ignore[method-assign]
    with pytest.raises(pytest.skip.Exception, match="no fresh last price"):
        rig.run()
    assert rig.writes() == ["PlaceGTTOrder", "CancelGTTOrder"]
    assert client.pending_gtts() == [] and rig.session.budget.used == 2


def test_an_unknown_place_is_found_by_tag_cancelled_and_fails_the_test(rig: Rig) -> None:
    rig.broker.fail_next("PlaceGTTOrder", drills.timeout_after_send())
    with pytest.raises(AssertionError, match="unknown"):
        rig.run()
    assert rig.writes() == ["PlaceGTTOrder", "CancelGTTOrder"]  # never resent, never modified
    assert rig.session.client.pending_gtts() == []


def test_a_rejected_place_is_skipped_with_the_reason_recorded(rig: Rig) -> None:
    rig.broker.fail_next("PlaceGTTOrder", drills.reject("GTT not enabled"))
    record: dict[str, Any] = {}
    rig.session.test_budget = OrderBudget(gtt.GTT_CALLS)
    with pytest.raises(pytest.skip.Exception, match="rejected"):
        live.test_far_gtt_buy_is_placed_read_back_modified_once_and_cancelled(rig.session, record)
    assert rig.writes() == ["PlaceGTTOrder"]
    assert record["gtt"]["place"]["outcome"] == "rejected"
    assert "GTT not enabled" in record["gtt"]["place"]["message"]


def test_a_gtt_left_pending_is_check_the_account(rig: Rig) -> None:
    rig.broker.fail_next("CancelGTTOrder", drills.reject("Oi delete failed"))
    with pytest.raises(CheckTheAccount, match="CHECK THE ACCOUNT"):
        rig.run()
    assert rig.writes() == ["PlaceGTTOrder", "ModifyGTTOrder", "CancelGTTOrder"]  # never retried
    assert len(rig.session.client.pending_gtts()) == 1


def test_a_gtt_that_fired_is_check_the_account(rig: Rig) -> None:
    client = rig.session.client
    real = client.modify_gtt

    def fire_then_modify(alert_id: str, **kwargs: Any) -> str:
        rig.broker.trigger_gtt(alert_id)  # the GTT fires before the modify reaches the server
        result: str = real(alert_id, **kwargs)
        return result

    client.modify_gtt = fire_then_modify  # type: ignore[method-assign]
    with pytest.raises(CheckTheAccount, match="fired"):
        rig.run()
    # the known id is still cancelled once (refused: it fired), never retried
    assert rig.writes() == ["PlaceGTTOrder", "ModifyGTTOrder", "CancelGTTOrder"]


def _book_with_uat_remarks(rig: Rig, token: Callable[[str], str]) -> None:
    """From now on ``orders()`` also lists an order without ``add_ord_id`` whose remarks are in
    the UAT form ``<ai_t>: <tag>: <text>: <tag>`` (live, 13 Nov 2026), ``token`` of our tag."""
    client = rig.session.client
    real = client.orders

    def orders() -> list[Order]:
        (placed,) = rig.requests("PlaceGTTOrder")
        word = token(placed["remarks"])
        row = Order.model_validate({
            "norenordno": "880001", "status": "OPEN", "exch": "NSE", "tsym": "ITC-EQ",
            "trantype": "B", "qty": "1",
            "remarks": f"LTP_B_O: {word}: Ltp 284.25 is below 298.55: {word}",
        })  # fmt: skip
        return [row, *real()]

    client.orders = orders  # type: ignore[method-assign]


def test_an_order_whose_uat_remarks_carry_our_tag_as_a_token_is_fired(rig: Rig) -> None:
    _book_with_uat_remarks(rig, lambda tag: tag)
    with pytest.raises(CheckTheAccount, match="order 880001"):
        rig.run()


@pytest.mark.parametrize("token", [lambda t: f"{t}0", lambda t: t[:-1]], ids=["longer", "shorter"])
def test_an_order_whose_uat_remarks_carry_another_tag_is_not_fired(
    rig: Rig, token: Callable[[str], str]
) -> None:
    _book_with_uat_remarks(rig, token)
    record = rig.run()
    assert "fired" not in record["gtt"]


def _list_cancelled_gtts_as_triggered(rig: Rig, stat: str | None) -> None:
    """UAT lists a cancelled GTT in ``GetTrgOi`` with row ``stat`` ``CANCELLED`` (spec F32): each
    GTT cancelled so far appears there with ``stat``."""
    client = rig.session.client

    def triggered() -> list[TriggeredGtt]:
        (placed,) = rig.requests("PlaceGTTOrder")
        return [
            TriggeredGtt.model_validate(
                {"al_id": c["al_id"], "remarks": placed["remarks"], "stat": stat}
            )
            for c in rig.requests("CancelGTTOrder")
        ]

    client.triggered_gtts = triggered  # type: ignore[method-assign]


@pytest.mark.parametrize("stat", ["CANCELLED", "Cancelled", "cancelled"])
def test_a_cancelled_gtt_in_the_triggered_list_is_recorded_not_fired(rig: Rig, stat: str) -> None:
    _list_cancelled_gtts_as_triggered(rig, stat)
    record = rig.run()
    (cancel,) = rig.requests("CancelGTTOrder")
    assert record["gtt"]["triggered_cancelled"] == [cancel["al_id"]]
    assert "fired" not in record["gtt"]


@pytest.mark.parametrize("stat", ["TRIGGERED", "Ok", "CANCELLED ", "CANCELED", None])
def test_any_other_triggered_row_of_ours_is_fired(rig: Rig, stat: str | None) -> None:
    # Only the word CANCELLED in any letter case is not fired; anything else fails closed.
    _list_cancelled_gtts_as_triggered(rig, stat)
    with pytest.raises(CheckTheAccount, match="fired"):
        rig.run()
    assert rig.writes() == ["PlaceGTTOrder", "ModifyGTTOrder", "CancelGTTOrder"]


def test_the_read_only_gtt_tests_record_what_they_read(rig: Rig) -> None:
    record: dict[str, Any] = {}
    live.test_gtt_alert_types_are_a_list_of_words(rig.session, record)
    assert record["alert_types"] == ["ATP", "LTP"]
    record = {}
    live.test_triggered_gtts_are_readable(rig.session, record)
    assert record["triggered_count"] == 0
    assert rig.writes() == []


# -- review round 1: GTTs not (yet) visible by tag, ValueError after sending, interruptions --


def _hide_from_pending(rig: Rig, reads: set[int] | None, *, strip_tag: bool = False) -> list[int]:
    """Make ``pending_gtts()`` hide this test's GTT on the given read numbers after the place
    (1 = the first read after it; ``None``: on every read), or show it without its tag."""
    client = rig.session.client
    real = client.pending_gtts
    calls: list[int] = []
    placed = lambda: bool(rig.requests("PlaceGTTOrder"))  # noqa: E731

    def pending() -> list[Any]:
        rows = real()
        if not placed():
            return rows
        calls.append(1)
        if strip_tag:
            return [row.model_copy(update={"tag": None}) for row in rows]
        if reads is None or len(calls) in reads:
            return [row for row in rows if not (row.tag or "").startswith("sdkgtt-")]
        return rows

    client.pending_gtts = pending  # type: ignore[method-assign]
    return calls


def test_an_accepted_gtt_missing_from_the_first_read_is_looked_up_again(rig: Rig) -> None:
    _hide_from_pending(rig, {1})
    record = rig.run()
    assert rig.writes() == ["PlaceGTTOrder", "ModifyGTTOrder", "CancelGTTOrder"]
    assert rig.pauses == [gtt.LOOKUP_PAUSE_SECONDS]  # one more read, after one pause
    assert rig.session.client.pending_gtts() == []
    assert record["gtt"]["left_pending"] == []


def test_an_accepted_gtt_never_visible_is_cancelled_by_its_id(rig: Rig) -> None:
    _hide_from_pending(rig, None)
    with pytest.raises(AssertionError, match="never seen in pending_gtts"):
        rig.run()
    assert rig.writes() == ["PlaceGTTOrder", "CancelGTTOrder"]  # cancelled by the acked id
    assert len(rig.pauses) == gtt.LOOKUPS - 1
    assert rig.broker.client().pending_gtts() == []


def test_an_unknown_place_that_appears_on_a_later_read_is_cancelled(rig: Rig) -> None:
    rig.broker.fail_next("PlaceGTTOrder", drills.timeout_after_send())
    _hide_from_pending(rig, {1, 2})
    with pytest.raises(AssertionError, match="unknown"):
        rig.run()
    assert rig.writes() == ["PlaceGTTOrder", "CancelGTTOrder"]  # never resent, never modified
    assert rig.pauses == [gtt.LOOKUP_PAUSE_SECONDS] * 2
    assert rig.broker.client().pending_gtts() == []


def test_an_unknown_place_never_found_is_check_the_account(rig: Rig) -> None:
    rig.broker.fail_next("PlaceGTTOrder", drills.timeout_after_send())
    _hide_from_pending(rig, None)
    with pytest.raises(CheckTheAccount, match="never seen in pending_gtts"):
        rig.run()
    assert rig.writes() == ["PlaceGTTOrder"]  # no id to cancel by, never resent
    assert len(rig.pauses) == gtt.LOOKUPS - 1


def test_a_gtt_left_pending_without_its_tag_is_found_by_its_id(rig: Rig) -> None:
    _hide_from_pending(rig, set(), strip_tag=True)  # the rows come back without the tag
    rig.broker.fail_next("CancelGTTOrder", drills.reject("Oi delete failed"))
    with pytest.raises(CheckTheAccount, match="still pending"):
        rig.run()
    # found by its acknowledged id, so modified as usual; the rejected cancel leaves it
    assert rig.writes() == ["PlaceGTTOrder", "ModifyGTTOrder", "CancelGTTOrder"]


def test_a_value_error_after_sending_is_an_unknown_outcome(rig: Rig) -> None:
    client = rig.session.client
    real = client.place_gtt

    def sent_then_value_error(**kwargs: Any) -> str:
        real(**kwargs)
        raise ValueError("unexpected")

    client.place_gtt = sent_then_value_error  # type: ignore[method-assign]
    record: dict[str, Any] = {}
    rig.session.test_budget = OrderBudget(gtt.GTT_CALLS)
    with pytest.raises(AssertionError, match="unknown"):
        live.test_far_gtt_buy_is_placed_read_back_modified_once_and_cancelled(rig.session, record)
    assert record["gtt"]["place"]["outcome"] == "unknown"
    assert rig.writes() == ["PlaceGTTOrder", "CancelGTTOrder"]


@pytest.mark.parametrize("method", ["modify_gtt", "cancel_gtt"])
def test_an_interruption_after_the_place_prints_and_records_an_alert(
    rig: Rig, method: str, capsys: pytest.CaptureFixture[str]
) -> None:
    client = rig.session.client

    def interrupted(*args: Any, **kwargs: Any) -> str:
        raise KeyboardInterrupt

    setattr(client, method, interrupted)
    record: dict[str, Any] = {}
    rig.session.test_budget = OrderBudget(gtt.GTT_CALLS)
    with pytest.raises(KeyboardInterrupt):
        live.test_far_gtt_buy_is_placed_read_back_modified_once_and_cancelled(rig.session, record)
    assert "CHECK THE ACCOUNT" in capsys.readouterr().err
    assert "CHECK THE ACCOUNT" in record["gtt"]["alert"]
    assert "KeyboardInterrupt" in record["gtt"]["alert"]


# -- OCO (Task 9): the same gates, 4 writes on UAT, 3 on production --------------------------


def _triggers(payload: dict[str, Any]) -> dict[str, Decimal]:
    return {item["var_name"]: Decimal(str(item["d"])) for item in payload["oivariable"]}


def test_the_oco_is_placed_read_back_modified_cancelled_and_probed_on_uat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for rig in _make_rig(monkeypatch, tmp_path, uat=True):
        record = rig.run_oco()
        # the 4th write: a modify on the cancelled id
        assert rig.writes() == [*OCO_WRITES, "ModifyOCOOrder"]
        (placed,) = rig.requests("PlaceOCOOrder")
        assert _triggers(placed) == {"x": Decimal("440.00"), "y": Decimal("360.00")}
        leg = placed["place_order_params"]
        assert (leg["trantype"], leg["prctyp"], leg["prd"], leg["qty"]) == ("B", "LMT", "C", "1")
        assert placed["tsym"] == "ITC-EQ" and placed["place_order_params_leg2"]["prctyp"] == "LMT"
        first, probe = rig.requests("ModifyOCOOrder")
        assert _triggers(first) == {"x": Decimal("460.00"), "y": Decimal("360.00")}
        assert probe["al_id"] == first["al_id"]
        assert rig.session.budget.used == 4
        assert record["oco"]["probe_after_cancel"]["outcome"] == "rejected"
        assert record["oco"]["left_pending"] == []
        assert rig.session.client.pending_gtts() == []


def test_the_oco_on_production_is_three_writes_without_the_probe(prod_rig: Rig) -> None:
    record = prod_rig.run_oco()
    assert prod_rig.writes() == OCO_WRITES
    assert prod_rig.session.budget.used == 3
    assert "probe_after_cancel" not in record["oco"]


def test_the_oco_on_production_refuses_unless_the_run_has_all_three_writes(prod_rig: Rig) -> None:
    prod_rig.session.budget.used = 1
    with pytest.raises(pytest.skip.Exception, match="-k oco"):
        prod_rig.run_oco()
    assert prod_rig.writes() == [] and prod_rig.session.budget.used == 1


def test_the_oco_records_whether_the_tag_comes_back_and_the_cancelled_row(rig: Rig) -> None:
    record = rig.run_oco()
    assert record["oco"]["tag_in_pending_row"] is False  # the offline rows carry no leg remarks
    (cancel,) = rig.requests("CancelOCOOrder")
    assert record["oco"]["triggered_cancelled"] == [cancel["al_id"]]
    assert "fired" not in record["oco"]


def test_an_oco_left_pending_is_check_the_account(rig: Rig) -> None:
    rig.broker.fail_next("CancelOCOOrder", drills.reject("Oi delete failed"))
    with pytest.raises(CheckTheAccount, match="CHECK THE ACCOUNT"):
        rig.run_oco()
    assert rig.writes() == OCO_WRITES  # never retried, no probe
    assert len(rig.session.client.pending_gtts()) == 1


def test_a_missing_fresh_quote_skips_the_oco_before_sending(rig: Rig) -> None:
    _quotes_after_the_first(rig, {"ti": "0.05"})  # no lp
    with pytest.raises(pytest.skip.Exception, match="no fresh last price"):
        rig.run_oco()
    assert rig.writes() == [] and rig.session.budget.used == 0


def test_the_oco_is_refused_in_funded_mode(prod_rig: Rig) -> None:
    prod_rig.session.funded = True
    with pytest.raises(pytest.skip.Exception, match="never in funded mode"):
        prod_rig.run_oco()
    assert prod_rig.writes() == []


def test_the_oco_record_redacts_the_ip_address_and_the_account_id(rig: Rig) -> None:
    record = rig.run_oco()
    record["oco"]["pending_probe"] = {"ipaddr": "10.1.2.3", "actid": rig.broker.client_id}
    rig.session.recorder.write(record)
    text = rig.session.recorder.path.read_text(encoding="utf-8")
    assert "10.1.2.3" not in text and rig.broker.client_id not in text


# -- OCO review fix round 1: an unknown place, and a probe that is not rejected --------------


def _rows_carry_the_tag(rig: Rig) -> None:
    """``pending_gtts()`` shows the placed OCO with the run's tag on the row."""
    client = rig.session.client
    real = client.pending_gtts

    def pending() -> list[Any]:
        rows = real()
        placed = rig.requests("PlaceOCOOrder")
        if not placed:
            return rows
        tag = placed[0]["place_order_params"]["remarks"]
        return [r.model_copy(update={"tag": tag}) for r in rows]

    client.pending_gtts = pending  # type: ignore[method-assign]


def test_an_unknown_oco_place_found_by_tag_is_cancelled_once_and_never_resent(rig: Rig) -> None:
    rig.broker.fail_next("PlaceOCOOrder", drills.timeout_after_send())
    _rows_carry_the_tag(rig)
    with pytest.raises(AssertionError, match="unknown"):
        rig.run_oco()
    assert len(rig.requests("PlaceOCOOrder")) == 1  # never retried
    assert rig.writes() == ["PlaceOCOOrder", "CancelOCOOrder"]  # one cancel, no modify, no probe
    assert rig.broker.client().pending_gtts() == []


def _hide_ocos_after_the_place(rig: Rig) -> None:
    """``pending_gtts()`` lists no OCO once the place was sent (the list lags or drops it)."""
    client = rig.session.client
    real = client.pending_gtts

    def pending() -> list[Any]:
        rows = real()
        return [r for r in rows if not r.is_oco] if rig.requests("PlaceOCOOrder") else rows

    client.pending_gtts = pending  # type: ignore[method-assign]


def test_an_unknown_oco_place_not_found_is_check_the_account_and_never_resent(rig: Rig) -> None:
    rig.broker.fail_next("PlaceOCOOrder", drills.timeout_after_send())
    _hide_ocos_after_the_place(rig)
    with pytest.raises(CheckTheAccount, match="never seen in pending_gtts"):
        rig.run_oco()
    assert len(rig.requests("PlaceOCOOrder")) == 1  # never retried
    assert rig.writes() == ["PlaceOCOOrder"]  # no id to cancel by


def _answer_the_probe_with_another_new_oco(rig: Rig) -> list[str]:
    """The UAT server accepts the modify on the cancelled id, answers an alert id of a NEW OCO
    that carries another tag (so only the returned id can find it); returns that id."""
    client = rig.session.client
    real_modify = client.modify_oco
    real_place = client.place_oco
    new_ids: list[str] = []

    def modify(alert_id: str, **kwargs: Any) -> str:
        if not rig.requests("CancelOCOOrder"):
            return real_modify(alert_id, **kwargs)
        kwargs["tag"] = "unrelated-tag"
        new_id = real_place(**kwargs)
        new_ids.append(new_id)
        return new_id

    client.modify_oco = modify  # type: ignore[method-assign]
    return new_ids


def test_a_probe_answered_with_another_alert_id_that_is_pending_is_check_the_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for rig in _make_rig(monkeypatch, tmp_path, uat=True):
        new_ids = _answer_the_probe_with_another_new_oco(rig)
        with pytest.raises(CheckTheAccount, match="CHECK THE ACCOUNT") as caught:
            rig.run_oco()
        assert len(new_ids) == 1 and new_ids[0] in str(caught.value)
        # one write for the probe, nothing sent to clean up: the new OCO is still pending
        assert rig.writes().count("CancelOCOOrder") == 1
        assert [r.alert_id for r in rig.session.client.pending_gtts()] == new_ids


# -- OCO rows carry no top-level tag (remarks ""): found by the leg tag or by symbol + triggers --


def _found_by(record: dict[str, Any], key: str) -> list[str]:
    return sorted(set(record["oco"]["found_by"][key].values()))


def test_an_unknown_oco_place_without_an_id_is_found_by_symbol_and_triggers(rig: Rig) -> None:
    # the offline rows are as observed: no top-level remarks and no leg remarks
    rig.broker.fail_next("PlaceOCOOrder", drills.timeout_after_send())
    record: dict[str, Any] = {"test": "oco"}
    with pytest.raises(AssertionError, match="unknown"):
        rig.run_oco(record)
    assert record["oco"]["place"]["outcome"] == "unknown"
    assert len(rig.requests("PlaceOCOOrder")) == 1  # never retried
    assert rig.writes() == ["PlaceOCOOrder", "CancelOCOOrder"]  # cancelled once, no modify
    assert _found_by(record, "after_place") == ["symbol_and_triggers"]
    assert rig.broker.client().pending_gtts() == []


def test_an_unknown_oco_place_found_by_the_tag_in_a_leg_is_cancelled_once(rig: Rig) -> None:
    rig.broker.fail_next("PlaceOCOOrder", drills.timeout_after_send())
    client = rig.session.client
    real = client.pending_gtts

    def pending() -> list[Any]:
        placed = rig.requests("PlaceOCOOrder")
        if not placed:
            return real()
        tag = placed[0]["place_order_params"]["remarks"]
        out = []
        for row in real():
            assert not (row.tag or "").strip()  # never a top-level tag on an OCO row
            leg = row.oco_first.model_copy(update={"tag": tag}) if row.oco_first else None
            # other triggers on the row: only the leg tag can find it
            out.append(row.model_copy(update={"oco_first": leg, "oco_triggers": []}))
        return out

    client.pending_gtts = pending  # type: ignore[method-assign]
    record: dict[str, Any] = {"test": "oco"}
    with pytest.raises(AssertionError, match="unknown"):
        rig.run_oco(record)
    assert rig.writes() == ["PlaceOCOOrder", "CancelOCOOrder"]
    assert _found_by(record, "after_place") == ["leg_tag"]
    assert rig.broker.client().pending_gtts() == []


def test_an_accepted_oco_is_found_by_its_alert_id(rig: Rig) -> None:
    record = rig.run_oco()
    assert _found_by(record, "after_place") == ["alert_id"]


def test_an_oco_pending_before_the_place_is_never_taken_for_this_run(rig: Rig) -> None:
    # another OCO on the same instrument, with the same two triggers the run will send
    other = rig.broker.client().place_oco(
        exchange="NSE",
        tradingsymbol="ITC-EQ",
        transaction_type="BUY",
        quantity=1,
        product="CNC",
        stop_loss=OcoLeg(trigger_value="440.00", price="360.00"),
        target=OcoLeg(trigger_value="360.00", price="360.00"),
        tag="someone-else",
        alert_validity="DAY",
    )
    rig.broker.fail_next("PlaceOCOOrder", drills.timeout_after_send())
    record: dict[str, Any] = {"test": "oco"}
    with pytest.raises(AssertionError, match="unknown"):
        rig.run_oco(record)
    cancels = [r["al_id"] for r in rig.requests("CancelOCOOrder")]
    assert other not in cancels and len(cancels) == 1  # only this run's OCO
    assert [r.alert_id for r in rig.broker.client().pending_gtts()] == [other]
