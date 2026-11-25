"""Reply builders shared by the offline broker's REST adapter and stream server.

Pure functions returning wire dicts with the documented field names. Every builder's key set
includes the keys of the matching file in ``specifications/test-fixtures/`` (checked by the
test suite), so the offline broker cannot drift from the shapes the SDK is tested against.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, get_args

from pydantic import BaseModel

from tradesmart.models.base import IST
from tradesmart.models.market import Candle, DailyCandle, Quote
from tradesmart.models.stream import DepthFeed

if TYPE_CHECKING:
    from tradesmart.testing._state import OfflineOrder

PRODUCT_ALIAS = {"C": "CNC", "I": "MIS", "M": "NRML", "H": "CO", "B": "BO", "F": "MTF"}
SEGMENT = {
    "NSE": "EQT",
    "BSE": "EQT",
    "NFO": "DER",
    "BFO": "DER",
    "CDS": "FX",
    "BCD": "FX",
    "MCX": "COM",
}
# Friendly ``set_quote`` / ``tick`` names and their wire keys.
QUOTE_FIELDS = {
    "last_price": "lp",
    "open": "o",
    "high": "h",
    "low": "l",
    "close": "c",
    "volume": "v",
    "oi": "oi",
    "change_percent": "pc",
    "bid": "bp1",
    "ask": "sp1",
    "bid_qty": "bq1",
    "ask_qty": "sq1",
}
# Friendly ``set_candles`` names and their wire keys.
CANDLE_FIELDS = {
    "open": "into",
    "high": "inth",
    "low": "intl",
    "close": "intc",
    "volume": "intv",
    "oi": "intoi",
    "vwap": "intvwap",
}


def _wire_keys(*models: type[BaseModel]) -> dict[str, bool]:
    """Wire key -> whether its value is a number, from the SDK models that parse the reply."""
    keys: dict[str, bool] = {}
    for model in models:
        for name, info in model.model_fields.items():
            numeric = bool({Decimal, int} & {info.annotation, *get_args(info.annotation)})
            key = info.alias or name
            keys[key] = keys.get(key, False) or numeric
    return keys


_EPOCH_KEYS = {"lut": True, "ft": True, "ssboe": True}  # epoch seconds: numbers too
# Keys ``set_quote`` / ``tick`` accept besides the friendly names (never the message identity).
QUOTE_WIRE = {
    key: numeric
    for key, numeric in {**_wire_keys(Quote, DepthFeed), **_EPOCH_KEYS}.items()
    if key not in ("t", "e", "tk", "ssboe")
}
QUOTE_WIRE.update(dict.fromkeys(QUOTE_FIELDS.values(), True))
# Keys ``set_candles`` accepts besides the friendly names (``time`` is checked on its own).
CANDLE_WIRE = {**_wire_keys(Candle, DailyCandle), "ssboe": True}
CANDLE_WIRE.pop("time")
_DEPTH = [f"{side}{level}" for side in ("bp", "sp") for level in range(1, 6)]
_DEPTH_QTY = [f"{side}{level}" for side in ("bq", "sq", "bo", "so") for level in range(1, 6)]


# --- value formatting ----------------------------------------------------------------------


def wire_value(value: Any) -> Any:
    """A scalar as wire text (``Decimal(str(x))`` for floats); lists and dicts unchanged."""
    if isinstance(value, (str, list, dict)) or value is None:
        return value
    if isinstance(value, (float, Decimal)):
        return str(Decimal(str(value)))
    return str(value)


def price_text(value: Decimal | None, empty: str = "0.00") -> str:
    """A price with at least two decimals (``101.5`` -> ``101.50``); ``empty`` for ``None``."""
    if value is None:
        return empty
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        return format(value, "f")
    return format(value.quantize(Decimal("0.01")), "f")


def average_text(value: Decimal | None, empty: str = "0.00") -> str:
    """An average price rounded to two decimals, as the API shows averages."""
    if value is None:
        return empty
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def request_time(when: datetime) -> str:
    """``HH:MM:SS dd-mm-YYYY`` in IST (``request_time``, ``norentm``)."""
    return when.astimezone(IST).strftime("%H:%M:%S %d-%m-%Y")


def _fill_time(when: datetime) -> str:
    """``dd-mm-YYYY HH:MM:SS`` in IST (``fltm``, ``exch_tm``)."""
    return when.astimezone(IST).strftime("%d-%m-%Y %H:%M:%S")


def _epoch(when: datetime) -> str:
    return str(int(when.timestamp()))


def _base(instrument: Mapping[str, Any] | None) -> dict[str, Any]:
    """Instrument fields every record repeats, with defaults for an instrument never added."""
    inst = instrument or {}
    return {
        "token": inst.get("token", ""),
        "pp": inst.get("pp", "2"),
        "ls": inst.get("ls", "1"),
        "ti": inst.get("ti", "0.05"),
        "instname": inst.get("instname", ""),
        "symname": inst.get("symname", ""),
        "cname": inst.get("cname", ""),
        "dname": inst.get("dname", ""),
        "mult": "1",
        "prcftr": "1.000000",
    }


# --- instruments ---------------------------------------------------------------------------


def instrument_row(
    exchange: str,
    token: str,
    symbol: str,
    *,
    lot_size: int,
    tick_size: Any,
    instrument: str,
    expiry: date | None,
    strike: Any,
    option_type: str | None,
    underlying: str | None,
    extra: Mapping[str, Any],
) -> dict[str, Any]:
    """An instrument as a ``SearchScrip`` match (also used for chains and the script master)."""
    tick = Decimal(str(tick_size))
    exponent = tick.as_tuple().exponent
    decimals = -exponent if isinstance(exponent, int) else 2
    row: dict[str, Any] = {
        "exch": exchange,
        "token": str(token),
        "tsym": symbol,
        "cname": symbol,
        "dname": symbol,
        "instname": instrument,
        "symname": underlying or symbol.split("-")[0],
        "seg": SEGMENT.get(exchange, ""),
        "pp": str(max(2, decimals)),
        "ls": str(lot_size),
        "ti": str(tick),
    }
    if option_type is not None:
        row["optt"] = option_type
    elif instrument.upper().startswith("FUT"):
        row["optt"] = "XX"
    if strike is not None:
        row["strprc"] = price_text(Decimal(str(strike)))
    if expiry is not None:
        row["exd"] = expiry.strftime("%d-%b-%Y").upper()
    row.update({key: wire_value(value) for key, value in extra.items()})
    return row


def search_scrip(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """``SearchScrip`` reply."""
    return {"stat": "Ok", "values": [dict(row) for row in rows]}


def option_chain_row(instrument: Mapping[str, Any]) -> dict[str, Any]:
    """One ``GetOptionChain`` contract."""
    keys = ("exch", "token", "tsym", "optt", "pp", "ls", "ti", "strprc", "instname", "cname")
    row = {key: instrument.get(key, "") for key in keys}
    row["dname"] = instrument.get("dname", "")
    return row


def option_chain(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """``GetOptionChain`` reply."""
    return {"stat": "Ok", "values": [option_chain_row(row) for row in rows]}


def security_info(instrument: Mapping[str, Any], when: datetime) -> dict[str, Any]:
    """``GetSecurityInfo`` reply (values the test did not set are empty)."""
    reply: dict[str, Any] = {
        "request_time": request_time(when),
        "stat": "Ok",
        "isin": "",
        "issue_d": "",
        "listing_d": "",
        "uc": "",
        "lc": "",
        "prcftr_d": "(1 / 1 ) * (1 / 1)",
        "frzqty": "",
    }
    reply.update(_base(instrument))
    reply.update(instrument)
    return reply


# --- market data ---------------------------------------------------------------------------


def quote(
    exchange: str,
    token: str,
    instrument: Mapping[str, Any] | None,
    fields: Mapping[str, Any],
    when: datetime,
) -> dict[str, Any]:
    """``GetQuotes`` reply: the instrument, zero defaults, then the fields the test set."""
    reply: dict[str, Any] = {
        "request_time": request_time(when),
        "stat": "Ok",
        "exch": exchange,
        "tsym": (instrument or {}).get("tsym", ""),
        "seg": SEGMENT.get(exchange, ""),
        "isin": "",
        "lut": _epoch(when),
        "uc": "",
        "lc": "",
        "wk52_h": "",
        "wk52_l": "",
        "toi": "0",
        "issuecap": "",
        "cutof_all": "false",
        "prcftr_d": "(1 / 1 ) * (1 / 1)",
        "ltq": "0",
        "ltt": when.astimezone(IST).strftime("%H:%M:%S"),
        "ltd": when.astimezone(IST).strftime("%d-%m-%Y"),
        "tbq": "0",
        "tsq": "0",
    }
    reply.update(_base(instrument))
    reply.pop("prcftr")
    reply.pop("dname")
    reply["token"] = token
    for key in ("lp", "c", "h", "l", "ap", "o", *_DEPTH):
        reply[key] = "0.00"
    for key in ("v", "oi", *_DEPTH_QTY):
        reply[key] = "0"
    reply.update(fields)
    return reply


def candle_row(candle: Mapping[str, Any]) -> dict[str, Any]:
    """One ``TPSeries`` candle from friendly names (``time`` naive = IST) or wire keys."""
    row: dict[str, Any] = {"stat": "Ok", "intvwap": "0.00", "intv": "0", "intoi": "0"}
    for key, value in candle.items():
        if key != "time":
            row[CANDLE_FIELDS.get(key, key)] = wire_value(value)
    stamp = candle.get("time")
    if isinstance(stamp, datetime):
        when = stamp if stamp.tzinfo else stamp.replace(tzinfo=IST)
        row["time"] = _fill_time(when)
        row["ssboe"] = _epoch(when)
    else:
        row["time"] = str(stamp or "")
        row.setdefault("ssboe", "0")
    row.setdefault("v", row["intv"])
    row.setdefault("oi", row["intoi"])
    return row


def eod_row(candle: Mapping[str, Any]) -> str:
    """One ``EODChartData`` element: a JSON-encoded string, as the API sends it."""
    return json.dumps(eod_record(candle))


def eod_record(candle: Mapping[str, Any]) -> dict[str, Any]:
    """One ``EODChartData`` candle before encoding (``ssboe`` is 00:00 UTC of the day)."""
    row: dict[str, Any] = {}
    for key, value in candle.items():
        if key != "time":
            row[CANDLE_FIELDS.get(key, key)] = wire_value(value)
    stamp = candle.get("time")
    if isinstance(stamp, date):
        day = stamp.date() if isinstance(stamp, datetime) else stamp
        row["time"] = day.strftime("%d-%b-%Y").upper()
        row["ssboe"] = _epoch(datetime.combine(day, time(), tzinfo=UTC))
    else:
        row["time"] = str(stamp or "")
        row.setdefault("ssboe", "0")
    return row


# --- account -------------------------------------------------------------------------------


_LIMITS_ZERO = [
    "cash",
    "payin",
    "payout",
    "brkcollamt",
    "unclearedcash",
    "aux_daycash",
    "aux_brkcollamt",
    "aux_unclearedcash",
    "daycash",
    "turnoverlmt",
    "pendordvallmt",
    "remarks_amt",
    "blk_amt",
    "cac_daycash",
]


def limits(funds: Mapping[str, Any], when: datetime) -> dict[str, Any]:
    """``Limits`` reply: zero defaults, then the values the test set."""
    reply: dict[str, Any] = {"request_time": request_time(when), "stat": "Ok", "prfname": "OFFLINE"}
    reply.update({key: "0.00" for key in _LIMITS_ZERO})
    reply["remarks_text"] = ""
    reply.update(funds)
    return reply


_HOLDING_ZERO = [
    "holdqty",
    "dpqty",
    "npoadqty",
    "brkcolqty",
    "colqty",
    "benqty",
    "unplgdqty",
    "btstqty",
    "btstcolqty",
    "usedqty",
]


def holding_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One ``Holdings`` record: zero defaults, then the test's wire fields."""
    record: dict[str, Any] = {"stat": "Ok", "exch_tsym": []}
    record.update({key: "0" for key in _HOLDING_ZERO})
    record["upldprc"] = "0.00"
    record.update({key: wire_value(value) for key, value in row.items()})
    return record


