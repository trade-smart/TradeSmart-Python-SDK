"""The mock-session switch, the matrix gate in mock mode and the limits preflight, offline.

Mock mode (``tests/live_orders/mock_session.py``; the owner-approved design of 2026-11-13): the
UAT matrix on the **production** server during an exchange mock trading session, only when
``TRADESMART_LIVE_MOCK_SESSION`` is
today's IST date, today is a Saturday, Sunday or a listed holiday, the typed phrase
``TRADESMART_LIVE_MOCK_CONFIRM`` matches exactly, the REST host is exactly the production host,
``TRADESMART_ENV`` is unset or ``production`` and ``TRADESMART_LIVE_ORDER_FUNDED`` is not set.
Fixed dates only: 2026-11-14 is a Saturday, 2026-11-15 a Sunday, 2026-11-16 a Monday.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from tests.live_orders import mock_session, safety
from tests.live_orders.safety import Refusal
from tests.live_orders.uat_matrix import gate
from tests.live_orders.uat_matrix.record import Recorder
from tests.unit.test_uat_matrix_safety import (
    ACCOUNT,
    MATRIX_ENV,
    PRODUCTION_URL,
    UAT_ENV,
    UAT_URL,
    FakeTime,
    MatrixClient,
)
from tradesmart import ClientConfig
from tradesmart.models.base import IST

SATURDAY = date(2026, 11, 14)
SUNDAY = date(2026, 11, 15)
MONDAY = date(2026, 11, 16)
SESSION = "TRADESMART_LIVE_MOCK_SESSION"
CONFIRM = "TRADESMART_LIVE_MOCK_CONFIRM"
FUNDED = "TRADESMART_LIVE_ORDER_FUNDED"
SWITCH = {SESSION: "2026-11-14", CONFIRM: "mock session 2026-11-14"}
MOCK_ENV = {
    "TRADESMART_LIVE_TESTS": "1",
    "TRADESMART_LIVE_ORDER_TESTS": "1",
    "TRADESMART_LIVE_ORDER_ACCOUNT": ACCOUNT,
    **SWITCH,
}
MOCK_MATRIX_ENV = {**MOCK_ENV, "TRADESMART_LIVE_ORDER_UAT_MATRIX": "1"}
SAT_MORNING = datetime(2026, 11, 14, 9, 30, tzinfo=IST)


def _env(**changes: str | None) -> dict[str, str]:
    env = dict(SWITCH)
    for name, value in changes.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def _switch(day: str, **extra: str) -> dict[str, str]:
    return {SESSION: day, CONFIRM: f"mock session {day}", **extra}


# -- the switch ---------------------------------------------------------------------------------


def test_the_holiday_list_is_empty() -> None:
    assert frozenset() == mock_session.MOCK_SESSION_HOLIDAYS


def test_the_production_host_is_the_sdk_default_host() -> None:
    assert urlsplit(ClientConfig().base_url).hostname == mock_session.PRODUCTION_HOST
    assert mock_session.PRODUCTION_HOST == safety.PRODUCTION_HOST


@pytest.mark.parametrize(
    ("environ", "today"),
    [
        pytest.param(SWITCH, SATURDAY, id="saturday"),
        pytest.param(_switch("2026-11-15"), SUNDAY, id="sunday"),
        pytest.param({**SWITCH, "TRADESMART_ENV": "production"}, SATURDAY, id="env-production"),
        pytest.param(
            {SESSION: " 2026-11-14\n", CONFIRM: "\t mock session 2026-11-14  "},
            SATURDAY,
            id="surrounding-whitespace",
        ),
    ],
)
def test_the_switch_passes(environ: dict[str, str], today: date) -> None:
    assert mock_session.refusal(environ, today, PRODUCTION_URL) is None


@pytest.mark.parametrize(
    ("environ", "today", "url", "named"),
    [
        pytest.param(SWITCH, SUNDAY, PRODUCTION_URL, "today", id="date-not-today"),
        pytest.param(_switch("2026-11-15"), SATURDAY, PRODUCTION_URL, "today", id="tomorrow"),
        pytest.param(_switch("2026-11-16"), MONDAY, PRODUCTION_URL, "weekday", id="weekday"),
        pytest.param(SWITCH, SATURDAY, UAT_URL, "production host", id="uat-host"),
        pytest.param(
            SWITCH,
            SATURDAY,
            "https://v2api.tradesmartonline.in.evil.test/NorenWClientAPIv2",
            "production host",
            id="host-starts-with-production",
        ),
        pytest.param(
            SWITCH,
            SATURDAY,
            "https://uat.v2api.tradesmartonline.in/NorenWClientAPIv2",
            "production host",
            id="host-contains-production",
        ),
        pytest.param(
            SWITCH,
            SATURDAY,
            "https://someone@v2api.tradesmartonline.in/NorenWClientAPIv2",
            "production host",
            id="user-info",
        ),
        pytest.param(SWITCH, SATURDAY, "not a url", "production host", id="not-a-url"),
        pytest.param(
            _env(TRADESMART_ENV="uat"), SATURDAY, PRODUCTION_URL, "TRADESMART_ENV", id="env-uat"
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_ENV="staging"),
            SATURDAY,
            PRODUCTION_URL,
            "TRADESMART_ENV",
            id="env-staging",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_ORDER_FUNDED="1"), SATURDAY, PRODUCTION_URL, FUNDED, id="funded-1"
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_ORDER_FUNDED="0"), SATURDAY, PRODUCTION_URL, FUNDED, id="funded-0"
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_ORDER_FUNDED=""),
            SATURDAY,
            PRODUCTION_URL,
            FUNDED,
            id="funded-empty",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_MOCK_CONFIRM=None),
            SATURDAY,
            PRODUCTION_URL,
            CONFIRM,
            id="phrase-missing",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_MOCK_CONFIRM=""),
            SATURDAY,
            PRODUCTION_URL,
            CONFIRM,
            id="phrase-empty",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_MOCK_CONFIRM="Mock session 2026-11-14"),
            SATURDAY,
            PRODUCTION_URL,
            CONFIRM,
            id="phrase-case",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_MOCK_CONFIRM="MOCK SESSION 2026-11-14"),
            SATURDAY,
            PRODUCTION_URL,
            CONFIRM,
            id="phrase-upper",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_MOCK_CONFIRM="mock session 2026-11-15"),
            SATURDAY,
            PRODUCTION_URL,
            CONFIRM,
            id="phrase-wrong-date",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_MOCK_CONFIRM="mock  session 2026-11-14"),
            SATURDAY,
            PRODUCTION_URL,
            CONFIRM,
            id="phrase-inner-space",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_MOCK_CONFIRM="mock session  2026-11-14"),
            SATURDAY,
            PRODUCTION_URL,
            CONFIRM,
            id="phrase-inner-space-2",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_MOCK_CONFIRM="mock session 2026-11-14."),
            SATURDAY,
            PRODUCTION_URL,
            CONFIRM,
            id="phrase-trailing-dot",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_MOCK_SESSION=None),
            SATURDAY,
            PRODUCTION_URL,
            SESSION,
            id="date-missing",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_MOCK_SESSION=""),
            SATURDAY,
            PRODUCTION_URL,
            SESSION,
            id="date-empty",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_MOCK_SESSION="2026-11-31"),
            SATURDAY,
            PRODUCTION_URL,
            SESSION,
            id="date-impossible",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_MOCK_SESSION="14-11-2026"),
            SATURDAY,
            PRODUCTION_URL,
            SESSION,
            id="date-day-first",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_MOCK_SESSION="20261114"),
            SATURDAY,
            PRODUCTION_URL,
            SESSION,
            id="date-basic-format",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_MOCK_SESSION="2026-11-14T00:00"),
            SATURDAY,
            PRODUCTION_URL,
            SESSION,
            id="date-with-time",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_MOCK_SESSION="2026 -11-14"),
            SATURDAY,
            PRODUCTION_URL,
            SESSION,
            id="date-inner-space",
        ),  # fmt: skip
        pytest.param(
            _env(TRADESMART_LIVE_MOCK_SESSION="２０２６-11-14"),
            SATURDAY,
            PRODUCTION_URL,
            SESSION,
            id="date-non-ascii-digits",
        ),  # fmt: skip
    ],
)
def test_the_switch_is_refused_naming_the_check(
    environ: dict[str, str], today: date, url: str, named: str
) -> None:
    reason = mock_session.refusal(environ, today, url)
    assert reason is not None and named in reason


def test_a_listed_holiday_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    environ = _switch("2026-11-16")
    assert mock_session.refusal(environ, MONDAY, PRODUCTION_URL) is not None
    monkeypatch.setattr(mock_session, "MOCK_SESSION_HOLIDAYS", frozenset({MONDAY}))
    assert mock_session.refusal(environ, MONDAY, PRODUCTION_URL) is None


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, False),
        ({"TRADESMART_ENV": "uat"}, False),
        ({SESSION: "2026-11-14"}, True),
        ({SESSION: ""}, True),
        ({SESSION: "nonsense"}, True),
        ({CONFIRM: "mock session 2026-11-14"}, True),  # a half-set switch is a mistake: refused
    ],
)
def test_requested_means_any_part_of_the_switch_is_present(
    environ: dict[str, str], expected: bool
) -> None:
    assert mock_session.requested(environ) is expected


def test_switch_date_is_the_parsed_date_or_none() -> None:
    assert mock_session.switch_date(SWITCH) == SATURDAY
    assert mock_session.switch_date({SESSION: " 2026-11-14 "}) == SATURDAY
    assert mock_session.switch_date({SESSION: "14-11-2026"}) is None
    assert mock_session.switch_date({}) is None


# -- the matrix gate ----------------------------------------------------------------------------


@pytest.mark.parametrize("tier", [gate.SMOKE, gate.FULL])
def test_the_gate_opens_production_with_a_valid_switch(tier: str) -> None:
    env = MOCK_MATRIX_ENV if tier == gate.FULL else MOCK_ENV
    assert gate.gate_refusal(env, PRODUCTION_URL, tier, today=SATURDAY) is None


def test_the_full_tier_still_needs_the_matrix_opt_in_in_mock_mode() -> None:
    reason = gate.gate_refusal(MOCK_ENV, PRODUCTION_URL, gate.FULL, today=SATURDAY)
    assert reason is not None and "TRADESMART_LIVE_ORDER_UAT_MATRIX=1" in reason


@pytest.mark.parametrize(
    "env",
    [
        {k: v for k, v in MOCK_ENV.items() if k != "TRADESMART_LIVE_ORDER_TESTS"},
        {k: v for k, v in MOCK_ENV.items() if k != "TRADESMART_LIVE_TESTS"},
        {**MOCK_ENV, "PYTEST_XDIST_WORKER": "gw0"},
    ],
)
def test_the_order_opt_ins_still_apply_in_mock_mode(env: dict[str, str]) -> None:
    assert gate.gate_refusal(env, PRODUCTION_URL, gate.SMOKE, today=SATURDAY) is not None


def test_the_gate_refuses_production_without_the_switch_as_today() -> None:
    env = {k: v for k, v in MOCK_ENV.items() if k not in SWITCH}
    reason = gate.gate_refusal(env, PRODUCTION_URL, gate.SMOKE, today=SATURDAY)
    assert reason is not None and "production host" in reason and "UAT only" in reason


@pytest.mark.parametrize(
    ("env", "url"),
    [
        pytest.param({**UAT_ENV, **SWITCH}, UAT_URL, id="uat-env-and-host"),
        pytest.param({**MOCK_ENV, "TRADESMART_ENV": "uat"}, PRODUCTION_URL, id="uat-env"),
        pytest.param(MOCK_ENV, UAT_URL, id="uat-host"),
        pytest.param({**UAT_ENV, CONFIRM: "mock session 2026-11-14"}, UAT_URL, id="phrase-only"),
    ],
)
def test_the_switch_on_a_uat_run_is_refused(env: dict[str, str], url: str) -> None:
    assert gate.gate_refusal(env, url, gate.SMOKE, today=SATURDAY) is not None


def test_the_gate_refuses_the_switch_on_a_weekday() -> None:
    env = {**MOCK_ENV, **_switch("2026-11-16")}
    reason = gate.gate_refusal(env, PRODUCTION_URL, gate.SMOKE, today=MONDAY)
    assert reason is not None and "weekday" in reason


def test_the_collection_gate_uses_the_production_default_host() -> None:
    assert gate.collection_refusal(MOCK_ENV, gate.SMOKE, today=SATURDAY) is None
    elsewhere = {**MOCK_ENV, "TRADESMART_BASE_URL": "https://example.test/NorenWClientAPIv2"}
    reason = gate.collection_refusal(elsewhere, gate.SMOKE, today=SATURDAY)
    assert reason is not None and "production host" in reason
    assert gate.collection_refusal(MOCK_ENV, gate.SMOKE, today=MONDAY) is not None


# -- the matrix session -------------------------------------------------------------------------


def _session(
    client: MatrixClient,
    tmp_path: Path,
    environ: Mapping[str, str] = MOCK_ENV,
    now: datetime = SAT_MORNING,
) -> gate.MatrixSession:
    t = FakeTime()
    return gate.matrix_session(
        environ,
        client,  # type: ignore[arg-type]
        root=tmp_path,
        now=now,
        sleep=t.sleep,
        clock=t.clock,
    )


def _prod() -> MatrixClient:
    return MatrixClient(base_url=PRODUCTION_URL)


def test_a_mock_session_opens_on_production_with_mock_and_not_uat(tmp_path: Path) -> None:
    client = _prod()
    session = _session(client, tmp_path)
    assert session.mock is True and session.uat is False
    assert session.mock_date == SATURDAY
    assert session.uat_quote_pacer() is None
    # read-only calls only: the account, the limits preflight, the open-order invariant
    assert [name for name, _ in client.calls] == ["profile", "margins", "orders"]


def test_the_mock_preflight_runs_with_the_mock_minimum_before_any_order(tmp_path: Path) -> None:
    client = _prod()
    at_minimum = str(safety.MOCK_MIN_LIMIT)
    client.limits = {"stat": "Ok", "cash": at_minimum, "turnoverlmt": at_minimum,
                     "pendordvallmt": at_minimum}  # fmt: skip
    session = _session(client, tmp_path)
    run = json.loads((session.run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["mock_preflight"]["minimum"] == str(safety.MOCK_MIN_LIMIT)
    assert run["mock_preflight"]["passed"] is True
    assert "uat_preflight" not in run
    lines = [json.loads(x) for x in session.ledger.path.read_text(encoding="utf-8").splitlines()]
    assert any(line.get("type") == "mock_preflight" for line in lines)


@pytest.mark.parametrize("field", ["cash", "turnoverlmt", "pendordvallmt"])
def test_the_mock_preflight_below_the_minimum_refuses_with_no_order_call(
    tmp_path: Path, field: str
) -> None:
    client = _prod()
    below = str(safety.MOCK_MIN_LIMIT - Decimal("0.01"))
    client.limits = {**client.limits, field: below}
    with pytest.raises(Refusal) as caught:
        _session(client, tmp_path)
    assert field in caught.value.reason and "mock-session preflight" in caught.value.reason
    assert [name for name, _ in client.calls] == ["profile", "margins"]
    assert list(tmp_path.iterdir()) == []


def test_run_file_and_ledger_carry_the_mock_environment_and_date(tmp_path: Path) -> None:
    session = _session(_prod(), tmp_path)
    text = (session.run_dir / "run.json").read_text(encoding="utf-8")
    run = json.loads(text)
    assert run["environment"] == "production-mock"
    assert run["mock_session"] == "2026-11-14"
    assert run["trading_day"] == "2026-11-14"
    assert ACCOUNT not in text
    lines = [json.loads(x) for x in session.ledger.path.read_text(encoding="utf-8").splitlines()]
    [note] = [line for line in lines if line.get("type") == "mock_session"]
    assert note["environment"] == "production-mock" and note["mock_session"] == "2026-11-14"


def test_every_record_of_a_mock_run_carries_the_environment_and_date(tmp_path: Path) -> None:
    session = _session(_prod(), tmp_path)
    assert session.record_fields() == {
        "environment": "production-mock",
        "mock_session": "2026-11-14",
    }
    recorder = Recorder(session.run_dir, run_id=session.run_id)
    written = recorder.write({"case_id": "x", "environment": "spoofed"})
    assert written["environment"] == "production-mock"
    assert written["mock_session"] == "2026-11-14"


def test_a_uat_session_is_not_mock_and_its_records_are_unchanged(tmp_path: Path) -> None:
    t = FakeTime()
    session = gate.matrix_session(
        MATRIX_ENV, MatrixClient(), root=tmp_path,  # type: ignore[arg-type]
        now=datetime(2026, 11, 13, 10, 0, tzinfo=IST), sleep=t.sleep, clock=t.clock,
    )  # fmt: skip
    assert session.mock is False and session.uat is True and session.mock_date is None
    assert session.record_fields() == {}
    assert session.mock_refusal() is None
    written = Recorder(session.run_dir, run_id=session.run_id).write({"case_id": "x"})
    assert "mock_session" not in written and "environment" not in written


def test_a_production_session_without_the_switch_is_refused_before_any_call(
    tmp_path: Path,
) -> None:
    client = _prod()
    env = {k: v for k, v in MOCK_ENV.items() if k not in SWITCH}
    with pytest.raises(Refusal, match="production host"):
        _session(client, tmp_path, env)
    assert client.calls == [] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("env", "now"),
    [
        pytest.param(
            {**MOCK_ENV, **_switch("2026-11-16")},
            datetime(2026, 11, 16, 10, 0, tzinfo=IST),
            id="weekday",
        ),  # fmt: skip
        pytest.param(MOCK_ENV, datetime(2026, 11, 14, 19, 0, tzinfo=UTC), id="ist-is-sunday"),
        pytest.param({**MOCK_ENV, FUNDED: "0"}, SAT_MORNING, id="funded-0"),
        pytest.param(
            {**MOCK_ENV, "TRADESMART_LIVE_ORDER_UAT_IGNORE_OPEN": "abc"},
            SAT_MORNING,
            id="ignore-open-must-be-order-numbers",
        ),  # fmt: skip
    ],
)
def test_a_failed_switch_refuses_the_session_before_any_call(
    tmp_path: Path, env: dict[str, str], now: datetime
) -> None:
    client = _prod()
    with pytest.raises(Refusal):
        _session(client, tmp_path, env, now=now)
    assert client.calls == [] and list(tmp_path.iterdir()) == []


def test_a_mock_session_carries_named_ignored_orders(tmp_path: Path) -> None:
    # Owner, 14 Nov 2026: orders the API still lists as open after a cancel may be named.
    env = {**MOCK_ENV, "TRADESMART_LIVE_ORDER_UAT_IGNORE_OPEN": "777"}
    session = _session(_prod(), tmp_path, env)
    assert session.mock is True and session.ignored_open == frozenset({"777"})


def test_the_switch_date_is_compared_with_the_ist_date(tmp_path: Path) -> None:
    # 2026-11-13 20:00 UTC is 2026-11-14 01:30 IST: the switch date is today.
    session = _session(_prod(), tmp_path, now=datetime(2026, 11, 13, 20, 0, tzinfo=UTC))
    assert session.mock is True and session.trading_day == SATURDAY


def test_the_session_rechecks_the_switch_against_the_clock(tmp_path: Path) -> None:
    session = _session(_prod(), tmp_path)
    session.today = lambda: SATURDAY
    assert session.mock_refusal() is None
    session.today = lambda: SUNDAY  # the run crossed midnight IST
    reason = session.mock_refusal()
    assert reason is not None and "today" in reason


# -- resume -------------------------------------------------------------------------------------


def test_a_mock_run_resumes_with_the_same_switch_date(tmp_path: Path) -> None:
    first = _session(_prod(), tmp_path)
    gate.release_run_lock(first)
    env = {**MOCK_ENV, "TRADESMART_UAT_RUN_ID": first.run_id}
    later = datetime(2026, 11, 14, 14, 0, tzinfo=IST)
    second = _session(_prod(), tmp_path, env, now=later)
    assert second.resumed is True and second.mock is True and second.run_id == first.run_id


def test_a_uat_run_cannot_be_resumed_in_mock_mode(tmp_path: Path) -> None:
    t = FakeTime()
    first = gate.matrix_session(
        MATRIX_ENV, MatrixClient(), root=tmp_path, now=SAT_MORNING,  # type: ignore[arg-type]
        sleep=t.sleep, clock=t.clock,
    )  # fmt: skip
    gate.release_run_lock(first)
    client = _prod()
    env = {**MOCK_ENV, "TRADESMART_UAT_RUN_ID": first.run_id}
    with pytest.raises(Refusal, match="mock"):
        _session(client, tmp_path, env)
    assert client.calls == []


def test_a_mock_run_cannot_be_resumed_outside_mock_mode(tmp_path: Path) -> None:
    first = _session(_prod(), tmp_path)
    gate.release_run_lock(first)
    client = MatrixClient()  # UAT, the same day
    env = {**MATRIX_ENV, "TRADESMART_UAT_RUN_ID": first.run_id}
    t = FakeTime()
    with pytest.raises(Refusal, match="mock"):
        gate.matrix_session(
            env, client, root=tmp_path, now=SAT_MORNING,  # type: ignore[arg-type]
            sleep=t.sleep, clock=t.clock,
        )  # fmt: skip
    assert client.calls == []


def test_a_mock_run_with_another_switch_date_is_not_resumed(tmp_path: Path) -> None:
    first = _session(_prod(), tmp_path)
    gate.release_run_lock(first)
    run_file = first.run_dir / "run.json"
    data = json.loads(run_file.read_text(encoding="utf-8"))
    data["mock_session"] = "2026-11-07"  # tampered: the run's switch date differs
    run_file.write_text(json.dumps(data), encoding="utf-8")
    client = _prod()
    env = {**MOCK_ENV, "TRADESMART_UAT_RUN_ID": first.run_id}
    with pytest.raises(Refusal, match="mock"):
        _session(client, tmp_path, env)
    assert client.calls == []


# -- the limits preflight -----------------------------------------------------------------------


class _LimitsClient:
    def __init__(self, limits: dict[str, Any]) -> None:
        self.limits = limits
        self.calls = 0

    def margins(self) -> Any:
        from tradesmart.models.portfolio import Limits

        self.calls += 1
        return Limits.model_validate(self.limits)


def test_limits_preflight_takes_the_minimum_and_the_label() -> None:
    value = str(safety.MOCK_MIN_LIMIT)
    client = _LimitsClient({"stat": "Ok", "cash": value, "turnoverlmt": value,
                            "pendordvallmt": value})  # fmt: skip
    mock = safety.limits_preflight(client, safety.MOCK_MIN_LIMIT, "mock-session")  # type: ignore[arg-type]
    assert mock.refusal is None and mock.record()["minimum"] == value
    uat = safety.limits_preflight(client, safety.UAT_MIN_LIMIT, "UAT")  # type: ignore[arg-type]
    assert uat.refusal is not None and uat.refusal.startswith("UAT preflight refused")
    assert uat.record()["minimum"] == str(safety.UAT_MIN_LIMIT)
    assert client.calls == 2


def test_the_mock_preflight_message_names_the_mock_session() -> None:
    client = _LimitsClient({"stat": "Ok", "cash": "1"})
    check = safety.limits_preflight(
        client,  # type: ignore[arg-type]
        safety.MOCK_MIN_LIMIT,
        "mock-session",
        ask=mock_session.LIMITS_ASK,
    )
    assert check.refusal is not None
    assert check.refusal.startswith("mock-session preflight refused before any order")
    assert mock_session.LIMITS_ASK in check.refusal
    assert "UAT" not in check.refusal
