"""``TradeSmartStream`` / ``AsyncTradeSmartStream``: ``from_env()`` and ``from_client()``."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import fixture
from tests.integration.ws_server import MockWsServer, ThreadedMockWsServer
from tradesmart import (
    AccessToken,
    AsyncTradeSmart,
    AuthenticationError,
    ClientConfig,
    ConfigurationError,
    TradeSmart,
    _token_store,
)
from tradesmart.models.base import IST
from tradesmart.stream import AsyncTradeSmartStream, StreamState, TradeSmartStream

HINT = "python -m tradesmart login"
FILE_TOKEN = "file-access-token"
AnyStream = TradeSmartStream | AsyncTradeSmartStream
STREAMS = [TradeSmartStream, AsyncTradeSmartStream]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TRADESMART_CLIENT_ID",
        "TRADESMART_ACCESS_TOKEN",
        "TRADESMART_APP_KEY",
        "TRADESMART_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def _token(*, expired: bool = False) -> AccessToken:
    issued = datetime.now(IST) - (timedelta(days=3) if expired else timedelta(minutes=1))
    data = {**fixture("gen_acs_tok_success"), "access_token": FILE_TOKEN}
    token = AccessToken.from_response(data, issued_at=issued)
    if not expired and token.is_expired():  # pragma: no cover - only within a minute of 06:00
        token = AccessToken.from_response(data)
    return token


def _save(path: Path, *, client_id: str = "TEST123", expired: bool = False) -> AccessToken:
    token = _token(expired=expired)
    _token_store.save(client_id, token, path)
    return token


def _inner(stream: AnyStream) -> AsyncTradeSmartStream:
    return stream._stream if isinstance(stream, TradeSmartStream) else stream


def _identity(stream: AnyStream) -> tuple[str, str]:
    inner = _inner(stream)
    return inner._client_id, inner._token.get_secret_value()


# -- from_env -------------------------------------------------------------------------


@pytest.mark.parametrize("cls", STREAMS)
def test_from_env_uses_the_saved_login(_no_saved_login: Path, cls: type[AnyStream]) -> None:
    _save(_no_saved_login)
    stream = cls.from_env()
    assert _identity(stream) == ("TEST123", FILE_TOKEN)
    assert FILE_TOKEN not in repr(stream)


@pytest.mark.parametrize("cls", STREAMS)
def test_from_env_environment_wins_over_the_saved_login(
    _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch, cls: type[AnyStream]
) -> None:
    _save(_no_saved_login)
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", "test-access-token")
    assert _identity(cls.from_env()) == ("TEST123", "test-access-token")


@pytest.mark.parametrize("cls", STREAMS)
def test_from_env_env_token_needs_the_env_client_id(
    _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch, cls: type[AnyStream]
) -> None:
    _save(_no_saved_login)
    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", "test-access-token")
    with pytest.raises(ConfigurationError, match="TRADESMART_CLIENT_ID"):
        cls.from_env()


@pytest.mark.parametrize("cls", STREAMS)
def test_from_env_saved_login_of_another_client_id(
    _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch, cls: type[AnyStream]
) -> None:
    _save(_no_saved_login, client_id="OTHER1")
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    with pytest.raises(ConfigurationError, match=HINT) as info:
        cls.from_env()
    assert "different client id" in str(info.value)
    assert FILE_TOKEN not in str(info.value)


@pytest.mark.parametrize("cls", STREAMS)
def test_from_env_expired_saved_login(_no_saved_login: Path, cls: type[AnyStream]) -> None:
    _save(_no_saved_login, expired=True)
    with pytest.raises(AuthenticationError, match=HINT) as info:
        cls.from_env()
    assert "expired" in str(info.value) and FILE_TOKEN not in str(info.value)


@pytest.mark.parametrize("cls", STREAMS)
def test_from_env_expired_saved_login_is_used_when_the_check_is_off(
    _no_saved_login: Path, cls: type[AnyStream]
) -> None:
    _save(_no_saved_login, expired=True)
    stream = cls.from_env(config=ClientConfig(check_token_expiry=False))
    assert _identity(stream) == ("TEST123", FILE_TOKEN)


@pytest.mark.parametrize("cls", STREAMS)
def test_from_env_nothing_configured(_no_saved_login: Path, cls: type[AnyStream]) -> None:
    with pytest.raises(ConfigurationError, match=HINT):
        cls.from_env()


@pytest.mark.parametrize("cls", STREAMS)
def test_from_env_app_key_only_cannot_log_a_stream_in(
    _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch, cls: type[AnyStream]
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.setenv("TRADESMART_APP_KEY", "test-app-key")
    monkeypatch.setenv("TRADESMART_SECRET_KEY", "test-secret-key")
    with pytest.raises(ConfigurationError, match=HINT) as info:
        cls.from_env()
    assert "test-app-key" not in str(info.value) and "test-secret-key" not in str(info.value)


def test_from_env_forwards_config_and_sync_callbacks(_no_saved_login: Path) -> None:
    _save(_no_saved_login)
    config = ClientConfig(heartbeat_interval=3)
    callbacks: dict[str, Any] = {
        name: (lambda *args: None)
        for name in (
            "on_touchline",
            "on_depth",
            "on_order_update",
            "on_position",
            "on_connect",
            "on_disconnect",
            "on_error",
            "on_message",
            "on_reconnect",
        )
    }
    stream = TradeSmartStream.from_env(config=config, **callbacks)
    assert stream._stream._config is config
    for name, callback in callbacks.items():
        assert getattr(stream, f"_{name}") is callback


def test_from_env_forwards_config_and_async_callbacks(_no_saved_login: Path) -> None:
    _save(_no_saved_login)
    config = ClientConfig(heartbeat_interval=3)

    def on_message(msg: object) -> None:
        return None

    def on_state_change(state: StreamState) -> None:
        return None

    stream = AsyncTradeSmartStream.from_env(
        config=config, on_message=on_message, on_state_change=on_state_change
    )
    assert stream._config is config
    assert stream._on_message is on_message
    assert stream._on_state_change is on_state_change


# -- expiry before connecting ---------------------------------------------------------


async def test_expired_access_token_is_rejected_before_connecting() -> None:
    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream(
            "TEST123", _token(expired=True), config=ClientConfig(ws_url=srv.url)
        )
        with pytest.raises(AuthenticationError, match=HINT) as info:
            await stream.connect()
        assert "06:00 IST" in str(info.value) and FILE_TOKEN not in str(info.value)
        assert srv.connections == 0
        assert stream.state is StreamState.CLOSED


def test_sync_expired_access_token_is_rejected_before_connecting(
    threaded_server: ThreadedMockWsServer,
) -> None:
    stream = TradeSmartStream(
        "TEST123", _token(expired=True), config=ClientConfig(ws_url=threaded_server.url)
    )
    with pytest.raises(AuthenticationError, match=HINT):
        stream.start()
    assert threaded_server.connections == 0


async def test_expired_access_token_connects_when_the_check_is_off() -> None:
    async with MockWsServer() as srv:
        config = ClientConfig(ws_url=srv.url, check_token_expiry=False)
        async with AsyncTradeSmartStream("TEST123", _token(expired=True), config=config) as s:
            assert s.state is StreamState.CONNECTED
        assert srv.received[0]["accesstoken"] == FILE_TOKEN


# -- from_client ----------------------------------------------------------------------


@pytest.mark.parametrize("cls", STREAMS)
@pytest.mark.parametrize("client_cls", [TradeSmart, AsyncTradeSmart])
def test_from_client_uses_the_client_id_and_token(
    cls: type[AnyStream], client_cls: type[TradeSmart]
) -> None:
    config = ClientConfig(heartbeat_interval=3)
    token = _token()
    client = client_cls("TEST123", access_token=token, config=config)
    stream = cls.from_client(client)
    inner = _inner(stream)
    assert _identity(stream) == ("TEST123", FILE_TOKEN)
    assert inner._access_token is token  # keeps the expiry for the connect check
    assert inner._config is config  # the client's config by default


@pytest.mark.parametrize("cls", STREAMS)
def test_from_client_with_a_plain_token_and_own_config(cls: type[AnyStream]) -> None:
    client = TradeSmart("TEST123", access_token="test-access-token")
    config = ClientConfig(heartbeat_interval=3)
    stream = cls.from_client(client, config=config)
    assert _identity(stream) == ("TEST123", "test-access-token")
    assert _inner(stream)._config is config


def test_from_client_forwards_callbacks() -> None:
    client = TradeSmart("TEST123", access_token="test-access-token")

    def on_reconnect() -> None:
        return None

    def on_message(msg: object) -> None:
        return None

    assert TradeSmartStream.from_client(client, on_reconnect=on_reconnect)._on_reconnect is (
        on_reconnect
    )
    assert AsyncTradeSmartStream.from_client(client, on_message=on_message)._on_message is (
        on_message
    )


@pytest.mark.parametrize("cls", STREAMS)
@pytest.mark.parametrize("client_cls", [TradeSmart, AsyncTradeSmart])
def test_from_client_without_a_token(cls: type[AnyStream], client_cls: type[TradeSmart]) -> None:
    with pytest.raises(AuthenticationError, match="No access token set"):
        cls.from_client(client_cls("TEST123"))


@pytest.mark.parametrize("cls", STREAMS)
def test_from_client_with_an_expired_token(cls: type[AnyStream]) -> None:
    client = TradeSmart("TEST123", access_token=_token(expired=True))
    with pytest.raises(AuthenticationError, match=HINT) as info:
        cls.from_client(client)
    assert "expired" in str(info.value) and FILE_TOKEN not in str(info.value)
