"""Run guards of the UAT matrix: declared counts, ``MAX_OPEN``, circuit breakers, the STOP file.

There is no per-run or per-day order cap on UAT (owner decision U1). What stays:

- a per-case **declared** place-call count (a bug guard): ``reserve(case_id, n)`` once, then every
  place call spends from it before anything is sent; a case can never exceed it;
- ``MAX_OPEN`` simultaneously open orders of the run, slots claimed atomically by each place call
  (``check_send`` .. ``release``; the batch waits for room or cancels);
- per-segment **circuit breakers**: one stuck order, one CHECK THE ACCOUNT, or three unexpected
  rejections in a row trip the segment; a tripped segment sends no new order (cancels and exits
  still go out: they only reduce what is open);
- the **STOP file** (``.live-results/uat-matrix/STOP``), checked before every state-changing
  call: once seen, no new place or modify call is made for the rest of the run (sticky).
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from tests.live_orders.safety import CHECK_THE_ACCOUNT, Refusal

MAX_OPEN = 50  # simultaneously open orders of one run
STUCK_TRIP = 1  # stuck orders that trip a segment
CHECK_TRIP = 1  # CHECK THE ACCOUNT outcomes that trip a segment
REJECTIONS_TRIP = 3  # unexpected rejections in a row that trip a segment
STOP_REASON = "stopped by the STOP file"
ACK_GRACE = 10.0  # seconds an acknowledged order may be missing from the book (not yet listed)


class StopRun(Exception):
    """Stop the whole run: no new orders; cancel ours; skip every remaining item with ``reason``.

    ``foreign`` names open orders that are not this run's (never touched).
    """

    def __init__(self, reason: str, foreign: Iterable[str] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.foreign = tuple(foreign)


class MaxOpenReached(Exception):
    """``MAX_OPEN`` orders of the run are open: nothing was sent; wait for room or cancel."""


class Guard:
    """The run's guards (thread-safe: the batch engine may place from several threads)."""

    def __init__(
        self,
        stop_file: Path,
        *,
        max_open: int = MAX_OPEN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stop_file = stop_file
        self.max_open = max_open
        self.clock = clock
        self.declared: dict[str, int] = {}
        self.used: dict[str, int] = {}
        self.open_ids: set[str] = set()
        self.pending = 0  # slots claimed by place calls in flight (check_send .. release)
        self._opened_at: dict[str, float] = {}
        self.trips: dict[str, str] = {}
        self._rejections: dict[str, int] = {}
        self._checks: dict[str, int] = {}
        self._stuck: dict[str, int] = {}
        self.stop_reason: str | None = None
        # Called once per newly tripped segment (the run persists it to the ledger, so a resume
        # restores a trip from any path: batch, burst, sliced, async views, GTT).
        self.on_trip: Callable[[str, str], None] | None = None
        self._lock = threading.RLock()

    # -- declared counts ------------------------------------------------------------------

    def reserve(self, case_id: str, n: int) -> None:
        """Declare that ``case_id`` may make at most ``n`` place calls (once per case)."""
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ValueError(f"case {case_id}: the declared order count must be >= 1, got {n!r}")
        with self._lock:
            if case_id in self.declared:
                raise ValueError(f"case {case_id}: order count already declared")
            self.declared[case_id] = n

    def spend(self, case_id: str, n: int = 1) -> None:
        """Spend ``n`` of the case's declared count before sending; refuses past it."""
        with self._lock:
            declared = self.declared.get(case_id)
            if declared is None:
                raise Refusal(f"case {case_id}: no declared order count (Guard.reserve); refusing")
            used = self.used.get(case_id, 0)
            if used + n > declared:
                raise Refusal(
                    f"case {case_id}: declared order count {declared} exceeded "
                    f"({used} sent, {n} more asked); refusing (a bug in the case)"
                )
            self.used[case_id] = used + n

    # -- open orders ----------------------------------------------------------------------

    @property
    def open_count(self) -> int:
        return len(self.open_ids)

    def opened(self, *order_ids: str) -> None:
        with self._lock:
            now = self.clock()
            for order_id in order_ids:
                if order_id:
                    self.open_ids.add(order_id)
                    self._opened_at[order_id] = now

    def closed(self, *order_ids: str) -> None:
        with self._lock:
            self.open_ids.difference_update(order_ids)

    def release(self, n: int, *opened_ids: str) -> None:
        """End of a place call that claimed ``n`` slots (``check_send``): what it opened."""
        with self._lock:
            self.pending = max(0, self.pending - n)
            self.opened(*opened_ids)

    def sync_open(self, order_ids: Iterable[str], *, seen: Iterable[str] | None = None) -> None:
        """Follow the order book: ``order_ids`` are our non-final orders there, ``seen`` every
        order id the book listed. An order missing from the book is dropped when the book shows
        it final (in ``seen``) or when it was acknowledged more than ``ACK_GRACE`` seconds ago;
        a fresh acknowledgement the book does not list yet stays open."""
        with self._lock:
            now = self.clock()
            listed = set(seen) if seen is not None else set()
            keep = {i for i in order_ids if i}
            for order_id in self.open_ids - keep:
                if order_id in listed:
                    continue
                if now - self._opened_at.get(order_id, float("-inf")) < ACK_GRACE:
                    keep.add(order_id)
            self.open_ids = keep

    def wait_for_room(
        self,
        n: int,
        refresh: Callable[[], tuple[set[str], set[str]]],
        *,
        timeout: float,
        poll_interval: float,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ) -> None:
        """Wait until ``n`` more orders fit under ``max_open``; ``refresh`` reads the book
        (read-only) and returns (our non-final ids, every id listed). Raises ``StopRun`` once
        stopped, ``MaxOpenReached`` after ``timeout`` (the caller then cancels the batch)."""
        deadline = clock() + timeout
        while self.max_open - self.open_count - self.pending < n:
            if self.stopped():
                raise StopRun(self.stop_reason or STOP_REASON)
            if clock() >= deadline:
                raise MaxOpenReached(
                    f"{self.open_count} orders of the run still open after {timeout} s "
                    f"(MAX_OPEN {self.max_open}); cancel the batch"
                )
            sleep(poll_interval)
            open_ids, seen = refresh()
            self.sync_open(open_ids, seen=seen)

    # -- circuit breakers -----------------------------------------------------------------

    def trip(self, segment: str, reason: str) -> None:
        """Trip ``segment``'s breaker (the first reason is kept); a new trip is reported to
        ``on_trip`` (its failure never hides the trip, which stays in memory)."""
        with self._lock:
            if segment in self.trips:
                return
            self.trips[segment] = reason
            if self.on_trip is not None:
                with contextlib.suppress(Exception):
                    self.on_trip(segment, reason)

    def tripped(self, segment: str) -> str | None:
        return self.trips.get(segment)

    def note_stuck(self, segment: str, order_id: str) -> None:
        with self._lock:
            self._stuck[segment] = self._stuck.get(segment, 0) + 1
            if self._stuck[segment] >= STUCK_TRIP:
                self.trip(segment, f"order {order_id} stuck (not final after the cancel)")

    def note_check_the_account(self, segment: str, what: str) -> None:
        with self._lock:
            self._checks[segment] = self._checks.get(segment, 0) + 1
            if self._checks[segment] >= CHECK_TRIP:
                self.trip(segment, f"{CHECK_THE_ACCOUNT}: {what}")

    def note_rejection(self, segment: str, *, expected: bool) -> None:
        """An outcome of ``segment``: an unexpected rejection counts, anything expected resets."""
        with self._lock:
            if expected:
                self._rejections[segment] = 0
                return
            self._rejections[segment] = self._rejections.get(segment, 0) + 1
            if self._rejections[segment] >= REJECTIONS_TRIP:
                self.trip(segment, f"{REJECTIONS_TRIP} unexpected rejections in a row")

    # -- stop -----------------------------------------------------------------------------

    def stop(self, reason: str) -> None:
        """Stop the run (the first reason is kept)."""
        with self._lock:
            if self.stop_reason is None:
                self.stop_reason = reason

    def stopped(self) -> bool:
        """Whether the run is stopped: the STOP file exists now or did once, or ``stop()``."""
        if self.stop_reason is None and self.stop_file.exists():
            self.stop(STOP_REASON)
        return self.stop_reason is not None

    # -- before a call --------------------------------------------------------------------

    def check_new(self, segment: str) -> None:
        """Before a call that adds risk (place, place_sliced, modify): not stopped, not tripped."""
        if self.stopped():
            raise StopRun(self.stop_reason or STOP_REASON)
        reason = self.tripped(segment)
        if reason is not None:
            raise Refusal(f"circuit breaker tripped for {segment}: {reason}")

    def check_send(self, segment: str, n: int = 1) -> None:
        """Before a place call of ``n`` orders: ``check_new``, then **claim** ``n`` slots under
        ``max_open`` (open + claimed + n, atomically). The caller must ``release(n, ...)`` in a
        ``finally`` block, whatever the call did."""
        with self._lock:
            self.check_new(segment)
            if self.open_count + self.pending + n > self.max_open:
                raise MaxOpenReached(
                    f"{self.open_count} orders of the run open and {self.pending} being placed; "
                    f"{n} more would pass MAX_OPEN {self.max_open}; nothing sent"
                )
            self.pending += n
