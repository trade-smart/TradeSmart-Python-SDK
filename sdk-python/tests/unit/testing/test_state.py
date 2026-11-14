import threading
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from tests.conftest import fixture
from tradesmart.testing import OfflineRequest, drills
from tradesmart.testing._state import BrokerState

SYMBOL = "NIFTY25NOV24000CE"


def _payload(**overrides: str) -> dict[str, Any]:
    base = {
        "uid": "OFFLINE01",
        "actid": "OFFLINE01",
        "exch": "NFO",
        "tsym": SYMBOL,
        "qty": "75",
        "prc": "101.50",
        "prd": "M",
        "trantype": "B",
        "prctyp": "LMT",
        "ret": "DAY",
    }
    base.update(overrides)
    return base


@pytest.fixture
def state() -> BrokerState:
    s = BrokerState()
    s.add_instrument(
        "NFO",
        "43210",
        SYMBOL,
        lot_size=75,
        tick_size="0.05",
        instrument="OPTIDX",
        expiry=date(2025, 11, 25),
        strike="24000",
        option_type="CE",
        underlying="NIFTY",
    )
    return s


def test_place_opens_order_with_new_id(state: BrokerState) -> None:
    limit = state.place(_payload())
    market = state.place(_payload(prctyp="MKT", prc="0"))

    assert limit.id != market.id
    assert limit.id.isdigit() and len(limit.id) == 14
    assert int(market.id) == int(limit.id) + 1
    assert state.orders == [limit, market]
    assert limit.status == "OPEN"
    assert limit.price == Decimal("101.50")
    assert market.price is None
    assert (limit.exchange, limit.symbol, limit.side, limit.quantity) == ("NFO", SYMBOL, "B", 75)
    assert (limit.order_type, limit.product, limit.trigger_price) == ("LMT", "M", None)
    assert [row["status"] for row in limit.history] == ["OPEN", "PENDING"]  # newest first
    assert limit.history[0]["norenordno"] == limit.id
    assert limit.payload == _payload()


def test_default_place_pending_and_reject(state: BrokerState) -> None:
    state.default_place = "pending"
    waiting = state.place(_payload())
    assert waiting.status == "PENDING"
    state.accept(waiting.id)
    assert waiting.status == "OPEN"
    with pytest.raises(ValueError, match="PENDING"):
        state.accept(waiting.id)

    state.default_place = "reject"
    refused = state.place(_payload())
    assert refused.status == "REJECTED"
    assert refused.reject_reason == "RMS:Rejected by OfflineBroker"
    assert refused.history[0]["rejreason"] == "RMS:Rejected by OfflineBroker"

    state.default_reject_message = "RMS:Insufficient balance"
    assert state.place(_payload()).reject_reason == "RMS:Insufficient balance"

    state.default_place = "later"
    with pytest.raises(ValueError, match="default_place"):
        state.place(_payload())


def test_fill_partial_then_full(state: BrokerState) -> None:
    order = state.place(_payload())

    state.fill(order.id, price="100", quantity=25)
    assert order.status == "OPEN"
    assert order.filled_quantity == 25
    assert order.average_price == Decimal("100")

    state.fill(order.id, price=Decimal("103"))  # the remaining 50
    assert order.status == "COMPLETE"
    assert order.filled_quantity == 75
    assert order.average_price == Decimal("102")  # (25 * 100 + 50 * 103) / 75

    assert [(t["norenordno"], t["flqty"], t["flprc"]) for t in state.trades] == [
        (order.id, "25", "100.00"),
        (order.id, "50", "103.00"),
    ]
    (position,) = state.positions
    assert (position["exch"], position["tsym"], position["prd"]) == ("NFO", SYMBOL, "M")
    assert position["netqty"] == "75"
    assert Decimal(position["netavgprc"]) == Decimal("102")

    with pytest.raises(ValueError, match="COMPLETE"):
        state.fill(order.id, price="100")


def test_fill_rejects_more_than_the_open_quantity(state: BrokerState) -> None:
    order = state.place(_payload())
    with pytest.raises(ValueError, match="quantity"):
        state.fill(order.id, price="100", quantity=76)
    with pytest.raises(ValueError, match="quantity"):
        state.fill(order.id, price="100", quantity=0)
    assert order.filled_quantity == 0


def test_sell_fill_reduces_a_starting_position(state: BrokerState) -> None:
    state.set_positions(
        [{"exch": "NFO", "tsym": SYMBOL, "prd": "M", "cfbuyqty": "150", "cfbuyamt": "15000.00"}]
    )
    order = state.place(_payload(trantype="S", qty="75"))
    state.fill(order.id, price="110")

    (position,) = state.positions
    assert position["netqty"] == "75"
    assert Decimal(position["netavgprc"]) == Decimal("100")
    assert Decimal(position["rpnl"]) == Decimal("750")  # 75 * (110 - 100)


