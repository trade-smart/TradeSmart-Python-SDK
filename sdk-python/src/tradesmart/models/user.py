"""Response models for the user endpoints."""

from pydantic import Field

from tradesmart.models.base import ISTDateTime, LenientStr, ProductField, TradeSmartModel


class ProductInfo(TradeSmartModel):
    """One enabled product with the exchanges it is available on."""

    product: ProductField = Field(default=None, alias="prd")
    s_prdt_ali: LenientStr = None
    exch: list[str] | None = None


class UserDetails(TradeSmartModel):
    """Result of ``UserDetails``."""

    user_id: LenientStr = Field(default=None, alias="uid")
    account_id: LenientStr = Field(default=None, alias="actid")
    uname: LenientStr = None
    email: LenientStr = None
    m_num: LenientStr = None
    brkname: LenientStr = None
    brnchid: LenientStr = None
    uprev: LenientStr = None
    access_type: list[str] | None = None
    exarr: list[str] | None = None
    orarr: list[str] | None = None
    prarr: list[ProductInfo] | None = None
    request_time: ISTDateTime = None
    stat: LenientStr = None
