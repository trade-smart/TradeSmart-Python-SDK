import io
import os
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from tradesmart import (
    AsyncTradeSmart,
    ClientConfig,
    ConfigurationError,
    Exchange,
    NotFoundError,
    TradeSmart,
)
from tradesmart.errors import NetworkError, ResponseParseError, TradeSmartError
from tradesmart.instruments import find, load

URL = "https://v2api.tradesmartonline.in/NSE_symbols.txt.zip"

# SYNTHETIC data: the real script-master layout is undocumented (an open question to TradeSmart).
SAMPLE = (
    "Exchange,Token,LotSize,Symbol,TradingSymbol,Instrument,TickSize\n"
    "NSE,22,1,ACC,ACC-EQ,EQ,0.05\n"
    "NSE,25,1,ADANIENT,ADANIENT-EQ,EQ,0.05\n"
)


def make_zip(*members: tuple[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in members:
            zf.writestr(name, text)
    return buf.getvalue()


@respx.mock
def test_download_extract_load_find(client, tmp_path):
    respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    path = client.download_instruments(Exchange.NSE, tmp_path)
    rows = load(path)
    assert path.name == "NSE_symbols.txt" and rows[0]["Token"] == "22"
    assert find(rows, TradingSymbol="ACC-EQ")[0]["Token"] == "22"
    assert find(rows, TradingSymbol="NOPE") == []


@respx.mock
def test_download_sends_no_auth_header_and_creates_dest(client, tmp_path):
    route = respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    path = client.download_instruments("NSE", tmp_path / "a" / "b")
    assert path.exists()
    assert "authorization" not in route.calls[0].request.headers


@respx.mock
def test_download_cached_same_day(client, tmp_path):
    route = respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    client.download_instruments("NSE", tmp_path)
    client.download_instruments("NSE", tmp_path)
    assert route.call_count == 1
    client.download_instruments("NSE", tmp_path, force=True)
    assert route.call_count == 2


@respx.mock
def test_download_refetches_stale_file(client, tmp_path):
    route = respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    path = client.download_instruments("NSE", tmp_path)
    old = path.stat().st_mtime - 3 * 86400
    os.utime(path, (old, old))
    client.download_instruments("NSE", tmp_path)
    assert route.call_count == 1  # stale, but the server was checked moments ago
    (tmp_path / "NSE_symbols.txt.checked").unlink()
    client.download_instruments("NSE", tmp_path)
    assert route.call_count == 2  # no recent check: the server is asked again


@respx.mock
def test_zip_slip_member_is_flattened(client, tmp_path):
    respx.get(URL).respond(200, content=make_zip(("../../evil.txt", SAMPLE)))
    out = client.download_instruments("NSE", tmp_path)
    assert out.parent == tmp_path and out.name == "NSE_symbols.txt"
    assert not (tmp_path.parent.parent / "evil.txt").exists()


@respx.mock
def test_zip_without_txt_is_parse_error(client, tmp_path):
    respx.get(URL).respond(200, content=make_zip(("readme.md", "x")))
    with pytest.raises(ResponseParseError):
        client.download_instruments("NSE", tmp_path)


@respx.mock
def test_zip_multiple_txt_prefers_exchange_prefix(client, tmp_path):
    respx.get(URL).respond(
        200, content=make_zip(("other.txt", "A,B\n1,2\n"), ("NSE_symbols.txt", SAMPLE))
    )
    path = client.download_instruments("NSE", tmp_path)
    assert load(path)[0]["Token"] == "22"


@respx.mock
def test_not_a_zip_is_parse_error(client, tmp_path):
    respx.get(URL).respond(200, content=b"<html>nope</html>")
    with pytest.raises(ResponseParseError):
        client.download_instruments("NSE", tmp_path)


@respx.mock
def test_http_404_raises_not_found(client, tmp_path):
    respx.get(URL).respond(404, text="missing")
    with pytest.raises(NotFoundError) as ei:
        client.download_instruments("NSE", tmp_path)
    assert ei.value.endpoint == "script_master"


@respx.mock
def test_uses_injected_http_client(tmp_path):
    respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    http = httpx.Client()
    c = TradeSmart("TEST123", access_token="t", http_client=http)
    assert c.download_instruments("NSE", tmp_path).exists()


def test_load_sniffs_pipe_delimiter_and_strips(tmp_path):
    p = Path(tmp_path) / "x.txt"
    p.write_text(" Token | Symbol \n 1 | AAA \n\n 2 | BBB \n", encoding="utf-8")
    rows = load(p)
    assert rows == [{"Token": "1", "Symbol": "AAA"}, {"Token": "2", "Symbol": "BBB"}]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the user's home (and so the default instrument cache) at a temp dir, on every OS."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows takes the home folder from here
    return tmp_path


@respx.mock
def test_download_defaults_to_user_cache_dir(client, home):
    respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    path = client.download_instruments("NSE")
    assert path == home / ".cache" / "tradesmart" / "NSE_symbols.txt" and path.exists()


@respx.mock
def test_instruments_returns_rows_downloaded_once_per_day(client, home):
    route = respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    rows = client.instruments(Exchange.NSE)
    assert rows == client.instruments("NSE")
    assert route.call_count == 1
    assert find(rows, TradingSymbol="ACC-EQ")[0]["Token"] == "22"


@respx.mock
async def test_async_instruments(home):
    route = respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    async with AsyncTradeSmart("TEST123", access_token="t") as c:
        rows = await c.instruments("NSE")
        assert await c.instruments("NSE") == rows
    assert rows[1]["TradingSymbol"] == "ADANIENT-EQ" and route.call_count == 1


@respx.mock
def test_instruments_rejects_unknown_exchange(client, home):
    route = respx.get(url__regex=r".*").respond(200, content=b"")
    with pytest.raises(ValueError, match="NSE"):
        client.instruments("../../evil")
    assert not route.called and not (home / ".cache").exists()


@respx.mock
async def test_async_download(tmp_path):
    respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    async with AsyncTradeSmart("TEST123", access_token="t", config=ClientConfig()) as c:
        path = await c.download_instruments(Exchange.NSE, tmp_path)
        again = await c.download_instruments(Exchange.NSE, tmp_path)
        rows = load(path)
    assert again == path and rows[0]["TradingSymbol"] == "ACC-EQ"
    assert len(respx.calls) == 1


@respx.mock
@pytest.mark.parametrize("bad", ["../../evil", "XYZ", ""])
def test_download_rejects_unknown_exchange(client, tmp_path, bad):
    route = respx.get(url__regex=r".*").respond(200, content=b"")
    with pytest.raises(ValueError, match="NSE") as info:
        client.download_instruments(bad, tmp_path)
    assert repr(bad) in str(info.value)
    assert not route.called
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_download_accepts_lowercase_exchange(client, tmp_path):
    respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    assert client.download_instruments("nse", tmp_path).name == "NSE_symbols.txt"


@respx.mock
async def test_async_download_rejects_unknown_exchange(tmp_path):
    route = respx.get(url__regex=r".*").respond(200, content=b"")
    async with AsyncTradeSmart("TEST123", access_token="t", config=ClientConfig()) as aclient:
        with pytest.raises(ValueError):
            await aclient.download_instruments("../../evil", tmp_path)
    assert not route.called


@respx.mock
async def test_async_instruments_does_file_work_off_the_event_loop(home, monkeypatch):
    # Unzipping, writing and parsing a large script master must not block the loop.
    import threading

    import tradesmart.async_client as async_client_module
    import tradesmart.instruments as instruments_module

    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def spy(name, fn):
        def wrapper(*args, **kwargs):
            seen[name] = threading.get_ident()
            return fn(*args, **kwargs)

        return wrapper

    for name in ("_extract_text_member", "_write_atomic"):
        monkeypatch.setattr(instruments_module, name, spy(name, getattr(instruments_module, name)))
    monkeypatch.setattr(async_client_module, "load", spy("load", async_client_module.load))

    big = SAMPLE + "".join(f"NSE,{i},1,S{i},S{i}-EQ,EQ,0.05\n" for i in range(100, 20100))
    respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", big)))
    async with AsyncTradeSmart("TEST123", access_token="t") as c:
        rows = await c.instruments("NSE")
    assert len(rows) == 20002
    assert set(seen) == {"_extract_text_member", "_write_atomic", "load"}
    assert all(ident != loop_thread for ident in seen.values()), seen


# --- every httpx request failure maps to an SDK error (review 2026-11-10 1b) -------------


def corrupt_gzip(request: httpx.Request) -> httpx.Response:
    """A body that claims gzip but is not: httpx fails while decoding it."""
    return httpx.Response(
        200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip at all")
    )


@respx.mock
def test_corrupt_gzip_body_is_a_network_error(client, tmp_path):
    respx.get(URL).mock(side_effect=corrupt_gzip)
    with pytest.raises(NetworkError, match="script_master") as info:
        client.download_instruments("NSE", tmp_path)
    assert isinstance(info.value, TradeSmartError)
    assert isinstance(info.value.__cause__, httpx.DecodingError)
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_async_corrupt_gzip_body_is_a_network_error(tmp_path):
    respx.get(URL).mock(side_effect=corrupt_gzip)
    async with AsyncTradeSmart("TEST123", access_token="t") as c:
        with pytest.raises(NetworkError, match="script_master"):
            await c.download_instruments("NSE", tmp_path)


@respx.mock
def test_too_many_redirects_is_a_network_error(tmp_path):
    respx.get(URL).mock(side_effect=httpx.TooManyRedirects("redirect loop"))
    with pytest.raises(NetworkError, match="TooManyRedirects"):
        TradeSmart("TEST123", access_token="t").download_instruments("NSE", tmp_path)


# --- a byte-order mark never prefixes the first header (1c) ------------------------------


def test_load_strips_a_utf8_byte_order_mark(tmp_path):
    p = tmp_path / "NFO_symbols.txt"
    p.write_text("Exchange,Token,TradingSymbol\nNFO,35001,NIFTY-FUT\n", encoding="utf-8-sig")
    assert p.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = load(p)
    assert list(rows[0]) == ["Exchange", "Token", "TradingSymbol"]
    assert find(rows, Exchange="NFO") == [
        {"Exchange": "NFO", "Token": "35001", "TradingSymbol": "NIFTY-FUT"}
    ]


def test_load_non_utf8_file_raises_parse_error(tmp_path):
    bad = tmp_path / "NSE_symbols.txt"
    bad.write_bytes(b"Exchange,Token\nNSE,\xff\xfe22\n")
    with pytest.raises(ResponseParseError, match="UTF-8"):
        load(bad)


# --- redirects are never followed, also on an injected follow_redirects=True client -------


class _RedirectCounter:
    """Redirects the script master to another host (which would serve a valid zip)."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "other.example":
            return httpx.Response(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
        return httpx.Response(
            302, headers={"Location": "https://other.example/NSE_symbols.txt.zip"}
        )


def test_script_master_redirect_is_not_followed_by_an_injected_client(tmp_path):
    counter = _RedirectCounter()
    http = httpx.Client(transport=httpx.MockTransport(counter), follow_redirects=True)
    client = TradeSmart("TEST123", access_token="t", http_client=http)
    with pytest.raises(ResponseParseError):
        client.download_instruments("NSE", tmp_path)
    assert counter.seen == ["v2api.tradesmartonline.in/NSE_symbols.txt.zip"]


async def test_async_script_master_redirect_is_not_followed_by_an_injected_client(tmp_path):
    counter = _RedirectCounter()
    http = httpx.AsyncClient(transport=httpx.MockTransport(counter), follow_redirects=True)
    async with AsyncTradeSmart("TEST123", access_token="t", http_client=http) as client:
        with pytest.raises(ResponseParseError):
            await client.download_instruments("NSE", tmp_path)
    assert counter.seen == ["v2api.tradesmartonline.in/NSE_symbols.txt.zip"]


# --- ClientConfig.instruments_dir: where the script master is cached when no dest_dir is given ---


@respx.mock
def test_instruments_dir_replaces_the_user_cache_dir(home, tmp_path):
    respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    folder = tmp_path / "private"
    with TradeSmart("TEST123", access_token="t", config=ClientConfig(instruments_dir=folder)) as c:
        path = c.download_instruments("NSE")
        rows = c.instruments("NSE")
    assert path == folder / "NSE_symbols.txt" and path.exists()
    assert rows[0]["TradingSymbol"] == "ACC-EQ"
    assert not (home / ".cache").exists()


@respx.mock
def test_instruments_dir_accepts_a_str_and_dest_dir_still_wins(home, tmp_path):
    respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    config = ClientConfig(instruments_dir=str(tmp_path / "private"))
    with TradeSmart("TEST123", access_token="t", config=config) as c:
        assert c.download_instruments("NSE") == tmp_path / "private" / "NSE_symbols.txt"
        explicit = c.download_instruments("NSE", tmp_path / "explicit")
    assert explicit == tmp_path / "explicit" / "NSE_symbols.txt" and explicit.exists()
    assert not (home / ".cache").exists()


@respx.mock
async def test_async_instruments_dir_replaces_the_user_cache_dir(home, tmp_path):
    respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    folder = tmp_path / "private"
    config = ClientConfig(instruments_dir=folder)
    async with AsyncTradeSmart("TEST123", access_token="t", config=config) as c:
        path = await c.download_instruments("NSE")
        rows = await c.instruments("NSE")
    assert path == folder / "NSE_symbols.txt" and path.exists()
    assert rows[0]["TradingSymbol"] == "ACC-EQ"
    assert not (home / ".cache").exists()


def test_instruments_dir_default_and_validation(tmp_path):
    assert ClientConfig().instruments_dir is None
    assert ClientConfig(instruments_dir=tmp_path).instruments_dir == tmp_path
    assert ClientConfig(instruments_dir="cache").instruments_dir == "cache"
    for bad in ("", "   ", 5, b"/tmp"):
        with pytest.raises(ConfigurationError, match="instruments_dir"):
            ClientConfig(instruments_dir=bad)  # type: ignore[arg-type]


@respx.mock
def test_instruments_dir_expands_the_user_folder(home):
    respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    config = ClientConfig(instruments_dir="~/instruments")
    with TradeSmart("TEST123", access_token="t", config=config) as c:
        path = c.download_instruments("NSE")
    assert path == home / "instruments" / "NSE_symbols.txt" and path.exists()