def test_cancel_and_reject_by_test(state: BrokerState) -> None:
    first = state.place(_payload())
    state.cancel(first.id)
    assert first.status == "CANCELED"
    assert first.history[0]["status"] == "CANCELED"

    second = state.place(_payload())
    state.reject(second.id, "RMS:Margin Exceeds")
    assert second.status == "REJECTED"
    assert second.reject_reason == "RMS:Margin Exceeds"

    with pytest.raises(ValueError, match="CANCELED"):
        state.cancel(first.id)
    with pytest.raises(ValueError, match="no order"):
        state.fill("99999999999999", price="1")


def test_strategy_cancel_of_closed_order_is_refused(state: BrokerState) -> None:
    order = state.place(_payload())
    assert state.cancel_request(order.id) == (True, order.id)
    assert order.status == "CANCELED"

    assert state.cancel_request(order.id) == (False, "order is not open to cancel")
    assert state.cancel_request("99999999999999") == (False, "Order not found to Cancel")


def test_modify_updates_price_and_adds_history_row(state: BrokerState) -> None:
    order = state.place(_payload())
    rows_before = len(order.history)
    change = {"norenordno": order.id, "exch": "NFO", "tsym": SYMBOL, "qty": "150", "prc": "99.00"}
    change.update(prctyp="LMT", prd="M", uid="OFFLINE01", actid="OFFLINE01")

    assert state.modify(change) == (True, order.id)
    assert order.price == Decimal("99.00")
    assert order.quantity == 150
    assert len(order.history) == rows_before + 1
    assert (order.history[0]["prc"], order.history[0]["qty"]) == ("99.00", "150")
    assert order.history[0]["rpt"] == "Replaced"

    state.cancel(order.id)
    assert state.modify(change) == (False, "order is not open to modify")
    assert state.modify({**change, "norenordno": "1"}) == (False, "Order not found to Modify")


def test_trigger_moves_trigger_pending_to_open(state: BrokerState) -> None:
    order = state.place(_payload(prctyp="SL-LMT", prc="101.50", trgprc="101.00"))
    assert order.status == "TRIGGER_PENDING"
    assert order.trigger_price == Decimal("101.00")

    state.trigger(order.id)
    assert order.status == "OPEN"
    with pytest.raises(ValueError, match="TRIGGER_PENDING"):
        state.trigger(order.id)


def test_events_emitted_for_order_and_position_changes(state: BrokerState) -> None:
    events: list[dict[str, Any]] = []
    state.add_listener(events.append)

    order = state.place(_payload())
    assert [(e["t"], e["status"]) for e in events] == [("om", "PENDING"), ("om", "OPEN")]

    events.clear()
    state.fill(order.id, price="100")
    assert [e["t"] for e in events] == ["om", "pm"]
    om, pm = events
    ws = fixture("ws_messages")
    assert set(ws["om"]) <= set(om)
    assert set(ws["pm"]) <= set(pm)
    assert (om["norenordno"], om["status"], om["reporttype"]) == (order.id, "COMPLETE", "Fill")
    assert (om["flqty"], om["flprc"], om["fillshares"]) == ("75", "100.00", "75")
    assert (pm["tsym"], pm["netqty"]) == (SYMBOL, "75")


def test_listeners_are_called_outside_the_lock(state: BrokerState) -> None:
    lock_free: list[bool] = []

    def listener(message: dict[str, Any]) -> None:
        result: list[bool] = []

        def probe() -> None:
            got = state.lock.acquire(blocking=False)
            if got:
                state.lock.release()
            result.append(got)

        worker = threading.Thread(target=probe)
        worker.start()
        worker.join()
        lock_free.extend(result)

    state.add_listener(listener)
    state.place(_payload())
    assert lock_free == [True, True]


def test_failing_listener_does_not_stop_the_change(
    state: BrokerState, caplog: pytest.LogCaptureFixture
) -> None:
    received: list[str] = []

    def broken(message: dict[str, Any]) -> None:
        raise RuntimeError("listener bug")

    state.add_listener(broken)
    state.add_listener(lambda message: received.append(message["t"]))
    order = state.place(_payload())

    assert order.status == "OPEN"
    assert received == ["om", "om"]
    assert "listener" in caplog.text


