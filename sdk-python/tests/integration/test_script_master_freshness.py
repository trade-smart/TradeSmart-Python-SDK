"""Script-master freshness follows TradeSmart's daily refresh (review 2026-11-10 C5)."""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
import respx

import tradesmart.instruments as instruments
from tests.integration.test_instruments import SAMPLE, URL, make_zip
from tradesmart import AsyncTradeSmart, ClientConfig, ConfigurationError, TradeSmart
from tradesmart.models.base import IST

DAY = datetime(2026, 11, 10, tzinfo=IST)


def at(hour: int, minute: int, *, days: int = 0) -> datetime:
    return DAY + timedelta(days=days, hours=hour, minutes=minute)


def written(path: Path, when: datetime) -> Path:
    path.write_text(SAMPLE, encoding="utf-8")
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))
    return path


@pytest.mark.parametrize(
    "mtime,now,fresh",
    [
        (at(6, 30), at(7, 5), False),
        (at(7, 10), at(7, 20), True),
        (at(7, 10), at(23, 0), True),
        (at(8, 0, days=-1), at(6, 0), True),
        (at(8, 0, days=-1), at(7, 0), False),
        (at(6, 59), at(7, 0), False),
        (at(7, 0), at(7, 0), True),
    ],
    ids=[
        "0630-stale-at-0705",
        "0710-fresh-at-0720",
        "0710-fresh-at-2300",
        "yesterday-0800-fresh-at-0600",
        "yesterday-0800-stale-at-0700",
        "0659-stale-at-0700",
        "0700-fresh-at-0700",
    ],
)
def test_is_fresh_uses_last_refresh_instant(tmp_path, mtime, now, fresh) -> None:
    path = written(tmp_path / "NSE_symbols.txt", mtime)
    assert instruments._is_fresh(path, force=False, refresh_time="07:00", now=now) is fresh


def test_is_fresh_force_and_missing(tmp_path) -> None:
    path = written(tmp_path / "NSE_symbols.txt", at(7, 10))
    assert instruments._is_fresh(path, force=True, refresh_time="07:00", now=at(7, 20)) is False
    missing = tmp_path / "BSE_symbols.txt"
    assert instruments._is_fresh(missing, force=False, refresh_time="07:00", now=at(7, 20)) is False


def test_is_fresh_honours_configured_refresh_time(tmp_path) -> None:
    path = written(tmp_path / "NSE_symbols.txt", at(6, 30))
    assert instruments._is_fresh(path, force=False, refresh_time="06:00", now=at(7, 5)) is True


def test_refresh_time_default_and_validation() -> None:
    assert ClientConfig().script_master_refresh_time == "07:00"
    assert ClientConfig(script_master_refresh_time="23:59").script_master_refresh_time == "23:59"
    for bad in ("7:00", "24:00", "07:60", "0700", "abc", "07:00 "):
        with pytest.raises(ConfigurationError, match="script_master_refresh_time"):
            ClientConfig(script_master_refresh_time=bad)


@respx.mock
def test_download_before_refresh_is_replaced_after_it(tmp_path, monkeypatch) -> None:
    route = respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    written(tmp_path / "NSE_symbols.txt", at(0, 30))
    client = TradeSmart("TEST123", access_token="t")
    monkeypatch.setattr(instruments, "_now", lambda: at(6, 45))
    client.download_instruments("NSE", tmp_path)
    assert route.call_count == 0  # still before today's refresh
    monkeypatch.setattr(instruments, "_now", lambda: at(7, 5))
    client.download_instruments("NSE", tmp_path)
    assert route.call_count == 1  # the 00:30 file predates the 07:00 refresh


@respx.mock
async def test_async_download_before_refresh_is_replaced_after_it(tmp_path, monkeypatch) -> None:
    route = respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    written(tmp_path / "NSE_symbols.txt", at(0, 30))
    monkeypatch.setattr(instruments, "_now", lambda: at(7, 5))
    async with AsyncTradeSmart("TEST123", access_token="t") as client:
        await client.download_instruments("NSE", tmp_path)
    assert route.call_count == 1


# --- the file's mtime follows Last-Modified (fix round 1, item 3) -----------------------


def _http_date(when: datetime) -> str:
    return format_datetime(when.astimezone(UTC), usegmt=True)


