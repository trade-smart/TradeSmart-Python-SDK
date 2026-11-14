"""A failed conditional re-check of the script master falls back to the cached file."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import respx

from tests.integration.test_instruments import SAMPLE, URL
from tests.integration.test_script_master_freshness import (
    Clock,
    checked_at,
    past,
    written,
    zipped,
)
from tradesmart import (
    AsyncTradeSmart,
    NetworkError,
    NotFoundError,
    RequestTimeoutError,
    ResponseParseError,
    ServerError,
    TradeSmart,
)

FAILURES = [
    (httpx.ConnectError("down"), "NetworkError"),
    (httpx.ReadTimeout("slow"), "RequestTimeoutError"),
    (httpx.Response(503, text="maintenance"), "ServerError"),
]
IDS = ["network", "timeout", "5xx"]


def _side_effect(failure: object) -> dict[str, object]:
    if isinstance(failure, httpx.Response):
        return {"return_value": failure}
    return {"side_effect": failure}


def _cache_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "cached script master" in r.getMessage()
    ]


@pytest.mark.parametrize(("failure", "error_type"), FAILURES, ids=IDS)
@respx.mock
def test_failed_recheck_returns_cached_file_with_one_warning(
    tmp_path, monkeypatch, caplog, failure, error_type
) -> None:
    caplog.set_level(logging.WARNING, logger="tradesmart")
    route = respx.get(URL).mock(**_side_effect(failure))
    path = written(tmp_path / "NSE_symbols.txt", past(6, 55))  # older than the refresh
    Clock(monkeypatch, past(10, 0))
    client = TradeSmart("TEST123", access_token="t")
    assert client.download_instruments("NSE", tmp_path) == path
    assert route.call_count == 1
    assert "If-Modified-Since" in route.calls[0].request.headers
    assert path.read_text(encoding="utf-8") == SAMPLE
    assert _cache_warnings(caplog) == [
        f"using the cached script master; re-check failed: {error_type}"
    ]


@pytest.mark.parametrize(("failure", "error_type"), FAILURES, ids=IDS)
@respx.mock
async def test_async_failed_recheck_returns_cached_file_with_one_warning(
    tmp_path, monkeypatch, caplog, failure, error_type
) -> None:
    caplog.set_level(logging.WARNING, logger="tradesmart")
    respx.get(URL).mock(**_side_effect(failure))
    path = written(tmp_path / "NSE_symbols.txt", past(6, 55))
    Clock(monkeypatch, past(10, 0))
    async with AsyncTradeSmart("TEST123", access_token="t") as client:
        assert await client.download_instruments("NSE", tmp_path) == path
    assert _cache_warnings(caplog) == [
        f"using the cached script master; re-check failed: {error_type}"
    ]


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (httpx.ConnectError("down"), NetworkError),
        (httpx.ReadTimeout("slow"), RequestTimeoutError),
        (httpx.Response(503, text="maintenance"), ServerError),
    ],
    ids=IDS,
)
@respx.mock
def test_first_download_failure_still_raises(tmp_path, caplog, failure, error) -> None:
    caplog.set_level(logging.WARNING, logger="tradesmart")
    respx.get(URL).mock(**_side_effect(failure))
    with pytest.raises(error):
        TradeSmart("TEST123", access_token="t").download_instruments("NSE", tmp_path)
    assert _cache_warnings(caplog) == []


@respx.mock
def test_forced_download_failure_raises_even_with_a_cached_file(tmp_path, monkeypatch) -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
    written(tmp_path / "NSE_symbols.txt", past(6, 55))
    Clock(monkeypatch, past(10, 0))
    with pytest.raises(NetworkError):
        TradeSmart("TEST123", access_token="t").download_instruments("NSE", tmp_path, force=True)


@respx.mock
def test_recheck_client_error_is_not_masked_by_the_cache(tmp_path, monkeypatch) -> None:
    respx.get(URL).mock(return_value=httpx.Response(404, text="gone"))
    written(tmp_path / "NSE_symbols.txt", past(6, 55))
    Clock(monkeypatch, past(10, 0))
    with pytest.raises(NotFoundError):
        TradeSmart("TEST123", access_token="t").download_instruments("NSE", tmp_path)


@respx.mock
async def test_async_first_download_failure_still_raises(tmp_path) -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
    async with AsyncTradeSmart("TEST123", access_token="t") as client:
        with pytest.raises(NetworkError):
            await client.download_instruments("NSE", tmp_path)


# --- an outage is throttled like an unchanged file: one re-check per window ------------


@pytest.mark.parametrize(("failure", "error_type"), FAILURES, ids=IDS)
@respx.mock
def test_failed_recheck_is_throttled_by_the_recheck_window(
    tmp_path, monkeypatch, failure, error_type
) -> None:
    route = respx.get(URL).mock(**_side_effect(failure))
    path = written(tmp_path / "NSE_symbols.txt", past(6, 55))
    clock = Clock(monkeypatch, past(10, 0))
    client = TradeSmart("TEST123", access_token="t")
    assert client.download_instruments("NSE", tmp_path) == path
    assert checked_at(path) == past(10, 0)
    for minute in (1, 5, 14):  # within script_master_recheck_minutes (15): no request
        clock.now = past(10, minute)
        assert client.download_instruments("NSE", tmp_path) == path
    assert route.call_count == 1
    clock.now = past(10, 16)
    assert client.download_instruments("NSE", tmp_path) == path
    assert route.call_count == 2
    assert checked_at(path) == past(10, 16)


@respx.mock
async def test_async_failed_recheck_is_throttled_by_the_recheck_window(
    tmp_path, monkeypatch
) -> None:
    route = respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
    path = written(tmp_path / "NSE_symbols.txt", past(6, 55))
    clock = Clock(monkeypatch, past(10, 0))
    async with AsyncTradeSmart("TEST123", access_token="t") as client:
        assert await client.download_instruments("NSE", tmp_path) == path
        clock.now = past(10, 5)
        assert await client.download_instruments("NSE", tmp_path) == path
    assert route.call_count == 1
    assert checked_at(path) == past(10, 0)


# --- a 304 for a file that vanished meanwhile is followed by one full download ---------


def _vanish_then_304(path: Path) -> Callable[[httpx.Request], httpx.Response]:
    """First request: the cached file disappears while it is in flight, then 304."""
    responses = iter([httpx.Response(304), zipped()])

    def respond(request: httpx.Request) -> httpx.Response:
        path.unlink(missing_ok=True)
        return next(responses)

    return respond


@respx.mock
def test_304_for_a_vanished_file_downloads_it_in_full_once(tmp_path, monkeypatch) -> None:
    path = written(tmp_path / "NSE_symbols.txt", past(6, 55))
    route = respx.get(URL).mock(side_effect=_vanish_then_304(path))
    Clock(monkeypatch, past(10, 0))
    assert TradeSmart("TEST123", access_token="t").download_instruments("NSE", tmp_path) == path
    assert route.call_count == 2
    assert "If-Modified-Since" in route.calls[0].request.headers
    assert "If-Modified-Since" not in route.calls[1].request.headers
    assert path.read_text(encoding="utf-8") == SAMPLE
    assert checked_at(path) == past(10, 0)


@respx.mock
async def test_async_304_for_a_vanished_file_downloads_it_in_full_once(
    tmp_path, monkeypatch
) -> None:
    path = written(tmp_path / "NSE_symbols.txt", past(6, 55))
    route = respx.get(URL).mock(side_effect=_vanish_then_304(path))
    Clock(monkeypatch, past(10, 0))
    async with AsyncTradeSmart("TEST123", access_token="t") as client:
        assert await client.download_instruments("NSE", tmp_path) == path
    assert route.call_count == 2
    assert "If-Modified-Since" not in route.calls[1].request.headers
    assert path.read_text(encoding="utf-8") == SAMPLE


_REDIRECT = httpx.Response(302, headers={"Location": "https://cdn.example.test/NSE.zip"})
_REDIRECT_MESSAGE = "script master download was redirected (HTTP 302); redirects are never followed"


@respx.mock
def test_redirect_raises_a_parse_error_with_the_real_status(tmp_path) -> None:
    respx.get(URL).mock(return_value=_REDIRECT)
    with pytest.raises(ResponseParseError) as info:
        TradeSmart("TEST123", access_token="t").download_instruments("NSE", tmp_path)
    assert info.value.http_status == 302
    assert _REDIRECT_MESSAGE in str(info.value)
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_async_redirect_raises_a_parse_error_with_the_real_status(tmp_path) -> None:
    respx.get(URL).mock(return_value=_REDIRECT)
    async with AsyncTradeSmart("TEST123", access_token="t") as client:
        with pytest.raises(ResponseParseError) as info:
            await client.download_instruments("NSE", tmp_path)
    assert info.value.http_status == 302
    assert _REDIRECT_MESSAGE in str(info.value)
