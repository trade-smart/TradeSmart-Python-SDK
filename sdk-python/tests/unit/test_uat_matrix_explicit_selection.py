"""The UAT matrix runs only when selected explicitly (controller ruling, final fix wave).

Matrix items are **deselected** (not skipped) unless the ``-m`` expression selects the
``uat_matrix`` marker positively (a token, not under ``not``), or a command-line path points
inside ``tests/live_orders/uat_matrix``. So the old UAT order-suite command
``pytest tests/live_orders -m live_orders`` never collects the matrix smoke tier.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.live_orders.uat_matrix import gate

SDK_DIR = Path(__file__).resolve().parents[2]
MATRIX = "tests/live_orders/uat_matrix"
OLD_SUITES = (
    "test_live_orders.py",
    "test_uat_lifecycle.py",
    "test_uat_sliced.py",
    "test_gtt_live.py",
)


@pytest.mark.parametrize(
    ("markexpr", "selected"),
    [
        ("", False),
        ("live_orders", False),
        ("uat_matrix", True),
        ("live_orders and uat_matrix", True),
        ("uat_matrix and smoke", True),
        ("live_orders or uat_matrix", True),
        ("(uat_matrix)", True),
        ("not uat_matrix", False),
        ("live_orders and not uat_matrix", False),
        ("not (live_orders or uat_matrix)", False),
        ("not not uat_matrix", True),
        ("uat_matrix_extra", False),  # a token, not a substring
        ("my_uat_matrix", False),
        ("uat_matrix and", False),  # not an expression: not selected
        ("uat_matrix(x=1)", False),  # not understood: not selected (fails closed)
    ],
)
def test_the_marker_expression_selects_the_matrix_only_positively(
    markexpr: str, selected: bool
) -> None:
    assert gate.marker_selects(markexpr, "uat_matrix") is selected


@pytest.mark.parametrize(
    ("args", "selected"),
    [
        ([], False),
        (["tests"], False),
        (["tests/live_orders"], False),
        (["tests/live_orders/test_uat_lifecycle.py"], False),
        ([MATRIX], True),
        ([f"{MATRIX}/"], True),
        ([f"{MATRIX}/test_shapes.py"], True),
        ([f"{MATRIX}/test_shapes.py::test_shapes_case"], True),
        (["tests/live_orders", f"{MATRIX}/test_gtt.py"], True),
        (["tests/live_orders/uat_matrix_old"], False),
    ],
)
def test_a_path_argument_inside_the_matrix_selects_it(args: list[str], selected: bool) -> None:
    assert gate.path_selects_matrix(args, SDK_DIR) is selected


class _Item:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self.markers: list[Any] = []

    def add_marker(self, marker: Any) -> None:
        self.markers.append(marker)

    def iter_markers(self) -> list[Any]:
        return []


def _config(markexpr: str, args: list[str]) -> Any:
    deselected: list[Any] = []
    return SimpleNamespace(
        option=SimpleNamespace(markexpr=markexpr),
        args=args,
        invocation_params=SimpleNamespace(dir=SDK_DIR),
        hook=SimpleNamespace(pytest_deselected=lambda items: deselected.extend(items)),
        deselected=deselected,
    )


def test_the_hook_deselects_matrix_items_not_selected_explicitly() -> None:
    from tests.live_orders.uat_matrix import conftest as matrix_conftest

    matrix = _Item(f"{MATRIX}/test_shapes.py::test_shapes_case[x]")
    other = _Item("tests/live_orders/test_uat_lifecycle.py::test_x")
    items = [matrix, other]
    config = _config("live_orders", ["tests/live_orders"])
    matrix_conftest.pytest_collection_modifyitems(config, items)  # type: ignore[arg-type]
    assert items == [other]
    assert config.deselected == [matrix]


def _collected(*args: str) -> list[str]:
    done = subprocess.run(  # noqa: S603 - this interpreter, a fixed pytest command
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "-o", "addopts=", "-p", "no:xdist", *args],
        cwd=SDK_DIR, capture_output=True, text=True, timeout=300, check=False,
        env={k: v for k, v in __import__("os").environ.items()
             if not k.startswith("TRADESMART_")},
    )  # fmt: skip
    assert done.returncode in (0, 5), done.stdout[-2000:] + done.stderr[-2000:]
    return [line for line in done.stdout.splitlines() if "::" in line]


def test_real_collection_selects_the_matrix_only_when_asked() -> None:
    old = _collected("-m", "live_orders", "tests/live_orders")
    assert old and not any("/uat_matrix/" in line for line in old)
    for suite in OLD_SUITES:  # the old suites are unaffected
        assert any(line.startswith(f"tests/live_orders/{suite}::") for line in old), suite
    by_marker = _collected("-m", "live_orders and uat_matrix", "tests/live_orders")
    assert by_marker and all("/uat_matrix/" in line for line in by_marker)
    by_path = _collected("-m", "live_orders", MATRIX)
    assert by_path and all("/uat_matrix/" in line for line in by_path)
    negated = _collected("-m", "not uat_matrix", "tests/live_orders")
    assert negated and not any("/uat_matrix/" in line for line in negated)
