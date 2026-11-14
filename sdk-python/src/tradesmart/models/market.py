"""Response and request models for the market data endpoints."""

from pydantic import AliasChoices, Field, field_validator

from tradesmart.models.base import (
    EpochTime,
    ISTDateTime,
    LenientStr,
    Money,
    Price,
    Quantity,
    RequestModel,
    TradeSmartModel,
    Volume,
)

TPS_INTERVALS = ("1", "3", "5", "10", "15", "30", "60", "120", "240")


class ScripMatch(TradeSmartModel):
    """One match of ``SearchScrip``."""

    exchange: LenientStr = Field(default=None, alias="exch")
    tradingsymbol: LenientStr = Field(default=None, alias="tsym")
    token: LenientStr = None
    price_precision: Quantity = Field(default=None, alias="pp")
    tick_size: Price = Field(default=None, alias="ti")
    lot_size: Quantity = Field(default=None, alias="ls")
    weekly: LenientStr = None
    nontrd: LenientStr = None
    dname: LenientStr = None
    cname: LenientStr = None
    optt: LenientStr = None
    instname: LenientStr = None
    symname: LenientStr = None
    seg: LenientStr = None
    exd: LenientStr = None


class Quote(TradeSmartModel):
    """Result of ``GetQuotes`` (flat snapshot incl. five depth levels)."""

    stat: LenientStr = None
    request_time: ISTDateTime = None
    exchange: LenientStr = Field(default=None, alias="exch")
    tradingsymbol: LenientStr = Field(default=None, alias="tsym")
    token: LenientStr = None
    price_precision: Quantity = Field(default=None, alias="pp")
    lot_size: Quantity = Field(default=None, alias="ls")
    tick_size: Price = Field(default=None, alias="ti")
    mult: LenientStr = None
    sptprc: Price = None
    feed_time: EpochTime = Field(default=None, alias="lut")
    last_price: Price = Field(default=None, alias="lp")
    uc: Price = None
    lc: Price = None
    wk52_h: Price = None
    wk52_l: Price = None
    open_interest: Volume = Field(default=None, alias="oi")
    toi: Volume = None
    strprc: Price = None
    cname: LenientStr = None
    symname: LenientStr = None
    seg: LenientStr = None
    exd: LenientStr = None
    instname: LenientStr = None
    optt: LenientStr = None
    isin: LenientStr = None
    prcftr_d: LenientStr = None
    nav: Price = None
    open: Price = Field(default=None, alias="o")
    close: Price = Field(default=None, alias="c")
    high: Price = Field(default=None, alias="h")
    low: Price = Field(default=None, alias="l")
    volume: Volume = Field(default=None, alias="v")
    average_trade_price: Price = Field(default=None, alias="ap")
    ltq: Quantity = None
    ltt: LenientStr = None
    ltd: LenientStr = None
    tbq: Quantity = None
    tsq: Quantity = None
    bp1: Price = None
    bp2: Price = None
    bp3: Price = None
    bp4: Price = None
    bp5: Price = None
    sp1: Price = None
    sp2: Price = None
    sp3: Price = None
    sp4: Price = None
    sp5: Price = None
    bq1: Quantity = None
    bq2: Quantity = None
    bq3: Quantity = None
    bq4: Quantity = None
    bq5: Quantity = None
    sq1: Quantity = None
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
    und_exch: LenientStr = None
    und_tk: LenientStr = None
    ord_msg: LenientStr = None
    scrip_base_prc: Price = None
    issuecap: Price = None
    e_date: LenientStr = None
    cutof_all: LenientStr = None