@respx.mock
def test_download_mtime_follows_last_modified_yesterday_is_stale(tmp_path) -> None:
    # Downloaded at 07:00:10, before the server's actual refresh (07:00:24): the file is
    # yesterday's, so it must count as stale after 07:00.
    served = at(7, 0, days=-1) + timedelta(seconds=24)
    respx.get(URL).respond(
        200,
        content=make_zip(("NSE_symbols.txt", SAMPLE)),
        headers={"Last-Modified": _http_date(served)},
    )
    path = TradeSmart("TEST123", access_token="t").download_instruments("NSE", tmp_path)
    assert path.stat().st_mtime == served.timestamp()
    assert instruments._is_fresh(path, force=False, refresh_time="07:00", now=at(7, 5)) is False


@respx.mock
async def test_async_download_mtime_follows_last_modified_today_is_fresh(tmp_path) -> None:
    # A past day (a future Last-Modified is clamped to now); "today" is that day here.
    served = at(7, 0, days=-30) + timedelta(seconds=24)
    respx.get(URL).respond(
        200,
        content=make_zip(("NSE_symbols.txt", SAMPLE)),
        headers={"Last-Modified": _http_date(served)},
    )
    async with AsyncTradeSmart("TEST123", access_token="t") as client:
        path = await client.download_instruments("NSE", tmp_path)
    assert path.stat().st_mtime == served.timestamp()
    now = at(7, 5, days=-30)
    assert instruments._is_fresh(path, force=False, refresh_time="07:00", now=now) is True


@pytest.mark.parametrize("header", [None, "garbage", "", "Mon, 99 Foo 2026 25:61:00 GMT"])
@respx.mock
def test_download_without_usable_last_modified_keeps_write_time(tmp_path, header) -> None:
    headers = {} if header is None else {"Last-Modified": header}
    respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)), headers=headers)
    before = time.time()
    path = TradeSmart("TEST123", access_token="t").download_instruments("NSE", tmp_path)
    assert path.stat().st_mtime >= before - 2  # the write time, not a server time


# --- the Last-Modified stamp can never break or fake freshness (re-review hardening) ----


@respx.mock
def test_future_last_modified_is_clamped_to_now(tmp_path) -> None:
    future = datetime.now(IST) + timedelta(days=3)
    respx.get(URL).respond(
        200,
        content=make_zip(("NSE_symbols.txt", SAMPLE)),
        headers={"Last-Modified": _http_date(future)},
    )
    path = TradeSmart("TEST123", access_token="t").download_instruments("NSE", tmp_path)
    assert path.stat().st_mtime <= time.time()
    assert path.stat().st_mtime < future.timestamp() - 86400


@respx.mock
def test_utime_failure_keeps_write_time_and_download_succeeds(
    tmp_path, monkeypatch, caplog
) -> None:
    served = at(7, 0, days=-1)
    respx.get(URL).respond(
        200,
        content=make_zip(("NSE_symbols.txt", SAMPLE)),
        headers={"Last-Modified": _http_date(served)},
    )

    def broken_utime(*args: object, **kwargs: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(instruments.os, "utime", broken_utime)
    before = time.time()
    with caplog.at_level(logging.DEBUG, logger="tradesmart"):
        path = TradeSmart("TEST123", access_token="t").download_instruments("NSE", tmp_path)
    assert path.exists() and path.stat().st_mtime >= before - 2
    debug = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "NSE_symbols.txt" in r.getMessage()
    ]
    assert len(debug) == 1
    assert "read-only" not in debug[0].getMessage()


# --- an unchanged server file is re-checked conditionally, not re-downloaded (1a) -------

# A past day, so a Last-Modified on it is never clamped to the real clock.
PAST = datetime(2020, 1, 15, tzinfo=IST)


def past(hour: int, minute: int, second: int = 0) -> datetime:
    return PAST + timedelta(hours=hour, minutes=minute, seconds=second)


