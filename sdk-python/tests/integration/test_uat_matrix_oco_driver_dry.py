"""Dry run of ``oco-far-buy`` through a live-order driver (driver parity, offline).

The SDK under test is a ``DriverSdk`` over ``tests/unit/fake_driver.py`` in relay mode: every
request line the harness writes goes, through the fake driver process, to ``Bridge`` below, which
plays the driver's SDK. It reads the request as a strict driver would (each OCO leg exactly the
protocol's leg object: ``trigger_value``, ``price``, ``order_type`` as a wire code,
``trigger_price``; prices as strings), makes the one call with a second Python client on the same
offline broker, and answers with the wire body it received (account-id keys removed). The case's
reads stay on the referee client, as in a real run. Nothing here reaches a network: the relay is
a loopback socket and the broker is in memory.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.test_uat_matrix_oco_dry import OcoMarket
from tests.integration.uat_family_rig import ACCOUNT, family_rig
from tests.integration.uat_like import UatLikeMarket
from tests.live_orders.uat_matrix import batch, cases_oco
from tests.live_orders.uat_matrix.driver_process import DriverSdk
from tradesmart import APIError, OcoLeg, OrderType, TradeSmart
from tradesmart.testing import OfflineBroker

FAKE = Path(__file__).resolve().parents[1] / "unit" / "fake_driver.py"
NONCE = "nonce-oco-dry-0123456789"
HELLO = {
    "sdk": "javascript",
    "version": "2.1.0",
    "environment": "uat",
    "client_id": ACCOUNT,
    "protocol": 2,
}
LEG_KEYS = {"trigger_value", "price", "order_type", "trigger_price"}
WIRE_ORDER_TYPES = {t.value for t in OrderType}
ACCOUNT_KEYS = {"client_id", "user_id", "account_id", "uid", "actid"}


def _without_accounts(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_accounts(v) for k, v in value.items() if k not in ACCOUNT_KEYS}
    if isinstance(value, list):
        return [_without_accounts(v) for v in value]
    return value


def _price(leg: dict[str, Any], key: str) -> Decimal | None:
    value = leg[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"leg {key} is a {type(value).__name__}, not a string")
    return Decimal(value)


def _leg(name: str, value: Any) -> OcoLeg:
    """A protocol leg object read as the JavaScript driver reads it, but strictly: anything
    other than exactly the four keys, string prices and a wire order type is refused."""
    if not isinstance(value, dict) or set(value) != LEG_KEYS:
        raise ValueError(f"{name} is not the protocol's leg object: {value!r}")
    if value["order_type"] not in WIRE_ORDER_TYPES:
        raise ValueError(f"{name}.order_type {value['order_type']!r} is not a wire code")
    trigger_value = _price(value, "trigger_value")
    if trigger_value is None:
        raise ValueError(f"{name}.trigger_value is missing")
    return OcoLeg(
        trigger_value=trigger_value,
        price=_price(value, "price"),
        order_type=OrderType(value["order_type"]),
        trigger_price=_price(value, "trigger_price"),
    )


class Bridge:
    """The driver's SDK for the relay: one call per request on its own client, never more."""

    def __init__(self, broker: OfflineBroker, market: UatLikeMarket) -> None:
        self.client: TradeSmart = market.install(broker.client())  # OcoMarket keeps leg tags
        self.bodies: list[httpx.Response] = []
        http_client = self.client._transport.http_client
        http_client.event_hooks = {"request": [], "response": [self.bodies.append]}
        self.seen: list[tuple[str, dict[str, Any]]] = []
        self.problems: list[str] = []
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port: int = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.close()
        self.client.close()

    def _serve(self) -> None:
        conn, _ = self.server.accept()
        with conn, conn.makefile("rwb") as peer:
            for line in peer:
                request = json.loads(line)
                reply = {"id": request["id"], **self._answer(request["method"], request["args"])}
                peer.write((json.dumps(reply) + "\n").encode("utf-8"))
                peer.flush()

    def _answer(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        self.seen.append((method, args))
        try:
            kwargs = dict(args)
            if method in ("place_oco", "modify_oco"):
                kwargs["stop_loss"] = _leg("stop_loss", args.get("stop_loss"))
                kwargs["target"] = _leg("target", args.get("target"))
            elif method != "cancel_oco":
                raise ValueError(f"{method} is not a call of this case")
        except (ValueError, ArithmeticError) as exc:
            self.problems.append(f"{method}: {exc}")
            return {"error": {"type": "ValueError", "message": str(exc)}}
        sent = len(self.bodies)
        try:
            if method == "place_oco":
                result: str = self.client.place_oco(**kwargs)
            elif method == "modify_oco":
                result = self.client.modify_oco(kwargs.pop("alert_id"), **kwargs)
            else:
                result = self.client.cancel_oco(kwargs["alert_id"])
        except APIError as exc:
            raw = _without_accounts(exc.raw)
            return {
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "stat": raw.get("stat") if isinstance(raw, dict) else None,
                    "emsg": exc.emsg,
                    "http_status": exc.http_status,
                    "raw": raw,
                }
            }
        if len(self.bodies) != sent + 1:
            self.problems.append(f"{method}: {len(self.bodies) - sent} responses for one call")
        response = self.bodies[-1]
        return {
            "result": result,
            "raw": _without_accounts(response.json()),
            "http_status": response.status_code,
        }


