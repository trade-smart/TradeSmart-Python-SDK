"""The SDK version is always identifiable in User-Agent (review 2026-11-10 C4)."""

from __future__ import annotations

from dataclasses import replace

import pytest
import respx

from tests.conftest import BASE
from tests.integration.test_instruments import SAMPLE, URL, make_zip
from tests.integration.ws_server import MockWsServer, wait_until_sync
from tradesmart import (
    AsyncTradeSmart,
    AsyncTradeSmartStream,
    ClientConfig,
    ConfigurationError,
    TradeSmart,
    TradeSmartStream,
    __version__,
)

TOKEN = f"tradesmart-python/{__version__}"
CASES = [
    pytest.param(None, TOKEN, id="default"),
    pytest.param("my-algo/1.0", f"my-algo/1.0 {TOKEN}", id="custom"),
    pytest.param(f"{TOKEN} (my-algo)", f"{TOKEN} (my-algo)", id="custom-with-token"),
]


def _config(agent: str | None, **kwargs: object) -> ClientConfig:
    config = ClientConfig(**kwargs)  # type: ignore[arg-type]
    return config if agent is None else replace(config, user_agent=agent)


@pytest.mark.parametrize("agent,expected", CASES)
@respx.mock
def test_rest_user_agent(agent, expected) -> None:
    route = respx.post(f"{BASE}/Limits").respond(200, json={"stat": "Ok"})
    TradeSmart("TEST123", access_token="t", config=_config(agent)).margins()
    assert route.calls.last.request.headers["User-Agent"] == expected


@pytest.mark.parametrize("agent,expected", CASES)
@respx.mock
async def test_async_rest_user_agent(agent, expected) -> None:
    route = respx.post(f"{BASE}/Limits").respond(200, json={"stat": "Ok"})
    async with AsyncTradeSmart("TEST123", access_token="t", config=_config(agent)) as client:
        await client.margins()
    assert route.calls.last.request.headers["User-Agent"] == expected


@pytest.mark.parametrize("agent,expected", CASES)
@respx.mock
def test_script_master_user_agent(agent, expected, tmp_path) -> None:
    route = respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    TradeSmart("TEST123", access_token="t", config=_config(agent)).download_instruments(
        "NSE", tmp_path
    )
    assert route.calls.last.request.headers["User-Agent"] == expected


@pytest.mark.parametrize("agent,expected", CASES)
@respx.mock
async def test_async_script_master_user_agent(agent, expected, tmp_path) -> None:
    route = respx.get(URL).respond(200, content=make_zip(("NSE_symbols.txt", SAMPLE)))
    async with AsyncTradeSmart("TEST123", access_token="t", config=_config(agent)) as client:
        await client.download_instruments("NSE", tmp_path)
    assert route.calls.last.request.headers["User-Agent"] == expected


@pytest.mark.parametrize("agent,expected", CASES)
async def test_websocket_handshake_user_agent(agent, expected) -> None:
    async with MockWsServer() as srv:
        config = _config(agent, ws_url=srv.url, heartbeat_interval=5, ws_auth_timeout=1.0)
        async with AsyncTradeSmartStream("TEST123", "tok", config=config):
            pass
    assert srv.user_agents == [expected]


@pytest.mark.parametrize("agent,expected", CASES[1:2])
def test_sync_websocket_handshake_user_agent(threaded_server, agent, expected) -> None:
    config = _config(agent, ws_url=threaded_server.url, heartbeat_interval=5, ws_auth_timeout=1.0)
    s = TradeSmartStream("TEST123", "tok", config=config)
    s.start()
    try:
        wait_until_sync(lambda: threaded_server.user_agents)
    finally:
        s.stop()
    assert threaded_server.user_agents == [expected]


@pytest.mark.parametrize(
    "agent",
    ["myapp\r\nX-Evil: 1", "a\tb", "a\nb", "a\rb", "a\x00b", "a\x1fb", "a\x7fb"],
    ids=["crlf-header", "tab", "lf", "cr", "nul", "us", "del"],
)
def test_user_agent_with_control_characters_is_rejected(agent) -> None:
    with pytest.raises(ConfigurationError, match="user_agent") as info:
        ClientConfig(user_agent=agent)
    assert agent not in str(info.value) and "Evil" not in str(info.value)


def test_normal_user_agent_is_accepted() -> None:
    assert ClientConfig(user_agent="my-algo/1.0 (desk 7)").user_agent == "my-algo/1.0 (desk 7)"
