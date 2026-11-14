"""Lenient response parsing: one odd field never drops a record (review 2026-11-10 C2)."""

import logging
from typing import Any, ClassVar

import pytest

from tradesmart.errors import ResponseParseError
from tradesmart.models.base import TradeSmartModel
from tradesmart.models.orders import Order, OrderHistoryEntry, Trade
from tradesmart.models.portfolio import Holding, Position
from tradesmart.models.stream import (
    OrderUpdate,
    PositionAck,
    PositionUpdate,
    TouchlineFeed,
    UnknownMessage,
)
from tradesmart.stream.protocol import parse_message

OM = {
    "t": "om",
    "norenordno": "24111000000001",
    "uid": "TEST123",
    "exch": "NSE",
    "tsym": "INFY-EQ",
    "qty": "10",
    "prc": "1584.00",
    "status": "OPEN",
    "tm": 1731234567,
    "ntm": 1731234567.5,
    "snoordt": 0,
    "pcode": 1,
    "reporttype": 2,
    "fltm": 3,
}


def test_order_update_with_numeric_text_fields_is_parsed():
    msg = parse_message(OM)
    assert isinstance(msg, OrderUpdate) and not isinstance(msg, UnknownMessage)
    assert msg.order_id == "24111000000001"
    assert msg.tm == "1731234567" and msg.ntm == "1731234567.5"
    assert msg.snoordt == "0" and msg.pcode == "1"
    assert msg.reporttype == "2" and msg.fltm == "3"


def test_touchline_feed_numeric_percent_change_is_string():
    msg = parse_message({"t": "tf", "e": "NSE", "tk": "22", "pc": 0.3})
    assert isinstance(msg, TouchlineFeed)
    assert msg.percent_change == "0.3"


@pytest.mark.parametrize(
    "model,payload",
    [
        (Order, {"norenordno": "1", "token": 22}),
        (OrderHistoryEntry, {"norenordno": "1", "token": 22, "rpt": 5}),
        (Trade, {"norenordno": "1", "token": 22, "flid": 7}),
        (Position, {"tsym": "X-EQ", "token": 22, "dname": 1}),
    ],
)
def test_rest_free_text_fields_accept_numbers(model, payload):
    item = model.from_wire(payload)
    assert item.token == "22"


_BAD_FIELD = [
    (Order, {"norenordno": "1", "prc": "abc-9z"}, "prc", "price"),
    (OrderHistoryEntry, {"norenordno": "1", "qty": "1.59z"}, "qty", "quantity"),
    (Trade, {"norenordno": "1", "flprc": "abc-9z"}, "flprc", "flprc"),
    (Position, {"tsym": "X-EQ", "netqty": "abc-9z"}, "netqty", "net_quantity"),
    (Holding, {"holdqty": "abc-9z", "upldprc": "1.5"}, "holdqty", "holdqty"),
    (OrderUpdate, {"t": "om", "norenordno": "1", "prc": "abc-9z"}, "prc", "price"),
    (PositionUpdate, {"t": "pm", "tsym": "X-EQ", "netqty": "abc-9z"}, "netqty", "net_quantity"),
    (PositionAck, {"t": "pk", "uid": {"v": "zz-9z"}}, "uid", "user_id"),
]


@pytest.mark.parametrize("model,payload,wire,attr", _BAD_FIELD)
def test_lenient_models_null_only_the_failing_field(model, payload, wire, attr, caplog):
    with caplog.at_level(logging.WARNING, logger="tradesmart"):
        item = model.from_wire(payload, endpoint="X")
    assert isinstance(item, model)
    assert getattr(item, attr) is None
    assert item.raw == payload and item.raw[wire] == payload[wire]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert wire in text and "9z" not in caplog.text


def test_lenient_keeps_other_fields():
    item = Order.from_wire({"norenordno": "7", "prc": "abc", "qty": "3", "tsym": "INFY-EQ"})
    assert item.order_id == "7" and item.quantity == 3 and item.tradingsymbol == "INFY-EQ"


def test_strict_models_still_raise():
    with pytest.raises(ResponseParseError):
        TouchlineFeed.from_wire({"t": "tf", "lp": "abc"})


def test_lenient_second_failure_raises_as_before(caplog):
    class Strictish(TradeSmartModel):
        lenient_parsing: ClassVar[bool] = True
        a: int

    caplog.set_level(logging.WARNING, logger="tradesmart")
    with pytest.raises(ResponseParseError) as info:
        Strictish.from_wire({"a": "x-secret"}, endpoint="E")
    assert "(fields: a)" in info.value.message
    assert "x-secret" not in caplog.text


def test_lenient_stream_frame_logs_fields_not_values(caplog):
    frame: dict[str, Any] = dict(OM, prc="secret-ish-9z")
    with caplog.at_level(logging.WARNING, logger="tradesmart"):
        msg = parse_message(frame)
    assert isinstance(msg, OrderUpdate) and msg.price is None and msg.order_id == OM["norenordno"]
    assert "secret-ish-9z" not in caplog.text and "prc" in caplog.text


# --- identity fields are never nulled (fix round 1, item 1) -----------------------------


@pytest.mark.parametrize(
    "bad",
    [{"norenordno": True}, {"norenordno": {"x": 1}}, {"status": {"x": 1}}],
    ids=["order-id-bool", "order-id-object", "status-object"],
)
def test_order_update_with_bad_identity_field_is_unknown(bad):
    msg = parse_message(dict(OM, **bad))
    assert isinstance(msg, UnknownMessage) and msg.t == "om"


def test_order_update_with_bad_identity_and_other_field_is_unknown():
    msg = parse_message(dict(OM, norenordno=True, prc="abc"))
    assert isinstance(msg, UnknownMessage)


def test_order_update_with_bad_tm_is_still_lenient():
    msg = parse_message(dict(OM, tm={"x": 1}))
    assert isinstance(msg, OrderUpdate) and msg.tm is None and msg.order_id == OM["norenordno"]


@pytest.mark.parametrize("model", [Order, OrderHistoryEntry, Trade])
def test_rest_row_with_bad_order_id_raises(model):
    with pytest.raises(ResponseParseError) as info:
        model.from_wire({"norenordno": True, "prc": "1"}, endpoint="OrderBook")
    assert "norenordno" in info.value.message


def test_rest_order_with_bad_status_raises():
    with pytest.raises(ResponseParseError):
        Order.from_wire({"norenordno": "1", "status": {"x": 1}}, endpoint="OrderBook")


def test_protected_keys_default():
    assert TradeSmartModel.lenient_protected_keys == frozenset({"norenordno", "status"})
