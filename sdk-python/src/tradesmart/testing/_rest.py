"""The offline broker's REST side: in-memory ``httpx`` transports (sync and async).

Every call stays in the process: the transports answer ``POST {OFFLINE_BASE_URL}/{Endpoint}``
from a ``BrokerState`` and ``GET {OFFLINE_SCRIPT_MASTER_URL}/{EXCH}_symbols.txt.zip`` with a
script master built from the added instruments. The URLs use the reserved ``.invalid`` host,
so a client that is not wired to an offline transport can never reach a real server.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import secrets
import zipfile
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, TypeVar
from urllib.parse import unquote

import httpx

from tradesmart.auth import next_expiry
from tradesmart.config import ClientConfig
from tradesmart.enums import Exchange
from tradesmart.models.base import IST
from tradesmart.testing import _replies
from tradesmart.testing._state import _DEFAULT_PLACE as PLACE_MODES
from tradesmart.testing._state import BrokerState
from tradesmart.testing.drills import Drill

__all__ = [
    "OFFLINE_AUTHORIZE_URL",
    "OFFLINE_BASE_URL",
    "OFFLINE_SCRIPT_MASTER_URL",
    "AsyncOfflineTransport",
    "OfflineTransport",
    "offline_config",
]

OFFLINE_HOST = "offline.tradesmart.invalid"
OFFLINE_BASE_URL = "https://offline.tradesmart.invalid/NorenWClientAPIv2"
OFFLINE_SCRIPT_MASTER_URL = "https://offline.tradesmart.invalid"
OFFLINE_AUTHORIZE_URL = "https://offline.tradesmart.invalid/authorize"

NO_DATA = 'Error Occurred : 5 "no data"'
SESSION_EXPIRED = "Session Expired :  Invalid Session Key"  # documented text (two spaces)
# The smallest backoff the SDK accepts: retries and stream reconnects keep their production
# count but never sleep (a ``Retry-After`` is clamped to the cap); only the order rate limit waits.
_NO_WAIT = 0.001
# Streams send a heartbeat this often and detect a silent server after twice as long (1 s).
_HEARTBEAT = 0.5
_BASE_PATH = httpx.URL(OFFLINE_BASE_URL).path + "/"
_ZIP_SUFFIX = "_symbols.txt.zip"
# Script-master columns (the layout the SDK's instrument and option helpers read) and the
# instrument-row key each one comes from. The BFO file names ``StrikePrice`` ``Strike``.
SCRIPT_MASTER_COLUMNS = {
    "Exchange": "exch",
    "Token": "token",
    "LotSize": "ls",
    "Symbol": "symname",
    "TradingSymbol": "tsym",
    "Expiry": "exd",
    "Instrument": "instname",
    "OptionType": "optt",
    "StrikePrice": "strprc",
    "TickSize": "ti",
}

Reply = Callable[[dict[str, Any], datetime], httpx.Response]


def offline_config(ws_url: str, **overrides: Any) -> ClientConfig:
    """A ``ClientConfig`` whose REST, login and script-master URLs point at the offline broker.

    ``ws_url`` is the stream server's URL; ``overrides`` set any other field. Reads are
    retried as in production (``max_retries``), but the backoff is 1 ms, so a retried drill
    costs no real time; pass ``max_retries=0`` to see a read drill's error. Streams reconnect
    after 1 ms (``reconnect_max_attempts`` as in production) and send a heartbeat every 0.5 s,
    so a silent server is detected in about 1 s.
    """
    settings: dict[str, Any] = {
        "base_url": OFFLINE_BASE_URL,
        "authorize_url": OFFLINE_AUTHORIZE_URL,
        "script_master_base_url": OFFLINE_SCRIPT_MASTER_URL,
        "ws_url": ws_url,
        "backoff_base": _NO_WAIT,
        "backoff_cap": _NO_WAIT,
        "reconnect_backoff_base": _NO_WAIT,
        "reconnect_backoff_cap": _NO_WAIT,
        "heartbeat_interval": _HEARTBEAT,
    }
    settings.update(overrides)
    return ClientConfig(**settings)


def _json(body: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body)


def _not_ok(message: str, now: datetime, status: int = 200) -> httpx.Response:
    return _json(_replies.not_ok(message, now), status)


def _decode(content: bytes) -> dict[str, Any] | None:
    """The ``jData`` JSON object of a form body, or ``None`` if there is none."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    for part in text.split("&"):
        if part.startswith("jData="):
            try:
                data = json.loads(unquote(part[len("jData=") :]))
            except ValueError:
                return None
            return data if isinstance(data, dict) else None
    return None


