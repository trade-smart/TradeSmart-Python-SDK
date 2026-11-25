"""Generate ``specifications/models.md`` from the pydantic models (developer script).

Not shipped (excluded from the wheel and sdist). Run it in Docker from ``sdk-python/``::

    docker compose run --rm dev python scripts/gen_model_tables.py

One table per response model and stream message (field name, wire key, type, required,
lenient), one per Greeks/analytics and order result record (field, type, nullable), one
table of every wire enum with its wire values and one per value enum (``StreamState``,
``Buildup``). The output is deterministic
(definition order, no timestamps), so a run with unchanged models leaves ``git diff`` empty.
``tests/unit/test_model_tables.py`` fails when the committed file differs from ``render()``.
The same run writes ``specifications/models.json`` (``render_json()``), the machine-readable form
of the same tables for generators in other languages.
"""

# ruff: noqa: E402
from __future__ import annotations

import sys
import types
import typing
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

SDK_ROOT = Path(__file__).resolve().parents[1]
OUT_FILE = SDK_ROOT.parent / "specifications" / "models.md"
JSON_FILE = SDK_ROOT.parent / "specifications" / "models.json"

import dataclasses
import inspect
import json
import re

from pydantic import BaseModel, SecretStr

from tradesmart import analytics, auth, enums, greeks
from tradesmart._slicing import SlicedOrderResult
from tradesmart.models import base, gtt, market, orders, portfolio, stream, user
from tradesmart.models.base import TradeSmartModel
from tradesmart.models.oco_rows import OcoLegRow, OcoSide, OcoTrigger
from tradesmart.stream.state import StreamState

# Sections in reading order: (title, module, endpoint note).
SECTIONS: tuple[tuple[str, types.ModuleType], ...] = (
    ("Authentication (`GenAcsTok`)", auth),
    ("User (`UserDetails`)", user),
    ("Portfolio (`Limits`, `Holdings`, `PositionBook`)", portfolio),
    ("Orders (`OrderBook`, `SingleOrdHist`, `TradeBook`, `GetOrderMargin`, order acks)", orders),
    ("GTT orders (`GetPendingGTTOrder`, `GetTrgOi`)", gtt),
    (
        "Market data (`SearchScrip`, `GetQuotes`, `GetSecurityInfo`, `GetOptionChain`, "
        "`TPSeries`, `EODChartData`)",
        market,
    ),
    ("Stream messages (WebSocket)", stream),
)

# The shared wire field types of ``tradesmart.models.base`` (matched by identity).
_ALIASES: tuple[tuple[Any, str], ...] = (
    (base.Price, "Price"),
    (base.Volume, "Volume"),
    (base.StreamPrice, "StreamPrice"),
    (base.Quantity, "Quantity"),
    (base.EpochTime, "EpochTime"),
    (base.ISTDateTime, "ISTDateTime"),
    (base.LenientStr, "str"),
    (base.TransactionTypeField, "enum TransactionType"),
    (base.OrderTypeField, "enum OrderType"),
    (base.ProductField, "enum Product"),
    (base.ValidityField, "enum Validity"),
    (base.OrderStatusField, "enum OrderStatus"),
    (base.GttAlertTypeField, "enum GttAlertType"),
    (base.GttValidityField, "enum GttValidity"),
)

_WIRE_ENUMS: tuple[type[Enum], ...] = (
    enums.Exchange,
    enums.TransactionType,
    enums.OrderType,
    enums.Validity,
    enums.Product,
    enums.OrderStatus,
    enums.GttAlertType,
    enums.GttValidity,
)

# Enums the SDK returns but never sends: exact values, no accepted words.
_VALUE_ENUMS: tuple[type[Enum], ...] = (StreamState, analytics.Buildup)

# Result records of the recommended modules (frozen dataclasses, not wire models).
_RESULT_TYPES: tuple[type[Any], ...] = (
    greeks.Greeks,
    greeks.OptionGreeks,
    analytics.ChainRow,
    analytics.OptionChainSnapshot,
    analytics.MaxPain,
    analytics.BuildupReading,
)

# Order-call results (frozen dataclasses), with the public module they are imported from.
_ORDER_RESULT_TYPES: tuple[tuple[type[Any], str], ...] = ((SlicedOrderResult, "tradesmart"),)

