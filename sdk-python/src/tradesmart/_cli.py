"""``python -m tradesmart``: log in once a day and save the token for ``TradeSmart.from_env()``.

Commands: ``login`` (the default), ``token-path`` and ``logout``. This module is the only part of
the package that prints; it never prints the access token, the secret key or the login code.

After a successful login the App Key and Secret key typed at the prompt (or the ``--app-key``
option) are saved per client id in ``credentials.json`` next to the token file
(:mod:`tradesmart._key_store`; ``--no-save-keys`` skips it; keys from environment variables are
never saved). The next login takes each key from the option / environment, else the saved entry,
else the prompt. ``logout --forget-keys [--client-id ID]`` removes them.
Exit codes: 0 success, 1 login failed, 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import os
import socketserver
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from tradesmart import _key_store, _token_store
from tradesmart.client import TradeSmart
from tradesmart.errors import TradeSmartError
from tradesmart.models.base import IST

_PROG = "python -m tradesmart"
_HOST = "127.0.0.1"  # loopback only: the callback is never exposed on other interfaces
_CALLBACK_PATH = "/callback"
_CALLBACK_TIMEOUT = 120.0
_POLL_SECONDS = 0.5
_DEFAULT_PORT = 8080
_COMMANDS = ("login", "token-path", "logout")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


class _UsageError(Exception):
    """A missing or invalid setting: exit code 2."""


class _CallbackServer(socketserver.ThreadingMixIn, HTTPServer):
    """One-shot loopback HTTP server remembering the ``code`` of the OAuth redirect.

    Each connection is served in its own daemon thread, so an idle pre-connect (browsers open
    speculative connections) cannot delay the real redirect; the first valid code wins.
    """

    daemon_threads = True
    # On Windows SO_REUSEADDR would let us bind a port another program is listening on.
    allow_reuse_address = os.name != "nt"
    auth_code: str | None = None  # published (seen by the waiting thread) after the reply is sent

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.code_lock = threading.Lock()
        self._claimed = False
        super().__init__(*args, **kwargs)

    def claim_code(self) -> bool:
        """Win the single use of the callback; ``False`` when a code was already received."""
        with self.code_lock:
            if self._claimed:
                return False
            self._claimed = True
            return True

    def server_bind(self) -> None:
        # HTTPServer.server_bind resolves the host's FQDN, which can stall for seconds.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Stay silent: a browser closing its connection early is not worth a traceback."""