def _bound(value: Any) -> int | None:
    """A range bound in epoch seconds (``None`` when absent); ``ValueError`` if not a number."""
    if value is None or not str(value).strip():
        return None
    return int(str(value).strip())


def _candle_epoch(row: Mapping[str, Any]) -> int | None:
    """A candle's ``ssboe`` in epoch seconds, or ``None`` when it has none (blank or ``0``).

    ``set_candles`` has already checked that a given ``ssboe`` is a number.
    """
    text = str(row.get("ssboe") or "").strip()
    return int(Decimal(text or "0")) or None


def _candle_day(row: Mapping[str, Any]) -> date | None:
    """The day of a daily candle (its ``ssboe`` is 00:00 UTC of that day), or ``None``."""
    epoch = _candle_epoch(row)
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC).date()


_Row = TypeVar("_Row", bound=Mapping[str, Any])


def _newest_first(
    rows: list[_Row], first: Any, last: Any, moment: Callable[[_Row], Any]
) -> list[_Row]:
    """The rows whose ``moment`` is within ``first``..``last`` (both included; ``None`` is
    open), newest first. Rows without a time are always kept, after the others, in order."""
    timed = [
        (at, row)
        for row in rows
        if (at := moment(row)) is not None
        and (first is None or at >= first)
        and (last is None or at <= last)
    ]
    timed.sort(key=lambda pair: pair[0], reverse=True)  # stable: equal times keep their order
    return [row for _, row in timed] + [row for row in rows if moment(row) is None]


def _gateway_page(status: int, title: str | None = None) -> httpx.Response:
    """An HTML error page, as a gateway or proxy sends it (not JSON)."""
    if title is None:
        reason = httpx.codes.get_reason_phrase(status)
        title = f"{status} {reason}".strip()
    html = f"<html><body><h1>{title}</h1></body></html>"
    return httpx.Response(status, text=html, headers={"Content-Type": "text/html"})


def _drill_reply(drill: Drill, now: datetime) -> httpx.Response:
    """The reply of an armed drill (``connect_error`` / ``timeout_after_send`` raise instead)."""
    if drill.kind == "reject":
        return _not_ok(drill.message or "Rejected by OfflineBroker", now)
    if drill.kind == "rate_limit":
        response = _not_ok("Too many requests", now, 429)
        if drill.retry_after is not None:
            response.headers["Retry-After"] = format(drill.retry_after, "g")
        return response
    if drill.kind == "server_error":
        status = drill.status or 503
        if drill.json:
            return _not_ok("Internal Server Error", now, status)
        return _gateway_page(status)
    if drill.kind == "bad_body":
        return _gateway_page(200, "502 Bad Gateway")
    if drill.kind == "session_expired":
        return _not_ok(SESSION_EXPIRED, now, 401)
    raise ValueError(f"unknown drill kind {drill.kind!r}")


