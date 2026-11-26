"""``DriverSdk``, the harness side of a live-order driver process (``live-driver.md``),
and the multi-SDK gates of the matrix (Task 11 review items).

Every driver here is ``tests/unit/fake_driver.py``: a scriptable stand-in that makes no call of
any kind and prints only synthetic values. Every environment is a mapping given to the code
under test; nothing reads or sets a real ``TRADESMART_LIVE_*`` variable.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.live_orders.safety import CHECK_THE_ACCOUNT, Refusal, ResponseLog
from tests.live_orders.uat_matrix import batch, calls, gate
from tests.live_orders.uat_matrix import conftest as matrix_conftest
from tests.live_orders.uat_matrix.calls import CaseRef
from tests.live_orders.uat_matrix.contracts import Contracts
from tests.live_orders.uat_matrix.driver_process import (
    DRIVER_RUN_ENV,
    DriverSdk,
    default_reply_timeout,
)
from tests.live_orders.uat_matrix.expect import Profile
from tests.live_orders.uat_matrix.guard import Guard
from tests.live_orders.uat_matrix.ledger import LEDGER_NAME, TAG_MAX, Ledger
from tests.live_orders.uat_matrix.positions import POSITIONS_NAME
from tests.live_orders.uat_matrix.run_group import RunGroup
from tests.live_orders.uat_matrix.sdk_under_test import SDK_CODES
from tests.unit.test_uat_matrix_safety import ACCOUNT, MATRIX_ENV, NOW, PLAN, FakeTime, MatrixClient
from tradesmart import (
    NetworkError,
    OcoLeg,
    OrderRejectedError,
    OrderStatusUnknownError,
    OrderType,
    RequestValidationError,
    SlicedOrderResult,
)

FAKE = Path(__file__).with_name("fake_driver.py")
REPO = Path(__file__).resolve().parents[3]
NONCE = "nonce-0123456789abcdef"
SYNTHETIC_TOKEN = "synthetic-token-not-real-0001"
# A saved-login token: never in the environment (a driver reads it from the token file).
LOGIN_TOKEN = "synthetic-login-token-not-real-0002"
DRIVER_ENV = {
    "TRADESMART_LIVE_TESTS": "1",
    "TRADESMART_LIVE_ORDER_TESTS": "1",
    "TRADESMART_ENV": "uat",
    "TRADESMART_LIVE_ORDER_ACCOUNT": ACCOUNT,
    "TRADESMART_ACCESS_TOKEN": SYNTHETIC_TOKEN,
}
HELLO = {
    "sdk": "javascript",
    "version": "2.0.0",
    "environment": "uat",
    "client_id": ACCOUNT.lower(),  # compared case-insensitively
    "protocol": 2,
}
PLACE_RAW = {"stat": "Ok", "norenordno": "1001", "request_time": "10:00:00 13-11-2026"}
NSE = CaseRef("m1-nse-itc-cnc-lmt-day-buy", "nse")


# -- helpers --------------------------------------------------------------------------------------


class Fake:
    """One fake driver: its script, its request log and the ``DriverSdk`` built on it."""

    def __init__(self, tmp_path: Path, script: dict[str, Any], name: str = "drv") -> None:
        self.dir = tmp_path / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.script = self.dir / "script.json"
        self.log = self.dir / "driver.log"
        self.script.write_text(json.dumps(script), encoding="utf-8")
        self.environ = {
            **DRIVER_ENV,
            "FAKE_DRIVER_SCRIPT": str(self.script),
            "FAKE_DRIVER_LOG": str(self.log),
        }

    def start(self, *, reply_timeout: float = 5.0, **kwargs: Any) -> DriverSdk:
        kwargs.setdefault("referee_client_id", ACCOUNT)
        kwargs.setdefault("start_timeout", 20.0)
        return DriverSdk(
            [sys.executable, str(FAKE)],
            nonce=NONCE,
            environ=self.environ,
            reply_timeout=reply_timeout,
            **kwargs,
        )

    def entries(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]

    def requests(self) -> list[dict[str, Any]]:
        return [json.loads(e["request"]) for e in self.entries() if "request" in e]

    def request_lines(self) -> list[str]:
        return [e["request"] for e in self.entries() if "request" in e]


class Hooks:
    """What ``DriverSdk.bind`` reports: ledger notes and stops."""

    def __init__(self) -> None:
        self.notes: list[tuple[str, dict[str, Any]]] = []
        self.stops: list[tuple[str, bool]] = []
        self.responses = ResponseLog()

    def note(self, kind: str, **detail: Any) -> None:
        self.notes.append((kind, detail))

    def stop(self, reason: str, check_the_account: bool) -> None:
        self.stops.append((reason, check_the_account))

    def bind(self, sdk: DriverSdk) -> None:
        sdk.bind(note=self.note, stop=self.stop, responses=self.responses)


def _script(*replies: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"hello": HELLO, "replies": list(replies), **extra}


def _running(pid: int) -> bool:
    """The process runs (a zombie nobody reaped does not count). Checked for real on every CI
    platform: ``/proc`` on Linux, ``tasklist`` on Windows, signal 0 elsewhere (macOS)."""
    if os.name == "nt":
        listed = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return f'"{pid}"' in listed
    if Path("/proc").is_dir():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return False
        return stat.rsplit(")", 1)[-1].split()[0] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _gone(pid: int, wait: float = 5.0) -> bool:
    """The process no longer runs, within ``wait`` seconds."""
    deadline = time.monotonic() + wait
    while _running(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


PLACE_KWARGS: dict[str, Any] = {**PLAN, "tag": "sdkuat-1113abcd-j000-1"}
# The quantity the harness sends for a sliced place: the fixtures' requested_quantity (30).
SLICED_KWARGS: dict[str, Any] = {**PLACE_KWARGS, "quantity": 30}


# -- the handshake --------------------------------------------------------------------------------


def test_the_handshake_names_the_sdk_and_passes_the_nonce(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script())
    sdk = fake.start()
    try:
        assert (sdk.name, sdk.code, sdk.version) == ("javascript", "j", "2.0.0")
        assert sdk.client_id.upper() == ACCOUNT
        assert fake.entries()[0] == {"started": True, "nonce": NONCE}
        assert fake.requests() == []  # nothing is sent before a call
    finally:
        sdk.close()


@pytest.mark.parametrize(
    ("hello", "why"),
    [
        ({**HELLO, "client_id": "OTHER9"}, "account"),
        ({**HELLO, "run": None}, "nonce"),
        ({**HELLO, "run": "another-run"}, "nonce"),
        ({key: v for key, v in HELLO.items() if key != "version"}, "version"),
        ({key: v for key, v in HELLO.items() if key != "sdk"}, "sdk"),
        ({**HELLO, "sdk": "cobol"}, "sdk"),
        ({**HELLO, "protocol": 3}, "protocol"),
        ({**HELLO, "environment": "production"}, "environment"),
        ("not json at all", "handshake"),
        ('{"id": 1, "result": "1001"}', "handshake"),
    ],
)
def test_a_bad_handshake_is_refused_before_any_order(tmp_path: Path, hello: Any, why: str) -> None:
    fake = Fake(tmp_path, {"hello": hello, "child": True})
    with pytest.raises(Refusal, match=why) as info:
        fake.start()
    assert ACCOUNT not in info.value.reason.upper()  # a refusal never names the account
    assert "OTHER9" not in info.value.reason
    assert fake.requests() == []
    child = next(e["child"] for e in fake.entries() if "child" in e)
    assert _gone(child)  # the whole process tree is killed


def test_the_referee_client_id_must_match_the_handshake(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script())
    with pytest.raises(Refusal, match="account"):
        fake.start(referee_client_id="OTHER9")
    assert fake.requests() == []


def test_a_driver_that_never_says_hello_is_refused(tmp_path: Path) -> None:
    fake = Fake(tmp_path, {"hello": None})
    started = time.monotonic()
    with pytest.raises(Refusal, match="handshake"):
        fake.start(start_timeout=1.0)
    assert time.monotonic() - started < 10


def test_a_refusing_driver_keeps_its_stderr_out_of_the_reason(tmp_path: Path) -> None:
    lines = [f"{{ accessToken: '{LOGIN_TOKEN}' }}", "fake: details of the refusal"]
    fake = Fake(tmp_path, {"refuse": True, "stderr": lines})
    log = tmp_path / "driver-stderr.jsonl"
    with pytest.raises(Refusal) as info:
        fake.start(stderr_log=log, secrets=[LOGIN_TOKEN])
    reason = info.value.reason
    assert LOGIN_TOKEN not in reason and "details of the refusal" not in reason  # never shown
    assert "refusing to start" not in reason
    kept = "\n".join(info.value.driver_stderr)  # type: ignore[attr-defined]
    assert "details of the refusal" in kept and "refusing to start" in kept
    assert LOGIN_TOKEN not in kept
    recorded = log.read_text(encoding="utf-8")
    assert "details of the refusal" in recorded and LOGIN_TOKEN not in recorded


def test_an_empty_nonce_starts_nothing(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script())
    with pytest.raises(Refusal, match="nonce"):
        DriverSdk([sys.executable, str(FAKE)], nonce=" ", environ=fake.environ, reply_timeout=5.0)
    assert fake.entries() == []


def test_the_account_variable_is_required(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script())
    del fake.environ["TRADESMART_LIVE_ORDER_ACCOUNT"]
    with pytest.raises(Refusal, match="TRADESMART_LIVE_ORDER_ACCOUNT"):
        fake.start()


# -- writes answered ------------------------------------------------------------------------------


def test_a_write_answered_normally_returns_its_result(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script({"result": "1001", "raw": PLACE_RAW, "http_status": 200}))
    sdk = fake.start()
    hooks = Hooks()
    hooks.bind(sdk)
    try:
        assert sdk.place_order(**PLACE_KWARGS) == "1001"
        assert sdk.findings == []
    finally:
        sdk.close()
    (request,) = fake.requests()
    assert request["method"] == "place_order"
    assert request["args"]["price"] == "360.00"  # Decimals as strings
    assert request["args"]["tag"] == PLACE_KWARGS["tag"]
    assert isinstance(request["id"], int)
    assert hooks.stops == []


def test_a_result_id_that_differs_from_the_wire_body_is_unknown_and_a_finding(
    tmp_path: Path,
) -> None:
    raw = {**PLACE_RAW, "norenordno": "1002"}
    fake = Fake(tmp_path, _script({"result": "1001", "raw": raw, "http_status": 200}))
    sdk = fake.start()
    hooks = Hooks()
    hooks.bind(sdk)
    try:
        with pytest.raises(OrderStatusUnknownError):  # the harness never acts on the id
            sdk.place_order(**PLACE_KWARGS)
        assert sdk.process.poll() is not None
    finally:
        sdk.close()
    (finding,) = sdk.findings
    assert finding["method"] == "place_order" and finding["field"] == "result"
    assert finding["driver"] == "1001" and finding["python"] == "1002"
    assert ("driver_finding", finding) in hooks.notes
    ((_, check),) = hooks.stops
    assert check is True  # CHECK THE ACCOUNT
    assert len(fake.request_lines()) == 1


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ({"stat": "Oi created", "al_id": "A1"}, True),
        ({"stat": "oi CREATED", "al_id": "A1"}, True),  # letter case ignored (F32)
        ({"stat": "Oi created", "al_id": "A2"}, False),  # another alert id
        ({"stat": "Ok", "al_id": "A1"}, False),  # not the documented word
        ({"stat": "Oi created"}, False),  # no al_id
    ],
)
def test_a_gtt_id_comes_from_the_wire_body(tmp_path: Path, raw: dict[str, Any], ok: bool) -> None:
    fake = Fake(tmp_path, _script({"result": "A1", "raw": raw, "http_status": 200}))
    sdk = fake.start()
    try:
        if ok:
            assert sdk.place_gtt(**PLACE_KWARGS) == "A1"
        else:
            with pytest.raises(OrderStatusUnknownError):
                sdk.place_gtt(**PLACE_KWARGS)
    finally:
        sdk.close()


LEG_KWARGS: dict[str, Any] = {
    "exchange": "NSE",
    "tradingsymbol": "ITC-EQ",
    "transaction_type": "BUY",
    "quantity": 1,
    "product": "CNC",
    "validity": "DAY",
    "alert_validity": "DAY",
    "tag": "sdkuat-1120abcd-j000-1",
    "stop_loss": OcoLeg(trigger_value=Decimal("440.00"), price=Decimal("380.00")),
    "target": OcoLeg(
        trigger_value=Decimal("360.00"),
        price=Decimal("355.00"),
        order_type=OrderType.SL,
        trigger_price=Decimal("358.00"),
    ),
}


def test_oco_legs_go_as_the_protocols_leg_objects(tmp_path: Path) -> None:
    """``place_oco`` / ``modify_oco`` send each ``OcoLeg`` as the protocol's leg object
    (``live-driver.md``): prices as strings, the order type's wire code, a ``null``
    ``trigger_price`` when the leg has none, and nothing else."""
    fake = Fake(
        tmp_path,
        _script(
            {"result": "A1", "raw": {"stat": "Oi created", "al_id": "A1"}, "http_status": 200},
            {"result": "A1", "raw": {"stat": "Oi replaced", "al_id": "A1"}, "http_status": 200},
        ),
    )
    sdk = fake.start()
    changed = {
        **LEG_KWARGS,
        "stop_loss": OcoLeg(trigger_value=Decimal("460.00"), price=None, order_type="LIMIT"),
    }
    try:
        assert sdk.place_oco(**LEG_KWARGS) == "A1"
        assert sdk.modify_oco("A1", **changed) == "A1"
    finally:
        sdk.close()
    place, modify = fake.requests()
    assert place["method"] == "place_oco"
    assert place["args"]["stop_loss"] == {
        "trigger_value": "440.00",
        "price": "380.00",
        "order_type": "LMT",
        "trigger_price": None,
    }
    assert place["args"]["target"] == {
        "trigger_value": "360.00",
        "price": "355.00",
        "order_type": "SL-LMT",
        "trigger_price": "358.00",
    }
    assert place["args"]["tag"] == LEG_KWARGS["tag"] and place["args"]["quantity"] == 1
    assert modify["method"] == "modify_oco" and modify["args"]["alert_id"] == "A1"
    assert modify["args"]["stop_loss"] == {
        "trigger_value": "460.00",
        "price": None,
        "order_type": "LMT",  # a plain word is sent as its wire code
        "trigger_price": None,
    }
    assert sdk.findings == []


def test_an_oco_leg_with_a_binary_float_is_never_sent(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script())
    sdk = fake.start()
    bad = {**LEG_KWARGS, "stop_loss": OcoLeg(trigger_value=440.5, price=Decimal("380.00"))}
    try:
        with pytest.raises(RequestValidationError, match="nothing was sent"):
            sdk.place_oco(**bad)
        assert sdk.process.poll() is None  # refused before the request: the driver goes on
    finally:
        sdk.close()
    assert fake.requests() == []


def test_a_rejection_raises_the_python_error_with_its_fields(tmp_path: Path) -> None:
    raw = {"stat": "Not_Ok", "emsg": "RMS: margin exceeds"}
    error = {
        "type": "OrderRejectedError",
        "message": "PlaceOrder failed: RMS: margin exceeds",
        "stat": "Not_Ok",
        "emsg": "RMS: margin exceeds",
        "http_status": 200,
        "raw": raw,
        "sliced_result": None,
        "fields": [],
    }
    fake = Fake(tmp_path, _script({"error": error}))
    sdk = fake.start()
    hooks = Hooks()
    hooks.bind(sdk)
    try:
        with pytest.raises(OrderRejectedError) as info:
            sdk.place_order(**PLACE_KWARGS)
        assert sdk.process.poll() is None  # a definitive answer: the driver keeps running
    finally:
        sdk.close()
    exc = info.value
    assert exc.raw == raw and exc.emsg == "RMS: margin exceeds" and exc.http_status == 200
    assert exc.endpoint == "PlaceOrder"
    assert len(fake.requests()) == 1
    assert hooks.stops == []
    seen = hooks.responses.last("PlaceOrder")
    assert seen is not None and seen["body"] == raw


def test_a_validation_refusal_raises_request_validation_error(tmp_path: Path) -> None:
    error = {
        "type": "RequestValidationError",
        "message": "Invalid request parameters: prc",
        "stat": None,
        "emsg": None,
        "http_status": None,
        "raw": None,
        "sliced_result": None,
        "fields": ["prc"],
    }
    fake = Fake(tmp_path, _script({"error": error}))
    sdk = fake.start()
    try:
        with pytest.raises(RequestValidationError) as info:
            sdk.place_order(**PLACE_KWARGS)
    finally:
        sdk.close()
    assert [entry["loc"] for entry in info.value.errors] == [("prc",)]


def test_the_sdks_own_unknown_outcome_is_raised_and_the_driver_keeps_running(
    tmp_path: Path,
) -> None:
    raw = {"unexpected": "shape"}
    error: dict[str, Any] = {
        "type": "OrderStatusUnknownError",
        "message": "response is not the expected JSON",
        "stat": None,
        "emsg": None,
        "http_status": 502,
        "raw": raw,
        "sliced_result": None,
        "fields": [],
    }
    fake = Fake(tmp_path, _script({"error": error}))
    sdk = fake.start()
    hooks = Hooks()
    hooks.bind(sdk)
    try:
        with pytest.raises(OrderStatusUnknownError) as info:
            sdk.cancel_order("1001")
        assert sdk.process.poll() is None
    finally:
        sdk.close()
    assert info.value.http_status == 502
    assert info.value.body is not None and json.loads(info.value.body) == raw  # body is raw
    assert info.value.endpoint == "CancelOrder"
    assert hooks.stops == []


def _slice(order_id: str, *, wire_id: str | None = None, **raw: Any) -> dict[str, Any]:
    body = {**PLACE_RAW, "norenordno": wire_id or order_id, **raw}
    return {"order_id": order_id, "raw": body, "http_status": 200}


def _sliced_result(**changes: Any) -> dict[str, Any]:
    result = {
        "requested_quantity": 30,
        "slice_quantities": [20, 10],
        "order_ids": ["1001", "1002"],
        "placed_quantity": 30,
        "unsent_quantity": 0,
        "complete": True,
        "error": None,
        "slices": [_slice("1001"), _slice("1002")],
    }
    result.update(changes)
    return result


def test_a_sliced_result_is_rebuilt_from_its_slices_wire_bodies(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script({"result": _sliced_result()}))
    sdk = fake.start()
    hooks = Hooks()
    hooks.bind(sdk)
    mark = hooks.responses.mark()
    try:
        out = sdk.place_sliced_order(**SLICED_KWARGS, max_slices=2, max_slice_quantity=None)
    finally:
        sdk.close()
    assert isinstance(out, SlicedOrderResult)
    assert out.order_ids == ("1001", "1002") and out.slice_quantities == (20, 10)
    assert out.complete and out.error is None
    assert hooks.stops == [] and sdk.findings == []
    assert len(hooks.responses._responses[mark:]) == 2  # each slice's body is recorded


# A definite failure of the next slice, as a returned incomplete result names it.
SLICE_REJECTED: dict[str, Any] = {
    "type": "OrderRejectedError", "message": "slice 3 rejected", "stat": "Not_Ok", "emsg": "RMS",
    "http_status": 200, "raw": {"stat": "Not_Ok", "emsg": "RMS"}, "sliced_result": None,
    "fields": [],
}  # fmt: skip
# An ambiguous slice: Python raises it (with the partial result), never returns it.
SLICE_UNKNOWN: dict[str, Any] = {
    "type": "OrderStatusUnknownError", "message": "slice 3: read timeout", "stat": None,
    "emsg": None, "http_status": None, "raw": None, "sliced_result": None, "fields": [],
}  # fmt: skip
# Two of three slices of 10 placed, the third refused: a consistent incomplete result.
TWO_OF_THREE: dict[str, Any] = {
    "slice_quantities": [10, 10, 10], "placed_quantity": 20, "unsent_quantity": 10,
    "complete": False,
}  # fmt: skip

# Each case has exactly one defect; ``why`` is the reason the stop must name, so a case can
# never pass on another rule.
SLICED_FAILURES = [
    pytest.param({"slices": None}, "without one wire body per placed slice", id="no-slices"),
    pytest.param(
        {"slices": [_slice("1001")]}, "without one wire body per placed slice", id="fewer-slices"
    ),
    pytest.param(
        {"slices": [_slice("1001"), _slice("1002"), _slice("1003")]},
        "without one wire body per placed slice",
        id="more-slices",
    ),
    pytest.param(
        {"slices": [_slice("1001"), _slice("1002", stat="Not_Ok")]},
        "its wire body is not one for the Python SDK",
        id="slice-not-ok",
    ),
    pytest.param(
        {"slices": [_slice("1001"), _slice("1002", wire_id="9999")]},
        "returned the id '1002' for place_order, but its wire body says '9999'",
        id="entry-id",
    ),
    pytest.param(
        {"order_ids": ["1001", "9999"]}, "listed the slice id '9999'", id="order-id-differs"
    ),
    pytest.param(
        {"placed_quantity": 20, "unsent_quantity": 10},
        "placed_quantity is not its slices'",
        id="placed-quantity",
    ),
    pytest.param(
        {"slices": [_slice("1001"), {"order_id": "1002", "http_status": 200}]},
        "without its order_id and raw wire body",
        id="slice-no-raw",
    ),
    # The result must agree with itself, as Python's SliceProgress.result builds it.
    pytest.param(
        {"order_ids": ["1001", "1001"], "slices": [_slice("1001"), _slice("1001")]},
        "lists one order id twice",
        id="duplicate-ids",
    ),
    pytest.param(
        {**TWO_OF_THREE, "complete": True},
        "complete does not match its ids",
        id="complete-with-fewer-ids-than-slices",
    ),
    pytest.param(
        {"complete": False, "error": SLICE_REJECTED},
        "complete does not match its ids",
        id="incomplete-with-every-id",
    ),
    pytest.param(
        {"unsent_quantity": 5},
        "unsent_quantity is not the rest",
        id="unsent-not-requested-minus-placed",
    ),
    pytest.param(
        {**TWO_OF_THREE, "error": None},
        "incomplete sliced result without its error",
        id="incomplete-without-an-error",
    ),
    # Python plans positive slices that add up to the quantity it was asked for (plan_slices).
    pytest.param(
        {"slice_quantities": [10, 10], "placed_quantity": 20, "unsent_quantity": 10},
        "slices do not add up to its requested_quantity",
        id="complete-but-slices-short-of-the-request",
    ),
    pytest.param(
        {
            "requested_quantity": 40,
            "slice_quantities": [20, 10, 10],
            "order_ids": ["1001", "1002"],
            "placed_quantity": 30,
            "unsent_quantity": 10,
            "complete": False,
            "error": SLICE_REJECTED,
        },
        "requested_quantity is not the quantity sent",
        id="requested-not-the-quantity-sent",
    ),  # fmt: skip
    pytest.param(
        {"slice_quantities": [30, 0]},
        "slice_quantities are not positive integers",
        id="a-zero-slice",
    ),
    pytest.param(
        {"slice_quantities": [40, -10]},
        "slice_quantities are not positive integers",
        id="a-negative-slice",
    ),
    # done() never names an error; stopped() never returns an unknown outcome (it is raised).
    pytest.param(
        {"error": SLICE_REJECTED},
        "complete sliced result with an error",
        id="complete-with-an-error",
    ),
    pytest.param(
        {**TWO_OF_THREE, "error": SLICE_UNKNOWN},
        "returned an unknown outcome as its error",
        id="an-unknown-outcome-returned-as-the-error",
    ),
]


@pytest.mark.parametrize(("changes", "why"), SLICED_FAILURES)
def test_a_sliced_result_not_backed_by_its_wire_bodies_is_unknown(
    tmp_path: Path, changes: dict[str, Any], why: str
) -> None:
    fake = Fake(tmp_path, _script({"result": _sliced_result(**changes)}))
    sdk = fake.start()
    hooks = Hooks()
    hooks.bind(sdk)
    try:
        with pytest.raises(OrderStatusUnknownError):
            sdk.place_sliced_order(**SLICED_KWARGS, max_slices=3, max_slice_quantity=None)
        assert sdk.process.poll() is not None
    finally:
        sdk.close()
    assert len(fake.request_lines()) == 1
    ((reason, check),) = hooks.stops
    assert check is True
    assert why in reason


def _unknown_with_partial(partial: dict[str, Any]) -> dict[str, Any]:
    return {
        "error": {
            "type": "OrderStatusUnknownError",
            "message": "slice 3: read timeout",
            "stat": None,
            "emsg": None,
            "http_status": None,
            "raw": None,
            "sliced_result": partial,
            "fields": [],
        }
    }


def test_a_partial_sliced_result_in_an_error_comes_from_its_wire_bodies(tmp_path: Path) -> None:
    partial = _sliced_result(complete=False, slice_quantities=[10, 10, 10], placed_quantity=20,
                             unsent_quantity=10)  # fmt: skip
    fake = Fake(tmp_path, _script(_unknown_with_partial(partial)))
    sdk = fake.start()
    hooks = Hooks()
    hooks.bind(sdk)
    try:
        with pytest.raises(OrderStatusUnknownError) as info:
            sdk.place_sliced_order(**SLICED_KWARGS, max_slices=3, max_slice_quantity=None)
        assert sdk.process.poll() is None  # the SDK's own unknown outcome: the driver stays
    finally:
        sdk.close()
    assert info.value.sliced_result is not None
    assert info.value.sliced_result.order_ids == ("1001", "1002")
    assert hooks.stops == []


PARTIAL_FAILURES = [
    # The wrong id is the only defect: two of three slices placed, consistent otherwise.
    pytest.param(
        {**TWO_OF_THREE, "order_ids": ["1001", "9999"]},
        "listed the slice id '9999'",
        id="a-wrong-id",
    ),
    # Python's partial result (SliceProgress.unknown) never names an error of its own ...
    pytest.param(
        {**TWO_OF_THREE, "error": SLICE_REJECTED},
        "partial sliced result with an error of its own",
        id="an-error-of-its-own",
    ),
    # ... and is never complete: a slice had an unknown outcome, so it has no id.
    pytest.param({}, "partial sliced result that is complete", id="complete"),
    # The same plan rules as a returned result.
    pytest.param(
        {
            "slice_quantities": [10, 10],
            "order_ids": ["1001"],
            "slices": [_slice("1001")],
            "placed_quantity": 10,
            "unsent_quantity": 20,
            "complete": False,
        },
        "slices do not add up to its requested_quantity",
        id="slices-short-of-the-request",
    ),  # fmt: skip
]


@pytest.mark.parametrize(("changes", "why"), PARTIAL_FAILURES)
def test_a_broken_partial_sliced_result_stops_the_driver(
    tmp_path: Path, changes: dict[str, Any], why: str
) -> None:
    partial = _sliced_result(**changes)
    fake = Fake(tmp_path, _script(_unknown_with_partial(partial)))
    sdk = fake.start()
    hooks = Hooks()
    hooks.bind(sdk)
    try:
        with pytest.raises(OrderStatusUnknownError):
            sdk.place_sliced_order(**SLICED_KWARGS, max_slices=3, max_slice_quantity=None)
        assert sdk.process.poll() is not None
    finally:
        sdk.close()
    assert len(fake.request_lines()) == 1
    ((reason, check),) = hooks.stops
    assert check is True
    assert why in reason


def test_positional_arguments_go_by_name(tmp_path: Path) -> None:
    fake = Fake(
        tmp_path,
        _script(
            {"result": "1001", "raw": {"stat": "Ok", "result": "1001"}, "http_status": 200},
            {"result": "1001", "raw": {"stat": "Ok"}, "http_status": 200},
        ),
    )
    sdk = fake.start()
    try:
        sdk.modify_order("1001", price=Decimal("361.5"), quantity=1)
        sdk.exit_order("1001", "CO")
    finally:
        sdk.close()
    first, second = fake.requests()
    assert first["args"] == {"order_id": "1001", "price": "361.5", "quantity": 1}
    assert second == {"id": second["id"], "method": "exit_order",
                      "args": {"order_id": "1001", "product": "CO"}}  # fmt: skip
    assert first["id"] != second["id"]


# -- Review Focus 2: no answer, a half line, an exit mid-write ------------------------------------


FAILURES = [
    pytest.param({"hang": True}, id="hang"),
    pytest.param({"half": '{"id": 1, "result": "10'}, id="half-line"),
    pytest.param({"exit": 0}, id="exit-mid-write"),
    pytest.param({"line": "this is not json"}, id="malformed"),
    pytest.param({"id": 999, "result": "1001"}, id="out-of-order"),
    pytest.param({"result": ""}, id="empty-result"),
    pytest.param({"error": {"type": "MadeUpError", "message": "x"}}, id="unknown-error-type"),
    pytest.param({"result": "1001"}, id="success-without-raw"),
    pytest.param({"result": "1001", "raw": {**PLACE_RAW, "norenordno": "1002"}}, id="wrong-id"),
    pytest.param(
        {"result": "1001", "raw": {"stat": "Not_Ok", "emsg": "RMS: x"}}, id="not-ok-as-success"
    ),
    pytest.param({"result": "1001", "raw": ["not", "an", "ack"]}, id="raw-not-an-object"),
]


@pytest.mark.parametrize("step", FAILURES)
def test_a_failed_write_is_unknown_never_resent_and_the_driver_is_killed(
    tmp_path: Path, step: dict[str, Any]
) -> None:
    fake = Fake(tmp_path, _script(step, child=True))
    sdk = fake.start(reply_timeout=1.0)
    hooks = Hooks()
    hooks.bind(sdk)
    started = time.monotonic()
    try:
        with pytest.raises(OrderStatusUnknownError) as info:
            sdk.place_order(**PLACE_KWARGS)
        assert time.monotonic() - started < 10
        assert info.value.endpoint == "PlaceOrder"
        assert len(fake.request_lines()) == 1  # exactly one request line: never resent
        assert sdk.process.poll() is not None  # the driver is killed
        child = next(e["child"] for e in fake.entries() if "child" in e)
        assert _gone(child)  # and its whole process tree
        (stop,) = hooks.stops
        assert stop[1] is True  # an unknown outcome on a write is CHECK THE ACCOUNT
        assert "javascript" in stop[0]
        assert sdk.stopped is not None
        # Every later call is refused before anything is written (nothing sent).
        with pytest.raises(RequestValidationError, match="stopped"):
            sdk.cancel_order("1001")
        assert len(fake.request_lines()) == 1
    finally:
        sdk.close()


def test_a_failed_read_is_a_network_error_and_stops_without_check_the_account(
    tmp_path: Path,
) -> None:
    fake = Fake(tmp_path, _script({"hang": True}))
    sdk = fake.start(reply_timeout=1.0)
    hooks = Hooks()
    hooks.bind(sdk)
    try:
        with pytest.raises(NetworkError):
            sdk.call("orders")
        assert sdk.process.poll() is not None
        assert len(fake.request_lines()) == 1
        ((reason, check),) = hooks.stops
        assert check is False and "javascript" in reason
    finally:
        sdk.close()


def test_a_read_answered_returns_its_result(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script({"result": [{"norenordno": "1001"}]}))
    sdk = fake.start()
    try:
        assert sdk.call("orders") == [{"norenordno": "1001"}]
    finally:
        sdk.close()


def test_the_sliced_place_waits_longer_than_one_call(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script())
    sdk = fake.start(reply_timeout=3.0)
    try:
        assert sdk.timeout_for("place_order", {}) == 3.0
        assert sdk.timeout_for("place_sliced_order", {"max_slices": 4}) == 3.0 * (4 + 2)
    finally:
        sdk.close()


# -- stderr ---------------------------------------------------------------------------------------


def test_stderr_is_captured_redacted_and_never_echoed(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    lines = [
        f"debug: Authorization: Bearer {SYNTHETIC_TOKEN}",
        f"debug: access_token={SYNTHETIC_TOKEN}&x=1",
        f"debug: the token is {SYNTHETIC_TOKEN}",
        f"debug: account {ACCOUNT} ready, run {NONCE}",
        "debug: ordinary line",
    ]
    fake = Fake(tmp_path, _script({"result": "1001", "raw": PLACE_RAW}, stderr=lines))
    sdk = fake.start()
    hooks = Hooks()
    hooks.bind(sdk)
    sdk.place_order(**PLACE_KWARGS)
    sdk.close()
    text = "\n".join(sdk.stderr_lines)
    assert "debug: ordinary line" in text
    assert SYNTHETIC_TOKEN not in text and ACCOUNT not in text and NONCE not in text
    assert "***" in text
    recorded = json.dumps(hooks.notes)
    assert "driver_stderr" in recorded and "ordinary line" in recorded
    assert SYNTHETIC_TOKEN not in recorded and ACCOUNT not in recorded
    out, err = capfd.readouterr()
    assert SYNTHETIC_TOKEN not in out + err


# -- close ----------------------------------------------------------------------------------------


def test_close_ends_the_driver_and_its_process_tree(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script(child=True))
    sdk = fake.start()
    child = next(e["child"] for e in fake.entries() if "child" in e)
    sdk.close()
    assert sdk.process.poll() is not None
    assert _gone(child)
    with pytest.raises(RequestValidationError):
        sdk.cancel_order("1001")
    sdk.close()  # twice is harmless


def test_close_never_waits_on_a_pipe_a_surviving_process_still_holds(tmp_path: Path) -> None:
    # A process the tree kill cannot reach keeps the driver's stdout and stderr open, so the
    # reader threads stay blocked in readline. Closing a stream under a blocked reader waits for
    # it forever (the Windows CI hang: there, process.kill() reached the driver only).
    fake = Fake(tmp_path, _script(escaped_child=True))
    sdk = fake.start()
    escaped = next(e["escaped_child"] for e in fake.entries() if "escaped_child" in e)
    try:
        closer = threading.Thread(target=sdk.close, daemon=True)
        closer.start()
        closer.join(30.0)
        assert not closer.is_alive(), "close() hung on a pipe that another process holds open"
        assert sdk.process.poll() is not None
    finally:
        with contextlib.suppress(OSError):
            os.kill(escaped, signal.SIGTERM)


def test_the_reply_timeout_is_stated_in_the_protocol_document() -> None:
    timeout = default_reply_timeout()
    assert timeout == 30.0
    text = (REPO / "specifications" / "live-driver.md").read_text(encoding="utf-8")
    assert "30 seconds" in text and "reply timeout" in text.lower()
    assert DRIVER_RUN_ENV in text


# -- the session: seam, raw bodies, stops ---------------------------------------------------------


class FakeSdk:
    """A non-Python ``SdkUnderTest`` that records its writes (no process)."""

    def __init__(self, name: str = "javascript", client_id: str = ACCOUNT) -> None:
        self.name = name
        self.code = SDK_CODES[name]
        self.version = "0.0.0"
        self.client_id = client_id
        self.seen: list[tuple[str, dict[str, Any]]] = []
        self.closed = 0

    def send_slot(self) -> contextlib.AbstractContextManager[Any]:
        return contextlib.nullcontext()

    def place_order(self, **kwargs: Any) -> str:
        self.seen.append(("place_order", kwargs))
        return "1001"

    def cancel_order(self, order_id: str) -> str:
        self.seen.append(("cancel_order", {"order_id": order_id}))
        return order_id

    def close(self) -> None:
        self.closed += 1


def _session(
    client: MatrixClient,
    tmp_path: Path,
    *,
    environ: dict[str, str] | None = None,
    group: RunGroup | None = None,
    sdk_name: str = "python",
    factory: Any = None,
) -> gate.MatrixSession:
    t = FakeTime()
    return gate.matrix_session(
        environ if environ is not None else MATRIX_ENV,
        client,  # type: ignore[arg-type]
        root=tmp_path,
        now=NOW,
        sleep=t.sleep,
        clock=t.clock,
        sdk_name=sdk_name,
        sdk_factory=factory,
        group=group,
    )


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_a_write_through_calls_reaches_the_driver_with_a_j_tag(tmp_path: Path) -> None:
    raw = {**PLACE_RAW, "uid": ACCOUNT, "actid": ACCOUNT}
    fake = Fake(tmp_path, _script({"result": "1001", "raw": raw, "http_status": 200}))
    client = MatrixClient()
    session = _session(
        client, tmp_path / "runs", sdk_name="javascript", factory=lambda: fake.start()
    )
    try:
        assert session.sdk.code == "j" and session.ledger.sdk_code == "j"
        session.guard.reserve(NSE.case_id, 1)
        outcome = calls.place(session, PLAN, NSE)
    finally:
        session.sdk.close()
    assert outcome.verdict == calls.ACKNOWLEDGED and outcome.order_id == "1001"
    assert client.count("place_order") == 0  # the Python client's write is never called
    assert re.fullmatch(rf"sdkuat-{session.run_id}-j[0-9a-z]{{3}}-\d+", outcome.tag)
    assert len(outcome.tag) <= TAG_MAX
    (request,) = fake.requests()
    assert request["args"]["tag"] == outcome.tag
    # The raw body is recorded in the outcome the matrix reads, redacted like the recorder's.
    (line,) = [x for x in _lines(session.ledger.path) if x.get("type") == "outcome"]
    response = line["detail"]["response"]
    assert response["http_status"] == 200
    assert response["body"]["norenordno"] == "1001"
    assert "uid" not in response["body"] and "actid" not in response["body"]
    assert response["request"]["method"] == "place_order"
    assert ACCOUNT not in json.dumps(line)


def test_a_driver_hang_through_calls_stops_the_run_and_every_later_sdk(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script({"hang": True}))
    client = MatrixClient()
    group = RunGroup(("javascript", "python"))
    root = tmp_path / "runs"
    session = _session(
        client,
        root,
        sdk_name="javascript",
        group=group,
        factory=lambda: fake.start(reply_timeout=1.0),
    )
    try:
        session.guard.reserve(NSE.case_id, 1)
        outcome = calls.place(session, PLAN, NSE)
    finally:
        session.sdk.close()
    assert outcome.check_the_account  # the book does not list it
    assert len(fake.request_lines()) == 1
    assert session.guard.stopped()
    assert session.guard.stop_reason is not None and "javascript" in session.guard.stop_reason
    stops = [x for x in _lines(session.ledger.path) if x.get("type") == "stop"]
    assert stops and "javascript" in stops[0]["reason"]
    assert session.item_refusal(gate.SMOKE) is not None  # every later case of the SDK skips
    matrix_conftest.end_session(session, group)
    assert group.check_the_account is not None
    with pytest.raises(Refusal, match=CHECK_THE_ACCOUNT):
        _session(MatrixClient(), root, group=group)  # the next SDK does not start


def test_a_tripped_breaker_stops_only_that_sdk(tmp_path: Path) -> None:
    group = RunGroup(("javascript", "python"))
    root = tmp_path / "runs"
    first = _session(MatrixClient(), root, sdk_name="javascript", group=group,
                     factory=lambda: FakeSdk())  # fmt: skip
    first.guard.trip("nse", "3 unexpected rejections in a row")
    matrix_conftest.end_session(first, group)
    assert group.check_the_account is None
    second = _session(MatrixClient(), root, group=group)
    assert second.sdk.name == "python"
    gate.release_run_lock(second)


def test_check_the_account_at_the_end_of_one_sdk_stops_every_later_sdk(tmp_path: Path) -> None:
    group = RunGroup(("python", "javascript"))
    root = tmp_path / "runs"
    first = _session(MatrixClient(), root, group=group)
    first.ledger.mark_unknown("sdkuat-x-p000-1", ["1001"])
    matrix_conftest.end_session(first, group)
    assert group.check_the_account is not None
    assert CHECK_THE_ACCOUNT in group.check_the_account
    started: list[int] = []
    with pytest.raises(Refusal, match=CHECK_THE_ACCOUNT):
        _session(MatrixClient(), root, sdk_name="javascript", group=group,
                 factory=_counting(started))  # fmt: skip
    assert started == []


# -- the account match and the start-up order (carries 3 and 4) -----------------------------------


def test_every_sdk_must_hold_the_referee_account(tmp_path: Path) -> None:
    client = MatrixClient()
    other = FakeSdk(client_id="OTHER9")
    with pytest.raises(Refusal, match="account") as info:
        _session(client, tmp_path, sdk_name="javascript", factory=lambda: other)
    assert "OTHER9" not in info.value.reason
    assert client.count("place_order") == 0 and client.count("orders") == 0
    assert other.closed == 1  # a refused SDK is closed
    ok = _session(
        MatrixClient(), tmp_path / "b", sdk_name="javascript",
        factory=lambda: FakeSdk(client_id=ACCOUNT.lower()),
    )  # fmt: skip
    assert ok.sdk.client_id == ACCOUNT.lower()
    gate.release_run_lock(ok)


def test_the_sdk_starts_only_after_every_gate_passed(tmp_path: Path) -> None:
    client = MatrixClient()
    seen: list[list[str]] = []

    def factory() -> FakeSdk:
        seen.append([name for name, _ in client.calls])
        return FakeSdk()

    session = _session(client, tmp_path, sdk_name="javascript", factory=factory)
    assert len(seen) == 1
    assert "profile" in seen[0] and "margins" in seen[0]  # account match and limits first
    assert "orders" not in seen[0]
    gate.release_run_lock(session)


@pytest.mark.parametrize(
    "environ",
    [
        {**MATRIX_ENV, "TRADESMART_LIVE_ORDER_TESTS": "0"},
        {**MATRIX_ENV, "TRADESMART_LIVE_ORDER_ACCOUNT": "OTHER9"},
        {**MATRIX_ENV, "TRADESMART_LIVE_ORDER_FUNDED": "0"},
        {**MATRIX_ENV, "TRADESMART_ENV": "production"},
    ],
)
def test_a_failed_gate_never_starts_the_sdk(tmp_path: Path, environ: dict[str, str]) -> None:
    started: list[int] = []
    with pytest.raises(Refusal):
        _session(MatrixClient(), tmp_path, environ=environ, sdk_name="javascript",
                 factory=_counting(started))  # fmt: skip
    assert started == []


def test_an_sdk_with_the_wrong_code_is_refused(tmp_path: Path) -> None:
    wrong = FakeSdk()
    wrong.code = "p"
    with pytest.raises(Refusal, match="code"):
        _session(MatrixClient(), tmp_path, sdk_name="javascript", factory=lambda: wrong)


# -- one shared budget for the SDKs of a run (carry 5) --------------------------------------------


def test_the_sdks_of_one_run_share_pacing_positions_and_the_market_path(tmp_path: Path) -> None:
    group = RunGroup(("python", "javascript"))
    root = tmp_path / "runs"
    first = _session(MatrixClient(), root, group=group)
    first.ledger.note(batch.MARKET_CLAIM, case_id="shapes-nse-b-mis-mkt-day-buy", segment="nse")
    matrix_conftest.end_session(first, group)
    second = _session(MatrixClient(), root, sdk_name="javascript", group=group,
                      factory=lambda: FakeSdk())  # fmt: skip
    assert second.run_id == first.run_id + "j"  # each SDK its own run id and ledger
    assert second.ledger.path != first.ledger.path
    assert second.order_pacer is first.order_pacer
    assert second.quote_pacer is first.quote_pacer
    assert second.guard.stop_file == first.guard.stop_file
    assert second.positions_path == first.run_dir / POSITIONS_NAME
    run = batch.open_run(
        second, contracts=Contracts(day=NOW.date()), profile=Profile(None, None, None)
    )
    assert run.positions.path == first.run_dir / POSITIONS_NAME
    assert run.market_filled is not None  # the first SDK used the run's single market path
    assert "shapes-nse-b-mis-mkt-day-buy" in run.market_filled
    gate.release_run_lock(second)


# -- resume with several SDKs (carry 6) ----------------------------------------------------------


def test_a_resume_resumes_each_sdk_in_its_own_run(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    group = RunGroup(("python", "javascript"))
    py = _session(MatrixClient(), root, group=group)
    py.ledger.verdict("case-python-done", "pass")
    gate.release_run_lock(py)
    js = _session(MatrixClient(), root, sdk_name="javascript", group=group,
                  factory=lambda: FakeSdk())  # fmt: skip
    js.ledger.verdict("case-js-done", "pass")
    gate.release_run_lock(js)
    resume = {**MATRIX_ENV, gate.RUN_ID_ENV: py.run_id}
    again = RunGroup(("python", "javascript"))
    py2 = _session(MatrixClient(), root, environ=resume, group=again)
    assert py2.run_id == py.run_id and py2.resumed
    assert py2.case_done("case-python-done") is not None
    assert py2.case_done("case-js-done") is None
    gate.release_run_lock(py2)
    js2 = _session(MatrixClient(), root, environ=resume, sdk_name="javascript", group=again,
                   factory=lambda: FakeSdk())  # fmt: skip
    assert js2.run_id == js.run_id and js2.resumed
    assert js2.case_done("case-js-done") is not None
    assert js2.case_done("case-python-done") is None
    gate.release_run_lock(js2)


def test_a_resume_starts_an_sdk_the_earlier_process_never_reached(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    py = _session(MatrixClient(), root, group=RunGroup(("python", "javascript")))
    gate.release_run_lock(py)
    resume = {**MATRIX_ENV, gate.RUN_ID_ENV: py.run_id}
    js = _session(MatrixClient(), root, environ=resume, sdk_name="javascript",
                  group=RunGroup(("python", "javascript")), factory=lambda: FakeSdk())  # fmt: skip
    assert js.run_id == py.run_id + "j" and not js.resumed
    run = json.loads((js.run_dir / gate.RUN_FILE_NAME).read_text(encoding="utf-8"))
    assert run["sdk"] == "javascript" and run["group"] == py.run_id
    gate.release_run_lock(js)


def test_a_resume_refuses_a_run_of_another_sdk(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    py = _session(MatrixClient(), root)
    gate.release_run_lock(py)
    run_file = py.run_dir / gate.RUN_FILE_NAME
    data = json.loads(run_file.read_text(encoding="utf-8"))
    run_file.write_text(json.dumps({**data, "sdk": "javascript"}), encoding="utf-8")
    with pytest.raises(Refusal, match="javascript"):
        _session(MatrixClient(), root, environ={**MATRIX_ENV, gate.RUN_ID_ENV: py.run_id})


# -- cleanups (carry 9) and the raw-body record (carry 7) -----------------------------------------


@pytest.mark.parametrize("code", ["-", "pp", "P", "", "1"])
def test_the_ledger_takes_one_lower_case_letter_as_the_sdk_code(tmp_path: Path, code: str) -> None:
    with pytest.raises(ValueError, match="sdk_code"):
        Ledger(tmp_path / LEDGER_NAME, "1113abcd", sdk_code=code)


def test_the_session_has_one_source_of_the_sdk_code(tmp_path: Path) -> None:
    session = _session(MatrixClient(), tmp_path)
    assert not hasattr(session, "sdk_code")
    assert session.ledger.sdk_code == session.sdk.code == "p"
    gate.release_run_lock(session)
    with pytest.raises(ValueError, match="sdk"):
        gate.MatrixSession(
            client=MatrixClient(),  # type: ignore[arg-type]
            run_id="1113abcd",
            trading_day=NOW.date(),
            run_dir=tmp_path / "x",
            ledger=Ledger(tmp_path / "x" / LEDGER_NAME, "1113abcd", sdk_code="j"),
            guard=Guard(tmp_path / "STOP"),
        )


def test_the_tag_docstring_names_the_sdk_code() -> None:
    from tests.live_orders.uat_matrix import cases_tags

    assert "sdkuat-<run>-<sdk><key>-<n>" in (cases_tags.__doc__ or "")


def test_the_response_log_keeps_a_driver_body() -> None:
    log = ResponseLog()
    mark = log.mark()
    log.record("PlaceOrder", 200, {"stat": "Ok", "uid": ACCOUNT, "susertoken": "x"},
               request={"method": "place_order", "args": {"tag": "t"}})  # fmt: skip
    seen = log.last("PlaceOrder", since=mark)
    assert seen == {
        "http_status": 200,
        "body": {"stat": "Ok"},
        "request": {"method": "place_order", "args": {"tag": "t"}},
    }
    assert log.last("CancelOrder", since=mark) is None


def test_the_driver_process_module_never_runs_on_import() -> None:
    assert os.environ.get(DRIVER_RUN_ENV) is None


def _counting(started: list[int]) -> Any:
    """An SDK factory that counts its calls (it must never be called here)."""

    def factory() -> FakeSdk:
        started.append(1)
        return FakeSdk()

    return factory


def test_a_driver_command_that_cannot_start_is_refused(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script())
    with pytest.raises(Refusal, match="could not be started"):
        DriverSdk(
            [str(tmp_path / "no-such-driver")],
            nonce=NONCE,
            environ=fake.environ,
            reply_timeout=5.0,
        )


# -- fix round 1 --------------------------------------------------------------------------------
# I2: a resume with an SDK's own run id never starts a run in the wrong group.


def _js_run(root: Path) -> gate.MatrixSession:
    """A finished JavaScript run of a new group (run id ``<base>j``), its lock released."""
    session = _session(MatrixClient(), root, sdk_name="javascript", group=RunGroup(("javascript",)),
                       factory=lambda: FakeSdk())  # fmt: skip
    session.ledger.verdict("case-js-done", "pass")
    gate.release_run_lock(session)
    return session


@pytest.mark.parametrize("name", ["javascript", "python"])
def test_a_resume_with_an_sdk_run_id_is_refused(tmp_path: Path, name: str) -> None:
    root = tmp_path / "runs"
    js = _js_run(root)
    resume = {**MATRIX_ENV, gate.RUN_ID_ENV: js.run_id}  # 1113xxxxj, not the group's id
    factory = (lambda: FakeSdk()) if name != "python" else None
    with pytest.raises(Refusal, match="group|SDK"):
        _session(MatrixClient(), root, environ=resume, sdk_name=name,
                 group=RunGroup((name,)), factory=factory)  # fmt: skip
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == [js.run_id]  # nothing new


def test_a_sibling_run_of_another_sdk_is_refused_as_the_group(tmp_path: Path) -> None:
    """The sibling a fresh SDK run joins must be the SDK its id belongs to."""
    root = tmp_path / "runs"
    first = _session(MatrixClient(), root, group=RunGroup(("python",)))
    gate.release_run_lock(first)
    run_file = first.run_dir / gate.RUN_FILE_NAME
    data = json.loads(run_file.read_text(encoding="utf-8"))
    run_file.write_text(json.dumps({**data, "sdk": "javascript"}), encoding="utf-8")
    resume = {**MATRIX_ENV, gate.RUN_ID_ENV: first.run_id}
    with pytest.raises(Refusal, match="javascript"):
        _session(MatrixClient(), root, environ=resume, sdk_name="javascript",
                 group=RunGroup(("javascript",)), factory=lambda: FakeSdk())  # fmt: skip


def test_a_resume_of_the_group_id_still_works_for_a_javascript_only_group(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    js = _js_run(root)
    resume = {**MATRIX_ENV, gate.RUN_ID_ENV: js.group_id}
    again = _session(MatrixClient(), root, environ=resume, sdk_name="javascript",
                     group=RunGroup(("javascript",)), factory=lambda: FakeSdk())  # fmt: skip
    assert again.run_id == js.run_id and again.resumed
    assert again.case_done("case-js-done") is not None
    gate.release_run_lock(again)


# I3: the positions a non-Python run left are listed at its end.


def test_the_end_of_session_notice_lists_the_group_positions(tmp_path: Path) -> None:
    from tests.live_orders.uat_matrix import notice
    from tests.live_orders.uat_matrix.positions import PositionLedger

    group = RunGroup(("javascript",))
    session = _session(MatrixClient(), tmp_path / "runs", sdk_name="javascript", group=group,
                       factory=lambda: FakeSdk())  # fmt: skip
    assert session.positions_path.parent.name == session.group_id != session.run_id
    PositionLedger(session.positions_path).add_fill(
        "NIFTYFUT", 65, lot_size=65, case_id="fills-x", order_id="1001"
    )
    shown = notice.positions_notice(session)
    assert shown is not None and "NIFTYFUT" in shown and "+65" in shown
    assert CHECK_THE_ACCOUNT not in shown  # leftover positions are listed, not unknown
    before = len(matrix_conftest.NOTICES)
    matrix_conftest.end_session(session, group)
    assert any("NIFTYFUT" in n for n in matrix_conftest.NOTICES[before:])
    assert group.check_the_account is None  # positions alone never stop the next SDK


def test_no_positions_no_positions_notice(tmp_path: Path) -> None:
    from tests.live_orders.uat_matrix import notice

    session = _session(MatrixClient(), tmp_path)
    assert notice.positions_notice(session) is None
    gate.release_run_lock(session)


# I4: secrets that are not in the environment, in Node-inspect form, error texts.


def test_a_saved_login_token_in_node_inspect_form_is_never_recorded(tmp_path: Path) -> None:
    unpassed = "synthetic-unpassed-token-not-real-0003"
    lines = [
        f"client {{ accessToken: '{LOGIN_TOKEN}', appKey: 'synthetic-app-key-77' }}",
        f"{{ accessToken: '{unpassed}' }}",
        'login { secretKey: "synthetic-secret-88", refresh_token: synthetic-refresh-99 }',
        f"raw token {LOGIN_TOKEN}",
        "debug: ordinary line",
    ]
    fake = Fake(tmp_path, _script({"result": "1001", "raw": PLACE_RAW}, stderr=lines))
    sdk = fake.start(secrets=[LOGIN_TOKEN])
    hooks = Hooks()
    hooks.bind(sdk)
    sdk.place_order(**PLACE_KWARGS)
    sdk.close()
    recorded = json.dumps(hooks.notes) + "\n".join(sdk.stderr_lines)
    for value in (LOGIN_TOKEN, unpassed, "synthetic-app-key-77", "synthetic-secret-88",
                  "synthetic-refresh-99"):  # fmt: skip
        assert value not in recorded, value
    assert "ordinary line" in recorded and "accessToken" in recorded  # keys kept, values masked


def test_the_error_message_and_emsg_are_redacted(tmp_path: Path) -> None:
    error = {
        "type": "OrderRejectedError",
        "message": (
            f"failed: Authorization: Bearer {LOGIN_TOKEN} {{ accessToken: '{LOGIN_TOKEN}' }}"
        ),
        "stat": "Not_Ok",
        "emsg": f"session {LOGIN_TOKEN} rejected",
        "http_status": 200,
        "raw": {"stat": "Not_Ok", "emsg": "rejected"},
        "sliced_result": None,
        "fields": [],
    }
    fake = Fake(tmp_path, _script({"error": error}))
    sdk = fake.start(secrets=[LOGIN_TOKEN])
    try:
        with pytest.raises(OrderRejectedError) as info:
            sdk.place_order(**PLACE_KWARGS)
    finally:
        sdk.close()
    assert LOGIN_TOKEN not in str(info.value) and LOGIN_TOKEN not in (info.value.emsg or "")
    assert "rejected" in (info.value.emsg or "")


def test_the_referee_clients_secrets_are_collected() -> None:
    from types import SimpleNamespace

    from pydantic import SecretStr

    from tests.live_orders.uat_matrix.sdk_under_test import referee_secrets

    token = SimpleNamespace(access_token=SecretStr("tok-from-file-123456"))
    client = SimpleNamespace(
        _creds=SimpleNamespace(_token=token, _app_key="app-123456", _secret_key="sec-123456")
    )
    assert set(referee_secrets(client)) == {"tok-from-file-123456", "app-123456", "sec-123456"}
    plain = SimpleNamespace(_creds=SimpleNamespace(_token="tok-plain-123456", _app_key=None,
                                                   _secret_key=None))  # fmt: skip
    assert referee_secrets(plain) == ["tok-plain-123456"]
    assert referee_secrets(object()) == []


# -- fix round 2 --------------------------------------------------------------------------------
# A4: with another SDK, teardown cancels a book row only when its tag is the ledger's.


def _placed_through(session: gate.MatrixSession) -> str:
    """One place of ours through the session's SDK (``FakeSdk`` answers ``1001``); its tag."""
    session.guard.reserve(NSE.case_id, 1)
    outcome = calls.place(session, PLAN, NSE)
    assert outcome.order_id == "1001"
    return outcome.tag


