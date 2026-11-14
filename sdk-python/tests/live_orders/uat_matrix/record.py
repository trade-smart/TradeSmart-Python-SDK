"""One JSON record per case of the UAT matrix: ``<run>/records.jsonl`` (local, never public).

A record holds what the case planned and saw (contract, price snapshot, plan, the expectation,
every call's outcome, the order-book rows, stream updates and cross-check notes) and its verdict.
Account ids never reach the file: ``ledger.redact`` drops ``client_id``, ``user_id``,
``account_id``, ``uid`` and ``actid`` at any depth (and secret keys), and replaces any text that
names a forbidden value (the session's client id). Lines are written whole under a lock, so the
batch engine's threads never interleave them.

A family may extend every record with ``enrich`` (a callable of the record returning more keys,
added before redaction; one case at a time; an error is recorded as ``views_error``, never
raised): the views family (M9) and the modify family (M2) add each order's history, trades, the
positions delta, the stream lag and the raw request / response bodies of the case's calls
(``views.RecordViews``, which reads the bodies from the ledger with ``calls_from_ledger``).
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.live_orders import mock_session
from tests.live_orders.uat_matrix.ledger import redact

RECORDS_NAME = "records.jsonl"

Enrich = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def calls_from_ledger(path: Path, call_ids: Iterable[str]) -> list[dict[str, Any]]:
    """The ledger's intent and outcome of each call id, in the order given: the request
    (``request``: the intent's detail, e.g. the plan or the modify changes), the ``verdict``
    and the raw ``response`` body (recorded when the session logs raw bodies) or ``error``.
    A call without an outcome line has ``verdict`` ``None``; a missing ledger gives ``[]``."""
    wanted = [str(c) for c in call_ids if c]
    if not wanted or not path.exists():
        return []
    intents: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a torn last line
        if not isinstance(entry, dict) or str(entry.get("call")) not in wanted:
            continue
        if entry.get("type") == "intent":
            intents[str(entry["call"])] = entry
        elif entry.get("type") == "outcome":
            outcomes[str(entry["call"])] = entry
    pairs: list[dict[str, Any]] = []
    for call in wanted:
        intent = intents.get(call)
        if intent is None:
            continue
        outcome = outcomes.get(call, {})
        detail = outcome.get("detail") or {}
        pairs.append(
            {
                "call": call,
                "kind": intent.get("kind"),
                "tag": intent.get("tag"),
                "order_id": outcome.get("order_id") or intent.get("order_id"),
                "request": intent.get("detail") or {},
                "verdict": outcome.get("verdict"),
                "response": detail.get("response"),
                "error": detail.get("error"),
            }
        )
    return pairs


MOCK_ENVIRONMENT = mock_session.ENVIRONMENT


def run_fields(run_dir: Path) -> dict[str, str]:
    """The fields every record of the run carries, from its ``run.json``: a mock-session run's
    ``environment`` (``production-mock``) and ``mock_session`` date; ``{}`` for any other run
    (or no readable run file)."""
    try:
        data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("environment") != MOCK_ENVIRONMENT:
        return {}
    return {"environment": MOCK_ENVIRONMENT, "mock_session": str(data.get("mock_session") or "")}


class Recorder:
    """Appends one redacted JSON line per case to the run's ``records.jsonl``; a mock-session
    run's records carry its ``environment`` and ``mock_session`` date (``run_fields``)."""

    def __init__(
        self,
        run_dir: Path,
        *,
        run_id: str,
        forbidden: Iterable[str] = (),
        enrich: Enrich | None = None,
    ) -> None:
        self.path = run_dir / RECORDS_NAME
        self.run_id = run_id
        self.forbidden = tuple(forbidden)
        self.enrich = enrich
        self.fields = run_fields(run_dir)
        self._lock = threading.Lock()
        self._enrich_lock = threading.Lock()

    def _extra(self, record: Mapping[str, Any]) -> dict[str, Any]:
        enrich = self.enrich
        if enrich is None:
            return {}
        with self._enrich_lock:  # one case at a time (its reads and its baselines)
            try:
                return dict(enrich(record))
            except Exception as exc:  # a record is never lost to its extras
                return {"views_error": f"{type(exc).__name__}: {exc}"}

    def write(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Write one case's record (with ``enrich``'s keys); returns what was written."""
        extra = self._extra(record)
        data = redact({"run_id": self.run_id, **record, **extra, **self.fields}, self.forbidden)
        data["at"] = datetime.now(UTC).isoformat()
        line = json.dumps(data, default=str, sort_keys=True) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        result: dict[str, Any] = data
        return result

    def read(self) -> list[dict[str, Any]]:
        """Every record of the run so far (a torn last line is ignored)."""
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records
