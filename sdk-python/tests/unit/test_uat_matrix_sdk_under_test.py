"""The matrix's SDK seam: ``PythonSdk`` forwards the eight writes unchanged; the session and the
ledger carry the SDK; tags carry its one-letter code and fit ``TAG_MAX``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.live_orders.uat_matrix import calls
from tests.live_orders.uat_matrix.ledger import TAG_MAX, Ledger
from tests.live_orders.uat_matrix.sdk_under_test import PythonSdk, SdkUnderTest

WRITES = (
    "place_order",
    "place_sliced_order",
    "modify_order",
    "cancel_order",
    "exit_order",
    "place_gtt",
    "modify_gtt",
    "cancel_gtt",
)


class Recorder:
    """A client that records every write (args and kwargs) and returns a marker."""

    client_id = "TESTUAT1"

    def __init__(self) -> None:
        self.seen: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.closed = 0

    def __getattr__(self, name: str) -> Any:
        if name not in WRITES:
            raise AttributeError(name)

        def record(*args: Any, **kwargs: Any) -> str:
            self.seen.append((name, args, kwargs))
            return f"result-of-{name}"

        return record

    def close(self) -> None:
        self.closed += 1


def test_python_sdk_identity() -> None:
    sdk = PythonSdk(Recorder())  # type: ignore[arg-type]
    assert sdk.name == "python" and sdk.code == "p"
    assert sdk.client_id == "TESTUAT1"
    assert sdk.version


@pytest.mark.parametrize("name", WRITES)
def test_every_write_is_forwarded_unchanged(name: str) -> None:
    client = Recorder()
    sdk = PythonSdk(client)  # type: ignore[arg-type]
    method = getattr(sdk, name)
    if name in ("place_order", "place_sliced_order", "place_gtt"):
        out = method(exchange="NSE", tag="t", quantity=1)
        assert client.seen == [(name, (), {"exchange": "NSE", "tag": "t", "quantity": 1})]
    elif name == "exit_order":
        out = method("1001", "CO")
        assert client.seen == [(name, ("1001", "CO"), {})]
    elif name in ("cancel_order", "cancel_gtt"):
        out = method("1001")
        assert client.seen == [(name, ("1001",), {})]
    else:
        out = method("1001", price="9.5", tag="t")
        assert client.seen == [(name, ("1001",), {"price": "9.5", "tag": "t"})]
    assert out == f"result-of-{name}"


def test_errors_pass_through_untouched() -> None:
    class Boom(Recorder):
        def __getattr__(self, name: str) -> Any:
            def fail(*args: Any, **kwargs: Any) -> None:
                raise RuntimeError("boom")

            return fail

    with pytest.raises(RuntimeError, match="boom"):
        PythonSdk(Boom()).cancel_order("1")  # type: ignore[arg-type]


def test_close_closes_the_client() -> None:
    client = Recorder()
    PythonSdk(client).close()  # type: ignore[arg-type]
    assert client.closed == 1


def test_python_sdk_satisfies_the_protocol() -> None:
    sdk: SdkUnderTest = PythonSdk(Recorder())  # type: ignore[arg-type]
    assert sdk.name == "python"


def test_python_sdk_send_slot_is_a_no_op() -> None:
    """Python's sends are not serial: its slot never blocks (nested, or from any thread), so
    its behaviour and timing stay exactly what they were."""
    import contextlib

    sdk = PythonSdk(Recorder())  # type: ignore[arg-type]
    assert isinstance(sdk.send_slot(), contextlib.nullcontext)
    with sdk.send_slot(), sdk.send_slot(), sdk.send_slot():
        pass


def test_the_protocol_has_a_send_slot() -> None:
    sdk: SdkUnderTest = PythonSdk(Recorder())  # type: ignore[arg-type]
    with sdk.send_slot():
        pass


def test_the_write_calls_go_through_the_session_sdk() -> None:
    import inspect

    source = inspect.getsource(calls)
    for name in WRITES:
        assert f"session.client.{name}(" not in source, name
        assert f"session.sdk.{name}(" in source, name


# -- tags ---------------------------------------------------------------------------------------


def test_new_tags_carry_the_sdk_code(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", "1116abcd")
    tag = ledger.new_tag("case-a")
    assert tag == "sdkuat-1116abcd-p001-1" and len(tag) == 22 <= TAG_MAX
    assert ledger.new_tag("case-a") == "sdkuat-1116abcd-p001-2"
    js = Ledger(tmp_path / "js.jsonl", "1116abcd", sdk_code="j")
    assert js.new_tag("case-a") == "sdkuat-1116abcd-j001-1"


def test_the_longest_tag_still_fits(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", "123456789012")  # a 12-character run id
    for _ in range(1, 10):
        tag = ledger.new_tag("case-a")
        assert len(tag) <= TAG_MAX, tag


def test_old_and_new_tags_both_map_back_to_their_case(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path, "1116abcd")
    new = ledger.new_tag("case-a")
    ledger.intent(new, "case-a", "place", segment="NSE", plan={})
    assert ledger.case_of(new) == "case-a"
    # an old-form tag of the same run (written before the SDK code existed)
    old = "sdkuat-1116abcd-001-7"
    assert ledger.case_of(old) == "case-a"
    ledger2 = Ledger(path, "1116abcd")
    ledger2._note_tag_number(old)
    assert ledger2.new_tag("case-a") == "sdkuat-1116abcd-p001-8"  # numbering continues
