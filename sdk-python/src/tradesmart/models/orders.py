"""Models for the order, trade and margin read endpoints."""

from decimal import Decimal
from typing import Annotated, Any, ClassVar

from pydantic import Field, StrictStr, model_validator

from tradesmart.enums import Exchange, OrderType, Product, TransactionType, Validity
from tradesmart.models.base import (
    EpochTime,
    ISTDateTime,
    LenientStr,
    Money,
    OrderStatusField,
    OrderTypeField,
    Price,
    ProductField,
    Quantity,
    RequestModel,
    StrictQuantity,
    TradeSmartModel,
    TransactionTypeField,
    ValidityField,
)


class Order(TradeSmartModel):
    """One order of ``OrderBook``.

    ``status`` is an ``OrderStatus`` member when known, else the raw string (values vary);
    compare it with ``OrderStatus`` members, which are ``str`` subclasses.
    """

    lenient_parsing: ClassVar[bool] = True

    stat: LenientStr = None
    exchange: LenientStr = Field(default=None, alias="exch")
    tradingsymbol: LenientStr = Field(default=None, alias="tsym")
    user_id: LenientStr = Field(default=None, alias="uid")
    account_id: LenientStr = Field(default=None, alias="actid")
    order_id: LenientStr = Field(default=None, alias="norenordno")
    price: Price = Field(default=None, alias="prc")
    quantity: Quantity = Field(default=None, alias="qty")
    market_protection: Price = Field(default=None, alias="mkt_protection")
    product: ProductField = Field(default=None, alias="prd")
    s_prdt_ali: LenientStr = None
    status: OrderStatusField = None
    transaction_type: TransactionTypeField = Field(default=None, alias="trantype")
    order_type: OrderTypeField = Field(default=None, alias="prctyp")
    filled_quantity: Quantity = Field(default=None, alias="fillshares")
    average_price: Price = Field(default=None, alias="avgprc")
    reject_reason: LenientStr = Field(default=None, alias="rejreason")
    exchange_order_id: LenientStr = Field(default=None, alias="exchordid")
    cancelqty: Quantity = None
    tag: LenientStr = Field(default=None, alias="remarks")
    disclosed_quantity: Quantity = Field(default=None, alias="dscqty")
    trigger_price: Price = Field(default=None, alias="trgprc")
    validity: ValidityField = Field(default=None, alias="ret")
    book_profit_price: Price = Field(default=None, alias="bpprc")
    book_loss_price: Price = Field(default=None, alias="blprc")
    trail_price: Price = Field(default=None, alias="trailprc")
    amo: LenientStr = None
    price_precision: Quantity = Field(default=None, alias="pp")
    tick_size: Price = Field(default=None, alias="ti")
    lot_size: Quantity = Field(default=None, alias="ls")
    token: LenientStr = None
    norentm: ISTDateTime = None
    ordenttm: EpochTime = None
    exch_tm: ISTDateTime = None
    sno_number: LenientStr = Field(default=None, alias="snonum")
    sno_fillid: LenientStr = None
    prcftr: LenientStr = None
    mult: LenientStr = None
    dname: LenientStr = None
    cname: LenientStr = None
    # Present in the official example but absent from the docs table (meaning undocumented).
    kidid: LenientStr = None
    rejby: LenientStr = None
    src_uid: LenientStr = None
    ipaddr: LenientStr = None
    instname: LenientStr = None
    ordersource: LenientStr = None
    symname: LenientStr = None
    rprc: Price = None
    brnchid: LenientStr = None
    C: LenientStr = None
    st_intrn: LenientStr = None
    algo_id: LenientStr = None
    rqty: Quantity = None
    ui_dev_code: LenientStr = None
    # The GTT (alert) id of an order a triggered GTT placed (GTT docs: "add_ord_id holds the
    # GTT order ID"; absent from the order book's field table).
    parent_id: LenientStr = Field(default=None, alias="add_ord_id")


class OrderHistoryEntry(Order):
    """One state transition of ``SingleOrdHist`` (an ``Order`` plus history-only fields)."""

    pan: LenientStr = None
    rpt: LenientStr = None
    introp_exch: LenientStr = None


