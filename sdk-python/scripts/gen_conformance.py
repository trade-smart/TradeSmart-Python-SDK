"""Generate the shared conformance vectors in ``specifications/conformance/`` (developer script).

Not shipped (excluded from the wheel and sdist). Run it in Docker from ``sdk-python/``::

    docker compose run --rm dev python scripts/gen_conformance.py

The files are written deterministically (sorted keys, ``repr`` floats, no timestamps), so a run
with an unchanged SDK leaves ``git diff`` empty. Each case lists an ``input``; the ``expected``
value is what the Python SDK produces for it, computed by the ``eval_*`` functions below.
``tests/unit/test_conformance_vectors.py`` imports this module and re-evaluates every committed
case, so the vectors and the SDK cannot drift apart unnoticed.

Encoding rules (see ``specifications/conformance/README.md``): Decimal values are strings,
``None`` is JSON ``null``, dates are ``YYYY-MM-DD``, instants are ISO 8601 with ``+05:30``,
non-finite numbers are never emitted (they are ``null``), and an error outcome is
``{"error": "<ErrorTypeName>"}``.
"""

# ruff: noqa: E402, S101, S106, S107
from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import re
import sys
import tempfile
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

SDK_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = SDK_ROOT.parent / "specifications" / "conformance"
if str(SDK_ROOT) not in sys.path:  # `tests.integration.ws_server` (ws_connect_ack)
    sys.path.insert(0, str(SDK_ROOT))

import httpx

from tradesmart import _option_contracts as oc
from tradesmart import _token_store, round_to_tick
from tradesmart import instruments as _instruments
from tradesmart._client_core import (
    ACK_ID_FIELDS,
    STATE_CHANGING_ENDPOINTS,
    SUCCESS_STATS,
    SUCCESS_STATS_IGNORE_CASE,
    build_raw_call,
)
from tradesmart._slicing import plan_slices
from tradesmart._transport.codec import OK_STATS, encode_jdata, parse_response
from tradesmart._transport.ratelimit import RATE_LIMITS, RateLimiter
from tradesmart._transport.retry import RetryPolicy
from tradesmart._transport.sync_http import apply_retry_after
from tradesmart._version import __version__
from tradesmart.analytics import (
    ChainRow,
    OptionChainSnapshot,
    buildup_from_snapshot,
    classify_buildup,
    max_pain,
    pcr,
)
from tradesmart.auth import AccessToken, compute_checksum, next_expiry
from tradesmart.client import TradeSmart
from tradesmart.config import ClientConfig
from tradesmart.endpoints.gtt import cancel_gtt_call, modify_gtt_call, place_gtt_call
from tradesmart.endpoints.market import to_epoch
from tradesmart.endpoints.orders import (
    cancel_order_call,
    exit_sno_order_call,
    modify_order_call,
    order_margin_call,
    place_order_call,
)
from tradesmart.errors import (
    APIError,
    AuthenticationError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    RequestTimeoutError,
    RequestValidationError,
    ResponseParseError,
    ServerError,
    TradeSmartError,
)
from tradesmart.greeks import (
    Greeks,
    black76,
    black_scholes,
    greeks_from_price,
    implied_volatility,
)
from tradesmart.models.base import IST, _parse_epoch, _parse_ist
from tradesmart.stream.delivery import DeliveryBuffer
from tradesmart.stream.protocol import format_keys, parse_message

# ----------------------------------------------------------------------------- small helpers


def _error(exc: BaseException) -> dict[str, Any]:
    return {"error": type(exc).__name__}


def _hhmm(value: str | None) -> time | None:
    if value is None:
        return None
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _dstr(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


@contextmanager
def _clock(now_iso: str) -> Iterator[None]:
    """Pin the SDK's clock seam (``_option_contracts._now``) to an IST instant."""
    now = datetime.fromisoformat(now_iso)
    original = oc._now
    oc._now = lambda: now  # type: ignore[assignment]
    try:
        yield
    finally:
        oc._now = original  # type: ignore[assignment]


def _greeks(result: Greeks) -> dict[str, Any]:
    return {
        "price": result.price,
        "iv": result.iv,
        "delta": result.delta,
        "gamma": result.gamma,
        "theta": result.theta,
        "vega": result.vega,
        "rho": result.rho,
        "status": result.status,
        "years_to_expiry": result.years_to_expiry,
    }


# ----------------------------------------------------------------------------- evaluators


def eval_checksum(i: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"checksum": compute_checksum(i["app_key"], i["secret_key"], i["code"])}
    except Exception as exc:
        return _error(exc)


def eval_not_ok(i: dict[str, Any]) -> dict[str, Any]:
    try:
        data = parse_response(
            endpoint=i["endpoint"],
            status_code=i["http_status"],
            text=i["body"],
            expect=i["expect"],
            allow_empty=i["allow_empty"],
            state_changing=i["state_changing"],
            success_stats=i.get("success_stats", OK_STATS),
            requires_ack_id=i.get("requires_ack_id"),
            stats_ignore_case=i.get("stats_ignore_case", False),
        )
    except APIError as exc:
        return {"error": type(exc).__name__, "emsg": exc.emsg, "message": exc.message}
    except Exception as exc:
        return _error(exc)
    return {"returns": data}


def _plan(exchange: str, expiry: date) -> oc.GreeksPlan:
    option = oc.OptionContractInfo(
        exchange=exchange,
        tradingsymbol="X",
        token="1",
        symbol="X",
        expiry=expiry,
        option_type="CE",
        strike=Decimal(100),
        tick_size=None,
        lot_size=None,
    )
    return oc.GreeksPlan(exchange, option, None, "black76")


def eval_expiry(i: dict[str, Any]) -> dict[str, Any]:
    try:
        expiry_time = _hhmm(i.get("expiry_time"))
        if i["function"] == "expiry_instant":
            instant = oc.expiry_instant(i["exchange"], date.fromisoformat(i["date"]), expiry_time)
            return {"instant": instant.isoformat()}
        plan = _plan(i["exchange"], date.fromisoformat(i["date"]))
        with _clock(i["now"]):
            return {"expired": oc.option_has_expired(plan, expiry_time)}
    except Exception as exc:
        return _error(exc)


def _option_json(o: oc.OptionContractInfo) -> dict[str, Any]:
    return {
        "exchange": o.exchange,
        "tradingsymbol": o.tradingsymbol,
        "token": o.token,
        "symbol": o.symbol,
        "expiry": o.expiry.isoformat(),
        "option_type": o.option_type,
        "strike": str(o.strike),
        "tick_size": _dstr(o.tick_size),
        "lot_size": o.lot_size,
    }


def eval_contract(i: dict[str, Any]) -> dict[str, Any]:
    try:
        if i["function"] == "find_option":
            return _option_json(oc.find_option(i["rows"], i["tradingsymbol"]))
        o = i["option"]
        option = oc.OptionContractInfo(
            exchange=o["exchange"],
            tradingsymbol=o["tradingsymbol"],
            token="",
            symbol=o["symbol"],
            expiry=date.fromisoformat(o["expiry"]),
            option_type="CE",
            strike=Decimal(0),
            tick_size=None,
            lot_size=None,
        )
        fut = oc.select_future(i["rows"], option)
        return {
            "tradingsymbol": fut.tradingsymbol,
            "token": fut.token,
            "expiry": fut.expiry.isoformat(),
            "same_expiry": fut.same_expiry,
        }
    except Exception as exc:
        return _error(exc)


def eval_price(i: dict[str, Any]) -> dict[str, Any]:
    try:
        if i["model"] == "black76":
            result = black76(
                i["underlying"],
                i["strike"],
                i["years"],
                i["rate"],
                i["volatility"],
                i["option_type"],
            )
        else:
            result = black_scholes(
                i["underlying"],
                i["strike"],
                i["years"],
                i["rate"],
                i["volatility"],
                i["option_type"],
                i.get("dividend_yield", 0.0),
            )
        return _greeks(result)
    except Exception as exc:
        return _error(exc)


def eval_iv(i: dict[str, Any]) -> dict[str, Any]:
    try:
        iv = implied_volatility(
            i["option_price"],
            i["underlying"],
            i["strike"],
            i["years"],
            i["rate"],
            i["option_type"],
            model=i["model"],
            dividend_yield=i.get("dividend_yield", 0.0),
        )
        return {"iv": iv}
    except Exception as exc:
        return _error(exc)


def eval_edge(i: dict[str, Any]) -> dict[str, Any]:
    try:
        result = greeks_from_price(
            i["option_price"],
            i["underlying"],
            i["strike"],
            i["years"],
            i["rate"],
            i["option_type"],
            model=i["model"],
            dividend_yield=i.get("dividend_yield", 0.0),
            tick_size=i.get("tick_size", 0.0),
        )
        return _greeks(result)
    except Exception as exc:
        return _error(exc)


def eval_carry(i: dict[str, Any]) -> dict[str, Any]:
    try:
        if i["function"] == "carry_adjusted_forward":
            forward = oc.carry_adjusted_forward(
                i["future_price"],
                i["rate"],
                i["future_years"],
                i["option_years"],
                i.get("dividend_yield", 0.0),
            )
            return {"forward": forward}
        option_expiry = date.fromisoformat(i["option_expiry"])
        future_expiry = date.fromisoformat(i["future_expiry"])
        plan = _plan(i["exchange"], option_expiry)
        future = oc.UnderlyingFuture("F", "2", future_expiry, future_expiry == option_expiry)
        plan = oc.GreeksPlan(plan.exchange, plan.option, future, "black76")
        with _clock(i["now"]):
            result = oc.option_greeks_result(
                plan,
                option_price=100.0,
                underlying_price=i["future_price"],
                rate=i["rate"],
                dividend_yield=i.get("dividend_yield", 0.0),
                expiry_time=_hhmm(i.get("expiry_time")),
            )
        return {"forward": result.forward, "same_expiry_future": result.same_expiry_future}
    except Exception as exc:
        return _error(exc)


def _snapshot(rows: Sequence[dict[str, Any]]) -> OptionChainSnapshot:
    chain = tuple(
        ChainRow(
            strike=Decimal(r["strike"]),
            option_type=r["option_type"],
            tradingsymbol="",
            token="",
            last_price=None,
            open_interest=_dec(r.get("open_interest")),
            volume=_dec(r.get("volume")),
            close=None,
        )
        for r in rows
    )
    return OptionChainSnapshot("NFO", "NIFTY", datetime(2026, 1, 1, tzinfo=IST), chain, ())


def eval_pcr(i: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"pcr": _dstr(pcr(_snapshot(i["rows"]), i["by"]))}
    except Exception as exc:
        return _error(exc)


def eval_max_pain(i: dict[str, Any]) -> dict[str, Any]:
    try:
        result = max_pain(_snapshot(i["rows"]))
        if result is None:
            return {"max_pain": None}
        return {
            "max_pain": {
                "strike": str(result.strike),
                "payouts": {str(k): str(v) for k, v in result.payouts.items()},
            }
        }
    except Exception as exc:
        return _error(exc)


def _number(value: Any) -> Any:
    return float(value) if isinstance(value, str) else value  # "NaN" / "Infinity" strings


def eval_buildup(i: dict[str, Any]) -> dict[str, Any]:
    try:
        if i["function"] == "classify_buildup":
            state = classify_buildup(_number(i["price_change"]), _number(i["oi_change"]))
            return {"state": str(state.value)}
        reading = buildup_from_snapshot(i["snapshot"])
        return {
            "state": str(reading.state.value),
            "price_change": _dstr(reading.price_change),
            "oi_change": _dstr(reading.oi_change),
            "basis": reading.basis,
        }
    except Exception as exc:
        return _error(exc)


def eval_ws_ack(i: dict[str, Any]) -> dict[str, Any]:
    from tests.integration.ws_server import MockWsServer
    from tradesmart.stream.async_stream import AsyncTradeSmartStream

    async def run() -> dict[str, Any]:
        async with MockWsServer() as server:
            server.ok_status = i["s"]
            config = ClientConfig(ws_url=server.url, ws_auth_timeout=2.0)
            try:
                async with AsyncTradeSmartStream("TEST123", "tok", config=config):
                    pass
            except AuthenticationError:
                return {"accepted": False}
            return {"accepted": True}

    return asyncio.run(run())


_CONFIG_UA = f"tradesmart-python/{__version__}"


_NON_FINITE = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}


def _non_finite(value: Any) -> Any:
    """JSON has no NaN / infinity: the vector files spell them as these three strings."""
    return _NON_FINITE.get(value, value) if isinstance(value, str) else value


def eval_config(i: dict[str, Any]) -> dict[str, Any]:
    overrides = {k: _non_finite(v) for k, v in i["overrides"].items()}
    try:
        values = asdict(ClientConfig(**overrides))
    except Exception as exc:
        return {**_error(exc), "message": str(exc)}
    if values["user_agent"] == _CONFIG_UA:
        values["user_agent"] = "tradesmart-python/<version>"
    fields = i.get("fields")
    return {k: v for k, v in values.items() if fields is None or k in fields}


# ----------------------------------------------------------------------------- case builders


def _checksum_cases() -> list[Any]:
    worked = {"app_key": "ABC", "secret_key": "123", "code": "x1y2z3"}
    return [
        ("worked_example_from_official_docs", worked),
        (
            "synthetic_typical",
            {"app_key": "APPKEY-SYNTH-001", "secret_key": "SECRET-SYNTH-001", "code": "code-0001"},
        ),
        (
            "synthetic_non_ascii_is_utf8",
            {"app_key": "kéy", "secret_key": "sécret", "code": "cöde"},
        ),
        ("empty_code_is_a_configuration_error", {**worked, "code": ""}),
    ]


def _nok(
    name: str,
    status: int,
    body: str,
    *,
    endpoint: str = "Limits",
    expect: str = "object",
    allow_empty: bool = False,
    state_changing: bool = False,
) -> Any:
    return (
        name,
        {
            "http_status": status,
            "endpoint": endpoint,
            "body": body,
            "expect": expect,
            "allow_empty": allow_empty,
            "state_changing": state_changing,
        },
    )


def _not_ok(emsg: str) -> str:
    return json.dumps({"stat": "Not_Ok", "emsg": emsg, "request_time": "10:00:00 01-01-2026"})


def _not_ok_cases() -> list[Any]:
    place = {"endpoint": "PlaceOrder", "state_changing": True}
    li = {"endpoint": "Positions", "expect": "list", "allow_empty": True}
    cases = [
        _nok("http_401_not_ok", 401, _not_ok("Session Expired : Invalid Session Key")),
        _nok("http_401_plain_message", 401, _not_ok("Unauthorized")),
        _nok("http_403_not_ok", 403, _not_ok("Forbidden")),
        _nok("http_404_not_ok", 404, _not_ok("Not found")),
        _nok("http_429_not_ok", 429, _not_ok("Too many requests")),
        _nok("http_500_not_ok_read", 500, _not_ok("Internal error")),
        _nok("http_503_not_ok_read", 503, _not_ok("Unavailable")),
        _nok(
            "http_500_not_ok_state_changing_is_a_definitive_rejection", 500, _not_ok("RMS"), **place
        ),
        _nok("http_200_session_expired", 200, _not_ok("Session Expired : Invalid Session Key")),
        _nok("http_200_invalid_access_token", 200, _not_ok("Invalid Access Token")),
        _nok("http_200_invalid_session_lowercase", 200, _not_ok("invalid session key")),
        _nok("http_200_invalid_user_id_uppercase", 200, _not_ok("INVALID USER ID")),
        _nok("http_200_invalid_acct_id", 200, _not_ok("Invalid Acct Id")),
        _nok("http_400_session_expired", 400, _not_ok("Session Expired")),
        _nok(
            "http_200_session_expired_on_state_changing", 200, _not_ok("Session Expired"), **place
        ),
        _nok("http_200_plain_not_ok_read", 200, _not_ok("Invalid Input : Missing uid")),
        _nok("http_400_plain_not_ok_read", 400, _not_ok("Bad request")),
        _nok("http_200_not_ok_without_emsg", 200, json.dumps({"stat": "Not_Ok"})),
        _nok("no_data_on_list_endpoint_is_an_empty_list", 200, _not_ok("no data"), **li),
        _nok(
            "no_data_is_case_insensitive_and_a_substring",
            200,
            _not_ok("Error : NO DATA found"),
            **li,
        ),
        _nok("no_data_without_allow_empty_is_a_plain_error", 200, _not_ok("no data")),
        _nok("other_not_ok_on_list_endpoint", 200, _not_ok("Something else"), **li),
        _nok(
            "no_data_check_runs_before_the_authentication_check",
            200,
            _not_ok("Session Expired no data"),
            **li,
        ),
        _nok("no_data_on_http_401_is_still_an_empty_list", 401, _not_ok("no data"), **li),
        _nok("order_rejected_http_200", 200, _not_ok("RMS:Margin Shortfall"), **place),
        _nok("order_rejected_http_400", 400, _not_ok("RMS:Margin Shortfall"), **place),
        _nok(
            "order_rejected_http_404",
            404,
            _not_ok("Order not found"),
            endpoint="CancelOrder",
            state_changing=True,
        ),
        _nok("order_not_ok_http_429_is_rate_limit", 429, _not_ok("Too many requests"), **place),
        _nok("order_not_ok_http_401_is_authentication", 401, _not_ok("Unauthorized"), **place),
        _nok("ok_object", 200, json.dumps({"stat": "Ok", "uname": "SYNTH"})),
        _nok(
            "ok_object_on_state_changing",
            200,
            json.dumps({"stat": "Ok", "norenordno": "1"}),
            **place,
        ),
        _nok(
            "ok_list",
            200,
            json.dumps([{"stat": "Ok", "tsym": "X"}]),
            endpoint="Positions",
            expect="list",
        ),
        _nok("empty_list_with_allow_empty", 200, "[]", **li),
        _nok(
            "ok_object_where_a_list_is_expected",
            200,
            json.dumps({"stat": "Ok"}),
            endpoint="Positions",
            expect="list",
        ),
        _nok(
            "ok_object_where_a_list_is_expected_state_changing_is_unknown",
            200,
            json.dumps({"stat": "Ok", "norenordno": "1"}),
            endpoint="PlaceMultiOrder",
            expect="list",
            state_changing=True,
        ),
        _nok("list_where_an_object_is_expected", 200, "[1]"),
        _nok(
            "empty_list_where_an_object_is_expected_with_allow_empty", 200, "[]", allow_empty=True
        ),
        _nok("list_on_http_500", 500, "[1]", endpoint="Positions", expect="list"),
        _nok("list_on_http_404", 404, "[1]", endpoint="Positions", expect="list"),
        _nok("list_on_http_400", 400, "[1]", endpoint="Positions", expect="list"),
        _nok("list_on_http_401", 401, "[1]", endpoint="Positions", expect="list"),
        _nok("list_on_http_429", 429, "[1]", endpoint="Positions", expect="list"),
        _nok("non_json_http_200_read", 200, "<html>oops</html>"),
        _nok("non_json_http_502_read", 502, "<html>Bad Gateway</html>"),
        _nok("non_json_http_401_read", 401, "Unauthorized"),
        _nok("non_json_http_403_read", 403, "Forbidden"),
        _nok("non_json_http_429_read", 429, "slow down"),
        _nok("non_json_http_404_read", 404, "<html>missing</html>"),
        _nok("non_json_http_200_state_changing_is_unknown", 200, "<html>oops</html>", **place),
        _nok("non_json_http_500_state_changing_is_unknown", 500, "<html>oops</html>", **place),
        _nok("empty_body_state_changing_is_unknown", 200, "", **place),
        _nok("empty_body_read", 200, ""),
        _nok("unexpected_shape_read", 200, json.dumps({"hello": "world"})),
        _nok("unexpected_shape_http_500_read", 500, json.dumps({"hello": "world"})),
        _nok(
            "unexpected_shape_state_changing_is_unknown",
            200,
            json.dumps({"hello": "world"}),
            **place,
        ),
        _nok("json_scalar_state_changing_is_unknown", 200, "null", **place),
        _nok("list_on_state_changing_is_unknown", 200, "[]", **place),
        _nok("stat_other_than_ok_or_not_ok_is_unexpected", 200, json.dumps({"stat": "Pending"})),
        _nok("emsg_not_a_string_is_ignored", 200, json.dumps({"stat": "Not_Ok", "emsg": 5})),
        # Redirects are never followed: a 3xx is classified like any other status.
        _nok("redirect_307_empty_body_state_changing_is_unknown", 307, "", **place),
        _nok("redirect_302_html_read_is_a_parse_error", 302, "<html>Found</html>"),
        _nok("redirect_301_not_ok_read", 301, _not_ok("Moved")),
    ]
    return cases


def _expiry_cases() -> list[Any]:
    out: list[Any] = []

    def inst(name: str, exchange: str, day: str, expiry_time: str | None = None) -> None:
        out.append(
            (
                name,
                {
                    "function": "expiry_instant",
                    "exchange": exchange,
                    "date": day,
                    "expiry_time": expiry_time,
                },
            )
        )

    inst("nfo_default_close", "NFO", "2026-11-26")
    inst("bfo_default_close", "BFO", "2026-11-26")
    inst("cds_default_close", "CDS", "2026-11-26")
    inst("bcd_default_close", "BCD", "2026-11-26")
    inst("mcx_default_close", "MCX", "2026-11-26")
    inst("nfo_with_expiry_time_override", "NFO", "2026-11-26", "15:00")
    inst("bfo_with_expiry_time_override", "BFO", "2026-11-26", "10:15")
    inst("cds_with_expiry_time_override", "CDS", "2026-11-26", "17:00")
    inst("bcd_with_expiry_time_override", "BCD", "2026-11-26", "09:00")
    inst("mcx_with_expiry_time_override", "MCX", "2026-11-26", "17:00")
    inst("mcx_override_to_midnight", "MCX", "2026-11-26", "00:00")
    inst("exchange_is_case_and_space_insensitive", "  nfo ", "2026-11-26")
    inst("unknown_exchange_without_override_is_an_error", "NSE", "2026-11-26")
    inst("unknown_exchange_with_override_is_accepted", "NSE", "2026-11-26", "15:30")
    inst("leap_day", "NFO", "2028-02-29")

    def expired(name: str, exchange: str, now: str, expiry_time: str | None = None) -> None:
        out.append(
            (
                name,
                {
                    "function": "option_has_expired",
                    "exchange": exchange,
                    "date": "2026-11-26",
                    "expiry_time": expiry_time,
                    "now": now,
                },
            )
        )

    expired("nfo_one_minute_before_close", "NFO", "2026-11-26T15:29:00+05:30")
    expired("nfo_exactly_at_close_is_expired", "NFO", "2026-11-26T15:30:00+05:30")
    expired("nfo_one_minute_after_close", "NFO", "2026-11-26T15:31:00+05:30")
    expired("nfo_morning_of_expiry", "NFO", "2026-11-26T09:15:00+05:30")
    expired("nfo_day_before", "NFO", "2026-11-25T23:59:00+05:30")
    expired("nfo_day_after", "NFO", "2026-11-27T09:00:00+05:30")
    expired("bfo_one_minute_before_close", "BFO", "2026-11-26T15:29:00+05:30")
    expired("bfo_one_minute_after_close", "BFO", "2026-11-26T15:31:00+05:30")
    expired("cds_one_minute_before_close", "CDS", "2026-11-26T12:29:00+05:30")
    expired("cds_one_minute_after_close", "CDS", "2026-11-26T12:31:00+05:30")
    expired("bcd_one_minute_after_close", "BCD", "2026-11-26T12:31:00+05:30")
    expired("mcx_one_minute_before_close", "MCX", "2026-11-26T23:29:00+05:30")
    expired("mcx_exactly_at_close_is_expired", "MCX", "2026-11-26T23:30:00+05:30")
    expired("mcx_one_minute_after_close", "MCX", "2026-11-26T23:31:00+05:30")
    expired("mcx_afternoon_is_not_expired", "MCX", "2026-11-26T16:00:00+05:30")
    expired("override_before", "NFO", "2026-11-26T14:59:00+05:30", "15:00")
    expired("override_after", "NFO", "2026-11-26T15:01:00+05:30", "15:00")
    expired("now_given_in_utc_is_compared_as_an_instant", "NFO", "2026-11-26T10:01:00+00:00")
    return out


