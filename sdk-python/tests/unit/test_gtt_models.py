"""GTT enums and response models (spec §3; docs ``03-gtt-orders.md``)."""

from __future__ import annotations

import copy
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

import tradesmart
from tradesmart import (
    Exchange,
    Gtt,
    GttAlertType,
    GttValidity,
    OrderType,
    Product,
    TransactionType,
    TriggeredGtt,
    Validity,
)
from tradesmart.endpoints.gtt import GET_ENABLED_GTTS, GET_PENDING_GTT_ORDER, GET_TRG_OI
from tradesmart.errors import ResponseParseError
from tradesmart.models.base import IST
from tradesmart.models.orders import Order

# The documented Pending GTT Orders row (03-gtt-orders.md; account id synthetic).
PENDING_ROW: dict[str, Any] = {
    "stat": "Ok",
    "ai_t": "LTP_B_O",
    "al_id": "26111200000010",
    "tsym": "INFY-EQ",
    "exch": "NSE",
    "token": "1594",
    "remarks": "Buy INFY below 1500",
    "validity": "GTT",
    "pp": "2",
    "ls": "1",
    "actid": "TEST123",
    "trantype": "B",
    "prctyp": "LMT",
    "qty": 1,
    "prc": "1500.00",
    "prd": "C",
    "s_prdt_ali": "CNC",
    "ret": "DAY",
    "ordersource": "API",
    "d": "1500.00",
}
TRIGGERED_ROW: dict[str, Any] = {
    **PENDING_ROW,
    "norentm": "10:41:27 13-11-2026",
    "sms_flag": "0",
    "email_flag": "0",
    "push_flag": "0",
}
ENABLED_BODY = {
    "stat": "Ok",
    "request_time": "12112026121503",
    "ai_ts": [{"ai_t": "ATP"}, {"ai_t": "LTP"}],
}


# --- enums ---------------------------------------------------------------------------------


def test_alert_type_has_the_13_documented_codes_and_the_two_oco_codes() -> None:
    assert {m.value for m in GttAlertType} == {
        "LTP_A_O",
        "LTP_B_O",
        "CH_PER_A_O",
        "CH_PER_B_O",
        "ATP_A_O",
        "ATP_B_O",
        "OI_A_O",
        "OI_B_O",
        "TOI_A_O",
        "TOI_B_O",
        "LTP_A_52HIGH_O",
        "LTP_B_52HIGH_O",
        "VOLUME_A_O",
        "LMT_BOS_O",
        "LMT_SOB_O",
    }
    assert len(GttAlertType) == 15


def test_alert_type_lookup() -> None:
    assert GttAlertType("ltp_b_o") is GttAlertType.LTP_BELOW
    assert GttAlertType("LTP_ABOVE") is GttAlertType.LTP_ABOVE
    assert str(GttAlertType.VOLUME_ABOVE) == "VOLUME_A_O"
    with pytest.raises(ValueError):
        GttAlertType("LTP")


def test_gtt_validity() -> None:
    assert [m.value for m in GttValidity] == ["DAY", "GTT"]
    assert GttValidity("gtt") is GttValidity.GTT
    with pytest.raises(ValueError):
        GttValidity("EOS")


def test_exports() -> None:
    for name in ("GttAlertType", "GttValidity", "Gtt", "TriggeredGtt"):
        assert name in tradesmart.__all__
        assert getattr(tradesmart, name) is not None


# --- Gtt -------------------------------------------------------------------------------------


def test_pending_row_parses() -> None:
    (gtt,) = GET_PENDING_GTT_ORDER.parse([PENDING_ROW])
    assert isinstance(gtt, Gtt)
    assert gtt.alert_id == "26111200000010"
    assert gtt.alert_type is GttAlertType.LTP_BELOW
    assert gtt.trigger_value == Decimal("1500.00")
    assert gtt.alert_validity is GttValidity.GTT
    assert gtt.exchange == "NSE" == Exchange.NSE
    assert gtt.tradingsymbol == "INFY-EQ"
    assert gtt.token == "1594"
    assert gtt.tag == "Buy INFY below 1500"
    assert gtt.transaction_type is TransactionType.BUY
    assert gtt.order_type is OrderType.LIMIT
    assert gtt.product is Product.CNC
    assert gtt.validity is Validity.DAY
    assert gtt.account_id == "TEST123"
    assert gtt.quantity == 1  # a JSON number in the docs example
    assert gtt.price == Decimal("1500.00")
    assert gtt.trigger_price is None
    assert gtt.price_precision == 2
    assert gtt.lot_size == 1
    assert gtt.ordersource == "API"
    assert gtt.raw == PENDING_ROW


def test_qty_as_text_also_parses() -> None:
    (gtt,) = GET_PENDING_GTT_ORDER.parse([{**PENDING_ROW, "qty": "25"}])
    assert gtt.quantity == 25


