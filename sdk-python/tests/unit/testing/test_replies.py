"""Every reply the offline broker builds carries the fixture's keys and parses with the SDK."""

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from tests.conftest import fixture
from tradesmart.endpoints.market import (
    EOD_CHART_DATA,
    GET_OPTION_CHAIN,
    GET_QUOTES,
    GET_SECURITY_INFO,
    SEARCH_SCRIP,
    TP_SERIES,
)
from tradesmart.endpoints.orders import (
    CANCEL_ORDER,
    MODIFY_ORDER,
    ORDER_BOOK,
    PLACE_ORDER,
    SINGLE_ORD_HIST,
    TRADE_BOOK,
)
from tradesmart.endpoints.portfolio import HOLDINGS, LIMITS, POSITION_BOOK
from tradesmart.endpoints.user import USER_DETAILS
from tradesmart.enums import OrderStatus
from tradesmart.models import base
from tradesmart.models.stream import (
    ConnectAck,
    DepthAck,
    DepthFeed,
    HeartbeatAck,
    OrderUpdate,
    PositionUpdate,
    TouchlineAck,
    TouchlineFeed,
)
from tradesmart.stream.protocol import parse_message
from tradesmart.testing import _replies
from tradesmart.testing._state import OfflineOrder

WHEN = datetime(2026, 11, 10, 10, 30, 15, tzinfo=base.IST)
CLIENT = "OFFLINE01"


def _common_keys(rows: list[dict[str, Any]]) -> set[str]:
    """Keys present in every row of a list fixture (the shape every row is promised to have)."""
    return set.intersection(*(set(row) for row in rows))


@pytest.fixture
def instrument() -> dict[str, Any]:
    return _replies.instrument_row(
        "NFO",
        "43210",
        "NIFTY25NOV24000CE",
        lot_size=75,
        tick_size="0.05",
        instrument="OPTIDX",
        expiry=date(2025, 11, 25),
        strike="24000",
        option_type="CE",
        underlying="NIFTY",
        extra={},
    )


@pytest.fixture
def order() -> OfflineOrder:
    return OfflineOrder(
        id="26111000000001",
        exchange="NFO",
        symbol="NIFTY25NOV24000CE",
        side="B",
        quantity=75,
        price=Decimal("101.50"),
        trigger_price=Decimal("101.00"),
        order_type="SL-LMT",
        product="M",
        status="OPEN",
        filled_quantity=25,
        average_price=Decimal("101.45"),
        payload={"ret": "DAY", "remarks": "tag-1"},
    )


def test_instrument_row_matches_search_scrip_values(instrument: dict[str, Any]) -> None:
    assert _common_keys(fixture("search_scrip_success")["values"]) <= set(instrument)
    assert SEARCH_SCRIP.parse({"stat": "Ok", "values": [instrument]})[0].lot_size == 75


def test_search_scrip(instrument: dict[str, Any]) -> None:
    reply = _replies.search_scrip([instrument])
    assert set(fixture("search_scrip_success")) <= set(reply)
    (match,) = SEARCH_SCRIP.parse(reply)
    assert (match.tradingsymbol, match.token, match.tick_size) == (
        "NIFTY25NOV24000CE",
        "43210",
        Decimal("0.05"),
    )


def test_order_row(order: OfflineOrder, instrument: dict[str, Any]) -> None:
    row = _replies.order_row(order, client_id=CLIENT, instrument=instrument, when=WHEN)
    assert _common_keys(fixture("order_book_success")) <= set(row)
    (parsed,) = ORDER_BOOK.parse([row])
    assert parsed.order_id == order.id
    assert parsed.status is OrderStatus.OPEN
    assert (parsed.price, parsed.trigger_price, parsed.quantity) == (
        Decimal("101.50"),
        Decimal("101.00"),
        75,
    )
    assert (parsed.filled_quantity, parsed.average_price) == (25, Decimal("101.45"))
    assert parsed.token == "43210"
    assert parsed.norentm == datetime(2026, 11, 10, 10, 30, 15, tzinfo=base.IST)
    assert parsed.tag == "tag-1"


def test_order_row_without_instrument(order: OfflineOrder) -> None:
    row = _replies.order_row(order, client_id=CLIENT, instrument=None, when=WHEN)
    (parsed,) = ORDER_BOOK.parse([row])
    assert parsed.token == ""
    assert parsed.lot_size == 1


