"""Request and response models for the portfolio endpoints."""

from typing import ClassVar

from pydantic import Field

from tradesmart.enums import Product
from tradesmart.models.base import (
    ISTDateTime,
    LenientStr,
    Price,
    ProductField,
    Quantity,
    RequestModel,
    TradeSmartModel,
)


class HoldingsRequest(RequestModel):
    """Validated ``Holdings`` parameters: ``prd`` resolves like the ``place_order`` product."""

    uid: str
    actid: str
    prd: Product


class HoldingInstrument(TradeSmartModel):
    """One exchange listing (``exch_tsym`` element) of a holding."""

    exchange: LenientStr = Field(default=None, alias="exch")
    tradingsymbol: LenientStr = Field(default=None, alias="tsym")
    token: LenientStr = None
    price_precision: Quantity = Field(default=None, alias="pp")
    tick_size: Price = Field(default=None, alias="ti")
    lot_size: Quantity = Field(default=None, alias="ls")
    last_price: Price = Field(default=None, alias="lp")
    cname: LenientStr = None
    dname: LenientStr = None


class Holding(TradeSmartModel):
    """One holding record of ``Holdings``."""

    lenient_parsing: ClassVar[bool] = True

    stat: LenientStr = None
    instruments: list[HoldingInstrument] | None = Field(default=None, alias="exch_tsym")
    holdqty: Quantity = None
    dpqty: Quantity = None
    npoadqty: Quantity = None
    brkcolqty: Quantity = None
    colqty: Quantity = None
    benqty: Quantity = None
    unplgdqty: Quantity = None
    btstqty: Quantity = None
    btstcolqty: Quantity = None
    usedqty: Quantity = None
    upldprc: Price = None


class Limits(TradeSmartModel):
    """Result of ``Limits``."""

    stat: LenientStr = None
    request_time: ISTDateTime = None
    account_id: LenientStr = Field(default=None, alias="actid")
    product: ProductField = Field(default=None, alias="prd")
    seg: LenientStr = None
    exchange: LenientStr = Field(default=None, alias="exch")
    prfname: LenientStr = None
    remarks_text: LenientStr = None
    realized_pnl: Price = Field(default=None, alias="rpnl")
    cash: Price = None
    payin: Price = None
    payout: Price = None
    remarks_amt: Price = None
    cac_daycash: Price = None
    brkcollamt: Price = None
    unclearedcash: Price = None
    daycash: Price = None
    marginused: Price = None
    mtomcurper: Price = None
    cbu: Price = None
    csc: Price = None
    unmtom: Price = None
    marprt: Price = None
    span: Price = None
    expo: Price = None
    premium: Price = None
    varelm: Price = None
    grexpo: Price = None
    greexpo_d: Price = None
    scripbskmar: Price = None
    addscripbskmrg: Price = None
    brokerage: Price = None
    collateral: Price = None
    cash_coll: Price = None
    grcoll: Price = None
    turnoverlmt: Price = None
    pendordvallmt: Price = None
    aux_daycash: Price = None
    aux_brkcollamt: Price = None
    aux_unclearedcash: Price = None
    blk_amt: Price = None


class Position(TradeSmartModel):
    """One open position of ``PositionBook``."""

    lenient_parsing: ClassVar[bool] = True

    stat: LenientStr = None
    request_time: ISTDateTime = None
    exchange: LenientStr = Field(default=None, alias="exch")
    tradingsymbol: LenientStr = Field(default=None, alias="tsym")
    token: LenientStr = None
    user_id: LenientStr = Field(default=None, alias="uid")
    account_id: LenientStr = Field(default=None, alias="actid")
    product: ProductField = Field(default=None, alias="prd")
    s_prdt_ali: LenientStr = None
    net_quantity: Quantity = Field(default=None, alias="netqty")
    last_price: Price = Field(default=None, alias="lp")
    realized_pnl: Price = Field(default=None, alias="rpnl")
    price_precision: Quantity = Field(default=None, alias="pp")
    tick_size: Price = Field(default=None, alias="ti")
    lot_size: Quantity = Field(default=None, alias="ls")
    instname: LenientStr = None
    dname: LenientStr = None
    cname: LenientStr = None
    daybuyqty: Quantity = None
    daysellqty: Quantity = None
    cfbuyqty: Quantity = None
    cfsellqty: Quantity = None
    openbuyqty: Quantity = None
    opensellqty: Quantity = None
    netavgprc: Price = None
    dayavgprc: Price = None
    daybuyavgprc: Price = None
    daysellavgprc: Price = None
    daybuyamt: Price = None
    daysellamt: Price = None
    cforgavgprc: Price = None
    cfbuyavgprc: Price = None
    cfsellavgprc: Price = None
    cfbuyamt: Price = None
    cfsellamt: Price = None
    totbuyamt: Price = None
    totsellamt: Price = None
    totbuyavgprc: Price = None
    totsellavgprc: Price = None
    bep: Price = None
    openbuyamt: Price = None
    opensellamt: Price = None
    openbuyavgprc: Price = None
    opensellavgprc: Price = None
    mult: Price = None
    prcftr: Price = None
    upldprc: Price = None
    netupldprc: Price = None
