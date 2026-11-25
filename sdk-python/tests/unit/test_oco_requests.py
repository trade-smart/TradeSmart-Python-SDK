"""OCO request building (spec 2.9): exact nested bodies, side check, GTT refusal of OCO codes."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tradesmart import OcoLeg, RequestValidationError
from tradesmart.endpoints.gtt import modify_gtt_call, place_gtt_call
from tradesmart.endpoints.oco import (
    CANCEL_OCO_ORDER,
    MODIFY_OCO_ORDER,
    PLACE_OCO_ORDER,
    cancel_oco_call,
    modify_oco_call,
    place_oco_call,
)

_BUY: dict[str, Any] = {
    "exchange": "NSE",
    "tradingsymbol": "RELIANCE-EQ",
    "transaction_type": "BUY",
    "quantity": 1,
    "product": "CNC",
    "stop_loss": OcoLeg(trigger_value="1430", price="1440"),
    "target": OcoLeg(trigger_value="1170", price="1160"),
}


def test_specs_are_the_oco_endpoints() -> None:
    assert PLACE_OCO_ORDER.name == "PlaceOCOOrder"
    assert MODIFY_OCO_ORDER.name == "ModifyOCOOrder"
    assert CANCEL_OCO_ORDER.name == "CancelOCOOrder"
    assert PLACE_OCO_ORDER.state_changing


def test_place_buy_body_and_key_order() -> None:
    payload = place_oco_call("U1", **_BUY).payload
    assert list(payload) == [
        "uid",
        "tsym",
        "exch",
        "validity",
        "ai_t",
        "oivariable",
        "place_order_params",
        "place_order_params_leg2",
    ]
    assert payload["ai_t"] == "LMT_BOS_O"
    assert payload["validity"] == "GTT"
    assert payload["oivariable"] == [
        {"var_name": "x", "d": "1430"},
        {"var_name": "y", "d": "1170"},
    ]
    leg = payload["place_order_params"]
    assert isinstance(leg, dict)
    assert list(leg) == [
        "tsym", "exch", "trantype", "prctyp", "prd", "ret", "actid", "uid", "qty", "prc",
        "ordersource",
    ]  # fmt: skip
    assert leg["ordersource"] == "API" and leg["ret"] == "DAY" and leg["trantype"] == "B"


def test_place_sell_uses_the_sell_code() -> None:
    call = place_oco_call(
        "U1",
        **{
            **_BUY,
            "transaction_type": "SELL",
            "stop_loss": OcoLeg(trigger_value="1170", price="1160"),
            "target": OcoLeg(trigger_value="1430", price="1420"),
        },
    )
    assert call.payload["ai_t"] == "LMT_SOB_O"


def test_place_oco_side_check_buy() -> None:
    bad = {**_BUY, "stop_loss": _BUY["target"], "target": _BUY["stop_loss"]}
    with pytest.raises(RequestValidationError) as info:
        place_oco_call("U1", **bad)
    assert [e["loc"] for e in info.value.errors] == [("oivariable",)]


def test_place_oco_side_check_sell() -> None:
    with pytest.raises(RequestValidationError) as info:
        place_oco_call("U1", **{**_BUY, "transaction_type": "SELL"})
    assert [e["loc"] for e in info.value.errors] == [("oivariable",)]


def test_place_oco_equal_refused() -> None:
    leg = OcoLeg(trigger_value="1300", price="1290")
    with pytest.raises(RequestValidationError) as info:
        place_oco_call("U1", **{**_BUY, "stop_loss": leg, "target": leg})
    assert [e["loc"] for e in info.value.errors] == [("oivariable",)]


def test_trigger_value_sent_as_given() -> None:
    call = place_oco_call(
        "U1", **{**_BUY, "target": OcoLeg(trigger_value="1169.123", price="1160")}
    )
    assert json.dumps(call.payload["oivariable"]) == json.dumps(
        [{"var_name": "x", "d": "1430"}, {"var_name": "y", "d": "1169.123"}]
    )


def test_modify_al_id_second_and_no_defaults() -> None:
    call = modify_oco_call("U1", "A1", **_BUY, validity="DAY", alert_validity="GTT", tag="t")
    assert list(call.payload)[:2] == ["uid", "al_id"]
    leg = call.payload["place_order_params"]
    assert isinstance(leg, dict) and list(leg)[-1] == "remarks"
    with pytest.raises(TypeError):
        modify_oco_call("U1", "A1", **_BUY)  # type: ignore[call-arg]


def test_cancel_body() -> None:
    assert cancel_oco_call("U1", "A1").payload == {"uid": "U1", "al_id": "A1"}


@pytest.mark.parametrize("code", ["LMT_BOS_O", "LMT_SOB_O"])
def test_place_gtt_refuses_oco_alert_type(code: str) -> None:
    args: dict[str, Any] = {
        "exchange": "NSE",
        "tradingsymbol": "INFY-EQ",
        "alert_type": code,
        "trigger_value": "1500",
        "quantity": 1,
        "transaction_type": "B",
        "product": "C",
        "order_type": "LMT",
        "price": "1500",
        "tag": "t",
    }
    with pytest.raises(RequestValidationError) as info:
        place_gtt_call("U1", **args)
    assert [e["loc"] for e in info.value.errors] == [("ai_t",)]
    assert "place_oco" in str(info.value.errors[0]["msg"])
    with pytest.raises(RequestValidationError) as info2:
        modify_gtt_call("U1", "A1", **args, validity="DAY", alert_validity="GTT")
    assert "modify_oco" in str(info2.value.errors[0]["msg"])


@pytest.mark.parametrize("bad", [{"trigger_value": "1430"}, 5, None, "x"])
@pytest.mark.parametrize(
    ("key", "loc"), [("stop_loss", "place_order_params"), ("target", "place_order_params_leg2")]
)
@pytest.mark.parametrize("method", ["place", "modify"])
def test_a_leg_that_is_not_an_oco_leg_is_refused(bad: Any, key: str, loc: str, method: str) -> None:
    arguments = {**_BUY, key: bad}
    with pytest.raises(RequestValidationError) as info:
        if method == "place":
            place_oco_call("U1", **arguments)
        else:
            modify_oco_call("U1", "A1", **arguments, validity="DAY", alert_validity="GTT")
    assert [e["loc"] for e in info.value.errors] == [(loc,)]