class Trade(TradeSmartModel):
    """One executed trade of ``TradeBook``."""

    lenient_parsing: ClassVar[bool] = True

    stat: LenientStr = None
    user_id: LenientStr = Field(default=None, alias="uid")
    account_id: LenientStr = Field(default=None, alias="actid")
    exchange: LenientStr = Field(default=None, alias="exch")
    tradingsymbol: LenientStr = Field(default=None, alias="tsym")
    order_id: LenientStr = Field(default=None, alias="norenordno")
    quantity: Quantity = Field(default=None, alias="qty")
    product: ProductField = Field(default=None, alias="prd")
    s_prdt_ali: LenientStr = None
    transaction_type: TransactionTypeField = Field(default=None, alias="trantype")
    order_type: OrderTypeField = Field(default=None, alias="prctyp")
    filled_quantity: Quantity = Field(default=None, alias="fillshares")
    average_price: Price = Field(default=None, alias="avgprc")
    exchange_order_id: LenientStr = Field(default=None, alias="exchordid")
    tag: LenientStr = Field(default=None, alias="remarks")
    validity: ValidityField = Field(default=None, alias="ret")
    price_precision: Quantity = Field(default=None, alias="pp")
    tick_size: Price = Field(default=None, alias="ti")
    lot_size: Quantity = Field(default=None, alias="ls")
    cstFrm: LenientStr = None
    fltm: ISTDateTime = None
    flid: LenientStr = None
    flqty: Quantity = None
    flprc: Price = None
    ordersource: LenientStr = None
    token: LenientStr = None
    norentm: ISTDateTime = None
    exch_tm: ISTDateTime = None
    snoordt: LenientStr = None
    sno_number: LenientStr = Field(default=None, alias="snonum")
    price: Price = Field(default=None, alias="prc")
    mult: LenientStr = None


class OrderMargin(TradeSmartModel):
    """Result of ``GetOrderMargin``."""

    stat: LenientStr = None
    request_time: ISTDateTime = None
    remarks: LenientStr = None
    cash: Price = None
    marginused: Price = None
    ordermargin: Price = None
    marginusedprev: Price = None


class OrderMarginRequest(RequestModel):
    """Validated ``GetOrderMargin`` parameters (field order is the wire key order)."""

    uid: str
    actid: str
    exch: Exchange
    tsym: str
    qty: StrictQuantity = Field(ge=1)
    prc: Money = Field(ge=0)
    prd: Product
    trantype: TransactionType
    prctyp: OrderType
    trgprc: Money | None = Field(default=None, ge=0)
    blprc: Money | None = Field(default=None, ge=0)
    norenordno: str | None = None
    snonum: str | None = None


class OrderAck(TradeSmartModel):
    """OMS acknowledgement of ``PlaceOrder``, ``ModifyOrder``, ``CancelOrder`` or ``ExitSNOOrder``.

    An acknowledgement is not an execution: confirm via ``order_history()``.
    """

    stat: LenientStr = None
    order_id: LenientStr = Field(default=None, alias="norenordno")
    display_message: LenientStr = Field(default=None, alias="dmsg")
    request_time: ISTDateTime = None


# A non-blank string (no bool or number): an exchange-approved algo ID (F29).
AlgoId = Annotated[StrictStr, Field(pattern=r"\S")]

_MARKET_TYPES = (OrderType.MARKET, OrderType.SL_M)
_STOP_TYPES = (OrderType.SL, OrderType.SL_M)


def _default_market_price(data: Any) -> Any:
    """Give MKT / SL-MKT orders a price of 0 when the caller supplied none.

    Works on input keyed by field name (``order_type``, ``price``) or by wire key (``prctyp``,
    ``prc``); the default is written under the wire key ``prc``, which every order request model
    accepts (as its alias, or as its field name for the wire-keyed GTT requests).
    """
    if not isinstance(data, dict):
        return data
    raw_type = data.get("order_type", data.get("prctyp"))
    if not isinstance(raw_type, str):
        return data  # no price type, or not text: field validation reports it
    try:
        order_type = OrderType(raw_type)
    except ValueError:
        return data  # field validation reports the bad price type
    if order_type in _MARKET_TYPES and data.get("price", data.get("prc")) is None:
        data = {k: v for k, v in data.items() if k not in ("price", "prc")}
        data["prc"] = Decimal("0")
    return data


def _check_price_rules(
    order_type: OrderType, price: Decimal, trigger_price: Decimal | None
) -> None:
    if order_type not in _MARKET_TYPES and price <= 0:
        raise ValueError(f"price must be greater than 0 for {order_type.value} orders")
    if order_type in _STOP_TYPES and (trigger_price is None or trigger_price <= 0):
        raise ValueError(
            f"trigger_price is required and must be greater than 0 for {order_type.value}"
        )


def _check_trigger_type(order_type: OrderType, trigger_price: Decimal | None) -> None:
    if trigger_price is not None and order_type not in _STOP_TYPES:
        raise ValueError("trigger_price applies to SL-LMT and SL-MKT orders only")


def _check_trigger_and_legs(
    order_type: OrderType,
    product: Product,
    trigger_price: Decimal | None,
    book_loss_price: Decimal | None,
    book_profit_price: Decimal | None,
    trail_price: Decimal | None,
) -> None:
    _check_trigger_type(order_type, trigger_price)
    if product not in (Product.CO, Product.BO) and any(
        v is not None for v in (book_loss_price, book_profit_price, trail_price)
    ):
        raise ValueError(
            "book_loss_price, book_profit_price and trail_price apply to cover/bracket orders only"
        )
    if book_profit_price is not None and product != Product.BO:
        raise ValueError("book_profit_price applies to bracket orders only")


