import logging
from decimal import Decimal

import httpx
import pytest
import respx

from tests.conftest import BASE, fixture
from tradesmart import (
    AsyncTradeSmart,
    Exchange,
    OrderType,
    Product,
    RequestValidationError,
    ResponseParseError,
    TradeSmart,
    TransactionType,
)


@respx.mock
def test_order_book_sends_uid_only_and_parses(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/OrderBook").respond(200, json=fixture("order_book_success"))
    orders = client.orders()
    assert route.calls.last.request.content == b'jData={"uid":"TEST123"}'
    assert orders[0].order_id and isinstance(orders[0].quantity, int)
    first = orders[0]
    assert first.status == "REJECTED" and isinstance(first.status, str)
    assert first.exchange == "NSE" and first.tradingsymbol == "INFY-EQ"
    assert first.user_id == "TEST123" and first.account_id == "TEST123"
    assert first.price == Decimal("1584.00") and first.trigger_price == Decimal("1584.00")
    assert first.book_profit_price == Decimal("2.00") and first.trail_price == Decimal("1.00")
    assert first.disclosed_quantity == 0 and first.lot_size == 1
    assert first.ordenttm is not None and first.ordenttm.tzinfo is not None
    assert first.norentm is not None and first.norentm.hour == 16
    assert first.reject_reason and first.ipaddr == "127.0.0.1"
    assert first.raw["norenordno"] == first.order_id


@respx.mock
@pytest.mark.parametrize("endpoint,method", [("OrderBook", "orders"), ("TradeBook", "trades")])
def test_books_no_data_empty(client: TradeSmart, endpoint: str, method: str) -> None:
    respx.post(f"{BASE}/{endpoint}").respond(200, json=fixture("errors")["no_data"])
    assert getattr(client, method)() == []


@respx.mock
def test_trade_book_object_is_error(client: TradeSmart) -> None:
    respx.post(f"{BASE}/TradeBook").respond(200, json={"stat": "Ok"})
    with pytest.raises(ResponseParseError):
        client.trades()


@respx.mock
def test_trade_book_payload_and_parse(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/TradeBook").respond(200, json=fixture("trade_book_success"))
    trades = client.trades()
    assert route.calls.last.request.content == b'jData={"uid":"TEST123","actid":"TEST123"}'
    t = trades[0]
    assert t.flqty == 10 and t.flprc == Decimal("1498.50") and t.average_price == Decimal("1498.50")
    assert t.fltm is not None and t.fltm.tzinfo is not None and t.fltm.minute == 5
    assert t.exch_tm is not None and t.norentm is not None
    assert t.transaction_type == "B" and t.cstFrm == "C"


@respx.mock
def test_history_payload(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/SingleOrdHist").respond(
        200, json=fixture("single_ord_hist_success")
    )
    history = client.order_history("26062000004241")
    assert route.calls.last.request.content == (
        b'jData={"uid":"TEST123","norenordno":"26062000004241"}'
    )
    assert [h.status for h in history] == ["REJECTED", "PENDING"]
    assert history[1].rpt == "NewAck" and history[1].introp_exch == "EQT"
    assert history[0].pan == "XXXXX0000X"


@respx.mock
def test_history_no_data_empty(client: TradeSmart) -> None:
    respx.post(f"{BASE}/SingleOrdHist").respond(200, json=fixture("errors")["no_data"])
    assert client.order_history("1") == []


@respx.mock
def test_margin_is_read_only_and_retried(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/GetOrderMargin").mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json=fixture("order_margin_success"))]
    )
    m = client.order_margin(
        exchange=Exchange.NSE,
        tradingsymbol="INFY-EQ",
        quantity=1,
        price=Decimal("1500"),
        product=Product.CNC,
        transaction_type=TransactionType.BUY,
        order_type=OrderType.LIMIT,
    )
    assert route.call_count == 2 and m.stat == "Ok"
    assert m.ordermargin == Decimal("1598.00") and m.marginusedprev == Decimal("0.00")
    assert m.remarks == "Insufficient Balance" and m.cash == Decimal("0.00")
    assert m.request_time is not None
    assert route.calls.last.request.content == (
        b'jData={"uid":"TEST123","actid":"TEST123","exch":"NSE","tsym":"INFY-EQ",'
        b'"qty":"1","prc":"1500","prd":"C","trantype":"B","prctyp":"LMT"}'
    )


@respx.mock
def test_margin_optional_fields_and_str_enums(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/GetOrderMargin").respond(200, json=fixture("order_margin_success"))
    client.order_margin(
        exchange="NFO",
        tradingsymbol="X",
        quantity=2,
        price=0,
        product="B",
        transaction_type="S",
        order_type="SL-LMT",
        trigger_price=Decimal("10.5"),
        book_loss_price=1,
        order_id="123",
        sno_number="4",
    )
    assert route.calls.last.request.content == (
        b'jData={"uid":"TEST123","actid":"TEST123","exch":"NFO","tsym":"X","qty":"2","prc":"0",'
        b'"prd":"B","trantype":"S","prctyp":"SL-LMT","trgprc":"10.5","blprc":"1",'
        b'"norenordno":"123","snonum":"4"}'
    )


@pytest.mark.parametrize(
    "overrides",
    [{"quantity": 0}, {"price": -1}, {"exchange": "XXX"}, {"product": "Z"}, {"order_type": "FOO"}],
)
def test_margin_validation(client: TradeSmart, overrides: dict) -> None:  # type: ignore[type-arg]
    args = {
        "exchange": Exchange.NSE,
        "tradingsymbol": "INFY-EQ",
        "quantity": 1,
        "price": 1,
        "product": Product.CNC,
        "transaction_type": TransactionType.BUY,
        "order_type": OrderType.LIMIT,
    }
    args.update(overrides)
    with pytest.raises(RequestValidationError):
        client.order_margin(**args)  # type: ignore[arg-type]


@respx.mock
async def test_async_order_methods(async_client: AsyncTradeSmart) -> None:
    respx.post(f"{BASE}/OrderBook").respond(200, json=fixture("order_book_success"))
    respx.post(f"{BASE}/SingleOrdHist").respond(200, json=fixture("single_ord_hist_success"))
    respx.post(f"{BASE}/TradeBook").respond(200, json=fixture("trade_book_success"))
    respx.post(f"{BASE}/GetOrderMargin").respond(200, json=fixture("order_margin_success"))
    assert len(await async_client.orders()) == 13
    assert len(await async_client.order_history("1")) == 2
    assert len(await async_client.trades()) == 2
    m = await async_client.order_margin(
        exchange=Exchange.NSE,
        tradingsymbol="INFY-EQ",
        quantity=1,
        price=1,
        product=Product.CNC,
        transaction_type=TransactionType.BUY,
        order_type=OrderType.LIMIT,
    )
    assert m.stat == "Ok"


@respx.mock
def test_order_book_keeps_rows_with_odd_fields(client: TradeSmart, caplog) -> None:
    row = fixture("order_book_success")[0]
    numeric_token = dict(row, token=22, norenordno="1001")
    bad_price = dict(row, prc="abc-9z", norenordno="1002")
    respx.post(f"{BASE}/OrderBook").respond(200, json=[numeric_token, bad_price])
    with caplog.at_level(logging.WARNING, logger="tradesmart"):
        orders = client.orders()
    assert [o.order_id for o in orders] == ["1001", "1002"]
    assert orders[0].token == "22"
    assert orders[1].price is None and orders[1].raw["prc"] == "abc-9z"
    assert orders[1].quantity == orders[0].quantity
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "prc" in warnings[0].getMessage() and "abc-9z" not in caplog.text


@respx.mock
def test_order_book_row_with_bad_order_id_raises(client: TradeSmart) -> None:
    row = fixture("order_book_success")[0]
    respx.post(f"{BASE}/OrderBook").respond(200, json=[dict(row, norenordno=True)])
    with pytest.raises(ResponseParseError):
        client.orders()