def user_details(client_id: str, user: Mapping[str, Any], when: datetime) -> dict[str, Any]:
    """``UserDetails`` reply for the synthetic account, then the fields the test set."""
    exchanges = ["NSE", "NFO", "CDS", "MCX", "BSE", "BFO", "BCD"]
    reply: dict[str, Any] = {
        "request_time": request_time(when),
        "uname": "Offline User",
        "m_num": "",
        "email": "offline.user@example.invalid",
        "access_type": ["API"],
        "exarr": exchanges,
        "prarr": [
            {"prd": prd, "s_prdt_ali": PRODUCT_ALIAS[prd], "exch": exchanges}
            for prd in ("C", "I", "M", "H", "B")
        ],
        "orarr": ["MKT", "LMT", "SL-LMT", "SL-MKT"],
        "brkname": "OFFLINE",
        "uid": client_id,
        "brnchid": "HO",
        "actid": client_id,
        "uprev": "INVESTOR",
        "stat": "Ok",
    }
    reply.update(user)
    return reply


# --- orders --------------------------------------------------------------------------------


def place_ack(order_id: str, when: datetime) -> dict[str, Any]:
    """``PlaceOrder`` acknowledgement."""
    return {"request_time": request_time(when), "stat": "Ok", "norenordno": order_id}


