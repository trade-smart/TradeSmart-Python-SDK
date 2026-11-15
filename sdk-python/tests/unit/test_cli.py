"""``python -m tradesmart`` (login, token-path, logout)."""

from __future__ import annotations

import contextlib
import json
import os
import runpy
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import respx

from tests.conftest import BASE, fixture
from tradesmart import _cli

SECRET = "test-secret-key"
APP_KEY = "test-app-key"


@pytest.fixture
def creds_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.setenv("TRADESMART_APP_KEY", APP_KEY)
    monkeypatch.setenv("TRADESMART_SECRET_KEY", SECRET)


@pytest.fixture
def no_creds_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TRADESMART_CLIENT_ID", "TRADESMART_APP_KEY", "TRADESMART_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Queue of answers for ``input()``; the test fails if a prompt is unexpected."""
    queue: list[str] = []

    def fake_input(prompt: str = "") -> str:
        assert queue, f"unexpected prompt: {prompt!r}"
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    yield queue
    assert not queue, f"unused answers: {queue}"


@pytest.fixture
def no_browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    opened: list[str] = []
    monkeypatch.setattr(_cli.webbrowser, "open", lambda url: opened.append(url) or True)
    return opened


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _gen_acs_tok(router: respx.MockRouter, name: str = "gen_acs_tok_success") -> respx.Route:
    return router.post(f"{BASE}/GenAcsTok").respond(200, json=fixture(name))


def _assert_no_secrets(text: str) -> None:
    for secret in ("test-access-token", SECRET, "test-code", "test-refresh-token"):
        assert secret not in text


def test_browser_callback_path(
    creds_env: None,
    _no_saved_login: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    port = _free_port()
    replies: list[int] = []

    def redirect() -> None:  # what the browser does after the user logs in
        url = f"http://127.0.0.1:{port}/callback?code=test-code"
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - fixed local URL
            replies.append(resp.status)

    def fake_open(url: str) -> bool:
        assert url.endswith(f"?client_id={APP_KEY}")
        thread = threading.Thread(target=redirect)
        thread.start()
        return True

    monkeypatch.setattr(_cli.webbrowser, "open", fake_open)
    with respx.mock(assert_all_mocked=True) as router:
        route = _gen_acs_tok(router)
        assert _cli.main(["login", "--port", str(port)]) == 0
    assert route.call_count == 1
    assert '"code":"test-code"' in route.calls.last.request.content.decode()
    assert _settled(replies, 1) == [200]
    saved = json.loads(_no_saved_login.read_text(encoding="utf-8"))
    assert saved["client_id"] == "TEST123"
    assert saved["access_token"] == "test-access-token"
    out = capsys.readouterr()
    assert "Test User" in out.out and "TEST123" in out.out
    assert "06:00 IST" in out.out
    assert str(_no_saved_login) in out.out
    assert "from_env() uses it" in out.out  # the default location: nothing else to set
    assert "Remove the saved login with `python -m tradesmart logout`." in out.out
    _assert_no_secrets(out.out + out.err)


def test_callback_ignores_other_paths_until_the_code_arrives(
    creds_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _free_port()
    statuses: list[int] = []

    def requests() -> None:
        for path in ("/favicon.ico", "/callback", "/callback?code=test-code"):
            try:
                with urllib.request.urlopen(  # noqa: S310 - fixed local URL
                    f"http://127.0.0.1:{port}{path}", timeout=5
                ) as resp:
                    statuses.append(resp.status)
            except urllib.error.HTTPError as exc:
                statuses.append(exc.code)

    monkeypatch.setattr(
        _cli.webbrowser, "open", lambda url: threading.Thread(target=requests).start() or True
    )
    with respx.mock(assert_all_mocked=True) as router:
        _gen_acs_tok(router)
        assert _cli.main(["--port", str(port)]) == 0  # login is the default command
    assert _settled(statuses, 3) == [404, 400, 200]


def test_paste_code_path(
    creds_env: None,
    no_browser: list[str],
    answers: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The hint below is the Linux/macOS form; Windows prints PowerShell/cmd (tested separately).
    monkeypatch.setattr(_cli._token_store, "_os_name", lambda: "posix")
    answers.append("  test-code  ")
    target = tmp_path / "custom" / "token.json"
    with respx.mock(assert_all_mocked=True) as router:
        route = _gen_acs_tok(router)
        assert _cli.main(["login", "--no-browser", "--token-file", str(target)]) == 0
    assert not no_browser  # --no-browser: the URL is printed instead
    assert '"code":"test-code"' in route.calls.last.request.content.decode()
    assert json.loads(target.read_text(encoding="utf-8"))["client_id"] == "TEST123"
    out = capsys.readouterr()
    assert f"?client_id={APP_KEY}" in out.out  # the login URL to open by hand
    assert str(target) in out.out
    # not the file from_env() reads by default: say how to point it there
    assert f"export TRADESMART_TOKEN_FILE='{target}'" in out.out
    assert "from_env() uses it" not in out.out
    # ... and how to remove exactly this file
    logout = f"`python -m tradesmart logout --token-file '{target}'`."
    assert f"Remove the saved login with {logout}" in out.out


def _login_to(target: Path, answers: list[str], capsys: pytest.CaptureFixture[str]) -> list[str]:
    answers.append("test-code")
    with respx.mock(assert_all_mocked=True) as router:
        _gen_acs_tok(router)
        assert _cli.main(["login", "--no-browser", "--token-file", str(target)]) == 0
    return capsys.readouterr().out.splitlines()


AWKWARD_DIR = "it's 100% $HOME my logins"


@pytest.mark.skipif(os.name != "posix", reason="runs the printed commands in sh")
def test_posix_hints_survive_the_shell(
    creds_env: None,
    no_browser: list[str],
    answers: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_cli._token_store, "_os_name", lambda: "posix")
    target = tmp_path / AWKWARD_DIR / "token.json"
    lines = _login_to(target, answers, capsys)
    (export,) = [ln.strip() for ln in lines if "export TRADESMART_TOKEN_FILE=" in ln]
    assert _sh(f'{export} && printf %s "$TRADESMART_TOKEN_FILE"') == str(target)
    (logout,) = [ln for ln in lines if ln.startswith("Remove the saved login with `")]
    quoted = logout.split("`")[1].split(" --token-file ", 1)[1]
    assert _sh(f'set -- {quoted}; printf %s "$1"') == str(target)
    assert not any("$env:" in ln or 'set "' in ln for ln in lines)  # POSIX: export only


def _sh(script: str) -> str:
    """Stdout of ``script`` run by /bin/sh (what a user pasting the hint would get)."""
    result = subprocess.run(  # noqa: S603 - the script is built by the test itself
        ["/bin/sh", "-c", script], capture_output=True, text=True, check=True
    )
    return result.stdout


def test_windows_hints_for_powershell_and_cmd(
    creds_env: None,
    no_browser: list[str],
    answers: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_cli._token_store, "_os_name", lambda: "nt")
    target = tmp_path / "my logins" / "token.json"
    out = "\n".join(_login_to(target, answers, capsys))
    assert f"PowerShell: $env:TRADESMART_TOKEN_FILE = '{target}'" in out
    assert f'cmd:        set "TRADESMART_TOKEN_FILE={target}"' in out
    assert "export " not in out
    # double quotes work in both PowerShell and cmd for a plain path
    assert f'`python -m tradesmart logout --token-file "{target}"`' in out


def test_windows_hints_for_an_awkward_path(
    creds_env: None,
    no_browser: list[str],
    answers: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_cli._token_store, "_os_name", lambda: "nt")
    target = tmp_path / AWKWARD_DIR / "token.json"
    out = "\n".join(_login_to(target, answers, capsys))
    quoted = "'" + str(target).replace("'", "''") + "'"  # PowerShell: '' inside '...'
    assert f"PowerShell: $env:TRADESMART_TOKEN_FILE = {quoted}" in out
    assert 'set "TRADESMART_TOKEN_FILE=' not in out  # % cannot be quoted for cmd
    assert "cmd:        (this path cannot be quoted safely for cmd; use PowerShell)" in out
    assert f"`python -m tradesmart logout --token-file {quoted}` (PowerShell)" in out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/home/me/token.json", "'/home/me/token.json'"),
        ("/a b/$x/100%", "'/a b/$x/100%'"),
        ("/it's", "'/it'\\''s'"),
    ],
)
def test_posix_quote(text: str, expected: str) -> None:
    assert _cli._posix_quote(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("C:\\a b\\t.json", "'C:\\a b\\t.json'"),
        ("C:\\$x\\100%", "'C:\\$x\\100%'"),
        ("C:\\it's", "'C:\\it''s'"),
        (f"C:\\it{chr(0x2019)}s", f"'C:\\it{chr(0x2019) * 2}s'"),  # a typographic quote
    ],
)
def test_powershell_quote(text: str, expected: str) -> None:
    assert _cli._powershell_quote(text) == expected


def test_cli_messages_are_ascii_only() -> None:
    """Redirected stderr on a legacy Windows code page must never fail to encode a message."""
    source = Path(_cli.__file__).read_text(encoding="utf-8")
    assert source.isascii(), [ln for ln in source.splitlines() if not ln.isascii()]


def test_paste_url_path(
    creds_env: None, no_browser: list[str], answers: list[str], _no_saved_login: Path
) -> None:
    answers.append("http://127.0.0.1:8080/callback?state=x&code=test-code")
    with respx.mock(assert_all_mocked=True) as router:
        route = _gen_acs_tok(router)
        assert _cli.main(["login", "--no-browser"]) == 0
    assert '"code":"test-code"' in route.calls.last.request.content.decode()
    assert _no_saved_login.exists()


@pytest.mark.parametrize("pasted", ["", "http://127.0.0.1:8080/callback?state=x"])
def test_paste_without_code_fails(
    creds_env: None,
    no_browser: list[str],
    answers: list[str],
    pasted: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers.append(pasted)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        assert _cli.main(["login", "--no-browser"]) == 1
    assert not router.calls
    assert "code" in capsys.readouterr().err


def test_timeout_falls_back_to_paste(
    creds_env: None,
    no_browser: list[str],
    answers: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_cli, "_CALLBACK_TIMEOUT", 0.2)
    answers.append("test-code")
    with respx.mock(assert_all_mocked=True) as router:
        _gen_acs_tok(router)
        assert _cli.main(["login", "--port", str(_free_port())]) == 0
    assert len(no_browser) == 1


def test_busy_port_falls_back_to_paste(
    creds_env: None,
    no_browser: list[str],
    answers: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers.append("test-code")
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        with respx.mock(assert_all_mocked=True) as router:
            _gen_acs_tok(router)
            assert _cli.main(["login", "--port", str(port)]) == 0
    assert str(port) in capsys.readouterr().err


def test_prompts_for_missing_credentials_and_hides_keys(
    no_creds_env: None,
    no_browser: list[str],
    answers: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hidden: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        hidden.append(prompt)
        return {1: APP_KEY, 2: SECRET}[len(hidden)]

    monkeypatch.setattr(_cli.getpass, "getpass", fake_getpass)
    answers.extend(["TEST123", "test-code"])  # client id, then the pasted code
    with respx.mock(assert_all_mocked=True) as router:
        _gen_acs_tok(router)
        assert _cli.main(["login", "--no-browser"]) == 0
    assert len(hidden) == 2 and "secret" in hidden[1].lower()
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err


def test_flags_override_environment(
    creds_env: None,
    no_browser: list[str],
    answers: list[str],
    monkeypatch: pytest.MonkeyPatch,
    _no_saved_login: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers.append("test-code")
    other_account = {**fixture("gen_acs_tok_success"), "uid": "OTHER1", "actid": "OTHER1"}
    with respx.mock(assert_all_mocked=True) as router:
        router.post(f"{BASE}/GenAcsTok").respond(200, json=other_account)
        code = _cli.main(
            ["login", "--no-browser", "--client-id", "OTHER1", "--app-key", "other-app-key"]
        )
    assert code == 0
    assert "?client_id=other-app-key" in capsys.readouterr().out
    assert json.loads(_no_saved_login.read_text(encoding="utf-8"))["client_id"] == "OTHER1"


@pytest.mark.parametrize(
    ("overrides", "account"),
    [({}, "TEST123"), ({"uid": None, "actid": "TEST123"}, "TEST123")],
    ids=["uid", "actid-only"],
)
def test_token_for_another_account_is_not_saved(
    creds_env: None,
    no_browser: list[str],
    answers: list[str],
    _no_saved_login: Path,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, str | None],
    account: str,
) -> None:
    """A redirect carrying someone else's code (or a typo) must not be saved as this login."""
    answers.append("test-code")
    body = {**fixture("gen_acs_tok_success"), **overrides}
    body = {key: value for key, value in body.items() if value is not None}
    with respx.mock(assert_all_mocked=True) as router:
        router.post(f"{BASE}/GenAcsTok").respond(200, json=body)
        assert _cli.main(["login", "--no-browser", "--client-id", "VICTIM9"]) == 1
    assert not _no_saved_login.exists()
    out = capsys.readouterr()
    assert f"This login belongs to account {account}, not VICTIM9 - nothing was saved." in out.err
    _assert_no_secrets(out.out + out.err)


