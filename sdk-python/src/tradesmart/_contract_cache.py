"""Per-client cache of the parsed script master: option, future and per-symbol rows.

One entry per exchange, keyed by the file's path, ``st_mtime_ns`` and size: a daily refresh
(or ``download_instruments(force=True)``) changes the key, the old entry is dropped and the
file is parsed again. ``instruments()`` does not use this cache; it returns a fresh list.
"""

from __future__ import annotations

import asyncio
import functools
import threading
from pathlib import Path

from tradesmart import instruments
from tradesmart._option_contracts import ContractIndex, build_contract_index

FileKey = tuple[str, int, int]


def file_key(path: Path) -> FileKey:
    """Path, modification time (ns) and size of ``path``."""
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _parse(path: Path) -> ContractIndex:
    return build_contract_index(instruments.load(path))


class ContractCache:
    """Thread-safe cache for ``TradeSmart``: concurrent first calls parse once."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[FileKey, ContractIndex]] = {}
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def get(self, exchange: str, path: Path) -> ContractIndex:
        # The key is read before the file: content newer than its key only costs a re-parse.
        key = file_key(path)
        entry = self._entries.get(exchange)
        if entry is not None and entry[0] == key:
            return entry[1]
        with self._guard:
            lock = self._locks.setdefault(exchange, threading.Lock())
        with lock:
            entry = self._entries.get(exchange)
            if entry is not None and entry[0] == key:
                return entry[1]
            self._entries.pop(exchange, None)  # never hold two copies of one exchange
            index = _parse(path)
            self._entries[exchange] = (key, index)
            return index


class AsyncContractCache:
    """Cache for ``AsyncTradeSmart``: one parse per exchange and file, in a worker thread.

    Concurrent callers await the same parse; a cancelled caller does not cancel it, and a
    failed parse reaches every caller waiting on it and is retried by the next call.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[FileKey, ContractIndex]] = {}
        self._pending: dict[str, tuple[FileKey, asyncio.Task[ContractIndex]]] = {}

    async def get(self, exchange: str, path: Path) -> ContractIndex:
        key = await asyncio.to_thread(file_key, path)
        entry = self._entries.get(exchange)
        if entry is not None and entry[0] == key:
            return entry[1]
        loop = asyncio.get_running_loop()
        pending = self._pending.get(exchange)
        if pending is None or pending[0] != key or pending[1].get_loop() is not loop:
            self._entries.pop(exchange, None)  # never hold two copies of one exchange
            task = loop.create_task(asyncio.to_thread(_parse, path))
            pending = (key, task)
            self._pending[exchange] = pending
            task.add_done_callback(functools.partial(self._settle, exchange, pending))
        return await asyncio.shield(pending[1])

    def _settle(
        self,
        exchange: str,
        pending: tuple[FileKey, asyncio.Task[ContractIndex]],
        task: asyncio.Task[ContractIndex],
    ) -> None:
        """Store a finished parse that is still the current one (runs before the waiters)."""
        failed = task.cancelled() or task.exception() is not None  # marks the error retrieved
        if self._pending.get(exchange) is not pending:
            return
        del self._pending[exchange]
        if not failed:
            self._entries[exchange] = (pending[0], task.result())
