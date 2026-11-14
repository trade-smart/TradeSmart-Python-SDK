"""``python -m tradesmart login`` saves the typed App Key and Secret per client id; ``logout
--forget-keys`` removes them. All values are synthetic; the token exchange is mocked (respx) and
the browser step is replaced, so nothing leaves this machine.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import respx

from tests.conftest import BASE, fixture
from tradesmart import _cli, _key_store
from tradesmart.auth import compute_checksum

APP_KEY = "synthetic-app-key-A1"
SECRET = "synthetic-secret-key-A1"
OTHER_APP_KEY = "synthetic-app-key-B2"
OTHER_SECRET = "synthetic-secret-key-B2"
ENV_APP_KEY = "synthetic-env-app-key"
ENV_SECRET = "synthetic-env-secret-key"
ALL_KEYS = (APP_KEY, SECRET, OTHER_APP_KEY, OTHER_SECRET, ENV_APP_KEY, ENV_SECRET)
NEVER_SHOWN = (*ALL_KEYS, "test-access-token", "test-refresh-token", "test-code")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TRADESMART_CLIENT_ID", "TRADESMART_APP_KEY", "TRADESMART_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def keys_file(_no_saved_login: Path) -> Path:
    """The production saved-keys file: ``credentials.json`` beside the (temporary) token file."""
    return _no_saved_login.parent / "credentials.json"


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The browser step: records the login URL and returns the code (the URL is not printed)."""
    urls: list[str] = []

    def receive(url: str, port: int, use_browser: bool, listen_host: str = "") -> str | None:
        urls.append(url)
        return "test-code"

    monkeypatch.setattr(_cli, "_receive_code", receive)
    return urls