def test_tick_emits_tf_and_updates_quote(state: BrokerState) -> None:
    state.set_quote("NFO", "43210", last_price="101.50", volume=10)
    events: list[dict[str, Any]] = []
    state.add_listener(events.append)

    state.tick("NFO", "43210", last_price="102.00", bid="101.95")

    assert [e["t"] for e in events] == ["tf", "df"]
    for message in events:
        assert (message["e"], message["tk"], message["lp"], message["bp1"]) == (
            "NFO",
            "43210",
            "102.00",
            "101.95",
        )
        assert "ft" in message
        assert "v" not in message  # a feed carries only the changed fields
    quote = state.quote("NFO", "43210")
    assert quote is not None
    assert (quote["lp"], quote["v"], quote["bp1"], quote["tsym"]) == (
        "102.00",
        "10",
        "101.95",
        SYMBOL,
    )


def test_set_quote_maps_friendly_names_and_passes_wire_keys(state: BrokerState) -> None:
    state.set_quote(
        "NFO",
        "43210",
        last_price=Decimal("101.5"),
        open="100",
        high="105",
        low="99",
        close="100.5",
        volume=1000,
        oi=5000,
        change_percent="1.00",
        bid="101.45",
        ask="101.55",
        bid_qty=150,
        ask_qty=75,
        uc="120.00",
    )
    quote = state.quote("NFO", "43210")
    assert quote is not None
    expected = {"lp": "101.5", "o": "100", "h": "105", "l": "99", "c": "100.5", "v": "1000"}
    expected.update(oi="5000", pc="1.00", bp1="101.45", sp1="101.55", bq1="150", sq1="75")
    expected.update(uc="120.00")
    assert {k: quote[k] for k in expected} == expected


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"ltp": "101"}, "unknown quote field 'ltp'"),
        ({"last_price": "abc"}, "last_price must be a number"),
        ({"bid_qty": float("nan")}, "bid_qty must be a number"),
        ({"bp2": "x"}, "bp2 must be a number"),
        ({"volume": True}, "volume must be a number"),
    ],
)
def test_set_quote_and_tick_reject_typos_and_bad_numbers(
    state: BrokerState, fields: dict[str, Any], error: str
) -> None:
    state.set_quote("NFO", "43210", last_price="101.50")
    events: list[dict[str, Any]] = []
    state.add_listener(events.append)
    with pytest.raises(ValueError, match=error):
        state.set_quote("NFO", "43210", **fields)
    with pytest.raises(ValueError, match=error):
        state.tick("NFO", "43210", **fields)
    assert events == []  # nothing pushed
    quote = state.quote("NFO", "43210")
    assert quote is not None and quote["lp"] == "101.50"  # nothing changed


def test_set_quote_accepts_the_apis_own_field_names(state: BrokerState) -> None:
    state.set_quote("NFO", "43210", lp="101", bp2="100.95", sq3=25, ap="100.5", lut="1762745100")
    quote = state.quote("NFO", "43210")
    assert quote is not None
    values = (quote["lp"], quote["bp2"], quote["sq3"], quote["ap"])
    assert values == ("101", "100.95", "25", "100.5")


@pytest.mark.parametrize(
    ("candle", "error"),
    [
        ({"time": "10-11-2026 09:15:00", "clsoe": "100"}, "unknown candle field 'clsoe'"),
        ({"time": "10-11-2026 09:15:00", "close": "abc"}, "close must be a number"),
        ({"time": "10-11-2026 09:15:00", "intv": "Infinity"}, "intv must be a number"),
        ({"time": 20261110, "close": "100"}, "time must be a datetime, a date or text"),
    ],
)
def test_set_candles_rejects_typos_and_bad_numbers(
    state: BrokerState, candle: dict[str, Any], error: str
) -> None:
    good = {"time": "10-11-2026 09:14:00", "close": "99"}
    state.set_candles("NFO", "43210", [good])
    with pytest.raises(ValueError, match=error):
        state.set_candles("NFO", "43210", [good, candle])
    assert state.candles[("NFO", "43210")] == [good]  # nothing changed


def test_quote_without_set_quote_is_none(state: BrokerState) -> None:
    assert state.quote("NFO", "43210") is None
    assert state.quote("NSE", "1") is None


def test_drills_are_one_shot_and_per_endpoint(state: BrokerState) -> None:
    state.fail_next("PlaceOrder", drills.reject("RMS: blocked"))
    state.fail_next("PlaceOrder", drills.rate_limit())
    state.fail_next("OrderBook", drills.bad_body())

    assert state.take_drill("CancelOrder") is None
    assert state.take_drill("PlaceOrder") == drills.reject("RMS: blocked")
    assert state.take_drill("PlaceOrder") == drills.rate_limit()
    assert state.take_drill("PlaceOrder") is None
    assert state.take_drill("OrderBook") == drills.bad_body()
    assert state.take_drill("OrderBook") is None


