"""``from_env()`` falls back to the login saved by ``python -m tradesmart login``."""

from __future__ import annotations

import traceback
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import respx

from tests.conftest import BASE, fixture
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

HINT = "python -m tradesmart login"
FILE_TOKEN = "file-access-token"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TRADESMART_CLIENT_ID",
        "TRADESMART_ACCESS_TOKEN",
        "TRADESMART_APP_KEY",
        "TRADESMART_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def _save(path: Path, *, client_id: str = "TEST123", expired: bool = False) -> AccessToken:
    issued = datetime.now(IST) - (timedelta(days=3) if expired else timedelta(minutes=1))
    data = {**fixture("gen_acs_tok_success"), "access_token": FILE_TOKEN}
    token = AccessToken.from_response(data, issued_at=issued)
    if not expired and token.is_expired():  # pragma: no cover - only within a minute of 06:00
        token = AccessToken.from_response(data)
    _token_store.save(client_id, token, path)
    return token


def _bearer_used(client: TradeSmart) -> str:
    with respx.mock(assert_all_mocked=True) as router:
        route = router.post(f"{BASE}/UserDetails").respond(
            200, json=fixture("user_details_success")
        )
        client.profile()
    return str(route.calls.last.request.headers["Authorization"])


def test_file_token_and_client_id_are_used(_no_saved_login: Path) -> None:
    saved = _save(_no_saved_login)
    with TradeSmart.from_env() as client:
        assert client.client_id == "TEST123"
        assert _bearer_used(client) == f"Bearer {FILE_TOKEN}"
        assert FILE_TOKEN not in repr(client)
    assert saved.expires_at > datetime.now(IST)


async def test_async_client_uses_the_file_token(_no_saved_login: Path) -> None:
    _save(_no_saved_login)
    with respx.mock(assert_all_mocked=True) as router:
        route = router.post(f"{BASE}/UserDetails").respond(
            200, json=fixture("user_details_success")
        )
        async with AsyncTradeSmart.from_env() as client:
            assert client.client_id == "TEST123"
            await client.profile()
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {FILE_TOKEN}"


def test_env_token_wins_over_the_file(
    _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(_no_saved_login)
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", "test-access-token")
    with TradeSmart.from_env() as client:
        assert _bearer_used(client) == "Bearer test-access-token"


@pytest.mark.parametrize("cls", [TradeSmart, AsyncTradeSmart])
def test_env_token_is_never_paired_with_the_file_client_id(
    _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch, cls: type[TradeSmart]
) -> None:
    _save(_no_saved_login)
    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", "test-access-token")
    with pytest.raises(ConfigurationError) as info:
        cls.from_env()
    message = str(info.value)
    assert "TRADESMART_ACCESS_TOKEN is set but TRADESMART_CLIENT_ID is not" in message
    assert "test-access-token" not in message and FILE_TOKEN not in message


def test_env_values_are_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "ABC123 ")
    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", "tok\n")
    monkeypatch.setenv("TRADESMART_APP_KEY", " test-app-key\t")
    monkeypatch.setenv("TRADESMART_SECRET_KEY", "test-secret-key \r\n")
    with TradeSmart.from_env() as client:
        assert client.client_id == "ABC123"
        assert _bearer_used(client) == "Bearer tok"
        assert client._creds.require_keys() == ("test-app-key", "test-secret-key")


async def test_async_env_values_are_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "ABC123 ")
    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", "tok\n")
    with respx.mock(assert_all_mocked=True) as router:
        route = router.post(f"{BASE}/UserDetails").respond(
            200, json=fixture("user_details_success")
        )
        async with AsyncTradeSmart.from_env() as client:
            assert client.client_id == "ABC123"
            await client.profile()
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok"


def test_env_client_id_with_a_trailing_space_matches_the_saved_login(
    _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(_no_saved_login)
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123 ")
    with TradeSmart.from_env() as client:
        assert client.client_id == "TEST123"
        assert _bearer_used(client) == f"Bearer {FILE_TOKEN}"


@pytest.mark.parametrize("cls", [TradeSmart, AsyncTradeSmart])
def test_blank_env_values_count_as_unset(
    _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch, cls: type[TradeSmart]
) -> None:
    _save(_no_saved_login)
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "  ")
    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", " \n")
    monkeypatch.setenv("TRADESMART_APP_KEY", "\t")
    monkeypatch.setenv("TRADESMART_SECRET_KEY", " ")
    client = cls.from_env()  # no "ACCESS_TOKEN is set but CLIENT_ID is not" error
    assert client.client_id == "TEST123"  # from the saved login
    with pytest.raises(ConfigurationError, match="app_key and secret_key are required"):
        client._creds.require_keys()