@pytest.fixture
def typed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Answers for the hidden prompts (App Key, then Secret); records each prompt asked."""
    prompts: list[str] = []
    answers = {"app key": APP_KEY, "secret key": SECRET}

    def fake_getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        for label, value in answers.items():
            if prompt.lower().startswith(label):
                return value
        raise AssertionError(f"unexpected prompt {prompt!r}")

    monkeypatch.setattr(_cli.getpass, "getpass", fake_getpass)
    return prompts


@pytest.fixture
def no_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(prompt: str = "") -> str:
        raise AssertionError(f"unexpected prompt {prompt!r}")

    monkeypatch.setattr(_cli.getpass, "getpass", fail)
    monkeypatch.setattr("builtins.input", fail)


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


def _login(args: list[str], body: dict[str, object] | None = None) -> tuple[int, respx.Route]:
    with respx.mock(assert_all_mocked=True) as router:
        route = router.post(f"{BASE}/GenAcsTok").respond(
            200, json=body if body is not None else fixture("gen_acs_tok_success")
        )
        rc = _cli.main(["login", *args])
    return rc, route


def _sent_checksum(route: respx.Route) -> str:
    body = route.calls.last.request.content.decode()
    payload = json.loads(body.split("jData=", 1)[1]) if "jData=" in body else json.loads(body)
    return str(payload["checksum"])


def _account(uid: str) -> dict[str, object]:
    return {**fixture("gen_acs_tok_success"), "uid": uid, "actid": uid, "USERID": uid}


def _assert_never_shown(
    capsys: pytest.CaptureFixture[str], logs: pytest.LogCaptureFixture
) -> tuple[str, str]:
    out = capsys.readouterr()
    for secret in NEVER_SHOWN:
        assert secret not in out.out, secret
        assert secret not in out.err, secret
        assert secret not in logs.text, secret
    return out.out, out.err


def _entries(path: Path) -> dict[str, dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    clients = data["clients"]
    assert isinstance(clients, dict)
    return clients


# --- saving -------------------------------------------------------------------------------------


def test_typed_keys_are_saved_after_a_successful_login(
    keys_file: Path,
    opened: list[str],
    typed: list[str],
    capsys: pytest.CaptureFixture[str],
    logs: pytest.LogCaptureFixture,
) -> None:
    rc, route = _login(["--client-id", "test123"])
    assert rc == 0
    assert len(typed) == 2
    # saved under the server's spelling of the client id
    assert _entries(keys_file) == {"TEST123": {"app_key": APP_KEY, "secret_key": SECRET}}
    out, _ = _assert_never_shown(capsys, logs)
    assert f"Saved the App Key and Secret key for TEST123 to {keys_file}" in out
    assert "not encrypted" in out
    assert "python -m tradesmart logout --forget-keys --client-id TEST123" in out


def test_keys_from_the_environment_are_not_saved(
    keys_file: Path,
    opened: list[str],
    no_prompt: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    logs: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("TRADESMART_APP_KEY", ENV_APP_KEY)
    monkeypatch.setenv("TRADESMART_SECRET_KEY", ENV_SECRET)
    rc, route = _login(["--client-id", "TEST123"])
    assert rc == 0
    assert _sent_checksum(route) == compute_checksum(ENV_APP_KEY, ENV_SECRET, "test-code")
    assert not keys_file.exists()
    out, _ = _assert_never_shown(capsys, logs)
    assert "Saved the App Key" not in out


def test_app_key_option_is_saved_but_not_a_secret_from_the_environment(
    keys_file: Path,
    opened: list[str],
    no_prompt: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    logs: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("TRADESMART_SECRET_KEY", ENV_SECRET)
    rc, _ = _login(["--client-id", "TEST123", "--app-key", APP_KEY])
    assert rc == 0
    assert _entries(keys_file) == {"TEST123": {"app_key": APP_KEY}}
    out, _ = _assert_never_shown(capsys, logs)
    assert f"Saved the App Key for TEST123 to {keys_file}" in out


def test_no_save_keys_logs_in_without_saving(
    keys_file: Path,
    _no_saved_login: Path,
    opened: list[str],
    typed: list[str],
    capsys: pytest.CaptureFixture[str],
    logs: pytest.LogCaptureFixture,
) -> None:
    rc, _ = _login(["--client-id", "TEST123", "--no-save-keys"])
    assert rc == 0
    assert _no_saved_login.exists()  # the token is saved as usual
    assert not keys_file.exists()
    out, _ = _assert_never_shown(capsys, logs)
    assert "Saved the App Key" not in out


@pytest.mark.parametrize(
    ("body", "receive_code"),
    [({"stat": "Not_Ok", "emsg": "Invalid checksum"}, True), (None, False)],
    ids=["token-exchange-refused", "no-code"],
)
def test_keys_are_not_saved_when_the_login_fails(
    keys_file: Path,
    typed: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    logs: pytest.LogCaptureFixture,
    body: dict[str, object] | None,
    receive_code: bool,
) -> None:
    monkeypatch.setattr(
        _cli, "_receive_code", lambda *a, **k: "test-code" if receive_code else None
    )
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post(f"{BASE}/GenAcsTok").respond(200, json=body or {})
        assert _cli.main(["login", "--client-id", "TEST123"]) == 1
    assert not keys_file.exists()
    _assert_never_shown(capsys, logs)


def test_keys_are_not_saved_for_a_login_of_another_account(
    keys_file: Path,
    opened: list[str],
    typed: list[str],
    capsys: pytest.CaptureFixture[str],
    logs: pytest.LogCaptureFixture,
) -> None:
    rc, _ = _login(["--client-id", "VICTIM9"])  # the token belongs to TEST123
    assert rc == 1
    assert not keys_file.exists()
    _assert_never_shown(capsys, logs)


def test_keys_that_cannot_be_saved_only_warn(
    keys_file: Path,
    _no_saved_login: Path,
    opened: list[str],
    typed: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    logs: pytest.LogCaptureFixture,
) -> None:
    def disk_full(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(_cli._key_store, "write_private", disk_full)
    rc, _ = _login(["--client-id", "TEST123"])
    assert rc == 0  # the login itself succeeded and the token is saved
    assert _no_saved_login.exists()
    _, err = _assert_never_shown(capsys, logs)
    assert f"could not be saved to {keys_file} (No space left on device)" in err
    assert not keys_file.exists()


def test_custom_token_file_keeps_the_keys_beside_it(
    tmp_path: Path,
    opened: list[str],
    typed: list[str],
    capsys: pytest.CaptureFixture[str],
    logs: pytest.LogCaptureFixture,
) -> None:
    target = tmp_path / "mine" / "login.json"
    rc, _ = _login(["--client-id", "TEST123", "--token-file", str(target)])
    assert rc == 0
    keys_file = target.parent / "credentials.json"
    assert _entries(keys_file)["TEST123"] == {"app_key": APP_KEY, "secret_key": SECRET}
    out, _ = _assert_never_shown(capsys, logs)
    # the forget command names the same token file, so it finds the same keys file
    (line,) = [ln for ln in out.splitlines() if "--forget-keys" in ln]
    assert "--token-file" in line and str(target) in line


@pytest.mark.skipif(not hasattr(__import__("os"), "geteuid"), reason="POSIX permission bits")
def test_saved_keys_file_is_owner_only(
    keys_file: Path, opened: list[str], typed: list[str]
) -> None:
    import stat

    assert _login(["--client-id", "TEST123"])[0] == 0
    assert stat.S_IMODE(keys_file.stat().st_mode) == 0o600


# --- reading ------------------------------------------------------------------------------------


def test_saved_keys_are_used_without_prompting(
    keys_file: Path,
    opened: list[str],
    no_prompt: None,
    capsys: pytest.CaptureFixture[str],
    logs: pytest.LogCaptureFixture,
) -> None:
    _key_store.put(keys_file, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    before = keys_file.read_bytes()
    rc, route = _login(["--client-id", "test123"])
    assert rc == 0
    assert opened[0].endswith(f"?client_id={APP_KEY}")
    assert _sent_checksum(route) == compute_checksum(APP_KEY, SECRET, "test-code")
    assert keys_file.read_bytes() == before  # nothing new to save
    out, _ = _assert_never_shown(capsys, logs)
    assert f"Using the App Key and Secret key saved for test123 in {keys_file}" in out


def test_each_client_id_uses_its_own_saved_keys(
    keys_file: Path,
    opened: list[str],
    no_prompt: None,
    capsys: pytest.CaptureFixture[str],
    logs: pytest.LogCaptureFixture,
) -> None:
    _key_store.put(keys_file, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    _key_store.put(keys_file, "OTHER9", app_key=OTHER_APP_KEY, secret_key=OTHER_SECRET)
    rc, route = _login(["--client-id", "OTHER9"], _account("OTHER9"))
    assert rc == 0
    assert opened[0].endswith(f"?client_id={OTHER_APP_KEY}")
    assert _sent_checksum(route) == compute_checksum(OTHER_APP_KEY, OTHER_SECRET, "test-code")
    _assert_never_shown(capsys, logs)


def test_environment_wins_over_saved_keys(
    keys_file: Path,
    opened: list[str],
    no_prompt: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _key_store.put(keys_file, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    before = keys_file.read_bytes()
    monkeypatch.setenv("TRADESMART_APP_KEY", ENV_APP_KEY)
    monkeypatch.setenv("TRADESMART_SECRET_KEY", ENV_SECRET)
    rc, route = _login(["--client-id", "TEST123"])
    assert rc == 0
    assert _sent_checksum(route) == compute_checksum(ENV_APP_KEY, ENV_SECRET, "test-code")
    assert keys_file.read_bytes() == before


def test_app_key_option_wins_over_the_saved_app_key(
    keys_file: Path, opened: list[str], no_prompt: None
) -> None:
    _key_store.put(keys_file, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    rc, route = _login(["--client-id", "TEST123", "--app-key", OTHER_APP_KEY])
    assert rc == 0
    assert _sent_checksum(route) == compute_checksum(OTHER_APP_KEY, SECRET, "test-code")
    # the App Key given on the command line replaces the saved one; the saved Secret stays
    assert _entries(keys_file)["TEST123"] == {"app_key": OTHER_APP_KEY, "secret_key": SECRET}


def test_prompt_when_only_another_client_id_has_saved_keys(
    keys_file: Path,
    opened: list[str],
    typed: list[str],
    capsys: pytest.CaptureFixture[str],
    logs: pytest.LogCaptureFixture,
) -> None:
    _key_store.put(keys_file, "OTHER9", app_key=OTHER_APP_KEY, secret_key=OTHER_SECRET)
    rc, route = _login(["--client-id", "TEST123"])
    assert rc == 0
    assert len(typed) == 2
    assert _sent_checksum(route) == compute_checksum(APP_KEY, SECRET, "test-code")
    assert _entries(keys_file) == {
        "OTHER9": {"app_key": OTHER_APP_KEY, "secret_key": OTHER_SECRET},
        "TEST123": {"app_key": APP_KEY, "secret_key": SECRET},
    }
    _assert_never_shown(capsys, logs)


def test_a_partial_entry_prompts_for_the_missing_key(
    keys_file: Path, opened: list[str], typed: list[str]
) -> None:
    _key_store.put(keys_file, "TEST123", app_key=APP_KEY)
    rc, route = _login(["--client-id", "TEST123"])
    assert rc == 0
    assert len(typed) == 1 and typed[0].lower().startswith("secret key")
    assert _sent_checksum(route) == compute_checksum(APP_KEY, SECRET, "test-code")
    assert _entries(keys_file)["TEST123"] == {"app_key": APP_KEY, "secret_key": SECRET}


def test_client_id_prompt_comes_before_the_saved_key_lookup(
    keys_file: Path, opened: list[str], no_prompt: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _key_store.put(keys_file, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    monkeypatch.setattr("builtins.input", lambda prompt="": "TEST123")
    rc, route = _login([])
    assert rc == 0
    assert _sent_checksum(route) == compute_checksum(APP_KEY, SECRET, "test-code")


@pytest.mark.parametrize(
    "content",
    ["garbage " + SECRET, json.dumps({"TEST123": {"app_key": APP_KEY, "secret_key": SECRET}})],
    ids=["corrupt", "foreign"],
)
def test_unusable_keys_file_refuses_the_login_clearly(
    keys_file: Path,
    no_prompt: None,
    capsys: pytest.CaptureFixture[str],
    logs: pytest.LogCaptureFixture,
    content: str,
) -> None:
    keys_file.parent.mkdir(parents=True, exist_ok=True)
    keys_file.write_text(content, encoding="utf-8")
    keys_file.chmod(0o600)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        assert _cli.main(["login", "--client-id", "TEST123"]) == 2
    assert not router.calls
    _, err = _assert_never_shown(capsys, logs)
    assert str(keys_file) in err and "Traceback" not in err
    assert "python -m tradesmart logout --forget-keys" in err
    assert keys_file.read_text(encoding="utf-8") == content


def test_unusable_keys_file_is_not_read_when_both_keys_come_from_the_environment(
    keys_file: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    keys_file.parent.mkdir(parents=True, exist_ok=True)
    keys_file.write_text("garbage", encoding="utf-8")
    monkeypatch.setenv("TRADESMART_APP_KEY", ENV_APP_KEY)
    monkeypatch.setenv("TRADESMART_SECRET_KEY", ENV_SECRET)
    assert _login(["--client-id", "TEST123"])[0] == 0


@pytest.mark.parametrize("receive_code", [True, False], ids=["refused", "no-code"])
def test_failed_login_with_saved_keys_says_how_to_forget_them(
    keys_file: Path,
    no_prompt: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    logs: pytest.LogCaptureFixture,
    receive_code: bool,
) -> None:
    _key_store.put(keys_file, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    monkeypatch.setattr(
        _cli, "_receive_code", lambda *a, **k: "test-code" if receive_code else None
    )
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        route = router.post(f"{BASE}/GenAcsTok").respond(
            200, json={"stat": "Not_Ok", "emsg": "Invalid checksum"}
        )
        assert _cli.main(["login", "--client-id", "TEST123"]) == 1
    assert route.call_count == (1 if receive_code else 0)  # never retried, no loop
    _, err = _assert_never_shown(capsys, logs)
    assert f"saved for TEST123 in {keys_file}" in err
    assert "python -m tradesmart logout --forget-keys --client-id TEST123" in err
    assert _key_store.get(keys_file, "TEST123") is not None  # the user decides


def test_failed_login_with_typed_keys_has_no_saved_keys_hint(
    keys_file: Path,
    typed: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(_cli, "_receive_code", lambda *a, **k: None)
    assert _cli.main(["login", "--client-id", "TEST123"]) == 1
    assert "--forget-keys" not in capsys.readouterr().err


# --- logout -------------------------------------------------------------------------------------


def _two_entries(keys_file: Path) -> None:
    _key_store.put(keys_file, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    _key_store.put(keys_file, "OTHER9", app_key=OTHER_APP_KEY, secret_key=OTHER_SECRET)


def test_logout_keeps_the_saved_keys(
    keys_file: Path, _no_saved_login: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_entries(keys_file)
    _no_saved_login.write_text("{}", encoding="utf-8")
    assert _cli.main(["logout"]) == 0
    assert not _no_saved_login.exists()
    assert _key_store.client_ids(keys_file) == ["OTHER9", "TEST123"]
    assert "--forget-keys" not in capsys.readouterr().out


def test_logout_forget_keys_for_one_client_id(
    keys_file: Path,
    _no_saved_login: Path,
    capsys: pytest.CaptureFixture[str],
    logs: pytest.LogCaptureFixture,
) -> None:
    _two_entries(keys_file)
    _no_saved_login.write_text("{}", encoding="utf-8")
    assert _cli.main(["logout", "--forget-keys", "--client-id", "test123"]) == 0
    assert not _no_saved_login.exists()
    assert _key_store.client_ids(keys_file) == ["OTHER9"]
    out, _ = _assert_never_shown(capsys, logs)
    assert f"Forgot the saved keys for test123 in {keys_file}." in out


def test_logout_forget_keys_for_a_client_id_without_keys(
    keys_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_entries(keys_file)
    assert _cli.main(["logout", "--forget-keys", "--client-id", "NOBODY"]) == 0
    assert _key_store.client_ids(keys_file) == ["OTHER9", "TEST123"]
    assert f"No saved keys for NOBODY in {keys_file}." in capsys.readouterr().out


def test_logout_forget_keys_for_every_client_id(
    keys_file: Path, capsys: pytest.CaptureFixture[str], logs: pytest.LogCaptureFixture
) -> None:
    _two_entries(keys_file)
    assert _cli.main(["logout", "--forget-keys"]) == 0
    assert not keys_file.exists()
    out, _ = _assert_never_shown(capsys, logs)
    assert (
        f"Forgot the saved keys of every client id (OTHER9, TEST123): removed {keys_file}." in out
    )


def test_logout_forget_keys_without_saved_keys(
    keys_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli.main(["logout", "--forget-keys"]) == 0
    assert f"No saved keys at {keys_file}." in capsys.readouterr().out


def test_logout_forget_keys_removes_an_unusable_file_without_showing_it(
    keys_file: Path, capsys: pytest.CaptureFixture[str], logs: pytest.LogCaptureFixture
) -> None:
    keys_file.parent.mkdir(parents=True, exist_ok=True)
    keys_file.write_text("garbage " + SECRET, encoding="utf-8")
    assert _cli.main(["logout", "--forget-keys"]) == 0
    assert not keys_file.exists()
    out, _ = _assert_never_shown(capsys, logs)
    assert f"Removed the unreadable saved keys file {keys_file}." in out


def test_logout_forget_keys_for_one_id_in_an_unusable_file_fails_clearly(
    keys_file: Path, capsys: pytest.CaptureFixture[str], logs: pytest.LogCaptureFixture
) -> None:
    keys_file.parent.mkdir(parents=True, exist_ok=True)
    keys_file.write_text("garbage " + SECRET, encoding="utf-8")
    assert _cli.main(["logout", "--forget-keys", "--client-id", "TEST123"]) == 1
    assert keys_file.exists()
    _, err = _assert_never_shown(capsys, logs)
    assert str(keys_file) in err
    assert "python -m tradesmart logout --forget-keys`" in err  # without --client-id: all


def test_logout_client_id_needs_forget_keys(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        _cli.main(["logout", "--client-id", "TEST123"])
    assert info.value.code == 2
    assert "--forget-keys" in capsys.readouterr().err


def test_logout_forget_keys_follows_the_token_file_option(
    tmp_path: Path, keys_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_entries(keys_file)  # the default location: untouched
    target = tmp_path / "mine" / "login.json"
    other = target.parent / "credentials.json"
    _key_store.put(other, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    assert _cli.main(["logout", "--token-file", str(target), "--forget-keys"]) == 0
    assert not other.exists()
    assert _key_store.client_ids(keys_file) == ["OTHER9", "TEST123"]


def test_login_help_documents_no_save_keys(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _cli.main(["login", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--no-save-keys" in text


def test_logout_help_documents_forget_keys(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _cli.main(["logout", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--forget-keys" in text and "--client-id" in text


# --- production never touches a test environment's keys -----------------------------------------


def test_production_login_never_reads_or_writes_uat_keys(
    keys_file: Path, opened: list[str], typed: list[str]
) -> None:
    uat = keys_file.parent / "uat-credentials.json"
    _key_store.put(uat, "TEST123", app_key=OTHER_APP_KEY, secret_key=OTHER_SECRET)
    before = uat.read_bytes()
    rc, route = _login(["--client-id", "TEST123"])
    assert rc == 0
    assert len(typed) == 2  # the UAT entry for the same client id was not used
    assert _sent_checksum(route) == compute_checksum(APP_KEY, SECRET, "test-code")
    assert uat.read_bytes() == before
    assert _cli.main(["logout", "--forget-keys"]) == 0
    assert uat.read_bytes() == before


# --- failures and quoting -----------------------------------------------------------------------


def test_resolved_keys_never_show_in_repr() -> None:
    keys = _cli._Keys(APP_KEY, SECRET, "typed", "saved")
    text = repr(keys) + str(keys)
    assert APP_KEY not in text and SECRET not in text
    assert "typed" in text and "saved" in text


@pytest.mark.parametrize(
    ("missing", "advice"),
    [
        ("app key", "pass --app-key or set TRADESMART_APP_KEY"),
        ("secret key", "set TRADESMART_SECRET_KEY"),
    ],
)
def test_no_terminal_input_for_a_key_is_a_usage_error(
    keys_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
    advice: str,
) -> None:
    def fake_getpass(prompt: str = "") -> str:
        if prompt.lower().startswith(missing):
            raise EOFError
        return APP_KEY

    monkeypatch.setattr(_cli.getpass, "getpass", fake_getpass)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        assert _cli.main(["login", "--client-id", "TEST123"]) == 2
    assert not router.calls
    assert f"No {missing} given: {advice}." in capsys.readouterr().err
    assert not keys_file.exists()


def _disk_full(*args: object, **kwargs: object) -> None:
    raise OSError(28, "No space left on device")


def test_logout_forget_keys_for_one_id_reports_a_write_failure(
    keys_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_entries(keys_file)
    before = keys_file.read_bytes()
    monkeypatch.setattr(_cli._key_store, "write_private", _disk_full)
    assert _cli.main(["logout", "--forget-keys", "--client-id", "TEST123"]) == 1
    assert keys_file.read_bytes() == before
    err = capsys.readouterr().err
    assert f"Could not update the saved keys at {keys_file} (No space left on device)." in err


def test_logout_forget_all_keys_reports_a_delete_failure(
    keys_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_entries(keys_file)
    monkeypatch.setattr(_cli._key_store, "delete", _disk_full)
    assert _cli.main(["logout", "--forget-keys"]) == 1
    assert keys_file.exists()
    assert f"Could not remove the saved keys at {keys_file}" in capsys.readouterr().err


def test_forget_hint_quotes_an_unusual_client_id(
    keys_file: Path,
    opened: list[str],
    typed: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A client id the shell would split or expand is quoted in the printed command."""
    body = {
        key: value
        for key, value in fixture("gen_acs_tok_success").items()
        if key not in ("uid", "actid")
    }
    rc, _ = _login(["--client-id", "AB 12"], body)
    assert rc == 0
    assert _key_store.get(keys_file, "AB 12") is not None
    out = capsys.readouterr().out
    quoted = "'AB 12'" if __import__("os").name != "nt" else '"AB 12"'
    assert f"--forget-keys --client-id {quoted}`" in out