_COLS = ("Exchange", "Token", "LotSize", "Symbol", "TradingSymbol", "Instrument", "Expiry")


def _row(
    ts: str,
    instrument: str,
    expiry: str,
    *,
    exchange: str = "NFO",
    symbol: str = "NIFTY",
    option_type: str = "",
    strike: str = "0",
    lot: str = "65",
    tick: str = "0.05",
    token: str | None = None,
) -> dict[str, str]:
    return {
        "Exchange": exchange,
        "Token": token if token is not None else "t-" + ts,
        "LotSize": lot,
        "Symbol": symbol,
        "TradingSymbol": ts,
        "Instrument": instrument,
        "Expiry": expiry,
        "OptionType": option_type,
        "StrikePrice": strike,
        "TickSize": tick,
    }


def _contract_cases() -> list[Any]:
    ce = _row("NIFTY26NOV26C24000", "OPTIDX", "26-NOV-2026", option_type="CE", strike="24000")
    pe = _row("NIFTY26NOV26P24000", "OPTIDX", "26-NOV-2026", option_type="PE", strike="24000")
    fut_nov = _row("NIFTY26NOV26FUT", "FUTIDX", "26-NOV-2026")
    fut_dec = _row("NIFTY31DEC26FUT", "FUTIDX", "31-DEC-2026")
    fut_oct = _row("NIFTY29OCT26FUT", "FUTIDX", "29-OCT-2026")
    fut_jan = _row("NIFTY28JAN27FUT", "FUTIDX", "28-JAN-2027")
    other = _row("BANKNIFTY26NOV26FUT", "FUTIDX", "26-NOV-2026", symbol="BANKNIFTY")
    base = [ce, pe, fut_nov, fut_dec, other]

    def find(name: str, rows: list[dict[str, Any]], ts: str) -> Any:
        return (name, {"function": "find_option", "rows": rows, "tradingsymbol": ts})

    def drop(row: dict[str, str], *keys: str) -> dict[str, str]:
        return {k: v for k, v in row.items() if k not in keys}

    def fut(name: str, rows: list[dict[str, Any]], expiry: str, **o: str) -> Any:
        option = {"exchange": "NFO", "tradingsymbol": "NIFTY26NOV26C24000", "symbol": "NIFTY"}
        option.update({"expiry": expiry, **o})
        return (name, {"function": "select_future", "rows": rows, "option": option})

    mcx_opt = _row(
        "CRUDEOIL19NOV26C6500",
        "OPTFUT",
        "19-NOV-2026",
        exchange="MCX",
        symbol="CRUDEOIL",
        option_type="CE",
        strike="6500",
        lot="100",
        tick="0.1",
    )
    mcx_fut = _row("CRUDEOIL19NOV26FUT", "FUTCOM", "19-NOV-2026", exchange="MCX", symbol="CRUDEOIL")

    def bfo(ts: str, instrument: str, expiry: str, symbol: str, **o: str) -> dict[str, str]:
        """A BFO row: the script master names the strike column ``Strike``."""
        row = _row(ts, instrument, expiry, exchange="BFO", symbol=symbol, lot="20", **o)
        return {("Strike" if k == "StrikePrice" else k): v for k, v in row.items()}

    sensex_ce = bfo(
        "SENSEX26N1985200CE", "OPTIDX", "19-NOV-2026", "BSXOPT", option_type="CE", strike="85200"
    )
    sensex_fut = bfo("SENSEX26NOVFUT", "FUTIDX", "26-NOV-2026", "BSXFUT")
    sx50_fut = bfo("SENSEX5026NOVFUT", "FUTIDX", "26-NOV-2026", "SX50FUT")

    def bfo_fut(name: str, rows: list[dict[str, Any]], symbol: str, **o: str) -> Any:
        option = {"exchange": "BFO", "tradingsymbol": "SENSEX26N1985200CE", "symbol": symbol}
        return fut(name, rows, "2026-11-19", **{**option, **o})

    return [
        find("find_call", base, "NIFTY26NOV26C24000"),
        find("find_put", base, "NIFTY26NOV26P24000"),
        find("find_is_case_insensitive", base, "nifty26nov26c24000"),
        find("find_trims_the_wanted_symbol", base, "  NIFTY26NOV26C24000  "),
        find(
            "find_trims_the_row_symbol",
            [{**ce, "TradingSymbol": " NIFTY26NOV26C24000 "}],
            "NIFTY26NOV26C24000",
        ),
        find("find_not_found", base, "NIFTY26NOV26C99999"),
        find("find_empty_tradingsymbol", base, "   "),
        find("find_ignores_future_rows_with_that_name", [fut_nov], "NIFTY26NOV26FUT"),
        find("find_optstk_prefix", [{**ce, "Instrument": "OPTSTK"}], "NIFTY26NOV26C24000"),
        find(
            "find_instrument_prefix_is_case_insensitive",
            [{**ce, "Instrument": "optidx"}],
            "NIFTY26NOV26C24000",
        ),
        find(
            "find_first_matching_row_wins",
            [ce, {**ce, "Token": "dup", "LotSize": "1"}],
            "NIFTY26NOV26C24000",
        ),
        find("find_mcx_option_on_future", [mcx_opt, mcx_fut], "CRUDEOIL19NOV26C6500"),
        find("find_decimal_strike", [{**ce, "StrikePrice": "24000.50"}], "NIFTY26NOV26C24000"),
        find(
            "find_strike_keeps_trailing_zeros",
            [{**ce, "StrikePrice": "24000.00"}],
            "NIFTY26NOV26C24000",
        ),
        find(
            "find_fractional_lot_size_truncates", [{**ce, "LotSize": "75.5"}], "NIFTY26NOV26C24000"
        ),
        find("find_blank_lot_size_is_null", [{**ce, "LotSize": " "}], "NIFTY26NOV26C24000"),
        find("find_missing_lot_size_is_null", [drop(ce, "LotSize")], "NIFTY26NOV26C24000"),
        find("find_blank_tick_size_is_null", [{**ce, "TickSize": ""}], "NIFTY26NOV26C24000"),
        find(
            "find_unparseable_tick_size_is_null", [{**ce, "TickSize": "abc"}], "NIFTY26NOV26C24000"
        ),
        find(
            "find_lowercase_expiry_month", [{**ce, "Expiry": "26-nov-2026"}], "NIFTY26NOV26C24000"
        ),
        find("find_lowercase_option_type", [{**ce, "OptionType": "ce"}], "NIFTY26NOV26C24000"),
        find("find_exchange_is_upper_cased", [{**ce, "Exchange": "nfo"}], "NIFTY26NOV26C24000"),
        find(
            "find_invalid_lot_size_is_malformed", [{**ce, "LotSize": "abc"}], "NIFTY26NOV26C24000"
        ),
        find("find_nan_lot_size_is_malformed", [{**ce, "LotSize": "NaN"}], "NIFTY26NOV26C24000"),
        find("find_malformed_option_type", [{**ce, "OptionType": "XX"}], "NIFTY26NOV26C24000"),
        find("find_missing_option_type", [drop(ce, "OptionType")], "NIFTY26NOV26C24000"),
        find("find_missing_expiry", [drop(ce, "Expiry")], "NIFTY26NOV26C24000"),
        find("find_unparseable_expiry", [{**ce, "Expiry": "2026-11-26"}], "NIFTY26NOV26C24000"),
        find("find_blank_symbol", [{**ce, "Symbol": " "}], "NIFTY26NOV26C24000"),
        find("find_missing_strike", [drop(ce, "StrikePrice")], "NIFTY26NOV26C24000"),
        find("find_invalid_strike", [{**ce, "StrikePrice": "abc"}], "NIFTY26NOV26C24000"),
        find("find_nan_strike", [{**ce, "StrikePrice": "NaN"}], "NIFTY26NOV26C24000"),
        find(
            "find_malformed_first_match_raises_even_if_a_later_row_is_valid",
            [{**ce, "OptionType": "XX"}, ce],
            "NIFTY26NOV26C24000",
        ),
        fut("select_same_expiry", base, "2026-11-26"),
        fut("select_later_when_no_same_expiry", [fut_dec, other], "2026-11-26"),
        fut("select_nearest_later_of_several", [fut_jan, fut_dec], "2026-11-26"),
        fut(
            "select_earlier_when_no_same_or_later",
            [fut_oct, _row("NIFTY24SEP26FUT", "FUTIDX", "24-SEP-2026")],
            "2026-11-26",
        ),
        fut(
            "select_nearest_earlier_of_several",
            [_row("NIFTY24SEP26FUT", "FUTIDX", "24-SEP-2026"), fut_oct],
            "2026-11-26",
        ),
        fut(
            "select_same_beats_later_and_earlier",
            [fut_oct, fut_dec, fut_nov, fut_jan],
            "2026-11-26",
        ),
        fut("select_later_beats_earlier", [fut_oct, fut_dec], "2026-11-26"),
        fut(
            "select_first_row_wins_among_same_expiry",
            [fut_nov, {**fut_nov, "TradingSymbol": "DUP", "Token": "dup"}],
            "2026-11-26",
        ),
        fut("select_ignores_other_symbols", [other], "2026-11-26"),
        fut("select_ignores_option_rows", [ce, pe], "2026-11-26"),
        fut(
            "select_cross_exchange_future_is_ignored",
            [_row("NIFTY26NOV26FUT", "FUTIDX", "26-NOV-2026", exchange="BFO"), fut_dec],
            "2026-11-26",
        ),
        fut(
            "select_only_other_exchange_is_not_found",
            [_row("NIFTY26NOV26FUT", "FUTIDX", "26-NOV-2026", exchange="BFO")],
            "2026-11-26",
        ),
        fut(
            "select_symbol_and_exchange_are_case_insensitive",
            [{**fut_nov, "Symbol": "nifty", "Exchange": "nfo"}],
            "2026-11-26",
        ),
        fut(
            "select_skips_future_with_unparseable_expiry",
            [{**fut_nov, "Expiry": "soon"}, fut_dec],
            "2026-11-26",
        ),
        fut(
            "select_skips_future_with_missing_expiry",
            [drop(fut_nov, "Expiry"), fut_dec],
            "2026-11-26",
        ),
        fut(
            "select_skips_future_with_blank_tradingsymbol",
            [{**fut_nov, "TradingSymbol": " "}, fut_dec],
            "2026-11-26",
        ),
        fut(
            "select_skips_future_with_missing_tradingsymbol",
            [drop(fut_nov, "TradingSymbol"), fut_dec],
            "2026-11-26",
        ),
        fut(
            "select_all_futures_malformed_is_not_found",
            [{**fut_nov, "Expiry": "soon"}],
            "2026-11-26",
        ),
        fut("select_no_rows_is_not_found", [], "2026-11-26"),
        fut("select_blank_option_symbol_is_an_error", base, "2026-11-26", symbol=" "),
        fut(
            "select_mcx_same_expiry",
            [mcx_opt, mcx_fut],
            "2026-11-19",
            exchange="MCX",
            symbol="CRUDEOIL",
            tradingsymbol="CRUDEOIL19NOV26C6500",
        ),
        fut(
            "select_mcx_future_on_another_exchange_is_ignored",
            [mcx_fut],
            "2026-11-19",
            exchange="NFO",
            symbol="CRUDEOIL",
        ),
        find("find_bfo_strike_column", [sensex_ce, sensex_fut], "SENSEX26N1985200CE"),
        find(
            "find_strikeprice_column_wins",
            [{**ce, "StrikePrice": "24000", "Strike": "25000"}],
            "NIFTY26NOV26C24000",
        ),
        find(
            "find_no_strike_column_is_malformed",
            [drop(sensex_ce, "Strike")],
            "SENSEX26N1985200CE",
        ),
        bfo_fut("select_bfo_opt_falls_back_to_fut", [sensex_ce, sx50_fut, sensex_fut], "BSXOPT"),
        bfo_fut("select_bfo_family_is_kept", [sensex_fut], "SX50OPT"),
        bfo_fut(
            "select_own_symbol_beats_fallback",
            [sensex_fut, {**sx50_fut, "Symbol": "BSXOPT", "TradingSymbol": "BSXOPT-FUTROW"}],
            "BSXOPT",
        ),
        bfo_fut(
            "select_fallback_other_exchange_is_ignored",
            [{**sensex_fut, "Exchange": "NFO"}],
            "BSXOPT",
        ),
        bfo_fut(
            "select_symbol_opt_alone_is_not_found",
            [{**sensex_fut, "Symbol": "FUT"}],
            "OPT",
        ),
        bfo_fut("select_fallback_symbol_is_trimmed_and_upper_cased", [sensex_fut], " bsxopt "),
        find(
            "find_blank_strikeprice_with_strike_is_malformed",
            [{**sensex_ce, "StrikePrice": ""}],
            "SENSEX26N1985200CE",
        ),
        bfo_fut(
            "select_fallback_when_own_rows_are_unusable",
            [
                {**sensex_fut, "Symbol": "BSXOPT", "TradingSymbol": "BAD", "Expiry": "garbage"},
                sensex_fut,
            ],
            "BSXOPT",
        ),
    ]


def _price_input(
    model: str,
    option_type: str,
    underlying: float,
    strike: float,
    years: float,
    rate: float,
    vol: float,
    q: float | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "model": model,
        "option_type": option_type,
        "underlying": underlying,
        "strike": strike,
        "years": years,
        "rate": rate,
        "volatility": vol,
    }
    if q is not None:
        data["dividend_yield"] = q
    return data


_GRID_YEARS = ((7 / 365, "7d"), (0.5, "half_year"))
_GRID_MONEYNESS = ((0.9, "otm_or_itm_90"), (1.0, "atm"), (1.1, "itm_or_otm_110"))
_GRID_VOLS = (0.2, 0.4)


def _price_cases() -> list[Any]:
    cases: list[Any] = [
        ("bsm_textbook_hull_call", _price_input("bsm", "CE", 42, 40, 0.5, 0.10, 0.20)),
        ("bsm_textbook_hull_put", _price_input("bsm", "PE", 42, 40, 0.5, 0.10, 0.20)),
        ("black76_textbook_hull_call", _price_input("black76", "CE", 20, 20, 4 / 12, 0.09, 0.25)),
        ("black76_textbook_hull_put", _price_input("black76", "PE", 20, 20, 4 / 12, 0.09, 0.25)),
        ("bsm_with_dividend_yield_call", _price_input("bsm", "CE", 100, 90, 1.0, 0.05, 0.35, 0.02)),
        ("bsm_with_dividend_yield_put", _price_input("bsm", "PE", 100, 90, 1.0, 0.05, 0.35, 0.02)),
        (
            "bsm_index_scale_with_yield",
            _price_input("bsm", "CE", 19500, 20000, 7 / 365, 0.065, 0.12, 0.01),
        ),
        (
            "black76_index_future_call",
            _price_input("black76", "CE", 24150, 24000, 7 / 365, 0.065, 0.14),
        ),
        (
            "black76_index_future_put",
            _price_input("black76", "PE", 24150, 24000, 7 / 365, 0.065, 0.14),
        ),
        ("black76_zero_rate_low_price", _price_input("black76", "CE", 3.5, 4.0, 0.1, 0.0, 0.8)),
        (
            "black76_option_type_call_alias",
            _price_input("black76", "CALL", 100, 100, 0.25, 0.065, 0.2),
        ),
        (
            "black76_option_type_put_alias_lowercase",
            _price_input("black76", "put", 100, 100, 0.25, 0.065, 0.2),
        ),
        (
            "black76_five_minutes_left_is_near_expiry",
            _price_input("black76", "CE", 100, 100, 5 / (365 * 24 * 60), 0.065, 0.2),
        ),
        (
            "bsm_five_minutes_left_is_near_expiry",
            _price_input("bsm", "PE", 100, 100, 5 / (365 * 24 * 60), 0.065, 0.2),
        ),
        (
            "black76_years_zero_is_expired_itm_call",
            _price_input("black76", "CE", 105, 100, 0.0, 0.065, 0.2),
        ),
        (
            "black76_negative_years_is_expired_otm_put",
            _price_input("black76", "PE", 105, 100, -0.01, 0.065, 0.2),
        ),
        (
            "bsm_years_zero_is_expired_atm_call",
            _price_input("bsm", "CE", 100, 100, 0.0, 0.065, 0.2),
        ),
        (
            "black76_sub_minute_years_uses_the_one_minute_floor",
            _price_input("black76", "CE", 100, 100, 1e-6, 0.065, 0.2),
        ),
        (
            "black76_option_type_invalid_is_an_error",
            _price_input("black76", "XX", 100, 100, 0.25, 0.065, 0.2),
        ),
        (
            "black76_zero_volatility_is_an_error",
            _price_input("black76", "CE", 100, 100, 0.25, 0.065, 0.0),
        ),
        (
            "black76_zero_strike_is_an_error",
            _price_input("black76", "CE", 100, 0, 0.25, 0.065, 0.2),
        ),
        (
            "bsm_negative_underlying_is_an_error",
            _price_input("bsm", "CE", -1, 100, 0.25, 0.065, 0.2),
        ),
    ]
    for model in ("black76", "bsm"):
        for kind in ("CE", "PE"):
            for moneyness, m_name in _GRID_MONEYNESS:
                for years, y_name in _GRID_YEARS:
                    for vol in _GRID_VOLS:
                        name = f"grid_{model}_{kind.lower()}_{m_name}_{y_name}_vol{int(vol * 100)}"
                        data = _price_input(
                            model, kind, 100.0, 100.0 * moneyness, years, 0.065, vol
                        )
                        if model == "bsm":
                            data["dividend_yield"] = 0.01
                        cases.append((name, data))
    return cases


def _iv_cases() -> list[Any]:
    cases: list[Any] = []
    for model in ("black76", "bsm"):
        for kind in ("CE", "PE"):
            for moneyness, m_name in ((0.9, "m90"), (1.0, "atm"), (1.1, "m110")):
                for years, y_name in ((7 / 365, "7d"), (0.5, "half_year")):
                    for vol in (0.1, 0.3, 1.0):
                        strike = 100.0 * moneyness
                        q = 0.015 if model == "bsm" else None
                        priced = eval_price(
                            _price_input(model, kind, 100.0, strike, years, 0.065, vol, q)
                        )
                        # Only well-conditioned round trips (vega high enough to pin vol).
                        if "error" in priced or priced["vega"] * 100 * vol < 1e-4:
                            continue
                        data: dict[str, Any] = {
                            "model": model,
                            "option_type": kind,
                            "option_price": priced["price"],
                            "underlying": 100.0,
                            "strike": strike,
                            "years": years,
                            "rate": 0.065,
                            "volatility_used": vol,
                        }
                        if q is not None:
                            data["dividend_yield"] = q
                        label = f"{model}_{kind.lower()}_{m_name}_{y_name}_vol{int(vol * 100)}"
                        cases.append((f"round_trip_{label}", data))

    def none(name: str, **over: Any) -> None:
        data: dict[str, Any] = {
            "model": "black76",
            "option_type": "CE",
            "option_price": 3.0,
            "underlying": 100.0,
            "strike": 100.0,
            "years": 0.25,
            "rate": 0.065,
        }
        data.update(over)
        cases.append((name, data))

    # discounted intrinsic of a 100/90 call at 0.25y, r=6.5 %: 100 - 90 * exp(-0.065 * 0.25) * ...
    none("no_iv_price_zero", option_price=0.0)
    none("no_iv_price_negative", option_price=-1.0)
    none("no_iv_expired_years_zero", years=0.0)
    none("no_iv_expired_years_negative", years=-0.5)
    none("no_iv_call_price_below_discounted_intrinsic", strike=80.0, option_price=19.0)
    none(
        "no_iv_put_price_below_discounted_intrinsic",
        option_type="PE",
        strike=120.0,
        option_price=19.0,
    )
    none("no_iv_call_price_above_the_forward", option_price=120.0)
    none("no_iv_put_price_above_the_discounted_strike", option_type="PE", option_price=120.0)
    none("no_iv_price_needing_volatility_above_500_percent", option_price=99.9999999)
    none("no_iv_bsm_call_price_above_the_spot", model="bsm", option_price=120.0)
    none(
        "no_iv_bsm_price_below_intrinsic_with_yield",
        model="bsm",
        strike=80.0,
        option_price=19.0,
        dividend_yield=0.02,
    )
    none("error_invalid_option_type", option_type="XX")
    none("error_invalid_model", model="heston")
    none("error_zero_strike", strike=0.0)
    return cases


def _edge_input(
    price: float | None,
    underlying: float | None,
    strike: float,
    years: float,
    kind: str = "CE",
    *,
    model: str = "black76",
    rate: float = 0.065,
    q: float = 0.0,
    tick: float = 0.0,
) -> dict[str, Any]:
    return {
        "option_price": price,
        "underlying": underlying,
        "strike": strike,
        "years": years,
        "rate": rate,
        "option_type": kind,
        "model": model,
        "dividend_yield": q,
        "tick_size": tick,
    }


_MIN = 1 / (365 * 24 * 60)