# Derived results of a GTT row (frozen dataclass), with the module it is documented under.
_OCO_RESULT_TYPES: tuple[tuple[type[Any], str], ...] = ((OcoSide, "gtt"),)

# Models defined in ``models/oco_rows.py`` that belong to a section, ahead of its own models.
_SECTION_PREFIX: dict[types.ModuleType, tuple[type[TradeSmartModel], ...]] = {
    gtt: (OcoTrigger, OcoLegRow),
}


def _models(module: types.ModuleType) -> list[type[TradeSmartModel]]:
    """Public response models defined in ``module``, in definition order."""
    found: list[type[TradeSmartModel]] = list(_SECTION_PREFIX.get(module, ()))
    for name, value in vars(module).items():
        if (
            isinstance(value, type)
            and issubclass(value, TradeSmartModel)
            and value.__module__ == module.__name__
            and not name.startswith("_")
        ):
            found.append(value)
    return found


def _raw_hints(cls: type[BaseModel]) -> dict[str, Any]:
    """The declared (un-normalised) annotation of every field, base classes first."""
    hints: dict[str, Any] = {}
    for klass in reversed(cls.__mro__):
        namespace = vars(sys.modules[klass.__module__])
        # inspect.get_annotations: on 3.14+ annotations need not be in the class __dict__.
        for name, hint in inspect.get_annotations(klass).items():
            hints[name] = eval(hint, namespace) if isinstance(hint, str) else hint  # noqa: S307
    return hints


def _label(hint: Any) -> str:
    for alias, name in _ALIASES:
        if hint is alias:
            return name
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)
    if origin is typing.Annotated:
        return _label(args[0])
    if origin is typing.Union or origin is types.UnionType:
        parts = [a for a in args if a is not type(None)]
        return " or ".join(_label(a) for a in parts)
    if origin is list:
        return f"list[{_label(args[0])}]"
    if origin is dict:
        return "object"
    if isinstance(hint, type):
        if issubclass(hint, Enum):
            return f"enum {hint.__name__}"
        if issubclass(hint, TradeSmartModel):
            return f"[{hint.__name__}](#{hint.__name__.lower()})"
        simple = {
            str: "str",
            int: "int",
            bool: "bool",
            Decimal: "Decimal",
            datetime: "datetime (set by the SDK)",
            SecretStr: "secret str",
        }
        if hint in simple:
            return simple[hint]
    if hint is Any:
        return "any"
    raise TypeError(f"no label for annotation {hint!r}")


def _model_table(cls: type[TradeSmartModel]) -> list[str]:
    hints = _raw_hints(cls)
    lenient = cls.lenient_parsing
    protected = cls.lenient_protected_keys
    parents = [
        b.__name__
        for b in cls.__bases__
        if issubclass(b, TradeSmartModel) and b is not TradeSmartModel
    ]
    lines = [f"### {cls.__name__}", ""]
    doc = (cls.__doc__ or "").strip().splitlines()
    if doc:
        lines += [doc[0].replace("``", "`"), ""]
    if parents and not parents[0].startswith("_"):
        lines += [f"Extends `{parents[0]}` (its fields are repeated below).", ""]
    lines += [
        f"Lenient record: {'yes' if lenient else 'no'}.",
        "",
        "| Field | Wire key | Type | Required | Lenient |",
        "|-------|----------|------|----------|---------|",
    ]
    for name, field in cls.model_fields.items():
        wire = field.alias or name
        required = "yes" if field.is_required() else "no"
        if not lenient:
            nulled = "no"
        elif wire in protected:
            nulled = "no (protected)"
        else:
            nulled = "yes"
        lines.append(f"| `{name}` | `{wire}` | {_label(hints[name])} | {required} | {nulled} |")
    return [*lines, ""]


