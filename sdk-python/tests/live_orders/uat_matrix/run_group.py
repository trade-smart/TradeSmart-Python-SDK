"""The SDKs of one matrix run: one after another, under one shared budget (design §9.1).

A pytest process runs the selected SDKs (``TRADESMART_LIVE_SDKS``) one after another. Each SDK
has its own run (``gate.sdk_run_id``: the group's run id for Python, the run id plus the SDK's
one-letter code for any other SDK), its own ledger and its own tag code. What they share is
held here, one ``RunGroup`` per process (``conftest``):

- the group's run id (``base_id``: the first session's, or ``TRADESMART_UAT_RUN_ID`` on a
  resume), so every SDK's run id derives from it;
- the order pacer (every state-changing call of every SDK, 6 in any rolling second: ALGO_CHK
  counts the user's calls, whatever SDK sends them) and the UAT quote pacer;
- the money limits that live in files: the per-instrument position ceiling (one
  ``positions.jsonl`` in the group's directory) and the run's single market path (every SDK
  reads the others' ledgers): ``gate.MatrixSession.positions_path`` / ``sibling_ledgers``;
- the STOP file (``<root>/STOP``, the same for every run already);
- **CHECK THE ACCOUNT stops all**: once any SDK of the group raised one (a driver failure on a
  write, a CHECK THE ACCOUNT trip, or an end-of-session notice), every later SDK is refused
  before its session opens (``refusal``). A tripped breaker or another stop stops only that
  SDK's run (its guard), never the next SDK.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

from tests.live_orders.safety import CHECK_THE_ACCOUNT
from tests.live_orders.uat_matrix.order_pacer import OrderPacer
from tests.live_orders.uat_matrix.quote_pacer import QuotePacer


class _Guard(Protocol):
    @property
    def trips(self) -> dict[str, str]: ...


class _Sdk(Protocol):
    @property
    def name(self) -> str: ...


class _Session(Protocol):
    @property
    def run_id(self) -> str: ...

    @property
    def guard(self) -> _Guard: ...

    @property
    def sdk(self) -> _Sdk: ...


@dataclass
class RunGroup:
    """What the SDKs of one process share (see the module docstring)."""

    names: tuple[str, ...]
    base_id: str | None = None
    check_the_account: str | None = None
    order_pacer: OrderPacer | None = field(default=None, repr=False)
    quote_pacer: QuotePacer | None = field(default=None, repr=False)
    finished_runs: list[str] = field(default_factory=list)

    def refusal(self) -> str | None:
        """Why no further SDK may start: a CHECK THE ACCOUNT of an earlier SDK, else ``None``."""
        if self.check_the_account is None:
            return None
        return (
            f"{self.check_the_account}; {CHECK_THE_ACCOUNT} stops every later SDK of the run "
            "(design §9.1); refusing to start the next SDK"
        )

    def stop_all(self, reason: str) -> None:
        """Record a CHECK THE ACCOUNT (the first reason is kept)."""
        if self.check_the_account is None:
            self.check_the_account = reason

    def pacers(
        self, clock: Callable[[], float], sleep: Callable[[float], None]
    ) -> tuple[OrderPacer, QuotePacer]:
        """The shared pacers, made on the first session's clock and sleep."""
        if self.order_pacer is None:
            self.order_pacer = OrderPacer(clock=clock, sleep=sleep)
        if self.quote_pacer is None:
            self.quote_pacer = QuotePacer(clock=clock, sleep=sleep)
        return self.order_pacer, self.quote_pacer

    def finished(self, session: _Session, notices: Iterable[str | None] = ()) -> None:
        """One SDK's session ended: its end-of-session CHECK THE ACCOUNT notice, or any CHECK
        THE ACCOUNT breaker trip of its run, stops every later SDK."""
        self.finished_runs.append(session.run_id)
        for notice in notices:
            if notice and CHECK_THE_ACCOUNT in notice:
                self.stop_all(notice)
        for segment, reason in sorted(dict(session.guard.trips).items()):
            if CHECK_THE_ACCOUNT in reason:
                self.stop_all(
                    f"{CHECK_THE_ACCOUNT} in the {session.sdk.name} run {session.run_id} "
                    f"(segment {segment}): {reason}"
                )