# --- review fix round 1 -------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   "])
def test_logout_forget_keys_with_a_blank_client_id_is_a_usage_error(
    keys_file: Path,
    _no_saved_login: Path,
    capsys: pytest.CaptureFixture[str],
    blank: str,
) -> None:
    """Only an omitted --client-id forgets every client id; a blank one removes nothing."""
    _two_entries(keys_file)
    _no_saved_login.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as info:
        _cli.main(["logout", "--forget-keys", "--client-id", blank])
    assert info.value.code == 2
    assert _no_saved_login.exists()
    assert _key_store.client_ids(keys_file) == ["OTHER9", "TEST123"]
    assert "--client-id" in capsys.readouterr().err


def _link_to_uat_keys(keys_file: Path) -> Path:
    uat = keys_file.parent / "uat-credentials.json"
    _key_store.put(uat, "TEST123", app_key=OTHER_APP_KEY, secret_key=OTHER_SECRET)
    keys_file.symlink_to(uat)
    return uat


@pytest.mark.parametrize("from_env", [False, True], ids=["prompt", "environment"])
def test_production_keys_file_linked_to_another_environment_is_refused(
    keys_file: Path,
    no_prompt: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    logs: pytest.LogCaptureFixture,
    from_env: bool,
) -> None:
    uat = _link_to_uat_keys(keys_file)
    before = uat.read_bytes()
    if from_env:  # even when no key would be read from the file, it must not be written
        monkeypatch.setenv("TRADESMART_SECRET_KEY", ENV_SECRET)
    args = ["login", "--client-id", "TEST123"] + (["--app-key", APP_KEY] if from_env else [])
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        assert _cli.main(args) == 2
    assert not router.calls
    assert uat.read_bytes() == before
    _, err = _assert_never_shown(capsys, logs)
    assert str(keys_file) in err and "another environment" in err


def test_forgetting_one_id_through_a_link_to_another_environment_is_refused(
    keys_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    uat = _link_to_uat_keys(keys_file)
    before = uat.read_bytes()
    assert _cli.main(["logout", "--forget-keys", "--client-id", "TEST123"]) == 1
    assert uat.read_bytes() == before
    assert keys_file.is_symlink()
    assert "another environment" in capsys.readouterr().err


def test_both_gitignores_cover_every_environment_keys_file() -> None:
    root = Path(__file__).resolve().parents[3]
    for ignore in (root / ".gitignore", root / "sdk-python" / ".gitignore"):
        assert "*credentials*.json" in ignore.read_text(encoding="utf-8").splitlines(), ignore


def test_authentication_guide_states_the_review_caveats() -> None:
    text = " ".join(
        (Path(__file__).resolve().parents[2] / "docs" / "authentication.md")
        .read_text(encoding="utf-8")
        .split()
    )
    assert "On a Windows computer" in text  # owner-only is enforced on POSIX, not on Windows
    assert "The printed URL contains your App Key" in text  # --no-browser
    assert "two logins at the same time" in text  # no locking
