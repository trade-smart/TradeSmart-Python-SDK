"""Dry run of the UAT matrix family M13 (GTT) on the offline broker.

The real family code (``cases_gtt``: the spec §5 far GTT, the GTT that fires at once, the
read-only lists; the never-resend GTT wrappers of ``calls``; the ledger) runs end to end on
``UatLikeMarket``. The offline broker never fires a GTT on its own: ``GttMarket`` below wraps the
client's ``place_gtt`` (as ``UatLikeMarket.install`` wraps ``place_order``) and fires a GTT whose
condition is already true when it is placed, through the broker's ``trigger_gtt``, as UAT does
(owner, O2); its order is then decided like any other (a far BUY rests ``OPEN``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.integration.uat_family_rig import family_rig
from tests.integration.uat_like import UatLikeMarket
from tests.live_orders.safety import CHECK_THE_ACCOUNT
from tests.live_orders.uat_matrix import batch, calls, cases_gtt
from tests.live_orders.uat_matrix.guard import STOP_REASON
from tradesmart import TradeSmart
from tradesmart.enums import GttAlertType
from tradesmart.models.gtt import TriggeredGtt
from tradesmart.models.orders import Order
from tradesmart.testing import drills


@dataclass
class GttMarket(UatLikeMarket):
    """``UatLikeMarket`` plus GTTs: ``fire`` = ``"when-true"`` fires a GTT whose condition holds
    at placement (UAT), ``"never"`` fires none, ``"always"`` fires every one."""

    fire: str = "when-true"
    fired: list[str] | None = None
    uat_remarks: bool = False  # the fired order's remarks in the UAT form (live, 13 Nov 2026)

    def install(self, client: TradeSmart) -> TradeSmart:
        client = super().install(client)
        place_gtt: Callable[..., str] = client.place_gtt
        self.fired = []

        def placed_gtt(**kwargs: Any) -> str:
            alert_id = place_gtt(**kwargs)
            if self.fire == "always" or (self.fire == "when-true" and self._true_now(kwargs)):
                order_id = self.broker.trigger_gtt(alert_id)
                if self.uat_remarks:  # ``<ai_t>: <tag>: <text>: <tag>``
                    tag = str(kwargs.get("tag") or "")
                    [order] = [o for o in self.broker.orders if o.id == order_id]
                    order.payload["remarks"] = (
                        f"{kwargs['alert_type']}: {tag}: Ltp 284.25 is below 298.55: {tag}"
                    )
                self.broker.accept(order_id)  # a far BUY rests OPEN
                assert self.fired is not None
                self.fired.append(order_id)
            return alert_id

        client.place_gtt = placed_gtt  # type: ignore[method-assign]
        return client

    def _true_now(self, kwargs: dict[str, Any]) -> bool:
        last = self.instruments[str(kwargs["tradingsymbol"])].last
        trigger = Decimal(str(kwargs["trigger_value"]))
        kind = GttAlertType(kwargs["alert_type"])
        if kind is GttAlertType.LTP_ABOVE:
            return last > trigger
        if kind is GttAlertType.LTP_BELOW:
            return last < trigger
        return False


def _ledger(rig: Any) -> list[dict[str, Any]]:
    text = rig.ledger_path().read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def _kinds(rig: Any) -> list[str]:
    return [x["kind"] for x in _ledger(rig) if x["type"] == "intent"]


# -- the spec §5 GTT --------------------------------------------------------------------------


def test_the_far_gtt_is_placed_read_back_modified_once_and_cancelled(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        result = cases_gtt.run_case(rig.run, cases_gtt.FAR)
        assert result.verdict == batch.PASS, result.reason
        [place] = rig.requests("PlaceGTTOrder")
        [modify] = rig.requests("ModifyGTTOrder")
        [cancel] = rig.requests("CancelGTTOrder")
        assert place["ai_t"] == "LTP_A_O" and Decimal(place["d"]) == Decimal("1200.00")  # 3 x 400
        assert Decimal(modify["d"]) == Decimal("1240.00")  # 3.1 x 400
        assert place["prd"] == "C" and place["prctyp"] == "LMT" and place["trantype"] == "B"
        assert Decimal(place["prc"]) == Decimal("360.00") and place["validity"] == "DAY"
        assert place["remarks"] == modify["remarks"] and place["remarks"].startswith("sdkuat-")
        assert cancel["al_id"] == modify["al_id"]
        assert rig.requests("PlaceOrder") == [] and rig.broker.orders == []
        assert rig.session.client.pending_gtts() == []
        assert _kinds(rig) == ["gtt_place", "gtt_modify", "gtt_cancel"]
        [record] = rig.records()
        assert record["case_id"] == cases_gtt.FAR.id and record["verdict"] == batch.PASS
        assert [c["kind"] for c in record["calls"]] == ["gtt_place", "gtt_modify", "gtt_cancel"]
        verdicts = [x for x in _ledger(rig) if x["type"] == "verdict"]
        assert [(v["case_id"], v["verdict"]) for v in verdicts] == [(cases_gtt.FAR.id, "pass")]


def test_an_ambiguous_gtt_place_is_found_by_tag_and_never_resent(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        rig.broker.fail_next("PlaceGTTOrder", drills.timeout_after_send())
        result = cases_gtt.run_case(rig.run, cases_gtt.FAR)
        assert result.verdict == batch.PASS, result.reason
        assert len(rig.requests("PlaceGTTOrder")) == 1
        assert rig.records()[0]["calls"][0]["verdict"] == calls.FOUND
        assert rig.session.client.pending_gtts() == []


def test_a_far_gtt_that_fires_is_check_the_account_and_its_order_is_cancelled(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=GttMarket, fire="always") as rig:
        result = cases_gtt.run_case(rig.run, cases_gtt.FAR)
        assert result.verdict == batch.CHECK and "fired" in result.reason
        assert [o.status for o in rig.broker.orders] == ["CANCELED"]
        assert rig.market.cancels == [rig.broker.orders[0].id]
        assert rig.session.guard.tripped("nse") is not None


def test_a_modify_without_a_fresh_price_skips_and_the_gtt_is_still_cancelled(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        client = rig.session.client
        real = client.place_gtt

        def then_no_price(**kwargs: Any) -> str:
            alert_id = real(**kwargs)
            rig.market.set_last("ITC-EQ", "0.00")
            return str(alert_id)

        client.place_gtt = then_no_price  # type: ignore[method-assign]
        result = cases_gtt.run_case(rig.run, cases_gtt.FAR)
        assert result.verdict == batch.SKIP and "last price" in result.reason
        assert rig.requests("ModifyGTTOrder") == [] and len(rig.requests("CancelGTTOrder")) == 1
        assert client.pending_gtts() == []


def test_ctrl_c_during_the_modify_sends_nothing_more_and_the_teardown_lists_the_gtt(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        client = rig.session.client

        def interrupted(alert_id: str, **kwargs: Any) -> str:
            raise KeyboardInterrupt

        client.modify_gtt = interrupted  # type: ignore[method-assign]
        with pytest.raises(KeyboardInterrupt):
            cases_gtt.run_case(rig.run, cases_gtt.FAR)
        assert rig.session.interrupted
        assert calls.teardown(rig.session) is None
        assert rig.requests("CancelGTTOrder") == []
        [pending] = client.pending_gtts()
        note = _ledger(rig)[-1]
        assert note["type"] == "teardown" and note["alerts"] == [pending.alert_id]


def test_a_rejected_gtt_skips_with_the_reason_recorded(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        rig.broker.fail_next("PlaceGTTOrder", drills.reject("GTT not enabled"))
        result = cases_gtt.run_case(rig.run, cases_gtt.FAR)
        assert result.verdict == batch.SKIP and "GTT not enabled" in result.reason
        assert rig.requests("ModifyGTTOrder") == [] and rig.requests("CancelGTTOrder") == []


def test_a_case_done_in_the_run_is_not_placed_again(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        assert cases_gtt.run_case(rig.run, cases_gtt.FAR).verdict == batch.PASS
        again = cases_gtt.run_case(rig.run, cases_gtt.FAR)
        assert again.verdict == batch.SKIP and "done in run" in again.reason
        assert len(rig.requests("PlaceGTTOrder")) == 1


def test_a_gtt_whose_cancel_is_rejected_is_check_the_account_and_listed_at_teardown(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        rig.broker.fail_next("CancelGTTOrder", drills.reject("GTT not found to Cancel"))
        result = cases_gtt.run_case(rig.run, cases_gtt.FAR)
        assert result.verdict == batch.CHECK and "still pending" in result.reason
        [pending] = rig.session.client.pending_gtts()
        report = calls.teardown(rig.session)
        assert report is not None and report.gtts is not None
        assert report.gtts.left == [pending.alert_id]  # its one cancel was used: listed only
        assert len(rig.requests("CancelGTTOrder")) == 1


# -- the GTT that fires at once (UAT only, O2) ----------------------------------------------------


def test_the_order_of_a_gtt_that_stays_open_after_its_cancel_is_stuck(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=GttMarket, stuck={"ITC-EQ"}) as rig:
        result = cases_gtt.run_case(rig.run, cases_gtt.FIRES)
        assert result.verdict == batch.STUCK
        assert rig.session.guard.tripped("nse") is not None
        assert len(rig.market.cancels) == 1
        report = calls.teardown(rig.session)
        assert report is not None and report.stuck == result.order_ids
        assert len(rig.market.cancels) == 1  # never a second CancelOrder


def test_the_gtt_that_fires_at_once_places_a_far_order_that_is_cancelled_once(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        result = cases_gtt.run_case(rig.run, cases_gtt.FIRES)
        assert result.verdict == batch.PASS, result.reason
        [place] = rig.requests("PlaceGTTOrder")
        assert place["ai_t"] == "LTP_B_O" and Decimal(place["d"]) > Decimal("400.00")
        [order] = rig.broker.orders
        assert order.status == "CANCELED" and rig.market.cancels == [order.id]
        alert_id = next(x["alert_id"] for x in _ledger(rig) if x.get("kind") == "gtt_place"
                        and x["type"] == "outcome")  # fmt: skip
        [row] = [o for o in rig.session.client.orders() if o.order_id == order.id]
        assert row.parent_id == alert_id and row.price == Decimal("360.00")
        adopt = [x for x in _ledger(rig) if x["type"] == "adopt"]
        assert [(a["order_id"], a["alert_id"]) for a in adopt] == [(order.id, alert_id)]
        assert result.order_ids == [order.id]
        assert rig.requests("PlaceOrder") == [] and rig.requests("CancelGTTOrder") == []
        assert _kinds(rig) == ["gtt_place", "cancel"]
        assert rig.session.client.pending_gtts() == []


def test_the_fired_order_with_uat_remarks_carrying_our_tag_is_found_and_cancelled_once(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=GttMarket, uat_remarks=True) as rig:
        result = cases_gtt.run_case(rig.run, cases_gtt.FIRES)
        assert result.verdict == batch.PASS, result.reason
        [place] = rig.requests("PlaceGTTOrder")
        [order] = rig.broker.orders
        [row] = [o for o in rig.session.client.orders() if o.order_id == order.id]
        tag = place["remarks"]
        assert row.tag == f"LTP_B_O: {tag}: Ltp 284.25 is below 298.55: {tag}"
        assert order.status == "CANCELED" and rig.market.cancels == [order.id]
        assert result.order_ids == [order.id]
        assert rig.session.ledger.tag_of(order.id) == tag
        assert rig.session.guard.tripped("nse") is None
        assert _kinds(rig) == ["gtt_place", "cancel"]


def test_a_gtt_that_does_not_fire_is_check_the_account_and_is_cancelled(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=GttMarket, fire="never") as rig:
        result = cases_gtt.run_case(rig.run, cases_gtt.FIRES)
        assert result.verdict == batch.CHECK and "did not fire" in result.reason
        assert CHECK_THE_ACCOUNT in result.reason
        assert len(rig.requests("CancelGTTOrder")) == 1
        assert rig.session.client.pending_gtts() == [] and rig.broker.orders == []
        assert rig.session.guard.tripped("nse") is not None


def test_the_order_of_a_foreign_gtt_is_never_touched(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        client = rig.session.client
        foreign = client.place_gtt(  # someone else's GTT (not through calls): it fires
            exchange="NSE", tradingsymbol="ITC-EQ", alert_type="LTP_B_O",
            trigger_value=Decimal("420.00"), quantity=1, transaction_type="BUY", product="CNC",
            order_type="LIMIT", price=Decimal("350.00"), tag="by-hand",
        )  # fmt: skip
        assert isinstance(rig.market, GttMarket)
        assert foreign and rig.market.fired
        result = cases_gtt.run_case(rig.run, cases_gtt.FIRES)
        assert result.verdict == batch.SKIP and CHECK_THE_ACCOUNT in result.reason  # a stop
        assert rig.market.cancels == [] and len(rig.requests("PlaceGTTOrder")) == 1
        assert [o.status for o in rig.broker.orders] == ["OPEN"]


# -- read-only --------------------------------------------------------------------------------


def test_the_alert_types_and_the_triggered_list_are_read_only(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        types = cases_gtt.read_alert_types(rig.run)
        triggered = cases_gtt.read_triggered(rig.run)
        assert types.verdict == triggered.verdict == batch.PASS
        assert types.record["notes"]["alert_types"] == ["ATP", "LTP"]
        assert triggered.record["notes"]["triggered_count"] == 0
        writes = ("PlaceGTTOrder", "ModifyGTTOrder", "CancelGTTOrder", "PlaceOrder")
        assert all(rig.requests(w) == [] for w in writes)
        assert not rig.ledger_path().exists() or _kinds(rig) == []


# -- fix round 1 ------------------------------------------------------------------------------


def _stop_on_quote(rig: Any, number: int) -> None:
    """The STOP file appears during the ``number``-th quote (a fresh price read)."""
    client = rig.session.client
    real = client.quote
    seen = [0]

    def quote(*args: Any, **kwargs: Any) -> Any:
        seen[0] += 1
        if seen[0] == number:
            rig.session.guard.stop_file.touch()
        return real(*args, **kwargs)

    client.quote = quote


@pytest.mark.parametrize("case", [cases_gtt.FAR, cases_gtt.FIRES], ids=lambda c: c.id)
def test_the_stop_file_before_the_gtt_place_skips_without_sending(
    tmp_path: Path, case: Any
) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        _stop_on_quote(rig, 1)
        result = cases_gtt.run_case(rig.run, case)
        assert result.verdict == batch.SKIP and result.reason == STOP_REASON
        assert rig.requests("PlaceGTTOrder") == []


def test_a_tripped_segment_before_the_gtt_place_skips_without_sending(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        client = rig.session.client
        real = client.quote

        def quote(*args: Any, **kwargs: Any) -> Any:
            rig.session.guard.trip("nse", "test")
            return real(*args, **kwargs)

        client.quote = quote  # type: ignore[method-assign]
        result = cases_gtt.run_case(rig.run, cases_gtt.FAR)
        assert result.verdict == batch.SKIP and "circuit breaker" in result.reason
        assert rig.requests("PlaceGTTOrder") == []


def test_the_stop_file_before_the_modify_skips_and_the_gtt_is_still_cancelled(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        _stop_on_quote(rig, 2)  # the modify's fresh quote
        result = cases_gtt.run_case(rig.run, cases_gtt.FAR)
        assert result.verdict == batch.SKIP and result.reason == STOP_REASON
        assert rig.requests("ModifyGTTOrder") == []
        assert len(rig.requests("CancelGTTOrder")) == 1
        assert rig.session.client.pending_gtts() == []


def test_a_modify_that_changes_the_alert_id_adopts_the_new_id_by_tag(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        client = rig.session.client
        real_pending, real_modify, real_cancel = (
            client.pending_gtts,
            client.modify_gtt,
            client.cancel_gtt,
        )
        renamed: dict[str, str] = {}  # the server's id -> the id it now lists (synthetic)

        def modify(alert_id: str, **kwargs: Any) -> str:
            returned = real_modify(alert_id, **kwargs)
            renamed[returned] = f"{returned}9"
            return f"{returned}9"

        def pending() -> list[Any]:
            return [g.model_copy(update={"alert_id": renamed.get(g.alert_id or "", g.alert_id)})
                    for g in real_pending()]  # fmt: skip

        def cancel(alert_id: str) -> str:
            real = next((k for k, v in renamed.items() if v == alert_id), alert_id)
            real_cancel(real)
            return alert_id

        client.modify_gtt = modify  # type: ignore[method-assign]
        client.pending_gtts = pending  # type: ignore[method-assign]
        client.cancel_gtt = cancel  # type: ignore[method-assign]
        result = cases_gtt.run_case(rig.run, cases_gtt.FAR)
        assert result.verdict == batch.PASS, result.reason
        [new_id] = renamed.values()
        assert new_id in rig.session.ledger.alerts()
        assert [x["alert_id"] for x in _ledger(rig) if x["type"] == "gtt_adopt"] == [new_id]
        [cancel_intent] = [x for x in _ledger(rig) if x.get("kind") == "gtt_cancel"
                           and x["type"] == "intent"]  # fmt: skip
        assert cancel_intent["alert_id"] == new_id and real_pending() == []


# -- fix round 2 ------------------------------------------------------------------------------


def _foreign_legs(rig: Any) -> None:
    """From now on the book also lists, for each alert id of ours, a foreign open order whose
    ``add_ord_id`` collides with it (an iceberg leg: another tag and symbol)."""
    client = rig.session.client
    real = client.orders

    def orders() -> list[Order]:
        rows = real()
        legs = [
            Order.model_validate({
                "norenordno": f"77{alert_id}", "status": "OPEN", "remarks": "by-hand-iceberg",
                "exch": "NSE", "tsym": "RELIANCE-EQ", "trantype": "B", "qty": "50",
                "add_ord_id": alert_id,
            })
            for alert_id in sorted(rig.session.ledger.alerts())
        ]  # fmt: skip
        return [*legs, *rows]

    client.orders = orders


def test_a_foreign_leg_with_our_alert_id_is_not_taken_for_the_fired_order(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        _foreign_legs(rig)
        result = cases_gtt.run_case(rig.run, cases_gtt.FIRES)
        assert result.verdict == batch.PASS, result.reason
        [order] = rig.broker.orders
        assert result.order_ids == [order.id] and rig.market.cancels == [order.id]


def test_a_foreign_leg_with_our_alert_id_is_not_a_sign_that_the_far_gtt_fired(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        _foreign_legs(rig)
        result = cases_gtt.run_case(rig.run, cases_gtt.FAR)
        assert result.verdict == batch.PASS, result.reason
        assert rig.market.cancels == [] and "fired" not in rig.records()[0]["notes"]


def test_a_foreign_leg_is_not_a_late_order_of_a_gtt_that_did_not_fire(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=GttMarket, fire="never") as rig:
        _foreign_legs(rig)
        result = cases_gtt.run_case(rig.run, cases_gtt.FIRES)
        assert result.verdict == batch.CHECK and "did not fire" in result.reason
        assert "fired late" not in result.reason
        assert rig.market.cancels == []


# -- cancelled GTTs in the triggered list (spec F32) ------------------------------------------


def _cancelled_listed_as_triggered(rig: Any, stat: str | None) -> None:
    """From now on ``triggered_gtts()`` also lists each GTT cancelled so far, with row ``stat``
    (UAT lists a cancelled GTT there with ``CANCELLED``)."""
    client = rig.session.client
    real = client.triggered_gtts

    def triggered() -> list[TriggeredGtt]:
        (place,) = rig.requests("PlaceGTTOrder")
        rows = [
            TriggeredGtt.model_validate(
                {"al_id": c["al_id"], "remarks": place["remarks"], "stat": stat}
            )
            for c in rig.requests("CancelGTTOrder")
        ]
        return [*rows, *real()]

    client.triggered_gtts = triggered


@pytest.mark.parametrize("stat", ["CANCELLED", "cancelled"])
def test_a_cancelled_far_gtt_in_the_triggered_list_is_recorded_not_fired(
    tmp_path: Path, stat: str
) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        _cancelled_listed_as_triggered(rig, stat)
        result = cases_gtt.run_case(rig.run, cases_gtt.FAR)
        assert result.verdict == batch.PASS, result.reason
        [cancel] = rig.requests("CancelGTTOrder")
        notes = rig.records()[0]["notes"]
        assert notes["triggered_cancelled"] == [cancel["al_id"]]
        assert "fired" not in notes


@pytest.mark.parametrize("stat", ["TRIGGERED", "CANCELLED ", None])
def test_any_other_triggered_row_of_the_far_gtt_is_check_the_account(
    tmp_path: Path, stat: str | None
) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        _cancelled_listed_as_triggered(rig, stat)
        result = cases_gtt.run_case(rig.run, cases_gtt.FAR)
        assert result.verdict == batch.CHECK and "fired" in result.reason
        assert len(rig.requests("CancelGTTOrder")) == 1
