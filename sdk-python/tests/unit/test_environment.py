"""The test-tree environment helper: selecting a non-production environment from variables."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests._environment import (
    environment_client,
    environment_config,
    environment_name,
    token_file_for,
)
from tradesmart import ClientConfig

OTHER = {
    "TRADESMART_ENV": "stage",
    "TRADESMART_ENV_BASE_URL": "https://rest.example.test/api",
    "TRADESMART_ENV_WS_URL": "wss://ws.example.test/ws/",
    "TRADESMART_ENV_AUTHORIZE_URL": "https://rest.example.test/authorize",
}


@pytest.fixture(autouse=True)
def _clean_token_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TRADESMART_TOKEN_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "cfg"))


@pytest.mark.parametrize("environ", [{}, {"TRADESMART_ENV": ""}, {"TRADESMART_ENV": "  "}])
def test_unset_or_blank_is_production(environ: dict[str, str]) -> None:
    assert environment_name(environ) == "production"


def test_name_is_stripped_and_lowercased() -> None:
    assert environment_name({"TRADESMART_ENV": " Stage "}) == "stage"


def test_production_config_is_the_default() -> None:
    assert environment_config({}) == ClientConfig()
    assert environment_config({"TRADESMART_ENV": "production"}) == ClientConfig()


def test_production_keeps_base_url_override() -> None:
    config = environment_config({"TRADESMART_BASE_URL": "https://x.example.test/v"})
    assert config.base_url == "https://x.example.test/v"
    assert config.ws_url == ClientConfig().ws_url


def test_other_environment_uses_its_three_urls() -> None:
    config = environment_config(OTHER)
    assert config.base_url == OTHER["TRADESMART_ENV_BASE_URL"]
    assert config.ws_url == OTHER["TRADESMART_ENV_WS_URL"]
    assert config.authorize_url == OTHER["TRADESMART_ENV_AUTHORIZE_URL"]
    assert config.script_master_base_url == ClientConfig().script_master_base_url


def test_other_environment_script_master_override() -> None:
    environ = {**OTHER, "TRADESMART_ENV_SCRIPT_MASTER_URL": "https://sm.example.test"}
    assert environment_config(environ).script_master_base_url == "https://sm.example.test"


def test_other_environment_ignores_production_base_url() -> None:
    environ = {**OTHER, "TRADESMART_BASE_URL": "https://v2api.example.test/prod"}
    assert environment_config(environ).base_url == OTHER["TRADESMART_ENV_BASE_URL"]


@pytest.mark.parametrize("missing", [k for k in OTHER if k != "TRADESMART_ENV"])
def test_environment_requires_all_urls(missing: str) -> None:
    environ = {k: v for k, v in OTHER.items() if k != missing}
    with pytest.raises(EnvironmentError) as info:
        environment_config(environ)
    assert missing in str(info.value)


def test_environment_requires_all_urls_blank_counts_as_missing() -> None:
    environ = {**OTHER, "TRADESMART_ENV_WS_URL": "  "}
    with pytest.raises(EnvironmentError, match="TRADESMART_ENV_WS_URL"):
        environment_config(environ)


def test_environment_requires_all_urls_names_every_missing() -> None:
    with pytest.raises(EnvironmentError) as info:
        environment_config({"TRADESMART_ENV": "stage"})
    for name in OTHER:
        if name != "TRADESMART_ENV":
            assert name in str(info.value)


def test_production_token_file_is_library_default() -> None:
    assert token_file_for({}) is None


def test_explicit_token_file_wins(tmp_path: Path) -> None:
    explicit = str(tmp_path / "mine.json")
    assert token_file_for({**OTHER, "TRADESMART_TOKEN_FILE": explicit}) == Path(explicit)
    assert token_file_for({"TRADESMART_TOKEN_FILE": explicit}) == Path(explicit)


def test_uat_token_file_is_separate(tmp_path: Path) -> None:
    from tradesmart import _token_store

    production = _token_store.token_path()
    other = token_file_for(OTHER)
    assert other is not None
    assert other.name == "stage-token.json"
    assert other.parent == production.parent
    assert other != production


def test_token_dir_ignores_process_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tradesmart import _token_store

    default_dir = _token_store.token_path().parent
    monkeypatch.setenv("TRADESMART_TOKEN_FILE", str(tmp_path / "elsewhere" / "token.json"))
    other = token_file_for(OTHER)
    assert other == default_dir / "stage-token.json"
    assert os.environ["TRADESMART_TOKEN_FILE"].endswith("token.json")


def _save_login(path: Path, client_id: str = "STAGE1") -> None:
    from tradesmart import _token_store
    from tradesmart.auth import AccessToken

    now = datetime.now(UTC)
    token = AccessToken.from_response({"access_token": "synthetic-stage-token"}, issued_at=now)
    _token_store.save(client_id, token, path)


@pytest.fixture
def other_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``stage`` environment in this process, without shell credentials."""
    monkeypatch.delenv("TRADESMART_CLIENT_ID", raising=False)
    monkeypatch.delenv("TRADESMART_ACCESS_TOKEN", raising=False)
    for key, value in OTHER.items():
        monkeypatch.setenv(key, value)


