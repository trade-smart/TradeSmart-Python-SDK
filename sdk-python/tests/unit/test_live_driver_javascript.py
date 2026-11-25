"""The JavaScript driver in the matrix (Task 13): its registration, the factory's wiring, the
stale-build refusal, the multi-SDK session behaviour that needed a second registered SDK, and the
end-to-end seam against the offline broker.

Everything is offline. Every environment is a mapping given to the code under test: no real
``TRADESMART_LIVE_*`` variable is read or set, no account exists, no order is placed. The
end-to-end tests start ``sdk-javascript/tests/helpers/offline-driver.ts`` (the real driver program
on the offline broker's in-memory dispatcher, so it cannot reach a network); they run only where
Node is installed (the live-sdks image) and skip with that reason elsewhere.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.live_orders import safety
from tests.live_orders.uat_matrix import calls, gate, sdk_select
from tests.live_orders.uat_matrix import conftest as matrix_conftest
from tests.live_orders.uat_matrix import test_views as m9
from tests.live_orders.uat_matrix.driver_process import (
    DRIVER_RUN_ENV,
    DriverSdk,
    default_reply_timeout,
)
from tests.live_orders.uat_matrix.run_group import RunGroup
from tests.live_orders.uat_matrix.sdk_under_test import referee_secrets
from tests.unit.test_uat_matrix_driver_process import FakeSdk, _session
from tests.unit.test_uat_matrix_safety import ACCOUNT, MATRIX_ENV, NSE, PLAN, MatrixClient
from tradesmart import ClientConfig, OcoLeg, OrderType

REPO = Path(__file__).resolve().parents[3]
JS_ROOT = REPO / "sdk-javascript"
OFFLINE_DRIVER = JS_ROOT / "tests" / "helpers" / "offline-driver.ts"
NODE = shutil.which("node")

# The JavaScript repository holds the driver; the Python-only export has no such folder.
pytestmark = pytest.mark.skipif(
    not JS_ROOT.is_dir(), reason="sdk-javascript/ is not in this tree (the Python-only export)"
)


# -- registration ---------------------------------------------------------------------------------


def test_javascript_is_registered_after_python() -> None:
    assert list(sdk_select.REGISTRY) == ["python", "javascript"]
    assert sdk_select.selected_sdks({"TRADESMART_LIVE_SDKS": "all"}) == ["python", "javascript"]
    assert sdk_select.selected_sdks({"TRADESMART_LIVE_SDKS": "javascript"}) == ["javascript"]
    assert sdk_select.selected_sdks({"TRADESMART_LIVE_SDKS": "javascript,python"}) == [
        "javascript",
        "python",
    ]
    assert sdk_select.selected_sdks({}) == ["python"]  # unset is still Python alone


def test_the_registered_command_is_node_on_the_driver_program() -> None:
    node, program = sdk_select.JS_COMMAND
    assert node == "node"
    assert program == str(JS_ROOT / "tests" / "live_orders" / "driver.ts")
    assert Path(program).is_file()  # the real driver exists where the command points
    if REPO.as_posix() == "/repo":  # in the live-sdks image the path is the documented one
        assert program == "/repo/sdk-javascript/tests/live_orders/driver.ts"


# -- the factory's wiring (Task 12 carries 1 and 2) -----------------------------------------------


class Recorded:
    """Stands in for ``DriverSdk``: records how the factory built it (no process starts)."""

    made: list[Recorded] = []

    def __init__(self, command: Any, **kwargs: Any) -> None:
        self.command = list(command)
        self.kwargs = kwargs
        Recorded.made.append(self)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> type[Recorded]:
    Recorded.made = []
    monkeypatch.setattr(sdk_select, "DriverSdk", Recorded)
    return Recorded


def _fresh_js(tmp_path: Path) -> Path:
    """A JavaScript tree whose build is newer than its sources."""
    root = tmp_path / "sdk-javascript"
    (root / "src" / "lib").mkdir(parents=True)
    (root / "dist").mkdir()
    (root / "src" / "index.ts").write_text("export {};\n", encoding="utf-8")
    (root / "src" / "lib" / "a.ts").write_text("export {};\n", encoding="utf-8")
    (root / "dist" / "index.js").write_text("export {};\n", encoding="utf-8")
    _set_mtime(root / "src" / "index.ts", 1_000)
    _set_mtime(root / "src" / "lib" / "a.ts", 1_000)
    _set_mtime(root / "dist" / "index.js", 2_000)
    return root


def _set_mtime(path: Path, seconds: int) -> None:
    os.utime(path, ns=(seconds * 1_000_000_000, seconds * 1_000_000_000))


class Referee:
    """A referee client as the factory sees it: id, config and the saved login's secrets."""

    client_id = "TEST01"

    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        token = SimpleNamespace(
            access_token="synthetic-access-token-0001",
            susertoken="synthetic-session-token-0002",
            refresh_token="synthetic-refresh-token-0003",
        )
        self._creds = SimpleNamespace(
            _token=token, _app_key="synthetic-app-key-0004", _secret_key="synthetic-secret-0005"
        )


