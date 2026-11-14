import json
import logging
from decimal import Decimal

import pytest

from tests.conftest import fixture
from tradesmart.enums import Exchange
from tradesmart.errors import RequestValidationError, SubscriptionLimitError
from tradesmart.models.stream import (
    ConnectAck,
    DepthAck,
    DepthFeed,
    HeartbeatAck,
    OrderUpdate,
    PositionAck,
    PositionUpdate,
    TouchlineAck,
    TouchlineFeed,
    UnknownMessage,
    UnsubscribeAck,
)
from tradesmart.stream.protocol import (
    as_instrument_list,
    connect_msg,
    depth_msg,
    format_keys,
    heartbeat_msg,
    order_updates_msg,
    parse_message,
    position_updates_msg,
    touchline_msg,
    unsubscribe_depth_msg,
    unsubscribe_orders_msg,
    unsubscribe_positions_msg,
    unsubscribe_touchline_msg,
)
from tradesmart.stream.state import (
    MAX_SUBSCRIPTIONS,
    SnapshotCache,
    StreamState,
    SubscriptionRegistry,
)


def test_connect_and_subscription_messages():
    assert connect_msg("TEST123", "tok") == {
        "t": "a",
        "uid": "TEST123",
        "actid": "TEST123",
        "source": "API",
        "accesstoken": "tok",
    }
    keys = format_keys(["NSE|22", (Exchange.BSE, "508123"), "NSE|NIFTY"])
    assert touchline_msg(keys) == {"t": "t", "k": "NSE|22#BSE|508123#NSE|NIFTY"}
    assert order_updates_msg("TEST123") == {"t": "o", "actid": "TEST123"}
    assert heartbeat_msg() == {"t": "h"}
    with pytest.raises(RequestValidationError):
        format_keys(["NSE22"])


def test_other_request_messages():
    assert unsubscribe_touchline_msg(["NSE|22"]) == {"t": "u", "k": "NSE|22"}
    assert depth_msg(["NSE|22", "BSE|1"]) == {"t": "d", "k": "NSE|22#BSE|1"}
    assert unsubscribe_depth_msg(["NSE|22"]) == {"t": "ud", "k": "NSE|22"}
    assert unsubscribe_orders_msg() == {"t": "uo"}
    assert position_updates_msg("TEST123") == {"t": "p", "uid": "TEST123"}
    assert unsubscribe_positions_msg() == {"t": "up"}


def test_format_keys_dedupes_and_validates():
    assert format_keys(["NSE|22", ("NSE", "22"), "NSE|23"]) == ["NSE|22", "NSE|23"]
    for bad in ["XXX|22", "NSE|", "NSE|a#b", "NSE|a b", "NSE|a|b", ("NSE", ""), ("ZZZ", "1"), 5]:
        with pytest.raises(RequestValidationError):
            format_keys([bad])  # type: ignore[list-item]


@pytest.mark.parametrize(
    "t,cls",
    [
        ("ak", ConnectAck),
        ("tk", TouchlineAck),
        ("tf", TouchlineFeed),
        ("dk", DepthAck),
        ("df", DepthFeed),
        ("om", OrderUpdate),
        ("pk", PositionAck),
        ("pm", PositionUpdate),
        ("uk", UnsubscribeAck),
        ("udk", UnsubscribeAck),
        ("uok", UnsubscribeAck),
        ("upk", UnsubscribeAck),
        ("hk", HeartbeatAck),
    ],
)
def test_parse_each_type(t, cls):
    assert isinstance(parse_message(json.dumps(fixture("ws_messages")[t])), cls)


def test_parse_bytes_and_mapping():
    data = fixture("ws_messages")["tk"]
    assert isinstance(parse_message(json.dumps(data).encode()), TouchlineAck)
    assert isinstance(parse_message(data), TouchlineAck)


def test_connect_ack_with_uppercase_ok_parses() -> None:
    # Observed live 2026-11-10: the server sends s="OK" (the docs show "Ok").
    ak = parse_message(fixture("ws_messages")["ak_upper_ok"])
    assert isinstance(ak, ConnectAck) and ak.status == "OK" and ak.user_id == "TEST123"


def test_unsubscribe_ack_variants():
    for t in ("uk", "udk", "uok", "upk"):
        msg = parse_message({"t": t})
        assert isinstance(msg, UnsubscribeAck) and msg.t == t


