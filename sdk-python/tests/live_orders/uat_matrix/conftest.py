"""Gate, session and teardown of the UAT matrix package (see ``gate.py`` for the rules).

- Collection: every item of this package whose tier gate fails is skipped with the reason (the
  parent conftest already skips everything without the order opt-ins). A production or funded
  environment skips every matrix item here, except production under a valid mock-session switch
  (``gate.collection_refusal``, on the base URL the client will be built with).
- ``matrix`` (session): the run's ``MatrixSession``; at the end every open order of this run is
  cancelled once and confirmed (``calls.teardown``); nothing else in the account is touched. After
  Ctrl-C nothing is cancelled: the ledger lists ours for the cleanup tool.
- Each item first checks the run is not stopped (STOP file, a foreign open order) and its tier.
- At the end (``end_session``): one CHECK THE ACCOUNT notice in the terminal summary, from the
  teardown report and the ledger only (never a call), when anything of the run may still be open
  or has an unknown outcome (``notice.leftover_notice``: not final, stuck, flagged, a call in
  flight, an unreadable book, ours never listed, unknown tags, GTTs left), with the run's
  after-market orders as its last line (``cases_amo.open_amo_notice``): an open AMO reaches the
  exchange at the open.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests._environment import environment_client
from tests.live_orders.safety import CHECK_THE_ACCOUNT, Refusal
from tests.live_orders.uat_matrix import calls, cases_amo, gate, notice

# Belt: the families that keep their cases outside ``cases.CASES`` register them on import, so
# every session knows them whatever modules it collects (``batch._restore`` itself reads the
# market fill and the RMS limits from every ledger verdict, whatever the case).
from tests.live_orders.uat_matrix import cases_cobo, cases_hold  # noqa: F401  # isort: skip
from tests.live_orders.uat_matrix.guard import StopRun

# The open matrix sessions of this process (one in practice): marked on Ctrl-C.
SESSIONS: list[gate.MatrixSession] = []
# End-of-session notices (open AMO orders), shown in the terminal summary.
NOTICES: list[str] = []


def _markexpr(config: Any) -> str:
    option = getattr(config, "option", None)
    return str(getattr(option, "markexpr", "") or "")


def _deselect_unless_selected(config: Any, items: list[pytest.Item], markexpr: str) -> None:
    """Matrix items are deselected (not skipped) unless the run asks for the matrix explicitly
    (``gate.explicitly_selected``: ``-m`` naming ``uat_matrix`` positively, or a path inside
    ``tests/live_orders/uat_matrix``), so ``pytest tests/live_orders -m live_orders`` (the
    older UAT order suites) never places the matrix's orders."""
    args = [str(a) for a in getattr(config, "args", None) or []]
    params = getattr(config, "invocation_params", None)
    where = Path(getattr(params, "dir", None) or Path.cwd())
    if gate.explicitly_selected(markexpr, args, where):
        return
    dropped = [item for item in items if gate.is_matrix_item(item.nodeid)]
    if dropped:
        items[:] = [item for item in items if not gate.is_matrix_item(item.nodeid)]
        config.hook.pytest_deselected(items=dropped)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # This hook sees every collected item, so it filters to the matrix package itself.
    markexpr = _markexpr(config)
    _deselect_unless_selected(config, items, markexpr)
    for item in items:
        if not gate.is_matrix_item(item.nodeid):
            continue
        names = [marker.name for marker in item.iter_markers()]
        tier = gate.tier_of(names)
        reason = gate.collection_refusal(os.environ, tier, markexpr=markexpr)
        if reason is None:  # mock session: the AMO and GTT families never run
            reason = gate.mock_family_refusal(os.environ, names)
        if reason is not None:
            item.add_marker(pytest.mark.skip(reason=reason))


def pytest_keyboard_interrupt(excinfo: pytest.ExceptionInfo[BaseException]) -> None:
    """Ctrl-C: called before the session fixtures are torn down; their teardown then sends
    no cancel (inventory §4.4: skipped after Ctrl-C; the cleanup tool covers it)."""
    for session in SESSIONS:
        session.interrupted = True


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """The end-of-session notices (read-only)."""
    for shown in NOTICES:
        terminalreporter.write_line(shown, red=True, bold=True)


def end_session(session: gate.MatrixSession) -> calls.CancelReport | None:
    """The session's teardown (``calls.teardown``), then, whatever it did, the end-of-session
    CHECK THE ACCOUNT notice when one is due (``_notice``: the report and the ledger, never a
    call); last, the run lock is removed (``gate.release_run_lock``)."""
    report: calls.CancelReport | None = None
    try:
        report = calls.teardown(session)
    finally:
        _notice(session, report)  # never raises: a teardown's own exception stays the one raised
        with contextlib.suppress(OSError):
            gate.release_run_lock(session)
    return report


def _notice(session: gate.MatrixSession, report: calls.CancelReport | None) -> None:
    """One end-of-session notice: the general CHECK THE ACCOUNT notice (``notice``) and the
    open-AMO notice as its last line (an open AMO reaches the exchange at the open)."""
    try:
        general = notice.leftover_notice(session, report)
    except Exception as exc:  # recorded instead; it must not hide the teardown's own error
        general = (
            f"{CHECK_THE_ACCOUNT} (run {session.run_id}): the end-of-session notice could not be "
            f"built ({type(exc).__name__}: {exc}); check every order and GTT of the run by hand"
        )
    try:
        amo = cases_amo.open_amo_notice(session, report)
    except Exception as exc:  # recorded instead; it must not hide the teardown's own error
        amo = (
            f"{cases_amo.NOTICE} (run {session.run_id}): the open-AMO notice could not be built "
            f"({type(exc).__name__}: {exc}); check every AMO of the run by hand"
        )
    if amo is not None:
        with contextlib.suppress(Exception):  # the terminal summary still shows it
            session.ledger.note("amo_open_notice", notice=amo)
    shown = "\n".join(line for line in (general, amo) if line is not None)
    if shown:
        NOTICES.append(shown)
        with contextlib.suppress(Exception):  # the terminal summary still shows it
            session.ledger.note("session_notice", notice=shown)


def _attach_response_log(session: gate.MatrixSession) -> bool:
    """Record raw order-endpoint bodies (test-only: the SDK has no public hook for this)."""
    transport: Any = getattr(session.client, "_transport", None)
    http_client = getattr(transport, "http_client", None)
    if http_client is None:
        return False
    session.responses.attach(http_client)
    return True


@pytest.fixture(scope="session")
def matrix() -> Iterator[gate.MatrixSession]:
    reason = gate.collection_refusal(os.environ, gate.SMOKE)
    if reason is not None:  # belt and braces: the collection hook already skipped
        pytest.skip(reason)
    client = environment_client(os.environ)
    session: gate.MatrixSession | None = None
    try:
        try:
            session = gate.matrix_session(os.environ, client)
        except (Refusal, StopRun) as exc:
            pytest.skip(exc.reason)
        SESSIONS.append(session)
        session.raw_bodies = _attach_response_log(session)
        yield session
    finally:
        try:
            if session is not None:
                end_session(session)
        finally:
            if session is not None and session in SESSIONS:
                SESSIONS.remove(session)
            client.close()


@pytest.fixture(autouse=True)
def _matrix_item_gate(request: pytest.FixtureRequest, matrix: gate.MatrixSession) -> None:
    """Skip an item when the run is stopped or its tier is not opted in."""
    tier = gate.tier_of(marker.name for marker in request.node.iter_markers())
    reason = matrix.item_refusal(tier, markexpr=_markexpr(request.config))
    if reason is not None:
        pytest.skip(reason)
