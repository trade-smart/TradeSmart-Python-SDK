"""``examples/sliced_order.py`` runs on the offline broker and handles every outcome."""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tradesmart.testing import OfflineBroker, drills

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "sliced_order.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_example_sliced_order", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _place_requests(broker: OfflineBroker) -> list[int]:
    return [int(r.payload["qty"]) for r in broker.requests if r.endpoint == "PlaceOrder"]


def test_the_example_runs_offline_and_reports_the_slices(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load()
    assert module.main([]) == 0
    out = capsys.readouterr().out
    assert "1755, 1755, 455" in out
    assert "stopped after 0 of 3 slices" in out
    assert "outcome unknown after 0 slice(s)" in out  # the third run: an ambiguous slice


def test_buy_in_slices_places_every_slice() -> None:
    module = _load()
    with OfflineBroker() as broker:
        module.add_nifty_future(broker)
        ids = module.buy_in_slices(broker.client(), 3965, Decimal("24000"), tag="demo-1")
        assert _place_requests(broker) == [1755, 1755, 455]
        assert ids == [order.id for order in broker.orders]


def test_buy_in_slices_stops_at_a_rejection() -> None:
    module = _load()
    with OfflineBroker() as broker:
        module.add_nifty_future(broker)
        broker.fail_next("PlaceOrder", drills.reject("RMS:Margin Exceeds"))
        assert module.buy_in_slices(broker.client(), 3965, Decimal("24000"), tag="demo-2") == []
        assert _place_requests(broker) == [1755]  # nothing after the rejected slice


def test_buy_in_slices_finds_an_unknown_slice_by_tag_and_never_resends() -> None:
    module = _load()
    with OfflineBroker() as broker:
        module.add_nifty_future(broker)
        broker.fail_next("PlaceOrder", drills.timeout_after_send())
        ids = module.buy_in_slices(broker.client(), 3965, Decimal("24000"), tag="demo-3")
        assert _place_requests(broker) == [1755]
        assert ids == [broker.orders[0].id]


def test_an_unknown_slice_is_looked_up_again_when_the_book_lags() -> None:
    module = _load()
    pauses: list[float] = []
    with OfflineBroker() as broker:
        module.add_nifty_future(broker)
        client = broker.client()
        real_orders = client.orders
        reads = 0

        def lagging_orders() -> list[Any]:
            nonlocal reads
            reads += 1
            return [] if reads == 1 else real_orders()  # the first read misses the slice

        client.orders = lagging_orders  # type: ignore[method-assign]
        broker.fail_next("PlaceOrder", drills.timeout_after_send())
        ids = module.buy_in_slices(
            client, 3965, Decimal("24000"), tag="demo-4", sleep=pauses.append
        )
        assert ids == [broker.orders[0].id]
        assert _place_requests(broker) == [1755]  # never resent
    assert reads == 2 and pauses == [module.LOOKUP_PAUSE_SECONDS]


def test_an_unknown_slice_never_found_is_reported_as_not_found_yet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load()
    pauses: list[float] = []
    with OfflineBroker() as broker:
        module.add_nifty_future(broker)
        client = broker.client()
        client.orders = list  # type: ignore[method-assign]  # the book never shows it
        broker.fail_next("PlaceOrder", drills.timeout_after_send())
        ids = module.buy_in_slices(
            client, 3965, Decimal("24000"), tag="demo-5", sleep=pauses.append
        )
    assert ids == []
    assert len(pauses) == module.LOOKUPS - 1
    assert "not found yet" in capsys.readouterr().out