# Prose rules shared by models.md and models.json (one source).
_WIRE_ENUM_RULES = (
    "Lookups trim the input and compare case-insensitively against the wire value and the "
    "accepted words. Response fields typed `enum X` keep an unknown value as the original "
    "string; request parameters reject it (`RequestValidationError`) before sending."
)
_VALUE_ENUM_RULES = (
    "Returned by the SDK, never sent to the server: the values are exact (case-sensitive) "
    "and there are no accepted words. They print as their value."
)
_FLOAT_NOTE = "Floats are never NaN or infinite: a non-finite value is null."
_OCO_NOTE = (
    "Derived from the row's `oco_first`, `oco_second` and `oco_triggers`; it has no wire keys and "
    "no `raw`. The side is decided by `alert_type` (`OCO_BUY`: the leg with the higher trigger "
    "is the stop-loss; `OCO_SELL`: the lower one), never by the leg's slot or `trantype`."
)
_UNITS_NOTE = (
    "Quantities are in trade units on every exchange, MCX included "
    "(whole multiples of the lot size)."
)
# Fragments (no capital, no final full stop): models.md joins them, models.json capitalises them.
_LENIENCY = {
    "required": "the field must be present (a missing required field fails the record)",
    "lenient": (
        "in a lenient record a top-level field that fails validation becomes null and the "
        "original value stays in `raw`"
    ),
    "protected": (
        "a protected key (`norenordno` and `status`; `al_id` on `Gtt` and `TriggeredGtt`) "
        "failing fails the whole record (`ResponseParseError`, or `UnknownMessage` on the "
        "stream)"
    ),
    "unknown_keys": "unknown wire keys are kept (`extra` fields) on every model",
}
# Wire types that are not models, records or enums, and what they mean in the JSON.
_TYPES = {
    "Decimal": "Exact decimal number (never a binary float): prices, money and volumes.",
    "float": "Binary floating-point number; never NaN or infinite (non-finite is null).",
    "int": "Integer.",
    "bool": "Boolean.",
    "datetime": "Timezone-aware date and time.",
    "object": "A JSON object kept as parsed: keys and values are untyped.",
    "TradeSmartError": "The SDK's error type (`TradeSmartError` and its subclasses).",
}


def _sentence(fragment: str) -> str:
    return f"{fragment[0].upper()}{fragment[1:]}."


def _aliases_of(enum_cls: type[Enum]) -> str:
    aliases = getattr(enum_cls, "_aliases", None)
    words = sorted(aliases()) if callable(aliases) else []
    return ", ".join(f"`{w}`" for w in words) if words else "-"


def _enum_table() -> list[str]:
    lines = [
        "## Enums",
        "",
        _WIRE_ENUM_RULES,
        "",
    ]
    for enum_cls in _WIRE_ENUMS:
        lines += [
            f"### {enum_cls.__name__}",
            "",
            f"Accepted words besides the wire values: {_aliases_of(enum_cls)}.",
            "",
            "| Member | Wire value |",
            "|--------|------------|",
        ]
        lines += [f"| `{m.name}` | `{m.value}` |" for m in enum_cls]
        lines.append("")
    return lines


def _value_enum_table() -> list[str]:
    lines = [
        "## Value enums",
        "",
        _VALUE_ENUM_RULES,
        "",
    ]
    for enum_cls in _VALUE_ENUMS:
        lines += [
            f"### {enum_cls.__name__}",
            "",
            f"Module: `{enum_cls.__module__}`.",
            "",
            "| Member | Value |",
            "|--------|-------|",
        ]
        lines += [f"| `{m.name}` | `{m.value}` |" for m in enum_cls]
        lines.append("")
    return lines


_SIMPLE_RESULT_TYPES = {
    "float": "float",
    "int": "int",
    "str": "str",
    "bool": "bool",
    "Decimal": "Decimal",
    "datetime": "datetime (timezone-aware)",
    "TradeSmartError": "error (`TradeSmartError`)",
}


def _result_part(text: str) -> str:
    """Label of one (non-None) member of a dataclass annotation written as text."""
    sequence = re.fullmatch(r"tuple\[(\w+), \.\.\.\]", text)
    if sequence:
        return f"list[{_result_part(sequence.group(1))}]"
    mapping = re.fullmatch(r"dict\[(\w+), (\w+)\]", text)
    if mapping:
        key, value = (_result_part(p) for p in mapping.groups())
        return f"map {key} to {value}"
    if text in _SIMPLE_RESULT_TYPES:
        return _SIMPLE_RESULT_TYPES[text]
    for cls in (*_RESULT_TYPES, OcoLegRow):
        if cls.__name__ == text:
            return f"[{text}](#{text.lower()})"
    for enum_cls in _VALUE_ENUMS:
        if enum_cls.__name__ == text:
            return f"enum {text}"
    raise TypeError(f"no label for result annotation {text!r}")