def _edge_cases() -> list[Any]:
    # Discounted intrinsic of the 100-strike call/put with the future at 105/95, 0.25y, r=6.5 %.
    call_intrinsic = 105 * math.exp(-0.065 * 0.25) - 100 * math.exp(-0.065 * 0.25)
    put_intrinsic = 100 * math.exp(-0.065 * 0.25) - 95 * math.exp(-0.065 * 0.25)
    return [
        ("expired_itm_call", _edge_input(6.0, 105.0, 100.0, 0.0)),
        ("expired_otm_call", _edge_input(0.5, 95.0, 100.0, 0.0)),
        ("expired_itm_put", _edge_input(6.0, 95.0, 100.0, 0.0, "PE")),
        ("expired_otm_put", _edge_input(0.5, 105.0, 100.0, 0.0, "PE")),
        ("expired_atm_call_delta_half", _edge_input(1.0, 100.0, 100.0, 0.0)),
        ("expired_atm_put_delta_minus_half", _edge_input(1.0, 100.0, 100.0, 0.0, "PE")),
        ("expired_negative_years", _edge_input(1.0, 105.0, 100.0, -0.2)),
        ("expired_price_is_intrinsic_not_the_given_price", _edge_input(50.0, 105.0, 100.0, 0.0)),
        ("expired_without_option_price", _edge_input(None, 105.0, 100.0, 0.0)),
        ("expired_bsm", _edge_input(6.0, 105.0, 100.0, 0.0, model="bsm", q=0.02)),
        ("expired_without_underlying_is_no_price", _edge_input(6.0, None, 100.0, 0.0)),
        ("near_expiry_ten_minutes", _edge_input(0.035, 100.0, 100.0, 10 * _MIN)),
        ("near_expiry_just_under_thirty_minutes", _edge_input(0.06, 100.0, 100.0, 29.9 * _MIN)),
        ("not_near_expiry_exactly_thirty_minutes", _edge_input(0.06, 100.0, 100.0, 30 * _MIN)),
        (
            "near_expiry_bsm_put",
            _edge_input(0.035, 100.0, 100.0, 10 * _MIN, "PE", model="bsm", q=0.01),
        ),
        ("one_minute_floor_half_a_minute_left", _edge_input(0.011, 100.0, 100.0, 0.5 * _MIN)),
        ("one_minute_floor_exactly_one_minute_left", _edge_input(0.011, 100.0, 100.0, _MIN)),
        ("one_minute_floor_one_second_left", _edge_input(0.011, 100.0, 100.0, 1 / (365 * 86400))),
        (
            "price_equal_to_discounted_intrinsic_call",
            _edge_input(call_intrinsic, 105.0, 100.0, 0.25),
        ),
        (
            "price_below_discounted_intrinsic_call",
            _edge_input(call_intrinsic - 0.5, 105.0, 100.0, 0.25),
        ),
        (
            "price_equal_to_discounted_intrinsic_put",
            _edge_input(put_intrinsic, 95.0, 100.0, 0.25, "PE"),
        ),
        (
            "price_below_discounted_intrinsic_put",
            _edge_input(put_intrinsic - 0.5, 95.0, 100.0, 0.25, "PE"),
        ),
        ("tiny_price_far_otm_call_has_an_iv", _edge_input(0.0001, 80.0, 100.0, 0.25)),
        (
            "price_below_the_cheapest_model_price_atm_has_no_iv",
            _edge_input(1e-12, 100.0, 100.0, 0.25),
        ),
        (
            "half_tick_rule_price_within_half_a_tick_of_intrinsic",
            _edge_input(call_intrinsic + 0.0249, 105.0, 100.0, 0.25, tick=0.05),
        ),
        (
            "half_tick_rule_price_just_beyond_half_a_tick",
            _edge_input(call_intrinsic + 0.0251, 105.0, 100.0, 0.25, tick=0.05),
        ),
        (
            "half_tick_rule_price_beyond_half_a_tick_is_ok",
            _edge_input(call_intrinsic + 0.05, 105.0, 100.0, 0.25, tick=0.05),
        ),
        (
            "half_tick_rule_not_applied_without_a_tick",
            _edge_input(call_intrinsic + 0.0249, 105.0, 100.0, 0.25),
        ),
        (
            "half_tick_rule_negative_tick_is_ignored",
            _edge_input(call_intrinsic + 0.0249, 105.0, 100.0, 0.25, tick=-1.0),
        ),
        (
            "half_tick_rule_put",
            _edge_input(put_intrinsic + 0.0249, 95.0, 100.0, 0.25, "PE", tick=0.05),
        ),
        (
            "half_tick_rule_bsm_uses_the_dividend_discounted_spot",
            _edge_input(
                105 * math.exp(-0.02 * 0.25) - 100 * math.exp(-0.065 * 0.25) + 0.01,
                105.0,
                100.0,
                0.25,
                model="bsm",
                q=0.02,
                tick=0.05,
            ),
        ),
        ("price_above_the_forward_has_no_iv", _edge_input(120.0, 100.0, 100.0, 0.25)),
        ("no_price_none", _edge_input(None, 100.0, 100.0, 0.25)),
        ("no_price_zero", _edge_input(0.0, 100.0, 100.0, 0.25)),
        ("no_price_negative", _edge_input(-2.0, 100.0, 100.0, 0.25)),
        ("no_price_underlying_none", _edge_input(3.0, None, 100.0, 0.25)),
        ("no_price_underlying_zero", _edge_input(3.0, 0.0, 100.0, 0.25)),
        ("no_price_underlying_negative", _edge_input(3.0, -5.0, 100.0, 0.25)),
        ("no_price_checked_before_expiry_for_the_underlying", _edge_input(3.0, None, 100.0, -1.0)),
        ("ok_atm_call", _edge_input(3.0, 100.0, 100.0, 0.25)),
        ("ok_otm_put", _edge_input(1.0, 100.0, 95.0, 0.1, "PE")),
        ("ok_bsm_call_with_yield", _edge_input(4.0, 100.0, 100.0, 0.25, model="bsm", q=0.02)),
        ("ok_negative_rate", _edge_input(3.0, 100.0, 100.0, 0.25, rate=-0.01)),
        ("ok_zero_rate", _edge_input(3.0, 100.0, 100.0, 0.25, rate=0.0)),
        ("absurd_huge_underlying_and_strike", _edge_input(1e300, 1e308, 1e308, 0.25)),
        ("absurd_tiny_strike_and_underlying", _edge_input(1e-300, 1e-290, 1e-300, 0.25)),
        ("absurd_huge_price_has_no_iv", _edge_input(1e300, 100.0, 100.0, 0.25)),
        ("absurd_very_large_years", _edge_input(50.0, 100.0, 100.0, 1e6)),
        ("absurd_huge_atm_future_with_a_normal_price", _edge_input(1e13, 1e15, 1e15, 0.25)),
        ("absurd_price_needing_a_very_high_volatility", _edge_input(39.0, 100.0, 100.0, 0.25)),
        ("absurd_micro_underlying_atm", _edge_input(1e-10, 1e-8, 1e-8, 0.25)),
        ("absurd_tiny_price_deep_otm", _edge_input(1e-300, 100.0, 1.0, 0.25)),
        (
            "absurd_deep_itm_with_a_time_value_price",
            _edge_input(1e9 + 1.0, 1e9 + 100.0, 100.0, 0.25),
        ),
        ("absurd_huge_rate", _edge_input(3.0, 100.0, 100.0, 0.25, rate=50.0)),
        ("absurd_very_negative_rate", _edge_input(3.0, 100.0, 100.0, 0.25, rate=-50.0)),
        ("absurd_strike_far_above_underlying", _edge_input(1e-9, 100.0, 1e12, 0.25)),
        ("error_zero_strike", _edge_input(3.0, 100.0, 0.0, 0.25)),
        ("error_negative_strike", _edge_input(3.0, 100.0, -100.0, 0.25)),
        ("error_invalid_option_type", _edge_input(3.0, 100.0, 100.0, 0.25, "XX")),
        ("error_invalid_model", _edge_input(3.0, 100.0, 100.0, 0.25, model="heston")),
        (
            "error_option_type_is_checked_before_a_missing_underlying",
            _edge_input(3.0, None, 100.0, 0.25, "XX"),
        ),
    ]


def _carry_cases() -> list[Any]:
    def carry(name: str, price: float, rate: float, ty_future: float, ty_option: float) -> Any:
        return (
            name,
            {
                "function": "carry_adjusted_forward",
                "future_price": price,
                "rate": rate,
                "future_years": ty_future,
                "option_years": ty_option,
            },
        )

    def fwd(
        name: str,
        exchange: str,
        option_expiry: str,
        future_expiry: str,
        now: str = "2026-11-12T10:00:00+05:30",
        price: float = 24200.0,
        rate: float = 0.065,
        dividend_yield: float | None = None,
    ) -> Any:
        case = {
            "function": "option_forward",
            "exchange": exchange,
            "now": now,
            "option_expiry": option_expiry,
            "future_expiry": future_expiry,
            "future_price": price,
            "rate": rate,
            "expiry_time": None,
        }
        if dividend_yield is not None:  # optional key: absent means 0
            case["dividend_yield"] = dividend_yield
        return name, case

    def carry_q(
        name: str, price: float, rate: float, q: float, ty_future: float, ty_option: float
    ) -> Any:
        _, case = carry(name, price, rate, ty_future, ty_option)
        return name, {**case, "dividend_yield": q}

    return [
        carry("later_future_is_discounted_back", 24200.0, 0.065, 0.2, 0.05),
        carry("same_years_gives_the_future_price", 24200.0, 0.065, 0.1, 0.1),
        carry("earlier_future_is_grown_forward", 24200.0, 0.065, 0.05, 0.2),
        carry("zero_rate_gives_the_future_price", 24200.0, 0.0, 0.5, 0.1),
        carry("negative_rate", 24200.0, -0.02, 0.5, 0.1),
        carry("one_year_apart", 100.0, 0.10, 1.0, 0.0),
        carry("high_rate_long_gap", 100.0, 0.5, 2.0, 0.1),
        fwd("nfo_later_expiry_future_carries_back", "NFO", "2026-11-26", "2026-12-31"),
        fwd("nfo_same_expiry_future_is_used_as_is", "NFO", "2026-11-26", "2026-11-26"),
        fwd("nfo_earlier_expiry_future_carries_forward", "NFO", "2026-11-26", "2026-11-19"),
        fwd("bfo_later_expiry_future_carries_back", "BFO", "2026-11-26", "2026-12-31"),
        fwd("cds_later_expiry_future_carries_back", "CDS", "2026-11-26", "2026-12-29", price=88.5),
        fwd(
            "mcx_later_expiry_future_has_no_carry", "MCX", "2026-11-19", "2026-12-19", price=6500.0
        ),
        fwd("mcx_same_expiry_future", "MCX", "2026-11-19", "2026-11-19", price=6500.0),
        fwd(
            "mcx_earlier_expiry_future_has_no_carry",
            "MCX",
            "2026-11-19",
            "2026-10-19",
            price=6500.0,
        ),
        fwd(
            "mcx_exchange_is_case_and_space_insensitive",
            " mcx ",
            "2026-11-19",
            "2026-12-19",
            price=6500.0,
        ),
        fwd("nfo_later_expiry_future_zero_rate", "NFO", "2026-11-26", "2026-12-31", rate=0.0),
        carry_q("dividend_yield_reduces_the_carry", 24200.0, 0.065, 0.013, 0.2, 0.05),
        carry_q("dividend_yield_equal_to_rate_is_no_carry", 24200.0, 0.065, 0.065, 0.2, 0.05),
        carry_q("foreign_rate_earlier_future_is_grown_forward", 88.5, 0.065, 0.045, 0.05, 0.2),
        carry_q("dividend_yield_above_rate_reverses_the_carry", 100.0, 0.02, 0.05, 0.5, 0.1),
        fwd(
            "nfo_index_option_dividend_yield_carries_back",
            "NFO",
            "2026-11-26",
            "2026-12-31",
            dividend_yield=0.013,
        ),
        fwd(
            "cds_foreign_rate_as_dividend_yield_carries_back",
            "CDS",
            "2026-11-26",
            "2026-12-29",
            price=88.5,
            dividend_yield=0.045,
        ),
        fwd(
            "nfo_dividend_yield_equal_to_rate_gives_the_future_price",
            "NFO",
            "2026-11-26",
            "2026-12-31",
            dividend_yield=0.065,
        ),
        fwd(
            "mcx_dividend_yield_has_no_effect",
            "MCX",
            "2026-11-19",
            "2026-12-19",
            price=6500.0,
            dividend_yield=0.03,
        ),
        fwd(
            "nfo_same_expiry_future_dividend_yield_has_no_effect",
            "NFO",
            "2026-11-26",
            "2026-11-26",
            dividend_yield=0.03,
        ),
    ]


def _chain(rows: Sequence[tuple[str, str, str | None, str | None]]) -> list[dict[str, Any]]:
    return [
        {"strike": s, "option_type": t, "open_interest": oi, "volume": v} for s, t, oi, v in rows
    ]


def _pcr_cases() -> list[Any]:
    basic = _chain(
        [
            ("24000", "CE", "1000", "100"),
            ("24000", "PE", "1500", "300"),
            ("24100", "CE", "2000", "200"),
            ("24100", "PE", "1500", "100"),
        ]
    )

    def case(name: str, rows: list[dict[str, Any]], by: str = "oi") -> Any:
        return (name, {"rows": rows, "by": by})

    return [
        case("by_oi", basic),
        case("by_volume", basic, "volume"),
        case("repeating_quotient", _chain([("1", "CE", "3", None), ("1", "PE", "1", None)])),
        case("no_calls_is_null", _chain([("24000", "PE", "100", "10")])),
        case(
            "calls_with_zero_oi_is_null",
            _chain([("24000", "CE", "0", "5"), ("24000", "PE", "100", "10")]),
        ),
        case("no_puts_is_zero", _chain([("24000", "CE", "100", "10")])),
        case("empty_chain_is_null", []),
        case(
            "missing_oi_counts_as_zero",
            _chain(
                [
                    ("24000", "CE", "100", None),
                    ("24000", "PE", None, None),
                    ("24100", "PE", "50", None),
                ]
            ),
        ),
        case(
            "missing_volume_counts_as_zero",
            _chain(
                [
                    ("24000", "CE", None, "10"),
                    ("24000", "PE", None, None),
                    ("24100", "PE", None, "5"),
                ]
            ),
            "volume",
        ),
        case(
            "all_oi_missing_is_null",
            _chain([("24000", "CE", None, "10"), ("24000", "PE", None, "20")]),
        ),
        case(
            "other_option_types_are_ignored",
            _chain(
                [
                    ("24000", "CE", "100", None),
                    ("24000", "PE", "100", None),
                    ("24000", "XX", "999", None),
                ]
            ),
        ),
        case(
            "lowercase_option_types_are_ignored",
            _chain([("24000", "ce", "100", None), ("24000", "pe", "100", None)]),
        ),
        case(
            "decimal_open_interest",
            _chain([("24000", "CE", "100.5", None), ("24000", "PE", "50.25", None)]),
        ),
        case("invalid_by_is_an_error", basic, "price"),
    ]


def _max_pain_cases() -> list[Any]:
    def case(name: str, rows: list[dict[str, Any]]) -> Any:
        return (name, {"rows": rows})

    return [
        case(
            "textbook_three_strikes",
            _chain(
                [
                    ("100", "CE", "50", None),
                    ("100", "PE", "10", None),
                    ("110", "CE", "30", None),
                    ("110", "PE", "40", None),
                    ("120", "CE", "10", None),
                    ("120", "PE", "60", None),
                ]
            ),
        ),
        case(
            "tie_goes_to_the_lowest_strike",
            _chain(
                [
                    ("100", "CE", "10", None),
                    ("100", "PE", "10", None),
                    ("110", "CE", "10", None),
                    ("110", "PE", "10", None),
                ]
            ),
        ),
        case(
            "zero_payout_between_the_outer_rows_ties_to_the_lowest_strike",
            _chain(
                [("100", "PE", "10", None), ("120", "CE", "10", None), ("110", "CE", "0", None)]
            ),
        ),
        case(
            "all_zero_oi_ties_to_the_lowest_strike",
            _chain([("110", "CE", "0", None), ("100", "PE", "0", None), ("120", "CE", "0", None)]),
        ),
        case(
            "all_oi_missing_is_null", _chain([("100", "CE", None, "5"), ("100", "PE", None, "5")])
        ),
        case("empty_chain_is_null", []),
        case(
            "some_oi_missing_counts_as_zero",
            _chain(
                [("100", "CE", "10", None), ("100", "PE", None, None), ("110", "PE", "10", None)]
            ),
        ),
        case("single_strike", _chain([("100", "CE", "10", None), ("100", "PE", "20", None)])),
        case(
            "calls_only",
            _chain(
                [("100", "CE", "10", None), ("110", "CE", "10", None), ("120", "CE", "10", None)]
            ),
        ),
        case(
            "puts_only",
            _chain(
                [("100", "PE", "10", None), ("110", "PE", "10", None), ("120", "PE", "10", None)]
            ),
        ),
        case(
            "other_option_types_are_ignored_but_their_strikes_are_candidates",
            _chain(
                [("100", "CE", "10", None), ("105", "XX", "999", None), ("110", "PE", "10", None)]
            ),
        ),
        case(
            "unsorted_input_rows",
            _chain(
                [
                    ("120", "PE", "60", None),
                    ("100", "CE", "50", None),
                    ("110", "CE", "30", None),
                    ("100", "PE", "10", None),
                    ("110", "PE", "40", None),
                    ("120", "CE", "10", None),
                ]
            ),
        ),
        case(
            "decimal_strikes_and_oi",
            _chain(
                [
                    ("24000.5", "CE", "100.5", None),
                    ("24100.5", "PE", "200.25", None),
                    ("24200.5", "CE", "50", None),
                ]
            ),
        ),
        case(
            "duplicate_rows_for_a_strike_add_up",
            _chain(
                [("100", "CE", "10", None), ("100", "CE", "10", None), ("110", "PE", "15", None)]
            ),
        ),
    ]


def _buildup_cases() -> list[Any]:
    def classify(name: str, price: Any, oi: Any) -> Any:
        return (
            name,
            {"function": "classify_buildup", "price_change": price, "oi_change": oi},
        )

    def snap(name: str, **snapshot: Any) -> Any:
        return (name, {"function": "buildup_from_snapshot", "snapshot": snapshot})

    return [
        classify("price_up_oi_up_is_long_buildup", 1.5, 100),
        classify("price_down_oi_up_is_short_buildup", -1.5, 100),
        classify("price_down_oi_down_is_long_unwinding", -1.5, -100),
        classify("price_up_oi_down_is_short_covering", 1.5, -100),
        classify("tiny_positive_changes", 1e-12, 1e-12),
        classify("price_change_zero_is_neutral", 0, 100),
        classify("oi_change_zero_is_neutral", 1.5, 0),
        classify("both_zero_is_neutral", 0.0, 0.0),
        classify("negative_zero_is_neutral", -0.0, 5),
        classify("price_change_null_is_neutral", None, 100),
        classify("oi_change_null_is_neutral", 1.5, None),
        classify("both_null_is_neutral", None, None),
        classify("price_change_nan_is_neutral", "NaN", 100),
        classify("oi_change_nan_is_neutral", 1.5, "NaN"),
        classify("price_change_infinity_is_neutral", "Infinity", 100),
        classify("oi_change_negative_infinity_is_neutral", 1.5, "-Infinity"),
        snap("snapshot_long_buildup", lp="105.5", c="100.0", oi="1200", poi="1000"),
        snap("snapshot_short_buildup", lp="95.5", c="100.0", oi="1200", poi="1000"),
        snap("snapshot_long_unwinding", lp="95.5", c="100.0", oi="800", poi="1000"),
        snap("snapshot_short_covering", lp="105.5", c="100.0", oi="800", poi="1000"),
        snap("snapshot_price_unchanged_is_neutral", lp="100.0", c="100.0", oi="1200", poi="1000"),
        snap("snapshot_oi_unchanged_is_neutral", lp="105.0", c="100.0", oi="1000", poi="1000"),
        snap("snapshot_missing_lp", c="100.0", oi="1200", poi="1000"),
        snap("snapshot_missing_c", lp="105.0", oi="1200", poi="1000"),
        snap("snapshot_missing_oi", lp="105.0", c="100.0", poi="1000"),
        snap("snapshot_missing_poi", lp="105.0", c="100.0", oi="1200"),
        snap("snapshot_empty"),
        snap("snapshot_null_values", lp=None, c=None, oi=None, poi=None),
        snap("snapshot_blank_strings", lp=" ", c="100", oi="", poi="1000"),
        snap("snapshot_garbage_strings", lp="abc", c="100", oi="1200", poi="1000"),
        snap("snapshot_nan_string", lp="NaN", c="100", oi="1200", poi="1000"),
        snap("snapshot_infinity_string", lp="105", c="100", oi="Infinity", poi="1000"),
        snap("snapshot_json_numbers", lp=105.5, c=100, oi=1200, poi=1000),
        snap("snapshot_booleans_are_missing", lp=True, c="100", oi="1200", poi="1000"),
        snap("snapshot_padded_strings", lp=" 105.5 ", c=" 100.0 ", oi=" 1200 ", poi=" 1000 "),
        snap("snapshot_decimal_precision", lp="100.15", c="100.10", oi="1000.5", poi="1000.25"),
        snap(
            "snapshot_extra_keys_are_ignored",
            lp="105",
            c="100",
            oi="1200",
            poi="1000",
            ts="X",
            v="9",
        ),
    ]


def _ack_cases() -> list[Any]:
    return [(f"ack_s_{name}", {"s": s}) for name, s in _ACK_VALUES]


_ACK_VALUES = (
    ("Ok", "Ok"),
    ("OK_as_sent_by_the_live_server", "OK"),
    ("ok_lowercase", "ok"),
    ("Ok_padded_with_spaces", " Ok "),
    ("oK_mixed_case", "oK"),
    ("Not_Ok", "Not_Ok"),
    ("NOT_OK", "NOT_OK"),
    ("empty", ""),
    ("Okay", "Okay"),
    ("Ok_with_a_suffix", "Ok."),
    ("space_inside", "O k"),
)