def _book(*rows: Any) -> list[Any]:
    from tests.unit.test_uat_matrix_safety import _order

    return [_order(order_id, status, tag) for order_id, status, tag in rows]


def test_teardown_for_another_sdk_needs_the_ledgers_tag_on_the_row(tmp_path: Path) -> None:
    sdk = FakeSdk()
    client = MatrixClient()
    session = _session(client, tmp_path, sdk_name="javascript", factory=lambda: sdk)
    _placed_through(session)
    client.books = [_book(("1001", "OPEN", "sdkuat-someone-else-1"))]
    report = calls.cancel_ours(session)
    assert report.tag_mismatch == ["1001"] and report.cancelled == []
    assert ("cancel_order", {"order_id": "1001"}) not in sdk.seen  # never cancelled by id alone
    from tests.live_orders.uat_matrix import notice

    shown = notice.leftover_notice(session, report)
    assert shown is not None and CHECK_THE_ACCOUNT in shown and "1001" in shown
    gate.release_run_lock(session)


def test_teardown_for_another_sdk_cancels_a_row_with_the_ledgers_tag(tmp_path: Path) -> None:
    sdk = FakeSdk()
    client = MatrixClient()
    session = _session(client, tmp_path, sdk_name="javascript", factory=lambda: sdk)
    tag = _placed_through(session)
    client.books = [_book(("1001", "OPEN", tag)), _book(("1001", "CANCELED", tag))]
    report = calls.cancel_ours(session)
    assert report.cancelled == ["1001"] and report.tag_mismatch == []
    assert ("cancel_order", {"order_id": "1001"}) in sdk.seen
    gate.release_run_lock(session)