def test_blank_env_app_key_does_not_allow_a_programmatic_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.setenv("TRADESMART_APP_KEY", "   ")
    with pytest.raises(ConfigurationError, match="No access token"):
        TradeSmart.from_env()


def test_env_client_id_matching_the_file_uses_the_file_token(
    _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(_no_saved_login)
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "test123")  # same account, other case
    with TradeSmart.from_env() as client:
        assert client.client_id == "test123"  # the environment wins for the client id
        assert _bearer_used(client) == f"Bearer {FILE_TOKEN}"


def test_file_token_of_another_client_id_is_not_used(
    _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(_no_saved_login, client_id="OTHER1")
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    with pytest.raises(ConfigurationError, match=HINT) as info:
        TradeSmart.from_env()
    assert "different client id" in str(info.value)
    assert FILE_TOKEN not in str(info.value)


def test_expired_file_token_is_rejected_before_any_request(_no_saved_login: Path) -> None:
    _save(_no_saved_login, expired=True)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        with pytest.raises(AuthenticationError, match=HINT) as info:
            TradeSmart.from_env()
        with pytest.raises(AuthenticationError, match=HINT):
            AsyncTradeSmart.from_env()
    assert not router.calls
    message = str(info.value)
    assert "expired" in message and "06:00 IST" in message
    assert FILE_TOKEN not in message


def test_expired_file_token_is_used_when_the_expiry_check_is_off(_no_saved_login: Path) -> None:
    _save(_no_saved_login, expired=True)
    with TradeSmart.from_env(config=ClientConfig(check_token_expiry=False)) as client:
        assert _bearer_used(client) == f"Bearer {FILE_TOKEN}"


def test_file_token_expiring_later_raises_with_the_hint(
    _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(_no_saved_login)
    client = TradeSmart.from_env()
    future = datetime.now(IST) + timedelta(days=2)
    monkeypatch.setattr(AccessToken, "is_expired", lambda self, now=None: future >= self.expires_at)
    with pytest.raises(AuthenticationError, match=HINT):
        client.profile()


def test_nothing_configured(_no_saved_login: Path) -> None:
    with pytest.raises(ConfigurationError, match=HINT) as info:
        TradeSmart.from_env()
    assert "TRADESMART_CLIENT_ID" in str(info.value)
    with pytest.raises(ConfigurationError, match=HINT):
        AsyncTradeSmart.from_env()


def test_client_id_without_any_token(
    _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    with pytest.raises(ConfigurationError, match=HINT) as info:
        TradeSmart.from_env()
    assert "TRADESMART_ACCESS_TOKEN" in str(info.value)


def test_app_keys_without_token_still_allow_a_programmatic_login(
    _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.setenv("TRADESMART_APP_KEY", "test-app-key")
    monkeypatch.setenv("TRADESMART_SECRET_KEY", "test-secret-key")
    with TradeSmart.from_env() as client:
        assert client.login_url().endswith("?client_id=test-app-key")
        with pytest.raises(AuthenticationError, match=HINT):
            client.profile()  # no token yet


@pytest.mark.parametrize("cls", [TradeSmart, AsyncTradeSmart])
def test_expired_saved_login_with_app_keys_explains_the_missing_token(
    _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch, cls: type[TradeSmart]
) -> None:
    _save(_no_saved_login, expired=True)
    monkeypatch.setenv("TRADESMART_APP_KEY", "test-app-key")
    client = cls.from_env()  # a programmatic login stays possible
    assert client.client_id == "TEST123"
    with pytest.raises(AuthenticationError, match=HINT) as info:
        client._creds.bearer()
    message = str(info.value)
    assert "expired" in message and str(_no_saved_login) in message
    assert FILE_TOKEN not in message


@pytest.mark.parametrize(
    "content", ["{broken secret-value", '{"client_id": "TEST123", "access_token": "secret-value"}']
)
def test_unusable_token_file_is_reported_without_its_contents(
    _no_saved_login: Path, content: str
) -> None:
    _no_saved_login.write_text(content, encoding="utf-8")
    _no_saved_login.chmod(0o600)
    with pytest.raises(ConfigurationError, match=HINT) as info:
        TradeSmart.from_env()
    e = info.value
    rendered = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    assert str(_no_saved_login) in str(e)
    assert "secret-value" not in rendered