def test_depth_feed_values_and_key():
    m = parse_message(fixture("ws_messages")["df"])
    assert isinstance(m, DepthFeed)
    assert m.key == "NSE|22" and m.bp1 == Decimal("1156.00") and m.bq2 == 67


def test_depth_ack_aliases_and_order_position_fields():
    msgs = fixture("ws_messages")
    dk = parse_message(msgs["dk"])
    assert isinstance(dk, DepthAck)
    assert dk.high_52w == Decimal(msgs["dk"]["52h"]) and dk.low_52w == Decimal(msgs["dk"]["52l"])
    om = parse_message(msgs["om"])
    assert isinstance(om, OrderUpdate)
    assert om.order_id == msgs["om"]["norenordno"] and om.quantity == int(msgs["om"]["qty"])
    pm = parse_message(msgs["pm"])
    assert isinstance(pm, PositionUpdate)
    assert (
        pm.net_quantity == int(msgs["pm"]["netqty"])
        and pm.child_orders == msgs["pm"]["child_orders"]
    )
    ak = parse_message(msgs["ak"])
    assert isinstance(ak, ConnectAck) and ak.status == "Ok" and ak.user_id == "TEST123"


def test_parse_garbage_never_raises():
    assert isinstance(parse_message("not json"), UnknownMessage)
    assert parse_message("not json").raw == {"text": "not json"}
    assert isinstance(parse_message({"t": "zz"}), UnknownMessage)
    assert isinstance(parse_message("[1, 2]"), UnknownMessage)
    assert isinstance(parse_message("{}"), UnknownMessage)
    assert isinstance(parse_message({"t": 5}), UnknownMessage)
    assert isinstance(parse_message(b"\xff\xfe"), UnknownMessage)
    bad = parse_message({"t": "tf", "e": "NSE", "tk": "22", "lp": "abc"})
    assert isinstance(bad, UnknownMessage) and bad.raw["lp"] == "abc"


@pytest.mark.parametrize(
    "frame",
    [
        {"t": "tf", "e": "NSE", "tk": "22", "ft": "inf"},
        '{"t":"tf","e":"NSE","tk":"22","ft":"inf"}',
        {"t": "df", "e": "NSE", "tk": "22", "ft": "-Infinity"},
    ],
)
def test_parse_overflowing_epoch_never_raises(frame):
    msg = parse_message(frame)
    assert isinstance(msg, UnknownMessage) and msg.t in ("tf", "df")


def test_parse_failure_logs_type_and_fields_only(caplog):
    with caplog.at_level(logging.WARNING, logger="tradesmart"):
        parse_message({"t": "tf", "e": "NSE", "tk": "22", "lp": "secret-ish-9z"})
    records = [r for r in caplog.records if r.name == "tradesmart.stream"]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    text = records[0].getMessage()
    assert "tf" in text and "lp" in text and "secret-ish-9z" not in text


def test_registry_limit_and_restore():
    reg = SubscriptionRegistry()
    reg.add_touchline([f"NSE|{i}" for i in range(999)])
    reg.ensure_capacity(depth_add=["NSE|5000"])
    with pytest.raises(SubscriptionLimitError):
        reg.ensure_capacity(touchline_add=["NSE|9000", "NSE|9001"])
    reg.orders = True
    assert [m["t"] for m in reg.restore_messages("TEST123")] == ["t", "o"]
    assert MAX_SUBSCRIPTIONS == 1000


def test_registry_union_counting_and_restore_order():
    reg = SubscriptionRegistry()
    reg.add_touchline(["NSE|2", "NSE|1"])
    reg.ensure_capacity(touchline_add=["NSE|1"])  # already present: no growth
    reg.add_depth(["NSE|3"])
    reg.orders = reg.positions = True
    assert reg.restore_messages("TEST123") == [
        {"t": "t", "k": "NSE|1#NSE|2"},
        {"t": "d", "k": "NSE|3"},
        {"t": "o", "actid": "TEST123"},
        {"t": "p", "uid": "TEST123"},
    ]
    reg.remove_touchline(["NSE|1", "NSE|2"])
    reg.remove_depth(["NSE|3"])
    assert [m["t"] for m in reg.restore_messages("TEST123")] == ["o", "p"]


