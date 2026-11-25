"""``selected_sdks``: which SDKs the matrix runs, from ``TRADESMART_LIVE_SDKS``."""

from __future__ import annotations

from typing import Any

import pytest

from tests.live_orders import safety
from tests.live_orders.uat_matrix import sdk_select


@pytest.fixture
def two(monkeypatch: pytest.MonkeyPatch) -> None:
    """A two-entry registry of stand-ins (the real one is checked above)."""
    monkeypatch.setattr(
        sdk_select, "REGISTRY", {"python": lambda *a: "py", "javascript": lambda *a: "js"}
    )


def test_unset_or_blank_is_python_only() -> None:
    assert sdk_select.selected_sdks({}) == ["python"]
    assert sdk_select.selected_sdks({"TRADESMART_LIVE_SDKS": ""}) == ["python"]
    assert sdk_select.selected_sdks({"TRADESMART_LIVE_SDKS": "  "}) == ["python"]


def test_the_variable_name() -> None:
    assert sdk_select.SDKS_ENV == "TRADESMART_LIVE_SDKS"


def test_all_is_every_registered_sdk_in_fixed_order(two: None) -> None:
    assert sdk_select.selected_sdks({"TRADESMART_LIVE_SDKS": "all"}) == ["python", "javascript"]
    assert sdk_select.selected_sdks({"TRADESMART_LIVE_SDKS": " ALL "}) == ["python", "javascript"]


def test_a_comma_list_keeps_order_and_drops_duplicates(two: None) -> None:
    env = {"TRADESMART_LIVE_SDKS": "javascript, python,javascript"}
    assert sdk_select.selected_sdks(env) == ["javascript", "python"]
    assert sdk_select.selected_sdks({"TRADESMART_LIVE_SDKS": "python"}) == ["python"]


def test_an_unknown_name_is_refused_naming_the_known_ones(two: None) -> None:
    with pytest.raises(safety.Refusal) as caught:
        sdk_select.selected_sdks({"TRADESMART_LIVE_SDKS": "python,ruby"})
    assert "ruby" in caught.value.reason
    assert "python" in caught.value.reason and "javascript" in caught.value.reason


def test_javascript_is_known_and_an_unregistered_name_is_refused() -> None:
    assert sdk_select.selected_sdks({"TRADESMART_LIVE_SDKS": "javascript"}) == ["javascript"]
    with pytest.raises(safety.Refusal) as caught:
        sdk_select.selected_sdks({"TRADESMART_LIVE_SDKS": "ruby"})
    assert "python" in caught.value.reason and "javascript" in caught.value.reason


def test_all_with_the_real_registry_is_python_then_javascript() -> None:
    assert sdk_select.selected_sdks({"TRADESMART_LIVE_SDKS": "all"}) == ["python", "javascript"]
    assert list(sdk_select.REGISTRY) == ["python", "javascript"]


def test_an_empty_item_in_a_list_is_refused(two: None) -> None:
    with pytest.raises(safety.Refusal):
        sdk_select.selected_sdks({"TRADESMART_LIVE_SDKS": "python,,javascript"})


def test_build_makes_the_python_sdk_from_the_client() -> None:
    class Client:
        client_id = "X"

    built: Any = sdk_select.build_sdk("python", Client(), {})  # type: ignore[arg-type]
    assert built.name == "python" and built.client_id == "X"