class _CallbackHandler(BaseHTTPRequestHandler):
    """Stores the ``code`` query parameter of a ``GET /callback`` on the server."""

    timeout = 10  # a connection that sends nothing cannot block the login

    def do_GET(self) -> None:  # noqa: N802 - name required by BaseHTTPRequestHandler
        url = urlparse(self.path)
        if url.path.rstrip("/") != _CALLBACK_PATH:
            self._reply(404, "Not found.")
            return
        codes = parse_qs(url.query).get("code")
        if codes and codes[0].strip():
            server = cast(_CallbackServer, self.server)
            if server.claim_code():
                try:
                    self._reply(
                        200,
                        "Login complete. You can close this window and return to the terminal.",
                    )
                finally:
                    # Publish only after the reply is written (the main thread may exit as soon
                    # as it sees the code); also when the browser went away mid-reply.
                    server.auth_code = codes[0].strip()
            else:
                self._reply(409, "Login already received. You can close this window.")
        else:
            self._reply(400, "No authorisation code in the redirect.")

    def _reply(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the default access log: the request line contains the code."""


def _say(message: str) -> None:
    print(message)


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _reason(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


def _wait_for_code(server: _CallbackServer, timeout: float) -> str | None:
    """Serve until a redirect carrying ``code`` arrives or ``timeout`` seconds pass."""
    deadline = time.monotonic() + timeout
    while server.auth_code is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        # Short slices: on Windows Ctrl-C is only seen between blocking select() calls.
        server.timeout = min(remaining, _POLL_SECONDS)
        server.handle_request()
    return server.auth_code


def _extract_code(text: str) -> str | None:
    """The ``code`` from a pasted redirect URL, or the pasted text itself when it is a bare code."""
    text = text.strip()
    if not text:
        return None
    if "://" in text or text.startswith(("?", "/")) or "code=" in text:
        parsed = urlparse(text)
        query = parsed.query or text.lstrip("?")
        codes = parse_qs(query).get("code")
        return (codes[0].strip() or None) if codes else None
    return text


def _paste_code() -> str | None:
    _say(
        "Paste the `code` from the address bar after logging in (or the whole URL) and press Enter."
    )
    try:
        return _extract_code(input("code or URL: "))
    except EOFError:
        return None


def _receive_code(url: str, port: int, use_browser: bool, listen_host: str = _HOST) -> str | None:
    """Get the authorisation code: browser + local callback first, then a pasted code or URL.

    The redirect URL is always ``http://127.0.0.1:PORT/callback``; ``listen_host`` is only the
    address the server binds (inside a container: ``0.0.0.0`` with the port published to the
    host's loopback).
    """
    if not use_browser:
        _say("Open this URL in a browser and log in:")
        _say(url)
        return _paste_code()
    if not ipaddress.ip_address(listen_host).is_loopback:
        _err(
            f"Warning: listening on {listen_host}:{port}. Anyone who can reach that address can "
            "deliver a login code; use --listen-host only inside a container whose port is "
            f"published to 127.0.0.1 (for example -p 127.0.0.1:{port}:{port}). The account check "
            "still refuses a token issued to another account."
        )
    server: _CallbackServer | None = None
    try:
        server = _CallbackServer((listen_host, port), _CallbackHandler)
    except OSError as exc:
        _err(f"Cannot listen on {listen_host}:{port} ({_reason(exc)}); paste the code instead.")
    try:
        if webbrowser.open(url):
            _say("Your browser shows the TradeSmart login page; log in there.")
        elif server is not None:
            _say(
                "Open this URL in your browser and log in; this window continues by itself "
                "after the redirect."
            )
            _say(url)
        else:
            _say("Open this URL in a browser and log in:")
            _say(url)
        if server is not None:
            _say(
                f"Waiting up to {_CALLBACK_TIMEOUT:.0f} s for the redirect to "
                f"http://{_HOST}:{port}{_CALLBACK_PATH} ..."
            )
            code = _wait_for_code(server, _CALLBACK_TIMEOUT)
            if code is not None:
                return code
            _err(
                "No redirect received in time. If no browser opened, run "
                f"`{_PROG} login --no-browser`."
            )
    finally:
        if server is not None:
            server.server_close()
    return _paste_code()


def _setting(
    value: str | None, env: str, label: str, flag: str | None, ask: Callable[[str], str]
) -> str:
    """A flag value, else the environment variable, else an interactive prompt."""
    if value is None:
        value = os.environ.get(env) or None
    if value is None:
        try:
            value = ask(f"{label[0].upper()}{label[1:]}: ")
        except EOFError:
            how = f"pass {flag} or set {env}" if flag else f"set {env}"
            raise _UsageError(f"No {label} given: {how}.") from None
    value = value.strip()
    if not value:
        raise _UsageError(f"The {label} must not be empty.")
    return value


def _ask_visible(prompt: str) -> str:
    return input(prompt)


def _ask_hidden(prompt: str) -> str:
    return getpass.getpass(prompt)


def _token_file(args: argparse.Namespace) -> Path:
    option = cast("str | None", args.token_file)
    # Absolute, so the printed TRADESMART_TOKEN_FILE and logout hints work from any directory
    # (token_path() makes a relative TRADESMART_TOKEN_FILE absolute too).
    if not option:
        return _token_store.token_path()
    return Path(os.path.abspath(os.path.expanduser(option)))  # abspath keeps a symlink's path


_SAVED = "saved"
_TYPED = "typed"  # a prompt answer or a command-line option: saved after a successful login
_FROM_ENV = "environment"  # already stored there: never saved


@dataclass(frozen=True, repr=False)
class _Keys:
    """The App Key and Secret for one login and where each came from (keys never in ``repr``)."""

    app_key: str
    secret_key: str
    app_key_source: str
    secret_key_source: str

    def __repr__(self) -> str:
        return f"_Keys(app_key=***, secret_key=***, sources={self.sources})"

    @property
    def sources(self) -> tuple[str, str]:
        return self.app_key_source, self.secret_key_source

    @property
    def any_saved(self) -> bool:
        return _SAVED in self.sources


def _quoted_arg(text: str) -> str:
    """``text`` as one command-line argument: as is when plain, else quoted for this platform."""
    if text and all(c.isascii() and (c.isalnum() or c in "-_.") for c in text):
        return text
    return _command_arg(text)[0]


def _resolve_keys(
    client_id: str,
    keys_path: Path,
    *,
    app_key_option: str | None,
    app_key_env: str,
    secret_key_env: str,
    forget_command: str,
    app_key_flag: str | None = "--app-key",
) -> _Keys:
    """App Key and Secret from, in order: the option / environment, the saved entry, a prompt.

    The saved-keys file is read only when a key is still missing; an unusable file is a usage
    error (its content is never shown).
    """
    found: dict[str, tuple[str, str] | None] = {
        "app_key": None,
        "secret_key": None,
    }
    if app_key_option is not None:
        found["app_key"] = (app_key_option, _TYPED)
    for name, env in (("app_key", app_key_env), ("secret_key", secret_key_env)):
        value = os.environ.get(env) or None
        if found[name] is None and value is not None:
            found[name] = (value, _FROM_ENV)
    if found["app_key"] is None or found["secret_key"] is None:
        try:
            saved = _key_store.get(keys_path, client_id)
        except _key_store.KeyFileError as exc:
            raise _UsageError(
                f"Cannot log in with saved keys: {exc}. Remove that file with "
                f"`{forget_command}` (it removes every saved key there and the saved login), "
                f"or set {app_key_env} and {secret_key_env}."
            ) from None
        if saved is not None:
            for name, value in (("app_key", saved.app_key), ("secret_key", saved.secret_key)):
                if found[name] is None and value is not None:
                    found[name] = (value, _SAVED)
    labels = {"app_key": "app key", "secret_key": "secret key"}
    for name in ("app_key", "secret_key"):
        if found[name] is None:
            try:
                found[name] = (_ask_hidden(f"{labels[name].capitalize()}: "), _TYPED)
            except EOFError:
                env = app_key_env if name == "app_key" else secret_key_env
                flag = app_key_flag if name == "app_key" else None
                how = f"pass {flag} or set {env}" if flag else f"set {env}"
                raise _UsageError(f"No {labels[name]} given: {how}.") from None
    app_key, app_key_source = cast("tuple[str, str]", found["app_key"])
    secret_key, secret_key_source = cast("tuple[str, str]", found["secret_key"])
    app_key, secret_key = app_key.strip(), secret_key.strip()
    for label, value in (("app key", app_key), ("secret key", secret_key)):
        if not value:
            raise _UsageError(f"The {label} must not be empty.")
    return _Keys(app_key, secret_key, app_key_source, secret_key_source)


def _saved_keys_hint(keys: _Keys, client_id: str, keys_path: Path, forget_command: str) -> None:
    """After a failed login: the saved keys may be wrong; say how to forget them (no loop)."""
    if not keys.any_saved:
        return
    _err(
        f"The keys saved for {client_id} in {keys_path} were used. If they are wrong or were "
        f"changed on the API Dashboard, forget them with "
        f"`{forget_command} --client-id {_quoted_arg(client_id)}` and log in again."
    )


def _remember_keys(keys: _Keys, client_id: str, keys_path: Path, forget_command: str) -> None:
    """Save the typed keys (never ones from the environment) after a successful login."""
    given = {
        "app_key": keys.app_key if keys.app_key_source == _TYPED else None,
        "secret_key": keys.secret_key if keys.secret_key_source == _TYPED else None,
    }
    if not any(given.values()):
        return
    try:
        _key_store.put(keys_path, client_id, **given)
    except (OSError, _key_store.KeyFileError) as exc:
        reason = _reason(exc) if isinstance(exc, OSError) else str(exc)
        _err(
            f"The keys could not be saved to {keys_path} ({reason}); the next login asks for "
            "them again."
        )
        return
    what = " and ".join(
        label
        for label, value in (("App Key", given["app_key"]), ("Secret key", given["secret_key"]))
        if value
    )
    _say(
        f"Saved the {what} for {client_id} to {keys_path} (readable only by you, not "
        "encrypted); the next login uses them."
    )
    _say(f"Forget them with `{forget_command} --client-id {_quoted_arg(client_id)}`.")


def _forget_command(base: str, path: Path) -> str:
    """``<base> logout --forget-keys`` naming ``path`` when it is not the default token file."""
    command = f"{base} logout"
    if path != _token_store.token_path():
        command += f" --token-file {_command_arg(str(path))[0]}"
    return command + " --forget-keys"


def _other_environment_keys(keys_path: Path) -> str | None:
    """Why the production keys file is refused: a link to another environment's keys file.

    Other environments keep their keys in ``<env>-credentials.json``; reading or rewriting one
    through the production name would mix the environments (the mirror of their own check).
    """
    real = Path(os.path.realpath(keys_path))
    name = real.name.casefold()
    if name != _key_store.FILE_NAME and name.endswith(f"-{_key_store.FILE_NAME}"):
        return (
            f"{keys_path} is a link to another environment's saved keys ({real.name}); remove "
            "the link (each environment keeps its own keys)."
        )
    return None


def _login(args: argparse.Namespace) -> int:
    path = _token_file(args)
    keys_path = _key_store.path_for(path)
    forget = _forget_command(_PROG, path)
    problem = _other_environment_keys(keys_path)
    if problem is not None:
        _err(f"Cannot use the saved keys: {problem}")
        return EXIT_USAGE
    try:
        client_id = _setting(
            args.client_id, "TRADESMART_CLIENT_ID", "client id", "--client-id", _ask_visible
        )
        keys = _resolve_keys(
            client_id,
            keys_path,
            app_key_option=args.app_key,
            app_key_env="TRADESMART_APP_KEY",
            secret_key_env="TRADESMART_SECRET_KEY",  # noqa: S106 - a variable name, not a key
            forget_command=forget,
        )
    except _UsageError as exc:
        _err(str(exc))
        return EXIT_USAGE
    if keys.any_saved:
        _say(f"Using the {_saved_what(keys)} saved for {client_id} in {keys_path}.")
    with TradeSmart(client_id, app_key=keys.app_key, secret_key=keys.secret_key) as client:
        code = _receive_code(
            client.login_url(),
            args.port,
            use_browser=not args.no_browser,
            listen_host=args.listen_host,
        )
        if code is None:
            _err(f"No authorisation code received; run `{_PROG} login` again.")
            _saved_keys_hint(keys, client_id, keys_path, forget)
            return EXIT_FAILED
        try:
            token = client.generate_session(code)  # never retried: the code is single-use
        except TradeSmartError as exc:  # SDK messages never contain secrets
            _err(f"Login failed: {exc}")
            _saved_keys_hint(keys, client_id, keys_path, forget)
            return EXIT_FAILED
    # TradeSmart defines no OAuth ``state``: any local process or page can deliver a code to
    # the callback, so only keep a token issued to the account the user asked for.
    for account in (token.user_id, token.account_id):
        if account and account.casefold() != client_id.casefold():
            _err(f"This login belongs to account {account}, not {client_id} - nothing was saved.")
            return EXIT_FAILED
    client_id = token.user_id or token.account_id or client_id  # the server's spelling
    try:
        saved = _token_store.save(client_id, token, path)
    except OSError as exc:
        _err(
            f"Logged in, but the token could not be saved to {path} ({_reason(exc)}). "
            f"Choose another file with --token-file PATH and run `{_PROG} login` again "
            "(the login code is single-use)."
        )
        return EXIT_FAILED
    expires = token.expires_at.astimezone(IST)
    _say(f"Logged in as {token.uname or client_id} ({token.user_id or client_id}).")
    _say(
        f"The token is valid until {expires:%Y-%m-%d %H:%M} IST "
        "(TradeSmart tokens expire daily at 06:00 IST)."
    )
    logout, note = f"{_PROG} logout", ""
    if saved == _token_store.token_path():
        _say(f"Saved to {saved}; TradeSmart.from_env() uses it.")
    else:
        _say(
            f"Saved to {saved}. For TradeSmart.from_env() to use this file, set "
            "TRADESMART_TOKEN_FILE in the environment of your scripts:"
        )
        for line in _env_hints(str(saved)):
            _say(f"  {line}")
        arg, powershell_only = _command_arg(str(saved))
        logout += f" --token-file {arg}"
        note = " (PowerShell)" if powershell_only else ""
    _say(f"Remove the saved login with `{logout}`{note}.")
    if not args.no_save_keys:
        _remember_keys(keys, client_id, keys_path, forget)
    return EXIT_OK


def _saved_what(keys: _Keys) -> str:
    """Which keys came from the saved file, in words."""
    names = [
        label
        for label, source in zip(("App Key", "Secret key"), keys.sources, strict=True)
        if source == _SAVED
    ]
    return " and ".join(names)


# PowerShell also treats the typographic quotes as quote characters.
_PS_SINGLE_QUOTES = "'" + "".join(map(chr, (0x2018, 0x2019, 0x201A, 0x201B)))
_PS_DOUBLE_QUOTES = '"' + "".join(map(chr, (0x201C, 0x201D, 0x201E)))


def _posix_quote(text: str) -> str:
    """``text`` single-quoted for sh/bash/zsh (nothing expands; ``'`` becomes ``'\\''``)."""
    return "'" + text.replace("'", "'\\''") + "'"


def _powershell_quote(text: str) -> str:
    """``text`` single-quoted for PowerShell (nothing expands; quotes are doubled)."""
    for quote in _PS_SINGLE_QUOTES:
        text = text.replace(quote, quote * 2)
    return f"'{text}'"


def _cmd_safe(text: str) -> bool:
    """Whether ``text`` survives double quotes in cmd.exe (``%`` always expands there)."""
    return "%" not in text and '"' not in text


def _env_hints(path: str) -> list[str]:
    """Commands setting ``TRADESMART_TOKEN_FILE`` for each shell of the current platform."""
    if _token_store._os_name() != "nt":
        return [f"export {_token_store.ENV_VAR}={_posix_quote(path)}"]
    cmd = (
        f'set "{_token_store.ENV_VAR}={path}"'
        if _cmd_safe(path)
        else "(this path cannot be quoted safely for cmd; use PowerShell)"
    )
    return [
        f"PowerShell: $env:{_token_store.ENV_VAR} = {_powershell_quote(path)}",
        f"cmd:        {cmd}",
    ]


def _command_arg(path: str) -> tuple[str, bool]:
    """``path`` quoted for a command line of the current platform; True if PowerShell-only.

    On Windows double quotes work in both PowerShell and cmd unless the path contains
    characters one of them expands (``$``, a backtick, ``%``) or a quote.
    """
    if _token_store._os_name() != "nt":
        return _posix_quote(path), False
    if _cmd_safe(path) and not any(c in path for c in "$`" + _PS_DOUBLE_QUOTES):
        return f'"{path}"', False
    return _powershell_quote(path), True


def _token_path(args: argparse.Namespace) -> int:
    _say(str(_token_file(args)))
    return EXIT_OK


def _remove_login(path: Path) -> int:
    """Delete the token file and say so."""
    try:
        removed = _token_store.delete(path)
    except OSError as exc:
        _err(f"Could not remove the saved login at {path} ({_reason(exc)}).")
        return EXIT_FAILED
    _say(f"Removed the saved login at {path}." if removed else f"No saved login at {path}.")
    return EXIT_OK


def _forget_keys(keys_path: Path, client_id: str | None, forget_command: str) -> int:
    """Remove the saved keys of ``client_id``, or the whole file when ``None``; say which."""
    if client_id is not None:
        try:
            forgotten = _key_store.forget(keys_path, client_id)
        except _key_store.KeyFileError as exc:
            _err(
                f"Cannot forget the keys of {client_id}: {exc}. Run `{forget_command}` "
                "(without --client-id) to remove the whole file."
            )
            return EXIT_FAILED
        except OSError as exc:
            _err(f"Could not update the saved keys at {keys_path} ({_reason(exc)}).")
            return EXIT_FAILED
        _say(
            f"Forgot the saved keys for {client_id} in {keys_path}."
            if forgotten
            else f"No saved keys for {client_id} in {keys_path}."
        )
        return EXIT_OK
    try:
        ids: list[str] | None = _key_store.client_ids(keys_path)
    except _key_store.KeyFileError:
        ids = None  # unusable: removed below without reading or showing it
    try:
        removed = _key_store.delete(keys_path)
    except OSError as exc:
        _err(f"Could not remove the saved keys at {keys_path} ({_reason(exc)}).")
        return EXIT_FAILED
    if not removed:
        _say(f"No saved keys at {keys_path}.")
    elif ids is None:
        _say(f"Removed the unreadable saved keys file {keys_path}.")
    else:
        _say(f"Forgot the saved keys of every client id ({', '.join(ids)}): removed {keys_path}.")
    return EXIT_OK


def _logout(args: argparse.Namespace) -> int:
    path = _token_file(args)
    status = _remove_login(path)
    if args.forget_keys:
        keys_path = _key_store.path_for(path)
        client_id = cast("str | None", args.client_id)
        client_id = client_id.strip() if client_id is not None else None  # blank: refused in main
        problem = _other_environment_keys(keys_path) if client_id is not None else None
        if problem is not None:  # forgetting one id would rewrite that file under this name
            _err(f"Cannot forget the keys of {client_id}: {problem}")
            return EXIT_FAILED
        forgot = _forget_keys(keys_path, client_id, _forget_command(_PROG, path))
        status = max(status, forgot)
    return status


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {text!r}") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 1-65535, not {port}")
    return port


def _ip_address(text: str) -> str:
    try:
        address = ipaddress.ip_address(text.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an IP address: {text!r}") from None
    if address.version != 4:
        raise argparse.ArgumentTypeError(
            f"{text!r} is an IPv6 address; --listen-host accepts IPv4 addresses only"
        )
    return str(address)


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--token-file",
        metavar="PATH",
        help="token file to use instead of TRADESMART_TOKEN_FILE or the per-user default",
    )
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Log in to TradeSmart and save the day's access token for "
        "TradeSmart.from_env(). Without a command, runs `login`.",
    )
    commands = parser.add_subparsers(dest="command", metavar="{login,token-path,logout}")
    login = commands.add_parser(
        "login",
        parents=[common],
        help="log in in the browser and save the access token (default)",
        description="Log in in the browser and save the access token (valid until 06:00 IST). "
        "The secret key is read from TRADESMART_SECRET_KEY or asked for without echo.",
    )
    login.add_argument("--client-id", help="your TradeSmart client id (else TRADESMART_CLIENT_ID)")
    login.add_argument(
        "--app-key",
        help="your app's App Key; prefer TRADESMART_APP_KEY or the prompt (command-line "
        "arguments are visible to other local users)",
    )
    login.add_argument(
        "--port",
        type=_port,
        default=_DEFAULT_PORT,
        help=f"local port of the redirect URL http://{_HOST}:PORT{_CALLBACK_PATH}; must match "
        f"the Redirect URL registered with TradeSmart (default {_DEFAULT_PORT})",
    )
    login.add_argument(
        "--listen-host",
        metavar="HOST",
        type=_ip_address,
        default=_HOST,
        help=f"IP address the local redirect server binds (default {_HOST}). In a container "
        "use 0.0.0.0 with the port published to the host's loopback only (-p "
        "127.0.0.1:8080:8080); the redirect URL stays http://127.0.0.1:PORT/callback. "
        "Any other address lets anyone who can reach it deliver a login code",
    )
    login.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open a browser or listen for the redirect; print the URL and ask for the code",
    )
    login.add_argument(
        "--no-save-keys",
        action="store_true",
        help="do not save the App Key and Secret key typed at the prompt (or given with "
        "--app-key). By default they are saved after a successful login, per client id, in "
        "credentials.json next to the token file (owner-only, not encrypted); keys from "
        "environment variables are never saved",
    )
    login.set_defaults(handler=_login)
    commands.add_parser(
        "token-path", parents=[common], help="print the token file path"
    ).set_defaults(handler=_token_path)
    logout = commands.add_parser(
        "logout",
        parents=[common],
        help="delete the saved token file (and, with --forget-keys, the saved keys)",
        description="Delete the saved token file. The saved App Key and Secret key are kept "
        "unless --forget-keys is given.",
    )
    logout.add_argument(
        "--forget-keys",
        action="store_true",
        help="also remove the saved App Key and Secret key: of --client-id only, or of every "
        "client id when --client-id is not given",
    )
    logout.add_argument(
        "--client-id", help="with --forget-keys: forget only the keys of this client id"
    )
    logout.set_defaults(handler=_logout, parser=logout)
    return parser


def _check_logout_args(args: argparse.Namespace) -> None:
    """``--client-id`` needs ``--forget-keys`` and must not be blank (exit 2, nothing removed).

    Only an omitted ``--client-id`` forgets every client id's keys; a blank one (an unset shell
    variable, say) must never do that by accident. Shared with the environment login tool.
    """
    if args.client_id is None:
        return
    parser = cast(argparse.ArgumentParser, args.parser)
    if not args.forget_keys:
        parser.error(
            "--client-id is used only with --forget-keys (logout always removes the token file)"
        )
    if not args.client_id.strip():
        parser.error(
            "--client-id must not be blank; leave it out to forget the keys of every client id"
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line; returns the exit code."""
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list or args_list[0] not in (*_COMMANDS, "-h", "--help"):
        args_list.insert(0, "login")
    args = _parser().parse_args(args_list)
    if args.command == "logout":
        _check_logout_args(args)
    handler = cast("Callable[[argparse.Namespace], int]", args.handler)
    try:
        return handler(args)
    except KeyboardInterrupt:
        _err("Cancelled.")
        return EXIT_FAILED