def test_matching_account_is_saved_as_the_server_spells_it(
    creds_env: None, no_browser: list[str], answers: list[str], _no_saved_login: Path
) -> None:
    answers.append("test-code")
    with respx.mock(assert_all_mocked=True) as router:
        _gen_acs_tok(router)
        assert _cli.main(["login", "--no-browser", "--client-id", "test123"]) == 0
    assert json.loads(_no_saved_login.read_text(encoding="utf-8"))["client_id"] == "TEST123"


def test_token_without_account_ids_is_saved_with_the_typed_client_id(
    creds_env: None, no_browser: list[str], answers: list[str], _no_saved_login: Path
) -> None:
    """GenAcsTok bodies without ``uid``/``actid`` cannot be checked: the typed id is kept."""
    answers.append("test-code")
    body = {
        key: value
        for key, value in fixture("gen_acs_tok_success").items()
        if key not in ("uid", "actid")
    }
    with respx.mock(assert_all_mocked=True) as router:
        router.post(f"{BASE}/GenAcsTok").respond(200, json=body)
        assert _cli.main(["login", "--no-browser", "--client-id", "typed1"]) == 0
    assert json.loads(_no_saved_login.read_text(encoding="utf-8"))["client_id"] == "typed1"


def test_wait_polls_in_short_slices_so_ctrl_c_is_seen() -> None:
    class FakeServer:
        auth_code: str | None = None
        timeout: float | None = None

        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def handle_request(self) -> None:
            assert self.timeout is not None
            self.timeouts.append(self.timeout)
            if len(self.timeouts) == 3:
                self.auth_code = "test-code"

    server = FakeServer()
    assert _cli._wait_for_code(server, 120.0) == "test-code"  # type: ignore[arg-type]
    assert server.timeouts and max(server.timeouts) <= 0.5