def _config_cases() -> list[Any]:
    return [
        ("defaults", {"overrides": {}, "fields": None}),
        (
            "base_url_trailing_slash_is_stripped",
            {"overrides": {"base_url": "https://x.example/api/"}, "fields": ["base_url"]},
        ),
        (
            "reconnect_max_attempts_may_be_null",
            {"overrides": {"reconnect_max_attempts": None}, "fields": ["reconnect_max_attempts"]},
        ),
        (
            "explicit_user_agent_is_kept",
            {"overrides": {"user_agent": "my-app/1"}, "fields": ["user_agent"]},
        ),
        ("base_url_needs_http_scheme", {"overrides": {"base_url": "ftp://x"}, "fields": None}),
        ("ws_url_needs_ws_scheme", {"overrides": {"ws_url": "https://x"}, "fields": None}),
        ("connect_timeout_must_be_positive", {"overrides": {"connect_timeout": 0}, "fields": None}),
        (
            "keepalive_connections_cannot_exceed_max_connections",
            {"overrides": {"max_connections": 2, "max_keepalive_connections": 3}, "fields": None},
        ),
        (
            "script_master_refresh_time_must_be_hh_mm",
            {"overrides": {"script_master_refresh_time": "7:00"}, "fields": None},
        ),
        ("max_retries_cannot_be_negative", {"overrides": {"max_retries": -1}, "fields": None}),
        (
            "rate_limit_mode_must_be_wait_or_raise",
            {"overrides": {"rate_limit_mode": "drop"}, "fields": None},
        ),
        (
            "order_rate_limit_per_second_30_is_allowed",
            {
                "overrides": {"order_rate_limit_per_second": 30, "algo_id": "153763"},
                "fields": ["order_rate_limit_per_second", "algo_id"],
            },
        ),
        (
            "order_rate_limit_per_second_11_needs_algo_id",
            {"overrides": {"order_rate_limit_per_second": 11}, "fields": None},
        ),
        (
            "order_rate_limit_per_minute_below_per_second_is_rejected",
            {
                "overrides": {"order_rate_limit_per_second": 10, "order_rate_limit_per_minute": 5},
                "fields": None,
            },
        ),
        (
            "order_rate_limit_per_minute_600_is_allowed",
            {
                "overrides": {"order_rate_limit_per_minute": 600},
                "fields": ["order_rate_limit_per_minute"],
            },
        ),
        (
            "order_rate_limit_per_second_10_needs_no_algo_id",
            {
                "overrides": {"order_rate_limit_per_second": 10},
                "fields": ["order_rate_limit_per_second", "algo_id"],
            },
        ),
        (
            "algo_id_is_kept",
            {"overrides": {"algo_id": "153763"}, "fields": ["algo_id"]},
        ),
        (
            "order_rate_limit_max_wait_zero_is_allowed",
            {
                "overrides": {"order_rate_limit_max_wait": 0},
                "fields": ["order_rate_limit_max_wait"],
            },
        ),
        (
            "stream_backlog_limit_must_be_at_least_one",
            {"overrides": {"stream_backlog_limit": 0}, "fields": None},
        ),
        (
            "instruments_dir_is_kept",
            {"overrides": {"instruments_dir": "cache/instruments"}, "fields": ["instruments_dir"]},
        ),
        (
            "instruments_dir_must_not_be_empty",
            {"overrides": {"instruments_dir": ""}, "fields": None},
        ),
        (
            "connect_timeout_nan_is_rejected",
            {"overrides": {"connect_timeout": "NaN"}, "fields": None},
        ),
        (
            "read_timeout_infinity_is_rejected",
            {"overrides": {"read_timeout": "Infinity"}, "fields": None},
        ),
        (
            "backoff_cap_negative_infinity_is_rejected",
            {"overrides": {"backoff_cap": "-Infinity"}, "fields": None},
        ),
        (
            "order_rate_limit_max_wait_nan_is_rejected",
            {"overrides": {"order_rate_limit_max_wait": "NaN"}, "fields": None},
        ),
        (
            "order_rate_limit_max_wait_infinity_is_rejected",
            {"overrides": {"order_rate_limit_max_wait": "Infinity"}, "fields": None},
        ),
        *[
            (f"{name}_is_rejected", {"overrides": {key: value}, "fields": None})
            for name, key, value in (
                ("max_retries_nan", "max_retries", "NaN"),
                ("max_retries_float", "max_retries", 2.0),
                ("max_retries_boolean", "max_retries", True),
                ("stream_backlog_limit_nan", "stream_backlog_limit", "NaN"),
                ("stream_backlog_limit_float", "stream_backlog_limit", 1000.5),
                ("reconnect_max_attempts_nan", "reconnect_max_attempts", "NaN"),
                ("reconnect_max_attempts_boolean", "reconnect_max_attempts", False),
                ("reconnect_max_attempts_negative", "reconnect_max_attempts", -1),
                ("max_connections_float", "max_connections", 10.0),
                ("max_keepalive_connections_zero", "max_keepalive_connections", 0),
                ("script_master_recheck_minutes_float", "script_master_recheck_minutes", 15.0),
                ("connect_timeout_string", "connect_timeout", "5"),
                ("connect_timeout_boolean", "connect_timeout", True),
                ("read_timeout_null", "read_timeout", None),
                ("order_rate_limit_max_wait_string", "order_rate_limit_max_wait", "1"),
                ("order_rate_limit_per_second_zero", "order_rate_limit_per_second", 0),
                ("order_rate_limit_per_second_over_30", "order_rate_limit_per_second", 31),
                ("order_rate_limit_per_second_float", "order_rate_limit_per_second", 10.0),
                ("order_rate_limit_per_second_boolean", "order_rate_limit_per_second", True),
                ("order_rate_limit_per_second_string", "order_rate_limit_per_second", "10"),
                ("order_rate_limit_per_minute_zero", "order_rate_limit_per_minute", 0),
                ("order_rate_limit_per_minute_over_600", "order_rate_limit_per_minute", 601),
                ("order_rate_limit_per_minute_float", "order_rate_limit_per_minute", 2.5),
                ("order_rate_limit_per_minute_boolean", "order_rate_limit_per_minute", True),
                ("order_rate_limit_per_minute_string", "order_rate_limit_per_minute", "200"),
                ("log_payloads_string", "log_payloads", "yes"),
                ("check_token_expiry_integer", "check_token_expiry", 1),
                ("base_url_not_a_string", "base_url", 42),
                ("user_agent_control_character", "user_agent", "my-app/1\r\nX-Evil: 1"),
                ("user_agent_non_ascii", "user_agent", "my-app/1 é"),
                ("instruments_dir_blank", "instruments_dir", "   "),
                ("algo_id_empty", "algo_id", ""),
                ("algo_id_blank", "algo_id", "   "),
                ("algo_id_boolean", "algo_id", True),
                ("algo_id_integer", "algo_id", 153763),
            )
        ],
        (
            "connect_timeout_integer_is_accepted",
            {"overrides": {"connect_timeout": 3}, "fields": ["connect_timeout"]},
        ),
        (
            "reconnect_max_attempts_zero_is_accepted",
            {"overrides": {"reconnect_max_attempts": 0}, "fields": ["reconnect_max_attempts"]},
        ),
    ]


# ----------------------------------------------------------------------------- request-side vectors


def eval_jdata(i: dict[str, Any]) -> dict[str, Any]:
    body = encode_jdata(dict(i["payload"]))
    return {"body": body, "utf8_hex": body.encode("utf-8").hex()}


def _jdata_cases() -> list[Any]:
    def case(name: str, *pairs: tuple[str, str]) -> tuple[str, dict[str, Any]]:
        return name, {"payload": [list(p) for p in pairs]}

    return [
        case("plain_values", ("uid", "TEST123"), ("exch", "NSE"), ("token", "22")),
        case("key_order_is_kept_not_sorted", ("uid", "TEST123"), ("actid", "TEST123")),
        case("empty_payload"),
        case("empty_value", ("uid", "TEST123"), ("remarks", "")),
        case("ampersand_is_percent_encoded", ("remarks", "a&b")),
        case("plus_is_percent_encoded", ("remarks", "a+b")),
        case("percent_is_encoded_first", ("remarks", "100%")),
        case("literal_percent_escape_is_encoded_again", ("remarks", "%26")),
        case("equals_is_sent_as_is", ("remarks", "a=b")),
        case("space_and_slash_are_sent_as_is", ("tsym", "M&M FIN/X")),
        case("double_quote_is_json_escaped", ("remarks", 'say "hi"')),
        case("backslash_is_json_escaped", ("remarks", "a\\b")),
        case("newline_and_tab_are_json_escaped", ("remarks", "a\nb\tc")),
        case("carriage_return_is_short_escaped", ("remarks", "a\rb")),
        case("backspace_is_short_escaped", ("remarks", "a\bb")),
        case("form_feed_is_short_escaped", ("remarks", "a\fb")),
        case("other_control_character_is_lowercase_u_escape", ("remarks", "a\x1fb\x01c")),
        case("delete_character_is_sent_raw", ("remarks", "a\x7fb")),
        case("line_separator_u2028_is_sent_raw", ("remarks", "a b c")),
        case("html_characters_are_not_escaped", ("remarks", "<b>&'x'</b>")),
        case("non_ascii_is_sent_as_utf8_not_escaped", ("remarks", "₹ नमस्ते")),
        case("astral_character_is_sent_as_utf8", ("remarks", "😀")),
        case("all_specials_together", ("remarks", "%&+=?#")),
    ]


def eval_token_expiry(i: dict[str, Any]) -> dict[str, Any]:
    if i["function"] == "is_expired":
        token = AccessToken.model_validate(
            {
                "access_token": "synthetic-token",
                "issued_at": datetime.fromisoformat(i["issued_at"]),
                "expires_at": datetime.fromisoformat(i["expires_at"]),
            }
        )
        return {"expired": token.is_expired(datetime.fromisoformat(i["now"]))}
    try:
        expiry = next_expiry(datetime.fromisoformat(i["issued_at"]))
    except Exception as exc:
        return _error(exc)
    return {"expires_at": expiry.isoformat()}


def _token_expiry_cases() -> list[Any]:
    expiry = {
        "function": "is_expired",
        "issued_at": "2026-11-10T09:15:00+05:30",
        "expires_at": "2026-11-11T06:00:00+05:30",
    }
    boundary = [
        (
            "is_expired_one_microsecond_before",
            {**expiry, "now": "2026-11-11T05:59:59.999999+05:30"},
        ),
        ("is_expired_exactly_at_expires_at", {**expiry, "now": "2026-11-11T06:00:00+05:30"}),
        ("is_expired_exactly_at_expires_at_in_utc", {**expiry, "now": "2026-11-11T00:30:00+00:00"}),
        ("is_expired_after_expires_at", {**expiry, "now": "2026-11-11T09:00:00+05:30"}),
    ]
    return [
        (name, {"function": "next_expiry", "issued_at": issued})
        for name, issued in (
            ("one_second_before_06_00_ist", "2026-11-10T05:59:59+05:30"),
            ("issued_exactly_at_06_00_expires_next_day", "2026-11-10T06:00:00+05:30"),
            ("one_second_after_06_00_ist", "2026-11-10T06:00:01+05:30"),
            ("midday", "2026-11-10T12:30:00+05:30"),
            ("just_before_midnight", "2026-11-10T23:59:59+05:30"),
            ("midnight", "2026-11-10T00:00:00+05:30"),
            ("utc_input_before_00_30z", "2026-11-10T00:29:59+00:00"),
            ("utc_input_at_00_30z", "2026-11-10T00:30:00+00:00"),
            ("utc_input_previous_evening", "2026-11-09T20:00:00+00:00"),
            ("other_offset_minus_05_00", "2026-11-09T19:30:00-05:00"),
            ("month_end", "2026-11-30T18:00:00+05:30"),
            ("year_end", "2026-12-31T06:00:00+05:30"),
            ("leap_day", "2028-02-28T07:00:00+05:30"),
            ("microseconds_before_06_00", "2026-11-10T05:59:59.999999+05:30"),
            ("naive_datetime_is_an_error", "2026-11-10T05:00:00"),
        )
    ] + boundary


