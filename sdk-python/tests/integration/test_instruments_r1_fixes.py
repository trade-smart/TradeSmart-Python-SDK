"""Round-1 review fixes for the script master: CSV errors and ``~`` in ``dest_dir``."""

from __future__ import annotations

from pathlib import Path

import pytest
import respx

from tests.integration.test_instruments import SAMPLE, URL, home, make_zip  # noqa: F401
from tradesmart import AsyncTradeSmart, TradeSmart
from tradesmart.errors import ResponseParseError
from tradesmart.instruments import load

HEADER = "Exchange,Token,Symbol,TradingSymbol\n"


def test_a_field_over_the_csv_limit_is_a_response_parse_error(tmp_path: Path) -> None:
    # One stray quote turns the rest of the file into one field over csv's 131 072 limit.
    path = tmp_path / "NSE_symbols.txt"
    path.write_text(HEADER + 'NSE,22,"ACC,' + "x" * 200_000 + "\n", encoding="utf-8")
    with pytest.raises(ResponseParseError, match="NSE_symbols.txt.*not a valid script master"):
        load(path)


def test_a_nul_byte_is_a_response_parse_error_on_every_python(tmp_path: Path) -> None:
    path = tmp_path / "NSE_symbols.txt"
    path.write_text(HEADER + "NSE,22,AB\x00C,ACC-EQ\n", encoding="utf-8")
    with pytest.raises(ResponseParseError, match="NSE_symbols.txt.*not a valid script master"):
        load(path)


def test_instruments_maps_a_csv_error(client: TradeSmart, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "NSE_symbols.txt"
    path.write_text(HEADER + "NSE,22,AB\x00C,ACC-EQ\n", encoding="utf-8")
    monkeypatch.setattr(client._instruments, "download", lambda *a, **k: path)
    with pytest.raises(ResponseParseError):
        client.instruments("NSE")


async def test_async_instruments_maps_a_csv_error(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "NSE_symbols.txt"
    path.write_text(HEADER + "NSE,22,AB\x00C,ACC-EQ\n", encoding="utf-8")

    async def download(*args: object, **kwargs: object) -> Path:
        return path

    async with AsyncTradeSmart("TEST123", access_token="t") as client:
        monkeypatch.setattr(client._instruments, "download", download)
        with pytest.raises(ResponseParseError):
            await client.instruments("NSE")


@respx.mock
def test_dest_dir_expands_the_user_folder(home: Path) -> None:  # noqa: F811
    respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    with TradeSmart("TEST123", access_token="t") as client:
        path = client.download_instruments("NSE", "~/data")
    assert path == home / "data" / "NSE_symbols.txt" and path.exists()


@respx.mock
async def test_async_dest_dir_expands_the_user_folder(home: Path) -> None:  # noqa: F811
    respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    async with AsyncTradeSmart("TEST123", access_token="t") as client:
        path = await client.download_instruments("NSE", "~/data")
    assert path == home / "data" / "NSE_symbols.txt" and path.exists()
