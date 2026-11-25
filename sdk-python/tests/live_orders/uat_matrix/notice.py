"""The end-of-session CHECK THE ACCOUNT notice of the UAT matrix (read-only: never a call).

``leftover_notice(session, report)`` builds one notice from the teardown's ``CancelReport``
(``calls.teardown``) and the ledger whenever anything of the run may still be open or has an
unknown outcome: orders not final after their cancel, stuck, flagged (unknown outcome), with a
call in flight, an unreadable order book, orders of ours the book never listed, tags with an
unknown outcome, GTTs left pending, flagged or not readable, and a driver process of the SDK
under test that failed during a write (``MatrixSession.sdk_stop``). After Ctrl-C, or a teardown
that did not finish (``report`` ``None``), it lists every order, unknown tag and GTT of the run.
``None`` when the session ended clean. ``conftest.end_session`` shows it (with the open-AMO
notice, ``cases_amo.open_amo_notice``) in the terminal summary.
"""

from __future__ import annotations

from collections.abc import Iterable

from tests.live_orders.safety import CHECK_THE_ACCOUNT
from tests.live_orders.uat_matrix.calls import CancelReport
from tests.live_orders.uat_matrix.gate import MatrixSession
from tests.live_orders.uat_matrix.positions import PositionLedger


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
            (
                report.not_sent,
                "whose cancel was not sent (the SDK's driver is stopped, so nothing more is "
                "sent through it; run uat_cleanup for them)",
            ),
            (report.pending, "with a call in flight at the end"),
            (report.missing, "acknowledged but never listed in the order book"),
            (report.tag_mismatch, "ours by id but not carrying the run's tag (not cancelled)"),
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
    stop = session.sdk_stop
    if stop is not None and CHECK_THE_ACCOUNT in stop:  # a driver failed during a write
        parts.append(f"the {session.sdk.name} SDK's run was stopped: {stop}")
    return parts


POSITIONS_NOTICE = "POSITIONS LEFT"


def positions_notice(session: MatrixSession) -> str | None:
    """The positions the run's fills left (``positions.PositionLedger.leftovers``), from the
    position ledger the run used: its own, or the group's shared one for another SDK's run
    (``MatrixSession.positions_path``); ``None`` when none are left. Leftovers are expected (a
    UAT closing SELL does not fill, U8; mock-session positions are wiped, F1), so this is a
    listing, never a CHECK THE ACCOUNT. Never a call."""
    path = session.positions_path
    if not path.exists():
        return None
    try:
        left = PositionLedger(path).leftovers()
    except Exception as exc:  # listed instead: the notice must never hide the teardown
        return (
            f"{POSITIONS_NOTICE} (run {session.run_id}): the position ledger {path} could not "
            f"be read ({type(exc).__name__}); check the account's positions by hand"
        )
    if not left:
        return None
    shown = ", ".join(f"{symbol} {quantity:+d}" for symbol, quantity in sorted(left.items()))
    return (
        f"{POSITIONS_NOTICE} (run {session.run_id}, positions of the group "
        f"{session.shared_dir.name}): {shown} - closed by hand or by the end-of-day square-off; "
        f"see {path}"
    )


def leftover_notice(session: MatrixSession, report: CancelReport | None) -> str | None:
    """The run's end-of-session notice (see the module doc), else ``None``. Never a call."""
    parts = _parts(session, report)
    if not parts:
        return None
    return (
        f"{CHECK_THE_ACCOUNT} (run {session.run_id}): " + "; ".join(parts) + f" - see "
        f"{session.ledger.path} and run the cleanup tool"
    )