def eval_token_file(i: dict[str, Any]) -> dict[str, Any]:
    """Write a login with the SDK's writer, or read a file another SDK may have written."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "token.json"
        if i["function"] == "write":
            issued = datetime.fromisoformat(i["issued_at"])
            token = AccessToken.model_validate(
                {
                    "access_token": "synthetic-token",
                    "issued_at": issued,
                    "expires_at": next_expiry(issued),
                }
            )
            _token_store.save("TEST123", token, path)
            data = json.loads(path.read_text(encoding="utf-8"))
            return {"issued_at": data["issued_at"], "expires_at": data["expires_at"]}
        content = {
            "client_id": "TEST123",
            "access_token": "synthetic-token",
            "issued_at": i["issued_at"],
            "expires_at": i["expires_at"],
        }
        path.write_text(json.dumps(content), encoding="utf-8")
        os.chmod(path, 0o600)
        try:
            saved = _token_store.load(path)
        except _token_store.TokenFileError:
            return {"error": "invalid"}
        assert saved is not None
        return {
            "issued_at": saved.token.issued_at.astimezone(IST).isoformat(),
            "expires_at": saved.token.expires_at.astimezone(IST).isoformat(),
        }


def _token_file_cases() -> list[Any]:
    def read(name: str, issued: str, expires: str) -> tuple[str, dict[str, Any]]:
        return name, {"function": "read", "issued_at": issued, "expires_at": expires}

    def write(name: str, issued: str) -> tuple[str, dict[str, Any]]:
        return name, {"function": "write", "issued_at": issued}

    exp = "2026-11-11T06:00:00+05:30"
    return [
        write("write_seconds_precision_numeric_offset", "2026-11-10T09:15:30.123456+05:30"),
        write("write_utc_instant_keeps_its_offset", "2026-11-10T03:45:30+00:00"),
        read("read_numeric_offset_as_written", "2026-11-10T09:15:30+05:30", exp),
        read("read_z_suffix", "2026-11-10T03:45:30Z", "2026-11-11T00:30:00Z"),
        read("read_lower_case_z_suffix", "2026-11-10T03:45:30z", exp),
        read("read_three_fraction_digits", "2026-11-10T03:45:30.123Z", exp),
        read("read_nine_fraction_digits_truncated", "2026-11-10T03:45:30.123456789Z", exp),
        read("read_one_fraction_digit", "2026-11-10T03:45:30.1Z", exp),
        read("read_other_offset", "2026-11-09T22:15:30-05:00", exp),
        read(
            "read_edited_expiry_is_capped", "2026-11-10T09:15:30+05:30", "2026-11-20T06:00:00+05:30"
        ),
        read("read_naive_is_refused", "2026-11-10T09:15:30", exp),
        read("read_date_only_is_refused", "2026-11-10", exp),
        read("read_not_a_timestamp_is_refused", "yesterday", exp),
        read("read_blank_is_refused", " ", exp),
        read("read_offset_without_colon_is_refused", "2026-11-10T09:15:30+0530", exp),
        read("read_basic_format_is_refused", "20261110T091530+05:30", exp),
        read("read_space_separator_is_refused", "2026-11-10 09:15:30+05:30", exp),
        read("read_ten_fraction_digits_is_refused", "2026-11-10T03:45:30.1234567890Z", exp),
        read("read_no_seconds_is_refused", "2026-11-10T09:15+05:30", exp),
        read("read_padded_is_accepted", " 2026-11-10T09:15:30+05:30\n", f"\t{exp} "),
        read("read_lower_case_t_is_refused", "2026-11-10t09:15:30+05:30", exp),
        read(
            "read_issued_at_in_the_future_is_refused",
            "2099-11-10T09:15:30+05:30",
            "2099-11-11T06:00:00+05:30",
        ),
    ]


def eval_scrip_keys(i: dict[str, Any]) -> dict[str, Any]:
    raw = i["instruments"]
    if isinstance(raw, list):
        items: Any = [tuple(x) if isinstance(x, list) else x for x in raw]
    else:
        items = raw
    try:
        return {"keys": format_keys(items)}
    except Exception as exc:
        return _error(exc)


def _scrip_keys_cases() -> list[Any]:
    return [
        (name, {"instruments": value})
        for name, value in (
            ("single_string", "NSE|22"),
            ("list_of_strings", ["NSE|22", "NFO|43650"]),
            ("exchange_token_pairs", [["NSE", "22"], ["MCX", "432294"]]),
            ("mixed_forms", ["NSE|22", ["BSE", "500325"]]),
            ("lower_case_exchange_is_upper_cased", ["nse|22", ["mcx", "1"]]),
            ("padded_exchange_is_trimmed", [" Nse |22"]),
            ("duplicates_removed_first_kept", ["NSE|22", "nse|22", "NSE|23", ["NSE", "22"]]),
            ("order_is_kept", ["NSE|3", "NSE|1", "NSE|2"]),
            ("empty_list", []),
            ("unknown_exchange", ["XYZ|22"]),
            ("missing_separator", ["NSE22"]),
            ("empty_token", ["NSE|"]),
            ("token_with_space", ["NSE|2 2"]),
            ("token_with_hash", ["NSE|22#23"]),
            ("token_with_second_pipe", ["NSE|22|23"]),
            ("pair_with_non_string_token", [["NSE", 22]]),
        )
    ]


def eval_time(i: dict[str, Any]) -> dict[str, Any]:
    function = i["function"]
    try:
        if function == "to_epoch":
            value = i["value"]
            arg: Any
            if "datetime" in value:
                arg = datetime.fromisoformat(value["datetime"])
            elif "date" in value:
                arg = date.fromisoformat(value["date"])
            else:
                arg = value["epoch"]
            return {"epoch": to_epoch(arg)}
        parser = _parse_ist if function == "parse_ist" else _parse_epoch
        parsed = parser(i["value"])
    except Exception as exc:
        return _error(exc)
    return {"datetime": None if parsed is None else parsed.isoformat()}


def _time_cases() -> list[Any]:
    def to_epoch_case(name: str, value: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return f"to_epoch_{name}", {"function": "to_epoch", "value": value}

    def parse(function: str, name: str, value: Any) -> tuple[str, dict[str, Any]]:
        return f"{function}_{name}", {"function": function, "value": value}

    return [
        to_epoch_case("ist_datetime", {"datetime": "2026-11-10T09:15:00+05:30"}),
        to_epoch_case("utc_datetime", {"datetime": "2026-11-10T03:45:00+00:00"}),
        to_epoch_case("fraction_is_truncated", {"datetime": "2026-11-10T09:15:00.900+05:30"}),
        to_epoch_case("date_is_00_00_ist", {"date": "2026-11-10"}),
        to_epoch_case("epoch_int_passes_through", {"epoch": 1794282300}),
        to_epoch_case("naive_datetime_is_an_error", {"datetime": "2026-11-10T09:15:00"}),
        parse("parse_ist", "time_first_quote_format", "09:15:00 10-11-2026"),
        parse("parse_ist", "date_first_order_format", "10-11-2026 09:15:00"),
        parse("parse_ist", "slash_date_format", "10/11/2026 09:15:00"),
        parse("parse_ist", "date_only", "10-11-2026"),
        parse("parse_ist", "month_name_upper_case", "26-NOV-2026"),
        parse("parse_ist", "month_name_title_case", "26-Nov-2026"),
        parse("parse_ist", "padded_text_is_trimmed", "  10-11-2026 09:15:00 "),
        parse("parse_ist", "unknown_format_is_null", "2026-11-10T09:15:00"),
        parse("parse_ist", "empty_is_null", ""),
        parse("parse_ist", "null_is_null", None),
        parse("parse_ist", "number_is_null", 1794282300),
        parse("parse_epoch", "string_seconds", "1794282300"),
        parse("parse_epoch", "number_seconds", 1794282300),
        parse("parse_epoch", "decimal_seconds_truncated", "1794282300.9"),
        parse("parse_epoch", "padded_string", " 1794282300 "),
        parse("parse_epoch", "zero_is_null", "0"),
        parse("parse_epoch", "empty_is_null", ""),
        parse("parse_epoch", "null_is_null", None),
        parse("parse_epoch", "text_is_an_error", "abc"),
    ]


def eval_round_to_tick(i: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"result": str(round_to_tick(i["price"], i["tick_size"]))}
    except Exception as exc:
        return _error(exc)


def _round_to_tick_cases() -> list[Any]:
    return [
        (name, {"price": price, "tick_size": tick})
        for name, price, tick in (
            ("already_on_tick", "100.05", "0.05"),
            ("rounds_down", "100.02", "0.05"),
            ("rounds_up", "100.03", "0.05"),
            ("half_way_rounds_up", "100.025", "0.05"),
            ("result_has_the_tick_places", "100", "0.05"),
            ("tick_trailing_zero_ignored", "100.04", "0.10"),
            ("whole_number_tick", "1234.5", "1"),
            ("tick_of_five", "1232.5", "5"),
            ("currency_tick", "83.12345", "0.0025"),
            ("negative_price_half_way_rounds_away_from_zero", "-100.025", "0.05"),
            ("zero_price", "0", "0.05"),
            ("json_number_inputs", 1586.03445, 0.05),
            ("integer_tick_number", 99.5, 1),
            ("padded_strings", " 100.03 ", " 0.05 "),
            ("zero_tick_is_an_error", "100", "0"),
            ("negative_tick_is_an_error", "100", "-0.05"),
            ("non_numeric_price_is_an_error", "abc", "0.05"),
            ("blank_price_is_an_error", "", "0.05"),
            ("nan_price_is_an_error", "NaN", "0.05"),
            ("infinite_tick_is_an_error", "100", "Infinity"),
        )
    ]


def eval_slice_plan(i: dict[str, Any]) -> Any:
    try:
        return list(
            plan_slices(
                i["exchange"],
                i["quantity"],
                i["lot_size"],
                i["freeze_quantity"],
                max_slice_quantity=i["max_slice_quantity"],
                max_slices=i["max_slices"],
            )
        )
    except Exception as exc:
        return _error(exc)


def _slice_plan_cases() -> list[Any]:
    def case(
        name: str,
        exchange: str,
        quantity: Any,
        lot_size: Any,
        freeze_quantity: Any,
        max_slice_quantity: Any = None,
        max_slices: Any = 25,
    ) -> tuple[str, dict[str, Any]]:
        return (
            name,
            {
                "exchange": exchange,
                "quantity": quantity,
                "lot_size": lot_size,
                "freeze_quantity": freeze_quantity,
                "max_slice_quantity": max_slice_quantity,
                "max_slices": max_slices,
            },
        )

    full_nifty = 1755  # 27 lots of 65: the largest NIFTY slice under frzqty 1801
    return [
        # Live production values (2026-11-12): NIFTY lot 65 / frzqty 1801, SENSEX option lot 20 /
        # 1000, SENSEX future lot 20 / 500, MCX CRUDEOIL lot 100 / 2500, GOLD lot 1 / 1, ITC lot 1 /
        # 350854. MCX quantity is in trade units like NFO (F21): a whole multiple of the lot size.
        case("nifty_two_full_slices_and_a_remainder", "NFO", 3965, 65, 1801),
        case("nifty_exactly_one_full_slice", "NFO", full_nifty, 65, 1801),
        # A quantity below the largest slice goes out as one slice, never refused (owner).
        case("nifty_one_lot_is_one_slice", "NFO", 65, 65, 1801),
        case("nifty_two_lots_is_one_slice", "NFO", 130, 65, 1801),
        case("sensex_option_one_lot_is_one_slice", "BFO", 20, 20, 1000),
        case("mcx_three_lots_is_one_slice", "MCX", 300, 100, 2500),
        case("below_max_slice_quantity_is_one_slice", "NFO", 130, 65, 1801, 650),
        case("sensex_option_slice_equals_the_freeze_quantity", "BFO", 2000, 20, 1000),
        case("sensex_future", "BFO", 600, 20, 500),
        case("mcx_crudeoil_in_units", "MCX", 6000, 100, 2500),
        case("mcx_gold_one_lot_is_one_slice", "MCX", 1, 1, 1),
        case("mcx_gold_three_lots_one_lot_a_slice", "MCX", 3, 1, 1),
        case("mcx_max_slice_quantity_in_units", "MCX", 1000, 100, 2500, 400),
        case("mcx_quantity_not_a_whole_number_of_lots_is_an_error", "MCX", 150, 100, 2500),
        case(
            "mcx_max_slice_quantity_not_a_whole_number_of_lots_is_an_error",
            "MCX",
            1000,
            100,
            2500,
            7,
        ),
        case("itc_cash_one_share", "NSE", 1, 1, 350854),
        case("exchange_in_any_case", "nfo", full_nifty, 65, 1801),
        case("max_slice_quantity_lowers_the_slice", "NFO", full_nifty, 65, 1801, 650),
        case("max_slice_quantity_above_the_freeze_limit_is_ignored", "NFO", 1820, 65, 1801, 1820),
        case("freeze_unknown_with_max_slice_quantity", "NFO", 260, 65, None, 130),
        case("freeze_zero_with_max_slice_quantity", "NFO", 325, 65, 0, 130),
        case("freeze_negative_with_max_slice_quantity", "NFO", 325, 65, -5, 130),
        case("exactly_max_slices", "NFO", 25 * full_nifty, 65, 1801),
        case("max_slices_raised", "NFO", 26 * full_nifty, 65, 1801, None, 26),
        case("quantity_zero_is_an_error", "NFO", 0, 65, 1801),
        case("quantity_negative_is_an_error", "NFO", -65, 65, 1801),
        case("quantity_fraction_is_an_error", "NFO", 1.5, 65, 1801),
        case("quantity_boolean_is_an_error", "NFO", True, 65, 1801),
        case("quantity_not_a_whole_number_of_lots_is_an_error", "NFO", 100, 65, 1801),
        case("nifty_4000_is_not_a_whole_number_of_lots", "NFO", 4000, 65, 1801),
        case("freeze_unknown_without_max_slice_quantity_is_an_error", "NFO", 65, 65, None),
        case("freeze_zero_without_max_slice_quantity_is_an_error", "NFO", 65, 65, 0),
        case("freeze_negative_without_max_slice_quantity_is_an_error", "NFO", 65, 65, -5),
        case("freeze_below_one_lot_is_an_error", "NFO", 65, 65, 50),
        case("mcx_freeze_below_one_lot_is_an_error", "MCX", 100, 100, 50),
        case(
            "max_slice_quantity_not_a_whole_number_of_lots_is_an_error",
            "NFO",
            full_nifty,
            65,
            1801,
            700,
        ),
        case("max_slice_quantity_zero_is_an_error", "NFO", 65, 65, 1801, 0),
        case("lot_size_zero_is_an_error", "NFO", 65, 0, 1801),
        case("more_slices_than_the_default_25_is_an_error", "NFO", 26 * full_nifty, 65, 1801),
        case("max_slices_zero_is_an_error", "NFO", 65, 65, 1801, None, 0),
        case("max_slices_boolean_is_an_error", "NFO", 65, 65, 1801, None, True),
    ]


_RETRY_ERRORS: dict[str, Any] = {
    "NetworkError": lambda: NetworkError("x"),
    "RequestTimeoutError": lambda: RequestTimeoutError("x"),
    "RateLimitError": lambda: RateLimitError("x", endpoint="E", http_status=429),
    "ServerError": lambda: ServerError("x", endpoint="E", http_status=500),
    "AuthenticationError": lambda: AuthenticationError("x", endpoint="E", http_status=401),
    "NotFoundError": lambda: NotFoundError("x", endpoint="E", http_status=404),
    "APIError": lambda: APIError("x", endpoint="E", http_status=200),
    "ResponseParseError": lambda: ResponseParseError("x", endpoint="E", http_status=200, body=""),
}


def eval_retry(i: dict[str, Any]) -> dict[str, Any]:
    function = i["function"]
    if function == "delay":

        def policy(value: float) -> RetryPolicy:
            return RetryPolicy(max_retries=2, base=i["base"], cap=i["cap"], rng=lambda: value)

        retry_after = _non_finite(i["retry_after"])
        low = policy(0.0).delay(i["attempt"], retry_after)
        high = policy(1.0).delay(i["attempt"], retry_after)
        return {"min": low, "max": high}
    if function == "should_retry":
        retry_policy = RetryPolicy(max_retries=i["max_retries"], base=0.5, cap=8.0)
        exc = _RETRY_ERRORS[i["error"]]()
        retry = retry_policy.should_retry(i["attempt"], exc, state_changing=i["state_changing"])
        return {"retry": retry}
    exc = RateLimitError("x", endpoint="E", http_status=i["http_status"])
    headers = {} if i["header"] is None else {"Retry-After": i["header"]}
    apply_retry_after(exc, httpx.Response(i["http_status"], headers=headers))
    return {"retry_after": exc.retry_after}


def _retry_cases() -> list[Any]:
    defaults = ClientConfig()
    base, cap = defaults.backoff_base, defaults.backoff_cap
    cases: list[Any] = []
    for attempt in range(6):
        cases.append(
            (
                f"delay_attempt_{attempt}_default_backoff",
                {
                    "function": "delay",
                    "attempt": attempt,
                    "base": base,
                    "cap": cap,
                    "retry_after": None,
                },
            )
        )
    for name, attempt, retry_after in (
        ("delay_retry_after_is_used_without_jitter", 0, 2.0),
        ("delay_retry_after_zero", 1, 0.0),
        ("delay_retry_after_fraction", 0, 0.25),
        ("delay_retry_after_is_capped", 0, 120.0),
        ("delay_retry_after_negative_is_zero", 0, -5),
        ("delay_retry_after_nan_is_ignored", 0, "NaN"),
        ("delay_retry_after_infinity_is_ignored", 0, "Infinity"),
    ):
        cases.append(
            (
                name,
                {
                    "function": "delay",
                    "attempt": attempt,
                    "base": base,
                    "cap": cap,
                    "retry_after": retry_after,
                },
            )
        )
    cases.append(
        (
            "delay_custom_base_and_cap",
            {"function": "delay", "attempt": 3, "base": 1.0, "cap": 5.0, "retry_after": None},
        )
    )
    for error in sorted(_RETRY_ERRORS):
        cases.append(
            (
                f"should_retry_read_{error}",
                {
                    "function": "should_retry",
                    "attempt": 0,
                    "max_retries": defaults.max_retries,
                    "error": error,
                    "state_changing": False,
                },
            )
        )
    for name, attempt, max_retries, state_changing in (
        ("should_retry_state_changing_never", 0, 2, True),
        ("should_retry_last_allowed_attempt", 1, 2, False),
        ("should_retry_attempts_exhausted", 2, 2, False),
        ("should_retry_max_retries_zero", 0, 0, False),
    ):
        cases.append(
            (
                name,
                {
                    "function": "should_retry",
                    "attempt": attempt,
                    "max_retries": max_retries,
                    "error": "NetworkError",
                    "state_changing": state_changing,
                },
            )
        )
    for name, status, header in (
        ("retry_after_header_seconds", 429, "3"),
        ("retry_after_header_fraction", 429, "1.5"),
        ("retry_after_header_padded", 429, " 2 "),
        ("retry_after_header_http_date_is_ignored", 429, "Wed, 21 Oct 2026 07:28:00 GMT"),
        ("retry_after_header_missing", 429, None),
        ("retry_after_header_ignored_without_429", 200, "3"),
        ("retry_after_header_negative_is_kept", 429, "-5"),
        ("retry_after_header_nan_is_ignored", 429, "nan"),
        ("retry_after_header_inf_is_ignored", 429, "inf"),
    ):
        cases.append(
            (
                name,
                {"function": "retry_after_header", "http_status": status, "header": header},
            )
        )
    return cases


_ORDER_CALLS: dict[str, Any] = {
    "place_order": place_order_call,
    "modify_order": modify_order_call,
    "cancel_order": lambda uid, **a: cancel_order_call(uid, a["order_id"]),
    "exit_order": lambda uid, **a: exit_sno_order_call(uid, a["order_id"], a["product"]),
    "order_margin": order_margin_call,
    "place_gtt": place_gtt_call,
    "modify_gtt": modify_gtt_call,
    "cancel_gtt": lambda uid, **a: cancel_gtt_call(uid, a["alert_id"]),
}


def _validation_error(exc: BaseException) -> dict[str, Any]:
    """The error type; for ``RequestValidationError`` also the wire keys it names.

    ``fields`` lists the wire keys of the field-level entries of ``errors`` (sorted); a
    cross-field rule (for example "MTF is not offered for MCX") has no field, so a failure
    of such a rule alone gives an empty list.
    """
    out = _error(exc)
    if isinstance(exc, RequestValidationError):
        names = {".".join(str(p) for p in entry.get("loc", ())) for entry in exc.errors}
        out["fields"] = sorted(names - {""})
    return out


def eval_order_validation(i: dict[str, Any]) -> dict[str, Any]:
    try:
        call = _ORDER_CALLS[i["call"]]("TEST123", **i["arguments"])
    except Exception as exc:
        return _validation_error(exc)
    return {"endpoint": call.spec.name, "body": encode_jdata(call.payload)}


_LIMIT_BUY = {
    "exchange": "NSE",
    "tradingsymbol": "ACC-EQ",
    "quantity": 1,
    "transaction_type": "BUY",
    "product": "CNC",
    "order_type": "LIMIT",
    "price": "2000.05",
}


_MODIFY = {
    "order_id": "26111000000001",
    "exchange": "NSE",
    "tradingsymbol": "ACC-EQ",
    "quantity": 1,
    "price": "2001.00",
    "order_type": "LIMIT",
    "product": "CNC",
}
_MARGIN = dict(_LIMIT_BUY)


# The documented Place GTT Order example (03-gtt-orders.md), in SDK argument names.
_GTT = {
    "exchange": "NSE",
    "tradingsymbol": "INFY-EQ",
    "alert_type": "LTP_B_O",
    "trigger_value": "1500",
    "alert_validity": "GTT",
    "tag": "Buy INFY below 1500",
    "transaction_type": "B",
    "order_type": "LMT",
    "product": "C",
    "validity": "DAY",
    "quantity": 1,
    "price": "1500",
}
_ALERT_ID = "26111200000010"


def _gtt_validation_cases() -> list[Any]:
    def gtt(name: str, call: str = "place_gtt", **changes: Any) -> tuple[str, dict[str, Any]]:
        base = {**_GTT, "alert_id": _ALERT_ID} if call == "modify_gtt" else _GTT
        arguments = {k: v for k, v in {**base, **changes}.items() if v is not None}
        return f"gtt_{name}", {"call": call, "arguments": arguments}

    return [
        gtt("place_documented_example"),
        gtt("place_defaults_validity_day_alert_validity_gtt", validity=None, alert_validity=None),
        gtt("place_alert_validity_day", alert_validity="DAY"),
        gtt("place_words_any_case", exchange="nse", transaction_type="buy", product="cnc"),
        gtt("place_alert_type_member_name", alert_type="ltp_above"),
        gtt("place_52_week_high_alert_type", alert_type="LTP_A_52HIGH_O"),
        gtt("place_volume_alert_type", alert_type="VOLUME_A_O", trigger_value="100000"),
        gtt("place_negative_trigger_value", alert_type="CH_PER_B_O", trigger_value="-5"),
        gtt("place_json_number_trigger_value", trigger_value=1500.5),
        gtt("place_market_order_price_defaults_to_0", order_type="MARKET", price=None),
        gtt(
            "place_stop_loss_trigger_price_and_disclosed_quantity",
            order_type="SL-LMT",
            trigger_price="1499.5",
            quantity=10,
            disclosed_quantity=1,
        ),
        gtt("place_nrml_on_nfo", exchange="NFO", tradingsymbol="NIFTY25NOV26FUT", product="NRML"),
        gtt("place_cover_order_without_book_loss", product="CO"),
        gtt(
            "place_mcx_quantity_in_trade_units",  # CRUDEOIL lot 100: 2 lots = 200 units
            exchange="MCX",
            tradingsymbol="CRUDEOIL",
            quantity=200,
        ),
        gtt("place_bfo", exchange="BFO", tradingsymbol="SENSEX25NOV26FUT", product="M"),
        gtt("modify_al_id_third", "modify_gtt", trigger_value="1550"),
        gtt("cancel", "cancel_gtt", **{k: None for k in _GTT}, alert_id=_ALERT_ID),
        gtt("error_place_unknown_alert_type", alert_type="LTP_X_O"),
        gtt("error_place_alert_validity_eos", alert_validity="EOS"),
        gtt("error_place_validity_gtt_is_not_a_retention", validity="GTT"),
        gtt("error_place_cds", exchange="CDS"),
        gtt("error_place_bcd", exchange="BCD"),
        gtt("error_place_mis", product="MIS"),
        gtt("error_place_bracket", product="BO"),
        gtt("error_place_mtf", product="MTF"),
        gtt("error_place_blank_tag", tag="  "),
        gtt("error_place_integer_tag", tag=5),
        gtt("error_place_boolean_trigger_value", trigger_value=True),
        gtt("error_place_nan_trigger_value", trigger_value="nan"),
        gtt("error_place_text_trigger_value", trigger_value="abc"),
        gtt("error_place_limit_without_price", price=None),
        gtt("error_place_stop_loss_without_trigger", order_type="SL-LMT"),
        gtt("error_place_trigger_on_limit_order", trigger_price="1499"),
        gtt("error_place_zero_quantity", quantity=0),
        gtt("error_place_disclosed_above_quantity", quantity=5, disclosed_quantity=6),
        gtt("error_modify_integer_alert_id", "modify_gtt", alert_id=26111200000010),
        gtt("error_modify_blank_alert_id", "modify_gtt", alert_id=" "),
        gtt("error_modify_mis", "modify_gtt", product="MIS"),
        gtt("error_cancel_blank_alert_id", "cancel_gtt", **{k: None for k in _GTT}, alert_id=""),
        gtt(
            "error_cancel_integer_alert_id",
            "cancel_gtt",
            **{k: None for k in _GTT},
            alert_id=26111200000010,
        ),
    ]


def _order_validation_cases() -> list[Any]:
    def case(name: str, **changes: Any) -> tuple[str, dict[str, Any]]:
        arguments = {k: v for k, v in {**_LIMIT_BUY, **changes}.items() if v is not None}
        return name, {"call": "place_order", "arguments": arguments}

    def other(name: str, call: str, base: dict[str, Any], **changes: Any) -> Any:
        # null is kept: modify_order and order_margin take price as a required argument.
        return name, {"call": call, "arguments": {**base, **changes}}

    order_id = {"order_id": "26111000000001"}
    return [
        other("modify_limit", "modify_order", _MODIFY),
        other(
            "modify_market_price_0_no_amo", "modify_order", _MODIFY, order_type="MARKET", price=None
        ),
        other(
            "modify_validity_and_trigger",
            "modify_order",
            _MODIFY,
            order_type="SL",
            trigger_price="2000.50",
            validity="IOC",
            tag="my-tag",
        ),
        other(
            "modify_words_and_wire_codes",
            "modify_order",
            _MODIFY,
            order_type="LMT",
            product="i",
            exchange="nse",
        ),
        other("modify_algo_id_sent_last", "modify_order", _MODIFY, tag="t", algo_id="153763"),
        other("error_modify_blank_algo_id", "modify_order", _MODIFY, algo_id="  "),
        other("error_modify_without_order_id", "modify_order", _MODIFY, order_id=""),
        other("error_modify_limit_without_price", "modify_order", _MODIFY, price=None),
        other("cancel", "cancel_order", order_id),
        other("error_cancel_without_order_id", "cancel_order", {"order_id": ""}),
        other("exit_bracket", "exit_order", {**order_id, "product": "BO"}),
        other("exit_cover_wire_code", "exit_order", {**order_id, "product": "H"}),
        other("error_exit_with_cnc", "exit_order", {**order_id, "product": "CNC"}),
        other("order_margin_limit", "order_margin", _MARGIN),
        other("order_margin_market", "order_margin", _MARGIN, order_type="MARKET", price="0"),
        other(
            "order_margin_stop_loss",
            "order_margin",
            _MARGIN,
            order_type="SL",
            price="2000",
            trigger_price="1999.5",
        ),
        other("error_order_margin_zero_quantity", "order_margin", _MARGIN, quantity=0),
        case("limit_buy_minimal"),
        case("words_any_case", exchange="nse", transaction_type="buy", product="cnc"),
        case("wire_codes", transaction_type="B", product="C", order_type="LMT"),
        case("sell_intraday_ioc", transaction_type="SELL", product="MIS", validity="IOC"),
        case("market_order_price_defaults_to_0", order_type="MARKET", price=None),
        case("market_order_explicit_zero_price", order_type="MKT", price="0"),
        case("stop_loss_limit", order_type="SL", price="2000", trigger_price="1999.5"),
        case("stop_loss_market", order_type="SL-M", price=None, trigger_price="1999.5"),
        case("amo_true", amo=True),
        case("tag_and_disclosed_quantity", quantity=10, disclosed_quantity=5, tag="my-tag"),
        case("decimal_price_trailing_zeros_kept", price="2000.50"),
        case("json_number_price", price=2000.05),
        case("integer_price", price=2000),
        case("market_protection", order_type="MARKET", price=None, market_protection="5"),
        case("nfo_quantity_in_units", exchange="NFO", tradingsymbol="NIFTY", quantity=65),
        case("cover_order", product="CO", book_loss_price="10"),
        case(
            "bracket_order",
            product="BO",
            book_loss_price="10",
            book_profit_price="20",
            trail_price="1",
        ),
        case("mtf_on_nse", product="MTF"),
        case("algo_id_sent_last", algo_id="153763"),
        case(
            "algo_id_after_market_protection",
            order_type="MARKET",
            price=None,
            market_protection="5",
            algo_id="153763",
        ),
        case("error_empty_algo_id", algo_id=""),
        case("error_blank_algo_id", algo_id="   "),
        case("error_integer_algo_id", algo_id=153763),
        case("error_boolean_algo_id", algo_id=True),
        case("error_unknown_exchange", exchange="XYZ"),
        case("error_unknown_product", product="DELIVERY"),
        case("error_unknown_order_type", order_type="STOP"),
        case("error_unknown_validity", validity="GTC"),
        case("error_zero_quantity", quantity=0),
        case("error_boolean_quantity", quantity=True),
        case("error_limit_without_price", price=None),
        case("error_limit_with_zero_price", price="0"),
        case("error_negative_price", price="-1"),
        case("error_non_numeric_price", price="abc"),
        case("error_stop_loss_without_trigger", order_type="SL", price="2000"),
        case("error_trigger_on_limit_order", trigger_price="1999"),
        case("error_disclosed_above_quantity", quantity=5, disclosed_quantity=6),
        case("error_cover_without_book_loss", product="CO"),
        case("error_bracket_without_book_profit", product="BO", book_loss_price="10"),
        case("error_book_loss_on_cnc", book_loss_price="10"),
        case(
            "error_book_profit_on_cover", product="CO", book_loss_price="10", book_profit_price="5"
        ),
        case("error_mtf_on_mcx", exchange="MCX", tradingsymbol="CRUDEOIL", product="MTF"),
        *_gtt_validation_cases(),
    ]


# ----------------------------------------------------------------------------- behaviour harness
#
# The vectors below drive the real client, transport, script-master helper and stream buffer
# with an in-memory HTTP transport (``httpx.MockTransport``): no network, no sleeping.


@contextmanager
def _captured(logger_name: str) -> Iterator[list[logging.LogRecord]]:
    """Collect the records of one SDK logger (all levels) while the block runs."""
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(logger_name)
    handler = _Collect(level=logging.DEBUG)
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


# Transport failures, raised by the in-memory transport while the request is being sent.
_FAILURES: dict[str, Any] = {
    "connect_error": lambda r: httpx.ConnectError("refused", request=r),
    "connect_timeout": lambda r: httpx.ConnectTimeout("connect", request=r),
    "pool_timeout": lambda r: httpx.PoolTimeout("pool", request=r),
    "read_timeout": lambda r: httpx.ReadTimeout("read", request=r),
    "write_timeout": lambda r: httpx.WriteTimeout("write", request=r),
    "connection_reset": lambda r: httpx.ReadError("reset", request=r),
    "remote_protocol_error": lambda r: httpx.RemoteProtocolError("protocol", request=r),
    "decoding_error": lambda r: httpx.DecodingError("decoding", request=r),
    "hook_exception": lambda r: RuntimeError("raised by the caller's hook"),
    "hook_sdk_error": lambda r: ServerError(
        "raised by the caller's hook", endpoint="hook", http_status=503
    ),
    "keyboard_interrupt": lambda r: KeyboardInterrupt(),
}

# Fast retries, so a read that is retried does not wait in real time.
_FAST = {"backoff_base": 1e-6, "backoff_cap": 1e-6}
_ACK = '{"stat":"Ok","norenordno":"26111000000001","request_time":"10:00:00 10-11-2026"}'
_EXPIRED = {
    "access_token": "synthetic-token",
    "issued_at": datetime(2026, 1, 1, 9, 0, tzinfo=IST),
    "expires_at": datetime(2026, 1, 2, 6, 0, tzinfo=IST),
}


def _reply(step: dict[str, Any], request: httpx.Request) -> httpx.Response:
    """One scripted reply: ``{"failure": kind}`` raises, else ``{"status", "body"}``."""
    if "failure" in step:
        raise _FAILURES[step["failure"]](request)
    return httpx.Response(step.get("status", 200), text=step.get("body", ""))


def _scripted_client(
    replies: Sequence[dict[str, Any]],
    sent: list[httpx.Request],
    *,
    token: Any = "synthetic-token",
    **config: Any,
) -> TradeSmart:
    """A client whose transport answers with ``replies`` in turn (the last one repeats)."""

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return _reply(replies[min(len(sent), len(replies)) - 1], request)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return TradeSmart(
        "TEST123",
        access_token=token,
        config=ClientConfig(**{**_FAST, **config}),
        http_client=http,
    )


_ORDER_ARGUMENTS: dict[str, dict[str, Any]] = {
    "place_order": dict(_LIMIT_BUY),
    "modify_order": dict(_MODIFY),
    "cancel_order": {"order_id": "26111000000001"},
    "exit_order": {"order_id": "26111000000001", "product": "BO"},
    "place_gtt": dict(_GTT),
    "modify_gtt": {**_GTT, "alert_id": _ALERT_ID},
    "cancel_gtt": {"alert_id": _ALERT_ID},
}


def _invoke(client: TradeSmart, call: str, invalid: bool = False) -> Any:
    """Run ``call`` on ``client``: an order method, ``request`` or a read."""
    if call == "request_place_order":
        return client.request("placeorder", {"exch": "NSE"}, state_changing=True)
    if call == "request_place_gtt":
        return client.request("placegttorder", {"exch": "NSE"}, state_changing=False)
    if call == "request_cancel_gtt":
        return client.request("CANCELGTTORDER", {"al_id": "26111200000099"}, state_changing=False)
    if call == "request_place_iceberg":
        return client.request("placeicebergorder", {"exch": "NSE"}, state_changing=False)
    if call == "request_order_book_state_changing":
        return client.request("OrderBook", {}, state_changing=True, expect="list")
    if call == "limits":
        return client.margins()
    arguments = dict(_ORDER_ARGUMENTS[call])
    if invalid:
        arguments["quantity"] = 0
    return getattr(client, call)(**arguments)


def eval_outcome(i: dict[str, Any]) -> dict[str, Any]:
    """The order outcome line(s) of one call (``tradesmart.orders``), the error, and how
    many requests reached the transport for that call."""
    sent: list[httpx.Request] = []
    token: Any = {
        "valid": "synthetic-token",
        "none": None,
        "expired": AccessToken.model_validate(_EXPIRED),
    }[i["token"]]
    client = _scripted_client(
        [{"body": _ACK}] * i["prior_orders"] + i["replies"],
        sent,
        token=token,
        rate_limit_mode=i["rate_limit_mode"],
        order_rate_limit_max_wait=i["order_rate_limit_max_wait"],
    )
    # A frozen limiter clock: the prior orders and the call share one instant (so a case
    # must never need a wait the cap allows: the frozen clock would never let it end).
    client._transport._limiter._limiters["place_order"] = RateLimiter(
        RATE_LIMITS["place_order"], clock=lambda: 0.0
    )
    for _ in range(i["prior_orders"]):
        client.place_order(**_LIMIT_BUY)
    before = len(sent)
    if i["closed"]:
        client.close()
    error: str | None = None
    with _captured("tradesmart.orders") as records:
        try:
            _invoke(client, i["call"], invalid=i["invalid_parameters"])
        except (Exception, KeyboardInterrupt) as exc:
            error = type(exc).__name__
    info = [r.getMessage() for r in records if r.levelno == logging.INFO]
    warning = [r.getMessage() for r in records if r.levelno == logging.WARNING]
    assert len(info) <= 1 and len(warning) <= 1, (info, warning)
    return {
        "error": error,
        "info": re.sub(r" in \d+ ms$", "", info[0]) if info else None,
        "warning": warning[0] if warning else None,
        "requests": len(sent) - before,
    }


def _outcome_cases() -> list[Any]:
    def case(name: str, *replies: dict[str, Any], **changes: Any) -> tuple[str, dict[str, Any]]:
        base: dict[str, Any] = {
            "call": "place_order",
            "token": "valid",
            "closed": False,
            "rate_limit_mode": "wait",
            "order_rate_limit_max_wait": 1.0,
            "prior_orders": 0,
            "invalid_parameters": False,
            # the prior orders get an Ok ack each; the call gets these replies in turn
            "replies": list(replies),
        }
        return name, {**base, **changes}

    not_ok = '{"stat":"Not_Ok","emsg":"RMS:Blocked"}'

    def gtt_ack(word: str) -> str:
        return json.dumps({"stat": word, "al_id": _ALERT_ID}, separators=(",", ":"))

    return [
        case("accepted_ack", {"body": _ACK}),
        case("not_ok_is_rejected", {"body": not_ok}),
        case(
            "server_429_is_rejected",
            {"status": 429, "body": '{"stat":"Not_Ok","emsg":"Too many requests"}'},
        ),
        case(
            "session_expired_is_rejected",
            {"body": '{"stat":"Not_Ok","emsg":"Session Expired : Invalid Session Key"}'},
        ),
        case("non_json_body_is_unknown", {"status": 502, "body": "<html>Bad gateway</html>"}),
        case("ok_without_order_id_is_unknown", {"body": '{"stat":"Ok"}'}),
        case("read_timeout_is_unknown", {"failure": "read_timeout"}),
        case("connection_reset_is_unknown", {"failure": "connection_reset"}),
        case("hook_exception_is_unknown", {"failure": "hook_exception"}),
        case("connect_error_is_not_sent", {"failure": "connect_error"}),
        case("connect_timeout_is_not_sent", {"failure": "connect_timeout"}),
        case("pool_timeout_is_not_sent", {"failure": "pool_timeout"}),
        case("no_token_is_not_sent", {"body": _ACK}, token="none"),
        case("expired_access_token_is_not_sent", {"body": _ACK}, token="expired"),
        case("closed_client_is_not_sent", {"body": _ACK}, closed=True),
        case(
            "client_rate_limit_is_not_sent",
            {"body": _ACK},
            rate_limit_mode="raise",
            prior_orders=10,
        ),
        case(
            # the 11th order in the same instant needs a 1 s wait, above the 0.5 s cap
            "client_rate_limit_wait_over_the_cap_is_not_sent",
            {"body": _ACK},
            prior_orders=10,
            order_rate_limit_max_wait=0.5,
        ),
        case("invalid_parameters_log_nothing", {"body": _ACK}, invalid_parameters=True),
        case(
            "keyboard_interrupt_after_send_is_unknown",
            {"failure": "keyboard_interrupt"},
        ),
        case(
            "cancel_accepted_names_the_callers_order_id",
            {"body": '{"stat":"Ok","request_time":"10:00:00 10-11-2026"}'},
            call="cancel_order",
        ),
        case("modify_accepted", {"body": _ACK}, call="modify_order"),
        case("exit_rejected", {"body": not_ok}, call="exit_order"),
        case("request_place_order_is_logged", {"body": _ACK}, call="request_place_order"),
        case(
            "request_state_changing_order_book_is_not_logged",
            {"body": "[]"},
            call="request_order_book_state_changing",
        ),
        case("reads_are_not_logged", {"body": not_ok}, call="limits"),
        case(
            "request_place_gtt_accepted_logs_the_al_id",
            {"body": '{"stat":"Oi created","al_id":"26111200000010"}'},
            call="request_place_gtt",
        ),
        case(
            "request_place_gtt_ok_is_unknown",
            {"body": '{"stat":"Ok","al_id":"26111200000010"}'},
            call="request_place_gtt",
        ),
        case("request_place_gtt_not_ok_is_rejected", {"body": not_ok}, call="request_place_gtt"),
        case(
            "request_place_gtt_read_timeout_is_unknown",
            {"failure": "read_timeout"},
            call="request_place_gtt",
        ),
        case(
            "request_cancel_gtt_unknown_names_the_payload_al_id",
            {"failure": "read_timeout"},
            call="request_cancel_gtt",
        ),
        case(
            "request_cancel_gtt_accepted",
            {"body": '{"stat":"Oi delete success","al_id":"26111200000099"}'},
            call="request_cancel_gtt",
        ),
        case(
            "request_place_gtt_test_env_case_word_is_accepted",
            {"body": '{"stat":"OI created","al_id":"26111200000010"}'},
            call="request_place_gtt",
        ),
        case(
            "request_cancel_gtt_test_env_word_oi_deleted_is_unknown",
            {"body": '{"stat":"OI deleted","al_id":"26111200000099"}'},
            call="request_cancel_gtt",
        ),
        case(
            "request_place_iceberg_accepted",
            {"body": '{"stat":"true","aq_id":"26111200000020"}'},
            call="request_place_iceberg",
        ),
        case(
            "request_place_iceberg_false_is_rejected",
            {"body": '{"stat":"false","rejreason":"Insufficient margin"}'},
            call="request_place_iceberg",
        ),
        case(
            "request_place_iceberg_capitalised_true_is_unknown",
            {"body": '{"stat":"True"}'},
            call="request_place_iceberg",
        ),
        # The typed GTT writes: the outcome line names the al_id (the ack's, else the one sent).
        case(
            "place_gtt_accepted_logs_the_al_id", {"body": gtt_ack("Oi created")}, call="place_gtt"
        ),
        case(
            "place_gtt_ok_is_unknown",
            {"body": '{"stat":"Ok","al_id":"' + _ALERT_ID + '"}'},
            call="place_gtt",
        ),
        case(
            "place_gtt_test_env_case_word_is_accepted",
            {"body": gtt_ack("OI created")},
            call="place_gtt",
        ),
        case("place_gtt_not_ok_is_rejected", {"body": not_ok}, call="place_gtt"),
        case("place_gtt_read_timeout_is_unknown", {"failure": "read_timeout"}, call="place_gtt"),
        case("place_gtt_connect_error_is_not_sent", {"failure": "connect_error"}, call="place_gtt"),
        case(
            "modify_gtt_accepted_logs_the_al_id",
            {"body": gtt_ack("Oi replaced")},
            call="modify_gtt",
        ),
        case("modify_gtt_read_timeout_is_unknown", {"failure": "read_timeout"}, call="modify_gtt"),
        case(
            "cancel_gtt_accepted_logs_the_al_id",
            {"body": gtt_ack("Oi delete success")},
            call="cancel_gtt",
        ),
        case("cancel_gtt_read_timeout_is_unknown", {"failure": "read_timeout"}, call="cancel_gtt"),
        case(
            "cancel_gtt_test_env_word_oi_deleted_is_unknown",
            {"body": gtt_ack("OI deleted")},
            call="cancel_gtt",
        ),
    ]


def eval_transport_failure(i: dict[str, Any]) -> dict[str, Any]:
    """The error a call raises when sending fails, and how many requests were attempted."""
    sent: list[httpx.Request] = []
    client = _scripted_client([{"failure": i["failure"]}], sent)
    try:
        _invoke(client, i["call"])
    except (Exception, KeyboardInterrupt) as exc:
        return {"error": type(exc).__name__, "requests": len(sent)}
    raise AssertionError("the call did not fail")  # pragma: no cover


def _transport_cases() -> list[Any]:
    kinds = (
        "connect_error",
        "connect_timeout",
        "pool_timeout",
        "read_timeout",
        "write_timeout",
        "connection_reset",
        "remote_protocol_error",
        "decoding_error",
        "hook_exception",
        "hook_sdk_error",
        "keyboard_interrupt",
    )
    cases = []
    for prefix, call in (
        ("place_order", "place_order"),
        ("cancel_order", "cancel_order"),
        ("request_state_changing", "request_order_book_state_changing"),
        ("read", "limits"),
        ("place_gtt", "place_gtt"),
        ("modify_gtt", "modify_gtt"),
        ("cancel_gtt", "cancel_gtt"),
    ):
        for kind in kinds:
            if prefix in (
                "cancel_order",
                "request_state_changing",
                "modify_gtt",
                "cancel_gtt",
            ) and kind not in (
                "connect_error",
                "read_timeout",
                "hook_exception",
            ):
                continue
            cases.append((f"{prefix}_{kind}", {"call": call, "failure": kind}))
    return cases


def eval_request_name(i: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in i["payload"]}
    try:
        call = build_raw_call(
            "TEST123",
            i["endpoint"],
            payload,
            state_changing=i["state_changing"],
            expect=i["expect"],
        )
    except Exception as exc:
        return _error(exc)
    return {
        "endpoint": call.spec.name,
        "state_changing": call.spec.state_changing,
        "rate_group": call.spec.rate_group,
        "expect": call.spec.expect,
        "allow_empty": call.spec.allow_empty,
        "body": encode_jdata(call.payload),
        "success_stats": sorted(call.spec.success_stats),
        "requires_ack_id": call.spec.requires_ack_id,
    }


def _request_name_cases() -> list[Any]:
    def case(
        name: str,
        endpoint: str,
        *pairs: tuple[str, Any],
        state_changing: bool = False,
        expect: str = "object",
    ) -> tuple[str, dict[str, Any]]:
        return name, {
            "endpoint": endpoint,
            "payload": [list(p) for p in pairs],
            "state_changing": state_changing,
            "expect": expect,
        }

    return [
        case("documented_spelling", "PlaceOrder", ("exch", "NSE")),
        case("padded_lower_case_place_order_is_canonical", " /placeorder/ ", ("exch", "NSE")),
        case("upper_case_cancel_order_is_state_changing", "CANCELORDER", ("norenordno", "1")),
        case("modify_order_any_case", "modifyORDER"),
        case("exit_sno_order_any_case", "exitsnoorder"),
        case("place_multi_order_has_the_orders_group", "placemultiorder"),
        case("gen_acs_tok_is_state_changing", "genacstok"),
        case("get_quotes_any_case_has_the_quotes_group", "getquotes", ("exch", "NSE")),
        case("get_quotes_multiple_has_the_quotes_group", "GETQUOTESMULTIPLE"),
        case("tpseries_any_case_has_the_tpseries_group", "tpseries"),
        case("unknown_name_is_sent_as_given", "limits"),
        case("unknown_name_keeps_its_case", "OrderBook", expect="list"),
        case("read_marked_state_changing", "OrderBook", state_changing=True, expect="list"),
        case("uid_is_added_last_when_absent", "Limits", ("actid", "TEST123")),
        case("given_uid_is_kept", "Limits", ("uid", "OTHER1"), ("actid", "OTHER1")),
        case(
            "values_are_converted_to_wire_strings",
            "Custom",
            ("flag", True),
            ("off", False),
            ("count", 5),
            ("price", 101.5),
        ),
        case("dot_in_name_is_rejected", "Place.Order"),
        case("inner_slash_is_rejected", "api/PlaceOrder"),
        case("inner_space_is_rejected", "Place Order"),
        case("digits_are_rejected", "PlaceOrder2"),
        case("non_ascii_letter_is_rejected", "Plàceorder"),
        case("empty_name_is_rejected", " / "),
        case("null_value_is_a_type_error", "Limits", ("actid", None)),
        case("place_gtt_any_case_is_state_changing", "placegttorder", ("exch", "NSE")),
        case("modify_gtt_any_case_is_state_changing", "MODIFYGTTORDER", ("al_id", "1")),
        case("cancel_gtt_any_case_is_state_changing", " /CancelGttOrder/ ", ("al_id", "1")),
        case("place_iceberg_any_case_is_state_changing", "PLACEICEBERGORDER"),
        case("modify_iceberg_any_case_is_state_changing", "modifyicebergorder"),
        case("cancel_iceberg_any_case_is_state_changing", "CancelIcebergOrder"),
        case("gtt_book_read_is_sent_as_given", "GetPendingGTTOrder", expect="list"),
        case("iceberg_book_read_is_sent_as_given", "GetIceBergOrder", expect="list"),
    ]


def eval_success_word(i: dict[str, Any]) -> dict[str, Any]:
    """How a reply to one endpoint is classified with that endpoint's success words."""
    endpoint = i["endpoint"]
    return eval_not_ok(
        {
            "endpoint": endpoint,
            "http_status": i["http_status"],
            "body": i["body"],
            "expect": i["expect"],
            "allow_empty": False,
            "state_changing": endpoint in STATE_CHANGING_ENDPOINTS,
            "success_stats": SUCCESS_STATS.get(endpoint, OK_STATS),
            "requires_ack_id": ACK_ID_FIELDS.get(endpoint),
            "stats_ignore_case": endpoint in SUCCESS_STATS_IGNORE_CASE,
        }
    )


