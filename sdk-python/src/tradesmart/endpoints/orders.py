"""Order read endpoints: ``OrderBook``, ``SingleOrdHist``, ``TradeBook``, ``GetOrderMargin``."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from tradesmart._logging import redact
from tradesmart.endpoints.base import Call, EndpointSpec, parse_list, parse_object, require_str
from tradesmart.enums import Exchange, OrderType, Product, TransactionType, Validity
from tradesmart.errors import OrderStatusUnknownError
from tradesmart.models.base import PriceInput, validate_request
from tradesmart.models.orders import (
    CancelOrderRequest,
    ExitSnoOrderRequest,
    ModifyOrderRequest,
    Order,
    OrderAck,
    OrderHistoryEntry,
    OrderMargin,
    OrderMarginRequest,
    PlaceOrderRequest,
    Trade,
)

ORDER_BOOK: EndpointSpec[list[Order]] = EndpointSpec(
    name="OrderBook",
    parse=parse_list(Order, "OrderBook"),
    expect="list",
    allow_empty=True,
)

SINGLE_ORD_HIST: EndpointSpec[list[OrderHistoryEntry]] = EndpointSpec(
    name="SingleOrdHist",
    parse=parse_list(OrderHistoryEntry, "SingleOrdHist"),
    expect="list",
    allow_empty=True,
)

TRADE_BOOK: EndpointSpec[list[Trade]] = EndpointSpec(
    name="TradeBook",
    parse=parse_list(Trade, "TradeBook"),
    expect="list",
    allow_empty=True,
)

GET_ORDER_MARGIN: EndpointSpec[OrderMargin] = EndpointSpec(
    name="GetOrderMargin",
    parse=parse_object(OrderMargin, "GetOrderMargin"),
)


def _parse_ack(endpoint: str, *, require_order_id: bool) -> Callable[[Any], OrderAck]:
    """Parse an order acknowledgement the codec already accepted (``stat`` Ok).

    The OMS accepted the request, so a parse failure must not look like a failure
    (the caller could re-place the order): it is an unknown outcome instead.
    """
    inner = parse_object(OrderAck, endpoint)

    def _usable(ack: OrderAck) -> bool:
        return not require_order_id or bool(ack.order_id and ack.order_id.strip())

    def _parse(body: Any) -> OrderAck:
        ack: OrderAck | None = None
        try:
            ack = inner(body)
        except Exception:
            ack = None  # reported below, outside the handler, so nothing is chained
        if ack is not None and _usable(ack):
            return ack
        raise OrderStatusUnknownError(
            endpoint,
            f"{endpoint} was accepted by the server (stat Ok) but the acknowledgement "
            "could not be parsed or carried no order number; "
            "check orders()/order_history() before acting",
            http_status=None,
            body=json.dumps(redact(body), default=str),
        )

    return _parse


def _ack_text(value: Any) -> str | None:
    """A non-blank ``str``/``int`` ack field as text, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text or None


def modify_ack_order_id(ack: OrderAck, order_id: str) -> str:
    """The order id a ``ModifyOrder`` ack confirms (spec A1, amended for ModifyOrder).

    A ``stat: Ok`` ModifyOrder ack is a definitive acceptance, so it is never an unknown
    outcome: use ``norenordno``, else a ``result`` field, else the id the caller modified.
    """
    return _ack_text(ack.order_id) or _ack_text(ack.raw.get("result")) or order_id


# State-changing endpoints: never retried (an ambiguous outcome raises
# ``OrderStatusUnknownError``). Do not add retries to these specs.
PLACE_ORDER: EndpointSpec[OrderAck] = EndpointSpec(
    name="PlaceOrder",
    parse=_parse_ack("PlaceOrder", require_order_id=True),
    state_changing=True,
    rate_group="place_order",
)

MODIFY_ORDER: EndpointSpec[OrderAck] = EndpointSpec(
    name="ModifyOrder",
    parse=_parse_ack("ModifyOrder", require_order_id=False),
    state_changing=True,
)

CANCEL_ORDER: EndpointSpec[OrderAck] = EndpointSpec(
    name="CancelOrder",
    parse=_parse_ack("CancelOrder", require_order_id=False),
    state_changing=True,
)

EXIT_SNO_ORDER: EndpointSpec[OrderAck] = EndpointSpec(
    name="ExitSNOOrder",
    parse=_parse_ack("ExitSNOOrder", require_order_id=False),
    state_changing=True,
)


def order_book_call(uid: str) -> Call[list[Order]]:
    """Build the ``OrderBook`` call (docs and examples send ``uid`` only)."""
    return Call(ORDER_BOOK, {"uid": uid})


