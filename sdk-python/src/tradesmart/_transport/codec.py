"""Sans-IO codec: jData request encoding and response classification."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Literal

from tradesmart._logging import redact
from tradesmart.errors import (
    APIError,
    AuthenticationError,
    NotFoundError,
    OrderRejectedError,
    OrderStatusUnknownError,
    RateLimitError,
    RequestValidationError,
    ResponseParseError,
    ServerError,
)
from tradesmart.models.base import WireValue

_AUTH_EMSG = re.compile(
    r"session expired|invalid access token|invalid session|invalid user id|invalid acct id",
    re.IGNORECASE,
)
# Kept on an error instead of a body nested too deeply to redact (RecursionError).
TOO_DEEP = "<response nested too deeply to show>"

# The success ``stat`` of every endpoint without its own documented word.
OK_STATS: frozenset[str] = frozenset({"Ok"})
# The GTT and OCO write endpoints: an unknown outcome is checked with pending_gtts(), not orders().
ALERT_WRITE_ENDPOINTS = frozenset(
    {
        "PlaceGTTOrder",
        "ModifyGTTOrder",
        "CancelGTTOrder",
        "PlaceOCOOrder",
        "ModifyOCOOrder",
        "CancelOCOOrder",
    }
)
# The iceberg write endpoints answer ``true`` / ``false``: ``false`` is a definite rejection
# (reason in ``rejreason``) for these names only, never for another endpoint whose word is "true".
ICEBERG_WRITE_ENDPOINTS = frozenset(
    {"PlaceIceBergOrder", "ModifyIceBergOrder", "CancelIceBergOrder"}
)
_ICEBERG_FAILURE_STAT = "false"


def check_hint(endpoint: str) -> str:
    """The read a caller uses to find out what an ambiguous call did."""
    return "pending_gtts()" if endpoint in ALERT_WRITE_ENDPOINTS else "orders()"


def _redact_body(text: str) -> str:
    """Mask secrets in a response body before it is stored on an error.

    A body nested too deeply for ``json``/``redact`` (``RecursionError``) is redacted as text.
    """
    try:
        data = json.loads(text)
        if isinstance(data, (dict, list)):
            return json.dumps(redact(data), separators=(",", ":"), ensure_ascii=False)
    except (ValueError, RecursionError):
        pass
    return str(redact(text))


def redact_body(value: Any) -> str:
    """Redacted text for an error ``body``: parsed JSON via ``redact()``, else text redaction."""
    if isinstance(value, str):
        return _redact_body(value)
    try:
        return json.dumps(redact(value), separators=(",", ":"), ensure_ascii=False, default=str)
    except RecursionError:
        return TOO_DEEP


def _redact_raw(raw: Any) -> Any:
    """Mask secrets in a payload (parsed JSON or text) kept on an error as ``raw``.

    A payload nested too deeply to redact is replaced by ``TOO_DEEP``.
    """
    if isinstance(raw, str):
        return _redact_body(raw)
    try:
        return redact(raw)
    except RecursionError:
        return TOO_DEEP


def encode_jdata(payload: Mapping[str, WireValue]) -> str:
    """Encode ``payload`` as the ``jData=<json>`` form body.

    Only ``%``, ``&`` and ``+`` are escaped (in that order); everything else is sent as-is.
    """
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    text = text.replace("%", "%25").replace("&", "%26").replace("+", "%2B")
    return "jData=" + text


def _is_utf8(value: WireValue) -> bool:
    """Whether every key and string at any depth of ``value`` encodes as UTF-8."""
    stack: list[object] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            try:
                current.encode("utf-8")
            except UnicodeEncodeError:  # a lone surrogate, e.g. from a mis-decoded file name
                return False
        elif isinstance(current, dict):
            for key, item in current.items():
                stack.append(key)
                stack.append(item)
        elif isinstance(current, list):
            stack.extend(current)
        else:  # not a wire value: invalid, never iterated
            return False
    return True


def encode_request_body(payload: Mapping[str, WireValue]) -> bytes:
    """The UTF-8 bytes of the ``jData`` body, encoded before anything is sent.

    Raises ``RequestValidationError`` naming each top-level field whose key or value (at any
    depth) cannot be encoded as UTF-8 (a lone surrogate), so the failure is definitive:
    nothing was sent. The value is never echoed.
    """
    bad = sorted(
        str(key) for key, value in payload.items() if not (_is_utf8(key) and _is_utf8(value))
    )
    if bad:
        error = RequestValidationError(
            [
                {"type": "string_unicode", "loc": (key,), "msg": "text is not valid UTF-8"}
                for key in bad
            ]
        )
        error.args = (f"Invalid request parameters: {', '.join(bad)} (text is not valid UTF-8)",)
        raise error
    return encode_jdata(payload).encode("utf-8")


def failure_message(endpoint: str, emsg: str | None) -> str:
    """``<Endpoint> failed: <emsg>``, or a fixed text when ``emsg`` is missing or blank."""
    if emsg is not None and emsg.strip():
        return f"{endpoint} failed: {emsg}"
    return f"{endpoint} failed (no error message from the server)"


def _status_error(
    cls: type[APIError],
    *,
    message: str,
    endpoint: str,
    status_code: int,
    emsg: str | None,
    request_time: str | None,
    raw: Any,
) -> APIError:
    return cls(
        message,
        endpoint=endpoint,
        http_status=status_code,
        emsg=emsg,
        request_time=request_time,
        raw=_redact_raw(raw),
    )


def _class_for_status(status_code: int) -> type[APIError] | None:
    if status_code == 429:
        return RateLimitError
    if status_code in (401, 403):
        return AuthenticationError
    if status_code >= 500:
        return ServerError
    return None


def _unexpected(
    *, endpoint: str, status_code: int, text: str, what: str, raw: Any = None
) -> Exception:
    cls = _class_for_status(status_code)
    if cls is not None:
        return _status_error(
            cls,
            message=f"{endpoint} failed: HTTP {status_code}",
            endpoint=endpoint,
            status_code=status_code,
            emsg=None,
            request_time=None,
            raw=text if raw is None else raw,
        )
    return ResponseParseError(
        f"{endpoint}: {what}", endpoint=endpoint, http_status=status_code, body=_redact_body(text)
    )


def _ambiguous(*, endpoint: str, status_code: int, text: str, what: str) -> OrderStatusUnknownError:
    """A state-changing call got a body that neither confirms nor rejects it."""
    return OrderStatusUnknownError(
        endpoint,
        f"{what} (HTTP {status_code}); the request may have been executed, "
        f"check {check_hint(endpoint)} before retrying",
        http_status=status_code,
        body=_redact_body(text),
    )


def _error_status_list(*, endpoint: str, status_code: int, text: str, data: list[Any]) -> APIError:
    """A JSON list on an HTTP error status: classify by status, never return it as data."""
    cls = _class_for_status(status_code) or (NotFoundError if status_code == 404 else APIError)
    return _status_error(
        cls,
        message=f"{endpoint} failed: HTTP {status_code}",
        endpoint=endpoint,
        status_code=status_code,
        emsg=None,
        request_time=None,
        raw=data,
    )


def parse_response(
    *,
    endpoint: str,
    status_code: int,
    text: str,
    expect: Literal["object", "list"],
    allow_empty: bool,
    state_changing: bool,
    success_stats: frozenset[str] = OK_STATS,
    requires_ack_id: str | None = None,
    stats_ignore_case: bool = False,
    failure_stats: frozenset[str] = frozenset(),
) -> Any:
    """Parse and classify a response body; return a ``dict`` or ``list`` or raise.

    Success is a ``stat`` string in ``success_stats`` (compared exactly: ``Ok`` for most
    endpoints, the documented word for the GTT and iceberg write endpoints; with
    ``stats_ignore_case`` only letter case is ignored, ``casefold()`` on both sides, nothing
    trimmed or normalised: the GTT and OCO writes, spec F32); a ``stat`` in ``failure_stats``
    (``Invalid Oi``, compared with ``casefold()``) is checked first and raises
    ``OrderRejectedError`` whatever else the body holds; with
    ``requires_ack_id`` the success object must also carry that field as a non-blank string
    and no ``emsg`` other than a blank string. ``Not_Ok`` (and ``false`` on the iceberg write
    endpoints, its message from ``rejreason``, else ``emsg``) is a definite failure; any JSON
    list on an endpoint with its own success words is ambiguous. For ``state_changing``
    calls any other body (non-JSON, empty, unexpected shape or word, list) raises
    ``OrderStatusUnknownError`` because the request may have been executed. A body nested
    too deeply for the JSON parser counts as not valid JSON.
    """
    data: Any = None
    parsed = False
    try:
        data = json.loads(text)
        parsed = True
    except (ValueError, RecursionError):  # RecursionError: nested too deeply to parse
        pass
    if not parsed:
        what = "response is not valid JSON"
        # Raised outside the handler: the JSONDecodeError's ``doc`` is the unredacted body,
        # so it must not be kept as ``__context__``.
        if state_changing:
            raise _ambiguous(
                endpoint=endpoint, status_code=status_code, text=text, what=what
            ) from None
        raise _unexpected(
            endpoint=endpoint, status_code=status_code, text=text, what=what
        ) from None

    if isinstance(data, list):
        # An endpoint with its own success words (GTT, iceberg writes) acknowledges with an
        # object: any list is ambiguous there, whatever ``expect`` says.
        own_words = success_stats != OK_STATS or requires_ack_id is not None
        if own_words or (state_changing and (status_code >= 400 or expect == "object")):
            raise _ambiguous(
                endpoint=endpoint,
                status_code=status_code,
                text=text,
                what="expected a JSON object but received a list",
            )
        if status_code >= 400:
            raise _error_status_list(
                endpoint=endpoint, status_code=status_code, text=text, data=data
            )
        if expect == "list" or (allow_empty and not data):
            return data
        raise ResponseParseError(
            f"{endpoint}: expected a JSON object but received a list",
            endpoint=endpoint,
            http_status=status_code,
            body=_redact_body(text),
        )

    what = "unexpected response shape"
    if isinstance(data, dict):
        stat = data.get("stat")
        # A documented-by-observation rejection word (``Invalid Oi`` on the alert modify and
        # cancel writes): a definite rejection, checked before the success words; ``stat`` is
        # compared with ``casefold()`` only (nothing trimmed), so a padded word stays unknown.
        if (
            isinstance(stat, str)
            and failure_stats
            and stat.casefold() in {word.casefold() for word in failure_stats}
        ):
            raise _status_error(
                OrderRejectedError,
                message="Invalid Oi",
                endpoint=endpoint,
                status_code=status_code,
                emsg=stat,
                request_time=(
                    data["request_time"] if isinstance(data.get("request_time"), str) else None
                ),
                raw=data,
            )
        # isinstance first: a list or object ``stat`` cannot be looked up in a set.
        success = isinstance(stat, str) and (
            stat in success_stats
            or (
                stats_ignore_case and stat.casefold() in {word.casefold() for word in success_stats}
            )
        )
        if success and requires_ack_id is not None:
            problem = _ack_problem(data, requires_ack_id)
            if problem is not None:
                success = False
                what = f"success word {problem}"
        if success:
            if expect == "object":
                return data
            if state_changing:  # an Ok answer of the wrong shape: it may have been executed
                raise _ambiguous(
                    endpoint=endpoint,
                    status_code=status_code,
                    text=text,
                    what="expected a JSON list but received an object",
                )
            raise ResponseParseError(
                f"{endpoint}: expected a JSON list but received an object",
                endpoint=endpoint,
                http_status=status_code,
                body=_redact_body(text),
            )
        if stat == "Not_Ok":
            return _classify_not_ok(
                data,
                endpoint=endpoint,
                status_code=status_code,
                allow_empty=allow_empty,
                state_changing=state_changing,
            )
        if stat == _ICEBERG_FAILURE_STAT and endpoint in ICEBERG_WRITE_ENDPOINTS:
            return _classify_not_ok(
                data,
                endpoint=endpoint,
                status_code=status_code,
                allow_empty=False,
                state_changing=state_changing,
                reason_keys=("rejreason", "emsg"),
            )

    if state_changing:
        raise _ambiguous(endpoint=endpoint, status_code=status_code, text=text, what=what)
    raise _unexpected(endpoint=endpoint, status_code=status_code, text=text, what=what)


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _ack_problem(data: dict[str, Any], ack_field: str) -> str | None:
    """Why a success word does not make a success (``None`` when it does)."""
    ack = data.get(ack_field)
    if not isinstance(ack, str) or not ack.strip():
        return f"without a non-blank {ack_field}"
    if not _is_blank(data.get("emsg")):
        return "with an error message"
    return None


def _reason(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """The first non-blank string among ``keys``, else the first string, else ``None``."""
    texts = [value for value in (data.get(key) for key in keys) if isinstance(value, str)]
    for text in texts:
        if text.strip():
            return text
    return texts[0] if texts else None


def _classify_not_ok(
    data: dict[str, Any],
    *,
    endpoint: str,
    status_code: int,
    allow_empty: bool,
    state_changing: bool,
    reason_keys: tuple[str, ...] = ("emsg",),
) -> list[Any]:
    emsg = _reason(data, reason_keys)
    if allow_empty and emsg is not None and "no data" in emsg.lower():
        return []

    cls: type[APIError]
    if status_code in (401, 403) or (emsg is not None and _AUTH_EMSG.search(emsg)):
        cls = AuthenticationError
    elif status_code == 429:
        cls = RateLimitError
    elif status_code >= 500:
        # A JSON Not_Ok is a definitive answer: the order was not accepted.
        cls = OrderRejectedError if state_changing else ServerError
    elif status_code == 404:
        cls = NotFoundError
    elif state_changing:
        cls = OrderRejectedError
    else:
        cls = APIError

    request_time = data.get("request_time")
    raise _status_error(
        cls,
        message=failure_message(endpoint, emsg),
        endpoint=endpoint,
        status_code=status_code,
        emsg=emsg,
        request_time=request_time if isinstance(request_time, str) else None,
        raw=data,
    )
