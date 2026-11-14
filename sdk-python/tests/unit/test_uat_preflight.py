"""The UAT preflight (owner, 2026-11-13), offline: before any order of a UAT order run, a
read-only ``margins()`` must show cash, the turnover limit and the pending-order value limit at
the code minimum or more; otherwise the run is refused with one message listing every unmet item (an
unreadable or missing field refuses too). UAT only: production, funded and zero-funds runs never
read it. The values are recorded (no account ids)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.live_orders import conftest as live_conftest
from tests.live_orders import safety
from tests.live_orders.safety import Refusal
from tests.unit.test_uat_matrix_safety import ACCOUNT, MATRIX_ENV, MatrixClient, _session
from tradesmart import ClientConfig
from tradesmart.models.portfolio import Limits

CASH_MET = "100000000.00"
MET = {"stat": "Ok", "cash": CASH_MET, "turnoverlmt": "250000000", "pendordvallmt": "100000000"}
UAT_URL = "https://uat.example.test/NorenWClientAPIv2"
PRODUCTION_URL = "https://v2api.tradesmartonline.in/NorenWClientAPIv2"
FIELDS = ("cash", "turnoverlmt", "pendordvallmt")


class _Client:
    def __init__(self, limits: Any, base_url: str = UAT_URL) -> None:
        self.config = ClientConfig(base_url=base_url)
        self.limits = limits
        self.calls: list[str] = []

    def margins(self) -> Limits:
        self.calls.append("margins")
        if isinstance(self.limits, BaseException):
            raise self.limits
        return Limits.model_validate(self.limits)


def _check(limits: Any) -> safety.UatPreflight:
    return safety.uat_preflight(_Client(limits))  # type: ignore[arg-type]


def test_the_thresholds_are_in_one_place() -> None:
    assert Decimal("100000000") == safety.UAT_MIN_LIMIT
    assert tuple(safety.UAT_PREFLIGHT_FIELDS) == FIELDS


def test_every_limit_at_the_minimum_or_more_passes() -> None:
    check = _check(MET)
    assert check.refusal is None
    assert check.record() == {
        "cash": CASH_MET,
        "turnoverlmt": "250000000",
        "pendordvallmt": "100000000",
        "minimum": "100000000",
        "passed": True,
    }


@pytest.mark.parametrize("field", FIELDS)
def test_each_limit_below_the_minimum_refuses_with_the_rms_ask(field: str) -> None:
    check = _check({**MET, field: "99999999.99"})
    assert check.refusal is not None
    assert field in check.refusal and "99999999.99" in check.refusal
    assert "ask the UAT RMS team to raise it" in check.refusal
    assert check.record()["passed"] is False
    for other in set(FIELDS) - {field}:
        assert other not in check.refusal  # only the unmet item is listed


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", [None, ""], ids=["missing", "blank"])
def test_a_missing_limit_refuses_naming_the_field(field: str, value: str | None) -> None:
    limits = dict(MET)
    if value is None:
        del limits[field]
    else:
        limits[field] = value
    check = _check(limits)
    assert check.refusal is not None and field in check.refusal
    assert "missing or unreadable" in check.refusal
    assert check.record()[field] is None


def test_every_unmet_item_is_listed_in_one_message() -> None:
    check = _check({"stat": "Ok", "cash": "0", "turnoverlmt": "5"})
    assert check.refusal is not None
    for field in FIELDS:
        assert field in check.refusal
    assert check.refusal.count("ask the UAT RMS team to raise it") == 1


@pytest.mark.parametrize(
    "limits",
    [{**MET, "stat": "Not_Ok"}, OSError("read timeout")],
    ids=["not-ok", "raises"],
)
def test_an_unreadable_margins_reply_refuses(limits: Any) -> None:
    check = _check(limits)
    assert check.refusal is not None and "margins()" in check.refusal
    assert "ask the UAT RMS team to raise it" in check.refusal


def test_the_record_carries_no_account_id() -> None:
    check = _check({**MET, "actid": ACCOUNT, "uid": ACCOUNT})
    assert ACCOUNT not in json.dumps(check.record())


# -- the older UAT order suites (``order_session``) -------------------------------------------


def test_the_preflight_is_never_read_outside_uat_mode() -> None:
    production = _Client({"stat": "Ok", "cash": "0"}, PRODUCTION_URL)
    assert live_conftest.uat_preflight_refusal(safety.OrderSession(production)) is None  # type: ignore[arg-type]
    funded = _Client({"stat": "Ok", "cash": "0"}, PRODUCTION_URL)
    session = safety.OrderSession(funded, funded=True)  # type: ignore[arg-type]
    assert live_conftest.uat_preflight_refusal(session) is None
    assert production.calls == [] and funded.calls == []


def test_a_uat_session_below_the_limits_is_refused_and_recorded(tmp_path: Path) -> None:
    client = _Client({**MET, "turnoverlmt": "1000"})
    recorder = safety.Recorder(tmp_path / "live_orders.jsonl")
    session = safety.OrderSession(client, uat=True, recorder=recorder)  # type: ignore[arg-type]
    reason = live_conftest.uat_preflight_refusal(session)
    assert reason is not None and "turnoverlmt" in reason
    assert client.calls == ["margins"]
    [line] = [json.loads(x) for x in recorder.path.read_text(encoding="utf-8").splitlines()]
    assert line["uat_preflight"]["turnoverlmt"] == "1000"
    assert line["uat_preflight"]["passed"] is False


def test_a_uat_session_with_every_limit_met_passes(tmp_path: Path) -> None:
    client = _Client(MET)
    recorder = safety.Recorder(tmp_path / "live_orders.jsonl")
    session = safety.OrderSession(client, uat=True, recorder=recorder)  # type: ignore[arg-type]
    assert live_conftest.uat_preflight_refusal(session) is None
    assert "uat_preflight" in recorder.path.read_text(encoding="utf-8")


def test_order_session_runs_the_preflight_after_the_account_check_and_before_any_test() -> None:
    text = Path(live_conftest.__file__).read_text(encoding="utf-8")
    account = text.index("safety.account_refusal(")
    preflight = text.index("uat_preflight_refusal(session)")
    assert account < preflight < text.index("yield session")


# -- the UAT matrix (``gate.matrix_session``) -------------------------------------------------


@pytest.mark.parametrize("field", FIELDS)
def test_the_matrix_session_refuses_before_any_order(tmp_path: Path, field: str) -> None:
    client = MatrixClient()
    client.limits = {**MET, field: "1"}
    with pytest.raises(Refusal, match="ask the UAT RMS team to raise it"):
        _session(client, tmp_path, dict(MATRIX_ENV))
    names = [name for name, _ in client.calls]
    assert names == ["profile", "margins"]  # read-only, then refused: no order book, no order
    assert list(tmp_path.iterdir()) == []


def test_the_matrix_session_records_the_preflight_values(tmp_path: Path) -> None:
    session = _session(MatrixClient(), tmp_path, dict(MATRIX_ENV))
    run = json.loads((session.run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["uat_preflight"]["passed"] is True
    assert run["uat_preflight"]["minimum"] == "100000000"
    lines = [json.loads(x) for x in session.ledger.path.read_text(encoding="utf-8").splitlines()]
    assert any(line.get("type") == "uat_preflight" for line in lines)
    assert ACCOUNT not in (session.run_dir / "run.json").read_text(encoding="utf-8")


def test_the_mock_session_minimum_is_pinned() -> None:
    # Used by the mock-session preflight (``tests/unit/test_mock_session.py``).
    assert Decimal("2000000") == safety.MOCK_MIN_LIMIT