def test_row_without_ret_keeps_validity_none() -> None:
    """Review Focus 3: the wire ``validity`` (DAY/GTT) never fills ``validity`` (``ret``)."""
    row = {k: v for k, v in PENDING_ROW.items() if k != "ret"}
    for validity in ("DAY", "GTT"):
        (gtt,) = GET_PENDING_GTT_ORDER.parse([{**row, "validity": validity}])
        assert gtt.validity is None
        assert gtt.alert_validity is GttValidity(validity)


def test_field_names_do_not_populate_fields() -> None:
    gtt = Gtt.from_wire({"alert_id": "x", "validity": "DAY", "price": "5"})
    assert gtt.alert_id is None
    assert gtt.price is None
    assert gtt.alert_validity is GttValidity.DAY


def test_unknown_alert_type_or_validity_kept_as_text() -> None:
    gtt = Gtt.from_wire({**PENDING_ROW, "ai_t": "NEW_TYPE", "validity": "WEEK"})
    assert gtt.alert_type == "NEW_TYPE"
    assert gtt.alert_validity == "WEEK"


def test_stop_loss_row_trigger_price() -> None:
    gtt = Gtt.from_wire({**PENDING_ROW, "prctyp": "SL-LMT", "trgprc": "1499.50"})
    assert gtt.trigger_price == Decimal("1499.50")
    assert gtt.order_type is OrderType.SL


def test_a_bad_field_is_nulled_leniently() -> None:
    gtt = Gtt.from_wire({**PENDING_ROW, "d": "abc"})
    assert gtt.trigger_value is None
    assert gtt.raw["d"] == "abc"


def test_alert_id_is_protected() -> None:
    with pytest.raises(ResponseParseError):
        Gtt.from_wire({**PENDING_ROW, "al_id": ["not", "an", "id"]})


def test_extra_keys_are_kept() -> None:
    gtt = Gtt.from_wire({**PENDING_ROW, "newkey": "v"})
    assert gtt.raw["newkey"] == "v"


def test_pending_parser_on_empty_list() -> None:
    assert GET_PENDING_GTT_ORDER.parse([]) == []


# --- TriggeredGtt --------------------------------------------------------------------------


def test_triggered_row_parses() -> None:
    (gtt,) = GET_TRG_OI.parse([TRIGGERED_ROW])
    assert isinstance(gtt, TriggeredGtt) and isinstance(gtt, Gtt)
    assert gtt.alert_id == "26111200000010"
    assert gtt.norentm == datetime(2026, 11, 13, 10, 41, 27, tzinfo=IST)
    assert (gtt.sms_flag, gtt.email_flag, gtt.push_flag) == ("0", "0", "0")
    assert gtt.quantity == 1


def test_triggered_row_without_ret() -> None:
    row = {k: v for k, v in TRIGGERED_ROW.items() if k != "ret"}
    assert GET_TRG_OI.parse([row])[0].validity is None


# --- enabled alert types ---------------------------------------------------------------------


def test_enabled_alert_types_documented_example() -> None:
    assert GET_ENABLED_GTTS.parse(copy.deepcopy(ENABLED_BODY)) == ["ATP", "LTP"]


def test_enabled_alert_types_empty() -> None:
    assert GET_ENABLED_GTTS.parse({"stat": "Ok", "ai_ts": []}) == []
    assert GET_ENABLED_GTTS.parse({"stat": "Ok"}) == []
    assert GET_ENABLED_GTTS.parse([]) == []  # the "no data" Not_Ok on an allow_empty read


@pytest.mark.parametrize(
    "ai_ts",
    ["ATP", {"ai_t": "ATP"}, [{"ai_t": 1}], ["ATP"], [{"other": "ATP"}], [{"ai_t": None}]],
)
def test_enabled_alert_types_bad_shape_is_a_parse_error(ai_ts: Any) -> None:
    with pytest.raises(ResponseParseError):
        GET_ENABLED_GTTS.parse({"stat": "Ok", "ai_ts": ai_ts})


# --- Order.parent_id -------------------------------------------------------------------------


def test_order_parent_id_from_add_ord_id() -> None:
    order = Order.from_wire({"stat": "Ok", "norenordno": "26111300000001", "add_ord_id": "2611"})
    assert order.parent_id == "2611"
    assert Order.from_wire({"norenordno": "1"}).parent_id is None


def test_order_parent_id_numeric_becomes_text() -> None:
    assert Order.from_wire({"norenordno": "1", "add_ord_id": 2611}).parent_id == "2611"


@pytest.mark.parametrize("body", ["ATP", [{"ai_t": "LTP"}], 5])
def test_enabled_alert_types_non_object_body_is_a_parse_error(body: Any) -> None:
    with pytest.raises(ResponseParseError):
        GET_ENABLED_GTTS.parse(body)
