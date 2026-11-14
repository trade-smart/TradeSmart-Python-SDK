"""``option_greeks()`` reuses the parsed script master per exchange (final review C1).

The script master is a real file; ``_instruments.download`` is replaced so that no request is
made, and every parse is counted wherever ``load`` is bound.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import respx

import tradesmart.async_client as async_client_module
import tradesmart.client as client_module
import tradesmart.instruments as instruments_module
from tests.integration.test_option_greeks_client import (
    NOW,
    ROWS,
    AnyClient,
    _row,
    call,
    mock_quotes,
)
from tradesmart import AsyncTradeSmart, ClientConfig, TradeSmart
from tradesmart import _option_contracts as oc

OPTION = "NIFTY26NOV26C24000"
QUOTES = {"1001": 24100, "1002": 24300, "2001": 300, "2003": 180, "2005": 200}


def write_master(path: Path, rows: list[dict[str, str]]) -> Path:
    header = list(rows[0])
    lines = [",".join(header)] + [",".join(r.get(k, "") for k in header) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class Parses:
    """Counts script-master parses (``load`` calls) and the threads they ran on."""

    def __init__(self, delay: float = 0.0) -> None:
        self.count = 0
        self.threads: list[int] = []
        self.delay = delay
        self.fail_next = False
        self._lock = threading.Lock()

    def wrap(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        def spy(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                self.count += 1
                self.threads.append(threading.get_ident())
                fail, self.fail_next = self.fail_next, False
            if self.delay:
                time.sleep(self.delay)
            if fail:
                raise OSError("disk read failed")
            return fn(*args, **kwargs)

        return spy


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "_now", lambda: NOW)


@pytest.fixture
def parses(monkeypatch: pytest.MonkeyPatch) -> Parses:
    counter = Parses()
    real = instruments_module.load
    for module in (instruments_module, client_module, async_client_module):
        monkeypatch.setattr(module, "load", counter.wrap(real))
    return counter


@pytest.fixture
def master(tmp_path: Path) -> Path:
    return write_master(tmp_path / "NFO_symbols.txt", ROWS)


@pytest.fixture(params=["sync", "async"])
async def any_client(
    request: pytest.FixtureRequest, master: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AnyClient, list[Any]]]:
    """A client whose script-master download returns ``master``; also the downloads made."""
    downloads: list[Any] = []
    kwargs: dict[str, Any] = {"access_token": "test-access-token", "config": ClientConfig()}
    if request.param == "sync":
        client = TradeSmart("TEST123", **kwargs)

        def download(exchange: Any, *args: Any, **kw: Any) -> Path:
            downloads.append(exchange)
            return master

        monkeypatch.setattr(client._instruments, "download", download)
        yield client, downloads
        client.close()
    else:
        aclient = AsyncTradeSmart("TEST123", **kwargs)

        async def adownload(exchange: Any, *args: Any, **kw: Any) -> Path:
            downloads.append(exchange)
            return master

        monkeypatch.setattr(aclient._instruments, "download", adownload)
        yield aclient, downloads
        await aclient.aclose()


def bump(path: Path, rows: list[dict[str, str]], *, keep_mtime: bool = False) -> None:
    """Rewrite ``path``; its mtime moves one second on unless ``keep_mtime``."""
    before = path.stat().st_mtime_ns
    write_master(path, rows)
    stamp = before if keep_mtime else before + 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


@respx.mock
async def test_second_option_greeks_call_does_not_reparse(
    any_client: tuple[AnyClient, list[Any]], parses: Parses
) -> None:
    client, downloads = any_client
    mock_quotes(QUOTES)

    first = await call(client, "option_greeks", "NFO", OPTION)
    second = await call(client, "option_greeks", "NFO", OPTION)

    assert parses.count == 1
    assert len(downloads) == 2  # the freshness check still runs on every call
    assert first.iv == second.iv and first.status == "ok"


@respx.mock
async def test_changed_file_mtime_is_reparsed(
    any_client: tuple[AnyClient, list[Any]], parses: Parses, master: Path
) -> None:
    client, _ = any_client
    mock_quotes(QUOTES)
    await call(client, "option_greeks", "NFO", OPTION)
    with pytest.raises(ValueError, match="NIFTY26NOV26C24100"):
        await call(client, "option_greeks", "NFO", "NIFTY26NOV26C24100")
    assert parses.count == 1

    added = _row("NIFTY26NOV26C24100", "2005", "OPTIDX", "26-NOV-2026", "CE", "24100")
    bump(master, [*ROWS, added])
    g = await call(client, "option_greeks", "NFO", "NIFTY26NOV26C24100")

    assert parses.count == 2
    assert g.option_tradingsymbol == "NIFTY26NOV26C24100"
    assert len(client._contracts._entries) == 1  # one copy per exchange


@respx.mock
async def test_changed_file_size_with_same_mtime_is_reparsed(
    any_client: tuple[AnyClient, list[Any]], parses: Parses, master: Path
) -> None:
    client, _ = any_client
    mock_quotes(QUOTES)
    await call(client, "option_greeks", "NFO", OPTION)

    added = _row("NIFTY26NOV26C24100", "2005", "OPTIDX", "26-NOV-2026", "CE", "24100")
    bump(master, [*ROWS, added], keep_mtime=True)
    g = await call(client, "option_greeks", "NFO", "NIFTY26NOV26C24100")

    assert parses.count == 2
    assert g.status == "ok"


@respx.mock
async def test_instruments_still_returns_a_fresh_independent_list(
    any_client: tuple[AnyClient, list[Any]], parses: Parses
) -> None:
    client, _ = any_client
    mock_quotes(QUOTES)
    await call(client, "option_greeks", "NFO", OPTION)

    rows = await call(client, "instruments", "NFO")
    assert rows == ROWS  # every row, the equity one included
    rows.clear()
    again = await call(client, "instruments", "NFO")
    assert again == ROWS and again is not rows
    for row in again:
        row["TradingSymbol"] = "MUTATED"

    g = await call(client, "option_greeks", "NFO", OPTION)
    assert g.option_tradingsymbol == OPTION
    assert parses.count == 3  # instruments() parses each time; option_greeks() reused its parse


@respx.mock
async def test_async_concurrent_option_greeks_parse_once_off_the_loop(
    master: Path, parses: Parses, monkeypatch: pytest.MonkeyPatch
) -> None:
    parses.delay = 0.05
    mock_quotes(QUOTES)
    loop_thread = threading.get_ident()
    async with AsyncTradeSmart("TEST123", access_token="t", config=ClientConfig()) as client:

        async def download(exchange: Any, *args: Any, **kw: Any) -> Path:
            return master

        monkeypatch.setattr(client._instruments, "download", download)
        results = await asyncio.gather(*(client.option_greeks("NFO", OPTION) for _ in range(10)))

    assert parses.count == 1
    assert parses.threads[0] != loop_thread
    assert {g.status for g in results} == {"ok"}


@respx.mock
async def test_async_failed_parse_reaches_every_waiter_and_is_retried(
    master: Path, parses: Parses, monkeypatch: pytest.MonkeyPatch
) -> None:
    parses.delay = 0.05
    parses.fail_next = True
    mock_quotes(QUOTES)
    async with AsyncTradeSmart("TEST123", access_token="t", config=ClientConfig()) as client:

        async def download(exchange: Any, *args: Any, **kw: Any) -> Path:
            return master

        monkeypatch.setattr(client._instruments, "download", download)
        outcomes = await asyncio.gather(
            *(client.option_greeks("NFO", OPTION) for _ in range(3)), return_exceptions=True
        )
        assert all(isinstance(o, OSError) for o in outcomes), outcomes
        assert parses.count == 1

        g = await client.option_greeks("NFO", OPTION)

    assert g.status == "ok"
    assert parses.count == 2


@respx.mock
async def test_async_cancelled_waiter_does_not_cancel_the_shared_parse(
    master: Path, parses: Parses, monkeypatch: pytest.MonkeyPatch
) -> None:
    parses.delay = 0.1
    mock_quotes(QUOTES)
    async with AsyncTradeSmart("TEST123", access_token="t", config=ClientConfig()) as client:

        async def download(exchange: Any, *args: Any, **kw: Any) -> Path:
            return master

        monkeypatch.setattr(client._instruments, "download", download)
        first = asyncio.ensure_future(client.option_greeks("NFO", OPTION))
        second = asyncio.ensure_future(client.option_greeks("NFO", OPTION))
        await asyncio.sleep(0.02)
        first.cancel()
        g = await second
        with pytest.raises(asyncio.CancelledError):
            await first

    assert g.status == "ok"
    assert parses.count == 1


@respx.mock
def test_sync_concurrent_threads_parse_once(master: Path, parses: Parses) -> None:
    parses.delay = 0.05
    mock_quotes(QUOTES)
    client = TradeSmart("TEST123", access_token="t", config=ClientConfig())
    client._instruments.download = lambda exchange, *a, **k: master  # type: ignore[method-assign]
    barrier = threading.Barrier(8)
    results: list[Any] = []
    errors: list[BaseException] = []

    def worker() -> None:
        barrier.wait()
        try:
            results.append(client.option_greeks("NFO", OPTION))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    client.close()

    assert not errors
    assert len(results) == 8
    assert parses.count == 1


@respx.mock
async def test_async_file_change_during_a_parse_keeps_only_the_newer_parse(
    master: Path, parses: Parses, monkeypatch: pytest.MonkeyPatch
) -> None:
    parses.delay = 0.1
    mock_quotes(QUOTES)
    added = _row("NIFTY26NOV26C24100", "2005", "OPTIDX", "26-NOV-2026", "CE", "24100")
    async with AsyncTradeSmart("TEST123", access_token="t", config=ClientConfig()) as client:

        async def download(exchange: Any, *args: Any, **kw: Any) -> Path:
            return master

        monkeypatch.setattr(client._instruments, "download", download)
        old = asyncio.ensure_future(client.option_greeks("NFO", OPTION))
        await asyncio.sleep(0.03)  # the first parse is running
        bump(master, [*ROWS, added])
        new = await client.option_greeks("NFO", "NIFTY26NOV26C24100")
        assert (await old).status == "ok"
        assert parses.count == 2

        again = await client.option_greeks("NFO", "NIFTY26NOV26C24100")

    assert new.status == "ok" and again.status == "ok"
    assert parses.count == 2  # the newer parse is the one kept
    assert len(client._contracts._entries) == 1