def test_teardown_for_python_still_cancels_by_id(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    calls.place(session, PLAN, NSE)  # MatrixClient answers 1001
    client.books = [_book(("1001", "OPEN", "")), _book(("1001", "CANCELED", ""))]
    report = calls.cancel_ours(session)
    assert report.cancelled == ["1001"] and report.tag_mismatch == []
    gate.release_run_lock(session)


# B1: the end of a session never fails open.


def test_end_session_propagates_check_the_account_when_a_notice_part_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.live_orders.uat_matrix import notice

    def boom(session: Any) -> str:
        raise RuntimeError("positions file unreadable")

    monkeypatch.setattr(notice, "positions_notice", boom)
    group = RunGroup(("python", "javascript"))
    session = _session(MatrixClient(), tmp_path, group=group)
    session.ledger.mark_unknown("sdkuat-x-p000-1", ["1001"])
    matrix_conftest.end_session(session, group)  # never raises
    assert group.check_the_account is not None
    assert session.lock is None  # the run lock is released


# B2: a reply nested too deeply is malformed, never an unhandled RecursionError.


def test_a_reply_nested_too_deeply_is_unknown(tmp_path: Path) -> None:
    deep = "[" * 200_000 + "]" * 200_000
    fake = Fake(tmp_path, _script({"line": '{"id": 1, "result": ' + deep + "}"}))
    sdk = fake.start()
    try:
        with pytest.raises(OrderStatusUnknownError):
            sdk.place_order(**PLACE_KWARGS)
        assert sdk.process.poll() is not None
    finally:
        sdk.close()


@pytest.mark.parametrize("depth", [1200, 5000])
def test_a_deep_error_raw_is_an_unknown_outcome_on_every_python(tmp_path: Path, depth: int) -> None:
    """Whether ``json.loads`` or the redaction overflows first depends on the Python version;
    the outcome never does: an unknown outcome, the driver killed and stopped once."""
    # Written as text: the test's own json.dumps of so deep a value fails on Python 3.11.
    raw = '{"a": ' * depth + '"x"' + "}" * depth
    line = (
        '{"id": 1, "error": {"type": "OrderRejectedError", "message": "m", "stat": "Not_Ok", '
        f'"emsg": "e", "http_status": 200, "raw": {raw}, "sliced_result": null, "fields": []}}}}'
    )
    fake = Fake(tmp_path, _script({"line": line}))
    sdk = fake.start()
    hooks = Hooks()
    hooks.bind(sdk)
    try:
        with pytest.raises(OrderStatusUnknownError):
            sdk.place_order(**PLACE_KWARGS)
        assert sdk.process.poll() is not None
        ((_, check),) = hooks.stops
        assert check is True
        with pytest.raises(RequestValidationError):
            sdk.cancel_order("1001")
    finally:
        sdk.close()
    assert len(fake.request_lines()) == 1


# B3: driver-controlled texts in stop and refusal reasons are redacted.


@pytest.mark.parametrize("field", ["sdk", "environment", "protocol"])
def test_handshake_refusals_never_echo_a_secret(tmp_path: Path, field: str) -> None:
    fake = Fake(tmp_path, {"hello": {**HELLO, field: f"{{ accessToken: '{LOGIN_TOKEN}' }}"}})
    with pytest.raises(Refusal) as info:
        fake.start(secrets=[LOGIN_TOKEN])
    assert LOGIN_TOKEN not in info.value.reason


def test_an_id_mismatch_never_echoes_a_secret(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script({"result": LOGIN_TOKEN, "raw": PLACE_RAW, "http_status": 200}))
    sdk = fake.start(secrets=[LOGIN_TOKEN])
    hooks = Hooks()
    hooks.bind(sdk)
    try:
        with pytest.raises(OrderStatusUnknownError) as info:
            sdk.place_order(**PLACE_KWARGS)
    finally:
        sdk.close()
    assert LOGIN_TOKEN not in str(info.value)
    assert LOGIN_TOKEN not in json.dumps(hooks.stops) + json.dumps(hooks.notes)


# B4 / B5: more secrets and secret-named keys by suffix.


def test_referee_secrets_include_the_session_and_refresh_tokens() -> None:
    from types import SimpleNamespace

    from pydantic import SecretStr

    from tests.live_orders.uat_matrix.sdk_under_test import referee_secrets

    token = SimpleNamespace(
        access_token=SecretStr("tok-access-123456"),
        susertoken=SecretStr("tok-suser-123456"),
        refresh_token=SecretStr("tok-refresh-123456"),
    )
    client = SimpleNamespace(_creds=SimpleNamespace(_token=token, _app_key=None, _secret_key=None))
    assert set(referee_secrets(client)) == {
        "tok-access-123456",
        "tok-suser-123456",
        "tok-refresh-123456",
    }


def test_secret_keys_match_by_suffix_and_common_keys_stay(tmp_path: Path) -> None:
    line = (
        "{ csrfToken: 'hidden-a-01', myApiSecret: 'hidden-b-02', privateKey: 'hidden-c-03', "
        "userPassword: 'hidden-d-04', jwt_secret_value: 'hidden-e-05', ssoPasswd: hidden-f-06, "
        "token: '2885', key: 'case-key-007', tokens: [ '26000' ], keyword: 'kept-word', "
        "exchange: 'NSE', tradingsymbol: 'ITC-EQ', norenordno: '1001', al_id: 'A1', "
        "remarks: 'sdkuat-1113abcd-j000-1', request_time: '10:00:00', stat: 'Ok' }"
    )
    fake = Fake(tmp_path, _script(stderr=[line]))
    sdk = fake.start()
    sdk.close()
    (shown,) = [x for x in sdk.stderr_lines if "exchange" in x]
    for hidden in ("hidden-a-01", "hidden-b-02", "hidden-c-03", "hidden-d-04", "hidden-e-05",
                   "hidden-f-06"):  # fmt: skip
        assert hidden not in shown, hidden
    for kept in ("'2885'", "case-key-007", "'26000'", "kept-word", "'NSE'", "'ITC-EQ'", "'1001'",
                 "'A1'", "sdkuat-1113abcd-j000-1", "10:00:00", "'Ok'"):  # fmt: skip
        assert kept in shown, kept


# C1: a client-side rate limit is "sent": false, never read from the message.


def _rate_limited(**fields: Any) -> dict[str, Any]:
    error: dict[str, Any] = {
        "type": "RateLimitError",
        "message": "PlaceOrder: rate limit",
        "stat": None,
        "emsg": None,
        "http_status": None,
        "raw": None,
        "sliced_result": None,
        "fields": [],
    }
    error.update(fields)
    return {"error": error}


@pytest.mark.parametrize(
    ("fields", "nothing_sent"),
    [
        ({"sent": False}, True),
        ({"sent": True, "message": "client-side rate limit"}, False),
        ({"message": "client-side rate limit"}, False),  # no field: sent, whatever the text
    ],
)
def test_a_drivers_rate_limit_is_classified_by_its_sent_field(
    tmp_path: Path, fields: dict[str, Any], nothing_sent: bool
) -> None:
    from tests.live_orders.uat_matrix.ledger import REJECTED
    from tests.live_orders.uat_matrix.ledger import nothing_sent as ledger_nothing_sent
    from tradesmart import RateLimitError

    fake = Fake(tmp_path, _script(_rate_limited(**fields)))
    sdk = fake.start()
    try:
        with pytest.raises(RateLimitError) as info:
            sdk.place_order(**PLACE_KWARGS)
    finally:
        sdk.close()
    record = calls._error(info.value)
    assert ledger_nothing_sent(REJECTED, record, kind="place") is nothing_sent
    assert calls._refused_before_sending(info.value) is nothing_sent


def test_pythons_own_rate_limit_is_unchanged() -> None:
    from tests.live_orders.uat_matrix.ledger import REJECTED
    from tests.live_orders.uat_matrix.ledger import nothing_sent as ledger_nothing_sent
    from tradesmart import RateLimitError

    exc = RateLimitError("PlaceOrder: client-side rate limit", endpoint="PlaceOrder",
                         http_status=None)  # fmt: skip
    assert "sent" not in calls._error(exc)
    assert ledger_nothing_sent(REJECTED, calls._error(exc), kind="place") is True


@pytest.mark.parametrize("status", ["200", 200.0, True])
def test_an_http_status_that_is_not_an_integer_is_malformed(tmp_path: Path, status: Any) -> None:
    fake = Fake(tmp_path, _script({"result": "1001", "raw": PLACE_RAW, "http_status": status}))
    sdk = fake.start()
    try:
        with pytest.raises(OrderStatusUnknownError):
            sdk.place_order(**PLACE_KWARGS)
        assert sdk.process.poll() is not None
    finally:
        sdk.close()


# C2: call() is for reads only.


@pytest.mark.parametrize("method", ["place_order", "cancel_order", "place_sliced_order"])
def test_call_refuses_every_write(tmp_path: Path, method: str) -> None:
    fake = Fake(tmp_path, _script())
    sdk = fake.start()
    try:
        with pytest.raises(ValueError, match=method):
            sdk.call(method, order_id="1001")
    finally:
        sdk.close()
    assert fake.request_lines() == []


# C3: an interruption after the request line stops the driver for good.


def test_an_interruption_after_the_request_stops_and_kills_the_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Fake(tmp_path, _script({"result": "1001", "raw": PLACE_RAW}, {"result": "1001"}))
    sdk = fake.start()
    hooks = Hooks()
    hooks.bind(sdk)

    def interrupted(timeout: float) -> object:
        # Interrupted only once the driver has the request (bounded wait): no timing guess.
        deadline = time.monotonic() + 5.0
        while not fake.request_lines() and time.monotonic() < deadline:
            time.sleep(0.02)
        raise KeyboardInterrupt

    monkeypatch.setattr(sdk, "_await", interrupted)
    try:
        with pytest.raises(KeyboardInterrupt):
            sdk.place_order(**PLACE_KWARGS)
        assert sdk.stopped is not None and sdk.process.poll() is not None
        ((_, check),) = hooks.stops
        assert check is True
        with pytest.raises(RequestValidationError):  # a stale reply can never answer this
            sdk.cancel_order("1001")
    finally:
        sdk.close()
    assert len(fake.request_lines()) == 1  # exactly one request line: never resent


# C4: a refused open whose stop leaves CHECK THE ACCOUNT stops the later SDKs.


def test_a_refused_open_with_check_the_account_stops_every_later_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.live_orders.uat_matrix.guard import StopRun

    def stop_run(session: gate.MatrixSession, stop: StopRun) -> calls.CancelReport:
        session.guard.note_check_the_account("nse", "order 1001: cancel outcome unknown")
        return calls.CancelReport()

    monkeypatch.setattr(calls, "stop_run", stop_run)
    group = RunGroup(("python", "javascript"))
    client = MatrixClient(books=[_book(("9999", "OPEN", "not-ours"))])
    with pytest.raises(StopRun):
        _session(client, tmp_path, group=group)
    assert group.check_the_account is not None
    with pytest.raises(Refusal, match=CHECK_THE_ACCOUNT):
        _session(MatrixClient(), tmp_path, sdk_name="javascript", group=group,
                 factory=lambda: FakeSdk())  # fmt: skip


# D: the protocol document states the round's rules.


def test_the_protocol_document_states_slices_sent_and_fail_closed_teardown() -> None:
    text = (REPO / "specifications" / "live-driver.md").read_text(encoding="utf-8")
    assert '"slices"' in text and '"sent": false' in text
    assert "fail-closed" in text.lower()
    readme = (REPO / "sdk-python" / "tests" / "live_orders" / "README.md").read_text(
        encoding="utf-8"
    )
    assert "fail-closed" in readme.lower() and "POSITIONS LEFT" in readme


# -- fix round 3 --------------------------------------------------------------------------------


def test_a_returned_incomplete_sliced_result_with_its_error_is_accepted(tmp_path: Path) -> None:
    rejected = {"type": "OrderRejectedError", "message": "slice 3 rejected", "stat": "Not_Ok",
                "emsg": "RMS", "http_status": 200, "raw": {"stat": "Not_Ok", "emsg": "RMS"},
                "sliced_result": None, "fields": []}  # fmt: skip
    result = _sliced_result(slice_quantities=[10, 10, 10], placed_quantity=20, unsent_quantity=10,
                            complete=False, error=rejected)  # fmt: skip
    fake = Fake(tmp_path, _script({"result": result}))
    sdk = fake.start()
    try:
        out = sdk.place_sliced_order(**SLICED_KWARGS, max_slices=3, max_slice_quantity=None)
    finally:
        sdk.close()
    assert out.order_ids == ("1001", "1002") and not out.complete
    assert isinstance(out.error, OrderRejectedError)


def test_recorded_success_bodies_are_redacted_like_error_bodies(tmp_path: Path) -> None:
    raw = {**PLACE_RAW, "susertoken": "synthetic-suser-token-0004"}
    fake = Fake(tmp_path, _script({"result": "1001", "raw": raw, "http_status": 200},
                                  {"result": _sliced_result()}))  # fmt: skip
    sdk = fake.start()
    hooks = Hooks()
    hooks.bind(sdk)
    try:
        assert sdk.place_order(**PLACE_KWARGS) == "1001"
        sdk.place_sliced_order(**SLICED_KWARGS, max_slices=2, max_slice_quantity=None)
    finally:
        sdk.close()
    kept = [entry for _, entry in hooks.responses._responses if isinstance(entry, dict)]
    assert len(kept) == 3
    assert kept[0]["body"]["susertoken"] == "***"  # the SDK's own redaction, before any scrub


def test_a_sliced_result_on_another_method_is_malformed(tmp_path: Path) -> None:
    error: dict[str, Any] = {
        "type": "OrderStatusUnknownError",
        "message": "m",
        "stat": None,
        "emsg": None,
        "http_status": None,
        "raw": None,
        "sliced_result": _sliced_result(),
        "fields": [],
    }
    fake = Fake(tmp_path, _script({"error": error}))
    sdk = fake.start()
    hooks = Hooks()
    hooks.bind(sdk)
    try:
        with pytest.raises(OrderStatusUnknownError):
            sdk.place_order(**PLACE_KWARGS)
        assert sdk.process.poll() is not None  # the driver broke the protocol: stopped
    finally:
        sdk.close()
    ((_, check),) = hooks.stops
    assert check is True


def test_a_rate_limit_not_sent_with_an_http_status_is_malformed(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script(_rate_limited(sent=False, http_status=429)))
    sdk = fake.start()
    try:
        with pytest.raises(OrderStatusUnknownError):
            sdk.place_order(**PLACE_KWARGS)
        assert sdk.process.poll() is not None
    finally:
        sdk.close()


def test_the_python_value_of_a_finding_and_the_hello_version_are_redacted(tmp_path: Path) -> None:
    raw = {**PLACE_RAW, "norenordno": LOGIN_TOKEN}
    fake = Fake(tmp_path, {"hello": {**HELLO, "version": f"2.0.0 {LOGIN_TOKEN}"},
                           "replies": [{"result": "1001", "raw": raw}]})  # fmt: skip
    sdk = fake.start(secrets=[LOGIN_TOKEN])
    hooks = Hooks()
    hooks.bind(sdk)
    try:
        with pytest.raises(OrderStatusUnknownError):
            sdk.place_order(**PLACE_KWARGS)
    finally:
        sdk.close()
    recorded = json.dumps(hooks.notes) + json.dumps(sdk.findings) + json.dumps(hooks.stops)
    assert "driver_hello" in recorded and "driver_finding" in recorded
    assert LOGIN_TOKEN not in recorded


# -- fix round 4 --------------------------------------------------------------------------------


def test_the_protocol_document_states_the_sliced_plan_rules() -> None:
    text = " ".join(
        (REPO / "specifications" / "live-driver.md").read_text(encoding="utf-8").split()
    )
    assert "positive integers that add up to `requested_quantity`" in text
    assert "the `quantity` the harness sent" in text
    assert "a complete result names no `error`" in text
    assert "never an `OrderStatusUnknownError`" in text
    assert "is never complete and carries no `error` of its own" in text


# -- Task 13: the order budget of a sliced result (the harness budgeted ``max_slices`` orders) ----


def _placed(*quantities: int, **changes: Any) -> dict[str, Any]:
    """A complete sliced result for ``quantities`` (one placed slice per quantity)."""
    ids = [str(1001 + i) for i in range(len(quantities))]
    total = sum(quantities)
    return _sliced_result(
        requested_quantity=total,
        slice_quantities=list(quantities),
        order_ids=ids,
        placed_quantity=total,
        unsent_quantity=0,
        complete=True,
        slices=[_slice(i) for i in ids],
        **changes,
    )


# Each case has exactly one defect (the result is otherwise the plan Python would build) and is
# sent with the stated limits: a driver must never place more orders, or a larger one, than the
# budget the harness reserved with ``guard.spend(case, max_slices)``.
BUDGET_FAILURES = [
    pytest.param(
        _placed(10, 10, 10), 2, None, "more slices than max_slices", id="more-slices-than-sent"
    ),
    pytest.param(
        _placed(20, 10), 2, 15, "slice larger than max_slice_quantity", id="slice-over-the-cap"
    ),
    pytest.param(
        _placed(10, 20), 2, None, "slices are not the plan's shape", id="smaller-slice-first"
    ),
    pytest.param(
        _placed(20, 5, 5), 3, None, "slices are not the plan's shape", id="uneven-middle-slice"
    ),
    pytest.param(
        _placed(5, 5, 20), 3, None, "slices are not the plan's shape", id="larger-last-slice"
    ),
]


@pytest.mark.parametrize(("result", "max_slices", "cap", "why"), BUDGET_FAILURES)
def test_a_sliced_result_beyond_the_order_budget_is_unknown(
    tmp_path: Path, result: dict[str, Any], max_slices: int, cap: int | None, why: str
) -> None:
    fake = Fake(tmp_path, _script({"result": result}))
    sdk = fake.start()
    hooks = Hooks()
    hooks.bind(sdk)
    try:
        with pytest.raises(OrderStatusUnknownError):
            sdk.place_sliced_order(**SLICED_KWARGS, max_slices=max_slices, max_slice_quantity=cap)
        assert sdk.process.poll() is not None
    finally:
        sdk.close()
    ((reason, check),) = hooks.stops
    assert check is True and why in reason


@pytest.mark.parametrize(
    ("result", "max_slices", "cap"),
    [
        pytest.param(_placed(10, 10, 10), 3, 10, id="equal-slices-at-the-cap"),
        pytest.param(_placed(20, 10), 25, None, id="a-smaller-last-slice"),
        pytest.param(_placed(30), 1, 30, id="one-slice"),
    ],
)
def test_a_sliced_result_inside_the_order_budget_is_accepted(
    tmp_path: Path, result: dict[str, Any], max_slices: int, cap: int | None
) -> None:
    fake = Fake(tmp_path, _script({"result": result}))
    sdk = fake.start()
    try:
        out = sdk.place_sliced_order(**SLICED_KWARGS, max_slices=max_slices, max_slice_quantity=cap)
    finally:
        sdk.close()
    assert out.complete and out.slice_quantities == tuple(result["slice_quantities"])


def test_the_protocol_document_states_the_order_budget_rules() -> None:
    text = " ".join(
        (REPO / "specifications" / "live-driver.md").read_text(encoding="utf-8").split()
    )
    assert "no more slices than the `max_slices` sent" in text
    assert "none above the `max_slice_quantity` sent" in text
    assert "all equal except a smaller last one" in text


# -- final review M-a: a driver is paced where it sends ---------------------------------------


class TimedFakeSdk(FakeSdk):
    """A driver stand-in: one request at a time (``_lock``, as ``DriverSdk._call``), its own
    send slot (as ``DriverSdk.send_slot``), a slow first reply, and the time each place request
    is sent (real time: several threads share it)."""

    def __init__(self, slow_first: float = 0.3) -> None:
        super().__init__()
        self.instants: list[float] = []
        self.slow_first = slow_first
        self._lock = threading.Lock()
        self._slot = threading.Lock()

    def send_slot(self) -> Any:
        return self._slot

    def place_order(self, **kwargs: Any) -> str:
        with self._lock:
            self.instants.append(time.monotonic())
            if len(self.instants) == 1:
                time.sleep(self.slow_first)  # a slow reply: the calls behind it queue
            return super().place_order(**kwargs)


def test_driver_sessions_use_the_normal_worker_count(tmp_path: Path) -> None:
    sdk = TimedFakeSdk()
    session = _session(MatrixClient(), tmp_path / "runs", sdk_name="javascript",
                       factory=lambda: sdk)  # fmt: skip
    run = batch.open_run(
        session, contracts=Contracts(day=NOW.date()), profile=Profile(None, None, None)
    )
    assert run.workers == batch.WORKERS > 1
    assert not hasattr(sdk, "serial_sends") and not hasattr(DriverSdk, "serial_sends")
    gate.release_run_lock(session)


def test_driver_calls_queued_behind_a_slow_reply_still_respect_the_pacer(tmp_path: Path) -> None:
    sdk = TimedFakeSdk()
    session = gate.matrix_session(
        MATRIX_ENV,
        MatrixClient(),  # type: ignore[arg-type]
        root=tmp_path / "runs",
        now=NOW,
        sleep=time.sleep,
        clock=time.monotonic,
        sdk_name="javascript",
        sdk_factory=lambda: sdk,
    )
    run = batch.open_run(
        session, contracts=Contracts(day=NOW.date()), profile=Profile(None, None, None)
    )
    assert run.workers == batch.WORKERS  # parallel workers, one send slot
    case = CaseRef("pacing-nse-itc-cnc-lmt-day-buy", "nse")
    session.guard.reserve(case.case_id, 14)
    items = list(range(14))
    batch._in_pool(run, lambda _i: calls.place(session, PLAN, case), items)
    assert len(sdk.instants) == 14
    limit = session.order_pacer.per_second
    window = session.order_pacer.window
    slack = 0.1  # the intent line is written between the pacer's count and the send
    for start, instant in enumerate(sdk.instants[:-limit]):
        assert sdk.instants[start + limit] - instant >= window - slack, start
    gate.release_run_lock(session)


def test_a_driver_send_slot_is_its_own_lock_apart_from_the_request_lock(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script({"result": "1001", "raw": PLACE_RAW, "http_status": 200}))
    sdk = fake.start()
    try:
        assert sdk.send_slot() is sdk.send_slot()
        assert sdk.send_slot() is not sdk._lock
        with sdk.send_slot():  # holding the slot never blocks the request itself
            assert sdk.place_order(**PLACE_KWARGS) == "1001"
        with sdk.send_slot():  # given back after the with block
            pass
    finally:
        sdk.close()


def test_a_second_write_waits_while_the_driver_send_slot_is_held(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script())
    sdk = fake.start()
    taken = threading.Event()
    try:
        with sdk.send_slot():

            def other() -> None:
                with sdk.send_slot():
                    taken.set()

            worker = threading.Thread(target=other, daemon=True)
            worker.start()
            assert not taken.wait(0.2)  # held: the other call waits
        assert taken.wait(5.0)  # released: it goes on
        worker.join(5.0)
    finally:
        sdk.close()


# -- final review M-b: a driver's error reply whose body is a success is not a rejection -----------


def test_an_error_reply_whose_raw_body_is_a_success_is_an_unknown_outcome(tmp_path: Path) -> None:
    error = {
        "type": "OrderRejectedError",
        "message": "PlaceOrder failed",
        "stat": "Not_Ok",
        "emsg": "RMS: margin exceeds",
        "http_status": 200,
        "raw": PLACE_RAW,  # Python's parse of this body is a success: the order may exist
        "sliced_result": None,
        "fields": [],
    }
    fake = Fake(tmp_path, _script({"error": error}))
    sdk = fake.start()
    hooks = Hooks()
    hooks.bind(sdk)
    try:
        with pytest.raises(OrderStatusUnknownError):
            sdk.place_order(**PLACE_KWARGS)
        assert sdk.process.poll() is not None  # a broken reply: the driver is stopped
        ((_, check),) = hooks.stops
        assert check is True
    finally:
        sdk.close()
    assert len(fake.request_lines()) == 1


# -- final review M-c: call() refuses a state-changing request, whatever its endpoint --------------


@pytest.mark.parametrize(
    "args",
    [
        {"endpoint": "OrderBook", "payload": {}, "state_changing": True},
        {"endpoint": "PlaceOrder", "payload": {}},
        {"endpoint": "cancelorder", "payload": {}},  # endpoint names are case-insensitive
        {"endpoint": "PlaceMultiOrder", "payload": {}},
        {"endpoint": "GenAcsTok", "payload": {}},
    ],
)
def test_call_refuses_a_request_that_can_change_state(tmp_path: Path, args: dict[str, Any]) -> None:
    fake = Fake(tmp_path, _script())
    sdk = fake.start()
    try:
        with pytest.raises(ValueError, match="request"):
            sdk.call("request", **args)
    finally:
        sdk.close()
    assert fake.request_lines() == []


def test_call_still_sends_a_read_only_request(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script({"result": {"stat": "Ok"}}))
    sdk = fake.start()
    try:
        assert sdk.call("request", endpoint="OrderBook", payload={}) == {"stat": "Ok"}
    finally:
        sdk.close()
    assert len(fake.request_lines()) == 1


# -- final review M-d: the notice says which cancels were never sent ---------------------------


class StoppedDriverSdk(FakeSdk):
    """A driver stopped for good: every cancel is refused before anything is written."""

    def cancel_order(self, order_id: str) -> str:
        raise RequestValidationError(
            [{"type": "driver_stopped", "loc": (), "msg": "the driver is stopped"}]
        )


def test_a_cancel_never_sent_is_listed_as_not_sent_and_the_notice_names_the_cleanup(
    tmp_path: Path,
) -> None:
    from tests.live_orders.uat_matrix import notice

    sdk = StoppedDriverSdk()
    client = MatrixClient()
    session = _session(client, tmp_path, sdk_name="javascript", factory=lambda: sdk)
    tag = _placed_through(session)
    client.books = [_book(("1001", "OPEN", tag))]
    report = calls.cancel_ours(session)
    assert report.not_sent == ["1001"]
    assert report.not_final == [] and report.cancelled == []  # never sent: never "not final"
    assert not session.ledger.stuck("1001")  # and not noted stuck
    shown = notice.leftover_notice(session, report)
    assert shown is not None and CHECK_THE_ACCOUNT in shown and "1001" in shown
    assert "not final after their cancel" not in shown
    assert "cancel was not sent" in shown and "uat_cleanup" in shown
    gate.release_run_lock(session)


# -- protocol 2: the OCO calls --------------------------------------------------------------------

OCO_LEG_SL = {"trigger_value": "95", "price": "94.5", "order_type": "LMT", "trigger_price": "95"}
OCO_LEG_TG = {"trigger_value": "105", "price": "105.5", "order_type": "LMT", "trigger_price": "105"}
OCO_KWARGS: dict[str, Any] = {
    "exchange": "NSE",
    "tradingsymbol": "ITC-EQ",
    "transaction_type": "B",
    "quantity": 1,
    "product": "C",
    "stop_loss": OCO_LEG_SL,
    "target": OCO_LEG_TG,
    "tag": "sdkuat-1113abcd-j000-1",
    "validity": "DAY",
    "alert_validity": "GTT",
}


def test_driver_protocol_is_2() -> None:
    from tests.live_orders.uat_matrix import driver_process

    assert driver_process.PROTOCOL == 2
    assert driver_process.ENDPOINTS["place_oco"] == "PlaceOCOOrder"
    assert driver_process.ENDPOINTS["modify_oco"] == "ModifyOCOOrder"
    assert driver_process.ENDPOINTS["cancel_oco"] == "CancelOCOOrder"
    assert {"place_oco", "modify_oco", "cancel_oco"} <= driver_process.WRITES


def test_v1_driver_refused_at_start(tmp_path: Path) -> None:
    fake = Fake(tmp_path, {"hello": {**HELLO, "protocol": 1}, "child": True})
    with pytest.raises(Refusal, match="protocol 1") as info:
        fake.start()
    assert "protocol 2" in info.value.reason  # the message names what is required
    assert fake.requests() == []  # refused before any order


def test_driver_oco_calls_round_trip(tmp_path: Path) -> None:
    place = {"result": "A9", "raw": {"stat": "OI created", "al_id": "A9"}, "http_status": 200}
    modify = {"result": "A9", "raw": {"stat": "OI replaced", "al_id": "A9"}, "http_status": 200}
    cancel = {"result": "A9", "raw": {"stat": "OI deleted", "al_id": "A9"}, "http_status": 200}
    fake = Fake(tmp_path, _script(place, modify, cancel))
    sdk = fake.start()
    try:
        assert sdk.place_oco(**OCO_KWARGS) == "A9"
        sdk.modify_oco("A9", **{k: v for k, v in OCO_KWARGS.items() if k != "tag"})
        assert sdk.cancel_oco("A9") == "A9"
    finally:
        sdk.close()
    sent = fake.requests()
    assert [r["method"] for r in sent] == ["place_oco", "modify_oco", "cancel_oco"]  # one each
    assert sent[0]["args"]["stop_loss"] == OCO_LEG_SL and sent[0]["args"]["target"] == OCO_LEG_TG
    assert sent[1]["args"]["alert_id"] == "A9" and sent[2]["args"] == {"alert_id": "A9"}


@pytest.mark.parametrize(
    "raw",
    [
        {"stat": "Not_Ok", "al_id": "A9"},
        {"stat": "OI deleted"},  # no alert id
        {"stat": "Oi delete success", "al_id": "A9"},  # the docs word is unknown now
    ],
)
def test_an_oco_success_reply_is_classified_by_the_python_parser(
    tmp_path: Path, raw: dict[str, Any]
) -> None:
    fake = Fake(tmp_path, _script({"result": "A9", "raw": raw, "http_status": 200}))
    sdk = fake.start()
    try:
        with pytest.raises(OrderStatusUnknownError):
            sdk.cancel_oco("A9")
    finally:
        sdk.close()
    assert len(fake.request_lines()) == 1  # never retried


def test_an_oco_success_reply_without_raw_is_unknown(tmp_path: Path) -> None:
    fake = Fake(tmp_path, _script({"result": "A9"}))
    sdk = fake.start()
    try:
        with pytest.raises(OrderStatusUnknownError):
            sdk.place_oco(**OCO_KWARGS)
    finally:
        sdk.close()
    assert len(fake.request_lines()) == 1
