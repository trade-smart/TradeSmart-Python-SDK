from decimal import Decimal
from typing import Any

import pytest

from tradesmart import Exchange, OrderType, Product, RequestValidationError, TransactionType
from tradesmart.endpoints.orders import (
    exit_sno_order_call,
    modify_order_call,
    place_order_call,
)

BASE_KW: dict[str, Any] = dict(
    exchange=Exchange.NSE,
    tradingsymbol="INFY-EQ",
    quantity=1,
    transaction_type=TransactionType.BUY,
    product=Product.CNC,
)
MODIFY_KW: dict[str, Any] = dict(
    exchange=Exchange.NSE,
    tradingsymbol="INFY-EQ",
    quantity=1,
    price=10,
    order_type=OrderType.LIMIT,
    product=Product.CNC,
)


@pytest.mark.parametrize(
    "kw",
    [
        dict(order_type=OrderType.LIMIT),
        dict(order_type=OrderType.LIMIT, price=Decimal("0")),
        dict(order_type=OrderType.SL, price=Decimal("10")),
        dict(order_type=OrderType.SL_M),
        dict(order_type=OrderType.SL_M, trigger_price=0),
        dict(order_type=OrderType.LIMIT, price=Decimal("10"), quantity=0),
        dict(order_type=OrderType.LIMIT, price=Decimal("-1")),
        dict(order_type=OrderType.LIMIT, price=Decimal("10"), book_loss_price=Decimal("2")),
        dict(order_type=OrderType.LIMIT, price=Decimal("10"), trail_price=Decimal("1")),
        dict(order_type=OrderType.LIMIT, price=Decimal("10"), disclosed_quantity=5),
        dict(order_type=OrderType.LIMIT, price=Decimal("10"), disclosed_quantity=-1),
        dict(order_type="XYZ", price=Decimal("10")),
        dict(order_type=OrderType.LIMIT, price=Decimal("10"), trigger_price=Decimal("9")),
        dict(order_type=OrderType.MARKET, trigger_price=Decimal("9")),
    ],
)
def test_place_order_validation_errors(kw: dict[str, Any]) -> None:
    with pytest.raises(RequestValidationError):
        place_order_call("TEST123", **{**BASE_KW, **kw})


def test_mtf_not_allowed_on_mcx() -> None:
    kw = {
        **BASE_KW,
        "exchange": Exchange.MCX,
        "product": Product.MTF,
        "order_type": OrderType.LIMIT,
        "price": 1,
    }
    with pytest.raises(RequestValidationError, match="MCX"):
        place_order_call("TEST123", **kw)


def test_market_order_wire() -> None:
    call = place_order_call(
        "TEST123", **{**BASE_KW, "order_type": OrderType.MARKET, "market_protection": 5}
    )
    assert call.payload == {
        "uid": "TEST123",
        "actid": "TEST123",
        "exch": "NSE",
        "tsym": "INFY-EQ",
        "qty": "1",
        "prc": "0",
        "prd": "C",
        "trantype": "B",
        "prctyp": "MKT",
        "ret": "DAY",
        "amo": "No",
        "ordersource": "API",
        "mkt_protection": "5",
    }
    assert list(call.payload) == [
        "uid",
        "actid",
        "exch",
        "tsym",
        "qty",
        "prc",
        "prd",
        "trantype",
        "prctyp",
        "ret",
        "amo",
        "ordersource",
        "mkt_protection",
    ]


def test_sl_market_wire_and_flags() -> None:
    call = place_order_call(
        "TEST123",
        **{**BASE_KW, "order_type": OrderType.SL_M, "trigger_price": "99.5"},
        disclosed_quantity=0,
        amo=True,
    )
    assert call.spec.state_changing is True and call.spec.rate_group == "place_order"
    assert call.payload["prc"] == "0" and call.payload["trgprc"] == "99.5"
    assert call.payload["dscqty"] == "0" and call.payload["amo"] == "Yes"


def test_bracket_requires_both_legs() -> None:
    kw = {**BASE_KW, "product": Product.BO, "order_type": OrderType.LIMIT, "price": 10}
    with pytest.raises(RequestValidationError):
        place_order_call("TEST123", **kw, book_loss_price=2)
    with pytest.raises(RequestValidationError):
        place_order_call("TEST123", **kw, book_profit_price=2)
    call = place_order_call("TEST123", **kw, book_loss_price=2, book_profit_price=3, trail_price=1)
    assert call.payload["blprc"] == "2" and call.payload["bpprc"] == "3"
    assert call.payload["trailprc"] == "1"