def _success_word_cases() -> list[Any]:
    def case(
        name: str, endpoint: str, body: Any, status: int = 200, expect: str = "object"
    ) -> tuple[str, dict[str, Any]]:
        text = body if isinstance(body, str) else json.dumps(body, separators=(",", ":"))
        return name, {"endpoint": endpoint, "http_status": status, "body": text, "expect": expect}

    words = {
        "place_gtt": ("PlaceGTTOrder", "Oi created"),
        "modify_gtt": ("ModifyGTTOrder", "Oi replaced"),
        "cancel_gtt": ("CancelGTTOrder", "Oi delete success"),
    }
    expired = {"stat": "Not_Ok", "emsg": "Session Expired :  Invalid Session Key"}
    cases = []
    for prefix, (endpoint, word) in words.items():
        al = "26111200000010"
        cases += [
            case(f"{prefix}_word_with_al_id_is_success", endpoint, {"stat": word, "al_id": al}),
            case(
                f"{prefix}_word_with_blank_emsg_is_success",
                endpoint,
                {"stat": word, "al_id": al, "emsg": ""},
            ),
            case(f"{prefix}_ok_is_unknown", endpoint, {"stat": "Ok", "al_id": al}),
            case(f"{prefix}_word_without_al_id_is_unknown", endpoint, {"stat": word}),
            case(f"{prefix}_word_blank_al_id_is_unknown", endpoint, {"stat": word, "al_id": " "}),
            case(
                f"{prefix}_word_numeric_al_id_is_unknown",
                endpoint,
                {"stat": word, "al_id": 26111200000010},
            ),
            case(
                f"{prefix}_word_with_emsg_is_unknown",
                endpoint,
                {"stat": word, "al_id": al, "emsg": "Invalid input"},
            ),
            # Letter case is ignored (spec F32: the test environment answers "OI created"); nothing
            # else is.
            case(f"{prefix}_lower_case_is_success", endpoint, {"stat": word.lower(), "al_id": al}),
            case(f"{prefix}_upper_case_is_success", endpoint, {"stat": word.upper(), "al_id": al}),
            case(
                f"{prefix}_test_env_case_is_success",
                endpoint,
                {"stat": "OI" + word[2:], "al_id": al},
            ),
            case(
                f"{prefix}_upper_case_without_al_id_is_unknown",
                endpoint,
                {"stat": word.upper()},
            ),
            case(
                f"{prefix}_double_space_is_unknown",
                endpoint,
                {"stat": word.replace(" ", "  ", 1), "al_id": al},
            ),
            case(f"{prefix}_leading_space_is_unknown", endpoint, {"stat": " " + word, "al_id": al}),
            case(
                f"{prefix}_trailing_space_is_unknown", endpoint, {"stat": word + " ", "al_id": al}
            ),
            case(f"{prefix}_missing_stat_is_unknown", endpoint, {"al_id": al}),
            case(f"{prefix}_not_ok_is_rejected", endpoint, {"stat": "Not_Ok", "emsg": "Invalid"}),
            case(f"{prefix}_not_ok_http_401_is_authentication", endpoint, expired, 401),
            case(f"{prefix}_non_json_is_unknown", endpoint, "<html>Bad gateway</html>", 502),
        ]
    cases.append(
        case(
            "place_gtt_word_of_modify_is_unknown",
            "PlaceGTTOrder",
            {"stat": "Oi replaced", "al_id": "26111200000010"},
        )
    )
    cases.append(
        case(
            "cancel_gtt_test_env_word_oi_deleted_is_unknown",
            "CancelGTTOrder",
            {"stat": "OI deleted", "al_id": "26111200000010"},
        )
    )
    for prefix, endpoint in (
        ("place_iceberg", "PlaceIceBergOrder"),
        ("modify_iceberg", "ModifyIceBergOrder"),
        ("cancel_iceberg", "CancelIceBergOrder"),
    ):
        cases += [
            case(f"{prefix}_true_is_success", endpoint, {"stat": "true", "aq_id": "1"}),
            case(
                f"{prefix}_false_is_rejected_with_rejreason",
                endpoint,
                {"stat": "false", "rejreason": "Insufficient margin"},
            ),
            case(f"{prefix}_not_ok_is_rejected", endpoint, {"stat": "Not_Ok", "emsg": "Invalid"}),
            case(f"{prefix}_capitalised_true_is_unknown", endpoint, {"stat": "True"}),
            case(f"{prefix}_upper_case_true_is_unknown", endpoint, {"stat": "TRUE"}),
            case(f"{prefix}_json_true_is_unknown", endpoint, {"stat": True}),
            case(f"{prefix}_ok_is_unknown", endpoint, {"stat": "Ok", "aq_id": "1"}),
        ]
    cases += [
        case(
            "iceberg_false_prefers_a_non_blank_rejreason",
            "PlaceIceBergOrder",
            {"stat": "false", "emsg": "Bad request", "rejreason": "Other"},
        ),
        case(
            "iceberg_false_blank_rejreason_falls_back_to_emsg",
            "PlaceIceBergOrder",
            {"stat": "false", "emsg": "Bad request", "rejreason": " "},
        ),
        case(
            "iceberg_false_http_403_is_authentication",
            "ModifyIceBergOrder",
            {"stat": "false", "rejreason": "x"},
            403,
        ),
        case(
            "iceberg_false_http_429_is_rate_limit",
            "ModifyIceBergOrder",
            {"stat": "false", "rejreason": "x"},
            429,
        ),
        case(
            "iceberg_false_http_404_is_not_found",
            "CancelIceBergOrder",
            {"stat": "false", "rejreason": "x"},
            404,
        ),
        case(
            "iceberg_false_http_500_is_rejected",
            "CancelIceBergOrder",
            {"stat": "false", "rejreason": "x"},
            500,
        ),
        case(
            "false_outside_the_iceberg_writes_is_unknown",
            "PlaceGTTOrder",
            {"stat": "false", "rejreason": "x"},
        ),
        case("iceberg_false_without_a_reason", "PlaceIceBergOrder", {"stat": "false"}),
        case(
            "iceberg_false_http_401_is_authentication",
            "PlaceIceBergOrder",
            {"stat": "false", "rejreason": "x"},
            401,
        ),
        case(
            "place_order_gtt_word_is_unknown",
            "PlaceOrder",
            {"stat": "Oi created", "al_id": "1", "norenordno": "1"},
        ),
        case("place_order_true_is_unknown", "PlaceOrder", {"stat": "true", "norenordno": "1"}),
        case("place_order_ok_is_success", "PlaceOrder", {"stat": "Ok", "norenordno": "1"}),
        case("read_gtt_word_is_a_parse_error", "Limits", {"stat": "Oi created", "al_id": "1"}),
        case("read_false_is_a_parse_error", "Limits", {"stat": "false"}),
        case("gtt_book_not_ok_read_is_an_api_error", "GetPendingGTTOrder", {"stat": "Not_Ok"}),
        case(
            "place_multi_order_list_with_expect_list_is_returned",
            "PlaceMultiOrder",
            [],
            expect="list",
        ),
    ]
    # Any JSON list on a GTT or iceberg write is unknown, whatever ``expect`` says.
    for endpoint in sorted(SUCCESS_STATS):
        for label, body in (
            ("empty", []),
            ("ack_in_list", [{"stat": "Oi created", "al_id": "1"}]),
            ("true_in_list", [{"stat": "true"}]),
        ):
            for expect in ("object", "list"):
                cases.append(
                    case(
                        f"{endpoint}_{label}_expect_{expect}_is_unknown",
                        endpoint,
                        body,
                        expect=expect,
                    )
                )
    return cases


def eval_read_payload(i: dict[str, Any]) -> dict[str, Any]:
    """The endpoint and exact body a read method sends, or the validation error."""

    def arg(value: Any) -> Any:
        if isinstance(value, dict) and "date" in value:
            return date.fromisoformat(value["date"])
        if isinstance(value, dict) and "datetime" in value:
            return datetime.fromisoformat(value["datetime"])
        return value

    sent: list[httpx.Request] = []
    client = _scripted_client([{"body": '{"stat":"Not_Ok","emsg":"captured"}'}], sent)
    arguments = {key: arg(value) for key, value in i["arguments"].items()}
    try:
        getattr(client, i["call"])(**arguments)
    except APIError:
        pass  # the scripted reply: the request was captured
    except Exception as exc:
        assert not sent, "a rejected call must send nothing"
        return _validation_error(exc)
    request = sent[0]
    return {"endpoint": request.url.path.rsplit("/", 1)[-1], "body": request.content.decode()}


def _read_payload_cases() -> list[Any]:
    def case(name: str, call: str, **arguments: Any) -> tuple[str, dict[str, Any]]:
        return name, {"call": call, "arguments": arguments}

    morning = {"datetime": "2026-11-10T09:15:00+05:30"}
    return [
        case("profile_sends_uid", "profile"),
        case("margins_sends_uid_and_actid", "margins"),
        case("positions_sends_uid_and_actid", "positions"),
        case("holdings_default_product_is_cnc", "holdings"),
        case("holdings_product_word_any_case", "holdings", product="nrml"),
        case("holdings_product_wire_code", "holdings", product="I"),
        case("error_holdings_unknown_product", "holdings", product="DELIVERY"),
        case("orders_sends_uid_only", "orders"),
        case("order_history", "order_history", order_id="26111000000001"),
        case("error_order_history_integer_id", "order_history", order_id=26111000000001),
        case("trades_sends_uid_and_actid", "trades"),
        case("search_instruments", "search_instruments", text="RELIANCE"),
        case("quote_exchange_any_case", "quote", exchange="nse", token="2885"),
        case("error_quote_integer_token", "quote", exchange="NSE", token=2885),
        case("error_quote_unknown_exchange", "quote", exchange="XYZ", token="2885"),
        case("security_info", "security_info", exchange="NFO", token="43650"),
        case(
            "option_chain",
            "option_chain",
            exchange="NFO",
            tradingsymbol="NIFTY24NOV26C24000",
            strike_price="24000",
            count=5,
        ),
        case(
            "error_option_chain_zero_count",
            "option_chain",
            exchange="NFO",
            tradingsymbol="NIFTY24NOV26C24000",
            strike_price="24000",
            count=0,
        ),
        case("historical_data_without_times", "historical_data", exchange="NSE", token="22"),
        case(
            "historical_data_aware_datetimes_and_interval",
            "historical_data",
            exchange="NSE",
            token="22",
            from_date=morning,
            to_date={"datetime": "2026-11-10T04:45:00+00:00"},
            interval=5,
        ),
        case(
            "historical_data_date_is_00_00_ist",
            "historical_data",
            exchange="NSE",
            token="22",
            from_date={"date": "2026-11-10"},
        ),
        case(
            "historical_data_epoch_seconds",
            "historical_data",
            exchange="NSE",
            token="22",
            from_date=1762746300,
            to_date=1762768800,
        ),
        case(
            "error_historical_data_naive_datetime",
            "historical_data",
            exchange="NSE",
            token="22",
            from_date={"datetime": "2026-11-10T09:15:00"},
        ),
        case(
            "error_historical_data_float_time",
            "historical_data",
            exchange="NSE",
            token="22",
            from_date=1762746300.5,
        ),
        case(
            "error_historical_data_text_time",
            "historical_data",
            exchange="NSE",
            token="22",
            from_date="1762746300",
        ),
        case(
            "error_historical_data_unknown_interval",
            "historical_data",
            exchange="NSE",
            token="22",
            interval=7,
        ),
        case(
            "daily_history_dates",
            "daily_history",
            exchange="nse",
            tradingsymbol="RELIANCE-EQ",
            from_date={"date": "2026-11-01"},
            to_date={"date": "2026-11-10"},
        ),
        case("pending_gtts_sends_uid_only", "pending_gtts"),
        case("triggered_gtts_sends_uid_and_actid", "triggered_gtts"),
        case("gtt_alert_types_sends_uid_only", "gtt_alert_types"),
    ]