class Clock:
    """Fake ``instruments._now``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, start: datetime) -> None:
        self.now = start
        monkeypatch.setattr(instruments, "_now", lambda: self.now)


def zipped(text: str = SAMPLE, *, modified: datetime | None = None) -> httpx.Response:
    headers = {} if modified is None else {"Last-Modified": _http_date(modified)}
    return httpx.Response(200, content=make_zip(("NSE_symbols.txt", text)), headers=headers)


def checked_at(path: Path) -> datetime:
    sidecar = path.with_name(path.name + ".checked")
    return datetime.fromisoformat(sidecar.read_text(encoding="utf-8").strip())


@respx.mock
def test_unchanged_server_file_is_downloaded_once_then_rechecked_conditionally(
    tmp_path, monkeypatch
) -> None:
    # Reviewer scenario: Last-Modified 06:55 IST, refresh 07:00, now 10:00 IST.
    route = respx.get(URL).mock(side_effect=[zipped(modified=past(6, 55)), httpx.Response(304)])
    clock = Clock(monkeypatch, past(10, 0))
    client = TradeSmart("TEST123", access_token="t")
    path = client.download_instruments("NSE", tmp_path)
    clock.now = past(10, 5)
    client.download_instruments("NSE", tmp_path)
    clock.now = past(10, 14)
    client.download_instruments("NSE", tmp_path)
    assert route.call_count == 1  # three calls within 15 minutes: one download
    assert "if-modified-since" not in route.calls[0].request.headers
    assert checked_at(path) == past(10, 0)
    content, mtime = path.read_bytes(), path.stat().st_mtime

    clock.now = past(10, 16)
    assert client.download_instruments("NSE", tmp_path) == path
    assert route.call_count == 2
    request = route.calls[1].request
    assert request.headers["If-Modified-Since"] == _http_date(past(6, 55))
    assert "tradesmart-python/" in request.headers["User-Agent"]
    assert path.read_bytes() == content and path.stat().st_mtime == mtime  # 304: untouched
    assert checked_at(path) == past(10, 16)

    clock.now = past(10, 20)
    client.download_instruments("NSE", tmp_path)
    assert route.call_count == 2


@respx.mock
async def test_async_unchanged_server_file_is_rechecked_conditionally(
    tmp_path, monkeypatch
) -> None:
    route = respx.get(URL).mock(side_effect=[zipped(modified=past(6, 55)), httpx.Response(304)])
    clock = Clock(monkeypatch, past(10, 0))
    async with AsyncTradeSmart("TEST123", access_token="t") as client:
        path = await client.download_instruments("NSE", tmp_path)
        clock.now = past(10, 10)
        await client.download_instruments("NSE", tmp_path)
        assert route.call_count == 1
        content, mtime = path.read_bytes(), path.stat().st_mtime
        clock.now = past(10, 16)
        assert await client.download_instruments("NSE", tmp_path) == path
    assert route.call_count == 2
    assert route.calls[1].request.headers["If-Modified-Since"] == _http_date(past(6, 55))
    assert path.read_bytes() == content and path.stat().st_mtime == mtime
    assert checked_at(path) == past(10, 16)


@respx.mock
def test_conditional_200_with_newer_file_rewrites_and_is_then_fresh(tmp_path, monkeypatch) -> None:
    served = past(7, 0, 24)
    newer = SAMPLE + "NSE,30,1,NEW,NEW-EQ,EQ,0.05\n"
    route = respx.get(URL).mock(return_value=zipped(newer, modified=served))
    path = written(tmp_path / "NSE_symbols.txt", past(6, 55))
    clock = Clock(monkeypatch, past(10, 0))
    client = TradeSmart("TEST123", access_token="t")
    client.download_instruments("NSE", tmp_path)
    assert route.call_count == 1
    assert route.calls[0].request.headers["If-Modified-Since"] == _http_date(past(6, 55))
    assert path.read_text(encoding="utf-8") == newer
    assert path.stat().st_mtime == served.timestamp()
    assert checked_at(path) == past(10, 0)
    for later in (past(10, 30), past(18, 0), past(6, 59, 59) + timedelta(days=1)):
        clock.now = later
        client.download_instruments("NSE", tmp_path)
    assert route.call_count == 1  # the file now postdates the refresh: no request at all


@respx.mock
def test_conditional_200_still_older_than_refresh_is_written_and_stamped(
    tmp_path, monkeypatch
) -> None:
    route = respx.get(URL).mock(return_value=zipped(modified=past(6, 58)))
    path = written(tmp_path / "NSE_symbols.txt", past(6, 55))
    clock = Clock(monkeypatch, past(10, 0))
    client = TradeSmart("TEST123", access_token="t")
    client.download_instruments("NSE", tmp_path)
    assert path.stat().st_mtime == past(6, 58).timestamp()
    assert checked_at(path) == past(10, 0)
    clock.now = past(10, 5)
    client.download_instruments("NSE", tmp_path)
    assert route.call_count == 1


@pytest.mark.parametrize(
    "sidecar",
    [None, "garbage", "", "2020-01-15T10:00:00", "2099-01-01T00:00:00+00:00", b"\xff\xfe"],
    ids=["missing", "garbage", "empty", "naive", "future", "binary"],
)
@respx.mock
def test_missing_or_unusable_checked_stamp_behaves_as_no_check(
    tmp_path, monkeypatch, sidecar
) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(304))
    path = written(tmp_path / "NSE_symbols.txt", past(6, 55))
    stamp = tmp_path / "NSE_symbols.txt.checked"
    if isinstance(sidecar, bytes):
        stamp.write_bytes(sidecar)
    elif sidecar is not None:
        stamp.write_text(sidecar, encoding="utf-8")
    Clock(monkeypatch, past(10, 5))
    TradeSmart("TEST123", access_token="t").download_instruments("NSE", tmp_path)
    assert route.call_count == 1
    assert "If-Modified-Since" in route.calls[0].request.headers
    assert checked_at(path) == past(10, 5)


@respx.mock
def test_force_downloads_even_within_the_recheck_window(tmp_path, monkeypatch) -> None:
    route = respx.get(URL).mock(return_value=zipped(modified=past(6, 55)))
    path = written(tmp_path / "NSE_symbols.txt", past(6, 55))
    path.with_name(path.name + ".checked").write_text(past(10, 0).isoformat(), encoding="utf-8")
    Clock(monkeypatch, past(10, 5))
    client = TradeSmart("TEST123", access_token="t")
    client.download_instruments("NSE", tmp_path)
    assert route.call_count == 0  # checked five minutes ago
    client.download_instruments("NSE", tmp_path, force=True)
    assert route.call_count == 1
    assert "If-Modified-Since" not in route.calls[0].request.headers
    assert checked_at(path) == past(10, 5)


@respx.mock
def test_recheck_window_follows_config(tmp_path, monkeypatch) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(304))
    path = written(tmp_path / "NSE_symbols.txt", past(6, 55))
    path.with_name(path.name + ".checked").write_text(past(10, 0).isoformat(), encoding="utf-8")
    clock = Clock(monkeypatch, past(10, 29))
    client = TradeSmart(
        "TEST123", access_token="t", config=ClientConfig(script_master_recheck_minutes=30)
    )
    client.download_instruments("NSE", tmp_path)
    assert route.call_count == 0
    clock.now = past(10, 30)
    client.download_instruments("NSE", tmp_path)
    assert route.call_count == 1


def test_recheck_minutes_default_and_validation() -> None:
    assert ClientConfig().script_master_recheck_minutes == 15
    assert ClientConfig(script_master_recheck_minutes=1).script_master_recheck_minutes == 1
    for bad in (0, -5, 1.5, True, "15"):
        with pytest.raises(ConfigurationError, match="script_master_recheck_minutes"):
            ClientConfig(script_master_recheck_minutes=bad)  # type: ignore[arg-type]


@respx.mock
def test_unwritable_checked_stamp_does_not_fail_the_download(tmp_path, monkeypatch, caplog) -> None:
    route = respx.get(URL).mock(return_value=zipped(modified=past(6, 55)))
    (tmp_path / "NSE_symbols.txt.checked").mkdir()  # cannot be replaced by a file
    Clock(monkeypatch, past(10, 0))
    with caplog.at_level(logging.DEBUG, logger="tradesmart"):
        path = TradeSmart("TEST123", access_token="t").download_instruments("NSE", tmp_path)
    assert route.call_count == 1 and path.read_text(encoding="utf-8") == SAMPLE
    assert any("NSE_symbols.txt" in r.getMessage() for r in caplog.records)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "NSE_symbols.txt",
        "NSE_symbols.txt.checked",
    ]