def test_cover_requires_book_loss() -> None:
    kw = {**BASE_KW, "product": Product.CO, "order_type": OrderType.LIMIT, "price": 10}
    with pytest.raises(RequestValidationError):
        place_order_call("TEST123", **kw)
    assert place_order_call("TEST123", **kw, book_loss_price=2).payload["blprc"] == "2"


def test_market_orders_not_blocked_client_side() -> None:
    call = place_order_call("TEST123", **{**BASE_KW, "order_type": OrderType.MARKET})
    assert call.payload["prctyp"] == "MKT" and "mkt_protection" not in call.payload


@pytest.mark.parametrize(
    "kw",
    [
        dict(price=0),
        dict(order_type=OrderType.SL),
        dict(quantity=0),
        dict(trigger_price=5),
        dict(order_type=OrderType.MARKET, trigger_price=5),
        dict(book_loss_price=1),
        dict(book_profit_price=1),
    ],
)
def test_modify_validation_errors(kw: dict[str, Any]) -> None:
    with pytest.raises(RequestValidationError):
        modify_order_call("TEST123", order_id="1", **{**MODIFY_KW, **kw})


def test_modify_wire_order() -> None:
    call = modify_order_call("TEST123", order_id="7", **MODIFY_KW, tag="r")
    assert list(call.payload) == [
        "uid",
        "actid",
        "exch",
        "norenordno",
        "tsym",
        "qty",
        "prc",
        "prctyp",
        "prd",
        "remarks",
        "ordersource",
    ]
    assert call.spec.state_changing is True and call.spec.rate_group is None


_LIMIT_KW: dict[str, Any] = {**BASE_KW, "order_type": OrderType.LIMIT, "price": 10}
_PLACE_KEYS = [
    "uid",
    "actid",
    "exch",
    "tsym",
    "qty",
    "prc",
    "prd",
    "trantype",
    "prctyp",
    "ret",
    "amo",
    "ordersource",
]


def test_place_without_algo_id_sends_no_algo_id_key() -> None:
    assert list(place_order_call("TEST123", **_LIMIT_KW).payload) == _PLACE_KEYS
    explicit_none = place_order_call("TEST123", **_LIMIT_KW, algo_id=None)
    assert list(explicit_none.payload) == _PLACE_KEYS


def test_place_algo_id_is_sent_last() -> None:
    call = place_order_call(
        "TEST123", **{**_LIMIT_KW, "market_protection": 5}, tag="t", algo_id="153763"
    )
    assert list(call.payload)[-2:] == ["mkt_protection", "algo_id"]
    assert call.payload["algo_id"] == "153763"


def test_modify_algo_id_is_sent_last() -> None:
    call = modify_order_call(
        "TEST123", order_id="7", **{**MODIFY_KW, "product": Product.CO}, book_loss_price=2,
        trail_price=1, algo_id="153763",
    )  # fmt: skip
    assert list(call.payload)[-2:] == ["trailprc", "algo_id"]
    assert call.payload["algo_id"] == "153763"


def test_modify_without_algo_id_sends_no_algo_id_key() -> None:
    assert "algo_id" not in modify_order_call("TEST123", order_id="7", **MODIFY_KW).payload


@pytest.mark.parametrize("value", ["", "  ", True, 153763, 1.5])
def test_place_and_modify_refuse_a_bad_algo_id(value: Any) -> None:
    with pytest.raises(RequestValidationError, match="algo_id"):
        place_order_call("TEST123", **_LIMIT_KW, algo_id=value)
    with pytest.raises(RequestValidationError, match="algo_id"):
        modify_order_call("TEST123", order_id="7", **MODIFY_KW, algo_id=value)


@pytest.mark.parametrize("product", ["C", "I", "M", "F", Product.CNC, "", "X"])
def test_exit_sno_rejects_other_products(product: Any) -> None:
    with pytest.raises(RequestValidationError):
        exit_sno_order_call("TEST123", "1", product)


@pytest.mark.parametrize("product", ["H", "B", Product.CO, Product.BO])
def test_exit_sno_accepts_h_b(product: Any) -> None:
    call = exit_sno_order_call("TEST123", "1", product)
    assert call.payload["prd"] in {"H", "B"} and call.spec.state_changing is True


def test_book_profit_price_only_for_bracket() -> None:
    kw = {**BASE_KW, "order_type": OrderType.LIMIT, "price": 10, "book_profit_price": 3}
    with pytest.raises(RequestValidationError):
        place_order_call("TEST123", **{**kw, "product": Product.CO, "book_loss_price": 2})
    ok = place_order_call("TEST123", **{**kw, "product": Product.BO, "book_loss_price": 2})
    assert ok.payload["bpprc"] == "3" and ok.payload["blprc"] == "2"