def test_environment_client_uses_environment_urls_and_restores_env(other_env: None) -> None:
    path = token_file_for(OTHER)
    assert path is not None
    _save_login(path)
    client = environment_client(os.environ)
    try:
        assert client.config.base_url == OTHER["TRADESMART_ENV_BASE_URL"]
    finally:
        client.close()
    assert "TRADESMART_TOKEN_FILE" not in os.environ


# -- environment name in the token file name --------------------------------------------


@pytest.mark.parametrize("name", ["uat", "stage-2", " UAT ", "u-a-t-9"])
def test_environment_name_allows_lowercase_letters_digits_and_hyphens(name: str) -> None:
    assert environment_name({"TRADESMART_ENV": name}) == name.strip().lower()


@pytest.mark.parametrize(
    "name",
    ["../x", "uat/x", "uat\\x", "uat_1", "u at", "uat.json", "üat", "uat\x00", "~uat", ".."],
)
def test_environment_name_refuses_anything_else(name: str) -> None:
    with pytest.raises(OSError, match="TRADESMART_ENV"):
        environment_name({"TRADESMART_ENV": name})


def test_token_file_for_refuses_an_unsafe_environment_name() -> None:
    with pytest.raises(OSError, match="TRADESMART_ENV"):
        token_file_for({**OTHER, "TRADESMART_ENV": "../escape"})


# -- an explicit TRADESMART_TOKEN_FILE outside production ---------------------------------


def test_explicit_token_file_refuses_the_production_default_outside_production() -> None:
    from tradesmart import _token_store

    production = _token_store.token_path()
    with pytest.raises(OSError, match="production"):
        token_file_for({**OTHER, "TRADESMART_TOKEN_FILE": str(production)})


def test_explicit_token_file_refuses_a_file_named_token_json_outside_production(
    tmp_path: Path,
) -> None:
    explicit = str(tmp_path / "elsewhere" / "token.json")
    with pytest.raises(OSError, match="token.json"):
        token_file_for({**OTHER, "TRADESMART_TOKEN_FILE": explicit})


def test_explicit_token_file_named_token_json_is_fine_on_production(tmp_path: Path) -> None:
    explicit = str(tmp_path / "token.json")
    assert token_file_for({"TRADESMART_TOKEN_FILE": explicit}) == Path(explicit)


# -- shell credentials outside production -----------------------------------------------


