"""``specifications/models.md`` is exactly what ``scripts/gen_model_tables.py`` renders.

Regenerate with ``docker compose run --rm dev python scripts/gen_model_tables.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

SDK_ROOT = Path(__file__).resolve().parents[2]
MODELS_MD = SDK_ROOT.parent / "specifications" / "models.md"


def _load_generator() -> ModuleType:
    path = SDK_ROOT / "scripts" / "gen_model_tables.py"
    spec = importlib.util.spec_from_file_location("gen_model_tables", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_model_tables"] = module
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


def test_committed_models_md_matches_the_generator() -> None:
    committed = MODELS_MD.read_text(encoding="utf-8")
    assert committed == gen.render(), "specifications/models.md is stale: run gen_model_tables.py"


def test_render_is_deterministic() -> None:
    assert gen.render() == gen.render()


def test_every_public_response_model_has_a_table() -> None:
    text = gen.render()
    for _title, module in gen.SECTIONS:
        for cls in gen._models(module):
            assert f"\n### {cls.__name__}\n" in text
    for name in ("Order", "OrderUpdate", "Quote", "Holding", "AccessToken", "DepthFeed"):
        assert f"\n### {name}\n" in text


def test_enum_tables_list_the_wire_values() -> None:
    text = gen.render()
    assert "| `CNC` | `C` |" in text
    assert "| `SL_M` | `SL-MKT` |" in text
    assert "| `MCX` | `MCX` |" in text


def test_greeks_and_analytics_result_types_have_tables() -> None:
    text = gen.render()
    for name in (
        "Greeks",
        "OptionGreeks",
        "ChainRow",
        "OptionChainSnapshot",
        "MaxPain",
        "BuildupReading",
    ):
        assert f"\n### {name}\n" in text
    assert "| `years_to_expiry` | float |" in text
    assert "| `rows` | list[[ChainRow](#chainrow)] |" in text
    assert "| `state` | enum Buildup |" in text


def test_stream_state_and_buildup_enums_are_listed() -> None:
    text = gen.render()
    assert "\n### StreamState\n" in text
    assert "| `RECONNECTING` | `RECONNECTING` |" in text
    assert "\n### Buildup\n" in text
    assert "| `SHORT_COVERING` | `short_covering` |" in text


def test_wire_keys_and_types_are_shown() -> None:
    text = gen.render()
    assert "| `last_price` | `lp` | Price | no | no |" in text
    assert "| `order_id` | `norenordno` |" in text
    assert "no (protected)" in text


def test_sliced_order_result_has_a_table() -> None:
    text = gen.render()
    assert "\n## Order results (`place_sliced_order`)\n" in text
    assert "\n### SlicedOrderResult\n" in text
    assert "Module: `tradesmart`. Immutable." in text
    for row in (
        "| `requested_quantity` | int | no |",
        "| `slice_quantities` | list[int] | no |",
        "| `order_ids` | list[str] | no |",
        "| `placed_quantity` | int | no |",
        "| `unsent_quantity` | int | no |",
        "| `complete` | bool | no |",
        "| `error` | error (`TradeSmartError`) | yes |",
    ):
        assert row in text
    assert text.index("### SlicedOrderResult") < text.index("## Enums")


def test_lenient_note_names_every_protected_key() -> None:
    from tradesmart.models.base import TradeSmartModel

    def subclasses(cls: type) -> list[type]:
        found = []
        for sub in cls.__subclasses__():
            found += [sub, *subclasses(sub)]
        return found

    note = next(line for line in gen.render().splitlines() if line.startswith("Required: "))
    for model in subclasses(TradeSmartModel):
        if getattr(model, "lenient_parsing", False):
            for key in model.lenient_protected_keys:
                assert f"`{key}`" in note, f"{model.__name__}: protected key {key} not named"


MODELS_JSON = SDK_ROOT.parent / "specifications" / "models.json"


def test_models_json_is_current() -> None:
    committed = MODELS_JSON.read_text(encoding="utf-8")
    assert committed == gen.render_json(), "specifications/models.json is stale: run the generator"


def test_models_json_covers_every_model_in_markdown() -> None:
    import json
    import re

    data = json.loads(gen.render_json())
    known = {m["name"] for m in data["models"]} | {r["name"] for r in data["records"]}
    markdown = MODELS_MD.read_text(encoding="utf-8")
    headings = set(re.findall(r"^### (\w+)$", markdown, re.M))
    enum_names = {e["name"] for e in data["enums"]} | {e["name"] for e in data["value_enums"]}
    assert headings - enum_names <= known
    assert known <= headings


def test_models_json_top_level_shape() -> None:
    import json

    data = json.loads(gen.render_json())
    assert list(data) == [
        "format",
        "source",
        "field_types",
        "types",
        "enum_rules",
        "record_notes",
        "leniency",
        "models",
        "records",
        "enums",
        "value_enums",
    ]
    assert data["format"] == 1
    assert data["source"] == "sdk-python/scripts/gen_model_tables.py"
    assert {"Price", "Quantity", "EpochTime", "ISTDateTime"} <= set(data["field_types"])
    by_name = {m["name"]: m for m in data["models"]}
    assert by_name["OrderUpdate"]["stream_types"] == ["om"]
    assert by_name["UnsubscribeAck"]["stream_types"] == ["uk", "udk", "uok", "upk"]
    assert by_name["Order"]["stream_types"] == []
    token = {f["name"]: f for f in by_name["AccessToken"]["fields"]}["access_token"]
    assert token["secret"] is True and token["type"] == "secret str"
    records = {r["name"]: r for r in data["records"]}
    assert records["Greeks"]["module"] == "greeks"
    assert records["MaxPain"]["module"] == "analytics"
    assert records["SlicedOrderResult"]["module"] == "orders"
    product = next(e for e in data["enums"] if e["name"] == "Product")
    assert {"name": "CNC", "wire": "C", "aliases": ["CNC"]} in product["members"]


def test_models_json_order_wire_keys() -> None:
    import json

    data = json.loads(gen.render_json())
    order = next(m for m in data["models"] if m["name"] == "Order")
    assert any(
        f["name"] == "order_id"
        and f["wire"] == "norenordno"
        and f["type"] == "str"
        and f["protected"] is True
        for f in order["fields"]
    )


def test_models_json_enum_exchange() -> None:
    import json

    data = json.loads(gen.render_json())
    exchange = next(e for e in data["enums"] if e["name"] == "Exchange")
    names = {m["name"] for m in exchange["members"]}
    assert {"NSE", "NFO", "BSE", "BFO", "MCX", "CDS", "BCD"} <= names


def _declared_type_names(data: dict[str, Any]) -> set[str]:
    return (
        set(data["field_types"])
        | set(data["types"])
        | {m["name"] for m in data["models"]}
        | {r["name"] for r in data["records"]}
        | {e["name"] for e in data["enums"]}
        | {e["name"] for e in data["value_enums"]}
    )


def _unwrap(type_name: str) -> list[str]:
    """The declared names a ``type`` string refers to (list[], dict[,] and enum unwrapped)."""
    import re

    if type_name in {"enum X", "secret str"}:
        return [type_name]
    if type_name.startswith("enum "):
        return [type_name[len("enum ") :]]
    inner = re.fullmatch(r"list\[(.+)\]", type_name)
    if inner:
        return _unwrap(inner.group(1))
    pair = re.fullmatch(r"dict\[(\w+), (.+)\]", type_name)
    if pair:
        return _unwrap(pair.group(1)) + _unwrap(pair.group(2))
    return [type_name]


def test_models_json_type_vocabulary_is_closed() -> None:
    import json

    data = json.loads(gen.render_json())
    declared = _declared_type_names(data)
    assert {"Decimal", "float", "int", "bool", "datetime", "object", "TradeSmartError"} <= set(
        data["types"]
    )
    used = [f["type"] for m in data["models"] for f in m["fields"]]
    used += [f["type"] for r in data["records"] for f in r["fields"]]
    for type_name in used:
        assert " or " not in type_name, type_name
        for name in _unwrap(type_name):
            assert name in declared, f"undeclared type {name!r} (from {type_name!r})"
    assert all(isinstance(text, str) and text for text in data["types"].values())


def test_models_json_records_are_immutable_with_notes() -> None:
    import json

    data = json.loads(gen.render_json())
    assert all(r["immutable"] is True for r in data["records"])
    notes = {n["applies_to"][0]: n["note"] for n in data["record_notes"]}
    assert "non-finite" in notes["Greeks"]
    assert "MCX" in notes["SlicedOrderResult"]
    assert "case-insensitively" in data["enum_rules"]["wire_enums"]
    assert "RequestValidationError" in data["enum_rules"]["wire_enums"]
    assert "case-sensitive" in data["enum_rules"]["value_enums"]
    leniency = data["leniency"]
    assert "ResponseParseError" in leniency["protected"]
    assert "UnknownMessage" in leniency["protected"]
    assert set(leniency) == {"required", "lenient", "protected", "unknown_keys"}


def test_models_json_refuses_a_union_type() -> None:
    import pytest

    with pytest.raises(TypeError, match="union"):
        gen._json_type(int | str)
    with pytest.raises(TypeError, match="union"):
        gen._json_record_type(["int", "str", "None"])


def test_models_json_is_deterministic() -> None:
    first = gen.render_json()
    assert first == gen.render_json()
    assert first.endswith("\n")
    assert "\r" not in first