def order_ack(order_id: str, when: datetime) -> dict[str, Any]:
    """``ModifyOrder`` / ``CancelOrder`` acknowledgement (live shape)."""
    return {"stat": "Ok", "request_time": request_time(when), "result": order_id}


def exit_ack(when: datetime) -> dict[str, Any]:
    """``ExitSNOOrder`` acknowledgement (the documented success example)."""
    return {"request_time": request_time(when), "stat": "Ok", "dmsg": "success"}


def not_ok(message: str, when: datetime) -> dict[str, Any]:
    """A ``Not_Ok`` reply."""
    return {"stat": "Not_Ok", "request_time": request_time(when), "emsg": message}


def exchange_order_id(order: OfflineOrder) -> str:
    """A synthetic exchange order number derived from the order id."""
    return f"11{order.id}"


def order_row(
    order: OfflineOrder,
    *,
    client_id: str,
    instrument: Mapping[str, Any] | None,
    when: datetime,
) -> dict[str, Any]:
    """One ``OrderBook`` record of the order as it is now; ``when`` is its last change."""
    payload = order.payload
    placed = order.history[-1]["ordenttm"] if order.history else _epoch(when)
    row: dict[str, Any] = {
        "stat": "Ok",
        "norenordno": order.id,
        "kidid": "1",
        "uid": client_id,
        "actid": client_id,
        "exch": order.exchange,
        "tsym": order.symbol,
        "rejby": "RMS" if order.status == "REJECTED" else "",
        "src_uid": "",
        "qty": str(order.quantity),
        "ipaddr": "127.0.0.1",
        "ordenttm": placed,
        "trantype": order.side,
        "prctyp": order.order_type,
        "ret": str(payload.get("ret", "DAY")),
        "rejreason": order.reject_reason or "",
        "ordersource": "API",
        "prc": price_text(order.price),
        "dscqty": str(payload.get("dscqty", "0")),
        "rprc": "0.00",
        "brnchid": "HO",
        "C": "C",
        "s_prdt_ali": PRODUCT_ALIAS.get(order.product, order.product),
        "prd": order.product,
        "status": order.status,
        "st_intrn": order.status,
        "norentm": request_time(when),
        "remarks": str(payload.get("remarks", "")),
        "rqty": "0",
        "ui_dev_code": f"{client_id}_API",
    }
    row.update(_base(instrument))
    for key in ("blprc", "bpprc", "trailprc"):  # bracket/cover order legs, only when sent
        if payload.get(key) not in (None, ""):
            row[key] = price_text(Decimal(str(payload[key])))
    if order.trigger_price is not None:
        row["trgprc"] = price_text(order.trigger_price)
    if order.status not in ("PENDING", "REJECTED"):
        row["exchordid"] = exchange_order_id(order)
    if order.filled_quantity:
        row["fillshares"] = str(order.filled_quantity)
        row["avgprc"] = average_text(order.average_price)
    if payload.get("add_ord_id"):  # an order a triggered GTT placed: the GTT (alert) id
        row["add_ord_id"] = str(payload["add_ord_id"])
    if order.status == "CANCELED":
        row["cancelqty"] = str(order.quantity - order.filled_quantity)
    return row