def _no_client_built(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import tests._environment as helper

    built: list[str] = []

    def from_env(**kwargs: object) -> None:
        built.append("from_env")
        raise AssertionError("no client may be built")

    monkeypatch.setattr(helper.TradeSmart, "from_env", from_env)
    return built


def test_environment_client_refuses_a_shell_access_token_outside_production(
    other_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _no_client_built(monkeypatch)
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "STAGE1")
    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", "synthetic-token")
    with pytest.raises(OSError, match="TRADESMART_ACCESS_TOKEN"):
        environment_client(os.environ)
    assert built == []


def test_environment_client_refuses_an_access_token_in_the_given_environ(
    other_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _no_client_built(monkeypatch)
    with pytest.raises(OSError, match="TRADESMART_ACCESS_TOKEN"):
        environment_client({**OTHER, "TRADESMART_ACCESS_TOKEN": "synthetic-token"})
    assert built == []


def test_environment_client_refuses_a_client_id_that_differs_from_the_token_file(
    other_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = token_file_for(OTHER)
    assert path is not None
    _save_login(path, "STAGE1")
    built = _no_client_built(monkeypatch)
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "PROD99")
    with pytest.raises(OSError, match="TRADESMART_CLIENT_ID"):
        environment_client(os.environ)
    assert built == []


def test_environment_client_accepts_the_token_file_client_id_in_any_case(
    other_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = token_file_for(OTHER)
    assert path is not None
    _save_login(path, "STAGE1")
    monkeypatch.setenv("TRADESMART_CLIENT_ID", " stage1 ")
    client = environment_client(os.environ)
    try:
        assert client.client_id.casefold() == "stage1"
    finally:
        client.close()


def test_production_keeps_shell_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADESMART_ENV", raising=False)
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST1")
    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", "synthetic-token")
    client = environment_client(os.environ)
    try:
        assert client.client_id == "TEST1"
    finally:
        client.close()


# -- the temporary TRADESMART_TOKEN_FILE swap -------------------------------------------


def test_environment_client_restores_a_preset_token_file_variable(
    other_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    preset = str(tmp_path / "preset.json")
    monkeypatch.setenv("TRADESMART_TOKEN_FILE", preset)
    path = token_file_for(OTHER)  # the given environ has no TRADESMART_TOKEN_FILE
    assert path is not None and path.name == "stage-token.json"
    _save_login(path)
    client = environment_client(dict(OTHER))
    try:
        assert client.client_id == "STAGE1"
    finally:
        client.close()
    assert os.environ["TRADESMART_TOKEN_FILE"] == preset


def test_environment_client_restores_the_token_file_variable_after_an_error(
    other_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tradesmart.errors import ConfigurationError

    preset = str(tmp_path / "preset.json")
    monkeypatch.setenv("TRADESMART_TOKEN_FILE", preset)
    with pytest.raises(ConfigurationError):  # no saved login for the environment
        environment_client(dict(OTHER))
    assert os.environ["TRADESMART_TOKEN_FILE"] == preset


def test_token_file_swap_is_serialised_by_a_lock() -> None:
    from tests._environment import _process_token_file

    seen: list[str] = []

    def other_thread() -> None:
        with _process_token_file("b.json"):
            seen.append(os.environ["TRADESMART_TOKEN_FILE"])

    with _process_token_file("a.json"):
        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(0.3)
        assert thread.is_alive(), "a second swap must wait for the first one"
        assert os.environ["TRADESMART_TOKEN_FILE"] == "a.json"
        assert seen == []
    thread.join(5)
    assert not thread.is_alive()
    assert seen == ["b.json"]
    assert "TRADESMART_TOKEN_FILE" not in os.environ


def test_environment_client_reads_the_environment_token_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from datetime import datetime

    from tradesmart import _token_store
    from tradesmart.auth import AccessToken

    monkeypatch.delenv("TRADESMART_CLIENT_ID", raising=False)
    monkeypatch.delenv("TRADESMART_ACCESS_TOKEN", raising=False)
    for key, value in OTHER.items():
        monkeypatch.setenv(key, value)
    other = token_file_for(OTHER)
    assert other is not None
    now = datetime.now(UTC)
    _token_store.save(
        "STAGE1",
        AccessToken.from_response({"access_token": "synthetic-stage-token"}, issued_at=now),
        other,
    )
    client = environment_client(os.environ)
    try:
        assert client.client_id == "STAGE1"
    finally:
        client.close()
    # the production token file was neither read nor created
    assert not _token_store.token_path().exists()


def test_explicit_token_file_named_token_json_in_any_case_is_refused(tmp_path: Path) -> None:
    explicit = str(tmp_path / "elsewhere" / "Token.JSON")
    with pytest.raises(OSError, match="token.json"):
        token_file_for({**OTHER, "TRADESMART_TOKEN_FILE": explicit})


def test_explicit_token_file_linked_to_the_production_file_is_refused(tmp_path: Path) -> None:
    from tradesmart import _token_store

    production = _token_store.token_path()
    production.parent.mkdir(parents=True)
    production.write_text("production login", encoding="utf-8")
    link = tmp_path / "uat-login.json"
    link.symlink_to(production)
    with pytest.raises(OSError, match="production"):
        token_file_for({**OTHER, "TRADESMART_TOKEN_FILE": str(link)})


# -- environment_stream ----------------------------------------------------------------


def test_environment_stream_uses_environment_urls_and_token_file(other_env: None) -> None:
    from tests._environment import environment_stream
    from tradesmart import _token_store

    path = token_file_for(OTHER)
    assert path is not None
    _save_login(path)
    stream = environment_stream(os.environ)
    assert stream._stream._config.ws_url == OTHER["TRADESMART_ENV_WS_URL"]
    assert stream._client_id == "STAGE1"
    assert "TRADESMART_TOKEN_FILE" not in os.environ
    assert not _token_store.token_path().exists()


def test_environment_stream_passes_callbacks(other_env: None) -> None:
    from tests._environment import environment_stream

    path = token_file_for(OTHER)
    assert path is not None
    _save_login(path)

    def on_error(exc: BaseException) -> None:
        return None

    stream = environment_stream(os.environ, on_error=on_error)
    assert stream._on_error is on_error


def test_environment_stream_refuses_a_shell_access_token_outside_production(
    other_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests._environment import environment_stream

    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", "synthetic-token")
    with pytest.raises(OSError, match="TRADESMART_ACCESS_TOKEN"):
        environment_stream(os.environ)


def test_environment_stream_restores_the_token_file_variable_after_an_error(
    other_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests._environment import environment_stream
    from tradesmart.errors import ConfigurationError

    preset = str(tmp_path / "preset.json")
    monkeypatch.setenv("TRADESMART_TOKEN_FILE", preset)
    with pytest.raises(ConfigurationError):  # no saved login for the environment
        environment_stream(dict(OTHER))
    assert os.environ["TRADESMART_TOKEN_FILE"] == preset


# -- is_uat ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environ", "expected"),
    [({}, False), ({"TRADESMART_ENV": "production"}, False), ({"TRADESMART_ENV": " UAT "}, True)],
)
def test_is_uat(environ: dict[str, str], expected: bool) -> None:
    from tests._environment import is_uat

    assert is_uat(environ) is expected
