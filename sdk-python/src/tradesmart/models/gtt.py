"""Models for the GTT endpoints (``PlaceGTTOrder`` ... ``GetEnabledGTTs``).

A GTT is an order held on the server as an alert: when the live value chosen by ``ai_t``
crosses ``d``, the order described by the other fields is sent to the exchange. A GTT whose
condition is already true when it is placed fires at once.
"""

from decimal import Decimal
from typing import Annotated, Any, ClassVar

from pydantic import ConfigDict, Field, StrictStr, field_validator, model_validator

from tradesmart.enums import (
    Exchange,
    GttAlertType,
    GttValidity,
    OrderType,
    Product,
    TransactionType,
    Validity,
)
from tradesmart.models.base import (
    GttAlertTypeField,
    GttValidityField,
    ISTDateTime,
    LenientStr,
    Money,
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
from tradesmart.models.orders import (
    _check_price_rules,
    _check_trigger_type,
    _default_market_price,
)

# --- responses ---------------------------------------------------------------------------------


class Gtt(TradeSmartModel):
    """One pending GTT of ``GetPendingGTTOrder``.

    ``alert_validity`` is the GTT's own ``validity`` (DAY/GTT); ``validity`` is the retention
    (``ret``) of the order it places. Fields are read by wire key only, so a row without ``ret``
    leaves ``validity`` ``None`` (it is never filled from the wire ``validity``).
    """

    # By wire key only: the field ``validity`` (wire ``ret``) must not be filled by its name
    # from the wire ``validity`` (DAY/GTT), which is ``alert_validity``.
    model_config = ConfigDict(populate_by_name=False)

    lenient_parsing: ClassVar[bool] = True
    lenient_protected_keys: ClassVar[frozenset[str]] = frozenset({"al_id"})

    stat: LenientStr = None
    alert_id: LenientStr = Field(default=None, alias="al_id")
    alert_type: GttAlertTypeField = Field(default=None, alias="ai_t")
    trigger_value: Price = Field(default=None, alias="d")
    alert_validity: GttValidityField = Field(default=None, alias="validity")
    exchange: LenientStr = Field(default=None, alias="exch")
    tradingsymbol: LenientStr = Field(default=None, alias="tsym")
    token: LenientStr = None
    tag: LenientStr = Field(default=None, alias="remarks")
    transaction_type: TransactionTypeField = Field(default=None, alias="trantype")
    order_type: OrderTypeField = Field(default=None, alias="prctyp")
    product: ProductField = Field(default=None, alias="prd")
    s_prdt_ali: LenientStr = None
    validity: ValidityField = Field(default=None, alias="ret")
    account_id: LenientStr = Field(default=None, alias="actid")
    quantity: Quantity = Field(default=None, alias="qty")
    price: Price = Field(default=None, alias="prc")
    trigger_price: Price = Field(default=None, alias="trgprc")
    price_precision: Quantity = Field(default=None, alias="pp")
    lot_size: Quantity = Field(default=None, alias="ls")
    ordersource: LenientStr = None


class TriggeredGtt(Gtt):
    """One triggered GTT of ``GetTrgOi`` (its order was sent; see ``Order.parent_id``).

    The test environment also lists cancelled GTTs here, with ``stat`` ``CANCELLED`` (spec F32).
    """

    # In the docs' example only (not in its field table): presumably the trigger time.
    norentm: ISTDateTime = None
    sms_flag: LenientStr = None
    email_flag: LenientStr = None
    push_flag: LenientStr = None


# --- requests ----------------------------------------------------------------------------------

# A non-blank string (no number or bool): the GTT tag and the alert id.
_NonBlank = Annotated[StrictStr, Field(pattern=r"\S")]

_EXCHANGES = (Exchange.NSE, Exchange.NFO, Exchange.BSE, Exchange.BFO, Exchange.MCX)
_PRODUCTS = (Product.CNC, Product.NRML, Product.CO)


class _GttRules(RequestModel):
    """The checks shared by the Place and Modify GTT requests (fields are wire keys)."""

    @model_validator(mode="before")
    @classmethod
    def _market_price_default(cls, data: Any) -> Any:
        """A MKT / SL-MKT order without a price is sent with ``prc`` 0, as in ``place_order``."""
        return _default_market_price(data)

    @field_validator("exch", check_fields=False)
    @classmethod
    def _documented_exchange(cls, value: Exchange) -> Exchange:
        if value not in _EXCHANGES:
            raise ValueError("a GTT is offered on NSE, NFO, BSE, BFO and MCX only")
        return value

    @field_validator("prd", check_fields=False)
    @classmethod
    def _documented_product(cls, value: Product) -> Product:
        if value not in _PRODUCTS:
            raise ValueError("a GTT order's product must be CNC, NRML or CO (not MIS, BO or MTF)")
        return value

    @model_validator(mode="after")
    def _check_rules(self) -> "_GttRules":
        values = vars(self)
        order_type: OrderType = values["prctyp"]
        trigger_price: Decimal | None = values["trgprc"]
        _check_price_rules(order_type, values["prc"], trigger_price)
        _check_trigger_type(order_type, trigger_price)
        disclosed: int | None = values["dscqty"]
        if disclosed is not None and disclosed > values["qty"]:
            raise ValueError("disclosed_quantity cannot exceed quantity")
        return self


class PlaceGttRequest(_GttRules):
    """Validated ``PlaceGTTOrder`` parameters (field order is the wire key order)."""

    uid: str
    actid: str
    exch: Exchange
    tsym: str
    ai_t: GttAlertType
    d: Money
    validity: GttValidity
    remarks: _NonBlank
    trantype: TransactionType
    prctyp: OrderType
    prd: Product
    ret: Validity
    qty: StrictQuantity = Field(ge=1)
    prc: Money = Field(ge=0)
    trgprc: Money | None = Field(default=None, ge=0)
    dscqty: StrictQuantity | None = Field(default=None, ge=0)


class ModifyGttRequest(_GttRules):
    """Validated ``ModifyGTTOrder`` parameters: every Place field again, ``al_id`` third."""

    uid: str
    actid: str
    al_id: _NonBlank
    exch: Exchange
    tsym: str
    ai_t: GttAlertType
    d: Money
    validity: GttValidity
    remarks: _NonBlank
    trantype: TransactionType
    prctyp: OrderType
    prd: Product
    ret: Validity
    qty: StrictQuantity = Field(ge=1)
    prc: Money = Field(ge=0)
    trgprc: Money | None = Field(default=None, ge=0)
    dscqty: StrictQuantity | None = Field(default=None, ge=0)


class CancelGttRequest(RequestModel):
    """Validated ``CancelGTTOrder`` parameters (``uid`` and ``al_id`` only, as documented)."""

    uid: str
    al_id: _NonBlank
