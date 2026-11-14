"""GTT request builders: wire bodies, key order, defaults and local refusals (spec §3).

Docs: ``03-gtt-orders.md`` (Place / Modify / Cancel GTT Order, Pending GTT Orders, Triggered GTT
Orders, Enabled GTT Alert Types). Every refusal is a ``RequestValidationError`` raised while the
call is built, so nothing can be sent.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

import pytest

from tradesmart import GttAlertType, GttValidity, Validity
from tradesmart._client_core import ACK_ID_FIELDS, STATE_CHANGING_ENDPOINTS, SUCCESS_STATS
from tradesmart.endpoints.gtt import (
    CANCEL_GTT_ORDER,
    GET_ENABLED_GTTS,
    GET_PENDING_GTT_ORDER,
    GET_TRG_OI,
    MODIFY_GTT_ORDER,
    PLACE_GTT_ORDER,
    cancel_gtt_call,
    gtt_alert_types_call,
    modify_gtt_call,
    pending_gtts_call,
    place_gtt_call,
    triggered_gtts_call,
)
from tradesmart.errors import OrderStatusUnknownError, RequestValidationError

UID = "TEST123"
AL_ID = "26111200000010"

# The documented Place GTT Order example (03-gtt-orders.md), in SDK argument names.
DOC_EXAMPLE: dict[str, Any] = {
    "exchange": "NSE",
    "tradingsymbol": "INFY-EQ",
    "alert_type": "LTP_B_O",
    "trigger_value": "1500",
    "alert_validity": "GTT",
    "tag": "Buy INFY below 1500",
    "transaction_type": "B",
    "order_type": "LMT",
    "product": "C",
    "validity": "DAY",
    "quantity": 1,
    "price": "1500",
}

PLACE_KEYS = [
    "uid",
    "actid",
    "exch",
    "tsym",
    "ai_t",
    "d",
    "validity",
    "remarks",
    "trantype",
    "prctyp",
    "prd",
    "ret",
    "qty",
    "prc",
]


def place(**changes: Any) -> dict[str, str]:
    arguments = {k: v for k, v in {**DOC_EXAMPLE, **changes}.items() if v is not None}
    return place_gtt_call(UID, **arguments).payload


def modify(alert_id: Any = AL_ID, **changes: Any) -> dict[str, str]:
    arguments = {k: v for k, v in {**DOC_EXAMPLE, **changes}.items() if v is not None}
    return modify_gtt_call(UID, alert_id, **arguments).payload


def refused(fn: Any, *args: Any, **kwargs: Any) -> RequestValidationError:
    with pytest.raises(RequestValidationError) as info:
        fn(*args, **kwargs)
    return info.value


def fields(error: RequestValidationError) -> set[str]:
    return {".".join(str(p) for p in entry.get("loc", ())) for entry in error.errors}


# --- the documented example and key order ---------------------------------------------------


def test_place_matches_the_documented_example_exactly() -> None:
    payload = place()
    assert payload == {
        "uid": UID,
        "actid": UID,
        "exch": "NSE",
        "tsym": "INFY-EQ",
        "ai_t": "LTP_B_O",
        "d": "1500",
        "validity": "GTT",
        "remarks": "Buy INFY below 1500",
        "trantype": "B",
        "prctyp": "LMT",
        "prd": "C",
        "ret": "DAY",
        "qty": "1",
        "prc": "1500",
    }
    assert list(payload) == PLACE_KEYS


def test_place_optional_keys_follow_in_order_only_when_given() -> None:
    payload = place(order_type="SL-LMT", trigger_price="1499.5", quantity=10, disclosed_quantity=1)
    assert list(payload) == [*PLACE_KEYS, "trgprc", "dscqty"]
    assert payload["trgprc"] == "1499.5"
    assert payload["dscqty"] == "1"


def test_place_sends_no_ordersource_and_no_algo_id() -> None:
    payload = place(order_type="SL-LMT", trigger_price="1499.5", disclosed_quantity=0)
    assert "ordersource" not in payload
    assert "algo_id" not in payload
    assert "amo" not in payload


def test_place_has_no_algo_id_argument() -> None:
    with pytest.raises(TypeError):
        place_gtt_call(UID, **DOC_EXAMPLE, algo_id="153763")


def test_modify_puts_al_id_third() -> None:
    payload = modify()
    assert list(payload) == ["uid", "actid", "al_id", *PLACE_KEYS[2:]]
    assert payload["al_id"] == AL_ID
    assert "algo_id" not in payload and "ordersource" not in payload


def test_modify_optional_keys_last() -> None:
    payload = modify(order_type="SL-MKT", price=None, trigger_price="1499")
    assert list(payload)[-1] == "trgprc"
    assert payload["prc"] == "0"


def test_cancel_sends_uid_and_al_id_only() -> None:
    assert cancel_gtt_call(UID, AL_ID).payload == {"uid": UID, "al_id": AL_ID}
    assert list(cancel_gtt_call(UID, AL_ID).payload) == ["uid", "al_id"]


def test_read_payloads() -> None:
    assert pending_gtts_call(UID).payload == {"uid": UID}
    assert triggered_gtts_call(UID).payload == {"uid": UID, "actid": UID}
    assert list(triggered_gtts_call(UID).payload) == ["uid", "actid"]
    assert gtt_alert_types_call(UID).payload == {"uid": UID}


# --- defaults and accepted words --------------------------------------------------------------


def test_defaults_validity_day_and_alert_validity_gtt() -> None:
    payload = place(validity=None, alert_validity=None)
    assert payload["ret"] == "DAY"
    assert payload["validity"] == "GTT"


def test_alert_validity_day() -> None:
    assert place(alert_validity=GttValidity.DAY)["validity"] == "DAY"
    assert place(alert_validity="day")["validity"] == "DAY"


def test_validity_eos_and_ioc_go_to_ret() -> None:
    assert place(validity="EOS")["ret"] == "EOS"
    assert place(validity=Validity.IOC)["ret"] == "IOC"


@pytest.mark.parametrize("member", list(GttAlertType))
def test_every_alert_type_is_sent_as_its_wire_code(member: GttAlertType) -> None:
    assert place(alert_type=member)["ai_t"] == member.value
    assert place(alert_type=member.value.lower())["ai_t"] == member.value
    assert place(alert_type=member.name.lower())["ai_t"] == member.value


def test_words_any_case() -> None:
    payload = place(exchange="nse", transaction_type="buy", order_type="limit", product="cnc")
    assert (payload["exch"], payload["trantype"], payload["prctyp"], payload["prd"]) == (
        "NSE",
        "B",
        "LMT",
        "C",
    )


@pytest.mark.parametrize("exchange", ["NSE", "NFO", "BSE", "BFO", "MCX"])
def test_documented_exchanges_are_accepted(exchange: str) -> None:
    assert place(exchange=exchange)["exch"] == exchange


@pytest.mark.parametrize(("product", "wire"), [("CNC", "C"), ("NRML", "M"), ("CO", "H")])
def test_documented_products_are_accepted(product: str, wire: str) -> None:
    assert place(product=product)["prd"] == wire


def test_cover_order_needs_no_book_loss_price() -> None:
    # The GTT request has no blprc field (docs): a CO GTT is sent as documented.
    assert "blprc" not in place(product="CO")


def test_mcx_quantity_is_sent_as_given_in_trade_units() -> None:
    # CRUDEOIL lot 100: 2 lots are 200 units (MCX quantity is in trade units, like NFO).
    assert place(exchange="MCX", tradingsymbol="CRUDEOIL25DECFUT", quantity=200)["qty"] == "200"


def test_market_order_price_defaults_to_zero() -> None:
    assert place(order_type="MARKET", price=None)["prc"] == "0"
    assert place(order_type="SL-M", price=None, trigger_price="1499")["prc"] == "0"


@pytest.mark.parametrize(
    ("value", "wire"),
    [
        ("1500.50", "1500.50"),
        (Decimal("1500.50"), "1500.50"),
        (1500, "1500"),
        (1500.5, "1500.5"),
        ("-5", "-5"),  # no sign rule: the docs give none (a % change can be negative)
        ("0", "0"),
    ],
)
def test_trigger_value_forms(value: Any, wire: str) -> None:
    assert place(trigger_value=value)["d"] == wire


# --- refusals: nothing is built, so nothing is sent ------------------------------------------


@pytest.mark.parametrize("value", ["LTP_X_O", "LTP", "", "LTP_A", 1])
def test_unknown_alert_type_is_refused(value: Any) -> None:
    assert "ai_t" in fields(refused(place_gtt_call, UID, **{**DOC_EXAMPLE, "alert_type": value}))


@pytest.mark.parametrize("value", ["EOS", "IOC", "GTC", "", Validity.EOS])
def test_alert_validity_other_than_day_or_gtt_is_refused(value: Any) -> None:
    error = refused(place_gtt_call, UID, **{**DOC_EXAMPLE, "alert_validity": value})
    assert "validity" in fields(error)


def test_validity_gtt_is_not_a_retention() -> None:
    assert "ret" in fields(refused(place_gtt_call, UID, **{**DOC_EXAMPLE, "validity": "GTT"}))


@pytest.mark.parametrize("exchange", ["CDS", "BCD", "XYZ"])
def test_undocumented_exchange_is_refused(exchange: str) -> None:
    error = refused(place_gtt_call, UID, **{**DOC_EXAMPLE, "exchange": exchange})
    assert "exch" in fields(error)


@pytest.mark.parametrize("product", ["MIS", "I", "BO", "B", "MTF", "F", "DELIVERY"])
def test_undocumented_product_is_refused(product: str) -> None:
    error = refused(place_gtt_call, UID, **{**DOC_EXAMPLE, "product": product})
    assert "prd" in fields(error)


@pytest.mark.parametrize("tag", ["", "   ", None, 5])
def test_blank_or_missing_tag_is_refused(tag: Any) -> None:
    arguments = {**DOC_EXAMPLE, "tag": tag}
    if tag is None:
        del arguments["tag"]
        with pytest.raises(TypeError):
            place_gtt_call(UID, **arguments)
        return
    assert "remarks" in fields(refused(place_gtt_call, UID, **arguments))


@pytest.mark.parametrize("value", [True, False, math.nan, math.inf, "nan", "abc", "", None])
def test_trigger_value_must_be_a_finite_number(value: Any) -> None:
    error = refused(place_gtt_call, UID, **{**DOC_EXAMPLE, "trigger_value": value})
    assert "d" in fields(error)


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"quantity": 0}, "qty"),
        ({"quantity": True}, "qty"),
        ({"quantity": 1.5}, "qty"),
        ({"price": None}, ""),  # LMT without a price (a cross-field rule)
        ({"price": "0"}, ""),
        ({"price": "-1"}, "prc"),
        ({"price": "abc"}, "prc"),
        ({"order_type": "SL-LMT"}, ""),  # no trigger price
        ({"trigger_price": "1499"}, ""),  # trigger price on a LMT order
        ({"quantity": 5, "disclosed_quantity": 6}, ""),
        ({"disclosed_quantity": -1}, "dscqty"),
        ({"order_type": "STOP"}, "prctyp"),
        ({"transaction_type": "HOLD"}, "trantype"),
        ({"tradingsymbol": 22}, "tsym"),
    ],
)
def test_place_order_rules_apply(changes: dict[str, Any], field: str) -> None:
    error = refused(place_gtt_call, UID, **{**DOC_EXAMPLE, **changes})
    if field:
        assert field in fields(error)


@pytest.mark.parametrize("alert_id", ["", "  ", 26111200000010, None])
def test_modify_refuses_a_blank_or_non_string_alert_id(alert_id: Any) -> None:
    assert "al_id" in fields(refused(modify_gtt_call, UID, alert_id, **DOC_EXAMPLE))


@pytest.mark.parametrize("alert_id", ["", "  ", 26111200000010, None])
def test_cancel_refuses_a_blank_or_non_string_alert_id(alert_id: Any) -> None:
    assert "al_id" in fields(refused(cancel_gtt_call, UID, alert_id))


def test_modify_applies_the_place_rules() -> None:
    assert "prd" in fields(
        refused(modify_gtt_call, UID, AL_ID, **{**DOC_EXAMPLE, "product": "MIS"})
    )
    assert "ai_t" in fields(
        refused(modify_gtt_call, UID, AL_ID, **{**DOC_EXAMPLE, "alert_type": "nope"})
    )


def test_refusal_message_never_echoes_the_tag() -> None:
    error = refused(place_gtt_call, UID, **{**DOC_EXAMPLE, "tag": "  ", "product": "MIS"})
    assert "remarks" in str(error) and "prd" in str(error)


# --- endpoint specs -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "word"),
    [
        (PLACE_GTT_ORDER, "Oi created"),
        (MODIFY_GTT_ORDER, "Oi replaced"),
        (CANCEL_GTT_ORDER, "Oi delete success"),
    ],
)
def test_write_specs_are_order_calls_with_their_words(spec: Any, word: str) -> None:
    assert spec.state_changing is True
    assert spec.success_stats == frozenset({word}) == SUCCESS_STATS[spec.name]
    assert spec.requires_ack_id == "al_id" == ACK_ID_FIELDS[spec.name]
    assert spec.stats_ignore_case is True
    assert spec.rate_group is None
    assert spec.expect == "object"
    assert spec.name in STATE_CHANGING_ENDPOINTS


@pytest.mark.parametrize("spec", [GET_PENDING_GTT_ORDER, GET_TRG_OI])
def test_list_reads(spec: Any) -> None:
    assert spec.state_changing is False
    assert spec.expect == "list" and spec.allow_empty is True
    assert spec.success_stats == frozenset({"Ok"})
    assert spec.requires_ack_id is None
    assert spec.stats_ignore_case is False


def test_enabled_alert_types_read() -> None:
    assert GET_ENABLED_GTTS.name == "GetEnabledGTTs"
    assert GET_ENABLED_GTTS.state_changing is False
    assert GET_ENABLED_GTTS.expect == "object"


def test_endpoint_names() -> None:
    assert [s.name for s in (PLACE_GTT_ORDER, MODIFY_GTT_ORDER, CANCEL_GTT_ORDER)] == [
        "PlaceGTTOrder",
        "ModifyGTTOrder",
        "CancelGTTOrder",
    ]
    assert GET_PENDING_GTT_ORDER.name == "GetPendingGTTOrder"
    assert GET_TRG_OI.name == "GetTrgOi"


def test_write_parsers_return_the_alert_id() -> None:
    body = {"request_time": "10:02:06 12-11-2026", "stat": "Oi created", "al_id": AL_ID}
    assert PLACE_GTT_ORDER.parse(body) == AL_ID
    assert MODIFY_GTT_ORDER.parse({**body, "stat": "Oi replaced"}) == AL_ID
    assert CANCEL_GTT_ORDER.parse({**body, "stat": "Oi delete success"}) == AL_ID


@pytest.mark.parametrize(
    "body", [{"stat": "Oi created"}, {"stat": "Oi created", "al_id": " "}, [], "Oi created"]
)
def test_write_parser_without_a_usable_alert_id_is_unknown(body: Any) -> None:
    # The codec checks al_id first; this is the parser's own guard (never a failure).
    with pytest.raises(OrderStatusUnknownError, match=r"pending_gtts\(\)"):
        PLACE_GTT_ORDER.parse(body)


@pytest.mark.parametrize("missing", ["validity", "alert_validity"])
def test_modify_call_requires_both_validities(missing: str) -> None:
    arguments = {k: v for k, v in DOC_EXAMPLE.items() if k != missing}
    with pytest.raises(TypeError, match=missing):
        modify_gtt_call(UID, AL_ID, **arguments)
    assert place_gtt_call(UID, **arguments).payload  # place keeps its defaults