def test_history_row(order: OfflineOrder, instrument: dict[str, Any]) -> None:
    # Both fixture rows are of a bracket order, whose leg prices are sent only for such orders.
    order.payload.update(blprc="2", bpprc="2", trailprc="1")
    row = _replies.history_row(
        order, client_id=CLIENT, instrument=instrument, when=WHEN, report="New"
    )
    assert _common_keys(fixture("single_ord_hist_success")) <= set(row)
    (parsed,) = SINGLE_ORD_HIST.parse([row])
    assert (parsed.order_id, parsed.rpt) == (order.id, "New")
    assert (parsed.book_loss_price, parsed.trail_price) == (Decimal("2.00"), Decimal("1.00"))


def test_order_row_has_bracket_legs_only_when_sent(
    order: OfflineOrder, instrument: dict[str, Any]
) -> None:
    row = _replies.order_row(order, client_id=CLIENT, instrument=instrument, when=WHEN)
    assert not {"blprc", "bpprc", "trailprc"} & set(row)


def test_order_book_row_is_the_latest_history_row(
    order: OfflineOrder, instrument: dict[str, Any]
) -> None:
    order.history.insert(
        0,
        _replies.history_row(
            order, client_id=CLIENT, instrument=instrument, when=WHEN, report="New"
        ),
    )
    row = _replies.order_book_row(order)
    assert row == _replies.order_row(order, client_id=CLIENT, instrument=instrument, when=WHEN)
    assert "rpt" not in row


def test_trade_row(order: OfflineOrder, instrument: dict[str, Any]) -> None:
    row = _replies.trade_row(
        order,
        client_id=CLIENT,
        instrument=instrument,
        when=WHEN,
        fill_id="1",
        quantity=25,
        price=Decimal("101.45"),
    )
    assert _common_keys(fixture("trade_book_success")) <= set(row)
    (parsed,) = TRADE_BOOK.parse([row])
    assert (parsed.order_id, parsed.flqty, parsed.flprc) == (order.id, 25, Decimal("101.45"))
    assert parsed.fltm == WHEN.replace(microsecond=0)


def test_position_row(instrument: dict[str, Any]) -> None:
    row = _replies.position_row(
        client_id=CLIENT,
        exchange="NFO",
        token="43210",
        symbol="NIFTY25NOV24000CE",
        product="M",
        instrument=instrument,
    )
    assert _common_keys(fixture("position_book_success")) <= set(row)
    (parsed,) = POSITION_BOOK.parse([row])
    assert (parsed.net_quantity, parsed.lot_size) == (0, 75)


def test_quote(instrument: dict[str, Any]) -> None:
    reply = _replies.quote("NFO", "43210", instrument, {"lp": "101.50", "v": "10"}, WHEN)
    assert set(fixture("get_quotes_success")) <= set(reply)
    parsed = GET_QUOTES.parse(reply)
    assert (parsed.last_price, parsed.volume, parsed.lot_size) == (Decimal("101.50"), 10, 75)
    assert parsed.tradingsymbol == "NIFTY25NOV24000CE"


def test_quote_without_instrument() -> None:
    reply = _replies.quote("NSE", "1", None, {"lp": "10.00"}, WHEN)
    parsed = GET_QUOTES.parse(reply)
    assert (parsed.exchange, parsed.token, parsed.last_price) == ("NSE", "1", Decimal("10.00"))


def test_security_info(instrument: dict[str, Any]) -> None:
    reply = _replies.security_info(instrument, WHEN)
    assert set(fixture("security_info_success")) <= set(reply)
    parsed = GET_SECURITY_INFO.parse(reply)
    assert (parsed.lot_size, parsed.optt, parsed.strprc) == (75, "CE", Decimal("24000.00"))


def test_option_chain(instrument: dict[str, Any]) -> None:
    reply = _replies.option_chain([instrument])
    expected = fixture("option_chain_success")
    assert set(expected) <= set(reply)
    assert _common_keys(expected["values"]) <= set(reply["values"][0])
    (contract,) = GET_OPTION_CHAIN.parse(reply)
    assert (contract.option_type, contract.strike_price) == ("CE", Decimal("24000.00"))


def test_limits() -> None:
    reply = _replies.limits({"cash": "100000.00"}, WHEN)
    assert set(fixture("limits_success")) <= set(reply)
    assert LIMITS.parse(reply).cash == Decimal("100000.00")


def test_holding_row() -> None:
    entry = {"exch": "NSE", "tsym": "INFY-EQ", "token": "1594"}
    row = _replies.holding_row({"holdqty": 10, "exch_tsym": [entry]})
    assert _common_keys(fixture("holdings_success")) <= set(row)
    (parsed,) = HOLDINGS.parse([row])
    assert parsed.instruments is not None
    assert parsed.instruments[0].tradingsymbol == "INFY-EQ"
    assert row["holdqty"] == "10"


