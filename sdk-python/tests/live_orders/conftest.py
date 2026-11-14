"""Double opt-in gate and the session for the live order tests (see README.md)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from tests._environment import environment_client
from tests.live_orders import safety
from tradesmart import TradeSmart


def gate_reason() -> str | None:
    return safety.gate_skip_reason(os.environ)


# Funded stage 1 (the no-fill tests): a test in this folder whose name starts with STAGE1_PREFIX
# (parameter ids included, e.g. "test_stage1_rest_modify_cancel[nse]") is a stage-1 test.
# Stage 2 (fills) runs only if no stage-1 test of the run failed in setup, call or teardown.
STAGE1_PREFIX = "test_stage1_"

# Each phase's report ("setup", "call", "teardown") of a test in this folder, kept on its item.
REPORTS = pytest.StashKey[dict[str, pytest.TestReport]]()
# Node ids of the stage-1 tests of this run with a failed report (any phase), kept on the config.
STAGE1_FAILURES = pytest.StashKey[set[str]]()


def is_stage1_item(item: pytest.Item) -> bool:
    """Whether ``item`` is a funded stage-1 test: its name starts with ``STAGE1_PREFIX``."""
    return item.name.startswith(STAGE1_PREFIX)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    """Keep every phase's report on the item, and note a stage-1 test whose setup, call or
    teardown failed on the config (a skip is not a failure). A teardown error comes after the
    test's own fixtures have run, so only this hook sees it."""
    outcome = yield
    report: pytest.TestReport = outcome.get_result()
    item.stash.setdefault(REPORTS, {})[report.when] = report
    if report.failed and is_stage1_item(item):
        item.config.stash.setdefault(STAGE1_FAILURES, set()).add(item.nodeid)


def stage1_failures(config: pytest.Config) -> frozenset[str]:
    """The stage-1 tests of this run that failed so far (read when a stage-2 test starts and
    passed to ``funded.stage_two_reason``)."""
    return frozenset(config.stash.get(STAGE1_FAILURES, set()))


def stage_one_failed(item: pytest.Item) -> bool:
    """Whether the test failed in any phase reported so far (a skip is not a failure).

    Fails closed: with no setup report kept, or a setup that passed without a call report,
    the outcome cannot be told and counts as failed. Called from the test's own fixture
    teardown, it cannot see a later teardown error: the gate reads ``stage1_failures``.
    """
    reports = item.stash.get(REPORTS, {})
    if any(report.failed for report in reports.values()):
        return True
    setup = reports.get("setup")
    if setup is None:
        return True
    if setup.skipped:
        return False
    return reports.get("call") is None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # This hook sees every collected item, so it filters to tests/live_orders/ itself.
    reason = gate_reason()
    if reason is None:
        return
    marker = pytest.mark.skip(reason=reason)
    for item in items:
        if safety.is_live_orders_item(item.nodeid):
            item.add_marker(marker)


def _attach_response_log(client: TradeSmart, log: safety.ResponseLog) -> bool:
    """Record raw order-endpoint bodies (test-only: the SDK has no public hook for this)."""
    transport: Any = getattr(client, "_transport", None)
    http_client = getattr(transport, "http_client", None)
    if http_client is None:
        return False
    log.attach(http_client)
    return True


def funded_balance_refusal(session: safety.OrderSession) -> str | None:
    """Funded mode only: refuse unless ``margins()`` cash is at most the session's ceiling."""
    if not session.funded:
        return None
    if session.max_balance is None:
        return "funded mode without a balance ceiling; refusing to place orders"
    return safety.balance_refusal(session.client.margins(), session.max_balance)


def uat_preflight_refusal(session: safety.OrderSession) -> str | None:
    """UAT mode only: the UAT preflight (``safety.uat_preflight``: cash, turnover limit and
    pending-order value limit at the code minimum, read once from ``margins()``), recorded in
    the results file; the refusal when an item is unmet. Never read outside UAT mode."""
    if not session.uat or session.funded:
        return None
    check = safety.uat_preflight(session.client)
    session.recorder.write({"test": "session", "uat_preflight": check.record()})
    return check.refusal


@pytest.fixture(scope="session")
def order_session() -> Iterator[safety.OrderSession]:
    reason = gate_reason()
    if reason is not None:  # belt and braces: the collection hook already skipped
        pytest.skip(reason)
    client = environment_client(os.environ)
    try:
        try:
            session = safety.open_session(client, os.environ)
        except safety.Refusal as exc:
            safety.Recorder().write({"test": "session", "skipped": exc.reason})
            pytest.skip(exc.reason)
        session.raw_bodies = _attach_response_log(client, session.responses)
        refusal = safety.account_refusal(
            client.profile(), os.environ.get(safety.ACCOUNT_ENV), client.client_id
        )
        if refusal is not None:
            session.recorder.write({"test": "session", "skipped": refusal})
            pytest.skip(refusal)
        refusal = funded_balance_refusal(session)
        if refusal is not None:
            session.recorder.write({"test": "session", "skipped": refusal})
            pytest.skip(refusal)
        refusal = uat_preflight_refusal(session)
        if refusal is not None:
            session.recorder.write({"test": "session", "skipped": refusal})
            pytest.skip(refusal)
        yield session
    finally:
        client.close()


@pytest.fixture
def record(
    request: pytest.FixtureRequest, order_session: safety.OrderSession
) -> Iterator[dict[str, Any]]:
    """What the test observed; written as one JSON line when the test ends (pass or not)."""
    data: dict[str, Any] = {
        "test": request.node.name,
        "raw_bodies_recorded": order_session.raw_bodies,
        "uat_mode": order_session.uat,
    }
    yield data
    order_session.recorder.write(data)