def eval_script_master_parse(i: dict[str, Any]) -> dict[str, Any]:
    """Rows parsed from a script-master file (``text`` as UTF-8, or raw ``bytes_hex``)."""
    data = bytes.fromhex(i["bytes_hex"]) if "bytes_hex" in i else i["text"].encode("utf-8")
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "NSE_symbols.txt"
        path.write_bytes(data)
        try:
            rows = _instruments.load(path)
        except Exception as exc:
            return _error(exc)
    return {"rows": rows}


def _script_master_parse_cases() -> list[Any]:
    def case(name: str, text: str) -> tuple[str, dict[str, Any]]:
        return name, {"text": text}

    nse = "Exchange,Token,LotSize,Symbol,TradingSymbol,Instrument,TickSize,\n"
    return [
        case("empty_trailing_header_column", nse + "NSE,22,1,ACC,ACC-EQ,EQ,0.05,\n"),
        case("pipe_delimiter", "Exchange|Token|TradingSymbol\nNSE|22|ACC-EQ\n"),
        case("tab_delimiter", "Exchange\tToken\tTradingSymbol\nNSE\t22\tACC-EQ\n"),
        case("semicolon_delimiter", "Exchange;Token\nNSE;22\n"),
        case("delimiter_most_frequent_in_header_wins", "A|B|C,D\n1|2|3,4\n"),
        case("delimiter_tie_first_in_order_wins", "A,B|C\n1,2|3\n"),
        case("delimiter_tie_pipe_before_tab", "A|B\tC\n1|2\t3\n"),
        case("delimiter_tie_tab_before_semicolon", "A\tB;C\n1\t2;3\n"),
        case("no_delimiter_is_one_column", "Exchange\nNSE\n"),
        case("delimiter_from_first_non_blank_line", "\n  \nA|B\n1|2\n"),
        case("quoted_field_holds_the_delimiter", 'A,B\n"x,y",2\n'),
        case("doubled_quote_in_quoted_field", 'A,B\n"say ""hi""",2\n'),
        case("quoted_field_holds_a_line_break", 'A,B\n"line1\nline2",2\n'),
        case("blank_rows_are_skipped", "A,B\n\n1,2\n , \n3,4\n"),
        case("header_and_values_are_trimmed", " A , B \n 1 , 2 \n"),
        case("short_row_has_no_key_for_missing_columns", "A,B,C\n1,2\n"),
        case("long_row_extra_cells_are_dropped", "A,B\n1,2,3,4\n"),
        case("duplicate_header_last_column_wins", "A,B,A\n1,2,3\n"),
        case("byte_order_mark_is_ignored", "﻿Exchange,Token\nNSE,22\n"),
        case("crlf_line_endings", "Exchange,Token\r\nNSE,22\r\n"),
        case("values_stay_strings", "Token,LotSize,TickSize\n00022,0065,0.050\n"),
        case("empty_file", ""),
        case("header_only", "Exchange,Token\n"),
        ("not_utf8_is_a_parse_error", {"bytes_hex": "45786368616e67650a4e5345ff0a"}),
        case("nul_byte_is_a_parse_error", "Exchange,Token\nNSE,2\x002\n"),
    ]


