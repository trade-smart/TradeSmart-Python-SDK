"""Sans-IO WebSocket protocol: request builders, key formatting, message parsing."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, cast

from tradesmart._logging import get_logger
from tradesmart.enums import Exchange
from tradesmart.errors import RequestValidationError, ResponseParseError
from tradesmart.models.stream import (
    ConnectAck,
    DepthAck,
    DepthFeed,
    HeartbeatAck,
    OrderUpdate,
    PositionAck,
    PositionUpdate,
    StreamMessage,
    TouchlineAck,
    TouchlineFeed,
    UnknownMessage,
    UnsubscribeAck,
)

_log = get_logger("stream")

InstrumentKey = str | tuple[Exchange | str, str]
# One instrument or an iterable of them (subscribe/unsubscribe arguments).
Instruments = InstrumentKey | Iterable[InstrumentKey]

_MODELS: dict[str, type[StreamMessage]] = {
    "ak": ConnectAck,
    "tk": TouchlineAck,
    "tf": TouchlineFeed,
    "dk": DepthAck,
    "df": DepthFeed,
    "om": OrderUpdate,
    "pk": PositionAck,
    "pm": PositionUpdate,
    "uk": UnsubscribeAck,
    "udk": UnsubscribeAck,
    "uok": UnsubscribeAck,
    "upk": UnsubscribeAck,
    "hk": HeartbeatAck,
}


def _invalid(index: int, reason: str) -> RequestValidationError:
    error = RequestValidationError(
        [{"type": "value_error", "loc": ("instruments", index), "msg": reason}]
    )
    error.args = (f"Invalid request parameters: instruments.{index} ({reason})",)
    return error


def _split(item: object, index: int) -> tuple[str, str]:
    if isinstance(item, str):
        exch, sep, token = item.partition("|")
        if not sep:
            raise _invalid(index, "expected EXCH|TOKEN")
        return exch, token
    if isinstance(item, tuple) and len(item) == 2:
        raw_exch, token = item
        exch = raw_exch.value if isinstance(raw_exch, Exchange) else raw_exch
        if isinstance(exch, str) and isinstance(token, str):
            return exch, token
        raise _invalid(index, "exchange and token must be strings")
    raise _invalid(index, "expected 'EXCH|TOKEN' or (exchange, token)")


def as_instrument_list(instruments: Instruments) -> list[InstrumentKey]:
    """One instrument (``"NSE|22"`` or ``("NSE", "22")``) or an iterable of them, as a list.

    A 2-tuple of strings whose first item has no ``|`` is one ``(exchange, token)``
    instrument; ``("NSE|22", "NSE|23")`` stays two instruments.
    """
    if isinstance(instruments, str):
        return [instruments]
    if (
        isinstance(instruments, tuple)
        and len(instruments) == 2
        and all(isinstance(part, str) for part in instruments)
        and "|" not in instruments[0]
    ):
        return [cast(InstrumentKey, instruments)]
    try:
        items = iter(instruments)
    except TypeError:  # not iterable (an int, None, ...)
        raise _invalid(0, "expected 'EXCH|TOKEN', (exchange, token) or a list of them") from None
    return list(items)


def format_keys(instruments: Instruments) -> list[str]:
    """Validate instruments and return ordered, de-duplicated ``EXCH|TOKEN`` keys.

    The exchange is accepted in any case and always sent upper-case (``nse|22`` → ``NSE|22``).
    """
    keys: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(as_instrument_list(instruments)):
        raw_exch, token = _split(item, index)
        try:
            exch = Exchange(raw_exch).value
        except ValueError:
            raise _invalid(index, "unknown exchange") from None
        if not token or "|" in token or "#" in token or any(c.isspace() for c in token):
            raise _invalid(index, "invalid token")
        key = f"{exch}|{token}"
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def connect_msg(uid: str, access_token: str, source: str = "API") -> dict[str, str]:
    return {"t": "a", "uid": uid, "actid": uid, "source": source, "accesstoken": access_token}


def touchline_msg(keys: Iterable[str]) -> dict[str, str]:
    return {"t": "t", "k": "#".join(keys)}


def unsubscribe_touchline_msg(keys: Iterable[str]) -> dict[str, str]:
    return {"t": "u", "k": "#".join(keys)}


def depth_msg(keys: Iterable[str]) -> dict[str, str]:
    return {"t": "d", "k": "#".join(keys)}


def unsubscribe_depth_msg(keys: Iterable[str]) -> dict[str, str]:
    return {"t": "ud", "k": "#".join(keys)}


def order_updates_msg(actid: str) -> dict[str, str]:
    return {"t": "o", "actid": actid}


def unsubscribe_orders_msg() -> dict[str, str]:
    return {"t": "uo"}


def position_updates_msg(uid: str) -> dict[str, str]:
    return {"t": "p", "uid": uid}


def unsubscribe_positions_msg() -> dict[str, str]:
    return {"t": "up"}


def heartbeat_msg() -> dict[str, str]:
    return {"t": "h"}


def _unknown(data: Mapping[str, Any]) -> UnknownMessage:
    t = data.get("t")
    msg = UnknownMessage(t=t if isinstance(t, str) else "")
    msg._raw = dict(data)
    return msg


def parse_message(raw: str | bytes | Mapping[str, Any]) -> StreamMessage:
    """Parse an inbound frame into a typed message. Never raises."""
    data: Any = raw
    if isinstance(raw, (str, bytes)):
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        try:
            data = json.loads(text)
        except ValueError:
            return _unknown({"text": text})
        if not isinstance(data, Mapping):
            return _unknown({"text": text})
    elif not isinstance(data, Mapping):
        return _unknown({"text": repr(raw)})
    t = data.get("t")
    model = _MODELS.get(t) if isinstance(t, str) else None
    if model is None:
        return _unknown(data)
    try:
        return model.from_wire(data)
    except ResponseParseError as exc:
        detail = exc.message  # names the failing fields only, never their values
    except Exception as exc:  # defensive: a frame must never kill the reader
        detail = type(exc).__name__
    _log.warning("Unparseable %r stream message delivered as UnknownMessage: %s", t, detail)
    return _unknown(data)
