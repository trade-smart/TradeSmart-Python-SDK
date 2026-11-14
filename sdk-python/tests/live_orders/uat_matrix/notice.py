"""The end-of-session CHECK THE ACCOUNT notice of the UAT matrix (read-only: never a call).

``leftover_notice(session, report)`` builds one notice from the teardown's ``CancelReport``
(``calls.teardown``) and the ledger whenever anything of the run may still be open or has an
unknown outcome: orders not final after their cancel, stuck, flagged (unknown outcome), with a
call in flight, an unreadable order book, orders of ours the book never listed, tags with an
unknown outcome, and GTTs left pending, flagged or not readable. After Ctrl-C, or a teardown
that did not finish (``report`` ``None``), it lists every order, unknown tag and GTT of the run.
``None`` when the session ended clean. ``conftest.end_session`` shows it (with the open-AMO
notice, ``cases_amo.open_amo_notice``) in the terminal summary.
"""

from __future__ import annotations

from collections.abc import Iterable

from tests.live_orders.safety import CHECK_THE_ACCOUNT
from tests.live_orders.uat_matrix.calls import CancelReport
from tests.live_orders.uat_matrix.gate import MatrixSession


def _ids(values: Iterable[str]) -> str:
    return ", ".join(sorted(dict.fromkeys(values)))


def _parts(session: MatrixSession, report: CancelReport | None) -> list[str]:
    ledger = session.ledger
    parts: list[str] = []
    if report is None:
        why = (
            "the run was interrupted (Ctrl-C): nothing was cancelled"
            if session.interrupted
            else "the session teardown did not finish: nothing confirmed final"
        )
        if ledger.ours():
            parts.append(f"{why}; orders of the run: {_ids(ledger.ours())}")
        elif ledger.tags() or ledger.alerts():
            parts.append(why)
        if ledger.alerts():
            parts.append(f"GTTs of the run: {_ids(ledger.alerts())}")
    else:
        if report.book_unreadable:
            listed = _ids(ledger.ours()) or "none acknowledged"
            parts.append(f"the order book could not be read at the end; orders of the run: "
                         f"{listed}")  # fmt: skip
        for ids, what in (
            (report.not_final, "not final after their cancel"),
            (report.stuck, "stuck (not final after their cancel earlier)"),
            (report.flagged, "with an unknown outcome (no call was made for them)"),
            (report.pending, "with a call in flight at the end"),
            (report.missing, "acknowledged but never listed in the order book"),
        ):
            if ids:
                parts.append(f"orders {what}: {_ids(ids)}")
        gtts = report.gtts
        if gtts is not None:
            if gtts.unreadable:
                parts.append("pending_gtts() could not be read at the end")
            if gtts.left:
                parts.append(f"GTTs still pending: {_ids(gtts.left)}")
            if gtts.flagged:
                parts.append(f"GTTs with an unknown outcome: {_ids(gtts.flagged)}")
    tags = ledger.flagged_tags()
    if tags:
        parts.append(f"tags with an unknown outcome: {_ids(tags)}")
    return parts


def leftover_notice(session: MatrixSession, report: CancelReport | None) -> str | None:
    """The run's end-of-session notice (see the module doc), else ``None``. Never a call."""
    parts = _parts(session, report)
    if not parts:
        return None
    return (
        f"{CHECK_THE_ACCOUNT} (run {session.run_id}): " + "; ".join(parts) + f" - see "
        f"{session.ledger.path} and run the cleanup tool"
    )
