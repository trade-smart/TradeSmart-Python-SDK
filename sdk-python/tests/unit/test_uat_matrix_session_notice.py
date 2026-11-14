"""The end-of-session CHECK THE ACCOUNT notice (final review I3): one notice, built from the
teardown report and the ledger only (never a call), whenever something of the run may still be
open or has an unknown outcome; the open-AMO notice is part of it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.live_orders.safety import CHECK_THE_ACCOUNT
from tests.live_orders.uat_matrix import calls, cases_amo, notice
from tests.live_orders.uat_matrix import conftest as matrix_conftest
from tests.live_orders.uat_matrix.calls import CancelReport, CaseRef, GttCleanup
from tests.unit.test_uat_matrix_safety import PLAN, MatrixClient, _order, _session

NSE = CaseRef("shapes-nse-b-cnc-lmt-day-buy", "nse")
AMO = CaseRef("amo-nse-b-cnc-lmt-day-buy", "nse")


class _Reporter:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_line(self, line: str, **markup: Any) -> None:
        self.lines.append(line)


def _placed(session: Any, case: CaseRef = NSE) -> str:
    session.guard.reserve(case.case_id, 1)
    outcome = calls.place(session, PLAN, case)
    assert outcome.order_id is not None
    return outcome.order_id


def test_a_clean_report_gives_no_notice(tmp_path: Path) -> None:
    session = _session(MatrixClient(), tmp_path)
    assert notice.leftover_notice(session, CancelReport()) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("not_final", ["7001"]),
        ("flagged", ["7002"]),
        ("stuck", ["7003"]),
        ("pending", ["7004"]),
        ("missing", ["7005"]),
    ],
)
def test_each_leftover_kind_is_in_the_notice(tmp_path: Path, field: str, value: list[str]) -> None:
    session = _session(MatrixClient(), tmp_path)
    report = CancelReport(**{field: value})
    text = notice.leftover_notice(session, report)
    assert text is not None and CHECK_THE_ACCOUNT in text and value[0] in text
    assert session.run_id in text


def test_gtts_left_flagged_or_unreadable_are_in_the_notice(tmp_path: Path) -> None:
    session = _session(MatrixClient(), tmp_path)
    left = notice.leftover_notice(session, CancelReport(gtts=GttCleanup(left=["A1"])))
    assert left is not None and "A1" in left
    flagged = notice.leftover_notice(session, CancelReport(gtts=GttCleanup(flagged=["A2"])))
    assert flagged is not None and "A2" in flagged
    unreadable = notice.leftover_notice(session, CancelReport(gtts=GttCleanup(unreadable=True)))
    assert unreadable is not None and "pending_gtts" in unreadable


def test_an_unreadable_book_lists_every_order_of_the_run(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    order_id = _placed(session)
    text = notice.leftover_notice(session, CancelReport(book_unreadable=True))
    assert text is not None and "could not be read" in text and order_id in text


def test_after_ctrl_c_every_order_and_unknown_tag_of_the_run_is_listed(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    order_id = _placed(session)
    session.interrupted = True
    text = notice.leftover_notice(session, None)
    assert text is not None and "interrupted" in text and order_id in text
    assert "cleanup tool" in text


def test_an_order_of_ours_never_in_the_book_is_missing_and_in_the_notice(tmp_path: Path) -> None:
    client = MatrixClient(books=[[]])  # the place answers 1001; the book never lists it
    session = _session(client, tmp_path)
    order_id = _placed(session)
    report = calls.teardown(session)
    assert report is not None and report.missing == [order_id]
    text = notice.leftover_notice(session, report)
    assert text is not None and order_id in text and "never" in text


def test_a_tag_with_an_unknown_outcome_is_in_the_notice(tmp_path: Path) -> None:
    session = _session(MatrixClient(), tmp_path)
    tag = session.ledger.new_tag(NSE.case_id)
    session.ledger.mark_unknown(tag)
    text = notice.leftover_notice(session, CancelReport())
    assert text is not None and tag in text


def test_the_notice_is_read_only(tmp_path: Path) -> None:
    client = MatrixClient(books=[[]])
    session = _session(client, tmp_path)
    _placed(session)
    report = calls.teardown(session)
    calls_before = list(client.calls)
    notice.leftover_notice(session, report)
    notice.leftover_notice(session, None)
    assert client.calls == calls_before  # not a single call, read-only or not


def test_end_session_shows_one_notice_with_the_amo_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(matrix_conftest, "NOTICES", [])
    client = MatrixClient()
    session = _session(client, tmp_path)
    regular = _placed(session)
    amo = _placed(session, AMO)
    session.interrupted = True
    assert matrix_conftest.end_session(session) is None
    (shown,) = matrix_conftest.NOTICES
    assert regular in shown and amo in shown
    assert cases_amo.NOTICE in shown  # the AMO wording is kept
    reporter = _Reporter()
    matrix_conftest.pytest_terminal_summary(reporter)
    assert any(CHECK_THE_ACCOUNT in line for line in reporter.lines)


def test_end_session_after_a_stuck_regular_order_shows_the_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(matrix_conftest, "NOTICES", [])
    client = MatrixClient()
    session = _session(client, tmp_path)
    order_id = _placed(session)
    tag = session.ledger.tag_of(order_id)
    client.books = [[_order(order_id, "OPEN", tag=tag or "")]]  # a cancel changes nothing
    report = matrix_conftest.end_session(session)
    assert report is not None and report.not_final == [order_id]
    (shown,) = matrix_conftest.NOTICES
    assert CHECK_THE_ACCOUNT in shown and order_id in shown


def test_a_clean_session_end_shows_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(matrix_conftest, "NOTICES", [])
    client = MatrixClient()
    session = _session(client, tmp_path)
    order_id = _placed(session)
    tag = session.ledger.tag_of(order_id)
    client.books = [[_order(order_id, "CANCELED", tag=tag or "")]]
    matrix_conftest.end_session(session)
    assert matrix_conftest.NOTICES == []


def test_an_amo_with_a_call_in_flight_at_the_end_is_in_the_amo_notice(tmp_path: Path) -> None:
    session = _session(MatrixClient(), tmp_path)
    amo = _placed(session, AMO)
    text = cases_amo.open_amo_notice(session, CancelReport(pending=[amo]))
    assert text is not None and amo in text
