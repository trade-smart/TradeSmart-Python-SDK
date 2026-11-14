"""The run's position ledger: what this run's fills left open, per instrument (UAT only).

On UAT a closing SELL at the last price does not fill (known issue ``closing-sell``, owner
answer U8): the fill families keep the position here instead of failing with CHECK THE ACCOUNT,
and stop adding to an instrument once its leftover would pass ``CEILING_LOTS`` lots. The owner's
(or the end-of-day) square-off clears the leftovers; ``leftovers()`` lists them.

``<run>/positions.jsonl`` (local, never public): one JSON line per fill and per close attempt,
replayed on open, so a resumed run of the same day keeps its leftovers. Account ids never reach
the file (``ledger.redact``). Fills on one instrument go one at a time (``one_at_a_time``), so
the position change of each is attributable.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.live_orders.safety import Refusal
from tests.live_orders.uat_matrix.ledger import redact

POSITIONS_NAME = "positions.jsonl"
CEILING_LOTS = 2  # leftover lots per instrument (owner answer U8)


class PositionLedger:
    """Signed net quantity (trade units) this run's fills left open, per trading symbol."""

    def __init__(
        self, path: Path, *, forbidden: Iterable[str] = (), ceiling_lots: int = CEILING_LOTS
    ) -> None:
        self.path = path
        self.forbidden = tuple(forbidden)
        self.ceiling_lots = ceiling_lots
        self._net: dict[str, int] = {}
        self._cnc: dict[str, int] = {}  # net CNC units (fills and closes with product CNC)
        self._lock = threading.RLock()
        self._instrument_locks: dict[str, threading.Lock] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a torn last line
            if isinstance(record, dict):
                self._apply(record)

    def _apply(self, record: dict[str, Any]) -> None:
        symbol = str(record.get("tradingsymbol"))
        change = int(record.get("change") or 0)
        if str(record.get("product", "")).upper() == "CNC":
            self._cnc[symbol] = self._cnc.get(symbol, 0) + change
        self._net[symbol] = self._net.get(symbol, 0) + change
        if self._net[symbol] == 0:
            del self._net[symbol]

    def _write(self, record: dict[str, Any]) -> None:
        data = redact(record, self.forbidden)
        data["at"] = datetime.now(UTC).isoformat()
        line = json.dumps(data, default=str, sort_keys=True) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            self._apply(data)

    # -- changes ------------------------------------------------------------------------------

    def add_fill(
        self,
        tradingsymbol: str,
        quantity: int,
        *,
        lot_size: int,
        case_id: str,
        order_id: str | None,
        **detail: Any,
    ) -> None:
        """A fill of ``quantity`` trade units: positive for a BUY, negative for a SELL."""
        self._write(
            {
                "type": "fill",
                "tradingsymbol": tradingsymbol,
                "change": quantity,
                "lot_size": lot_size,
                "case_id": case_id,
                "order_id": order_id,
                **detail,
            }
        )

    def close_attempt(
        self,
        tradingsymbol: str,
        filled: int,
        *,
        case_id: str,
        order_id: str | None,
        **detail: Any,
    ) -> None:
        """A closing order filled ``filled`` units (0: it did not fill; the position stays)."""
        with self._lock:
            net = self._net.get(tradingsymbol, 0)
            change = -filled if net > 0 else filled if net < 0 else 0
            self._write(
                {
                    "type": "close",
                    "tradingsymbol": tradingsymbol,
                    "change": change,
                    "filled": filled,
                    "case_id": case_id,
                    "order_id": order_id,
                    **detail,
                }
            )

    # -- reading ------------------------------------------------------------------------------

    def net(self, tradingsymbol: str) -> int:
        with self._lock:
            return self._net.get(tradingsymbol, 0)

    def cnc_bought(self, tradingsymbol: str) -> int:
        """Net CNC units this run's fills bought and still hold (a CNC SELL may close them)."""
        with self._lock:
            return self._cnc.get(tradingsymbol, 0)

    def leftovers(self) -> dict[str, int]:
        """Instruments with a position left by this run (signed trade units)."""
        with self._lock:
            return dict(self._net)

    def room(self, tradingsymbol: str, quantity: int, *, lot_size: int) -> bool:
        """Whether a fill of ``quantity`` (signed) keeps the instrument within the ceiling."""
        with self._lock:
            after = self._net.get(tradingsymbol, 0) + quantity
            return abs(after) <= self.ceiling_lots * lot_size

    def reached(self, tradingsymbol: str, *, lot_size: int) -> bool:
        """Whether the instrument's |net| position is at (or past) the ceiling."""
        with self._lock:
            return abs(self._net.get(tradingsymbol, 0)) >= self.ceiling_lots * lot_size

    def check_room(self, tradingsymbol: str, quantity: int, *, lot_size: int) -> None:
        """``Refusal`` when a fill of ``quantity`` would pass the per-instrument ceiling."""
        if not self.room(tradingsymbol, quantity, lot_size=lot_size):
            raise Refusal(
                f"{tradingsymbol}: this run's leftover position {self.net(tradingsymbol)} plus "
                f"{quantity} would pass the ceiling of {self.ceiling_lots} lots "
                f"({self.ceiling_lots * lot_size} units); no fill order"
            )

    @contextmanager
    def one_at_a_time(self, tradingsymbol: str) -> Iterator[None]:
        """Hold the instrument while one fill case runs (fills on it go one at a time)."""
        with self._lock:
            lock = self._instrument_locks.setdefault(tradingsymbol, threading.Lock())
        with lock:
            yield