def test_the_factory_builds_a_driver_with_the_referees_identity_and_secrets(
    recorded: type[Recorded], tmp_path: Path
) -> None:
    client = Referee(ClientConfig())
    environ = {**MATRIX_ENV}
    sdk_select.build_javascript(
        client,  # type: ignore[arg-type]
        environ,
        js_root=_fresh_js(tmp_path),
        results_root=tmp_path / "results",
    )
    (built,) = recorded.made
    assert built.command == list(sdk_select.JS_COMMAND)
    kwargs = built.kwargs
    assert kwargs["environ"] is environ  # the run's environment, as given
    assert kwargs["referee_client_id"] == client.client_id
    assert kwargs["name"] == "javascript"
    assert kwargs["secrets"] == referee_secrets(client)
    assert len(kwargs["secrets"]) == 5  # access, session, refresh, app key, secret key


def test_the_nonce_is_fresh_random_and_only_passed_to_the_driver(
    recorded: type[Recorded], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = Referee(ClientConfig())
    environ = {**MATRIX_ENV}
    root = _fresh_js(tmp_path)
    with caplog.at_level("DEBUG"):
        sdk_select.build_javascript(client, environ, js_root=root, results_root=tmp_path)  # type: ignore[arg-type]
        sdk_select.build_javascript(client, environ, js_root=root, results_root=tmp_path)  # type: ignore[arg-type]
    first, second = (item.kwargs["nonce"] for item in recorded.made)
    assert re.fullmatch(r"[0-9a-f]{32}", first) and re.fullmatch(r"[0-9a-f]{32}", second)
    assert first != second  # fresh for every run
    assert DRIVER_RUN_ENV not in environ  # never put in the harness's own environment
    assert first not in str(environ) and first not in caplog.text  # never logged
    assert first not in repr(recorded.made[0].command)  # nor on the command line


def test_the_nonce_comes_from_secrets_token_hex(
    recorded: type[Recorded], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sdk_select.secrets, "token_hex", lambda n=32: "ab" * 16)
    sdk_select.build_javascript(
        Referee(ClientConfig()),  # type: ignore[arg-type]
        MATRIX_ENV,
        js_root=_fresh_js(tmp_path),
        results_root=tmp_path,
    )
    assert recorded.made[0].kwargs["nonce"] == "ab" * 16


@pytest.mark.parametrize(
    "config",
    [
        ClientConfig(),
        ClientConfig(connect_timeout=7, read_timeout=20, order_rate_limit_max_wait=2),
        ClientConfig(connect_timeout=3, read_timeout=4, order_rate_limit_max_wait=0.5),
    ],
)
def test_the_reply_timeout_comes_from_the_referee_clients_config(
    recorded: type[Recorded], tmp_path: Path, config: ClientConfig
) -> None:
    sdk_select.build_javascript(
        Referee(config),  # type: ignore[arg-type]
        MATRIX_ENV,
        js_root=_fresh_js(tmp_path),
        results_root=tmp_path,
    )
    timeout = recorded.made[0].kwargs["reply_timeout"]
    assert timeout == default_reply_timeout(config)
    assert timeout == (
        config.connect_timeout + config.read_timeout + config.order_rate_limit_max_wait + 14.0
    )


def test_the_default_config_gives_the_documented_thirty_seconds(
    recorded: type[Recorded], tmp_path: Path
) -> None:
    sdk_select.build_javascript(
        Referee(ClientConfig()),  # type: ignore[arg-type]
        MATRIX_ENV,
        js_root=_fresh_js(tmp_path),
        results_root=tmp_path,
    )
    assert recorded.made[0].kwargs["reply_timeout"] == 30.0


def test_the_driver_stderr_log_is_a_file_under_the_results_folder(
    recorded: type[Recorded], tmp_path: Path
) -> None:
    results = tmp_path / "uat-matrix"
    sdk_select.build_javascript(
        Referee(ClientConfig()),  # type: ignore[arg-type]
        MATRIX_ENV,
        js_root=_fresh_js(tmp_path),
        results_root=results,
    )
    log = recorded.made[0].kwargs["stderr_log"]
    assert isinstance(log, Path) and results in log.parents
    assert "javascript" in log.name and log.suffix == ".jsonl"


def test_the_registered_factory_uses_the_gate_results_root(
    recorded: type[Recorded], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sdk_select, "RESULTS_ROOT", tmp_path / "results")
    monkeypatch.setattr(sdk_select, "JS_ROOT", _fresh_js(tmp_path))
    sdk_select.build_sdk("javascript", Referee(ClientConfig()), MATRIX_ENV)  # type: ignore[arg-type]
    (built,) = recorded.made
    assert tmp_path / "results" in built.kwargs["stderr_log"].parents


# -- the stale-build refusal ----------------------------------------------------------------------


def test_a_build_older_than_the_sources_is_refused_before_any_process(
    recorded: type[Recorded], tmp_path: Path
) -> None:
    root = _fresh_js(tmp_path)
    _set_mtime(root / "src" / "lib" / "a.ts", 3_000)  # a source changed after the build
    with pytest.raises(safety.Refusal, match=r"older than .*src") as caught:
        sdk_select.build_javascript(
            Referee(ClientConfig()),  # type: ignore[arg-type]
            MATRIX_ENV,
            js_root=root,
            results_root=tmp_path,
        )
    assert "npm run build" in caught.value.reason
    assert "a.ts" in caught.value.reason  # names the newest source
    assert recorded.made == []  # no driver, no process


def test_a_build_exactly_as_new_as_the_sources_is_accepted(
    recorded: type[Recorded], tmp_path: Path
) -> None:
    root = _fresh_js(tmp_path)
    _set_mtime(root / "dist" / "index.js", 1_000)
    sdk_select.build_javascript(
        Referee(ClientConfig()),  # type: ignore[arg-type]
        MATRIX_ENV,
        js_root=root,
        results_root=tmp_path,
    )
    assert len(recorded.made) == 1


def test_a_missing_build_is_refused(recorded: type[Recorded], tmp_path: Path) -> None:
    root = _fresh_js(tmp_path)
    (root / "dist" / "index.js").unlink()
    with pytest.raises(safety.Refusal, match="not built"):
        sdk_select.build_javascript(
            Referee(ClientConfig()),  # type: ignore[arg-type]
            MATRIX_ENV,
            js_root=root,
            results_root=tmp_path,
        )
    assert recorded.made == []


def test_a_new_source_in_a_subfolder_counts(recorded: type[Recorded], tmp_path: Path) -> None:
    root = _fresh_js(tmp_path)
    deep = root / "src" / "x" / "y"
    deep.mkdir(parents=True)
    (deep / "z.ts").write_text("export {};\n", encoding="utf-8")
    _set_mtime(deep / "z.ts", 5_000)
    with pytest.raises(safety.Refusal, match="z.ts"):
        sdk_select.build_javascript(
            Referee(ClientConfig()),  # type: ignore[arg-type]
            MATRIX_ENV,
            js_root=root,
            results_root=tmp_path,
        )


def test_the_refusal_skips_the_session_before_a_run_folder_exists(tmp_path: Path) -> None:
    root = _fresh_js(tmp_path)
    _set_mtime(root / "src" / "index.ts", 3_000)

    def factory() -> Any:
        return sdk_select.build_javascript(
            Referee(ClientConfig()),  # type: ignore[arg-type]
            MATRIX_ENV,
            js_root=root,
            results_root=tmp_path,
        )

    runs = tmp_path / "runs"
    with pytest.raises(safety.Refusal, match="older than"):
        _session(MatrixClient(), runs, sdk_name="javascript", factory=factory)
    assert not runs.exists() or list(runs.glob("*/ledger.jsonl")) == []  # nothing was started


# -- Task 11 carry: a second SDK is registered ----------------------------------------------------


def test_family_m9_is_skipped_with_a_reason_for_the_javascript_sdk() -> None:
    results = m9.results._get_wrapped_function()  # type: ignore[attr-defined]
    matrix = SimpleNamespace(sdk=SimpleNamespace(name="javascript"), uat=True, mock=False)
    with pytest.raises(pytest.skip.Exception) as caught:
        results(SimpleNamespace(), matrix)
    assert "M9" in str(caught.value) and "Python-only" in str(caught.value)
    assert "javascript" in str(caught.value)


class FakeMetafunc:
    def __init__(self, fixturenames: list[str]) -> None:
        self.fixturenames = fixturenames
        self.calls: list[tuple[Any, ...]] = []

    def parametrize(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


@pytest.mark.parametrize(
    ("value", "names"),
    [
        ("python,javascript", ["python", "javascript"]),
        ("all", ["python", "javascript"]),
        (" ALL ", ["python", "javascript"]),
        ("javascript,python", ["javascript", "python"]),
    ],
)
def test_one_serial_session_per_sdk_in_the_order_given(
    monkeypatch: pytest.MonkeyPatch, value: str, names: list[str]
) -> None:
    monkeypatch.setattr(matrix_conftest.os, "environ", {"TRADESMART_LIVE_SDKS": value})
    metafunc = FakeMetafunc(["matrix", "case"])
    matrix_conftest.pytest_generate_tests(metafunc)  # type: ignore[arg-type]
    ((args, kwargs),) = metafunc.calls
    assert args == ("matrix", names)
    # A session-scoped, indirect parameter: every item runs for one SDK, then for the next;
    # the SDK name is in the node id (``[python]``, ``[javascript]``).
    assert kwargs == {"indirect": True, "ids": names, "scope": "session"}


@pytest.mark.parametrize("value", [None, "", "python", "javascript"])
def test_a_single_sdk_adds_no_parameter_so_node_ids_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    environ = {} if value is None else {"TRADESMART_LIVE_SDKS": value}
    monkeypatch.setattr(matrix_conftest.os, "environ", environ)
    metafunc = FakeMetafunc(["matrix"])
    matrix_conftest.pytest_generate_tests(metafunc)  # type: ignore[arg-type]
    assert metafunc.calls == []


def test_each_sdk_has_its_own_run_directory_and_tag_code(tmp_path: Path) -> None:
    group = RunGroup(tuple(sdk_select.selected_sdks({"TRADESMART_LIVE_SDKS": "all"})))
    root = tmp_path / "runs"
    python = _session(MatrixClient(), root, group=group)
    try:
        session = _session(
            MatrixClient(), root, sdk_name="javascript", group=group, factory=lambda: FakeSdk()
        )
    except BaseException:
        gate.release_run_lock(python)
        raise
    try:
        assert python.run_id == group.base_id and session.run_id == f"{group.base_id}j"
        assert python.run_dir != session.run_dir
        assert python.run_dir.is_dir() and session.run_dir.is_dir()
        assert (python.ledger.sdk_code, session.ledger.sdk_code) == ("p", "j")
        assert (python.sdk.code, session.sdk.code) == ("p", "j")
        for one in (python, session):
            one.guard.reserve(NSE.case_id, 1)
        tags = [calls.place(one, PLAN, NSE).tag for one in (python, session)]
        assert re.fullmatch(rf"sdkuat-{python.run_id}-p[0-9a-z]{{3}}-\d+", tags[0])
        assert re.fullmatch(rf"sdkuat-{session.run_id}-j[0-9a-z]{{3}}-\d+", tags[1])
    finally:
        gate.release_run_lock(python)
        gate.release_run_lock(session)


def test_the_later_sdk_is_stopped_when_the_earlier_one_raises_check_the_account(
    tmp_path: Path,
) -> None:
    names = sdk_select.selected_sdks({"TRADESMART_LIVE_SDKS": "python,javascript"})
    group = RunGroup(tuple(names))
    root = tmp_path / "runs"
    first = _session(MatrixClient(), root, group=group)
    first.ledger.mark_unknown("sdkuat-x-p000-1", ["1001"])  # an unknown outcome in the Python run
    matrix_conftest.end_session(first, group)
    assert group.check_the_account is not None
    started: list[int] = []

    def factory() -> Any:
        started.append(1)
        return FakeSdk()

    with pytest.raises(safety.Refusal, match=safety.CHECK_THE_ACCOUNT):
        _session(MatrixClient(), root, sdk_name="javascript", group=group, factory=factory)
    assert started == []  # the JavaScript driver never starts


# -- end to end: the real driver program on the offline broker ------------------------------------


@pytest.fixture
def offline_js(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., Any]:
    """A factory of sessions whose JavaScript SDK is the real driver program on the offline
    broker (``tests/helpers/offline-driver.ts``), started through the registered factory."""
    if NODE is None:
        pytest.skip("node is not installed here (the live-sdks image has it)")
    stale = sdk_select.dist_refusal(JS_ROOT)
    if stale is not None:
        pytest.skip(f"the JavaScript build is not usable here: {stale}")
    monkeypatch.setattr(sdk_select, "JS_COMMAND", (NODE, str(OFFLINE_DRIVER)))
    monkeypatch.setattr(sdk_select, "RESULTS_ROOT", tmp_path / "results")
    made: list[DriverSdk] = []

    def open_session(
        client: MatrixClient, extra: dict[str, str] | None = None, *, group: RunGroup | None = None
    ) -> gate.MatrixSession:
        environ = {**MATRIX_ENV, **(extra or {})}

        def factory() -> Any:
            sdk = sdk_select.build_sdk("javascript", client, environ)  # type: ignore[arg-type]
            made.append(sdk)  # type: ignore[arg-type]
            return sdk

        return _session(
            client, tmp_path / "runs", environ=environ, sdk_name="javascript", factory=factory,
            group=group,
        )  # fmt: skip

    yield open_session
    for sdk in made:
        sdk.close()


def test_a_place_and_a_cancel_go_through_the_real_driver_and_its_wire_bodies(
    offline_js: Callable[..., gate.MatrixSession],
) -> None:
    client = MatrixClient()
    session = offline_js(client)
    try:
        assert session.sdk.name == "javascript" and session.sdk.code == "j"
        assert session.sdk.client_id.upper() == ACCOUNT
        session.guard.reserve(NSE.case_id, 1)
        placed = calls.place(session, PLAN, NSE)
        assert placed.verdict == calls.ACKNOWLEDGED
        assert placed.order_id is not None and placed.order_id.strip()
        # The id is the one in the wire body, which Python parsed itself (not the driver's say-so).
        assert session.sdk.findings == []  # type: ignore[attr-defined]
        (line,) = [x for x in _ledger(session) if x.get("type") == "outcome"]
        response = line["detail"]["response"]
        assert response["http_status"] == 200
        assert response["body"]["norenordno"] == placed.order_id
        assert "uid" not in response["body"] and "actid" not in response["body"]
        cancelled = calls.cancel(session, placed.order_id, NSE)
        assert cancelled.verdict == calls.ACKNOWLEDGED
        assert client.count("place_order") == 0 and client.count("cancel_order") == 0
        hello = [x for x in _ledger(session) if x.get("type") == "driver_hello"]
        assert hello and hello[0]["sdk"] == "javascript" and hello[0]["protocol"] == 2
    finally:
        session.sdk.close()
        gate.release_run_lock(session)


def _ledger(session: gate.MatrixSession) -> list[dict[str, Any]]:
    import json

    return [
        json.loads(x) for x in session.ledger.path.read_text(encoding="utf-8").splitlines() if x
    ]


def test_a_rejection_through_the_real_driver_is_a_definite_answer_and_the_driver_goes_on(
    offline_js: Callable[..., gate.MatrixSession],
) -> None:
    session = offline_js(MatrixClient(), {"OFFLINE_DRIVER_DRILL": "PlaceOrder:reject"})
    try:
        session.guard.reserve(NSE.case_id, 2)
        rejected = calls.place(session, PLAN, NSE)
        assert rejected.verdict == calls.REJECTED
        assert not session.guard.stopped()
        placed = calls.place(session, PLAN, NSE)  # the drill fires once: the driver still runs
        assert placed.verdict == calls.ACKNOWLEDGED
    finally:
        session.sdk.close()
        gate.release_run_lock(session)


def test_an_ambiguous_write_through_the_real_driver_is_check_the_account(
    offline_js: Callable[..., gate.MatrixSession],
) -> None:
    session = offline_js(MatrixClient(), {"OFFLINE_DRIVER_DRILL": "PlaceOrder:timeoutAfterSend"})
    try:
        session.guard.reserve(NSE.case_id, 1)
        outcome = calls.place(session, PLAN, NSE)
        # The SDK's own OrderStatusUnknownError crossed the protocol as itself; the referee's book
        # (MatrixClient: empty) does not show the order, so the matrix says CHECK THE ACCOUNT.
        assert outcome.check_the_account
    finally:
        session.sdk.close()
        gate.release_run_lock(session)


def test_a_modify_through_the_real_driver(offline_js: Callable[..., gate.MatrixSession]) -> None:
    session = offline_js(MatrixClient())
    try:
        session.guard.reserve(NSE.case_id, 1)
        placed = calls.place(session, PLAN, NSE)
        assert placed.order_id is not None
        modified = calls.modify(
            session, placed.order_id, NSE, exchange="NSE", tradingsymbol="ITC-EQ",
            order_type="LIMIT", product="CNC", price=Decimal("361.00"), quantity=1,
        )  # fmt: skip
        assert modified.verdict == calls.ACKNOWLEDGED
        assert modified.order_id == placed.order_id
    finally:
        session.sdk.close()
        gate.release_run_lock(session)


def test_the_driver_is_stopped_with_its_session_and_leaves_no_process(
    offline_js: Callable[..., gate.MatrixSession],
) -> None:
    session = offline_js(MatrixClient())
    sdk = session.sdk
    try:
        assert sdk.process.poll() is None  # type: ignore[attr-defined]
    finally:
        sdk.close()
        gate.release_run_lock(session)
    assert sdk.process.poll() is not None  # type: ignore[attr-defined]


def test_the_driver_refuses_to_start_by_hand(tmp_path: Path) -> None:
    if NODE is None:
        pytest.skip("node is not installed here (the live-sdks image has it)")
    import subprocess

    done = subprocess.run(  # noqa: S603 - a fixed command in a fake empty environment
        [NODE, str(JS_ROOT / "tests" / "live_orders" / "driver.ts")],
        env={},
        input=b"",
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 2 and done.stdout == b""
    assert b"refusing to start" in done.stderr


SLICED = {
    "exchange": "NFO",
    "tradingsymbol": "NIFTY27NOV27C24000",
    "quantity": 3600,
    "transaction_type": "BUY",
    "product": "NRML",
    "order_type": "LIMIT",
    "price": Decimal("100.00"),
    "tag": "sdkuat-e2e-j000-1",
}


def test_a_sliced_order_through_the_real_driver_is_accepted_slice_by_slice(
    offline_js: Callable[..., gate.MatrixSession],
) -> None:
    session = offline_js(MatrixClient())
    try:
        result = session.sdk.place_sliced_order(**SLICED, max_slices=5, max_slice_quantity=1800)
        assert result.complete and result.error is None
        assert result.slice_quantities == (1800, 1800)  # the plan's shape, inside the budget
        assert len(result.order_ids) == 2 and len(set(result.order_ids)) == 2
        assert result.placed_quantity == 3600 and result.unsent_quantity == 0
        assert session.sdk.findings == []  # type: ignore[attr-defined]
        assert not session.guard.stopped()
    finally:
        session.sdk.close()
        gate.release_run_lock(session)


def test_a_sliced_order_whose_first_slice_is_rejected_comes_back_incomplete_with_its_error(
    offline_js: Callable[..., gate.MatrixSession],
) -> None:
    session = offline_js(MatrixClient(), {"OFFLINE_DRIVER_DRILL": "PlaceOrder:reject"})
    try:
        result = session.sdk.place_sliced_order(**SLICED, max_slices=5, max_slice_quantity=1800)
        assert not result.complete and result.order_ids == ()
        assert result.placed_quantity == 0 and result.unsent_quantity == 3600
        assert type(result.error).__name__ == "OrderRejectedError"
    finally:
        session.sdk.close()
        gate.release_run_lock(session)


# -- fix round 1 (I1): every one of the eight writes through the real process and Python's parse ---

GTT_KWARGS = {
    "exchange": "NSE",
    "tradingsymbol": "ITC-EQ",
    "alert_type": "LTP_B_O",
    "trigger_value": Decimal("300"),
    "quantity": 1,
    "transaction_type": "BUY",
    "product": "CNC",
    "order_type": "LIMIT",
    "tag": "sdkuat-e2e-j000-gtt",
    "price": Decimal("300"),
    "validity": "DAY",
    "alert_validity": "GTT",
}
COVER = {**SLICED, "quantity": 75, "product": "CO", "book_loss_price": Decimal("95.00")}


def test_all_eight_writes_go_through_the_real_driver_and_pythons_parse(
    offline_js: Callable[..., gate.MatrixSession],
) -> None:
    session = offline_js(MatrixClient())
    sdk: Any = session.sdk
    try:
        plain = sdk.place_order(**{**PLAN, "tag": "sdkuat-e2e-j000-2"})
        assert isinstance(plain, str) and plain.strip()
        modified = sdk.modify_order(
            plain, exchange="NSE", tradingsymbol="ITC-EQ", order_type="LIMIT",
            product="CNC", price=Decimal("361.00"), quantity=1,
        )  # fmt: skip
        assert modified == plain
        assert sdk.cancel_order(plain) == plain
        sliced = sdk.place_sliced_order(**SLICED, max_slices=5, max_slice_quantity=1800)
        assert sliced.complete and len(sliced.order_ids) == 2
        cover = sdk.place_order(**{**COVER, "tag": "sdkuat-e2e-j000-3"})
        assert sdk.exit_order(cover, "CO") == cover
        alert = sdk.place_gtt(**GTT_KWARGS)
        assert isinstance(alert, str) and alert.strip()
        changed = {**GTT_KWARGS, "trigger_value": Decimal("290"), "price": Decimal("290")}
        assert sdk.modify_gtt(alert, **changed) == alert
        assert sdk.cancel_gtt(alert) == alert
        assert sdk.findings == []  # no driver_finding: every id equals Python's own parse
        assert sdk.process.poll() is None  # the driver is still alive
        assert not session.guard.stopped()
    finally:
        sdk.close()
        gate.release_run_lock(session)


@pytest.mark.parametrize(
    ("drill", "send"),
    [
        ("PlaceOrder:reject", lambda s: s.place_order(**{**PLAN, "tag": "sdkuat-e2e-j000-4"})),
        ("PlaceGTTOrder:reject", lambda s: s.place_gtt(**GTT_KWARGS)),
    ],
)
def test_a_rejection_reaches_the_harness_as_the_python_error_with_its_raw_body(
    offline_js: Callable[..., gate.MatrixSession], drill: str, send: Callable[[Any], Any]
) -> None:
    from tradesmart import OrderRejectedError

    session = offline_js(MatrixClient(), {"OFFLINE_DRIVER_DRILL": drill})
    sdk: Any = session.sdk
    try:
        with pytest.raises(OrderRejectedError) as caught:
            send(sdk)
        assert isinstance(caught.value.raw, dict) and caught.value.raw.get("stat") == "Not_Ok"
        assert caught.value.emsg
        assert sdk.process.poll() is None  # a definite answer: the driver still runs
        assert not session.guard.stopped()
    finally:
        sdk.close()
        gate.release_run_lock(session)


# -- fix round 1 (M1): a file that cannot be examined is a refusal, never a traceback ------------


@pytest.mark.parametrize("victim", ["a.ts", "index.js"])
def test_a_file_that_cannot_be_examined_is_a_refusal(
    recorded: type[Recorded], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, victim: str
) -> None:
    root = _fresh_js(tmp_path)
    real_stat = Path.stat

    def stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == victim:
            raise PermissionError("synthetic")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    with pytest.raises(safety.Refusal, match="cannot be examined"):
        sdk_select.build_javascript(
            Referee(ClientConfig()),  # type: ignore[arg-type]
            MATRIX_ENV,
            js_root=root,
            results_root=tmp_path,
        )
    assert recorded.made == []


def test_the_protocol_document_lists_an_unknown_method_as_a_violation() -> None:
    text = " ".join((REPO / "specifications" / "live-driver.md").read_text("utf-8").split())
    assert "an unknown method (the driver exits 1 with no reply)" in text


def test_the_image_is_pinned_to_exact_versions() -> None:

    # The maintainers' tooling folder is not part of a public export: skip where it is absent.
    tools = REPO / ("inter" + "nal") / "tools" / "live-sdks"
    if not tools.is_dir():
        pytest.skip("the live-testing image files are not in this tree")
    dockerfile = (tools / "Dockerfile").read_text("utf-8")
    compose = (tools / "compose.yaml").read_text("utf-8")
    for arg in ("PYTHON_VERSION", "NODE_VERSION", "LIBATOMIC_VERSION"):
        assert re.search(rf"^ARG {arg}=\d+\.\d+[.\-]\d+", dockerfile, re.M), arg
        assert re.search(rf'{arg}: "\d+\.\d+[.\-]\d+', compose), arg
    assert "libatomic1=${LIBATOMIC_VERSION}" in dockerfile


OCO_KWARGS = {
    "exchange": "NSE",
    "tradingsymbol": "ITC-EQ",
    "transaction_type": "SELL",
    "quantity": 1,
    "product": "CNC",
    "stop_loss": {"trigger_value": Decimal("95"), "price": Decimal("94.5"), "order_type": "LIMIT"},
    "target": {"trigger_value": Decimal("105"), "price": Decimal("105"), "order_type": "LIMIT"},
    "validity": "DAY",
    "alert_validity": "GTT",
    "tag": "sdkuat-e2e-j000-oco",
}


def test_the_three_oco_calls_go_through_the_real_driver_and_pythons_parse(
    offline_js: Callable[..., gate.MatrixSession],
) -> None:
    session = offline_js(MatrixClient())
    sdk: Any = session.sdk
    try:
        alert = sdk.place_oco(**OCO_KWARGS)
        assert isinstance(alert, str) and alert.strip()
        changed = {
            **OCO_KWARGS,
            "stop_loss": {**OCO_KWARGS["stop_loss"], "trigger_value": Decimal("96")},  # type: ignore[dict-item]
        }
        assert sdk.modify_oco(alert, **changed) == alert
        assert sdk.cancel_oco(alert) == alert
        assert sdk.findings == []  # no driver_finding: every id equals Python's own parse
        assert sdk.process.poll() is None
        assert not session.guard.stopped()
    finally:
        sdk.close()
        gate.release_run_lock(session)


def test_oco_legs_as_the_matrix_builds_them_go_through_the_real_driver(
    offline_js: Callable[..., gate.MatrixSession],
) -> None:
    """The matrix's OCO cases pass ``OcoLeg`` models (``cases_oco``), not dicts: they reach the
    real driver as the protocol's leg objects and are accepted (final review I1)."""
    session = offline_js(MatrixClient())
    sdk: Any = session.sdk
    legs = {
        "stop_loss": OcoLeg(trigger_value=Decimal("95"), price=Decimal("94.5")),
        "target": OcoLeg(
            trigger_value=Decimal("105"),
            price=Decimal("104"),
            order_type=OrderType.SL,
            trigger_price=Decimal("105"),
        ),
    }
    try:
        alert = sdk.place_oco(**{**OCO_KWARGS, **legs, "alert_validity": "DAY"})
        assert isinstance(alert, str) and alert.strip()
        assert sdk.cancel_oco(alert) == alert
        assert sdk.findings == [] and sdk.process.poll() is None
    finally:
        sdk.close()
        gate.release_run_lock(session)