class _Handler:
    """Maps one HTTP request to the broker state (shared by both transports)."""

    def __init__(self, state: BrokerState) -> None:
        self._state = state
        self._routes: dict[str, Reply] = {
            "GenAcsTok": self._gen_acs_tok,
            "UserDetails": self._user_details,
            "Limits": self._limits,
            "Holdings": self._holdings,
            "PositionBook": self._positions,
            "OrderBook": self._order_book,
            "SingleOrdHist": self._order_history,
            "TradeBook": self._trade_book,
            "PlaceOrder": self._place,
            "ModifyOrder": self._modify,
            "CancelOrder": self._cancel,
            "ExitSNOOrder": self._exit,
            "PlaceGTTOrder": self._place_gtt,
            "ModifyGTTOrder": self._modify_gtt,
            "CancelGTTOrder": self._cancel_gtt,
            "PlaceOCOOrder": self._place_oco,
            "ModifyOCOOrder": self._modify_oco,
            "CancelOCOOrder": self._cancel_oco,
            "GetPendingGTTOrder": self._pending_gtts,
            "GetTrgOi": self._triggered_gtts,
            "GetEnabledGTTs": self._enabled_gtts,
            "GetQuotes": self._quote,
            "SearchScrip": self._search,
            "GetSecurityInfo": self._security_info,
            "GetOptionChain": self._option_chain,
            "TPSeries": self._tp_series,
            "EODChartData": self._eod,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == OFFLINE_HOST:
            if request.method == "POST" and url.path.startswith(_BASE_PATH):
                return self._rest(request, url.path[len(_BASE_PATH) :])
            if request.method == "GET" and url.path.endswith(_ZIP_SUFFIX):
                return self._script_master(url.path[1 : -len(_ZIP_SUFFIX)])
        return httpx.Response(404, text="Not Found")

    # --- REST -------------------------------------------------------------------------------

    def _rest(self, request: httpx.Request, endpoint: str) -> httpx.Response:
        state = self._state
        now = datetime.now(IST)
        payload = _decode(request.content)
        if payload is None:  # the SDK never sends this; an armed drill stays armed
            state.record(endpoint, {})
            return _not_ok("Invalid Input : jData is not a JSON object", now)
        drill = state.take_drill(endpoint)
        if drill is not None and drill.kind == "connect_error":
            raise httpx.ConnectError("OfflineBroker drill: connect_error", request=request)
        state.record(endpoint, payload)
        if drill is None:
            return self._dispatch(endpoint, payload, now)
        if drill.kind == "timeout_after_send":
            self._dispatch(endpoint, payload, now)  # the call takes effect; the reply is lost
            raise httpx.ReadTimeout("OfflineBroker drill: timeout_after_send", request=request)
        return _drill_reply(drill, now)

    def _dispatch(self, endpoint: str, payload: dict[str, Any], now: datetime) -> httpx.Response:
        with self._state.lock:
            reply = copy.deepcopy(self._state.replies.get(endpoint))
        if reply is not None:
            return _json(reply)
        route = self._routes.get(endpoint)
        if route is None:
            return _not_ok(
                f"OfflineBroker does not model {endpoint}; set a reply with broker.set_reply(...)",
                now,
            )
        return route(payload, now)

    # --- login and account -------------------------------------------------------------------

    def _gen_acs_tok(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        uid = self._state.client_id
        with self._state.lock:
            uname = str(self._state.user.get("uname", "Offline User"))
        token = f"offline-token-{secrets.token_hex(16)}"
        return _json(
            {
                "USERID": uid,
                "access_token": token,
                "expires_in": str(int(next_expiry(now).timestamp())),
                "refresh_token": f"offline-refresh-{secrets.token_hex(16)}",
                "scope": "0",
                "actid": uid,
                "uname": uname,
                "prarr": [],
                "exch_prd": [],
                "stat": "Ok",
                "susertoken": token,
                "uid": uid,
                "brnchid": "HO",
                "totp_set": "1",
                "orarr": [],
                "exarr": [exchange.value for exchange in Exchange],
                "values": [],
                "mws": {},
                "brkname": "OFFLINE",
                "lastaccesstime": str(int(now.timestamp())),
                "lastpwdtime": "0",
            }
        )

    def _user_details(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        with self._state.lock:
            user = dict(self._state.user)
        return _json(_replies.user_details(self._state.client_id, user, now))

    def _limits(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        with self._state.lock:
            funds = dict(self._state.funds)
        return _json(_replies.limits(funds, now))

    def _holdings(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        with self._state.lock:
            rows = copy.deepcopy(self._state.holdings)
        return _json([_replies.holding_row(row) for row in rows])  # empty: [] (observed live)

    def _rows(self, rows: list[dict[str, Any]], now: datetime) -> httpx.Response:
        """A book reply: the rows, or the documented "no data" ``Not_Ok`` when empty."""
        return _json(rows) if rows else _not_ok(NO_DATA, now)

    def _positions(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        with self._state.lock:
            rows = [dict(row) for row in self._state.positions]
        return self._rows(rows, now)

    # --- orders ------------------------------------------------------------------------------

    def _order_book(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        with self._state.lock:  # newest first
            rows = [_replies.order_book_row(order) for order in reversed(self._state.orders)]
        return self._rows(rows, now)

    def _order_history(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        order_id = str(payload.get("norenordno", ""))
        with self._state.lock:
            rows = [
                dict(row)
                for order in self._state.orders
                if order.id == order_id
                for row in order.history
            ]
        return self._rows(rows, now)

    def _trade_book(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        with self._state.lock:
            rows = [dict(row) for row in self._state.trades]
        return self._rows(rows, now)

    def _place(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        mode = self._state.default_place
        if mode not in PLACE_MODES:  # a test misconfiguration, not a broker rejection
            raise ValueError(f"default_place must be 'open', 'pending' or 'reject', not {mode!r}")
        try:
            order = self._state.place(payload)
        except ValueError as exc:
            return _not_ok(f"Invalid Input : {exc}", now)
        return _json(_replies.place_ack(order.id, now))

    def _modify(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        try:
            accepted, result = self._state.modify(payload)
        except ValueError as exc:
            return _not_ok(f"Invalid Input : {exc}", now)
        return _json(_replies.order_ack(result, now)) if accepted else _not_ok(result, now)

    def _cancel(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        accepted, result = self._state.cancel_request(str(payload.get("norenordno", "")))
        return _json(_replies.order_ack(result, now)) if accepted else _not_ok(result, now)

    def _exit(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        accepted, result = self._state.exit_request(str(payload.get("norenordno", "")))
        return _json(_replies.exit_ack(now)) if accepted else _not_ok(result, now)

    # --- GTT ---------------------------------------------------------------------------------

    def _place_gtt(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        try:
            alert_id = self._state.place_gtt(payload)
        except ValueError as exc:
            return _not_ok(f"Invalid Input : {exc}", now)
        return _json(_replies.gtt_ack("Oi created", alert_id, now))

    def _modify_gtt(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        try:
            accepted, result = self._state.modify_gtt(payload)
        except ValueError as exc:
            return _not_ok(f"Invalid Input : {exc}", now)
        if not accepted:
            return _not_ok(result, now)
        return _json(_replies.gtt_ack("Oi replaced", result, now))

    def _cancel_gtt(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        accepted, result = self._state.cancel_gtt(str(payload.get("al_id", "")))
        if not accepted:
            return _not_ok(result, now)
        return _json(_replies.gtt_ack("OI deleted", result, now))

    def _place_oco(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        try:
            alert_id = self._state.place_oco(payload)
        except ValueError as exc:
            return _not_ok(f"Invalid Input : {exc}", now)
        return _json(_replies.gtt_ack("OI created", alert_id, now))

    def _modify_oco(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        try:
            accepted = self._state.modify_oco(payload)
        except ValueError as exc:
            return _not_ok(f"Invalid Input : {exc}", now)
        word = "OI replaced" if accepted else "Invalid Oi"
        return _json(_replies.gtt_ack(word, str(payload.get("al_id", "")), now))

    def _cancel_oco(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        alert_id = str(payload.get("al_id", ""))
        word = "OI deleted" if self._state.cancel_oco(alert_id) else "Invalid Oi"
        return _json(_replies.gtt_ack(word, alert_id, now))

    def _gtt_rows(
        self, gtts: list[dict[str, Any]], build: Callable[..., dict[str, Any]], now: datetime
    ) -> httpx.Response:
        state = self._state
        with state.lock:
            rows = [
                build(
                    gtt,
                    client_id=state.client_id,
                    instrument=state.find_instrument(str(gtt["exch"]), str(gtt["tsym"])),
                )
                for gtt in gtts
            ]
        return self._rows(rows, now)

    def _pending_gtts(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        return self._gtt_rows(self._state.gtts, _replies.gtt_row, now)

    def _triggered_gtts(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        return self._gtt_rows(self._state.triggered_gtts, _replies.triggered_gtt_row, now)

    def _enabled_gtts(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        return _json(_replies.enabled_gtts(now))

    # --- market data -------------------------------------------------------------------------

    def _quote(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        reply = self._state.quote(str(payload.get("exch", "")), str(payload.get("token", "")))
        return _not_ok(NO_DATA, now) if reply is None else _json(reply)

    def _search(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        text = str(payload.get("stext", "")).lower()
        with self._state.lock:
            rows = [
                dict(row) for row in self._state.instruments.values() if text in row["tsym"].lower()
            ]
        if not rows:
            return _not_ok("No Data :   ", now, 404)  # documented: no match is HTTP 404
        return _json(_replies.search_scrip(rows))

    def _security_info(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        key = (str(payload.get("exch", "")), str(payload.get("token", "")))
        with self._state.lock:
            row = self._state.instruments.get(key)
            instrument = None if row is None else dict(row)
        if instrument is None:
            return _not_ok("Invalid Input :  Invalid Scrip token/exch", now, 400)
        return _json(_replies.security_info(instrument, now))

    def _option_chain(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        """``cnt`` strikes below the mid strike and ``cnt`` at or above it, CE and PE, for the
        underlying and expiry of ``tsym``."""
        exchange = str(payload.get("exch", ""))
        try:
            mid = Decimal(str(payload.get("strprc", "")))
            count = int(str(payload.get("cnt", "")))
        except (ArithmeticError, ValueError):
            return _not_ok("Invalid Input : strprc and cnt must be numbers", now)
        anchor = self._instrument(exchange, str(payload.get("tsym", "")))
        with self._state.lock:
            rows = [
                dict(row) for (exch, _), row in self._state.instruments.items() if exch == exchange
            ]
        if anchor is None:
            return _not_ok(NO_DATA, now)
        options = [
            row
            for row in rows
            if row.get("optt") in ("CE", "PE")
            and row.get("symname") == anchor.get("symname")
            and row.get("exd") == anchor.get("exd")
        ]
        strikes = sorted({Decimal(row["strprc"]) for row in options})
        below = [strike for strike in strikes if strike < mid]
        count = max(count, 0)
        chosen = set(below[max(len(below) - count, 0) :])
        chosen.update([strike for strike in strikes if strike >= mid][:count])
        picked = sorted(
            (row for row in options if Decimal(row["strprc"]) in chosen),
            key=lambda row: (Decimal(row["strprc"]), row["optt"]),
        )
        return _json(_replies.option_chain(picked)) if picked else _not_ok(NO_DATA, now)

    def _instrument(self, exchange: str, symbol: str) -> dict[str, Any] | None:
        """A copy (taken under the lock) of the instrument row with this trading symbol."""
        with self._state.lock:
            row = self._state.find_instrument(exchange, symbol)
            return None if row is None else dict(row)

    def _candles(self, exchange: str, token: str) -> list[dict[str, Any]]:
        with self._state.lock:
            return copy.deepcopy(self._state.candles.get((exchange, token), []))

    def _tp_series(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        """The candles from ``st`` to ``et`` (both included; all when absent), newest first."""
        try:
            start, end = _bound(payload.get("st")), _bound(payload.get("et"))
        except ValueError:
            return _not_ok("Invalid Input : st and et must be epoch seconds", now)
        candles = self._candles(str(payload.get("exch", "")), str(payload.get("token", "")))
        rows = [_replies.candle_row(candle) for candle in candles]
        return self._rows(_newest_first(rows, start, end, _candle_epoch), now)

    def _eod(self, payload: dict[str, Any], now: datetime) -> httpx.Response:
        """The daily candles of the IST days ``from`` to ``to`` (both included), newest first."""
        try:
            start, end = _bound(payload.get("from")), _bound(payload.get("to"))
        except ValueError:
            return _not_ok("Invalid Input : from and to must be epoch seconds", now)
        exchange, _, symbol = str(payload.get("sym", "")).partition(":")
        instrument = self._instrument(exchange, symbol)
        if instrument is None:
            return _json([])  # documented: no data is an empty list
        candles = self._candles(exchange, instrument["token"])
        records = [_replies.eod_record(candle) for candle in candles]
        first = None if start is None else datetime.fromtimestamp(start, IST).date()
        last = None if end is None else datetime.fromtimestamp(end, IST).date()
        records = _newest_first(records, first, last, _candle_day)
        return _json([json.dumps(record) for record in records])

    # --- script master -----------------------------------------------------------------------

    def _script_master(self, code: str) -> httpx.Response:
        if code not in {exchange.value for exchange in Exchange}:
            return httpx.Response(404, text="Not Found")
        with self._state.lock:
            rows = [dict(row) for (exch, _), row in self._state.instruments.items() if exch == code]
        text = io.StringIO()
        writer = csv.writer(text, lineterminator="\n")
        header = list(SCRIPT_MASTER_COLUMNS)
        if code == "BFO":  # production BFO names the strike column "Strike" (spec 8.3)
            header = ["Strike" if name == "StrikePrice" else name for name in header]
        writer.writerow(header)
        for row in rows:
            writer.writerow([row.get(key, "") for key in SCRIPT_MASTER_COLUMNS.values()])
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(f"{code}_symbols.txt", text.getvalue())
        return httpx.Response(
            200, content=archive.getvalue(), headers={"Content-Type": "application/zip"}
        )


class OfflineTransport(httpx.BaseTransport):
    """``httpx.Client(transport=OfflineTransport(state))``: answers from the offline broker."""

    def __init__(self, state: BrokerState) -> None:
        self._handler = _Handler(state)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.read()
        return self._handler.handle(request)


class AsyncOfflineTransport(httpx.AsyncBaseTransport):
    """``httpx.AsyncClient(transport=AsyncOfflineTransport(state))``: the async twin."""

    def __init__(self, state: BrokerState) -> None:
        self._handler = _Handler(state)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        return self._handler.handle(request)
