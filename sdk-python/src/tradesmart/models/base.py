"""Pydantic base models and wire field types shared by all request/response models."""

import json
import logging
import re
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, Any, ClassVar, TypeVar

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    PrivateAttr,
    ValidationError,
    ValidatorFunctionWrapHandler,
    model_validator,
)

from tradesmart._logging import _MASK, SENSITIVE_KEYS, redact
from tradesmart.enums import (
    GttAlertType,
    GttValidity,
    OrderStatus,
    OrderType,
    Product,
    TransactionType,
    Validity,
)
from tradesmart.errors import RequestValidationError, ResponseParseError

_log = logging.getLogger("tradesmart")

# Stream "price not set" sentinel (an unsigned 32-bit value / 100): means "ATO", not a price.
# Only quote/depth and order-update price fields use it (``StreamPrice``); money amounts such
# as ``rpnl`` or ``daybuyamt`` are plain ``Price`` and keep the value.
ATO_PRICE = Decimal("42949672.95")

IST = timezone(timedelta(hours=5, minutes=30), "IST")

_IST_FORMATS = (
    "%H:%M:%S %d-%m-%Y",
    "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%d-%m-%Y",
    "%d-%b-%Y",
)


def _parse_price(value: Any) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError(f"invalid price: {value!r}")
    if isinstance(value, (str, int, float)):
        try:
            result = Decimal(str(value).strip())
        except InvalidOperation:
            raise ValueError(f"invalid price: {value!r}") from None
        if not result.is_finite():
            raise ValueError(f"invalid price: {value!r}")
        return result
    raise ValueError(f"invalid price: {value!r}")


def _parse_quantity(value: Any) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"invalid quantity: {value!r}")
    if isinstance(value, int):
        return value
    number = _parse_price(value)
    if number is None or number != number.to_integral_value():
        raise ValueError(f"quantity is not integral: {value!r}")
    return int(number)


def _parse_epoch(value: Any) -> datetime | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value
    try:
        seconds = int(Decimal(str(value).strip()))
    except (ArithmeticError, ValueError):  # InvalidOperation; OverflowError for "inf"
        raise ValueError(f"invalid epoch time: {value!r}") from None
    if seconds == 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise ValueError(f"invalid epoch time: {value!r}") from None


_WARNED_TIMESTAMP_SHAPES: set[str] = set()
_MAX_TIMESTAMP_SHAPES = 50  # bounded: a feed of ever-new shapes must not grow memory or logs


def _warn_unknown_timestamp(text: str) -> None:
    """Log once per distinct shape (digits as 9), for at most 50 shapes; never the raw value."""
    shape = re.sub(r"\d", "9", text)[:40]
    if shape in _WARNED_TIMESTAMP_SHAPES or len(_WARNED_TIMESTAMP_SHAPES) >= _MAX_TIMESTAMP_SHAPES:
        return
    _WARNED_TIMESTAMP_SHAPES.add(shape)
    _log.warning("Unrecognised timestamp format %r; value left as None", shape)
    if len(_WARNED_TIMESTAMP_SHAPES) == _MAX_TIMESTAMP_SHAPES:
        _log.warning(
            "%d unrecognised timestamp formats logged; further ones are not logged",
            _MAX_TIMESTAMP_SHAPES,
        )