@pytest.fixture
def bridges() -> Iterator[list[Bridge]]:
    made: list[Bridge] = []
    yield made
    for bridge in made:
        bridge.close()


def _driver(tmp_path: Path, made: list[Bridge]) -> Any:
    def build(broker: OfflineBroker, market: UatLikeMarket) -> DriverSdk:
        bridge = Bridge(broker, market)
        made.append(bridge)
        folder = tmp_path / "driver"
        folder.mkdir()
        script = folder / "script.json"
        script.write_text(json.dumps({"hello": HELLO, "relay": bridge.port}), encoding="utf-8")
        environ = {
            "TRADESMART_LIVE_TESTS": "1",
            "TRADESMART_LIVE_ORDER_TESTS": "1",
            "TRADESMART_ENV": "uat",
            "TRADESMART_LIVE_ORDER_ACCOUNT": ACCOUNT,
            "FAKE_DRIVER_SCRIPT": str(script),
            "FAKE_DRIVER_LOG": str(folder / "driver.log"),
        }
        return DriverSdk(
            [sys.executable, str(FAKE)],
            nonce=NONCE,
            environ=environ,
            reply_timeout=20.0,
            referee_client_id=ACCOUNT,
            start_timeout=20.0,
        )

    return build


def _triggers(payload: dict[str, Any]) -> dict[str, Decimal]:
    return {i["var_name"]: Decimal(i["d"]) for i in payload["oivariable"]}


def test_the_far_buy_oco_runs_end_to_end_through_a_driver_sdk(
    tmp_path: Path, bridges: list[Bridge]
) -> None:
    with family_rig(tmp_path, market_type=OcoMarket, sdk=_driver(tmp_path, bridges)) as rig:
        sdk: Any = rig.session.sdk
        assert isinstance(sdk, DriverSdk) and sdk.code == "j"
        result = cases_oco.run_case(rig.run, cases_oco.FAR_BUY)
        assert result.verdict == batch.PASS, result.reason
        [bridge] = bridges
        assert bridge.problems == []
        assert [m for m, _ in bridge.seen] == [
            "place_oco",
            "modify_oco",
            "cancel_oco",
            "modify_oco",
        ]
        place_args = bridge.seen[0][1]
        for name in ("stop_loss", "target"):
            leg = place_args[name]
            assert set(leg) == LEG_KEYS and leg["order_type"] == "LMT"
            assert leg["trigger_price"] is None and isinstance(leg["price"], str)
        assert place_args["stop_loss"]["trigger_value"] == "440.00"
        assert place_args["target"]["trigger_value"] == "360.00"
        assert bridge.seen[1][1]["stop_loss"]["trigger_value"] == "460.00"
        assert "-j" in place_args["tag"]  # the driver's tag code
        assert sdk.findings == [] and sdk.stopped is None
        # The writes reached the broker once each, from the driver's client.
        [place] = rig.requests("PlaceOCOOrder")
        assert _triggers(place) == {"x": Decimal("440.00"), "y": Decimal("360.00")}
        assert len(rig.requests("ModifyOCOOrder")) == 2
        assert len(rig.requests("CancelOCOOrder")) == 1
        assert rig.session.client.pending_gtts() == []
        [record] = rig.records()
        assert record["notes"]["probe"]["exception"] == "OrderRejectedError"
        assert [c["kind"] for c in record["calls"]] == [
            "oco_place",
            "oco_modify",
            "oco_cancel",
            "oco_modify",
        ]
        assert not rig.session.guard.stopped()