def test_modify_cover_allows_loss_and_trail_not_profit() -> None:
    kw = {**MODIFY_KW, "product": Product.CO}
    call = modify_order_call("TEST123", order_id="1", **kw, book_loss_price=2, trail_price=1)
    assert call.payload["blprc"] == "2" and call.payload["trailprc"] == "1"
    with pytest.raises(RequestValidationError):
        modify_order_call("TEST123", order_id="1", **kw, book_profit_price=3)


from tradesmart.endpoints.market import option_chain_call  # noqa: E402
from tradesmart.endpoints.orders import order_margin_call  # noqa: E402

MARGIN_KW: dict[str, Any] = dict(
    exchange=Exchange.NSE,
    tradingsymbol="INFY-EQ",
    quantity=1,
    price="1598",
    product=Product.CNC,
    transaction_type=TransactionType.BUY,
    order_type=OrderType.LIMIT,
)


def test_margin_accepts_string_prices() -> None:
    call = order_margin_call(
        "TEST123",
        **{
            **MARGIN_KW,
            "order_type": OrderType.SL,
            "product": Product.CO,
            "trigger_price": "1590.5",
            "book_loss_price": "10",
        },
    )
    assert call.payload["prc"] == "1598"
    assert call.payload["trgprc"] == "1590.5"
    assert call.payload["blprc"] == "10"


@pytest.mark.parametrize("field", ["price", "trigger_price", "book_loss_price"])
def test_margin_rejects_junk_price(field: str) -> None:
    with pytest.raises(RequestValidationError):
        order_margin_call("TEST123", **{**MARGIN_KW, field: "abc"})


def test_option_chain_rejects_junk_strike() -> None:
    with pytest.raises(RequestValidationError):
        option_chain_call("TEST123", Exchange.NFO, "NIFTY", "abc", 5)


def test_option_chain_accepts_string_strike() -> None:
    call = option_chain_call("TEST123", Exchange.NFO, "NIFTY", "24000.50", 5)
    assert call.payload["strprc"] == "24000.50"


def test_quantity_rejects_bool() -> None:
    with pytest.raises(RequestValidationError):
        place_order_call("TEST123", **{**BASE_KW, "quantity": True, "order_type": OrderType.MARKET})
    with pytest.raises(RequestValidationError):
        modify_order_call("TEST123", order_id="1", **{**MODIFY_KW, "quantity": True})
    with pytest.raises(RequestValidationError):
        order_margin_call("TEST123", **{**MARGIN_KW, "quantity": True})
    with pytest.raises(RequestValidationError):
        place_order_call(
            "TEST123",
            **{**BASE_KW, "order_type": OrderType.MARKET, "disclosed_quantity": True},
        )


def test_request_accepts_words() -> None:
    call = place_order_call(
        "TEST123",
        exchange="NSE",
        tradingsymbol="INFY-EQ",
        quantity=1,
        transaction_type="BUY",
        product="MIS",
        order_type="LIMIT",
        price="1500",
    )
    assert call.payload["trantype"] == "B"
    assert call.payload["prd"] == "I"
    assert call.payload["prctyp"] == "LMT"


def test_request_rejects_misspelt_word() -> None:
    with pytest.raises(RequestValidationError, match="order_type|prctyp"):
        place_order_call(
            "TEST123",
            exchange="NSE",
            tradingsymbol="INFY-EQ",
            quantity=1,
            transaction_type="BUY",
            product="MIS",
            order_type="LIMITT",
            price="1500",
        )


def test_response_enum_or_raw() -> None:
    from tradesmart import OrderStatus
    from tradesmart.enums import OrderType
    from tradesmart.models.orders import Order

    order = Order.from_wire({"trantype": "B", "prd": "X", "prctyp": "LMT", "status": "NEW_STATUS"})
    assert order.transaction_type is TransactionType.BUY
    assert order.product == "X"
    assert order.order_type is OrderType.LIMIT
    assert order.status == "NEW_STATUS"
    done = Order.from_wire({"status": "COMPLETE", "prd": "", "ret": None})
    assert done.status is OrderStatus.COMPLETE
    assert done.product is None
    assert done.validity is None


def test_renamed_attributes() -> None:
    from tradesmart.enums import Validity
    from tradesmart.models.orders import Order

    order = Order.from_wire({"norenordno": "1", "ret": "DAY", "remarks": "x"})
    assert order.order_id == "1"
    assert order.validity is Validity.DAY
    assert order.tag == "x"