def _parse_ist(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    for fmt in _IST_FORMATS:
        try:
            parsed = datetime.strptime(text.title() if "%b" in fmt else text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=IST)
    if text:
        _warn_unknown_timestamp(text)
    return None


def _parse_stream_price(value: Any) -> Decimal | None:
    result = _parse_price(value)
    return None if result == ATO_PRICE else result


def _lenient_str(value: Any) -> Any:
    """Coerce JSON numbers to ``str`` (ids/text); bools and other types are left to validation."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    return value


def _enum_or_str(enum_cls: "type[Enum]") -> Callable[[Any], Any]:
    """Build a validator: known value -> enum member, unknown -> the original string.

    ``None`` and blank strings become ``None``; unknown values never fail validation.
    """

    def convert(value: Any) -> Any:
        if value is None or isinstance(value, enum_cls):
            return value
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            return value
        if not value.strip():
            return None
        try:
            return enum_cls(value)
        except ValueError:
            return value

    return convert


Price = Annotated[Decimal | None, BeforeValidator(_parse_price)]
TransactionTypeField = Annotated[
    TransactionType | str | None, BeforeValidator(_enum_or_str(TransactionType))
]
OrderTypeField = Annotated[OrderType | str | None, BeforeValidator(_enum_or_str(OrderType))]
ProductField = Annotated[Product | str | None, BeforeValidator(_enum_or_str(Product))]
ValidityField = Annotated[Validity | str | None, BeforeValidator(_enum_or_str(Validity))]
OrderStatusField = Annotated[OrderStatus | str | None, BeforeValidator(_enum_or_str(OrderStatus))]
GttAlertTypeField = Annotated[
    GttAlertType | str | None, BeforeValidator(_enum_or_str(GttAlertType))
]
GttValidityField = Annotated[GttValidity | str | None, BeforeValidator(_enum_or_str(GttValidity))]
LenientStr = Annotated[str | None, BeforeValidator(_lenient_str)]
Volume = Annotated[Decimal | None, BeforeValidator(_parse_price)]
StreamPrice = Annotated[Decimal | None, BeforeValidator(_parse_stream_price)]
Quantity = Annotated[int | None, BeforeValidator(_parse_quantity)]
EpochTime = Annotated[datetime | None, BeforeValidator(_parse_epoch)]
ISTDateTime = Annotated[datetime | None, BeforeValidator(_parse_ist)]

PriceInput = Decimal | int | float | str

# A required, parsed price/amount (`None` and blanks fail validation).
Money = Annotated[Decimal, BeforeValidator(_parse_price)]


def _reject_bool(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError(f"invalid quantity: {value!r}")
    return value


# A required integer request quantity; `True`/`False` are rejected (they are `int` subclasses).
StrictQuantity = Annotated[int, BeforeValidator(_reject_bool)]

M = TypeVar("M", bound="TradeSmartModel")
R = TypeVar("R", bound="RequestModel")


# ``id()`` of the payload ``from_wire`` is validating: its raw is set by ``from_wire`` itself,
# so the nested-raw validator skips that (top-level) object.
_FROM_WIRE_PAYLOAD: ContextVar[int | None] = ContextVar("_FROM_WIRE_PAYLOAD", default=None)


class TradeSmartModel(BaseModel):
    """Base for response models: tolerant of new fields, keeps the raw payload."""

    model_config = ConfigDict(extra="allow", populate_by_name=True, frozen=True)

    _raw: dict[str, Any] = PrivateAttr(default_factory=dict)

    @property
    def raw(self) -> dict[str, Any]:
        """The original wire payload (shallow copy), for nested records too."""
        return self._raw

    @model_validator(mode="wrap")
    @classmethod
    def _keep_raw(cls, data: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        """Store the input object as ``raw`` on every model, nested ones included.

        Values under secret keys (e.g. ``access_token``) are masked. The top-level payload of
        ``from_wire`` is skipped (``from_wire`` sets the original payload itself, also after
        the lenient retry), so a record or tick pays for the copy of nested records only.
        """
        model = handler(data)
        if (
            isinstance(data, Mapping)
            and id(data) != _FROM_WIRE_PAYLOAD.get()
            and isinstance(model, TradeSmartModel)
        ):
            model._raw = {
                k: (_MASK if isinstance(k, str) and k.lower() in SENSITIVE_KEYS else v)
                for k, v in data.items()
            }
        return model

    # Records that are costly to lose (orders, trades, positions, holdings, order and
    # position updates) set this: a top-level field that fails validation becomes ``None``
    # (its original value stays in ``raw``) instead of failing the whole record.
    lenient_parsing: ClassVar[bool] = False
    # Identity wire keys the lenient fallback never nulls: if one of them fails, the
    # record fails as a whole (``ResponseParseError``, or ``UnknownMessage`` on a stream).
    lenient_protected_keys: ClassVar[frozenset[str]] = frozenset({"norenordno", "status"})

    @classmethod
    def _try_validate(
        cls: "type[M]", payload: dict[str, Any]
    ) -> "tuple[M | None, list[tuple[int | str, ...]]]":
        """Validate ``payload``; on failure return only the error locations (never values)."""
        token = _FROM_WIRE_PAYLOAD.set(id(payload))
        try:
            return cls.model_validate(payload), []
        except ValidationError as exc:
            return None, [tuple(e["loc"]) for e in exc.errors()]
        finally:
            _FROM_WIRE_PAYLOAD.reset(token)

    @classmethod
    def _failed_keys(
        cls, payload: Mapping[str, Any], locs: "list[tuple[int | str, ...]]"
    ) -> set[str] | None:
        """Payload keys of the failing top-level fields, or ``None`` if one cannot be nulled.

        A field cannot be nulled when it has no top-level key in the payload or its wire key
        is in ``lenient_protected_keys``.
        """
        keys: set[str] = set()
        for loc in locs:
            if not loc or not isinstance(loc[0], str):
                return None
            key = loc[0]
            if key not in payload:
                info = cls.model_fields.get(key)
                key = info.alias if info is not None and info.alias else key
            if key not in payload:
                return None  # e.g. a missing required field: nothing to null
            if key in cls.lenient_protected_keys:
                return None  # an identity field is never nulled
            keys.add(key)
        return keys

    @classmethod
    def from_wire(cls: "type[M]", data: Mapping[str, Any], *, endpoint: str = "") -> M:
        """Validate a wire payload, wrapping failures in ``ResponseParseError``.

        With ``lenient_parsing`` the failing top-level fields are set to ``None`` and the
        payload is validated once more; one WARNING names those fields (never values) and
        ``raw`` keeps the original payload. If that also fails, the error is raised.

        The ``ValidationError`` embeds raw input values (possibly secrets), so the
        ``ResponseParseError`` is raised outside the ``except`` block: it has neither a
        ``__cause__`` nor a ``__context__``. Only field names and a redacted body are kept.
        A ``data`` that is not a JSON object (e.g. a list item that is a string or a number)
        raises ``ResponseParseError`` too.
        """
        if not isinstance(data, Mapping):
            raise ResponseParseError(
                f"Could not parse {endpoint or cls.__name__} response (expected a JSON object)",
                endpoint=endpoint,
                http_status=None,
                body=json.dumps(redact(data), default=str),
            )
        payload = dict(data)
        model, locs = cls._try_validate(payload)
        if model is None and cls.lenient_parsing:
            keys = cls._failed_keys(payload, locs)
            if keys:
                retry = {k: (None if k in keys else v) for k, v in payload.items()}
                model, _ = cls._try_validate(retry)
                if model is not None:
                    _log.warning(
                        "Could not parse field(s) %s of a %s record; set to None "
                        "(original value kept in .raw)",
                        ", ".join(sorted(keys)),
                        endpoint or cls.__name__,
                    )
        if model is None:
            fields = ", ".join(sorted({".".join(str(p) for p in loc) for loc in locs}))
            raise ResponseParseError(
                f"Could not parse {endpoint or cls.__name__} response (fields: {fields})",
                endpoint=endpoint,
                http_status=None,
                body=json.dumps(redact(payload), default=str),
            )
        model._raw = payload
        return model


def to_wire_value(value: Any) -> str:
    """Convert a Python value to its wire string form."""
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return format(Decimal(str(value)), "f")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    raise TypeError(f"cannot convert {type(value).__name__} to a wire value")


class RequestModel(BaseModel):
    """Base for request models: strict, immutable, serialises to string wire params."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    def to_wire(self) -> dict[str, str]:
        dumped = self.model_dump(by_alias=True, exclude_none=True)
        return {key: to_wire_value(val) for key, val in dumped.items()}


def validate_request(model_cls: "type[R]", **kwargs: Any) -> R:
    """Build a request model, raising ``RequestValidationError`` on bad input."""
    try:
        return model_cls(**kwargs)
    except ValidationError as exc:
        errors = exc.errors(include_url=False)
        # Model-level rule failures have no field location: report their (secret-free)
        # message instead so the caller can see which rule was broken.
        fields = ", ".join(
            sorted(
                {
                    ".".join(str(p) for p in e["loc"])
                    or str(e["msg"]).removeprefix("Value error, ")
                    for e in errors
                }
            )
        )
        error = RequestValidationError([dict(e) for e in errors])
        error.args = (f"Invalid request parameters: {fields}",)
        raise error from exc