def _result_label(annotation: Any) -> tuple[str, str]:
    """``(type label, nullable)`` from a dataclass annotation (text: postponed evaluation).

    The annotations are read as text (postponed evaluation), so ``float | None`` is split on ``|``.
    """
    text = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    parts = [p.strip() for p in text.split("|")]
    nullable = "yes" if "None" in parts else "no"
    return " or ".join(_result_part(p) for p in parts if p != "None"), nullable


def _methods(cls: type[Any]) -> list[str]:
    found = []
    for name, value in vars(cls).items():
        if name.startswith("_") or not callable(value):
            continue
        params = [
            p.name if p.default is inspect.Parameter.empty else f"{p.name}={p.default!r}"
            for p in inspect.signature(value).parameters.values()
            if p.name != "self"
        ]
        found.append(f"`{name}({', '.join(params)})`")
    return found


def _result_table(cls: type[Any], module: str | None = None) -> list[str]:
    lines = [f"### {cls.__name__}", ""]
    doc = (cls.__doc__ or "").strip().splitlines()
    if doc:
        lines += [doc[0].replace("``", "`"), ""]
    lines += [
        f"Module: `{module or cls.__module__}`. Immutable.",
        "",
        "| Field | Type | Nullable |",
        "|-------|------|----------|",
    ]
    for field in dataclasses.fields(cls):
        label, nullable = _result_label(field.type)
        lines.append(f"| `{field.name}` | {label} | {nullable} |")
    methods = _methods(cls)
    if methods:
        lines += ["", f"Methods: {', '.join(methods)}."]
    return [*lines, ""]


def _result_section() -> list[str]:
    lines = [
        "## Greeks and analytics results (`tradesmart.greeks`, `tradesmart.analytics`)",
        "",
        "Plain immutable records returned by the recommended modules (not wire models: no wire "
        "keys, no `raw`). Field names are part of the contract (convert them to the "
        "language's case style); the functions that return them are listed in "
        f"`sdk-behaviour.md` sections 10.8 and 11.5. {_FLOAT_NOTE}",
        "",
    ]
    for cls in _RESULT_TYPES:
        lines += _result_table(cls)
    return lines


def _order_result_section() -> list[str]:
    lines = [
        "## Order results (`place_sliced_order`)",
        "",
        "Plain immutable records returned by order calls (not wire models: no wire keys, no "
        "`raw`). Field names are part of the contract (convert them to the language's case "
        f"style); the behaviour is in `sdk-behaviour.md` section 5.7. {_UNITS_NOTE}",
        "",
    ]
    for cls, module in _ORDER_RESULT_TYPES:
        lines += _result_table(cls, module)
    return lines


def _oco_result_section() -> list[str]:
    lines = [
        "## OCO results (`Gtt.stop_loss`, `Gtt.target`)",
        "",
        "Plain immutable records derived by the SDK from a row (not wire models: no wire keys, no "
        "`raw`). Field names are part of the contract (convert them to the language's case "
        "style); the behaviour is in `sdk-behaviour.md` section 2.9.",
        "",
    ]
    for cls, module in _OCO_RESULT_TYPES:
        lines += _result_table(cls, module)
    return lines


def _legend() -> list[str]:
    formats = ", ".join(f"`{f}`" for f in base._IST_FORMATS)
    return [
        "## Field types",
        "",
        "| Type | Wire value | Parsed value |",
        "|------|------------|--------------|",
        "| `Price` | string or number | Decimal; blank or missing is null; non-finite or "
        "non-numeric fails the field |",
        "| `StreamPrice` | string or number | as `Price`, and the stream sentinel "
        f"`{base.ATO_PRICE}` (ATO, no price) is null |",
        "| `Volume` | string or number | Decimal (volumes and open interest can exceed 32 bits); "
        "parsed like `Price` |",
        "| `Quantity` | string or number | int; integral decimals such as `10.00` are accepted, "
        "fractions and booleans fail the field; blank is null |",
        "| `EpochTime` | epoch seconds (string or number) | timezone-aware UTC datetime; `0` or "
        "blank is null |",
        f"| `ISTDateTime` | text in one of {formats} | datetime at +05:30; an unknown format is "
        "null (logged once per shape, never the value) |",
        "| `str` | string or number | string (JSON numbers become strings) |",
        "| `enum X` | string | member of `X`, or the original string when unknown |",
        "| `secret str` | string | masked in `repr` and logs |",
        "",
        f"Required: {_LENIENCY['required']}. Lenient: {_LENIENCY['lenient']}; "
        f"{_LENIENCY['protected']}. {_sentence(_LENIENCY['unknown_keys'])}",
        "",
    ]