def _sm_zip(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("NSE_symbols.txt", text)
    return buffer.getvalue()


_SM_NEW = "Exchange,Token,TradingSymbol,\nNSE,22,ACC-EQ,\n"


def eval_script_master_freshness(i: dict[str, Any]) -> dict[str, Any]:
    """One ``download_instruments("NSE")`` with a cached file in a given state.

    ``responses`` answer the requests in turn: ``{"status", "last_modified"}`` (200 carries a
    new file), ``{"failure": kind}`` raises, ``delete_file`` removes the cached file first.
    """
    now = datetime.fromisoformat(i["now"])
    responses = list(i["responses"])
    requests: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / "NSE_symbols.txt"
        sidecar = target.with_name(target.name + ".checked")
        if i["file_mtime"] is not None:
            target.write_text("cached", encoding="utf-8")
            stamp = datetime.fromisoformat(i["file_mtime"]).timestamp()
            os.utime(target, (stamp, stamp))
        if i["checked"] is not None:
            sidecar.write_text(i["checked"], encoding="utf-8")

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append({"if_modified_since": request.headers.get("If-Modified-Since")})
            step = responses.pop(0)
            if step.get("delete_file"):
                target.unlink()
            if "failure" in step:
                raise _FAILURES[step["failure"]](request)
            headers = {}
            if step.get("last_modified"):
                headers["Last-Modified"] = step["last_modified"]
            body = _sm_zip(_SM_NEW) if step["status"] == 200 else b""
            return httpx.Response(step["status"], headers=headers, content=body)

        helper = _instruments.Instruments(
            httpx.Client(transport=httpx.MockTransport(handler)), ClientConfig()
        )
        original = _instruments._now
        _instruments._now = lambda: now  # type: ignore[assignment]
        try:
            with _captured("tradesmart.instruments") as records:
                try:
                    helper.download("NSE", folder, force=i["force"])
                    outcome: dict[str, Any] = {}
                except TradeSmartError as exc:
                    outcome = _error(exc)
        finally:
            _instruments._now = original  # type: ignore[assignment]
        assert not responses, "every scripted response must be used"
        warnings = sum(1 for r in records if r.levelno == logging.WARNING)
        if "error" not in outcome:
            downloaded = target.read_text(encoding="utf-8") == _SM_NEW
            modified = datetime.fromtimestamp(target.stat().st_mtime, IST)
            outcome = {
                "file": "downloaded" if downloaded else "cached",
                "file_mtime": modified.isoformat(),
            }
        checked = sidecar.read_text(encoding="utf-8") if sidecar.is_file() else None
    return {**outcome, "requests": requests, "checked": checked, "warnings": warnings}


def _script_master_freshness_cases() -> list[Any]:
    morning = "2026-11-10T10:00:00+05:30"
    yesterday = "2026-11-09T07:00:00+05:30"
    lm_today = "Tue, 10 Nov 2026 01:30:00 GMT"
    ok = {"status": 200, "last_modified": lm_today}

    def case(
        name: str,
        *responses: dict[str, Any],
        now: str = morning,
        file_mtime: str | None = yesterday,
        checked: str | None = None,
        force: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        return name, {
            "now": now,
            "file_mtime": file_mtime,
            "checked": checked,
            "force": force,
            "responses": list(responses),
        }

    return [
        case("no_file_full_download", ok, file_mtime=None),
        case(
            "force_downloads_a_fresh_file", ok, file_mtime="2026-11-10T07:05:00+05:30", force=True
        ),
        case("modified_after_todays_refresh_is_used", file_mtime="2026-11-10T07:05:00+05:30"),
        case("modified_exactly_at_the_refresh_is_used", file_mtime="2026-11-10T07:00:00+05:30"),
        case(
            "before_07_00_yesterdays_refresh_applies",
            now="2026-11-10T06:30:00+05:30",
            file_mtime="2026-11-09T07:30:00+05:30",
        ),
        case(
            "older_file_checked_10_minutes_ago_is_used",
            checked="2026-11-10T04:20:00+00:00",
        ),
        case(
            "older_file_checked_16_minutes_ago_is_rechecked",
            {"status": 304},
            checked="2026-11-10T04:14:00+00:00",
        ),
        case("older_file_without_a_check_gets_304", {"status": 304}),
        case("older_file_replaced_by_a_new_one", ok),
        case("recheck_server_error_keeps_the_file", {"status": 503}),
        case("recheck_network_error_keeps_the_file", {"failure": "connect_error"}),
        case("recheck_timeout_keeps_the_file", {"failure": "read_timeout"}),
        case("recheck_not_found_raises", {"status": 404}),
        case("recheck_forbidden_raises", {"status": 403}),
        case("not_modified_but_file_vanished", {"status": 304, "delete_file": True}, ok),
        case("first_download_server_error_raises", {"status": 503}, file_mtime=None),
        case("first_download_network_error_raises", {"failure": "connect_error"}, file_mtime=None),
        case("first_download_redirect_is_a_parse_error", {"status": 302}, file_mtime=None),
        case("forced_download_failure_raises", {"status": 503}, force=True),
        case(
            "future_check_stamp_counts_as_none",
            {"status": 304},
            checked="2026-11-10T05:00:00+00:00",
        ),
        case("naive_check_stamp_counts_as_none", {"status": 304}, checked="2026-11-10T04:25:00"),
        case("unreadable_check_stamp_counts_as_none", {"status": 304}, checked="yesterday"),
    ]


def eval_stream_backlog(i: dict[str, Any]) -> dict[str, Any]:
    """Messages put into the delivery buffer, taken in between, and the rest drained."""
    buffer = DeliveryBuffer(i["limit"])
    delivered: list[dict[str, Any]] = []

    async def take(count: int) -> list[Any]:
        return [await buffer.get() for _ in range(count)]

    with _captured("tradesmart.stream") as records:
        for op in i["ops"]:
            if "put" in op:
                buffer.put(parse_message(op["put"]))
            else:
                delivered += [m.raw for m in asyncio.run(take(op["take"]))]
        delivered += [m.raw for m in buffer.drain()]
    warnings = []
    for record in records:
        text = record.getMessage()
        if text.startswith("Stream consumer is falling behind"):
            warnings.append("merging_started")
        elif text.startswith("Stream consumer caught up"):
            warnings.append("caught_up")
    return {"delivered": delivered, "merged": buffer.merged_count, "warnings": warnings}


def _stream_backlog_cases() -> list[Any]:
    def tf(token: str = "22", **fields: str) -> dict[str, Any]:
        return {"put": {"t": "tf", "e": "NSE", "tk": token, **fields}}

    def df(token: str = "22", **fields: str) -> dict[str, Any]:
        return {"put": {"t": "df", "e": "NSE", "tk": token, **fields}}

    def ack(kind: str = "tk", token: str = "22", **fields: str) -> dict[str, Any]:
        return {"put": {"t": kind, "e": "NSE", "tk": token, "s": "Ok", **fields}}

    def case(name: str, limit: int, *ops: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return name, {"limit": limit, "ops": list(ops)}

    om = {"put": {"t": "om", "norenordno": "1", "status": "OPEN"}}
    return [
        case("below_the_limit_nothing_merges", 5, tf(lp="1"), tf(lp="2"), tf(lp="3")),
        case(
            "rule_a_merges_into_the_newest_feed_of_the_same_type",
            2,
            tf(lp="1", v="10"),
            tf(lp="2"),
            tf(lp="3"),
            tf(v="30"),
        ),
        case(
            "rule_b_merges_into_the_second_newest_and_copies_shared_fields",
            2,
            tf(lp="1"),
            df(bp1="0.5", lp="1.5"),
            tf(lp="2", v="20"),
        ),
        case(
            "rule_b_copies_only_fields_the_other_type_can_carry",
            2,
            tf(lp="1"),
            df(bp1="0.5"),
            tf(lp="2", pc="1.2", oi="900"),
        ),
        case(
            "rule_b_depth_feed_merges_past_a_touchline_feed",
            3,
            df(bp1="1"),
            df(bp1="2"),
            tf(lp="3"),
            df(bp1="4"),
        ),
        case(
            "no_merge_when_the_second_newest_is_not_a_feed_of_the_same_type",
            1,
            tf(lp="1"),
            ack(lp="1.5"),
            df(bp1="2"),
            tf(lp="3"),
        ),
        case("no_merge_across_an_ack", 1, tf(lp="1"), ack(lp="1.5"), tf(lp="2")),
        case("no_merge_across_a_depth_ack", 1, df(bp1="1"), ack("dk", bp1="1.5"), df(bp1="2")),
        case(
            "no_merge_across_an_unknown_message_of_the_instrument",
            1,
            tf(lp="1"),
            {"put": {"t": "zz", "e": "NSE", "tk": "22"}},
            tf(lp="2"),
        ),
        case(
            "order_updates_are_never_merged_and_do_not_block_instrument_merging",
            1,
            tf(lp="1"),
            om,
            om,
            tf(lp="2"),
        ),
        case(
            "instruments_merge_separately",
            2,
            tf("22", lp="1"),
            tf("23", lp="10"),
            tf("22", lp="2"),
            tf("23", lp="20"),
        ),
        case(
            "same_token_on_another_exchange_is_another_instrument",
            1,
            tf(lp="1"),
            {"put": {"t": "tf", "e": "BSE", "tk": "22", "lp": "5"}},
            tf(lp="2"),
        ),
        case(
            "feed_without_exchange_and_token_is_never_merged",
            1,
            {"put": {"t": "tf", "lp": "1"}},
            {"put": {"t": "tf", "lp": "2"}},
        ),
        case(
            "taken_entries_are_not_merge_targets",
            1,
            tf(lp="1"),
            tf(lp="2"),
            {"take": 1},
            tf(lp="3"),
            tf(lp="4"),
        ),
        case(
            "warnings_when_merging_starts_and_when_caught_up",
            4,
            tf("1", lp="1"),
            tf("2", lp="1"),
            tf("3", lp="1"),
            tf("4", lp="1"),
            tf("1", lp="2"),
            {"take": 3},
            tf("4", lp="2"),
        ),
    ]


# ----------------------------------------------------------------------------- the files

_GREEKS_TOL = {"abs": 1e-9, "rel": 1e-9}

FILES: dict[str, dict[str, Any]] = {
    "checksum.json": {
        "description": (
            "Authorisation checksum for GenAcsTok: lowercase hex SHA-256 of the UTF-8 bytes of "
            "app_key + secret_key + code, no separators. Any empty part is a ConfigurationError."
        ),
        "source": "tradesmart.auth.compute_checksum",
        "tolerance": {},
        "evaluate": eval_checksum,
        "cases": _checksum_cases,
    },
    "not_ok_classification.json": {
        "description": (
            "Response classification of the codec. input.body is the raw response text; "
            'expected is {"returns": <parsed JSON>} or {"error": <error type name>, '
            '"emsg": <string or null>, "message": <the error message>} (emsg and message only '
            'for API errors: "<Endpoint> failed: <emsg>", "<Endpoint> failed (no error message '
            'from the server)" or "<Endpoint> failed: HTTP <status>"). Error type names: '
            "AuthenticationError, RateLimitError, NotFoundError, ServerError, OrderRejectedError, "
            "APIError (plain), ResponseParseError, OrderStatusUnknownError. state_changing is true "
            "for PlaceOrder, PlaceMultiOrder, ModifyOrder, CancelOrder, ExitSNOOrder, "
            "GenAcsTok and the GTT and iceberg writes. The success word here is always 'Ok' "
            "(the GTT and iceberg words: success_words.json). HTTP redirects are never "
            "followed: a 3xx status is classified like any other status."
        ),
        "source": "tradesmart._transport.codec.parse_response",
        "tolerance": {},
        "evaluate": eval_not_ok,
        "cases": _not_ok_cases,
    },
    "expiry_instants.json": {
        "description": (
            'input.function is expiry_instant (expected {"instant": ISO 8601 with +05:30}) or '
            'option_has_expired (expected {"expired": bool}; true when the expiry instant is at '
            "or before now). expiry_time is null or HH:MM in IST and overrides the exchange close "
            "(NFO/BFO 15:30, CDS/BCD 12:30, MCX 23:30). An unknown exchange without an override "
            'is {"error": "ValueError"}.'
        ),
        "source": "tradesmart._option_contracts.expiry_instant / option_has_expired",
        "tolerance": {},
        "evaluate": eval_expiry,
        "cases": _expiry_cases,
    },
    "contract_selection.json": {
        "description": (
            "Script-master rows (all values strings) to option / underlying-future selection. "
            "input.function is find_option (rows, tradingsymbol) or select_future (rows, option "
            "{exchange, tradingsymbol, symbol, expiry}). Expected is the selected contract "
            "(dates YYYY-MM-DD, strike and tick_size as Decimal strings, null for absent) or "
            '{"error": "ValueError"}.'
        ),
        "source": "tradesmart._option_contracts.find_option / select_future",
        "tolerance": {},
        "evaluate": eval_contract,
        "cases": _contract_cases,
    },
    "greeks_prices.json": {
        "description": (
            "Black-76 (underlying = future price) and Black-Scholes-Merton (underlying = spot, "
            "optional continuous dividend_yield) price and Greeks for a given volatility. years "
            "is in 365-day years; theta is per calendar day, vega per 0.01 volatility, rho per "
            "0.01 rate; Black-76 rho holds the future fixed. years <= 0 gives the expired result."
        ),
        "source": "tradesmart.greeks.black76 / black_scholes",
        "tolerance": _GREEKS_TOL,
        "evaluate": eval_price,
        "cases": _price_cases,
    },
    "implied_volatility.json": {
        "description": (
            "Implied volatility from an option price. expected.iv is null when no volatility in "
            "[0.0001, 5.0] reproduces the price within 1e-8. volatility_used (informational, "
            "not an input of the function) is the volatility the price was generated from."
        ),
        "source": "tradesmart.greeks.implied_volatility",
        "tolerance": {"abs": 1e-6},
        "evaluate": eval_iv,
        "cases": _iv_cases,
    },
    "greeks_edge_cases.json": {
        "description": (
            "greeks_from_price status rules: expired, near_expiry (< 30 minutes), "
            "price_at_or_below_intrinsic (including the half-tick rule), no_price, the one-minute "
            "floor on time to expiry, and absurd inputs. Every value is finite or null."
        ),
        "source": "tradesmart.greeks.greeks_from_price",
        "tolerance": {"abs": 1e-6, "rel": 1e-6},
        "evaluate": eval_edge,
        "cases": _edge_cases,
    },
    "carry_forward.json": {
        "description": (
            'input.function is carry_adjusted_forward (expected {"forward"}: F * exp(-(r - q) * '
            "(Tf - To))) or option_forward (the Black-76 forward used by option_greeks for a "
            "future of another expiry: carried on NFO/BFO/CDS/BCD, used as is on MCX or for a "
            "same-expiry future, whatever q). q is the optional input.dividend_yield (absent "
            "means 0): the index dividend yield, or the foreign interest rate for currency "
            "options. now is an ISO instant in IST."
        ),
        "source": ("tradesmart._option_contracts.carry_adjusted_forward / option_greeks_result"),
        "tolerance": {"abs": 1e-9, "rel": 1e-12},
        "evaluate": eval_carry,
        "cases": _carry_cases,
    },
    "pcr.json": {
        "description": (
            "Put-call ratio of a chain: sum(PE) / sum(CE) by open interest (by=oi) or volume "
            "(by=volume); null when the call sum is zero. Missing values count as zero. Decimal "
            "values are strings."
        ),
        "source": "tradesmart.analytics.pcr",
        "tolerance": {"decimal_abs": "1e-12"},
        "evaluate": eval_pcr,
        "cases": _pcr_cases,
    },
    "max_pain.json": {
        "description": (
            "Max pain: the candidate strike (any strike in the chain) minimising the total payout "
            "to option buyers; ties go to the lowest strike; null when no row has open interest. "
            "payouts maps each candidate strike to its total payout. Decimal values are strings."
        ),
        "source": "tradesmart.analytics.max_pain",
        "tolerance": {"decimal_abs": "0"},
        "evaluate": eval_max_pain,
        "cases": _max_pain_cases,
    },
    "buildup.json": {
        "description": (
            "Open-interest buildup. classify_buildup takes price_change and oi_change (JSON "
            "numbers, null, or the strings NaN / Infinity / -Infinity for non-finite values). "
            "buildup_from_snapshot takes a stream snapshot with the wire keys lp, c, oi, poi "
            "(strings; blank or non-numeric means missing) and returns the state with price_change "
            "= lp - c and oi_change = oi - poi as Decimal strings or null."
        ),
        "source": "tradesmart.analytics.classify_buildup / buildup_from_snapshot",
        "tolerance": {"decimal_abs": "0"},
        "evaluate": eval_buildup,
        "cases": _buildup_cases,
    },
    "ws_connect_ack.json": {
        "description": (
            "WebSocket connect acknowledgement: the ack field s is accepted when, trimmed and "
            "compared case-insensitively, it equals ok. The server may send Ok or OK."
        ),
        "source": "tradesmart.stream.async_stream.AsyncTradeSmartStream (connect)",
        "tolerance": {},
        "evaluate": eval_ws_ack,
        "cases": _ack_cases,
    },
    "config_defaults.json": {
        "description": (
            "ClientConfig defaults and validation. input.overrides are constructor arguments; "
            "expected holds the resulting fields (all of them when input.fields is null) or "
            '{"error": "ConfigurationError"}. user_agent defaults to "<sdk-name>/<version>" '
            "(tradesmart-python/<version> in the Python SDK); other SDKs substitute their own. "
            "Durations are seconds. The strings NaN, Infinity and -Infinity in input.overrides "
            "stand for those floating-point values (JSON has none): every duration must be a "
            "finite number > 0 (an integer or a float, never a boolean or a string), "
            "order_rate_limit_max_wait finite and >= 0; order_rate_limit_per_second an integer "
            "1 to 30, above 10 only with algo_id set; order_rate_limit_per_minute an integer 1 to "
            "600 (default 200), never below order_rate_limit_per_second; algo_id a non-blank "
            "string or null (a boolean or number rejected); the count settings (max_retries, "
            "max_connections, max_keepalive_connections, stream_backlog_limit, "
            "script_master_recheck_minutes, reconnect_max_attempts) must be integers (a float, "
            "even 2.0 or NaN, and a boolean are rejected); string settings must be strings and "
            "the two flags booleans. An error case also gives expected.message, the "
            "ConfigurationError text (it names the setting, never echoes the value)."
        ),
        "source": "tradesmart.config.ClientConfig",
        "tolerance": {"abs": 1e-12},
        "evaluate": eval_config,
        "cases": _config_cases,
    },
    "jdata_encoding.json": {
        "description": (
            "REST request body. input.payload is the ordered list of [key, value] string pairs "
            "(the wire key order is kept, never sorted). The body is jData= followed by compact "
            'JSON (separators "," and ":", no spaces), non-ASCII characters written as UTF-8 '
            "(no \\u escapes), no HTML escaping of < > & or ', then only % & + percent-encoded, "
            "in that order (% first). Everything else, including = space / ? #, is sent as is. "
            'JSON string escapes: " and \\ as \\" and \\\\; the control characters U+0008, '
            "U+0009, U+000A, U+000C, U+000D as \\b \\t \\n \\f \\r; every other character below "
            "U+0020 as \\u00XX with lower-case hex; DEL (U+007F), U+2028 and U+2029 are sent raw "
            "as UTF-8. expected.utf8_hex is the exact body bytes."
        ),
        "source": "tradesmart._transport.codec.encode_jdata",
        "tolerance": {},
        "evaluate": eval_jdata,
        "cases": _jdata_cases,
    },
    "token_expiry.json": {
        "description": (
            "Access-token expiry: the first 06:00 IST strictly after the issue instant (IST is "
            "a fixed +05:30, no daylight saving). input.function next_expiry: input.issued_at "
            "is ISO 8601 with any offset; expected.expires_at is ISO 8601 at +05:30, or "
            '{"error": "ValueError"} for an instant without an offset. input.function '
            "is_expired: a token with issued_at / expires_at is expired at now when now >= "
            "expires_at (expected.expired)."
        ),
        "source": "tradesmart.auth.next_expiry / AccessToken.is_expired",
        "tolerance": {},
        "evaluate": eval_token_expiry,
        "cases": _token_expiry_cases,
    },
    "token_file.json": {
        "description": (
            "The shared token file (JSON with client_id, access_token, issued_at, expires_at). "
            "input.function write: the SDK's writer stores a login issued at input.issued_at; "
            "expected holds the two timestamps as written (YYYY-MM-DDTHH:MM:SS+HH:MM: seconds "
            "precision, numeric offset, never Z). input.function read: a file holding the given "
            "issued_at / expires_at texts, as another SDK may write them; expected holds the "
            "instants read, at +05:30, with expires_at capped at the first 06:00 IST after "
            'issued_at, or {"error": "invalid"} when the file is refused. A reader accepts '
            "exactly YYYY-MM-DDTHH:MM:SS, optionally . and 1 to 9 fraction digits (more than 6 "
            "are truncated), then Z, z or +HH:MM / -HH:MM, after trimming surrounding "
            "whitespace; every other form (no offset, an offset without a colon, the basic "
            "format, a space or lower-case t separator, no seconds, 10 or more fraction "
            "digits) is refused, and so is an issued_at more than 5 minutes after the reader's "
            "clock (the 2099 case)."
        ),
        "source": "tradesmart._token_store.save / load",
        "tolerance": {},
        "evaluate": eval_token_file,
        "cases": _token_file_cases,
    },
    "scrip_keys.json": {
        "description": (
            "Stream subscription keys. input.instruments is one 'EXCH|TOKEN' string or a list "
            "of such strings and [exchange, token] pairs. expected.keys are the EXCH|TOKEN keys: "
            "exchange trimmed and upper-cased, duplicates removed (first kept), order kept. An "
            "unknown exchange, a missing |, or an empty token or one with whitespace, # or | is "
            '{"error": "RequestValidationError"}.'
        ),
        "source": "tradesmart.stream.protocol.format_keys",
        "tolerance": {},
        "evaluate": eval_scrip_keys,
        "cases": _scrip_keys_cases,
    },
    "time_conversion.json": {
        "description": (
            "input.function is to_epoch (request times: value is {datetime: ISO with offset}, "
            "{date: YYYY-MM-DD} meaning 00:00 IST, or {epoch: int}; expected.epoch is the "
            "decimal epoch-seconds string, fractions truncated; a datetime without an offset is "
            "RequestValidationError), parse_ist (response text times in the documented formats, "
            "read as IST; unknown formats, blanks and non-strings are null) or parse_epoch "
            "(epoch-seconds fields such as lut / ft; 0 and blank are null; expected.datetime is "
            'UTC; non-numeric text is {"error": "ValueError"}).'
        ),
        "source": "tradesmart.endpoints.market.to_epoch / tradesmart.models.base._parse_ist, "
        "_parse_epoch",
        "tolerance": {},
        "evaluate": eval_time,
        "cases": _time_cases,
    },
    "round_to_tick.json": {
        "description": (
            "round_to_tick(price, tick_size): the nearest multiple of the tick, half-way rounding "
            "away from zero (ROUND_HALF_UP), written with the tick's decimal places (trailing "
            "zeros of the tick ignored). Inputs are strings or JSON numbers (numbers are read "
            "through their shortest decimal text). A tick <= 0 or a non-finite or non-numeric "
            'input is {"error": "ValueError"}. expected.result is compared as text.'
        ),
        "source": "tradesmart.round_to_tick",
        "tolerance": {},
        "evaluate": eval_round_to_tick,
        "cases": _round_to_tick_cases,
    },
    "retry_schedule.json": {
        "description": (
            "Retries of reads. delay: attempt is 0-based (the delay before the 2nd call is "
            "attempt 0); without retry_after the delay is min(cap, base * 2^attempt) times a "
            "uniform random factor in [0.5, 1): expected.min is the factor 0.5, expected.max the "
            "exclusive upper bound; with a finite retry_after (seconds) it is max(0, min(cap, "
            "retry_after)) with no jitter (min = max); a non-finite retry_after (the strings "
            "NaN / Infinity in input) is ignored and the backoff applies. should_retry: true "
            "only for a read (state_changing false) whose error is NetworkError, "
            "RequestTimeoutError, RateLimitError or ServerError, while attempt < max_retries. "
            "retry_after_header: a finite numeric Retry-After header (seconds, sign kept) on an "
            "HTTP 429 RateLimitError sets retry_after; anything else (an HTTP date, nan, inf, "
            "another status) leaves null."
        ),
        "source": "tradesmart._transport.retry.RetryPolicy / "
        "tradesmart._transport.sync_http.apply_retry_after",
        "tolerance": {"abs": 1e-12},
        "evaluate": eval_retry,
        "cases": _retry_cases,
    },
    "order_validation.json": {
        "description": (
            "Order-call arguments (uid TEST123) to the endpoint and the exact request body (see "
            'jdata_encoding.json), or {"error": "RequestValidationError", "fields": [...]} when '
            "client-side validation rejects the arguments before anything is sent; fields "
            "lists the wire keys of the field-level errors, sorted (empty when only a "
            "cross-field rule failed, such as MTF on MCX). input.call is "
            "place_order (PlaceOrder; wire key order uid, actid, exch, tsym, qty, prc, trgprc, "
            "dscqty, prd, trantype, prctyp, ret, amo, remarks, ordersource, blprc, bpprc, "
            "trailprc, mkt_protection, algo_id; amo is always sent as Yes or No), modify_order "
            "(ModifyOrder; no amo; ret only when a validity is given; algo_id last, as on "
            "PlaceOrder; algo_id must be a non-blank string when given), cancel_order "
            "(CancelOrder: uid, norenordno), exit_order (ExitSNOOrder: uid, norenordno, prd H or "
            "B), order_margin (GetOrderMargin, a read), place_gtt (PlaceGTTOrder; wire key order "
            "uid, actid, exch, tsym, ai_t, d, validity, remarks, trantype, prctyp, prd, ret, qty, "
            "prc, trgprc, dscqty; alert_type -> ai_t, trigger_value -> d, alert_validity -> "
            "validity (DAY or GTT, default GTT), validity -> ret (default DAY), tag -> remarks "
            "(required, non-blank); exchanges NSE, NFO, BSE, BFO, MCX and products CNC, NRML, CO "
            "only; quantity in the exchange's trade units, a whole multiple of the lot size, on "
            "MCX too; no ordersource, no algo_id), modify_gtt (ModifyGTTOrder: the same keys "
            "with al_id third; validity and alert_validity have no defaults on modify_gtt, so "
            "every modify_gtt case gives both and both are always sent) or cancel_gtt "
            "(CancelGTTOrder: uid, al_id); GTT case names start with gtt_. Absent optional keys "
            'are omitted; a MARKET / SL-M order without a price is sent with prc "0".'
        ),
        "source": "tradesmart.endpoints.orders.place_order_call / modify_order_call / "
        "cancel_order_call / exit_sno_order_call / order_margin_call; "
        "tradesmart.endpoints.gtt.place_gtt_call / modify_gtt_call / cancel_gtt_call",
        "tolerance": {},
        "evaluate": eval_order_validation,
        "cases": _order_validation_cases,
    },
    "read_payloads.json": {
        "description": (
            "Read-method arguments (uid TEST123) to the endpoint and the exact request body, or "
            '{"error": "RequestValidationError", "fields": [...]} (wire keys, see '
            "order_validation.json) when the arguments are rejected before anything is sent. "
            "input.call names the client method, input.arguments its parameters; a time "
            'argument is {"datetime": ISO 8601} (an offset is required), {"date": YYYY-MM-DD} '
            "(00:00 IST) or an integer of epoch seconds; a float or text time is rejected. Ids "
            "and tokens must be strings (an integer is rejected, never converted). OrderBook "
            "sends uid only; EODChartData sends sym = EXCH:TSYM, from, to and no uid; optional "
            "TPSeries keys (st, et, intrv) are omitted when not given. GetPendingGTTOrder and "
            "GetEnabledGTTs send uid only; GetTrgOi sends uid and actid."
        ),
        "source": "tradesmart.TradeSmart read methods (request captured by an in-memory transport)",
        "tolerance": {},
        "evaluate": eval_read_payload,
        "cases": _read_payload_cases,
    },
    "request_names.json": {
        "description": (
            "The low-level request(endpoint, payload, state_changing, expect) call as built "
            "before sending. input.payload is the ordered list of [key, value] pairs. The "
            "endpoint is trimmed of whitespace and slashes and must then be ASCII letters only "
            '(else {"error": "ConfigurationError"}); the names PlaceOrder, PlaceMultiOrder, '
            "ModifyOrder, CancelOrder, ExitSNOOrder, GenAcsTok, PlaceGTTOrder, ModifyGTTOrder, "
            "CancelGTTOrder, PlaceIceBergOrder, ModifyIceBergOrder, CancelIceBergOrder, "
            "GetQuotes, GetQuotesMultiple and TPSeries match case-insensitively and are "
            "rewritten to that spelling, every other name is sent as given. "
            "expected.state_changing is the caller's flag or a state-changing name; "
            "expected.rate_group is place_order (the orders group), quotes, tpseries or null; "
            "allow_empty is always false; expected.success_stats is the sorted list of "
            "documented success words (['Ok'] except the GTT and iceberg writes; the GTT "
            "words compare ignoring letter case, see success_words.json) and "
            "expected.requires_ack_id the field a success must carry as a non-blank string "
            "(al_id for the GTT writes, else null). uid is appended when "
            "absent; values become wire strings (true / false as Yes / No); a null value is "
            '{"error": "TypeError"} (a programming error, nothing is sent).'
        ),
        "source": "tradesmart._client_core.build_raw_call",
        "tolerance": {},
        "evaluate": eval_request_name,
        "cases": _request_name_cases,
    },
    "outcome_words.json": {
        "description": (
            "The order outcome log (logger tradesmart.orders) of one call. input.call is an "
            "order method (place_order, modify_order, cancel_order, exit_order), a typed GTT "
            "write (place_gtt with the arguments of the order_validation.json gtt_ cases, "
            "modify_gtt the same with alert_id 26111200000010, cancel_gtt that alert_id), "
            "request_place_order (request('placeorder', ..., state_changing=True)), "
            "request_place_gtt (request('placegttorder', {exch: NSE})), request_cancel_gtt "
            "(request('CANCELGTTORDER', {al_id: '26111200000099'})), request_place_iceberg "
            "(request('placeicebergorder', {exch: NSE})), "
            "request_order_book_state_changing (not an order endpoint) or limits (a read). "
            "input.token is valid, none or expired (an AccessToken past its expiry); closed "
            "closes the client first; prior_orders accepted orders are placed first in the "
            "same instant (so an 11th order exceeds the 10 per second limit; their lines are "
            "not shown; order_rate_limit_max_wait is the configured wait cap); replies answer "
            "the call's requests in turn: {status, body} or "
            "{failure: kind} (kinds as in transport_failures.json). expected.info is the "
            "INFO line without its time ('<Endpoint> <word> order_id=<id>', id '-' when none; "
            "'al_id=<id>' for the GTT writes, the ack's al_id else the payload's), "
            "expected.warning the WARNING line (it names pending_gtts() for the GTT writes, "
            "orders() otherwise), both null when not logged; expected.error is "
            "the error type raised (null when the call returned) and expected.requests how "
            "many requests reached the transport."
        ),
        "source": "tradesmart._transport.outcome via tradesmart.TradeSmart (in-memory transport)",
        "tolerance": {},
        "evaluate": eval_outcome,
        "cases": _outcome_cases,
    },
    "success_words.json": {
        "description": (
            "Per-endpoint success words. input.body is the raw reply to input.endpoint with "
            "input.http_status, for a call that expects input.expect (object or list); "
            "expected as in not_ok_classification.json ({returns} or "
            "{error, emsg, message}). Success is a stat string compared exactly "
            "(no trimming, no normalising), except that the three GTT words compare ignoring "
            "letter case only (casefold on both sides; the test environment answers "
            "'OI created'): PlaceGTTOrder 'Oi created', ModifyGTTOrder "
            "'Oi replaced', CancelGTTOrder 'Oi delete success' (each also needs a non-blank "
            "string al_id and no emsg other than a blank string; 'OI deleted' is another "
            "word, not a case variant), PlaceIceBergOrder, ModifyIceBergOrder and "
            "CancelIceBergOrder 'true' (the string, case-sensitive), every other "
            "endpoint 'Ok'. Not_Ok keeps the classification of not_ok_classification.json; "
            "on the three iceberg writes only, 'false' is a definite failure classified the "
            "same way, its message from a non-blank rejreason, else emsg. Any JSON array on "
            "these six writes, whatever expect, and anything else on them "
            "is OrderStatusUnknownError; on a read it is a "
            "ResponseParseError."
        ),
        "source": "tradesmart._transport.codec.parse_response with "
        "tradesmart._client_core.SUCCESS_STATS, SUCCESS_STATS_IGNORE_CASE and ACK_ID_FIELDS",
        "tolerance": {},
        "evaluate": eval_success_word,
        "cases": _success_word_cases,
    },
    "transport_failures.json": {
        "description": (
            "A failure while a request is being sent, by kind: connect_error, connect_timeout "
            "and pool_timeout (nothing reached the server), read_timeout, write_timeout, "
            "connection_reset, remote_protocol_error, decoding_error (the request may have "
            "been executed), hook_exception (a non-SDK exception raised by the caller's own "
            "HTTP event hook or transport), hook_sdk_error (an SDK ServerError raised there) "
            "and keyboard_interrupt (passes through unchanged). input.call is place_order, "
            "cancel_order, place_gtt, modify_gtt, cancel_gtt (arguments as in "
            "outcome_words.json), request_state_changing (request('OrderBook', ..., "
            "state_changing=True)) or read (margins()). expected.error is the error type and "
            "expected.requests how many requests were sent (a read is retried for "
            "NetworkError and RequestTimeoutError: 3 attempts with max_retries 2)."
        ),
        "source": "tradesmart._transport.sync_http (map_transport_error, "
        "map_unexpected_error) via tradesmart.TradeSmart",
        "tolerance": {},
        "evaluate": eval_transport_failure,
        "cases": _transport_cases,
    },
    "script_master_parse.json": {
        "description": (
            "Parsing a script-master text file. input.text is the file content (UTF-8), or "
            "input.bytes_hex the raw bytes. expected.rows are the data rows keyed by the "
            "trimmed header cells (all values trimmed strings; a missing key means the row was "
            'shorter than the header), or {"error": "ResponseParseError"} for a file that '
            "is not UTF-8 text or not valid CSV (a NUL byte). The delimiter is whichever of "
            ", | TAB ; occurs most in the first non-blank line (first 4096 characters); a tie "
            "goes to the first in that order; , when none occurs. RFC 4180 quoting; rows whose "
            "cells are all blank are skipped; a UTF-8 byte-order mark is ignored; with "
            "duplicate header names the last column wins; extra cells are dropped."
        ),
        "source": "tradesmart.instruments.load",
        "tolerance": {},
        "evaluate": eval_script_master_parse,
        "cases": _script_master_parse_cases,
    },
    "script_master_freshness.json": {
        "description": (
            "download_instruments('NSE') with the cached file in a given state (refresh time "
            "07:00 IST, re-check window 15 minutes). input.now is the clock; file_mtime the "
            "cached file's modification time (null: no file); checked the text of the "
            ".checked sidecar (null: none); force the argument; responses answer the requests "
            "in turn ({status, last_modified}: a 200 carries a new file; {failure: kind}; "
            "delete_file removes the cached file before answering). expected.requests lists "
            "each request's If-Modified-Since header (null for a full download); file is "
            "downloaded or cached and file_mtime its modification time afterwards (a download "
            "takes the Last-Modified time), or error is the error type; checked is the "
            "sidecar text afterwards (UTC ISO 8601 of now when a check was recorded); warnings "
            "counts the WARNING lines (one when a failed re-check keeps the cached file)."
        ),
        "source": "tradesmart.instruments.Instruments.download (in-memory transport, clock pinned)",
        "tolerance": {},
        "evaluate": eval_script_master_freshness,
        "cases": _script_master_freshness_cases,
    },
    "stream_backlog.json": {
        "description": (
            "The stream's delivery buffer under backpressure. input.limit is "
            "stream_backlog_limit; input.ops put a wire message (as received) or take n "
            "messages (as the consumer would); the rest is drained at the end. "
            "expected.delivered are the wire payloads in delivery order (a merged entry "
            "carries the merged fields), merged the number of messages merged into an "
            "earlier entry, warnings the backlog WARNINGs in order (merging_started when the "
            "first merge happens, caught_up when the backlog falls below half the limit). While "
            ">= limit messages wait, a tf / df of an instrument (e, tk) is merged into its "
            "newest waiting entry when that is a feed of the same type (rule a), or into its "
            "second-newest when that is a feed of the same type and the newest a feed of the "
            "other type, copying the fields the newest's type can carry into the newest too "
            "(rule b); otherwise it is appended. Acks, order / position updates and unknown "
            "messages are never merged, and nothing merges across an entry of the same "
            "instrument."
        ),
        "source": "tradesmart.stream.delivery.DeliveryBuffer",
        "tolerance": {},
        "evaluate": eval_stream_backlog,
        "cases": _stream_backlog_cases,
    },
    "slice_plan.json": {
        "description": (
            "Splitting a sliced order into slices (place_sliced_order, before anything is sent). "
            "input.quantity, max_slice_quantity, lot_size, freeze_quantity (GetSecurityInfo "
            "frzqty) and the slices are all in trade units on every exchange, MCX included. The "
            "largest slice is floor(frzqty / lot) * lot units (a slice may equal frzqty); "
            "max_slice_quantity (null: none) lowers it and must be a whole number of lots; a "
            "freeze_quantity of null, 0 or negative is unknown, and max_slice_quantity is then "
            "required (a frzqty that is not an integer fails the GetSecurityInfo read before "
            "planning). expected is the list of slices (as many full slices as fit, then the "
            'remainder), or {"error": "ValueError"} when an argument is invalid (quantity, '
            "lot_size, max_slices or max_slice_quantity not an integer >= 1, booleans refused; "
            "quantity not a whole number of lots), the largest slice is below one lot, the "
            "freeze quantity is unknown without max_slice_quantity, or more than max_slices "
            "slices are needed."
        ),
        "source": "tradesmart._slicing.plan_slices",
        "tolerance": {},
        "evaluate": eval_slice_plan,
        "cases": _slice_plan_cases,
    },
}


def build(name: str) -> dict[str, Any]:
    """The full JSON document of one vector file, ``expected`` computed from the SDK."""
    spec = FILES[name]
    cases = []
    for case_name, case_input in spec["cases"]():
        cases.append(
            {"name": case_name, "input": case_input, "expected": spec["evaluate"](case_input)}
        )
    names = [c["name"] for c in cases]
    assert len(names) == len(set(names)), f"{name}: duplicate case names"
    return {
        "description": spec["description"],
        "source": spec["source"],
        "tolerance": spec["tolerance"],
        "cases": cases,
    }


def dump(document: dict[str, Any]) -> str:
    """Deterministic text: sorted keys, 2-space indent, ``repr`` floats, no NaN, final newline."""
    return (
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    )


def _self_checks(documents: dict[str, dict[str, Any]]) -> None:
    """Facts that must hold regardless of what the SDK currently does."""
    by_name = {n: {c["name"]: c for c in d["cases"]} for n, d in documents.items()}
    worked = by_name["checksum.json"]["worked_example_from_official_docs"]["expected"]
    assert worked["checksum"] == "7b482d7b380a3067eaba4c9c909b19253c4fa0edb5833e246401b8497c99a9c3"
    prices = by_name["greeks_prices.json"]
    assert abs(prices["bsm_textbook_hull_call"]["expected"]["price"] - 4.76) < 0.01
    assert abs(prices["bsm_textbook_hull_put"]["expected"]["price"] - 0.81) < 0.01
    assert abs(prices["black76_textbook_hull_put"]["expected"]["price"] - 1.12) < 0.01
    for case in documents["implied_volatility.json"]["cases"]:
        if case["name"].startswith("round_trip_"):
            iv, vol = case["expected"]["iv"], case["input"]["volatility_used"]
            assert iv is not None and abs(iv - vol) < 1e-6, case["name"]
        elif case["name"].startswith("no_iv_"):
            assert case["expected"] == {"iv": None}, case["name"]
        else:
            assert case["expected"] == {"error": "ValueError"}, case["name"]
    for case in documents["greeks_edge_cases.json"]["cases"]:
        if not case["name"].startswith("error_"):
            assert "error" not in case["expected"], case["name"]
    for case in documents["order_validation.json"]["cases"]:
        failed = "error" in case["expected"]
        assert failed == case["name"].removeprefix("gtt_").startswith("error_"), case["name"]
    # Owner decisions 2026-11-12 and 2026-11-13: only the documented words are a success (the
    # GTT words ignoring letter case only).
    for case in documents["success_words.json"]["cases"]:
        name, expected = case["name"], case["expected"]
        if name.endswith("_is_success"):
            assert "returns" in expected, name
        elif name.endswith("_is_unknown"):
            assert expected.get("error") == "OrderStatusUnknownError", name
        elif name.endswith("_is_rejected") or "_is_rejected_" in name:
            assert expected.get("error") == "OrderRejectedError", name


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    documents = {name: build(name) for name in FILES}
    _self_checks(documents)
    for name, document in documents.items():
        (OUT_DIR / name).write_text(dump(document), encoding="utf-8", newline="\n")
        print(f"wrote {name}: {len(document['cases'])} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