def order_history_call(uid: str, order_id: str) -> Call[list[OrderHistoryEntry]]:
    """Build the ``SingleOrdHist`` call."""
    return Call(SINGLE_ORD_HIST, {"uid": uid, "norenordno": require_str(order_id, "norenordno")})


def trade_book_call(uid: str) -> Call[list[Trade]]:
    """Build the ``TradeBook`` call."""
    return Call(TRADE_BOOK, {"uid": uid, "actid": uid})


def order_margin_call(
    uid: str,
    *,
    exchange: Exchange | str,
    tradingsymbol: str,
    quantity: int,
    price: PriceInput,
    product: Product | str,
    transaction_type: TransactionType | str,
    order_type: OrderType | str,
    trigger_price: PriceInput | None = None,
    book_loss_price: PriceInput | None = None,
    order_id: str | None = None,
    sno_number: str | None = None,
) -> Call[OrderMargin]:
    """Build the ``GetOrderMargin`` call (a read: not state-changing, so retried)."""
    request = validate_request(
        OrderMarginRequest,
        uid=uid,
        actid=uid,
        exch=exchange,
        tsym=tradingsymbol,
        qty=quantity,
        prc=price,
        prd=product,
        trantype=transaction_type,
        prctyp=order_type,
        trgprc=trigger_price,
        blprc=book_loss_price,
        norenordno=order_id,
        snonum=sno_number,
    )
    return Call(GET_ORDER_MARGIN, request.to_wire())


def place_order_call(
    uid: str,
    *,
    exchange: Exchange | str,
    tradingsymbol: str,
    quantity: int,
    transaction_type: TransactionType | str,
    product: Product | str,
    order_type: OrderType | str,
    price: PriceInput | None = None,
    trigger_price: PriceInput | None = None,
    validity: Validity | str = Validity.DAY,
    disclosed_quantity: int | None = None,
    amo: bool = False,
    tag: str | None = None,
    book_loss_price: PriceInput | None = None,
    book_profit_price: PriceInput | None = None,
    trail_price: PriceInput | None = None,
    market_protection: PriceInput | None = None,
    algo_id: str | None = None,
) -> Call[OrderAck]:
    """Build the ``PlaceOrder`` call (``amo`` always sent; other optional keys only when given)."""
    request = validate_request(
        PlaceOrderRequest,
        uid=uid,
        actid=uid,
        exch=exchange,
        tsym=tradingsymbol,
        qty=quantity,
        prc=price,
        trgprc=trigger_price,
        dscqty=disclosed_quantity,
        prd=product,
        trantype=transaction_type,
        prctyp=order_type,
        ret=validity,
        amo=amo,
        remarks=tag,
        blprc=book_loss_price,
        bpprc=book_profit_price,
        trailprc=trail_price,
        mkt_protection=market_protection,
        algo_id=algo_id,
    )
    return Call(PLACE_ORDER, request.to_wire())


def modify_order_call(
    uid: str,
    *,
    order_id: str,
    exchange: Exchange | str,
    tradingsymbol: str,
    quantity: int,
    price: PriceInput | None,
    order_type: OrderType | str,
    product: Product | str,
    trigger_price: PriceInput | None = None,
    validity: Validity | str | None = None,
    tag: str | None = None,
    book_loss_price: PriceInput | None = None,
    book_profit_price: PriceInput | None = None,
    trail_price: PriceInput | None = None,
    algo_id: str | None = None,
) -> Call[OrderAck]:
    """Build the ``ModifyOrder`` call."""
    request = validate_request(
        ModifyOrderRequest,
        uid=uid,
        actid=uid,
        exch=exchange,
        norenordno=order_id,
        tsym=tradingsymbol,
        qty=quantity,
        prc=price,
        prctyp=order_type,
        prd=product,
        trgprc=trigger_price,
        ret=validity,
        remarks=tag,
        blprc=book_loss_price,
        bpprc=book_profit_price,
        trailprc=trail_price,
        algo_id=algo_id,
    )
    return Call(MODIFY_ORDER, request.to_wire())


def cancel_order_call(uid: str, order_id: str) -> Call[OrderAck]:
    """Build the ``CancelOrder`` call (payload ``uid``, ``norenordno``)."""
    request = validate_request(CancelOrderRequest, uid=uid, norenordno=order_id)
    return Call(CANCEL_ORDER, request.to_wire())


def exit_sno_order_call(uid: str, order_id: str, product: Product | str) -> Call[OrderAck]:
    """Build the ``ExitSNOOrder`` call; ``product`` must be ``H`` (cover) or ``B`` (bracket)."""
    request = validate_request(ExitSnoOrderRequest, uid=uid, norenordno=order_id, prd=product)
    return Call(EXIT_SNO_ORDER, request.to_wire())
