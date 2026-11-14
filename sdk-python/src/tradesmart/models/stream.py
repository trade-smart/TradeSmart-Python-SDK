"""WebSocket message models (Noren streaming protocol)."""

from typing import Any, ClassVar

from pydantic import Field

from tradesmart.models.base import (
    EpochTime,
    ISTDateTime,
    LenientStr,
    OrderStatusField,
    OrderTypeField,
    Price,
    ProductField,
    Quantity,
    StreamPrice,
    TradeSmartModel,
    TransactionTypeField,
    ValidityField,
    Volume,
)


class StreamMessage(TradeSmartModel):
    """Base of every inbound stream message; ``t`` is the task type."""

    t: str = ""


class ConnectAck(StreamMessage):
    """``ak``: connect acknowledgement (``s`` is ``Ok`` / ``Not_Ok``)."""

    user_id: LenientStr = Field(default=None, alias="uid")
    status: LenientStr = Field(default=None, alias="s")


class _Touchline(StreamMessage):
    exchange: LenientStr = Field(default=None, alias="e")
    token: LenientStr = Field(default=None, alias="tk")
    tradingsymbol: LenientStr = Field(default=None, alias="ts")
    price_precision: Quantity = Field(default=None, alias="pp")
    tick_size: Price = Field(default=None, alias="ti")
    lot_size: Quantity = Field(default=None, alias="ls")
    last_price: StreamPrice = Field(default=None, alias="lp")
    percent_change: LenientStr = Field(default=None, alias="pc")
    volume: Volume = Field(default=None, alias="v")
    open: StreamPrice = Field(default=None, alias="o")
    high: StreamPrice = Field(default=None, alias="h")
    low: StreamPrice = Field(default=None, alias="l")
    close: StreamPrice = Field(default=None, alias="c")
    average_trade_price: StreamPrice = Field(default=None, alias="ap")
    open_interest: Volume = Field(default=None, alias="oi")
    poi: Volume = None
    toi: Volume = None
    bq1: Quantity = None
    bp1: StreamPrice = None
    sq1: Quantity = None
    sp1: StreamPrice = None
    ft: EpochTime = None
    ord_msg: LenientStr = None

    @property
    def key(self) -> str:
        """Instrument key ``EXCH|TOKEN``."""
        return f"{self.exchange}|{self.token}"


class TouchlineAck(_Touchline):
    """``tk``: touchline subscription acknowledgement."""


class TouchlineFeed(_Touchline):
    """``tf``: touchline update (only changed fields besides ``t``/``e``/``tk``)."""


class _Depth(_Touchline):
    cp: Price = None
    ltt: LenientStr = None
    ltq: Quantity = None
    tbq: Quantity = None
    tsq: Quantity = None
    bp2: StreamPrice = None
    bp3: StreamPrice = None
    bp4: StreamPrice = None
    bp5: StreamPrice = None
    sp2: StreamPrice = None
    sp3: StreamPrice = None
    sp4: StreamPrice = None
    sp5: StreamPrice = None
    bq2: Quantity = None
    bq3: Quantity = None
    bq4: Quantity = None
    bq5: Quantity = None
    sq2: Quantity = None
    sq3: Quantity = None
    sq4: Quantity = None
    sq5: Quantity = None
    bo1: Quantity = None
    bo2: Quantity = None
    bo3: Quantity = None
    bo4: Quantity = None
    bo5: Quantity = None
    so1: Quantity = None
    so2: Quantity = None
    so3: Quantity = None
    so4: Quantity = None
    so5: Quantity = None
    lc: StreamPrice = None
    uc: StreamPrice = None
    high_52w: StreamPrice = Field(default=None, alias="52h")
    low_52w: StreamPrice = Field(default=None, alias="52l")


class DepthAck(_Depth):
    """``dk``: depth subscription acknowledgement."""


class DepthFeed(_Depth):
    """``df``: depth update (only changed fields besides ``t``/``e``/``tk``)."""