_LINK = re.compile(r"\[(\w+)\]\(#\w+\)")


def _json_type(hint: Any) -> str:
    """Type name of a model field: the ``_label`` text without links and prose notes."""
    text = _LINK.sub(r"\1", _label(hint)).replace("datetime (set by the SDK)", "datetime")
    if " or " in text:
        raise TypeError(f"union type {text!r} has no JSON form: declare a single type")
    return text


def _is_nullable(hint: Any) -> bool:
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)
    if origin is typing.Annotated:
        return _is_nullable(args[0])
    if origin is typing.Union or origin is types.UnionType:
        return type(None) in args
    return hint is Any


def _has_secret(hint: Any) -> bool:
    if hint is SecretStr:
        return True
    return any(_has_secret(a) for a in typing.get_args(hint))


def _json_default(field: Any) -> Any:
    """The field default as JSON (null when required or not representable)."""
    if field.is_required():
        return None
    default = field.get_default(call_default_factory=True)
    if isinstance(default, (str, int, float, bool)) or default is None:
        return default
    return None


def _stream_types() -> dict[type[Any], list[str]]:
    from tradesmart.stream import protocol

    found: dict[type[Any], list[str]] = {}
    for key, cls in protocol._MODELS.items():
        found.setdefault(cls, []).append(key)
    return found


def _json_model(cls: type[TradeSmartModel], group: str) -> dict[str, Any]:
    hints = _raw_hints(cls)
    protected = cls.lenient_protected_keys
    parents = [
        b.__name__
        for b in cls.__bases__
        if issubclass(b, TradeSmartModel) and b is not TradeSmartModel
    ]
    doc = (cls.__doc__ or "").strip().splitlines()
    fields = []
    for name, field in cls.model_fields.items():
        wire = field.alias or name
        hint = hints[name]
        fields.append(
            {
                "name": name,
                "wire": wire,
                "type": _json_type(hint),
                "required": field.is_required(),
                "lenient": cls.lenient_parsing and wire not in protected,
                "protected": cls.lenient_parsing and wire in protected,
                "secret": _has_secret(hint),
                "nullable": _is_nullable(hint),
                "default": _json_default(field),
            }
        )
    return {
        "name": cls.__name__,
        "group": group,
        "description": doc[0].replace("``", "`") if doc else "",
        "extends": parents[0] if parents and not parents[0].startswith("_") else None,
        "lenient": cls.lenient_parsing,
        "stream_types": _stream_types().get(cls, []),
        "fields": fields,
    }


def _json_result_part(text: str) -> str:
    """Type name of one (non-None) member of a dataclass annotation written as text."""
    sequence = re.fullmatch(r"tuple\[(\w+), \.\.\.\]", text)
    if sequence:
        return f"list[{_json_result_part(sequence.group(1))}]"
    mapping = re.fullmatch(r"dict\[(\w+), (\w+)\]", text)
    if mapping:
        key, value = (_json_result_part(p) for p in mapping.groups())
        return f"dict[{key}, {value}]"
    if text in _SIMPLE_RESULT_TYPES:
        return text
    _result_part(text)  # raises TypeError for an annotation the Markdown cannot label
    return f"enum {text}" if any(e.__name__ == text for e in _VALUE_ENUMS) else text


def _json_record_type(parts: list[str]) -> str:
    """Type name of a dataclass annotation's members (``None`` excluded); a union is refused."""
    members = [p for p in parts if p != "None"]
    if len(members) != 1:
        raise TypeError(f"union type {' | '.join(parts)!r} has no JSON form: declare one type")
    return _json_result_part(members[0])


def _json_record(cls: type[Any], module: str) -> dict[str, Any]:
    doc = (cls.__doc__ or "").strip().splitlines()
    fields = []
    for field in dataclasses.fields(cls):
        text = field.type if isinstance(field.type, str) else getattr(field.type, "__name__", "")
        parts = [p.strip() for p in text.split("|")]
        fields.append(
            {
                "name": field.name,
                "type": _json_record_type(parts),
                "nullable": "None" in parts,
            }
        )
    return {
        "name": cls.__name__,
        "module": module,
        "immutable": bool(cls.__dataclass_params__.frozen),  # type: ignore[attr-defined]
        "description": doc[0].replace("``", "`") if doc else "",
        "fields": fields,
        "methods": [m.strip("`") for m in _methods(cls)],
    }


