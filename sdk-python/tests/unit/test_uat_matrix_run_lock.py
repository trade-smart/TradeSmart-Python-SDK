"""One process per run id (final review M12): ``matrix_session`` creates ``<run>/run.lock``
(``O_EXCL``) before the run's ledger is read; a second process on the same run id is refused
before any API call; the session end removes it; a stale lock (older than 12 h, the ledger
untouched for 10 minutes) is taken over only with ``TRADESMART_UAT_RUN_LOCK_TAKEOVER=1``."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tests.live_orders.safety import Refusal
from tests.live_orders.uat_matrix import conftest as matrix_conftest
from tests.live_orders.uat_matrix import gate
from tests.unit.test_uat_matrix_safety import MATRIX_ENV, NOW, MatrixClient, _session


def _resume(tmp_path: Path, run_id: str, client: MatrixClient, **extra: str) -> gate.MatrixSession:
    environ = {**MATRIX_ENV, gate.RUN_ID_ENV: run_id, **extra}
    return _session(client, tmp_path, environ, now=NOW)


def _age(path: Path, hours: float) -> None:
    then = time.time() - hours * 3600
    os.utime(path, (then, then))


def test_a_session_holds_the_run_lock_and_its_end_removes_it(tmp_path: Path) -> None:
    session = _session(MatrixClient(), tmp_path)
    lock = session.run_dir / gate.LOCK_NAME
    assert lock.is_file()
    matrix_conftest.end_session(session)
    assert not lock.exists()


def test_a_second_process_on_the_same_run_id_is_refused_before_any_call(tmp_path: Path) -> None:
    first = _session(MatrixClient(), tmp_path)
    second_client = MatrixClient()
    with pytest.raises(Refusal, match="in use"):
        _resume(tmp_path, first.run_id, second_client)
    assert second_client.calls == []  # refused before any API call
    assert (first.run_dir / gate.LOCK_NAME).is_file()  # the first process keeps its lock


def test_a_resume_after_the_first_process_ended_takes_the_lock(tmp_path: Path) -> None:
    first = _session(MatrixClient(), tmp_path)
    gate.release_run_lock(first)
    second = _resume(tmp_path, first.run_id, MatrixClient())
    assert (second.run_dir / gate.LOCK_NAME).is_file()


def test_a_refused_session_does_not_keep_the_lock(tmp_path: Path) -> None:
    first = _session(MatrixClient(), tmp_path)
    gate.release_run_lock(first)
    client = MatrixClient(books=[Exception("the book cannot be read")])
    with pytest.raises(Exception):  # noqa: B017,PT011 - the open-order check stops the run
        _resume(tmp_path, first.run_id, client)
    assert not (first.run_dir / gate.LOCK_NAME).exists()


def test_a_stale_lock_is_taken_over_only_with_the_opt_in(tmp_path: Path) -> None:
    first = _session(MatrixClient(), tmp_path)  # killed hard: its lock stays
    lock = first.run_dir / gate.LOCK_NAME
    ledger = first.run_dir / "ledger.jsonl"
    _age(lock, 13)
    if ledger.exists():
        _age(ledger, 13)
    with pytest.raises(Refusal, match=gate.LOCK_TAKEOVER_ENV):
        _resume(tmp_path, first.run_id, MatrixClient())
    second = _resume(tmp_path, first.run_id, MatrixClient(), **{gate.LOCK_TAKEOVER_ENV: "1"})
    assert (second.run_dir / gate.LOCK_NAME).is_file()


def test_a_fresh_lock_is_never_taken_over(tmp_path: Path) -> None:
    first = _session(MatrixClient(), tmp_path)
    with pytest.raises(Refusal, match="in use"):
        _resume(tmp_path, first.run_id, MatrixClient(), **{gate.LOCK_TAKEOVER_ENV: "1"})