class OrderUpdate(StreamMessage):
    """``om``: order update feed."""

    lenient_parsing: ClassVar[bool] = True

    order_id: LenientStr = Field(default=None, alias="norenordno")
    user_id: LenientStr = Field(default=None, alias="uid")
    account_id: LenientStr = Field(default=None, alias="actid")
    exchange: LenientStr = Field(default=None, alias="exch")
    tradingsymbol: LenientStr = Field(default=None, alias="tsym")
    quantity: Quantity = Field(default=None, alias="qty")
    price: StreamPrice = Field(default=None, alias="prc")
    pcode: LenientStr = None
    status: OrderStatusField = None
    reporttype: LenientStr = None
    transaction_type: TransactionTypeField = Field(default=None, alias="trantype")
    order_type: OrderTypeField = Field(default=None, alias="prctyp")
    validity: ValidityField = Field(default=None, alias="ret")
    filled_quantity: Quantity = Field(default=None, alias="fillshares")
    average_price: StreamPrice = Field(default=None, alias="avgprc")
    fltm: LenientStr = None
    flid: LenientStr = None
    flqty: Quantity = None
    flprc: StreamPrice = None
    reject_reason: LenientStr = Field(default=None, alias="rejreason")
    exchange_order_id: LenientStr = Field(default=None, alias="exchordid")
    cancelqty: Quantity = None
    tag: LenientStr = Field(default=None, alias="remarks")
    disclosed_quantity: Quantity = Field(default=None, alias="dscqty")
    trigger_price: StreamPrice = Field(default=None, alias="trgprc")
    sno_number: LenientStr = Field(default=None, alias="snonum")
    snoordt: LenientStr = None
    book_loss_price: Price = Field(default=None, alias="blprc")
    book_profit_price: Price = Field(default=None, alias="bpprc")
    trail_price: Price = Field(default=None, alias="trailprc")
    exch_tm: ISTDateTime = None
    amo: LenientStr = None
    tm: LenientStr = None
    ntm: LenientStr = None


class PositionAck(StreamMessage):
    """``pk``: position subscription acknowledgement."""

    lenient_parsing: ClassVar[bool] = True

    user_id: LenientStr = Field(default=None, alias="uid")


class PositionUpdate(StreamMessage):
    """``pm``: position feed."""

    lenient_parsing: ClassVar[bool] = True

    exchange: LenientStr = Field(default=None, alias="exch")
    token: LenientStr = None
    tradingsymbol: LenientStr = Field(default=None, alias="tsym")
    user_id: LenientStr = Field(default=None, alias="uid")
    account_id: LenientStr = Field(default=None, alias="actid")
    product: ProductField = Field(default=None, alias="prd")
    daybuyqty: Quantity = None
    daysellqty: Quantity = None
    daybuyamt: Price = None
    daysellamt: Price = None
    cfbuyqty: Quantity = None
    cfsellqty: Quantity = None
    cfbuyamt: Price = None
    cfsellamt: Price = None
    openbuyqty: Quantity = None
    opensellqty: Quantity = None
    openbuyamt: Price = None
    opensellamt: Price = None
    buyavgprc: Price = None
    sellavgprc: Price = None
    realized_pnl: Price = Field(default=None, alias="rpnl")
    net_quantity: Quantity = Field(default=None, alias="netqty")
    totbuyamt: Price = None
    totsellamt: Price = None
    totbuyavgprc: Price = None
    totsellavgprc: Price = None
    child_orders: list[dict[str, Any]] | None = None


class UnsubscribeAck(StreamMessage):
    """``uk`` / ``udk`` / ``uok`` / ``upk``: unsubscribe acknowledgement."""

    k: LenientStr = None


class HeartbeatAck(StreamMessage):
    """``hk``: heartbeat acknowledgement (``ft`` in seconds)."""

    ft: EpochTime = None


class UnknownMessage(StreamMessage):
    """A message that is not valid JSON, has an unknown ``t``, or failed validation."""