def test_user_details() -> None:
    reply = _replies.user_details(CLIENT, {"uname": "Strategy Tester"}, WHEN)
    assert set(fixture("user_details_success")) <= set(reply)
    parsed = USER_DETAILS.parse(reply)
    assert (parsed.user_id, parsed.account_id, parsed.uname) == (CLIENT, CLIENT, "Strategy Tester")


def test_candle_row() -> None:
    candle = {"time": datetime(2026, 11, 10, 9, 15), "open": "100", "high": "101", "low": "99"}
    candle.update(close="100.5", volume=1200, oi=0)
    row = _replies.candle_row(candle)
    assert _common_keys(fixture("tpseries_success")) <= set(row)
    (parsed,) = TP_SERIES.parse([row])
    assert parsed.time == datetime(2026, 11, 10, 9, 15, tzinfo=base.IST)
    assert (parsed.into, parsed.intc, parsed.intv) == (Decimal("100"), Decimal("100.5"), 1200)


def test_eod_row() -> None:
    candle = {"time": date(2026, 11, 10), "open": "100", "high": "101", "low": "99"}
    candle.update(close="100.5", volume=1200)
    text = _replies.eod_row(candle)
    expected = _common_keys([json.loads(item) for item in fixture("eod_chart_success")])
    assert expected <= set(json.loads(text))
    (parsed,) = EOD_CHART_DATA.parse([text])
    assert parsed.time == datetime(2026, 11, 10, tzinfo=base.IST)
    assert parsed.intc == Decimal("100.5")


def test_place_ack() -> None:
    reply = _replies.place_ack("26111000000001", WHEN)
    assert set(fixture("place_order_success")) <= set(reply)
    assert PLACE_ORDER.parse(reply).order_id == "26111000000001"


def test_order_ack_has_the_live_shape() -> None:
    reply = _replies.order_ack("26111000000001", WHEN)
    assert {"stat", "request_time"} <= set(reply)  # live: no dmsg, the order id in result
    assert reply["stat"] == "Ok"
    assert reply["result"] == "26111000000001"
    assert CANCEL_ORDER.parse(reply).raw["result"] == "26111000000001"
    assert MODIFY_ORDER.parse(reply).raw["result"] == "26111000000001"


def test_not_ok() -> None:
    reply = _replies.not_ok("Session Expired :  Invalid Session Key", WHEN)
    assert set(fixture("errors")["no_data"]) <= set(reply)
    assert (reply["stat"], reply["emsg"]) == ("Not_Ok", "Session Expired :  Invalid Session Key")


def _ws(name: str) -> set[str]:
    return set(fixture("ws_messages")[name])


def test_connect_ack() -> None:
    ok = _replies.connect_ack(CLIENT, ok=True)
    assert _ws("ak_upper_ok") <= set(ok)
    assert ok["s"] == "OK"
    assert isinstance(parse_message(ok), ConnectAck)
    assert _replies.connect_ack(CLIENT, ok=False)["s"] == "Not_Ok"


def test_touchline_and_depth_acks(instrument: dict[str, Any]) -> None:
    fields = {"lp": "101.50", "bp1": "101.45"}
    tk = _replies.touchline_ack("NFO", "43210", instrument, fields, WHEN)
    dk = _replies.depth_ack("NFO", "43210", instrument, fields, WHEN)
    assert _ws("tk") <= set(tk)
    assert _ws("dk") <= set(dk)
    parsed_tk, parsed_dk = parse_message(tk), parse_message(dk)
    assert isinstance(parsed_tk, TouchlineAck)
    assert isinstance(parsed_dk, DepthAck)
    assert parsed_tk.last_price == Decimal("101.50")
    assert parsed_dk.bp1 == Decimal("101.45")
    assert parsed_tk.lot_size == 75


def test_touchline_and_depth_feeds() -> None:
    fields = {"lp": "101.50", "pc": "0.50", "v": "10", "bq1": "75", "bp1": "101.45"}
    fields.update(o="100", h="102", l="99", c="101", ap="100.75", tbq="0", tsq="0")
    fields.update(sp1="101.55", bp2="0.00", sp2="0.00", sq1="75", bq2="0", sq2="0")
    tf = _replies.touchline_feed("NFO", "43210", fields, WHEN)
    df = _replies.depth_feed("NFO", "43210", fields, WHEN)
    assert _ws("tf") <= set(tf)
    assert _ws("df") <= set(df)
    assert isinstance(parse_message(tf), TouchlineFeed)
    assert isinstance(parse_message(df), DepthFeed)


