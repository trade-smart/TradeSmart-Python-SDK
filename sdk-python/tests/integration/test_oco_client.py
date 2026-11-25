"""``place_oco``, ``modify_oco`` and ``cancel_oco`` on both clients, on the offline broker.

Spec: ``specifications/sdk-behaviour.md`` 2.9. Every case runs on both clients.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tradesmart import (
    AsyncTradeSmart,
    OcoLeg,
    OrderRejectedError,
    OrderStatusUnknownError,
    RequestValidationError,
    TradeSmart,
)
from tradesmart.testing import OfflineBroker, drills

Client = TradeSmart | AsyncTradeSmart

_VECTORS = Path(__file__).resolve().parents[3] / "specifications" / "conformance"
_FIRST = json.loads((_VECTORS / "oco_requests.json").read_text(encoding="utf-8"))["cases"][0]

_PLACE: dict[str, Any] = {
    "exchange": "NSE",
    "tradingsymbol": "RELIANCE-EQ",
    "transaction_type": "BUY",
    "quantity": 1,
    "product": "CNC",
    "stop_loss": OcoLeg(trigger_value="1430", price="1440"),
    "target": OcoLeg(trigger_value="1170", price="1160"),
}
_MODIFY: dict[str, Any] = {**_PLACE, "validity": "DAY", "alert_validity": "GTT"}
_ID = "26111800000001"


@dataclass
class Rig:
    broker: OfflineBroker
    kind: str
    clients: list[Client] = field(default_factory=list)

    def client(self, **overrides: Any) -> Client:
        built: Client = (
            self.broker.client(**overrides)
            if self.kind == "sync"
            else self.broker.async_client(**overrides)
        )
        self.clients.append(built)
        return built

    async def run(self, client: Client, method: str, *args: Any, **kwargs: Any) -> Any:
        result = getattr(client, method)(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    def sent(self, endpoint: str) -> list[dict[str, Any]]:
        return [r.payload for r in self.broker.requests if r.endpoint == endpoint]


@pytest.fixture(params=["sync", "async"])
async def rig(request: pytest.FixtureRequest) -> AsyncIterator[Rig]:
    with OfflineBroker() as broker:
        built = Rig(broker, request.param)
        try:
            yield built
        finally:
            for client in built.clients:
                if isinstance(client, AsyncTradeSmart):
                    await client.aclose()
                else:
                    client.close()


@pytest.mark.parametrize("method", ["place_oco", "modify_oco", "cancel_oco"])
def test_same_signature_on_both_clients(method: str) -> None:
    assert inspect.signature(getattr(TradeSmart, method)) == inspect.signature(
        getattr(AsyncTradeSmart, method)
    )
    assert inspect.iscoroutinefunction(getattr(AsyncTradeSmart, method))


async def test_place_returns_the_al_id_and_sends_the_exact_body(rig: Rig) -> None:
    rig.broker.set_reply("PlaceOCOOrder", {"stat": "OI created", "al_id": _ID})
    client = rig.client()
    assert await rig.run(client, "place_oco", **_PLACE) == _ID
    (payload,) = rig.sent("PlaceOCOOrder")
    expected = json.loads(
        json.dumps(_FIRST["expected"]["payload"]).replace("TESTUID", rig.broker.client_id)
    )
    assert json.dumps(payload) == json.dumps(expected)


async def test_cancel_returns_the_al_id_on_oi_deleted(rig: Rig) -> None:
    rig.broker.set_reply("CancelOCOOrder", {"stat": "OI deleted", "al_id": _ID})
    assert await rig.run(rig.client(), "cancel_oco", _ID) == _ID
    assert rig.sent("CancelOCOOrder") == [{"uid": rig.broker.client_id, "al_id": _ID}]


async def test_modify_returns_the_al_id(rig: Rig) -> None:
    rig.broker.set_reply("ModifyOCOOrder", {"stat": "OI replaced", "al_id": _ID})
    assert await rig.run(rig.client(), "modify_oco", _ID, **_MODIFY) == _ID
    (payload,) = rig.sent("ModifyOCOOrder")
    assert list(payload)[:2] == ["uid", "al_id"]


async def test_cancel_docs_word_is_unknown_and_names_pending_gtts(rig: Rig) -> None:
    rig.broker.set_reply("CancelOCOOrder", {"stat": "Oi delete success", "al_id": _ID})
    with pytest.raises(OrderStatusUnknownError, match=r"pending_gtts\(\)"):
        await rig.run(rig.client(), "cancel_oco", _ID)
    assert len(rig.sent("CancelOCOOrder")) == 1


@pytest.mark.parametrize(
    ("method", "endpoint"), [("modify_oco", "ModifyOCOOrder"), ("cancel_oco", "CancelOCOOrder")]
)
async def test_invalid_oi_is_a_rejection(rig: Rig, method: str, endpoint: str) -> None:
    rig.broker.set_reply(endpoint, {"stat": "Invalid Oi", "al_id": "1"})
    arguments = _MODIFY if method == "modify_oco" else {}
    with pytest.raises(OrderRejectedError) as info:
        await rig.run(rig.client(), method, "1", **arguments)
    assert info.value.emsg == "Invalid Oi"
    assert len(rig.sent(endpoint)) == 1


async def test_timeout_after_send_is_unknown_never_retried(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.broker.fail_next("PlaceOCOOrder", drills.timeout_after_send())
    with (
        caplog.at_level(logging.INFO, logger="tradesmart.orders"),
        pytest.raises(OrderStatusUnknownError),
    ):
        await rig.run(rig.client(), "place_oco", **_PLACE)
    assert len(rig.sent("PlaceOCOOrder")) == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("pending_gtts()" in line for line in warnings)


async def test_refusals_send_nothing(rig: Rig) -> None:
    client = rig.client()
    swapped = {**_PLACE, "stop_loss": _PLACE["target"], "target": _PLACE["stop_loss"]}
    with pytest.raises(RequestValidationError):
        await rig.run(client, "place_oco", **swapped)
    with pytest.raises(RequestValidationError):
        await rig.run(client, "cancel_oco", " ")
    assert rig.broker.requests == []


@pytest.mark.parametrize("bad", [{"trigger_value": "1430"}, 5, None])
async def test_a_non_leg_is_refused_and_sends_nothing(rig: Rig, bad: Any) -> None:
    client = rig.client()
    with pytest.raises(RequestValidationError) as info:
        await rig.run(client, "place_oco", **{**_PLACE, "stop_loss": bad})
    assert [e["loc"] for e in info.value.errors] == [("place_order_params",)]
    with pytest.raises(RequestValidationError):
        await rig.run(client, "modify_oco", _ID, **{**_MODIFY, "target": bad})
    assert rig.broker.requests == []


# --- the offline broker models the OCO endpoints ---------------------------------------------


async def test_offline_place_lists_one_pending_oco_with_the_sent_legs(rig: Rig) -> None:
    client = rig.client()
    al_id = await rig.run(client, "place_oco", **_PLACE)
    assert al_id.endswith("00000001")
    rows = await rig.run(client, "pending_gtts")
    (row,) = rows
    assert row.is_oco and row.alert_id == al_id
    assert row.stop_loss is not None and row.target is not None
    assert row.stop_loss.trigger_value == Decimal("1430")
    assert row.target.trigger_value == Decimal("1170")
    assert row.stop_loss.leg.price == Decimal("1440") and row.target.leg.price == Decimal("1160")
    assert row.stop_loss.leg.quantity == 1
    raw = row.raw
    assert raw["d"] == "1430.00" and raw["remarks"] == "" and raw["qty"] == 1
    assert raw["place_order_params"]["qty"] == 1
    assert raw["place_order_params"]["s_prdt_ali"] == "CNC"
    assert "s_prdt_ali" not in raw["place_order_params_leg2"]
    assert [t["d"] for t in raw["oivariable"]] == ["1430.00", "1170.00"]
    assert "ipaddr" not in json.dumps(raw)  # no real IP is ever invented


async def test_offline_modify_updates_the_triggers_and_keeps_the_id(rig: Rig) -> None:
    client = rig.client()
    al_id = await rig.run(client, "place_oco", **_PLACE)
    changed = {
        **_MODIFY,
        "stop_loss": OcoLeg(trigger_value="1450", price="1460"),
    }
    assert await rig.run(client, "modify_oco", al_id, **changed) == al_id
    (row,) = await rig.run(client, "pending_gtts")
    assert row.alert_id == al_id
    assert row.stop_loss is not None
    assert row.stop_loss.trigger_value == Decimal("1450")
    assert row.stop_loss.leg.price == Decimal("1460")


async def test_offline_cancel_leaves_pending_and_is_cancelled_in_triggered(rig: Rig) -> None:
    client = rig.client()
    al_id = await rig.run(client, "place_oco", **_PLACE)
    assert await rig.run(client, "cancel_oco", al_id) == al_id
    assert await rig.run(client, "pending_gtts") == []
    (row,) = await rig.run(client, "triggered_gtts")
    assert row.alert_id == al_id and row.raw["stat"] == "CANCELLED" and row.is_oco


async def test_offline_modify_and_cancel_of_a_cancelled_or_unknown_id_are_rejected(
    rig: Rig,
) -> None:
    client = rig.client()
    al_id = await rig.run(client, "place_oco", **_PLACE)
    await rig.run(client, "cancel_oco", al_id)
    for target in (al_id, "26111800009999"):
        with pytest.raises(OrderRejectedError) as info:
            await rig.run(client, "modify_oco", target, **_MODIFY)
        assert info.value.emsg == "Invalid Oi"
        with pytest.raises(OrderRejectedError):
            await rig.run(client, "cancel_oco", target)


async def test_offline_place_requires_the_leg_fields(rig: Rig) -> None:
    with pytest.raises(OrderRejectedError, match="Invalid Input"):
        await rig.run(
            rig.client(),
            "request",
            "PlaceOCOOrder",
            {"uid": rig.broker.client_id, "tsym": "X"},
            state_changing=False,
        )


async def test_offline_trigger_gtt_on_an_oco_is_not_simulated(rig: Rig) -> None:
    al_id = await rig.run(rig.client(), "place_oco", **_PLACE)
    with pytest.raises(
        ValueError,
        match="OCO triggering is not simulated: no fired OCO has been observed yet",
    ):
        rig.broker.trigger_gtt(al_id)
    assert len(await rig.run(rig.client(), "pending_gtts")) == 1


@pytest.mark.parametrize("bad", ["LTP_B_O", "LTP_A_O", "", "lmt_bos_o"])
async def test_offline_place_with_a_non_oco_alert_type_is_refused_and_stores_nothing(
    rig: Rig, bad: str
) -> None:
    client = rig.client()
    body = {"uid": rig.broker.client_id, "tsym": "RELIANCE-EQ", "exch": "NSE", "validity": "GTT"}
    legs = {"trantype": "B", "prctyp": "LMT", "prd": "C", "ret": "DAY", "qty": "1", "prc": "1"}
    payload = {
        **body,
        "ai_t": bad,
        "oivariable": [{"var_name": "x", "d": "2"}, {"var_name": "y", "d": "1"}],
        "place_order_params": legs,
        "place_order_params_leg2": legs,
    }
    with pytest.raises(OrderRejectedError, match="Invalid Input : ai_t"):
        await rig.run(client, "request", "PlaceOCOOrder", payload, state_changing=False)
    assert await rig.run(client, "pending_gtts") == []


async def test_offline_modify_may_switch_codes_but_refuses_a_non_oco_one(rig: Rig) -> None:
    client = rig.client()
    al_id = await rig.run(client, "place_oco", **_PLACE)
    legs = {"trantype": "B", "prctyp": "LMT", "prd": "C", "ret": "DAY", "qty": "1", "prc": "1"}
    payload = {
        "uid": rig.broker.client_id,
        "al_id": al_id,
        "tsym": "RELIANCE-EQ",
        "exch": "NSE",
        "validity": "GTT",
        "ai_t": "LTP_B_O",
        "oivariable": [{"var_name": "x", "d": "2"}, {"var_name": "y", "d": "1"}],
        "place_order_params": legs,
        "place_order_params_leg2": legs,
    }
    with pytest.raises(OrderRejectedError, match="Invalid Input : ai_t"):
        await rig.run(client, "request", "ModifyOCOOrder", payload, state_changing=False)
    (row,) = await rig.run(client, "pending_gtts")
    assert row.raw["ai_t"] == "LMT_BOS_O" and row.raw["d"] == "1430.00"
    await rig.run(
        client, "request", "ModifyOCOOrder", {**payload, "ai_t": "LMT_SOB_O"}, state_changing=False
    )
    (row,) = await rig.run(client, "pending_gtts")
    assert row.raw["ai_t"] == "LMT_SOB_O"


# -- the two offline brokers answer the same (rows and refusals; JavaScript has the same tests) --

_PARITY_LEG = {
    "tsym": "SBIN-EQ",
    "exch": "NSE",
    "trantype": "B",
    "prctyp": "LMT",
    "prd": "C",
    "ret": "DAY",
    "actid": "TEST123",
    "qty": "1",
    "prc": "822",
    "ordersource": "API",
}


def _parity_body(**extra: Any) -> dict[str, Any]:
    return {
        "uid": "TEST123",
        "tsym": "SBIN-EQ",
        "exch": "NSE",
        "validity": "GTT",
        "ai_t": "LMT_BOS_O",
        "oivariable": [{"var_name": "x", "d": "820"}, {"var_name": "y", "d": "760"}],
        "place_order_params": dict(_PARITY_LEG),
        "place_order_params_leg2": {
            **_PARITY_LEG,
            "prctyp": "SL-LMT",
            "prc": "758",
            "trgprc": "759",
        },
        **extra,
    }


def test_offline_oco_rows_carry_the_times_and_the_trigger_price_and_no_ip_address() -> None:
    with OfflineBroker("TEST123") as broker, broker.client() as client:
        placed = client.request("PlaceOCOOrder", _parity_body(), state_changing=True)
        (row,) = client.request("GetPendingGTTOrder", {}, state_changing=False, expect="list")
        assert re.fullmatch(r"\d\d:\d\d:\d\d \d\d-\d\d-\d{4}", row["norentm"])
        assert row["upd_tm"] == row["norentm"]
        assert "trgprc" not in row["place_order_params"]
        assert row["place_order_params_leg2"]["trgprc"] == "759.00"
        assert "ipaddr" not in row["place_order_params"]  # no address is ever invented
        assert "ipaddr" not in row["place_order_params_leg2"]
        client.request("CancelOCOOrder", {"al_id": placed["al_id"]}, state_changing=True)
        rows = client.request("GetTrgOi", {}, state_changing=False, expect="list")
        (cancelled,) = [r for r in rows if r["al_id"] == placed["al_id"]]
        assert cancelled["stat"] == "CANCELLED"
        assert cancelled["norentm"] == cancelled["upd_tm"] == row["norentm"]


def test_offline_oco_refusals_name_the_fields_in_order_one_problem_at_a_time() -> None:
    two_x = [{"var_name": "x", "d": "820"}, {"var_name": "x", "d": "760"}]
    cases = [
        (
            _parity_body(tsym="", exch="", validity="", ai_t=""),
            "exch, tsym, ai_t, validity required",
        ),
        (_parity_body(tsym="", place_order_params="x"), "tsym required"),
        (_parity_body(ai_t="LTP_B_O"), "ai_t must be one of LMT_BOS_O, LMT_SOB_O"),
        (_parity_body(oivariable=two_x), "oivariable required: one trigger each for x and y"),
        (
            _parity_body(oivariable=[{"var_name": "x", "d": "820"}]),
            "oivariable required: one trigger each for x and y",
        ),
        (_parity_body(oivariable="x"), "oivariable required: one trigger each for x and y"),
        (_parity_body(place_order_params="x"), "place_order_params required"),
        (
            _parity_body(place_order_params_leg2={**_PARITY_LEG, "ret": "", "qty": ""}),
            "place_order_params_leg2: ret, qty required",
        ),
    ]
    with OfflineBroker("TEST123") as broker, broker.client() as client:
        for payload, message in cases:
            with pytest.raises(OrderRejectedError) as caught:
                client.request("PlaceOCOOrder", payload, state_changing=True)
            assert caught.value.emsg == f"Invalid Input : {message}"
        assert client.pending_gtts() == []