def test_fail_next_needs_a_drill(state: BrokerState) -> None:
    with pytest.raises(TypeError, match="drills"):
        state.fail_next("PlaceOrder", drills.reject)  # type: ignore[arg-type]


def test_payload_tokens_are_masked_in_requests(state: BrokerState) -> None:
    sent = {"code": "one-time-code", "checksum": "abc123", "uid": "OFFLINE01"}
    request = state.record("GenAcsTok", sent)

    assert request == OfflineRequest(
        "GenAcsTok", {"code": "***", "checksum": "***", "uid": "OFFLINE01"}
    )
    assert state.requests == [request]
    assert sent["code"] == "one-time-code"  # the caller's dict is not changed

    order = state.place(_payload(access_token="offline-token-1"))
    assert order.payload["access_token"] == "***"


def test_add_instrument_builds_a_scrip_row(state: BrokerState) -> None:
    state.add_instrument("NSE", "1594", "INFY-EQ", tick_size="0.10", isin="INE009A01021")

    row = state.instruments[("NSE", "1594")]
    expected = {"exch": "NSE", "token": "1594", "tsym": "INFY-EQ", "ls": "1", "ti": "0.10"}
    expected.update(pp="2", instname="EQ", symname="INFY", seg="EQT", isin="INE009A01021")
    assert {k: row[k] for k in expected} == expected
    assert "optt" not in row

    option = state.instruments[("NFO", "43210")]
    expected = {"optt": "CE", "strprc": "24000.00", "exd": "25-NOV-2025", "symname": "NIFTY"}
    expected.update(ls="75", seg="DER", instname="OPTIDX")
    assert {k: option[k] for k in expected} == expected
    assert state.find_instrument("NFO", SYMBOL) is option
    assert state.find_instrument("NFO", "UNKNOWN") is None


def test_setup_values_are_stored(state: BrokerState) -> None:
    state.set_funds(cash="100000.00", marginused=Decimal("2500.5"))
    state.set_funds(payin="10.00")
    state.set_user(uname="Strategy Tester")
    state.set_holdings([{"holdqty": "10"}])
    state.set_candles("NFO", "43210", [{"time": "10-11-2026 09:15:00", "open": "100"}])
    state.set_reply("GetOrderMargin", {"stat": "Ok", "cash": "0.00"})

    assert state.funds == {"cash": "100000.00", "marginused": "2500.5", "payin": "10.00"}
    assert state.user == {"uname": "Strategy Tester"}
    assert state.holdings == [{"holdqty": "10"}]
    assert state.candles[("NFO", "43210")] == [{"time": "10-11-2026 09:15:00", "open": "100"}]
    assert state.replies["GetOrderMargin"] == {"stat": "Ok", "cash": "0.00"}
    assert state.client_id == "OFFLINE01"
    assert (state.refuse_login_once, state.silenced) == (False, False)


def test_order_for_unknown_instrument_is_accepted(state: BrokerState) -> None:
    order = state.place(_payload(exch="NSE", tsym="NOTADDED-EQ"))
    assert order.status == "OPEN"
    assert order.history[0]["token"] == ""


def test_set_positions_carries_a_net_only_row_forward(state: BrokerState) -> None:
    state.set_positions(
        [{"exch": "NFO", "tsym": SYMBOL, "prd": "M", "netqty": -50, "netavgprc": "200"}]
    )

    (position,) = state.positions
    assert (position["cfsellqty"], position["cfsellamt"], position["netqty"]) == (
        "50",
        "10000.00",
        "-50",
    )
    assert Decimal(position["netavgprc"]) == Decimal("200")
    assert position["token"] == "43210"


def test_modify_a_stop_order_changes_its_trigger(state: BrokerState) -> None:
    order = state.place(_payload(prctyp="SL-LMT", trgprc="101.00"))

    assert state.modify({"norenordno": order.id, "trgprc": "100.50"}) == (True, order.id)
    assert order.trigger_price == Decimal("100.50")

    assert state.modify({"norenordno": order.id, "prctyp": "MKT"}) == (True, order.id)
    assert (order.order_type, order.price, order.trigger_price) == ("MKT", None, None)
    assert order.status == "OPEN"  # a market order no longer waits for a trigger