def test_callback_server_errors_are_silent(capsys: pytest.CaptureFixture[str]) -> None:
    server = _cli._CallbackServer(("127.0.0.1", 0), _cli._CallbackHandler)
    try:
        try:
            raise ConnectionResetError("browser went away")
        except ConnectionResetError:
            server.handle_error(None, ("127.0.0.1", 1))  # type: ignore[arg-type]
    finally:
        server.server_close()
    out = capsys.readouterr()
    assert out.err == "" and out.out == ""


def test_login_help_warns_about_flags_and_port(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _cli.main(["login", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "prefer TRADESMART_APP_KEY or the prompt" in text
    assert "visible to other local users" in text
    assert "must match the Redirect URL registered with TradeSmart" in text


def test_failure_exits_1_without_secrets(
    creds_env: None,
    no_browser: list[str],
    answers: list[str],
    _no_saved_login: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers.append("test-code")
    with respx.mock(assert_all_mocked=True) as router:
        route = router.post(f"{BASE}/GenAcsTok").respond(200, json=fixture("errors")["no_data"])
        assert _cli.main(["login", "--no-browser"]) == 1
    assert route.call_count == 1  # never retried: the code is single-use
    out = capsys.readouterr()
    assert "Login failed" in out.err
    assert fixture("errors")["no_data"]["emsg"] in out.err
    _assert_no_secrets(out.out + out.err)
    assert not _no_saved_login.exists()


def test_unwritable_token_file_exits_1_without_the_token(
    creds_env: None,
    no_browser: list[str],
    answers: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers.append("test-code")
    target = tmp_path / "token.json"
    target.mkdir()
    with respx.mock(assert_all_mocked=True) as router:
        _gen_acs_tok(router)
        assert _cli.main(["login", "--no-browser", "--token-file", str(target)]) == 1
    out = capsys.readouterr()
    assert str(target) in out.err
    _assert_no_secrets(out.out + out.err)


@pytest.mark.parametrize("missing", ["client id", "app key", "secret key"])
def test_empty_credentials_are_usage_errors(
    no_creds_env: None,
    no_browser: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    values = {"client id": "TEST123", "app key": APP_KEY, "secret key": SECRET}
    values[missing] = " "
    monkeypatch.setattr("builtins.input", lambda prompt="": values["client id"])
    hidden = iter([values["app key"], values["secret key"]])
    monkeypatch.setattr(_cli.getpass, "getpass", lambda prompt="": next(hidden))
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        assert _cli.main(["login", "--no-browser"]) == 2
    assert not router.calls
    assert missing in capsys.readouterr().err.lower()


def test_no_terminal_input_is_a_usage_error(
    no_creds_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert _cli.main(["login", "--no-browser"]) == 2
    assert "TRADESMART_CLIENT_ID" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["0", "70000", "x"])
def test_bad_port_is_a_usage_error(port: str) -> None:
    with pytest.raises(SystemExit) as info:
        _cli.main(["login", "--port", port])
    assert info.value.code == 2


def test_token_path_prints_the_path(
    _no_saved_login: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli.main(["token-path"]) == 0
    assert capsys.readouterr().out.strip() == str(_no_saved_login)


def test_token_path_honours_token_file_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli.main(["token-path", "--token-file", str(tmp_path / "x.json")]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "x.json")


def test_logout_removes_the_file(_no_saved_login: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _no_saved_login.write_text("{}", encoding="utf-8")
    assert _cli.main(["logout"]) == 0
    assert not _no_saved_login.exists()
    assert str(_no_saved_login) in capsys.readouterr().out


def test_logout_without_saved_login(
    _no_saved_login: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli.main(["logout"]) == 0
    assert "no saved login" in capsys.readouterr().out.lower()


def test_logout_failure_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "token.json"
    target.mkdir()
    (target / "keep").write_text("x", encoding="utf-8")
    assert _cli.main(["logout", "--token-file", str(target)]) == 1
    assert str(target) in capsys.readouterr().err


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_cli_token_file_is_owner_only(
    creds_env: None, no_browser: list[str], answers: list[str], _no_saved_login: Path
) -> None:
    answers.append("test-code")
    with respx.mock(assert_all_mocked=True) as router:
        _gen_acs_tok(router)
        assert _cli.main(["login", "--no-browser"]) == 0
    assert stat.S_IMODE(_no_saved_login.stat().st_mode) == 0o600


@pytest.mark.parametrize("args", [["--help"], ["login", "--help"], ["logout", "--help"]])
def test_help_exits_0(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        _cli.main(args)
    assert info.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_main_module_runs_the_cli(
    monkeypatch: pytest.MonkeyPatch, _no_saved_login: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["tradesmart", "token-path"])
    with pytest.raises(SystemExit) as info:
        runpy.run_module("tradesmart", run_name="__main__")
    assert info.value.code == 0
    assert capsys.readouterr().out.strip() == str(_no_saved_login)


def test_ctrl_c_is_reported_without_a_traceback(
    creds_env: None,
    no_browser: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupt(prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    assert _cli.main(["login", "--no-browser"]) == 1
    assert "Cancelled" in capsys.readouterr().err


def test_python_dash_m_help_exits_0() -> None:
    result = subprocess.run(  # noqa: S603 - fixed arguments
        [sys.executable, "-m", "tradesmart", "--help"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "login" in result.stdout and "logout" in result.stdout


# --- --listen-host: automatic login from inside a container ---------------------------------

_ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - the container-side bind under test


def _settled(results: list[int], count: int, timeout: float = 5.0) -> list[int]:
    """``results`` once a helper thread has added ``count`` items (or after ``timeout``).

    The login returns as soon as the callback server has the code, which can be before the
    thread that sent the request has stored the reply: checking at once is a race.
    """
    deadline = time.monotonic() + timeout
    while len(results) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    return results


def _redirect_after_open(port: int, replies: list[int]) -> Callable[[str], bool]:
    """A ``webbrowser.open`` stand-in that delivers the redirect over 127.0.0.1."""

    def fake_open(url: str) -> bool:
        def redirect() -> None:
            target = f"http://127.0.0.1:{port}/callback?code=test-code"
            with urllib.request.urlopen(target, timeout=5) as resp:  # noqa: S310 - local URL
                replies.append(resp.status)

        threading.Thread(target=redirect).start()
        return True

    return fake_open


@pytest.mark.parametrize("host", ["127.0.0.1", _ALL_INTERFACES])
def test_listen_host_binds_the_given_address(
    host: str,
    creds_env: None,
    _no_saved_login: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    port = _free_port()
    replies: list[int] = []
    bound: list[str] = []
    real_init = _cli._CallbackServer.__init__

    def spy(self: _cli._CallbackServer, address: tuple[str, int], handler: Any) -> None:
        bound.append(address[0])
        real_init(self, address, handler)

    monkeypatch.setattr(_cli._CallbackServer, "__init__", spy)
    monkeypatch.setattr(_cli.webbrowser, "open", _redirect_after_open(port, replies))
    with respx.mock(assert_all_mocked=True) as router:
        _gen_acs_tok(router)
        assert _cli.main(["login", "--port", str(port), "--listen-host", host]) == 0
    assert bound == [host]
    assert _settled(replies, 1) == [200]
    out = capsys.readouterr()
    assert f"http://127.0.0.1:{port}/callback" in out.out  # the redirect URL is always loopback
    assert _ALL_INTERFACES not in out.out
    if host == _ALL_INTERFACES:  # one warning, on stderr, only for a non-loopback address
        assert out.err.count("Warning") == 1
        assert "login code" in out.err
        assert "published to 127.0.0.1" in out.err
    else:
        assert "Warning" not in out.err


def test_default_listen_host_is_loopback_and_silent(
    creds_env: None,
    _no_saved_login: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    port = _free_port()
    bound: list[str] = []
    real_init = _cli._CallbackServer.__init__

    def spy(self: _cli._CallbackServer, address: tuple[str, int], handler: Any) -> None:
        bound.append(address[0])
        real_init(self, address, handler)

    monkeypatch.setattr(_cli._CallbackServer, "__init__", spy)
    monkeypatch.setattr(_cli.webbrowser, "open", _redirect_after_open(port, []))
    with respx.mock(assert_all_mocked=True) as router:
        _gen_acs_tok(router)
        assert _cli.main(["login", "--port", str(port)]) == 0
    assert bound == ["127.0.0.1"]
    assert "Warning" not in capsys.readouterr().err


@pytest.mark.parametrize("host", ["localhost", "example.com", "", "300.1.1.1", "0.0.0.0/0"])
def test_invalid_listen_host_is_a_usage_error(
    host: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as info:
        _cli.main(["login", "--listen-host", host])
    assert info.value.code == 2
    assert "not an IP address" in capsys.readouterr().err


def test_listen_host_is_documented_in_login_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _cli.main(["login", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--listen-host HOST" in text
    assert "container" in text
    assert "127.0.0.1:8080:8080" in text


def test_no_browser_still_does_not_listen(
    creds_env: None,
    _no_saved_login: Path,
    answers: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not listen with --no-browser")

    monkeypatch.setattr(_cli, "_CallbackServer", boom)
    answers.append("test-code")
    with respx.mock(assert_all_mocked=True) as router:
        _gen_acs_tok(router)
        assert _cli.main(["login", "--no-browser", "--listen-host", _ALL_INTERFACES]) == 0


def test_without_a_browser_the_url_is_shown_and_the_redirect_is_awaited(
    creds_env: None,
    _no_saved_login: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    port = _free_port()
    replies: list[int] = []
    deliver = _redirect_after_open(port, replies)
    monkeypatch.setattr(_cli.webbrowser, "open", lambda url: deliver(url) and False)
    with respx.mock(assert_all_mocked=True) as router:
        _gen_acs_tok(router)
        assert _cli.main(["login", "--port", str(port)]) == 0
    text = " ".join(capsys.readouterr().out.split())
    assert (
        "Open this URL in your browser and log in; this window continues by itself "
        "after the redirect." in text
    )
    assert _settled(replies, 1) == [200]


_COMPOSE = Path(__file__).resolve().parents[2] / "compose.yaml"


def _compose_service(name: str) -> list[str]:
    """Lines of a ``services:`` entry (text check; no YAML dependency)."""
    lines = _COMPOSE.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"  {name}:")
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("    "):
            break
        block.append(line)
    return block


def test_compose_login_service_publishes_only_host_loopback() -> None:
    block = _compose_service("login")
    ports = [ln.strip() for ln in block if ln.strip().startswith("- ") and ":8080" in ln]
    assert ports == ['- "127.0.0.1:8080:8080"']
    assert not any("0.0.0.0:" in ln for ln in block)


def test_relative_token_file_is_resolved_to_an_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _cli.main(["token-path", "--token-file", "rel.json"]) == 0
    printed = capsys.readouterr().out.strip()
    assert Path(printed).is_absolute()
    assert Path(printed) == tmp_path / "rel.json"


def test_relative_token_file_variable_is_printed_as_an_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRADESMART_TOKEN_FILE", os.path.join("rel", "token.json"))
    assert _cli.main(["token-path"]) == 0
    assert Path(capsys.readouterr().out.strip()) == tmp_path / "rel" / "token.json"


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_symlinked_token_file_keeps_its_link_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert _cli.main(["token-path", "--token-file", str(link)]) == 0
    assert capsys.readouterr().out.strip() == str(link)


def test_login_with_relative_token_file_prints_absolute_hints(
    creds_env: None,
    no_browser: list[str],
    answers: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    answers.append("test-code")
    with respx.mock(assert_all_mocked=True) as router:
        _gen_acs_tok(router)
        assert _cli.main(["login", "--no-browser", "--token-file", "tok.json"]) == 0
    out = capsys.readouterr().out
    assert str(tmp_path / "tok.json") in out
    assert "'tok.json'" not in out and '"tok.json"' not in out


def test_idle_connection_does_not_delay_the_real_redirect(
    creds_env: None, _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _free_port()
    idle: list[socket.socket] = []
    replies: list[int] = []

    def fake_open(url: str) -> bool:
        idle.append(socket.create_connection(("127.0.0.1", port), timeout=5))  # sends nothing

        def redirect() -> None:
            with urllib.request.urlopen(  # noqa: S310 - fixed local URL
                f"http://127.0.0.1:{port}/callback?code=test-code", timeout=5
            ) as resp:
                replies.append(resp.status)

        threading.Thread(target=redirect).start()
        return True

    monkeypatch.setattr(_cli.webbrowser, "open", fake_open)
    started = time.monotonic()
    try:
        with respx.mock(assert_all_mocked=True) as router:
            _gen_acs_tok(router)
            assert _cli.main(["login", "--port", str(port)]) == 0
    finally:
        for sock in idle:
            sock.close()
    assert time.monotonic() - started < 8
    assert _settled(replies, 1) == [200]


def test_code_is_published_only_after_the_reply_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _free_port()
    server = _cli._CallbackServer(("127.0.0.1", port), _cli._CallbackHandler)
    real_reply = _cli._CallbackHandler._reply
    reply_done = threading.Event()

    def slow_reply(self: _cli._CallbackHandler, status: int, text: str) -> None:
        time.sleep(0.8)
        real_reply(self, status, text)
        reply_done.set()

    monkeypatch.setattr(_cli._CallbackHandler, "_reply", slow_reply)
    result: dict[str, Any] = {}

    def browser() -> None:
        url = f"http://127.0.0.1:{port}/callback?code=test-code"
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            result["status"] = resp.status
            result["body"] = resp.read().decode()

    thread = threading.Thread(target=browser)
    thread.start()
    try:
        assert _cli._wait_for_code(server, 10.0) == "test-code"
        assert reply_done.is_set()  # the reply was written before the code became visible
        thread.join(5)
        assert result["status"] == 200 and "Login complete" in result["body"]
    finally:
        thread.join(5)
        server.server_close()


def test_code_is_published_even_when_the_reply_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _free_port()
    server = _cli._CallbackServer(("127.0.0.1", port), _cli._CallbackHandler)

    def broken_reply(self: _cli._CallbackHandler, status: int, text: str) -> None:
        raise BrokenPipeError("browser closed early")

    monkeypatch.setattr(_cli._CallbackHandler, "_reply", broken_reply)

    def browser() -> None:
        with contextlib.suppress(OSError):  # the server never answers
            urllib.request.urlopen(  # noqa: S310
                f"http://127.0.0.1:{port}/callback?code=test-code", timeout=5
            ).close()

    thread = threading.Thread(target=browser)
    thread.start()
    try:
        assert _cli._wait_for_code(server, 10.0) == "test-code"
    finally:
        thread.join(5)
        server.server_close()


def test_second_callback_gets_login_already_received() -> None:
    port = _free_port()
    server = _cli._CallbackServer(("127.0.0.1", port), _cli._CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def get(code: str) -> tuple[int, str]:
        url = f"http://127.0.0.1:{port}/callback?code={code}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
                return int(resp.status), str(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    try:
        assert get("first")[0] == 200
        status, body = get("second")
        assert status != 200 and "already received" in body
        deadline = time.monotonic() + 5
        while server.auth_code is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.auth_code == "first"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("host", ["::1", "::", "fe80::1"])
def test_ipv6_listen_host_is_a_usage_error(host: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        _cli.main(["login", "--listen-host", host])
    assert info.value.code == 2
    assert "IPv4" in capsys.readouterr().err


def test_compose_login_service_builds_like_dev() -> None:
    login = [ln.strip() for ln in _compose_service("login")]
    assert "build:" in login
    assert "context: ." in login
    assert 'PYTHON_VERSION: "3.12"' in login


def test_pytest_addopts_exclude_the_order_suite_by_default() -> None:
    text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert "not live_orders" in text


def test_every_live_order_test_module_carries_the_marker() -> None:
    folder = Path(__file__).resolve().parents[1] / "live_orders"
    for module in folder.glob("test_*.py"):
        assert "pytestmark = pytest.mark.live_orders" in module.read_text(encoding="utf-8")


def test_no_license_classifier_next_to_the_license_expression() -> None:
    text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = "Apache-2.0"' in text
    assert 'license-files = ["LICENSE", "NOTICE"]' in text
    assert "License ::" not in text


def test_licence_files_are_apache_2_0_owned_by_the_company() -> None:
    root = Path(__file__).resolve().parents[2]
    licence = (root / "LICENSE").read_text(encoding="utf-8")
    notice = (root / "NOTICE").read_text(encoding="utf-8")
    assert "Apache License" in licence
    assert "Version 2.0, January 2004" in licence
    assert "MIT License" not in licence
    assert "Copyright 2026 TradeSmart Fintech Securities Limited" in notice


def test_compose_login_service_entrypoint_listens_on_all_container_interfaces() -> None:
    text = "\n".join(_compose_service("login"))
    assert 'entrypoint: ["python", "-m", "tradesmart", "login", "--listen-host", "0.0.0.0"]' in text
    assert "tradesmart-login:/root/.config/tradesmart" in "\n".join(
        _COMPOSE.read_text(encoding="utf-8").splitlines()
    )
