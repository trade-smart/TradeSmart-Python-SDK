"""Market data endpoints: search, quotes, security info, option chain, TPSeries, EODChartData."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, TypeVar

from tradesmart._transport.codec import redact_body
from tradesmart.endpoints.base import (
    Call,
    EndpointSpec,
    invalid_field,
    parse_list,
    parse_object,
    require_str,
)
from tradesmart.enums import Exchange
from tradesmart.errors import RequestValidationError, ResponseParseError
from tradesmart.models.base import (
    IST,
    PriceInput,
    TradeSmartModel,
    validate_request,
)
from tradesmart.models.market import (
    TPS_INTERVALS,
    Candle,
    DailyCandle,
    OptionChainRequest,
    OptionContract,
    Quote,
    ScripMatch,
    SecurityInfo,
    TimePriceSeriesRequest,
)

M = TypeVar("M", bound=TradeSmartModel)

TimeInput = datetime | date | int
"""A timezone-aware ``datetime``, a ``date`` (00:00 IST that day) or epoch-seconds ``int``."""

__all__ = ["TPS_INTERVALS", "TimeInput", "eod_symbol", "to_epoch"]


def to_epoch(value: TimeInput) -> str:
    """Convert an aware datetime, a date (00:00 IST) or epoch seconds to a wire epoch string.

    Anything else (text, a float, a bool, ``None``) raises ``RequestValidationError``
    (field ``time``): nothing is guessed, truncated or sent.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise invalid_field("time", "datetime must be timezone-aware")
        return str(int(value.timestamp()))
    if isinstance(value, date):
        return str(int(datetime(value.year, value.month, value.day, tzinfo=IST).timestamp()))
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    raise invalid_field(
        "time", "must be a timezone-aware datetime, a date or an integer epoch in seconds"
    )


def exchange_code(exchange: Exchange | str) -> str:
    """The upper-case wire code of ``exchange`` (any case accepted).

    Raises ``RequestValidationError`` (field ``exch``) for an unknown exchange.
    """
    try:
        return Exchange(exchange).value
    except ValueError:
        error = RequestValidationError(
            [{"type": "enum", "loc": ("exch",), "msg": "unknown exchange"}]
        )
        error.args = ("Invalid request parameters: exch",)
        raise error from None


def eod_symbol(exchange: Exchange | str, tradingsymbol: str) -> str:
    """Build the ``EXCH:TSYM`` symbol used by ``EODChartData`` (e.g. ``NSE:SBIN-EQ``)."""
    return f"{exchange_code(exchange)}:{require_str(tradingsymbol, 'tsym')}"


def _parse_values(model: type[M], endpoint: str) -> Callable[[Any], list[M]]:
    """Parser for ``{"stat": "Ok", "values": [...]}`` bodies (``[]`` is the empty signal)."""

    def _parse(body: Any) -> list[M]:
        if isinstance(body, list) and not body:
            return []
        values = body.get("values") if isinstance(body, dict) else None
        if not isinstance(values, list):
            raise ResponseParseError(
                f"{endpoint}: response has no 'values' list",
                endpoint=endpoint,
                http_status=None,
                body=redact_body(body),
            )
        return [model.from_wire(item, endpoint=endpoint) for item in values]

    return _parse


SEARCH_SCRIP: EndpointSpec[list[ScripMatch]] = EndpointSpec(
    name="SearchScrip",
    parse=_parse_values(ScripMatch, "SearchScrip"),
    expect="object",
    allow_empty=True,
)

GET_QUOTES: EndpointSpec[Quote] = EndpointSpec(
    name="GetQuotes",
    parse=parse_object(Quote, "GetQuotes"),
    rate_group="quotes",
)

GET_SECURITY_INFO: EndpointSpec[SecurityInfo] = EndpointSpec(
    name="GetSecurityInfo",
    parse=parse_object(SecurityInfo, "GetSecurityInfo"),
)

