"""``examples/gtt_alert.py`` runs on the offline broker and handles every GTT outcome."""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tradesmart.testing import OfflineBroker, drills

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "gtt_alert.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_example_gtt_alert", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payloads(broker: OfflineBroker, endpoint: str) -> list[dict[str, Any]]:
    return [r.payload for r in broker.requests if r.endpoint == endpoint]


def _broker(module: ModuleType, broker: OfflineBroker, last_price: str = "400.00") -> None:
    module.add_itc(broker)
    broker.set_quote("NSE", module.TOKEN, lp=last_price, ti="0.05")


def test_the_example_runs_offline_and_shows_every_outcome(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load()
    assert module.main([]) == 0
    out = capsys.readouterr().out
    assert "pending GTT" in out
    assert "fired: order" in out  # the offline broker fires it on request; its order has parent_id
    assert "found by tag" in out  # the unknown outcome
    assert "would fire at once" in out  # the refusal before sending


def test_buy_below_places_one_ltp_below_gtt() -> None:
    module = _load()
    with OfflineBroker() as broker:
        _broker(module, broker)
        alert_id = module.buy_below(
            broker.client(), trigger=Decimal("380"), price=Decimal("380"), tag="dip-1"
        )
        (placed,) = _payloads(broker, "PlaceGTTOrder")
        assert placed["ai_t"] == "LTP_B_O" and placed["remarks"] == "dip-1"
        assert placed["validity"] == "GTT" and placed["prd"] == "C"
        assert alert_id == broker.client().pending_gtts()[0].alert_id


@pytest.mark.parametrize("last_price", ["380.00", "379.95"])
def test_buy_below_refuses_a_condition_already_true(last_price: str) -> None:
    module = _load()
    with OfflineBroker() as broker:
        _broker(module, broker, last_price)
        alert_id = module.buy_below(
            broker.client(), trigger=Decimal("380"), price=Decimal("380"), tag="dip-2"
        )
        assert alert_id is None
        assert _payloads(broker, "PlaceGTTOrder") == []  # nothing sent: it would fire at once


def test_buy_below_finds_an_unknown_outcome_by_tag_and_never_resends() -> None:
    module = _load()
    with OfflineBroker() as broker:
        _broker(module, broker)
        broker.fail_next("PlaceGTTOrder", drills.timeout_after_send())
        client = broker.client()
        alert_id = module.buy_below(
            client, trigger=Decimal("380"), price=Decimal("380"), tag="dip-3"
        )
        assert len(_payloads(broker, "PlaceGTTOrder")) == 1
        assert alert_id == client.pending_gtts()[0].alert_id


def test_buy_below_returns_none_on_a_rejection() -> None:
    module = _load()
    with OfflineBroker() as broker:
        _broker(module, broker)
        broker.fail_next("PlaceGTTOrder", drills.reject("GTT not allowed"))
        assert (
            module.buy_below(
                broker.client(), trigger=Decimal("380"), price=Decimal("380"), tag="dip-4"
            )
            is None
        )


def test_move_trigger_resends_every_field_with_both_validities_from_the_row() -> None:
    module = _load()
    with OfflineBroker() as broker:
        _broker(module, broker)
        client = broker.client()
        module.buy_below(client, trigger=Decimal("380"), price=Decimal("380"), tag="dip-5")
        (gtt,) = client.pending_gtts()
        module.move_trigger(client, gtt, Decimal("370"))
        (modified,) = _payloads(broker, "ModifyGTTOrder")
        assert Decimal(modified["d"]) == Decimal("370")
        assert modified["validity"] == "GTT" and modified["ret"] == "DAY"
        assert modified["remarks"] == "dip-5" and modified["ai_t"] == "LTP_B_O"
        assert client.pending_gtts()[0].trigger_value == Decimal("370")


@pytest.mark.parametrize("missing", ["exchange", "tradingsymbol", "alert_type", "quantity", "tag"])
def test_move_trigger_refuses_a_row_without_a_needed_field(
    missing: str, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load()
    with OfflineBroker() as broker:
        _broker(module, broker)
        client = broker.client()
        module.buy_below(client, trigger=Decimal("380"), price=Decimal("380"), tag="dip-6")
        (gtt,) = client.pending_gtts()
        assert (
            module.move_trigger(client, gtt.model_copy(update={missing: None}), Decimal("370"))
            is None
        )
        assert _payloads(broker, "ModifyGTTOrder") == []
    assert missing in capsys.readouterr().out


def _stop_loss_gtt(client: Any) -> Any:
    client.place_gtt(
        exchange="NSE", tradingsymbol="ITC-EQ", alert_type="LTP_B_O", trigger_value="380",
        quantity=1, transaction_type="BUY", product="CNC", order_type="SL-LMT", price="382",
        trigger_price="381", tag="dip-sl",
    )  # fmt: skip
    (gtt,) = client.pending_gtts()
    return gtt


def test_move_trigger_resends_the_stop_loss_trigger_price_from_the_row() -> None:
    module = _load()
    with OfflineBroker() as broker:
        _broker(module, broker)
        client = broker.client()
        module.move_trigger(client, _stop_loss_gtt(client), Decimal("370"))
        (modified,) = _payloads(broker, "ModifyGTTOrder")
        assert modified["prctyp"] == "SL-LMT" and Decimal(modified["trgprc"]) == Decimal("381")


def test_move_trigger_refuses_a_stop_loss_row_without_its_trigger_price(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load()
    with OfflineBroker() as broker:
        _broker(module, broker)
        client = broker.client()
        gtt = _stop_loss_gtt(client).model_copy(update={"trigger_price": None})
        assert module.move_trigger(client, gtt, Decimal("370")) is None
        assert _payloads(broker, "ModifyGTTOrder") == []
    assert "trigger_price" in capsys.readouterr().out