class PlaceOrderRequest(RequestModel):
    """Validated ``PlaceOrder`` parameters (field order is the wire key order)."""

    user_id: str = Field(alias="uid")
    account_id: str = Field(alias="actid")
    exchange: Exchange = Field(alias="exch")
    tradingsymbol: str = Field(alias="tsym")
    quantity: StrictQuantity = Field(alias="qty", ge=1)
    price: Money = Field(alias="prc", ge=0)
    trigger_price: Money | None = Field(default=None, alias="trgprc", ge=0)
    disclosed_quantity: StrictQuantity | None = Field(default=None, alias="dscqty", ge=0)
    product: Product = Field(alias="prd")
    transaction_type: TransactionType = Field(alias="trantype")
    order_type: OrderType = Field(alias="prctyp")
    validity: Validity = Field(default=Validity.DAY, alias="ret")
    amo: bool = False
    tag: str | None = Field(default=None, alias="remarks")
    ordersource: str = "API"
    book_loss_price: Money | None = Field(default=None, alias="blprc", ge=0)
    book_profit_price: Money | None = Field(default=None, alias="bpprc", ge=0)
    trail_price: Money | None = Field(default=None, alias="trailprc", ge=0)
    market_protection: Money | None = Field(default=None, alias="mkt_protection", ge=0)
    # Docs: optional, "Required when placing more than 10 orders per second"; sent last.
    algo_id: AlgoId | None = None

    @model_validator(mode="before")
    @classmethod
    def _market_price_default(cls, data: Any) -> Any:
        return _default_market_price(data)

    @model_validator(mode="after")
    def _check_rules(self) -> "PlaceOrderRequest":
        _check_price_rules(self.order_type, self.price, self.trigger_price)
        if self.disclosed_quantity is not None and self.disclosed_quantity > self.quantity:
            raise ValueError("disclosed_quantity cannot exceed quantity")
        if self.product == Product.CO and self.book_loss_price is None:
            raise ValueError("book_loss_price is required for cover orders")
        if self.product == Product.BO and (
            self.book_loss_price is None or self.book_profit_price is None
        ):
            raise ValueError(
                "book_loss_price and book_profit_price are required for bracket orders"
            )
        _check_trigger_and_legs(
            self.order_type,
            self.product,
            self.trigger_price,
            self.book_loss_price,
            self.book_profit_price,
            self.trail_price,
        )
        if self.product == Product.MTF and self.exchange == Exchange.MCX:
            raise ValueError("MTF is not offered for MCX")
        return self


class ModifyOrderRequest(RequestModel):
    """Validated ``ModifyOrder`` parameters (field order is the wire key order)."""

    user_id: str = Field(alias="uid")
    account_id: str = Field(alias="actid")
    exchange: Exchange = Field(alias="exch")
    order_id: str = Field(alias="norenordno", min_length=1)
    tradingsymbol: str = Field(alias="tsym")
    quantity: StrictQuantity = Field(alias="qty", ge=1)
    price: Money = Field(alias="prc", ge=0)
    order_type: OrderType = Field(alias="prctyp")
    product: Product = Field(alias="prd")
    trigger_price: Money | None = Field(default=None, alias="trgprc", ge=0)
    validity: Validity | None = Field(default=None, alias="ret")
    tag: str | None = Field(default=None, alias="remarks")
    ordersource: str = "API"
    book_loss_price: Money | None = Field(default=None, alias="blprc", ge=0)
    book_profit_price: Money | None = Field(default=None, alias="bpprc", ge=0)
    trail_price: Money | None = Field(default=None, alias="trailprc", ge=0)
    # Docs: optional, "Required when sending more than 10 orders per second"; sent last.
    algo_id: AlgoId | None = None

    @model_validator(mode="before")
    @classmethod
    def _market_price_default(cls, data: Any) -> Any:
        return _default_market_price(data)

    @model_validator(mode="after")
    def _check_rules(self) -> "ModifyOrderRequest":
        _check_price_rules(self.order_type, self.price, self.trigger_price)
        _check_trigger_and_legs(
            self.order_type,
            self.product,
            self.trigger_price,
            self.book_loss_price,
            self.book_profit_price,
            self.trail_price,
        )
        return self


class CancelOrderRequest(RequestModel):
    """Validated ``CancelOrder`` parameters."""

    uid: str
    norenordno: str = Field(min_length=1)


class ExitSnoOrderRequest(RequestModel):
    """Validated ``ExitSNOOrder`` parameters (product is cover ``H`` or bracket ``B`` only)."""

    uid: str
    norenordno: str = Field(min_length=1)
    prd: Product

    @model_validator(mode="after")
    def _check_product(self) -> "ExitSnoOrderRequest":
        if self.prd not in (Product.CO, Product.BO):
            raise ValueError("product must be H (cover) or B (bracket)")
        return self