def test_modify_to_a_stop_order_waits_for_the_trigger(state: BrokerState) -> None:
    order = state.place(_payload())
    change = {"norenordno": order.id, "prctyp": "SL-LMT", "trgprc": "101.00"}

    assert state.modify(change) == (True, order.id)
    assert (order.status, order.history[0]["status"]) == ("TRIGGER_PENDING", "TRIGGER_PENDING")


def test_modify_below_filled_quantity_is_refused(state: BrokerState) -> None:
    order = state.place(_payload())
    state.fill(order.id, price="100", quantity=50)
    events: list[dict[str, Any]] = []
    state.add_listener(events.append)
    rows_before = len(order.history)

    for qty in ("50", "25"):
        assert state.modify({"norenordno": order.id, "qty": qty}) == (
            False,
            "quantity must be more than the filled quantity 50",
        )
    assert (order.quantity, order.status, len(order.history), events) == (
        75,
        "OPEN",
        rows_before,
        [],
    )


def test_fill_tolerates_whole_number_text_in_starting_positions(state: BrokerState) -> None:
    state.set_positions(
        [{"exch": "NFO", "tsym": SYMBOL, "prd": "M", "daybuyqty": "10.0", "daybuyamt": 1000.0}]
    )
    assert state.positions[0]["daybuyqty"] == "10"

    order = state.place(_payload(qty="5"))
    state.fill(order.id, price="100")

    (position,) = state.positions
    assert (position["daybuyqty"], position["netqty"]) == ("15", "15")


def test_set_positions_refuses_fractional_quantity_before_any_change(state: BrokerState) -> None:
    state.set_positions([{"exch": "NFO", "tsym": SYMBOL, "prd": "M", "cfbuyqty": "10"}])
    before = [dict(row) for row in state.positions]

    with pytest.raises(ValueError, match="daybuyqty"):
        state.set_positions([{"exch": "NFO", "tsym": SYMBOL, "prd": "M", "daybuyqty": "10.5"}])
    with pytest.raises(ValueError, match="daybuyamt"):
        state.set_positions([{"exch": "NFO", "tsym": SYMBOL, "prd": "M", "daybuyamt": "lots"}])
    assert state.positions == before


def test_failed_fill_changes_nothing(state: BrokerState) -> None:
    order = state.place(_payload())
    events: list[dict[str, Any]] = []
    state.add_listener(events.append)
    rows_before = len(order.history)

    with pytest.raises(ValueError, match="price"):
        state.fill(order.id, price="abc")

    assert (order.filled_quantity, order.average_price, order.status) == (0, None, "OPEN")
    assert (len(order.history), state.trades, state.positions, events) == (rows_before, [], [], [])


def test_failed_place_books_nothing(state: BrokerState) -> None:
    events: list[dict[str, Any]] = []
    state.add_listener(events.append)

    with pytest.raises(ValueError, match="blprc"):
        state.place(_payload(prd="B", blprc="abc"))
    with pytest.raises(ValueError, match="qty"):
        state.place(_payload(qty="many"))

    assert (state.orders, events) == ([], [])
    assert state.place(_payload()).id.endswith("00000001")


def test_average_price_is_exact_and_two_decimals_on_the_wire(state: BrokerState) -> None:
    events: list[dict[str, Any]] = []
    order = state.place(_payload(qty="6"))
    state.add_listener(events.append)
    for quantity, price in ((1, "100"), (2, "101"), (3, "102")):
        state.fill(order.id, price=price, quantity=quantity)

    assert order.average_price == Decimal(608) / Decimal(6)
    om = events[-2]
    assert (order.history[0]["avgprc"], om["avgprc"], state.trades[-1]["avgprc"]) == (
        "101.33",
        "101.33",
        "101.33",
    )
    position = state.positions[0]
    assert (position["netavgprc"], position["totbuyavgprc"], position["daybuyamt"]) == (
        "101.33",
        "101.33",
        "608.00",
    )


def test_events_are_delivered_in_mutation_order(state: BrokerState) -> None:
    delivered: list[str] = []

    def listener(message: dict[str, Any]) -> None:
        delivered.append(message["status"])
        if message["status"] == "PENDING":  # a change made while an earlier event is delivered
            state.cancel(message["norenordno"])

    state.add_listener(listener)
    state.place(_payload())

    assert delivered == ["PENDING", "OPEN", "CANCELED"]


def test_fill_and_place_refuse_missing_or_non_finite_numbers(state: BrokerState) -> None:
    order = state.place(_payload())
    with pytest.raises(ValueError, match="price"):
        state.fill(order.id, price="")
    with pytest.raises(ValueError, match="prc"):
        state.place(_payload(prc="NaN"))
    assert (order.filled_quantity, len(state.orders)) == (0, 1)