class SecurityInfo(TradeSmartModel):
    """Result of ``GetSecurityInfo`` (accepts wire keys ``symname`` and ``symnam``)."""

    stat: LenientStr = None
    request_time: ISTDateTime = None
    exchange: LenientStr = Field(default=None, alias="exch")
    tradingsymbol: LenientStr = Field(default=None, alias="tsym")
    token: LenientStr = None
    cname: LenientStr = None
    symname: LenientStr = Field(default=None, validation_alias=AliasChoices("symname", "symnam"))
    seg: LenientStr = None
    exd: LenientStr = None
    instname: LenientStr = None
    strprc: Price = None
    optt: LenientStr = None
    isin: LenientStr = None
    tick_size: Price = Field(default=None, alias="ti")
    lot_size: Quantity = Field(default=None, alias="ls")
    price_precision: Quantity = Field(default=None, alias="pp")
    mult: LenientStr = None
    gp_nd: LenientStr = None
    prcunt: LenientStr = None
    prcqqty: Quantity = None
    trdunt: LenientStr = None
    delunt: LenientStr = None
    frzqty: Quantity = None
    gsmind: LenientStr = None
    elmbmrg: Price = None
    elmsmrg: Price = None
    addbmrg: Price = None
    addsmrg: Price = None
    splbmrg: Price = None
    splsmrg: Price = None
    delmrg: Price = None
    tenmrg: Price = None
    tenstrd: LenientStr = None
    tenendd: LenientStr = None
    exestrd: LenientStr = None
    exeendd: LenientStr = None
    mkt_t: LenientStr = None
    issue_d: LenientStr = None
    listing_d: LenientStr = None
    last_trd_d: LenientStr = None
    elmmrg: Price = None
    varmrg: Price = None
    expmrg: Price = None
    prcftr_d: LenientStr = None
    prcftr: Price = None
    weekly: LenientStr = None
    nontrd: LenientStr = None
    dname: LenientStr = None
    uc: Price = None
    lc: Price = None
    ord_msg: LenientStr = None
    exptime: LenientStr = None


class OptionContract(TradeSmartModel):
    """One contract of ``GetOptionChain``."""

    exchange: LenientStr = Field(default=None, alias="exch")
    tradingsymbol: LenientStr = Field(default=None, alias="tsym")
    token: LenientStr = None
    option_type: LenientStr = Field(default=None, alias="optt")
    strike_price: Price = Field(default=None, alias="strprc")
    price_precision: Quantity = Field(default=None, alias="pp")
    tick_size: Price = Field(default=None, alias="ti")
    lot_size: Quantity = Field(default=None, alias="ls")
    instname: LenientStr = None
    cname: LenientStr = None
    dname: LenientStr = None


class OptionChainRequest(RequestModel):
    """Validated ``GetOptionChain`` parameters."""

    uid: str
    exch: str
    tsym: str
    strprc: Money
    cnt: int = Field(ge=1)


class Candle(TradeSmartModel):
    """One intraday candle of ``TPSeries``."""

    stat: LenientStr = None
    time: ISTDateTime = None
    ssboe: EpochTime = None
    into: Price = None
    inth: Price = None
    intl: Price = None
    intc: Price = None
    intvwap: Price = None
    intv: Volume = None
    volume: Volume = Field(default=None, alias="v")
    intoi: Volume = None
    open_interest: Volume = Field(default=None, alias="oi")


class DailyCandle(TradeSmartModel):
    """One daily candle of ``EODChartData``."""

    stat: LenientStr = None
    time: ISTDateTime = None
    ssboe: EpochTime = None
    into: Price = None
    inth: Price = None
    intl: Price = None
    intc: Price = None
    intv: Volume = None


class TimePriceSeriesRequest(RequestModel):
    """Validated ``TPSeries`` parameters (``st``/``et``/``intrv`` optional)."""

    uid: str
    exch: str
    token: str
    st: str | None = None
    et: str | None = None
    intrv: str | None = None

    @field_validator("intrv")
    @classmethod
    def _check_interval(cls, value: str | None) -> str | None:
        if value is not None and value not in TPS_INTERVALS:
            raise ValueError(f"interval must be one of {', '.join(TPS_INTERVALS)} (minutes)")
        return value