def test_stream_state_values():
    assert [s.name for s in StreamState] == [
        "DISCONNECTED",
        "CONNECTING",
        "AUTHENTICATING",
        "CONNECTED",
        "RECONNECTING",
        "CLOSED",
    ]


def test_snapshot_merges_deltas():
    cache = SnapshotCache()
    cache.update({"t": "tk", "e": "NSE", "tk": "22", "lp": "100", "v": "5"})
    cache.update({"t": "tf", "e": "NSE", "tk": "22", "lp": "101"})
    assert cache.get("NSE|22") == {"e": "NSE", "tk": "22", "lp": "101", "v": "5"}
    cache.update({"t": "tf", "lp": "1"})  # no e/tk: ignored
    snap = cache.get("NSE|22")
    assert snap is not None
    snap["lp"] = "999"
    assert cache.get("NSE|22")["lp"] == "101"  # type: ignore[index]
    assert cache.get("NSE|1") is None


def test_om_numeric_ids_parse():
    msg = parse_message(
        '{"t":"om","norenordno":26110700000001,"exchordid":1100000000012345,"status":"OPEN"}'
    )
    assert isinstance(msg, OrderUpdate)
    assert msg.order_id == "26110700000001"
    assert msg.exchange_order_id == "1100000000012345"


def test_touchline_numeric_token_parses():
    msg = parse_message('{"t":"tf","e":"NSE","tk":22}')
    assert isinstance(msg, TouchlineFeed)
    assert msg.token == "22"


def test_ato_price_becomes_none():
    msg = parse_message('{"t":"tf","e":"NSE","tk":"22","bp1":"42949672.95","lp":"1156.00"}')
    assert isinstance(msg, TouchlineFeed)
    assert msg.bp1 is None
    assert msg.last_price == Decimal("1156.00")
    assert msg.raw["bp1"] == "42949672.95"


_ATO = "42949672.95"


_TOUCHLINE_PRICES = ["lp", "o", "h", "l", "c", "ap", "bp1", "sp1"]
_DEPTH_PRICES = [*_TOUCHLINE_PRICES, "lc", "uc", "52h", "52l"] + [
    f"{side}{n}" for side in ("bp", "sp") for n in range(2, 6)
]


@pytest.mark.parametrize(("t", "keys"), [("tf", _TOUCHLINE_PRICES), ("df", _DEPTH_PRICES)])
def test_ato_applies_to_every_quote_and_depth_price_field(t, keys):
    msg = parse_message(json.dumps({"t": t, "e": "NSE", "tk": "22", **dict.fromkeys(keys, _ATO)}))
    dumped = msg.model_dump(by_alias=True)
    for key in keys:
        assert dumped[key] is None, key
        assert msg.raw[key] == _ATO


def test_ato_applies_to_order_update_prices():
    msg = parse_message(
        json.dumps(
            {
                "t": "om",
                "norenordno": "1",
                "prc": _ATO,
                "trgprc": _ATO,
                "avgprc": _ATO,
                "flprc": _ATO,
            }
        )
    )
    assert isinstance(msg, OrderUpdate)
    assert (msg.price, msg.trigger_price, msg.average_price, msg.flprc) == (None,) * 4


def test_ato_sentinel_is_a_real_value_on_money_amounts():
    amounts = ("rpnl", "daybuyamt", "daysellamt", "totbuyamt", "cfbuyamt", "openbuyamt")
    msg = parse_message(json.dumps({"t": "pm", **dict.fromkeys(amounts, _ATO)}))
    assert isinstance(msg, PositionUpdate)
    assert msg.realized_pnl == Decimal(_ATO)
    assert msg.daybuyamt == msg.daysellamt == msg.totbuyamt == Decimal(_ATO)
    assert msg.cfbuyamt == msg.openbuyamt == Decimal(_ATO)


@pytest.mark.parametrize("bad", [22, None, 1.5, object()])
@pytest.mark.parametrize("convert", [as_instrument_list, format_keys])
def test_non_iterable_instruments_raise_request_validation_error(convert, bad):
    with pytest.raises(RequestValidationError) as info:
        convert(bad)
    assert str(info.value) == (
        "Invalid request parameters: instruments.0 "
        "(expected 'EXCH|TOKEN', (exchange, token) or a list of them)"
    )