def _json_enum(enum_cls: type[Enum], wire_key: str) -> dict[str, Any]:
    aliases_fn = getattr(enum_cls, "_aliases", None)
    aliases = aliases_fn() if callable(aliases_fn) else {}
    members = []
    for m in enum_cls:
        words = sorted(word for word, target in aliases.items() if target == m.name)
        members.append({"name": m.name, wire_key: m.value, "aliases": words})
    return {"name": enum_cls.__name__, "members": members}


def _json_field_types() -> dict[str, str]:
    """The "Field types" table as ``type -> description`` (read from the Markdown rows)."""
    found: dict[str, str] = {}
    for line in _legend():
        match = re.fullmatch(r"\| `([^`]+)` \| (.+?) \| (.+) \|", line)
        if match:
            found[match.group(1)] = f"Wire value: {match.group(2)}. Parsed value: {match.group(3)}."
    return found


def render_json() -> str:
    """``specifications/models.json``: the same tables as ``render()``, machine-readable."""
    models = []
    for _title, module in SECTIONS:
        group = module.__name__.rsplit(".", 1)[-1]
        models += [_json_model(cls, group) for cls in _models(module)]
    records = [_json_record(cls, cls.__module__.rsplit(".", 1)[-1]) for cls in _RESULT_TYPES]
    records += [_json_record(cls, "orders") for cls, _module in _ORDER_RESULT_TYPES]
    records += [_json_record(cls, module) for cls, module in _OCO_RESULT_TYPES]
    data = {
        "format": 1,
        "source": "sdk-python/scripts/gen_model_tables.py",
        "field_types": _json_field_types(),
        "types": dict(_TYPES),
        "enum_rules": {"wire_enums": _WIRE_ENUM_RULES, "value_enums": _VALUE_ENUM_RULES},
        "record_notes": [
            {"applies_to": [c.__name__ for c in _RESULT_TYPES], "note": _FLOAT_NOTE},
            {"applies_to": [c.__name__ for c, _m in _ORDER_RESULT_TYPES], "note": _UNITS_NOTE},
            {"applies_to": [c.__name__ for c, _m in _OCO_RESULT_TYPES], "note": _OCO_NOTE},
        ],
        "leniency": {
            "required": _sentence(_LENIENCY["required"]),
            "lenient": _sentence(_LENIENCY["lenient"]),
            "protected": _sentence(_LENIENCY["protected"]),
            "unknown_keys": _sentence(_LENIENCY["unknown_keys"]),
        },
        "models": models,
        "records": records,
        "enums": [_json_enum(e, "wire") for e in _WIRE_ENUMS],
        "value_enums": [{**_json_enum(e, "value"), "module": e.__module__} for e in _VALUE_ENUMS],
    }
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def render() -> str:
    lines = [
        "# Response and stream models",
        "",
        "Generated by `sdk-python/scripts/gen_model_tables.py` from the Python SDK's pydantic "
        "models. Do not edit by hand: run "
        "`docker compose run --rm dev python scripts/gen_model_tables.py` from `sdk-python/`. "
        "A unit test fails when this file is out of date. Behaviour around the models "
        "(parsing, `raw`, errors) is in `sdk-behaviour.md`. "
        "`models.json` is the machine-readable form of these tables.",
        "",
        *_legend(),
    ]
    for title, module in SECTIONS:
        lines += [f"## {title}", ""]
        for cls in _models(module):
            lines += _model_table(cls)
    lines += _result_section()
    lines += _order_result_section()
    lines += _oco_result_section()
    lines += _enum_table()
    lines += _value_enum_table()
    return "\n".join(lines).rstrip("\n") + "\n"


def main() -> int:
    OUT_FILE.write_text(render(), encoding="utf-8", newline="\n")
    JSON_FILE.write_text(render_json(), encoding="utf-8", newline="\n")
    sys.stdout.write(f"wrote {OUT_FILE.name} and {JSON_FILE.name}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
