"""The live suite's opt-in gate marks items by their file, never by a parameter id."""

from __future__ import annotations

from typing import Any

import pytest

from tests.live import conftest as live_conftest


class _Item:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self.markers: list[Any] = []

    def add_marker(self, marker: Any) -> None:
        self.markers.append(marker)


@pytest.fixture(autouse=True)
def _gate_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADESMART_LIVE_TESTS", raising=False)


def test_unit_test_with_live_path_in_its_parameter_id_is_not_skipped() -> None:
    item = _Item("tests/unit/test_docs.py::test_doc[tests/live/README.md]")
    live_conftest.pytest_collection_modifyitems(None, [item])  # type: ignore[arg-type,list-item]
    assert item.markers == []


@pytest.mark.parametrize(
    "path",
    ["tests/live/test_live_readonly.py", "tests\\live\\test_live_readonly.py"],
)
def test_items_in_the_live_folder_are_skipped_without_the_opt_in(path: str) -> None:
    item = _Item(f"{path}::test_limits[nse-eq]")
    live_conftest.pytest_collection_modifyitems(None, [item])  # type: ignore[arg-type,list-item]
    assert [m.name for m in item.markers] == ["skip"]


def test_live_items_are_left_alone_with_the_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADESMART_LIVE_TESTS", "1")
    item = _Item("tests/live/test_x.py::t")
    live_conftest.pytest_collection_modifyitems(None, [item])  # type: ignore[arg-type,list-item]
    assert item.markers == []