def test_order_update(order: OfflineOrder) -> None:
    message = _replies.order_update(
        order, client_id=CLIENT, when=WHEN, report="Fill", fill=("7", 25, Decimal("101.45"))
    )
    assert _ws("om") <= set(message)
    parsed = parse_message(message)
    assert isinstance(parsed, OrderUpdate)
    assert (parsed.order_id, parsed.status, parsed.flqty) == (order.id, OrderStatus.OPEN, 25)


def test_order_update_without_fill(order: OfflineOrder) -> None:
    message = _replies.order_update(order, client_id=CLIENT, when=WHEN, report="New")
    parsed = parse_message(message)
    assert isinstance(parsed, OrderUpdate)
    assert parsed.flqty is None


def test_position_update(instrument: dict[str, Any]) -> None:
    row = _replies.position_row(
        client_id=CLIENT,
        exchange="NFO",
        token="43210",
        symbol="NIFTY25NOV24000CE",
        product="M",
        instrument=instrument,
    )
    message = _replies.position_update(row)
    assert _ws("pm") <= set(message)
    parsed = parse_message(message)
    assert isinstance(parsed, PositionUpdate)
    assert parsed.net_quantity == 0


def test_heartbeat_ack() -> None:
    message = _replies.heartbeat_ack(WHEN)
    assert _ws("hk") <= set(message)
    assert isinstance(parse_message(message), HeartbeatAck)


def test_price_text() -> None:
    assert _replies.price_text(Decimal("101.5")) == "101.50"
    assert _replies.price_text(Decimal("0.0025")) == "0.0025"
    assert _replies.price_text(None) == "0.00"
    assert _replies.price_text(None, empty="") == ""


def test_wire_value() -> None:
    assert _replies.wire_value(101.5) == "101.5"
    assert _replies.wire_value(Decimal("2.50")) == "2.50"
    assert _replies.wire_value(75) == "75"
    assert _replies.wire_value(date(2026, 11, 10)) == "2026-11-10"
    assert _replies.wire_value(["NSE"]) == ["NSE"]
    assert _replies.wire_value(None) is None


def test_future_instrument_row_has_optt_xx() -> None:
    row = _replies.instrument_row(
        "NFO",
        "35001",
        "NIFTY25NOVFUT",
        lot_size=75,
        tick_size="0.10",
        instrument="FUTIDX",
        expiry=date(2025, 11, 25),
        strike=None,
        option_type=None,
        underlying="NIFTY",
        extra={},
    )
    assert (row["optt"], row["exd"], row["ti"]) == ("XX", "25-NOV-2025", "0.10")
    assert "strprc" not in row


def test_candle_time_text_is_passed_through() -> None:
    row = _replies.candle_row({"time": "10-11-2026 09:15:00", "ssboe": "1"})
    assert (row["time"], row["ssboe"]) == ("10-11-2026 09:15:00", "1")
    eod = json.loads(_replies.eod_row({"time": "10-NOV-2026"}))
    assert (eod["time"], eod["ssboe"]) == ("10-NOV-2026", "0")


@pytest.mark.parametrize(
    ("name", "word"),
    [
        ("place_gtt_success", "Oi created"),
        ("modify_gtt_success", "Oi replaced"),
        ("cancel_gtt_success", "Oi delete success"),
    ],
)
def test_gtt_ack(name: str, word: str) -> None:
    reply = _replies.gtt_ack(word, "26111200000010", WHEN)
    assert set(fixture(name)) <= set(reply)
    assert reply["stat"] == fixture(name)["stat"]


_GTT_STATE = {
    "ai_t": "LTP_B_O",
    "al_id": "26111200000010",
    "tsym": "INFY-EQ",
    "exch": "NSE",
    "remarks": "Buy INFY below 1500",
    "validity": "GTT",
    "trantype": "B",
    "prctyp": "LMT",
    "qty": 1,
    "prc": "1500",
    "prd": "C",
    "ret": "DAY",
    "d": "1500",
}


def test_gtt_row() -> None:
    row = _replies.gtt_row(_GTT_STATE, client_id=CLIENT, instrument=None)
    expected = _common_keys(fixture("pending_gtts_success")) - {"trgprc"}
    assert expected <= set(row)


def test_triggered_gtt_row() -> None:
    row = _replies.triggered_gtt_row(
        {**_GTT_STATE, "norentm": WHEN}, client_id=CLIENT, instrument=None
    )
    assert _common_keys(fixture("triggered_gtts_success")) <= set(row)


def test_enabled_gtts() -> None:
    reply = _replies.enabled_gtts(WHEN)
    assert set(fixture("enabled_gtts_success")) <= set(reply)
    assert reply["ai_ts"] == fixture("enabled_gtts_success")["ai_ts"]