def history_row(
    order: OfflineOrder,
    *,
    client_id: str,
    instrument: Mapping[str, Any] | None,
    when: datetime,
    report: str,
) -> dict[str, Any]:
    """One ``SingleOrdHist`` record: the order as it is now, plus the report type."""
    row = order_row(order, client_id=client_id, instrument=instrument, when=when)
    row["rpt"] = report
    row["pan"] = ""
    row["introp_exch"] = SEGMENT.get(order.exchange, "")
    return row


_HISTORY_ONLY = ("rpt", "pan", "introp_exch")


def order_book_row(order: OfflineOrder) -> dict[str, Any]:
    """The ``OrderBook`` record of an order: its latest history row without history-only keys."""
    return {key: value for key, value in order.history[0].items() if key not in _HISTORY_ONLY}


def trade_row(
    order: OfflineOrder,
    *,
    client_id: str,
    instrument: Mapping[str, Any] | None,
    when: datetime,
    fill_id: str,
    quantity: int,
    price: Decimal,
) -> dict[str, Any]:
    """One ``TradeBook`` record for one fill of the order."""
    row: dict[str, Any] = {
        "stat": "Ok",
        "uid": client_id,
        "actid": client_id,
        "exch": order.exchange,
        "tsym": order.symbol,
        "norenordno": order.id,
        "qty": str(order.quantity),
        "prd": order.product,
        "s_prdt_ali": PRODUCT_ALIAS.get(order.product, order.product),
        "trantype": order.side,
        "prctyp": order.order_type,
        "fillshares": str(order.filled_quantity),
        "avgprc": average_text(order.average_price),
        "exchordid": exchange_order_id(order),
        "remarks": str(order.payload.get("remarks", "")),
        "ret": str(order.payload.get("ret", "DAY")),
        "cstFrm": "C",
        "fltm": _fill_time(when),
        "flid": fill_id,
        "flqty": str(quantity),
        "flprc": price_text(price),
        "ordersource": "API",
        "norentm": request_time(when),
        "exch_tm": _fill_time(when),
        "prc": price_text(order.price),
    }
    base = _base(instrument)
    row.update({key: base[key] for key in ("token", "pp", "ti", "ls", "mult")})
    return row


