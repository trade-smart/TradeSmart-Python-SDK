"""Live, read-only: script masters of NSE, NFO and MCX (F10). Opt-in; see README.md."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tradesmart import TradeSmart
from tradesmart.config import header_user_agent
from tradesmart.instruments import script_master_url

pytestmark = pytest.mark.live

EXCHANGES = [
    pytest.param("NSE", id="nse", marks=pytest.mark.nse),
    pytest.param("NFO", id="nfo", marks=pytest.mark.nfo),
    pytest.param("MCX", id="mcx", marks=pytest.mark.mcx),
]


@pytest.mark.parametrize("exchange", EXCHANGES)
def test_script_master_rows_and_columns(
    shared_client: TradeSmart, exchange: str, observe: Callable[..., object]
) -> None:
    path = shared_client.download_instruments(exchange)
    rows = shared_client.instruments(exchange)
    columns = list(rows[0]) if rows else []
    observe(
        exchange=exchange,
        file=path.name,
        size_bytes=path.stat().st_size,
        rows=len(rows),
        columns=columns,
        first_row=rows[0] if rows else None,
        instruments=sorted({r.get("Instrument", "") for r in rows}),
    )
    assert rows, f"{exchange} script master has no rows"
    # The SDK's contract finders rely on these header names; a change here breaks them.
    for column in ("Token", "TradingSymbol"):
        assert column in columns, f"{exchange} script master lacks the {column!r} column"


@pytest.mark.parametrize("exchange", EXCHANGES)
def test_script_master_response_headers(
    shared_client: TradeSmart, exchange: str, observe: Callable[..., object]
) -> None:
    config = shared_client.config
    url = script_master_url(exchange, base_url=config.script_master_base_url)
    response = shared_client._transport.http_client.get(
        url, headers={"User-Agent": header_user_agent(config)}
    )
    headers = response.headers
    observe(
        exchange=exchange,
        status=response.status_code,
        last_modified=headers.get("Last-Modified"),
        etag=headers.get("ETag"),
        content_type=headers.get("Content-Type"),
        content_length=headers.get("Content-Length"),
        cache_control=headers.get("Cache-Control"),
    )
    assert response.status_code == 200
