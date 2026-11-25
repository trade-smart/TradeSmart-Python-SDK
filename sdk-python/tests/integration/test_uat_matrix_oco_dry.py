"""Dry run of the UAT matrix OCO family on the offline broker.

The real family code (``cases_oco``: the far, fires and slot-observation cases, the never-resend
OCO wrappers of ``calls``, the ledger) runs end to end on ``UatLikeMarket``. The offline broker
lists an OCO row without leg ``remarks``; ``OcoMarket`` below wraps the client's list reads and
adds the tag the server shows on both legs (F35 c), so a leg-tag lookup is driven too. The
offline broker never fires an OCO (its design §6): ``FiringOcoMarket`` scripts the fire (the
legs whose orders go out, and what happens to them) for the fires and slots cases.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.integration.uat_family_rig import family_rig
from tests.integration.uat_like import UatLikeMarket
from tests.live_orders import mock_session, safety
from tests.live_orders.safety import CHECK_THE_ACCOUNT, Refusal
from tests.live_orders.uat_matrix import batch, calls, cases_oco
from tradesmart import TradeSmart
from tradesmart.testing import drills


@dataclass
class OcoMarket(UatLikeMarket):
    """``UatLikeMarket`` plus the legs' ``remarks`` on OCO list rows (both legs carry exactly the
    tag), as the server lists them; ``strip_type`` hides ``ai_t`` (a row of unexpected type)."""

    tags: dict[str, str] = field(default_factory=dict)
    strip_type: bool = False

    def install(self, client: TradeSmart) -> TradeSmart:
        client = super().install(client)
        place = client.place_oco

        def placed(**kwargs: Any) -> str:
            alert_id = place(**kwargs)
            self.tags[alert_id] = str(kwargs.get("tag") or "")
            return alert_id

        def tagged(rows: list[Any]) -> list[Any]:
            out = []
            for row in rows:
                tag = self.tags.get(str(row.alert_id))
                if tag and row.oco_first is not None and row.oco_second is not None:
                    row = row.model_copy(
                        update={
                            "oco_first": row.oco_first.model_copy(update={"tag": tag}),
                            "oco_second": row.oco_second.model_copy(update={"tag": tag}),
                        }
                    )
                if self.strip_type:
                    row = row.model_copy(update={"alert_type": None})
                out.append(row)
            return out

        pending, triggered = client.pending_gtts, client.triggered_gtts
        client.place_oco = placed  # type: ignore[method-assign]
        client.pending_gtts = lambda *a, **k: tagged(pending(*a, **k))  # type: ignore[method-assign]
        client.triggered_gtts = lambda *a, **k: tagged(triggered(*a, **k))  # type: ignore[method-assign]
        return client


def _ledger(rig: Any) -> list[dict[str, Any]]:
    text = rig.ledger_path().read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def _kinds(rig: Any) -> list[str]:
    return [x["kind"] for x in _ledger(rig) if x["type"] == "intent"]


def _legs(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    return payload["place_order_params"], payload["place_order_params_leg2"]


def _triggers(payload: dict[str, Any]) -> dict[str, Decimal]:
    return {i["var_name"]: Decimal(i["d"]) for i in payload["oivariable"]}


def test_the_six_case_ids_are_registered() -> None:
    assert [c.id for c in cases_oco.OCO_CASES] == [
        "oco-far-buy",
        "oco-far-sell",
        "oco-far-sl-lmt",
        "oco-fires-buy",
        "oco-fires-sell",
        "oco-slots-reversed",
    ]
    assert {c.id for c in cases_oco.OCO_CASES if c.tier == "smoke"} == {"oco-far-buy"}
    assert all(c.orders == 1 and c.role == "nse-b" for c in cases_oco.OCO_CASES)


# -- oco-far-buy ----------------------------------------------------------------------------------


def test_the_far_buy_oco_is_placed_read_back_modified_once_cancelled_and_probed(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        result = cases_oco.run_case(rig.run, cases_oco.FAR_BUY)
        assert result.verdict == batch.PASS, result.reason
        [place] = rig.requests("PlaceOCOOrder")
        [modify, probe] = rig.requests("ModifyOCOOrder")
        [cancel] = rig.requests("CancelOCOOrder")
        assert _triggers(place) == {"x": Decimal("440.00"), "y": Decimal("360.00")}  # 1.10 / 0.90
        assert _triggers(modify)["x"] == Decimal("460.00")  # stop-loss modified to 1.15 x
        assert _triggers(modify)["y"] == Decimal("360.00")
        assert place["validity"] == "DAY" and modify["validity"] == "DAY"  # alert validity DAY
        stop, target = _legs(place)
        assert stop["trantype"] == "B" and target["trantype"] == "B" and stop["prd"] == "C"
        assert stop["ret"] == "DAY" and stop["qty"] == "1" and stop["prctyp"] == "LMT"
        assert Decimal(stop["prc"]) < Decimal("400.00") and Decimal(target["prc"]) < Decimal("400")
        assert stop["prc"] != target["prc"]  # distinct leg prices
        assert cancel["al_id"] == modify["al_id"] == probe["al_id"]
        assert rig.requests("PlaceOrder") == [] and rig.broker.orders == []
        assert rig.session.client.pending_gtts() == []
        assert _kinds(rig) == ["oco_place", "oco_modify", "oco_cancel", "oco_modify"]
        [record] = rig.records()
        assert record["case_id"] == "oco-far-buy" and record["verdict"] == batch.PASS
        assert [c["kind"] for c in record["calls"]] == [
            "oco_place",
            "oco_modify",
            "oco_cancel",
            "oco_modify",
        ]
        notes = record["notes"]
        assert notes["probe"]["exception"] == "OrderRejectedError"
        assert notes["triggered_cancelled"] == [cancel["al_id"]]
        assert [r["is_oco"] for r in notes["after_place"]] == [True]
        assert notes["after_place"][0]["stop_loss"]["trigger_value"] == "440.00"
        assert notes["after_place"][0]["target"]["trigger_value"] == "360.00"
        assert notes["after_place"][0]["leg_tags"] == [place["place_order_params"]["remarks"]] * 2
        assert notes["after_modify"][0]["stop_loss"]["trigger_value"] == "460.00"
        assert notes["plan"]["alert_validity"] == "DAY"
        verdicts = [x for x in _ledger(rig) if x["type"] == "verdict"]
        assert [(v["case_id"], v["verdict"]) for v in verdicts] == [("oco-far-buy", "pass")]


def test_every_oco_is_sent_with_alert_validity_day(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        cases_oco.run_case(rig.run, cases_oco.FAR_BUY)
        sent = [r for r in rig.broker.requests if r.endpoint in ("PlaceOCOOrder", "ModifyOCOOrder")]
        assert sent and all(r.payload["validity"] == "DAY" for r in sent)


# -- oco-far-sell and oco-far-sl-lmt --------------------------------------------------------------


def test_the_far_sell_oco_mirrors_the_buy(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        result = cases_oco.run_case(rig.run, cases_oco.FAR_SELL)
        assert result.verdict == batch.PASS, result.reason
        [place] = rig.requests("PlaceOCOOrder")
        [modify, _probe] = rig.requests("ModifyOCOOrder")
        assert _triggers(place) == {"x": Decimal("360.00"), "y": Decimal("440.00")}  # 0.90 / 1.10
        assert _triggers(modify)["x"] == Decimal("340.00")  # stop-loss modified to 0.85 x
        stop, target = _legs(place)
        assert stop["trantype"] == "S" and target["trantype"] == "S"
        assert Decimal(stop["prc"]) > Decimal("400.00") and Decimal(target["prc"]) > Decimal("400")
        assert stop["prc"] != target["prc"]
        assert rig.session.client.pending_gtts() == []


def test_the_far_sl_lmt_oco_has_a_stop_loss_leg_with_its_own_trigger_price(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        result = cases_oco.run_case(rig.run, cases_oco.FAR_SL_LMT)
        assert result.verdict == batch.PASS, result.reason
        [place] = rig.requests("PlaceOCOOrder")
        [modify, _probe] = rig.requests("ModifyOCOOrder")
        stop, target = _legs(place)
        assert stop["prctyp"] == "SL-LMT" and target["prctyp"] == "LMT"
        assert Decimal(stop["trgprc"]) <= Decimal(stop["prc"]) < Decimal("400.00")
        assert "trgprc" not in target
        assert _legs(modify)[0]["prctyp"] == "SL-LMT" and "trgprc" in _legs(modify)[0]
        assert rig.session.client.pending_gtts() == []


# -- refusals and unknown outcomes ----------------------------------------------------------------


def test_a_sell_oco_refused_by_rms_is_recorded_and_the_breaker_is_untouched(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        rig.broker.fail_next("PlaceOCOOrder", drills.reject("RED:RULE:{Holdings} no stock"))
        result = cases_oco.run_case(rig.run, cases_oco.FAR_SELL)
        assert result.verdict == batch.RECORD and "RED:RULE" in result.reason
        assert rig.session.guard.tripped("nse") is None
        assert rig.requests("ModifyOCOOrder") == [] and rig.requests("CancelOCOOrder") == []
        assert rig.records()[0]["verdict"] == batch.RECORD


def test_a_buy_oco_rejected_by_the_server_skips_with_the_reason(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        rig.broker.fail_next("PlaceOCOOrder", drills.reject("OCO not enabled"))
        result = cases_oco.run_case(rig.run, cases_oco.FAR_BUY)
        assert result.verdict == batch.SKIP and "OCO not enabled" in result.reason
        assert rig.session.guard.tripped("nse") is None


def test_an_ambiguous_oco_place_is_found_by_leg_tag_cancelled_once_and_never_resent(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        rig.broker.fail_next("PlaceOCOOrder", drills.timeout_after_send())
        result = cases_oco.run_case(rig.run, cases_oco.FAR_BUY)
        assert result.verdict == batch.PASS, result.reason
        assert len(rig.requests("PlaceOCOOrder")) == 1
        assert len(rig.requests("CancelOCOOrder")) == 1
        record = rig.records()[0]
        assert record["calls"][0]["verdict"] == calls.FOUND
        assert rig.session.client.pending_gtts() == []


def test_an_oco_place_that_cannot_be_found_is_check_the_account(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        rig.broker.fail_next("PlaceOCOOrder", drills.connect_error())
        result = cases_oco.run_case(rig.run, cases_oco.FAR_BUY)
        assert result.verdict == batch.CHECK and CHECK_THE_ACCOUNT in result.reason
        assert rig.requests("ModifyOCOOrder") == [] and rig.requests("CancelOCOOrder") == []
        assert rig.session.guard.tripped("nse") is not None


def test_an_oco_whose_cancel_is_rejected_is_check_the_account_and_never_cancelled_twice(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        rig.broker.fail_next("CancelOCOOrder", drills.reject("Invalid Oi"))
        result = cases_oco.run_case(rig.run, cases_oco.FAR_BUY)
        assert result.verdict == batch.CHECK and "still pending" in result.reason
        [pending] = rig.session.client.pending_gtts()
        report = calls.teardown(rig.session)
        assert report is not None and report.gtts is not None
        assert report.gtts.left == [pending.alert_id]
        assert len(rig.requests("CancelOCOOrder")) == 1


def test_a_case_done_in_the_run_is_not_placed_again(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        assert cases_oco.run_case(rig.run, cases_oco.FAR_BUY).verdict == batch.PASS
        again = cases_oco.run_case(rig.run, cases_oco.FAR_BUY)
        assert again.verdict == batch.SKIP and "done in run" in again.reason
        assert len(rig.requests("PlaceOCOOrder")) == 1


def test_ctrl_c_during_the_modify_sends_nothing_more_and_teardown_lists_the_oco(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        client = rig.session.client

        def interrupted(alert_id: str, **kwargs: Any) -> str:
            raise KeyboardInterrupt

        client.modify_oco = interrupted  # type: ignore[method-assign]
        with pytest.raises(KeyboardInterrupt):
            cases_oco.run_case(rig.run, cases_oco.FAR_BUY)
        assert rig.session.interrupted
        assert calls.teardown(rig.session) is None
        assert rig.requests("CancelOCOOrder") == []
        [pending] = client.pending_gtts()
        assert _ledger(rig)[-1]["alerts"] == [pending.alert_id]


def test_an_oco_row_of_our_id_without_a_type_still_pending_is_check_the_account(
    tmp_path: Path,
) -> None:
    """An id match is never type-filtered: a row with our alert id and no ``ai_t`` that the
    cancel did not remove is still pending."""
    with family_rig(tmp_path, market_type=OcoMarket, strip_type=True) as rig:
        rig.broker.fail_next("CancelOCOOrder", drills.reject("Invalid Oi"))
        result = cases_oco.run_case(rig.run, cases_oco.FAR_BUY)
        assert result.verdict == batch.CHECK and "still pending" in result.reason
        assert rig.session.guard.tripped("nse") is not None


def _second_modify(rig: Any, replace: Any) -> None:
    """Route the client's second ``modify_oco`` (the probe) through ``replace(real, id, kw)``."""
    client = rig.session.client
    real = client.modify_oco
    count = {"n": 0}

    def modify(alert_id: str, **kwargs: Any) -> str:
        count["n"] += 1
        if count["n"] == 2:
            return str(replace(real, alert_id, kwargs))
        return str(real(alert_id, **kwargs))

    client.modify_oco = modify  # type: ignore[method-assign]