def order_update(
    order: OfflineOrder,
    *,
    client_id: str,
    when: datetime,
    report: str,
    fill: tuple[str, int, Decimal] | None = None,
) -> dict[str, Any]:
    """Stream ``om`` message for a change of the order; ``fill`` is (fill id, qty, price)."""
    fill_id, fill_qty, fill_price = fill if fill else ("", None, None)
    return {
        "t": "om",
        "norenordno": order.id,
        "uid": client_id,
        "actid": client_id,
        "exch": order.exchange,
        "tsym": order.symbol,
        "qty": str(order.quantity),
        "prc": price_text(order.price),
        "pcode": order.product,
        "status": order.status,
        "reporttype": report,
        "trantype": order.side,
        "prctyp": order.order_type,
        "ret": str(order.payload.get("ret", "DAY")),
        "fillshares": str(order.filled_quantity),
        "avgprc": average_text(order.average_price, empty=""),
        "fltm": _fill_time(when) if fill else "",
        "flid": fill_id,
        "flqty": "" if fill_qty is None else str(fill_qty),
        "flprc": price_text(fill_price, empty=""),
        "rejreason": order.reject_reason or "",
        "exchordid": "" if order.status in ("PENDING", "REJECTED") else exchange_order_id(order),
        "cancelqty": (
            str(order.quantity - order.filled_quantity) if order.status == "CANCELED" else "0"
        ),
        "remarks": str(order.payload.get("remarks", "")),
        "dscqty": str(order.payload.get("dscqty", "0")),
        "trgprc": price_text(order.trigger_price),
        "exch_tm": _fill_time(when),
        "amo": "No",
        "tm": _epoch(when),
        "ntm": str(int(when.timestamp() * 1_000_000) * 1000),
    }