GET_OPTION_CHAIN: EndpointSpec[list[OptionContract]] = EndpointSpec(
    name="GetOptionChain",
    parse=_parse_values(OptionContract, "GetOptionChain"),
    expect="object",
    allow_empty=True,
)


def _parse_eod(body: Any) -> list[DailyCandle]:
    """Parser for ``EODChartData``: a list of JSON-encoded strings (dicts also accepted)."""
    endpoint = "EODChartData"

    def _fail(message: str, item: Any) -> ResponseParseError:
        return ResponseParseError(
            f"{endpoint}: {message}", endpoint=endpoint, http_status=None, body=redact_body(item)
        )

    if not isinstance(body, list):
        raise _fail("expected a JSON list", body)
    rows: list[DailyCandle] = []
    for item in body:
        decoded = item
        if isinstance(item, str):
            try:
                decoded = json.loads(item)
            except ValueError:
                raise _fail("element is not valid JSON", item) from None
        if not isinstance(decoded, dict):
            raise _fail("element is not a JSON object", item)
        rows.append(DailyCandle.from_wire(decoded, endpoint=endpoint))
    return rows


TP_SERIES: EndpointSpec[list[Candle]] = EndpointSpec(
    name="TPSeries",
    parse=parse_list(Candle, "TPSeries"),
    expect="list",
    allow_empty=True,
    rate_group="tpseries",
)

EOD_CHART_DATA: EndpointSpec[list[DailyCandle]] = EndpointSpec(
    name="EODChartData",
    parse=_parse_eod,
    expect="list",
    allow_empty=True,
)


def search_call(uid: str, text: str) -> Call[list[ScripMatch]]:
    """Build the ``SearchScrip`` call."""
    return Call(SEARCH_SCRIP, {"uid": uid, "stext": require_str(text, "stext")})


def quote_call(uid: str, exchange: Exchange | str, token: str) -> Call[Quote]:
    """Build the ``GetQuotes`` call (``token`` is a ``str``, e.g. ``"2885"``)."""
    payload = {"uid": uid, "exch": exchange_code(exchange), "token": require_str(token, "token")}
    return Call(GET_QUOTES, payload)


def security_info_call(uid: str, exchange: Exchange | str, token: str) -> Call[SecurityInfo]:
    """Build the ``GetSecurityInfo`` call (``token`` is a ``str``)."""
    payload = {"uid": uid, "exch": exchange_code(exchange), "token": require_str(token, "token")}
    return Call(GET_SECURITY_INFO, payload)


def option_chain_call(
    uid: str,
    exchange: Exchange | str,
    tradingsymbol: str,
    strike_price: PriceInput,
    count: int,
) -> Call[list[OptionContract]]:
    """Build the ``GetOptionChain`` call (``count`` >= 1 strikes per side)."""
    request = validate_request(
        OptionChainRequest,
        uid=uid,
        exch=exchange_code(exchange),
        tsym=tradingsymbol,
        strprc=strike_price,
        cnt=count,
    )
    return Call(GET_OPTION_CHAIN, request.to_wire())


def time_price_series_call(
    uid: str,
    exchange: Exchange | str,
    token: str,
    from_date: TimeInput | None = None,
    to_date: TimeInput | None = None,
    interval: int | str | None = None,
) -> Call[list[Candle]]:
    """Build the ``TPSeries`` call; ``interval`` is in minutes (see ``TPS_INTERVALS``)."""
    request = validate_request(
        TimePriceSeriesRequest,
        uid=uid,
        exch=exchange_code(exchange),
        token=token,
        st=None if from_date is None else to_epoch(from_date),
        et=None if to_date is None else to_epoch(to_date),
        intrv=None if interval is None else str(interval),
    )
    return Call(TP_SERIES, request.to_wire())


def eod_chart_call(
    symbol: str, from_date: TimeInput, to_date: TimeInput
) -> Call[list[DailyCandle]]:
    """Build the ``EODChartData`` call (documented without ``uid``)."""
    return Call(
        EOD_CHART_DATA, {"sym": symbol, "from": to_epoch(from_date), "to": to_epoch(to_date)}
    )