def test_a_probe_modify_the_server_accepts_is_check_the_account_with_no_second_cancel(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        _second_modify(rig, lambda real, alert_id, kw: alert_id)  # "accepted"
        result = cases_oco.run_case(rig.run, cases_oco.FAR_BUY)
        assert result.verdict == batch.CHECK and CHECK_THE_ACCOUNT in result.reason
        assert rig.session.guard.tripped("nse") is not None
        assert len(rig.requests("CancelOCOOrder")) == 1


def test_a_probe_modify_with_an_unknown_outcome_is_check_the_account(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:

        def timeout_after_send(real: Any, alert_id: str, kw: dict[str, Any]) -> str:
            rig.broker.fail_next("ModifyOCOOrder", drills.timeout_after_send())
            return str(real(alert_id, **kw))

        _second_modify(rig, timeout_after_send)
        result = cases_oco.run_case(rig.run, cases_oco.FAR_BUY)
        assert result.verdict == batch.CHECK and CHECK_THE_ACCOUNT in result.reason
        assert len(rig.requests("CancelOCOOrder")) == 1


# -- mock session ---------------------------------------------------------------------------------


def test_a_mock_session_skips_every_oco_case_and_sends_nothing(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        rig.session.mock = True
        for case in cases_oco.OCO_CASES:
            assert cases_oco.run_case(rig.run, case).verdict == batch.SKIP, case.id
        assert rig.requests("PlaceOCOOrder") == []


# -- the fires cases (scripted: the offline broker never fires an OCO) ---------------------------

LEG_KEYS = {"x": "place_order_params", "y": "place_order_params_leg2"}


@dataclass
class FiringOcoMarket(OcoMarket):
    """``OcoMarket`` whose OCOs fire right after their place, as scripted: ``fire`` lists the
    slots (``x``: ``place_order_params``, ``y``: ``place_order_params_leg2``) whose orders go
    out (empty: it never fires). Each order is booked from its leg (``add_ord_id`` = the alert
    id, the leg's ``remarks``), the OCO leaves pending for the triggered list, and the order is
    then accepted (``then="open"``), accepted and filled (``"fill"``) or rejected with
    ``reject``. Both the SDK path (``place_oco``) and ``request("PlaceOCOOrder", ...)`` fire."""

    fire: tuple[str, ...] = ("y",)
    then: str = "open"
    reject: str = "RED:RULE:{Holdings} insufficient holdings"
    fired: list[str] = field(default_factory=list)

    def install(self, client: TradeSmart) -> TradeSmart:
        client = super().install(client)
        place, request = client.place_oco, client.request

        def placed(**kwargs: Any) -> str:
            alert_id = place(**kwargs)
            self._fire(alert_id)
            return alert_id

        def requested(endpoint: str, payload: Any, **kwargs: Any) -> Any:
            reply = request(endpoint, payload, **kwargs)
            if endpoint == "PlaceOCOOrder":
                alert_id = str(reply["al_id"])
                self.tags[alert_id] = str(payload["place_order_params"].get("remarks") or "")
                self._fire(alert_id)
            return reply

        client.place_oco = placed  # type: ignore[method-assign]
        client.request = requested  # type: ignore[method-assign]
        return client

    def _fire(self, alert_id: str) -> None:
        if not self.fire:
            return
        state = self.broker._state  # scripted: OCO firing is not simulated by the broker
        ids = []
        with state.lock:
            oco = next(g for g in state.gtts if g["al_id"] == alert_id)
            for slot in self.fire:
                leg = oco[LEG_KEYS[slot]]
                payload = {"exch": oco["exch"], "tsym": oco["tsym"], **leg}
                payload.update({"uid": "UAT01", "actid": "UAT01", "add_ord_id": alert_id})
                ids.append(state._book(payload).id)
            state.gtts.remove(oco)
            state.triggered_gtts.append(dict(oco))
        state._drain()
        for order_id in ids:
            if self.then == "reject":
                self.broker.reject(order_id, self.reject)
                continue
            self.broker.accept(order_id)
            if self.then == "fill":
                [order] = [o for o in self.broker.orders if o.id == order_id]
                self.broker.fill(order_id, order.price)
        self.fired.extend(ids)


def _market(rig: Any) -> FiringOcoMarket:
    assert isinstance(rig.market, FiringOcoMarket)
    return rig.market


def test_the_fires_buy_oco_fires_one_leg_whose_order_is_cancelled_once_and_final(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket) as rig:
        result = cases_oco.run_case(rig.run, cases_oco.FIRES_BUY)
        assert result.verdict == batch.PASS, result.reason
        [place] = rig.requests("PlaceOCOOrder")
        assert _triggers(place) == {"x": Decimal("440.00"), "y": Decimal("408.00")}  # 1.10 / 1.02
        assert place["validity"] == "DAY"
        stop, target = _legs(place)
        assert stop["trantype"] == target["trantype"] == "B"
        assert Decimal(stop["prc"]) < Decimal("400.00") and Decimal(target["prc"]) < Decimal("400")
        assert stop["prc"] != target["prc"]
        [order] = rig.broker.orders
        assert order.status == "CANCELED" and rig.market.cancels == [order.id]
        assert result.order_ids == [order.id]
        assert rig.requests("PlaceOrder") == [] and rig.requests("CancelOCOOrder") == []
        assert _kinds(rig) == ["oco_place", "cancel"]
        adopt = [x for x in _ledger(rig) if x["type"] == "adopt"]
        assert [a["order_id"] for a in adopt] == [order.id]
        assert rig.session.client.pending_gtts() == []
        assert rig.session.guard.tripped("nse") is None
        notes = rig.records()[0]["notes"]
        assert notes["fired_leg"] == ["target"]  # one entry per fired order row
        assert [o["norenordno"] for o in notes["orders"]] == [order.id]
        assert [r["alert_id"] for r in notes["triggered"]] == [place_alert(rig)]


def place_alert(rig: Any) -> str:
    return str(
        next(
            x["alert_id"]
            for x in _ledger(rig)
            if x.get("kind") == "oco_place" and x["type"] == "outcome"
        )
    )


def test_a_far_oco_that_fires_is_check_the_account_with_a_list_of_fired_legs(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket, fire=("x",)) as rig:
        result = cases_oco.run_case(rig.run, cases_oco.FAR_BUY)
        assert result.verdict == batch.CHECK and "fired" in result.reason
        [order] = rig.broker.orders
        assert order.status == "CANCELED" and rig.market.cancels == [order.id]
        assert rig.records()[0]["notes"]["fired_leg"] == ["stop_loss"]  # a list, never overwritten


def test_a_fired_stop_loss_leg_is_recorded_as_the_stop_loss(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket, fire=("x",)) as rig:
        result = cases_oco.run_case(rig.run, cases_oco.FIRES_BUY)
        assert result.verdict == batch.PASS, result.reason
        assert rig.records()[0]["notes"]["fired_leg"] == ["stop_loss"]


def test_both_legs_orders_out_is_check_the_account_and_each_is_cancelled_once(
    tmp_path: Path,
) -> None:
    """Review Focus 5: the server sends both legs' orders: never a pass."""
    with family_rig(tmp_path, market_type=FiringOcoMarket, fire=("x", "y")) as rig:
        result = cases_oco.run_case(rig.run, cases_oco.FIRES_BUY)
        assert result.verdict == batch.CHECK and CHECK_THE_ACCOUNT in result.reason
        assert "both" in result.reason
        orders = rig.broker.orders
        assert len(orders) == 2 and all(o.status == "CANCELED" for o in orders)
        assert sorted(rig.market.cancels) == sorted(o.id for o in orders)  # once each
        assert sorted(result.order_ids) == sorted(o.id for o in orders)
        assert sorted(rig.records()[0]["notes"]["fired_leg"]) == ["stop_loss", "target"]
        assert rig.session.guard.tripped("nse") is not None


def test_a_fired_order_that_stays_open_after_its_cancel_is_stuck(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket, stuck={"ITC-EQ"}) as rig:
        result = cases_oco.run_case(rig.run, cases_oco.FIRES_BUY)
        assert result.verdict == batch.STUCK
        assert rig.session.guard.tripped("nse") is not None
        assert len(rig.market.cancels) == 1
        report = calls.teardown(rig.session)
        assert report is not None and report.stuck == result.order_ids
        assert len(rig.market.cancels) == 1  # never a second CancelOrder


def test_a_fired_order_that_fills_on_uat_goes_to_the_position_ledger_and_is_recorded(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket, then="fill", uat=True) as rig:
        result = cases_oco.run_case(rig.run, cases_oco.FIRES_BUY)
        assert result.verdict == batch.RECORD and batch.UAT_FILLED_FAR in result.reason
        assert rig.run.positions.net("ITC-EQ") == 1
        assert rig.market.cancels == []  # final: nothing to cancel
        notes = rig.records()[0]["notes"]
        assert notes["fired_leg"] == ["target"] and batch.UAT_FILLED_FAR in notes


def test_a_fired_order_that_fills_off_uat_is_check_the_account(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket, then="fill") as rig:
        result = cases_oco.run_case(rig.run, cases_oco.FIRES_BUY)
        assert result.verdict == batch.CHECK and "filled" in result.reason


def test_an_oco_that_does_not_fire_is_cancelled_once_looked_at_once_and_recorded(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket, fire=()) as rig:
        result = cases_oco.run_case(rig.run, cases_oco.FIRES_BUY)
        assert result.verdict == batch.RECORD and "did not fire" in result.reason
        assert len(rig.requests("CancelOCOOrder")) == 1
        assert rig.session.client.pending_gtts() == [] and rig.broker.orders == []
        assert rig.session.guard.tripped("nse") is None
        assert _kinds(rig) == ["oco_place", "oco_cancel"]


def test_an_oco_that_does_not_fire_and_cannot_be_cancelled_is_check_the_account(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket, fire=()) as rig:
        rig.broker.fail_next("CancelOCOOrder", drills.reject("Invalid Oi"))
        result = cases_oco.run_case(rig.run, cases_oco.FIRES_BUY)
        assert result.verdict == batch.CHECK and "did not fire" in result.reason
        assert "still pending" in result.reason
        assert len(rig.requests("CancelOCOOrder")) == 1
        assert rig.session.guard.tripped("nse") is not None


def test_the_fires_sell_oco_mirrors_the_buy_and_passes_with_holdings(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket) as rig:
        result = cases_oco.run_case(rig.run, cases_oco.FIRES_SELL)
        assert result.verdict == batch.PASS, result.reason
        [place] = rig.requests("PlaceOCOOrder")
        assert _triggers(place) == {"x": Decimal("360.00"), "y": Decimal("392.00")}  # 0.90 / 0.98
        stop, target = _legs(place)
        assert stop["trantype"] == target["trantype"] == "S"
        assert Decimal(stop["prc"]) > Decimal("400.00") and Decimal(target["prc"]) > Decimal("400")
        [order] = rig.broker.orders
        assert order.status == "CANCELED" and rig.market.cancels == [order.id]
        assert rig.records()[0]["notes"]["fired_leg"] == ["target"]


def test_the_fires_sell_oco_without_holdings_is_recorded_not_passed(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket) as rig:
        rig.broker.set_holdings([])
        result = cases_oco.run_case(rig.run, cases_oco.FIRES_SELL)
        assert result.verdict == batch.RECORD and "holdings" in result.reason
        [order] = rig.broker.orders
        assert order.status == "CANCELED" and rig.market.cancels == [order.id]
        assert rig.session.guard.tripped("nse") is None


def test_a_fired_sell_order_refused_by_rms_is_recorded_and_neutral_to_the_breaker(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket, then="reject") as rig:
        result = cases_oco.run_case(rig.run, cases_oco.FIRES_SELL)
        assert result.verdict == batch.RECORD and "RED:RULE" in result.reason
        assert rig.market.cancels == []  # rejected: final, nothing to cancel
        assert rig.session.guard.tripped("nse") is None
        assert rig.records()[0]["notes"]["fired_leg"] == ["target"]


def test_the_fires_case_never_resends_an_ambiguous_place(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket, fire=()) as rig:
        rig.broker.fail_next("PlaceOCOOrder", drills.connect_error())
        result = cases_oco.run_case(rig.run, cases_oco.FIRES_BUY)
        assert result.verdict == batch.CHECK and CHECK_THE_ACCOUNT in result.reason
        assert _kinds(rig) == ["oco_place"]  # one intent, one call, never resent
        assert rig.requests("CancelOCOOrder") == []


def test_the_stop_file_before_the_fires_place_skips_without_sending(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket) as rig:
        (rig.root / "STOP").write_text("stop", encoding="utf-8")
        result = cases_oco.run_case(rig.run, cases_oco.FIRES_BUY)
        assert result.verdict == batch.SKIP
        assert rig.requests("PlaceOCOOrder") == []


# -- oco-slots-reversed (Python only) -------------------------------------------------------------


def test_the_slots_reversed_case_sends_the_docs_slot_order_and_records_the_fired_leg(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket, fire=("x",)) as rig:
        result = cases_oco.run_case(rig.run, cases_oco.SLOTS_REVERSED)
        assert result.verdict == batch.RECORD, result.reason
        [place] = rig.requests("PlaceOCOOrder")
        assert _triggers(place) == {"x": Decimal("408.00"), "y": Decimal("440.00")}  # target in x
        first, second = _legs(place)
        assert Decimal(first["prc"]) == Decimal(second["prc"]) + Decimal("0.05")  # target first
        assert place["ai_t"] == "LMT_BOS_O" and place["validity"] == "DAY"
        assert first["remarks"] == second["remarks"] != ""  # the case tag on both legs
        [order] = rig.broker.orders
        assert order.status == "CANCELED" and rig.market.cancels == [order.id]
        assert _kinds(rig) == ["oco_place", "cancel"]
        assert rig.session.guard.tripped("nse") is None
        record = rig.records()[0]
        notes = record["notes"]
        assert notes["fired_leg"] == ["target"]  # x carries the target here
        assert notes["slot_order"] == {"x": "target", "y": "stop_loss"}
        assert record["calls"][0]["kind"] == "oco_place"
        verdicts = [x for x in _ledger(rig) if x["type"] == "verdict"]
        assert [(v["case_id"], v["verdict"]) for v in verdicts] == [
            ("oco-slots-reversed", "record")
        ]


def test_the_slots_reversed_case_is_record_when_both_legs_fire_and_are_cleaned_up(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket, fire=("x", "y")) as rig:
        result = cases_oco.run_case(rig.run, cases_oco.SLOTS_REVERSED)
        assert result.verdict == batch.RECORD and "both" in result.reason
        assert all(o.status == "CANCELED" for o in rig.broker.orders)
        assert sorted(rig.records()[0]["notes"]["fired_leg"]) == ["stop_loss", "target"]
        assert rig.session.guard.tripped("nse") is None


def test_the_slots_reversed_case_is_record_when_the_server_rejects_or_it_does_not_fire(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket) as rig:
        rig.broker.fail_next("PlaceOCOOrder", drills.reject("Invalid trigger"))
        result = cases_oco.run_case(rig.run, cases_oco.SLOTS_REVERSED)
        assert result.verdict == batch.RECORD and "Invalid trigger" in result.reason
        assert rig.session.guard.tripped("nse") is None
    with family_rig(tmp_path / "b", market_type=FiringOcoMarket, fire=()) as rig:
        result = cases_oco.run_case(rig.run, cases_oco.SLOTS_REVERSED)
        assert result.verdict == batch.RECORD and "did not fire" in result.reason
        assert len(rig.requests("CancelOCOOrder")) == 1
        assert rig.session.guard.tripped("nse") is None


def test_the_slots_reversed_case_is_check_the_account_when_cleanup_fails(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket, stuck={"ITC-EQ"}) as rig:
        result = cases_oco.run_case(rig.run, cases_oco.SLOTS_REVERSED)
        assert result.verdict == batch.CHECK and CHECK_THE_ACCOUNT in result.reason
        assert len(rig.market.cancels) == 1


def test_the_slots_reversed_case_is_skipped_for_a_driver_sdk(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket) as rig:
        rig.session.sdk.name = "javascript"  # type: ignore[misc]
        result = cases_oco.run_case(rig.run, cases_oco.SLOTS_REVERSED)
        assert result.verdict == batch.SKIP and "Python" in result.reason
        assert rig.requests("PlaceOCOOrder") == []


def test_oco_place_raw_refuses_a_driver_sdk_and_a_gtt_validity_before_spending(
    tmp_path: Path,
) -> None:
    from tradesmart import OcoLeg

    with family_rig(tmp_path, market_type=FiringOcoMarket) as rig:
        session = rig.session
        ref = calls.CaseRef(cases_oco.SLOTS_REVERSED.id, "nse")
        session.guard.reserve(ref.case_id, 1)
        legs = {
            "stop_loss": OcoLeg(trigger_value=Decimal("440.00"), price=Decimal("360.00")),
            "target": OcoLeg(trigger_value=Decimal("408.00"), price=Decimal("360.05")),
            "transaction_type": "BUY",
            "exchange": "NSE",
            "tradingsymbol": "ITC-EQ",
            "quantity": 1,
            "product": "CNC",
            "validity": "DAY",
        }
        with pytest.raises(ValueError, match="DAY"):
            calls.oco_place_raw(session, ref, alert_validity="GTT", **legs)
        session.sdk.name = "javascript"  # type: ignore[misc]
        with pytest.raises(ValueError, match="Python"):
            calls.oco_place_raw(session, ref, alert_validity="DAY", **legs)
        assert rig.requests("PlaceOCOOrder") == [] and not rig.ledger_path().exists()
        assert session.guard.used.get(ref.case_id, 0) == 0


# -- fix round 1: exceptions, a refused cancel, mock sessions -------------------------------------


@pytest.mark.parametrize("case", [cases_oco.FIRES_BUY, cases_oco.FIRES_SELL], ids=lambda c: c.id)
def test_an_exception_after_a_confirmed_fires_place_is_check_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: Any
) -> None:
    def broken(work: Any, alert_id: str) -> list[Any]:
        raise RuntimeError("book read blew up")

    with family_rig(tmp_path, market_type=FiringOcoMarket, fire=()) as rig:
        monkeypatch.setattr(cases_oco, "_wait_for_orders", broken)
        result = cases_oco.run_case(rig.run, case)
        assert result.verdict == batch.CHECK and CHECK_THE_ACCOUNT in result.reason
        assert "RuntimeError" in result.reason
        assert rig.session.guard.tripped("nse") is not None
        assert len(rig.requests("PlaceOCOOrder")) == 1


def test_a_refused_cancel_of_the_fired_order_is_check_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(session: Any, order_id: str, case: Any) -> Any:
        raise Refusal(f"order {order_id}: not cancelled (refused)")

    with family_rig(tmp_path, market_type=FiringOcoMarket) as rig:
        monkeypatch.setattr(calls, "cancel", refused)
        result = cases_oco.run_case(rig.run, cases_oco.FIRES_BUY)
        assert result.verdict == batch.CHECK and CHECK_THE_ACCOUNT in result.reason
        assert "refused" in result.reason
        assert rig.session.guard.tripped("nse") is not None
        assert "order_cancel_refused" in rig.records()[0]["notes"]


@pytest.mark.parametrize(
    "case",
    [cases_oco.FIRES_BUY, cases_oco.FIRES_SELL, cases_oco.SLOTS_REVERSED],
    ids=lambda c: c.id,
)
def test_a_mock_session_refuses_the_fires_and_slots_cases_even_with_the_oco_switch_on(
    tmp_path: Path, case: Any
) -> None:
    """Owner decision E5: no OCO is ever made to fire in a mock session, whatever the switch."""
    with family_rig(tmp_path, market_type=FiringOcoMarket) as rig:
        rig.session.mock = True
        rig.session.mock_oco = True
        runner = (
            cases_oco.run_slots_reversed
            if case is cases_oco.SLOTS_REVERSED
            else (cases_oco.run_fires)
        )
        for result in (runner(rig.run, case), cases_oco.run_case(rig.run, case)):
            assert result.verdict == batch.SKIP and "mock" in result.reason
        assert rig.requests("PlaceOCOOrder") == []
        assert rig.session.guard.used.get(case.id, 0) == 0


def test_oco_place_raw_refuses_a_mock_session_even_with_the_oco_switch_on(tmp_path: Path) -> None:
    from tradesmart import OcoLeg

    with family_rig(tmp_path, market_type=FiringOcoMarket) as rig:
        session = rig.session
        session.mock = True
        session.mock_oco = True
        ref = calls.CaseRef(cases_oco.SLOTS_REVERSED.id, "nse")
        session.guard.reserve(ref.case_id, 1)
        with pytest.raises(Refusal, match="mock"):
            calls.oco_place_raw(
                session,
                ref,
                stop_loss=OcoLeg(trigger_value=Decimal("440.00"), price=Decimal("360.00")),
                target=OcoLeg(trigger_value=Decimal("408.00"), price=Decimal("360.05")),
                transaction_type="BUY",
                exchange="NSE",
                tradingsymbol="ITC-EQ",
                quantity=1,
                product="CNC",
                validity="DAY",
                alert_validity="DAY",
            )
        assert rig.requests("PlaceOCOOrder") == [] and not rig.ledger_path().exists()
        assert session.guard.used.get(ref.case_id, 0) == 0


# -- Task 6: the owner's OCO switch in a mock session (design 2026-11-20 E3-E5) -------------------

SATURDAY = date(2026, 11, 14)
SUNDAY = date(2026, 11, 15)


def _mock_on(rig: Any, monkeypatch: pytest.MonkeyPatch, *, oco: bool = True) -> None:
    """The rig's session in mock mode for Saturday 2026-11-14, every mock check real: the offline
    broker's host stands in for the production host (``mock_session.PRODUCTION_HOST``)."""
    session = rig.session
    host = safety._rest_host(session.client.config.base_url)
    assert host
    monkeypatch.setattr(mock_session, "PRODUCTION_HOST", host)
    session.uat = False
    session.mock = True
    session.mock_date = SATURDAY
    session.today = lambda: SATURDAY
    session.mock_environ = mock_session.snapshot(
        {
            mock_session.SESSION_ENV: "2026-11-14",
            mock_session.CONFIRM_ENV: "mock session 2026-11-14",
        }
    )
    session.mock_oco = oco
    assert session.mock_refusal() is None


def _oco_fields() -> dict[str, Any]:
    return {
        "exchange": "NSE",
        "tradingsymbol": "ITC-EQ",
        "quantity": 1,
        "product": "CNC",
        "validity": "DAY",
        "alert_validity": "DAY",
    }


def _place_ours(rig: Any) -> calls.OcoOutcome:
    """One far BUY OCO of the run through the never-resend wrapper (left pending)."""
    from tradesmart import OcoLeg

    ref = calls.CaseRef(cases_oco.FAR_BUY.id, "nse")
    rig.session.guard.reserve(ref.case_id, 1)
    placed = calls.oco_place(
        rig.session,
        ref,
        stop_loss=OcoLeg(trigger_value=Decimal("440.00"), price=Decimal("360.00")),
        target=OcoLeg(trigger_value=Decimal("360.00"), price=Decimal("359.00")),
        transaction_type="BUY",
        **_oco_fields(),
    )
    assert placed.alert_id
    return placed


def _place_foreign(rig: Any) -> tuple[str, str]:
    """An OCO and an open order of the account that the run did not place (another tool)."""
    from tradesmart import OcoLeg

    client = rig.session.client
    alert_id = client.place_oco(
        stop_loss=OcoLeg(trigger_value=Decimal("441.00"), price=Decimal("350.00")),
        target=OcoLeg(trigger_value=Decimal("359.00"), price=Decimal("349.00")),
        transaction_type="BUY",
        tag="someone-else",
        **_oco_fields(),
    )
    order_id = client.place_order(
        exchange="NSE",
        tradingsymbol="ITC-EQ",
        transaction_type="BUY",
        quantity=1,
        product="CNC",
        order_type="LMT",
        price=Decimal("390.00"),  # inside the band, below the market: it rests open
        validity="DAY",
        tag="someone-else",
    )
    return str(alert_id), str(order_id)


def _pending_ids(rig: Any) -> set[str]:
    return {str(g.alert_id) for g in rig.session.client.pending_gtts()}


def _status(rig: Any, order_id: str) -> str:
    return str(next(o.status for o in rig.broker.orders if o.id == order_id))


def test_with_the_oco_switch_a_mock_session_runs_the_three_far_cases_all_day_validity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        _mock_on(rig, monkeypatch)
        for case in (cases_oco.FAR_BUY, cases_oco.FAR_SELL, cases_oco.FAR_SL_LMT):
            result = cases_oco.run_case(rig.run, case)
            assert result.verdict == batch.PASS, (case.id, result.reason)
        sent = [r for r in rig.broker.requests if r.endpoint in ("PlaceOCOOrder", "ModifyOCOOrder")]
        assert len(rig.requests("PlaceOCOOrder")) == 3
        assert all(r.payload["validity"] == "DAY" for r in sent)  # every OCO's alert validity
        assert len(rig.requests("CancelOCOOrder")) == 3
        assert rig.session.client.pending_gtts() == []


def test_in_a_mock_session_the_far_case_sends_no_probe_on_the_production_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production keeps the far OCO to its three writes (place, modify, cancel), as the live OCO
    lifecycle test does there; the ``Invalid Oi`` probe runs on UAT only."""
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        _mock_on(rig, monkeypatch)
        result = cases_oco.run_case(rig.run, cases_oco.FAR_BUY)
        assert result.verdict == batch.PASS, result.reason
        assert len(rig.requests("ModifyOCOOrder")) == 1
        assert _kinds(rig) == ["oco_place", "oco_modify", "oco_cancel"]
        assert "mock" in rig.records()[0]["notes"]["probe"]["not_sent"]


def test_a_mock_far_case_after_midnight_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        _mock_on(rig, monkeypatch)
        rig.session.today = lambda: SUNDAY  # the run crossed midnight IST: the switch is stale
        result = cases_oco.run_case(rig.run, cases_oco.FAR_BUY)
        assert result.verdict == batch.SKIP and "today" in result.reason
        assert rig.requests("PlaceOCOOrder") == []


def test_a_mock_session_without_the_oco_switch_skips_with_the_switch_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        _mock_on(rig, monkeypatch, oco=False)
        for case in cases_oco.OCO_CASES:
            result = cases_oco.run_case(rig.run, case)
            assert result.verdict == batch.SKIP, case.id
        assert rig.requests("PlaceOCOOrder") == []
        assert mock_session.OCO_ENV in cases_oco.run_case(rig.run, cases_oco.FAR_BUY).reason


# -- the mock clean-up (E4) ------------------------------------------------------------------------


def test_the_mock_cleanup_cancels_only_the_runs_oco_and_confirms_it_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        _mock_on(rig, monkeypatch)
        ours = _place_ours(rig).alert_id
        foreign_oco, foreign_order = _place_foreign(rig)
        result = cases_oco.mock_cleanup(rig.run)
        assert result.verdict == batch.PASS, result.reason
        [cancel] = rig.requests("CancelOCOOrder")
        assert cancel["al_id"] == ours
        assert _pending_ids(rig) == {foreign_oco}  # the unrelated OCO is untouched
        assert rig.requests("CancelOrder") == [] and _status(rig, foreign_order) == "OPEN"
        assert rig.requests("CancelGTTOrder") == []  # never a GTT call
        record = rig.records()[-1]
        assert record["case_id"] == cases_oco.MOCK_CLEANUP.id
        assert record["notes"]["ocos_cancelled"] == [ours]


def test_the_mock_cleanup_cancels_an_order_the_runs_oco_placed_and_confirms_it_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket, fire=("x",)) as rig:
        _mock_on(rig, monkeypatch)
        _place_ours(rig)  # fired at once (scripted): its stop-loss leg's order is open
        [fired] = _market(rig).fired
        _market(rig).fire = ()  # the foreign OCO below never fires
        foreign_oco, foreign_order = _place_foreign(rig)
        result = cases_oco.mock_cleanup(rig.run)
        assert result.verdict == batch.PASS, result.reason
        assert rig.market.cancels == [fired]  # one cancel, ours only
        assert _status(rig, fired) == "CANCELED"
        assert _status(rig, foreign_order) == "OPEN"
        assert _pending_ids(rig) == {foreign_oco} and rig.requests("CancelOCOOrder") == []
        assert result.order_ids == [fired]


def test_a_runs_oco_still_pending_after_the_mock_cleanup_is_check_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        _mock_on(rig, monkeypatch)
        ours = _place_ours(rig).alert_id
        rig.session.client.cancel_oco = lambda alert_id: alert_id  # type: ignore[method-assign]
        result = cases_oco.mock_cleanup(rig.run)
        assert result.verdict == batch.CHECK and CHECK_THE_ACCOUNT in result.reason
        assert ours is not None and ours in result.reason
        assert ours in _pending_ids(rig)
        assert rig.session.guard.tripped("nse") is not None


def test_a_fired_order_still_open_after_the_mock_cleanup_is_check_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with family_rig(tmp_path, market_type=FiringOcoMarket, fire=("x",)) as rig:
        _mock_on(rig, monkeypatch)
        _place_ours(rig)
        [fired] = _market(rig).fired
        rig.session.client.cancel_order = lambda order_id, **_: order_id  # type: ignore[method-assign]
        result = cases_oco.mock_cleanup(rig.run)
        assert result.verdict == batch.CHECK and CHECK_THE_ACCOUNT in result.reason
        assert fired in result.reason and _status(rig, fired) == "OPEN"


def test_an_unreadable_pending_list_at_the_mock_cleanup_is_check_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        _mock_on(rig, monkeypatch)
        _place_ours(rig)

        def unreadable(*args: Any, **kwargs: Any) -> Any:
            raise TimeoutError("read timed out")

        rig.session.client.pending_gtts = unreadable  # type: ignore[method-assign]
        result = cases_oco.mock_cleanup(rig.run)
        assert result.verdict == batch.CHECK and CHECK_THE_ACCOUNT in result.reason
        assert rig.requests("CancelOCOOrder") == []


def test_the_mock_cleanup_without_an_oco_of_the_run_reads_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        _mock_on(rig, monkeypatch, oco=False)
        _place_foreign(rig)
        before = len(rig.broker.requests)
        result = cases_oco.mock_cleanup(rig.run)
        assert result.verdict == batch.SKIP
        assert len(rig.broker.requests) == before  # not even the GTT list is read


def test_the_mock_cleanup_outside_a_mock_session_sends_nothing(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        before = len(rig.broker.requests)
        result = cases_oco.mock_cleanup(rig.run)
        assert result.verdict == batch.SKIP and "teardown" in result.reason
        assert len(rig.broker.requests) == before


def test_the_mock_cleanup_after_ctrl_c_sends_nothing_and_is_check_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        _mock_on(rig, monkeypatch)
        ours = _place_ours(rig).alert_id
        rig.session.interrupted = True
        before = len(rig.broker.requests)
        result = cases_oco.mock_cleanup(rig.run)
        assert result.verdict == batch.CHECK and CHECK_THE_ACCOUNT in result.reason
        assert len(rig.broker.requests) == before
        assert ours in _pending_ids(rig)


def test_the_mock_teardown_cancels_only_the_runs_oco(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket) as rig:
        _mock_on(rig, monkeypatch)
        ours = _place_ours(rig).alert_id
        foreign_oco, foreign_order = _place_foreign(rig)
        report = calls.teardown(rig.session)
        assert report is not None and report.gtts is not None
        assert report.gtts.cancelled == [ours] and report.gtts.left == []
        assert foreign_oco in report.gtts.untouched
        assert [r["al_id"] for r in rig.requests("CancelOCOOrder")] == [ours]
        assert rig.requests("CancelOrder") == [] and _status(rig, foreign_order) == "OPEN"
        assert _pending_ids(rig) == {foreign_oco}