# --- positions -----------------------------------------------------------------------------


POSITION_QTY = [
    "netqty",
    "daybuyqty",
    "daysellqty",
    "cfbuyqty",
    "cfsellqty",
    "openbuyqty",
    "opensellqty",
]
POSITION_AMOUNT = [
    "netavgprc",
    "dayavgprc",
    "daybuyavgprc",
    "daysellavgprc",
    "daybuyamt",
    "daysellamt",
    "cforgavgprc",
    "cfbuyavgprc",
    "cfsellavgprc",
    "cfbuyamt",
    "cfsellamt",
    "totbuyamt",
    "totsellamt",
    "totbuyavgprc",
    "totsellavgprc",
    "lp",
    "rpnl",
    "bep",
    "openbuyamt",
    "opensellamt",
    "openbuyavgprc",
    "opensellavgprc",
    "upldprc",
    "netupldprc",
]


def position_row(
    *,
    client_id: str,
    exchange: str,
    token: str,
    symbol: str,
    product: str,
    instrument: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """A flat ``PositionBook`` record (all quantities and amounts zero)."""
    base = _base(instrument)
    row: dict[str, Any] = {
        "stat": "Ok",
        "exch": exchange,
        "tsym": symbol,
        "token": token or base["token"],
        "uid": client_id,
        "actid": client_id,
        "prd": product,
        "s_prdt_ali": PRODUCT_ALIAS.get(product, product),
    }
    row.update({key: "0" for key in POSITION_QTY})
    row.update({key: "0.00" for key in POSITION_AMOUNT})
    for key in ("mult", "pp", "prcftr", "ti", "ls", "instname", "dname", "cname"):
        row[key] = base[key]
    return row


def position_update(row: Mapping[str, Any]) -> dict[str, Any]:
    """Stream ``pm`` message for a position record."""
    keys = [
        "exch",
        "token",
        "tsym",
        "uid",
        "actid",
        "prd",
        "daybuyqty",
        "daysellqty",
        "daybuyamt",
        "daysellamt",
        "cfbuyqty",
        "cfsellqty",
        "cfbuyamt",
        "cfsellamt",
        "openbuyqty",
        "opensellqty",
        "openbuyamt",
        "opensellamt",
        "rpnl",
        "netqty",
        "totbuyamt",
        "totsellamt",
        "totbuyavgprc",
        "totsellavgprc",
    ]
    message: dict[str, Any] = {"t": "pm"}
    message.update({key: row.get(key, "") for key in keys})
    message["buyavgprc"] = row.get("totbuyavgprc", "0.00")
    message["sellavgprc"] = row.get("totsellavgprc", "0.00")
    message["child_orders"] = [
        {"exch": row.get("exch", ""), "tsym": row.get("tsym", ""), "netqty": row.get("netqty")}
    ]
    return message


# --- stream --------------------------------------------------------------------------------


def connect_ack(uid: str, *, ok: bool) -> dict[str, Any]:
    """Stream connect acknowledgement (``s="OK"`` as observed live)."""
    return {"t": "ak", "uid": uid, "s": "OK" if ok else "Not_Ok"}


def heartbeat_ack(when: datetime) -> dict[str, Any]:
    """Stream heartbeat acknowledgement."""
    return {"t": "hk", "ft": _epoch(when)}


def _snapshot(
    kind: str,
    exchange: str,
    token: str,
    instrument: Mapping[str, Any] | None,
    fields: Mapping[str, Any],
    when: datetime,
) -> dict[str, Any]:
    base = _base(instrument)
    message: dict[str, Any] = {
        "t": kind,
        "e": exchange,
        "tk": token,
        "ts": (instrument or {}).get("tsym", ""),
        "pp": base["pp"],
        "ti": base["ti"],
        "ls": base["ls"],
        "pc": "0.00",
        "poi": "0",
        "toi": "0",
        "ft": _epoch(when),
    }
    for key in ("lp", "o", "h", "l", "c", "ap", "bp1", "sp1"):
        message[key] = "0.00"
    for key in ("v", "oi", "bq1", "sq1"):
        message[key] = "0"
    if kind == "dk":
        for key in _DEPTH:
            message.setdefault(key, "0.00")
        for key in _DEPTH_QTY:
            message.setdefault(key, "0")
        message.update({"cp": "0.00", "ltt": "", "ltq": "0", "tbq": "0", "tsq": "0"})
        message.update({"lc": "0.00", "uc": "0.00", "52h": "0.00", "52l": "0.00"})
    message.update({key: value for key, value in fields.items() if key not in ("tsym", "token")})
    message["t"] = kind
    return message


def touchline_ack(
    exchange: str,
    token: str,
    instrument: Mapping[str, Any] | None,
    fields: Mapping[str, Any],
    when: datetime,
) -> dict[str, Any]:
    """Stream ``tk`` (touchline subscription acknowledgement with the current quote)."""
    return _snapshot("tk", exchange, token, instrument, fields, when)


def depth_ack(
    exchange: str,
    token: str,
    instrument: Mapping[str, Any] | None,
    fields: Mapping[str, Any],
    when: datetime,
) -> dict[str, Any]:
    """Stream ``dk`` (depth subscription acknowledgement with the current quote)."""
    return _snapshot("dk", exchange, token, instrument, fields, when)


def touchline_feed(
    exchange: str, token: str, fields: Mapping[str, Any], when: datetime
) -> dict[str, Any]:
    """Stream ``tf``: only the changed fields besides ``t`` / ``e`` / ``tk`` / ``ft``."""
    return {"t": "tf", "e": exchange, "tk": token, **fields, "ft": _epoch(when)}


def depth_feed(
    exchange: str, token: str, fields: Mapping[str, Any], when: datetime
) -> dict[str, Any]:
    """Stream ``df``: only the changed fields besides ``t`` / ``e`` / ``tk`` / ``ft``."""
    return {"t": "df", "e": exchange, "tk": token, **fields, "ft": _epoch(when)}


# --- GTT ---------------------------------------------------------------------------------------

# GetEnabledGTTs: the alert families of the documented example.
GTT_ALERT_FAMILIES = ("ATP", "LTP")


def gtt_ack(word: str, alert_id: str, when: datetime) -> dict[str, Any]:
    """``PlaceGTTOrder`` / ``ModifyGTTOrder`` / ``CancelGTTOrder`` acknowledgement (documented)."""
    return {"request_time": request_time(when), "stat": word, "al_id": alert_id}


def gtt_row(
    gtt: Mapping[str, Any], *, client_id: str, instrument: Mapping[str, Any] | None
) -> dict[str, Any]:
    """One ``GetPendingGTTOrder`` record (the documented example's keys; ``qty`` a number)."""
    if gtt.get("oco"):
        return oco_row(gtt, client_id=client_id, instrument=instrument)
    base = _base(instrument)
    product = str(gtt["prd"])
    row: dict[str, Any] = {
        "stat": "Ok",
        "ai_t": gtt["ai_t"],
        "al_id": gtt["al_id"],
        "tsym": gtt["tsym"],
        "exch": gtt["exch"],
        "token": base["token"],
        "remarks": gtt["remarks"],
        "validity": gtt["validity"],
        "pp": base["pp"],
        "ls": base["ls"],
        "actid": client_id,
        "trantype": gtt["trantype"],
        "prctyp": gtt["prctyp"],
        "qty": int(Decimal(str(gtt["qty"]))),
        "prc": price_text(Decimal(str(gtt["prc"]))),
        "prd": product,
        "s_prdt_ali": PRODUCT_ALIAS.get(product, product),
        "ret": gtt["ret"],
        "ordersource": "API",
        "d": price_text(Decimal(str(gtt["d"]))),
    }
    if str(gtt.get("trgprc") or "").strip():
        row["trgprc"] = price_text(Decimal(str(gtt["trgprc"])))
    return row


def triggered_gtt_row(
    gtt: Mapping[str, Any], *, client_id: str, instrument: Mapping[str, Any] | None
) -> dict[str, Any]:
    """One ``GetTrgOi`` record: the pending record plus the trigger time and the flags."""
    if gtt.get("oco"):
        return oco_row(gtt, client_id=client_id, instrument=instrument)
    row = gtt_row(gtt, client_id=client_id, instrument=instrument)
    row["norentm"] = request_time(gtt["norentm"])
    row.update(sms_flag="0", email_flag="0", push_flag="0")
    return row


def enabled_gtts(when: datetime) -> dict[str, Any]:
    """``GetEnabledGTTs`` reply (documented; ``request_time`` as ``DDMMYYYYHHMMSS``)."""
    return {
        "stat": "Ok",
        "request_time": when.astimezone(IST).strftime("%d%m%Y%H%M%S"),
        "ai_ts": [{"ai_t": family} for family in GTT_ALERT_FAMILIES],
    }


def oco_row(
    oco: Mapping[str, Any], *, client_id: str, instrument: Mapping[str, Any] | None
) -> dict[str, Any]:
    """One OCO record of ``GetPendingGTTOrder`` / ``GetTrgOi`` (the shape of the fixture rows:
    the first leg's order fields repeated at the top, ``d`` the ``x`` trigger, ``qty`` a number,
    the account id and the empty ``remarks`` only inside / besides the legs)."""
    base = _base(instrument)
    legs = [oco["place_order_params"], oco["place_order_params_leg2"]]

    def leg_row(leg: Mapping[str, Any], *, alias: bool) -> dict[str, Any]:
        product = str(leg["prd"])
        row: dict[str, Any] = {
            "actid": client_id,
            "trantype": leg["trantype"],
            "prctyp": leg["prctyp"],
            "qty": int(Decimal(str(leg["qty"]))),
            "prc": price_text(Decimal(str(leg["prc"]))),
            "C": "0",
            "prd": product,
            "ordersource": "API",
        }
        if str(leg.get("trgprc") or "").strip():
            row["trgprc"] = price_text(Decimal(str(leg["trgprc"])))
        if alias:
            row["s_prdt_ali"] = PRODUCT_ALIAS.get(product, product)
        return row

    first, second = leg_row(legs[0], alias=True), leg_row(legs[1], alias=False)
    triggers = {item["var_name"]: item["d"] for item in oco["oivariable"]}
    placed = request_time(oco["norentm"])
    row: dict[str, Any] = {
        "stat": oco.get("stat", "Ok"),
        "ai_t": oco["ai_t"],
        "al_id": oco["al_id"],
        "tsym": oco["tsym"],
        "exch": oco["exch"],
        "token": base["token"],
        "remarks": "",
        "validity": oco["validity"],
        "pp": base["pp"],
        "ls": base["ls"],
        "ti": base["ti"],
        "norentm": placed,
        "upd_tm": placed,
        "d": price_text(triggers["x"]),
        "trantype": first["trantype"],
        "prctyp": first["prctyp"],
        "qty": first["qty"],
        "prc": first["prc"],
        "prd": first["prd"],
        "ordersource": "API",
        "C": "0",
        "place_order_params": first,
        "place_order_params_leg2": second,
        "oivariable": [{"var_name": name, "d": price_text(triggers[name])} for name in ("x", "y")],
    }
    return row
