"""``specifications/models.md`` is exactly what ``scripts/gen_model_tables.py`` renders.

Regenerate with ``docker compose run --rm dev python scripts/gen_model_tables.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

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
