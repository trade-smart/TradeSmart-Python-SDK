"""OAuth login: capture the authorisation ``code`` on localhost and exchange it for a token.

Server-side only: the secret key must never reach a browser or mobile app.

For your own account the built-in ``python -m tradesmart login`` does all of this and saves
the token for ``TradeSmart.from_env()``. This script shows the same flow with the SDK's calls,
to adapt for a server that logs in other users.

Prerequisites
-------------
* Your app's registered Redirect URL points at this script, e.g.
  ``http://127.0.0.1:8080/callback`` (for a deployed app it is an endpoint on *your* server).
* Environment variables: ``TRADESMART_CLIENT_ID``, ``TRADESMART_APP_KEY``,
  ``TRADESMART_SECRET_KEY``. Optional: ``TRADESMART_CALLBACK_PORT`` (default 8080).

Run: ``python examples/auth_server_flow.py [--save-token PATH]``

The script prints the authorise URL, waits for the browser redirect, calls ``GenAcsTok``
and prints only the token's expiry. It never prints the token, the code or the checksum.
TradeSmart's OAuth has no ``state`` parameter, so the script refuses (and saves nothing) when
the token's ``uid`` / ``actid`` is not ``TRADESMART_CLIENT_ID``.

``--save-token PATH`` (opt-in) writes the access token -- and nothing else -- to ``PATH``,
created with owner-only permissions (``0o600``; on Windows the file inherits the folder's
permissions, so keep it inside your user profile). ``examples/first_call.py --token-file PATH``
reads it back. TradeSmart access tokens are valid until the next 06:00 IST; treat the file
like a password and delete it when you no longer need it.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from tradesmart import AuthenticationError, TradeSmart, TradeSmartError

_HOST = "127.0.0.1"  # loopback only: never expose the callback on other interfaces
_CALLBACK_PATH = "/callback"
_WAIT_SECONDS = 300.0


class _CallbackServer(ThreadingMixIn, HTTPServer):
    """One-shot HTTP server remembering the first ``code`` of the OAuth redirect.

    Each connection gets its own thread, so an idle browser pre-connect cannot block the redirect.
    """

    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows it would allow binding a busy port
    auth_code: str | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.code_lock = threading.Lock()
        self.claimed = False  # the first valid code wins
        super().__init__(*args, **kwargs)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Stay silent: a browser closing its connection early is not worth a traceback."""


class _CallbackHandler(BaseHTTPRequestHandler):
    """Stores the ``code`` query parameter of ``GET /callback`` on the server."""

    timeout = 10  # a connection that sends nothing cannot hold its thread for long

    def do_GET(self) -> None:  # noqa: N802 - name required by BaseHTTPRequestHandler
        url = urlparse(self.path)
        if url.path.rstrip("/") != _CALLBACK_PATH:
            self._reply(404, "Not found.")
            return
        codes = parse_qs(url.query).get("code")
        if not (codes and codes[0].strip()):
            self._reply(400, "No authorisation code in the redirect.")
            return
        server = cast(_CallbackServer, self.server)
        with server.code_lock:
            first, server.claimed = not server.claimed, True
        if not first:
            self._reply(409, "Login already received. You can close this window.")
            return
        try:
            self._reply(200, "Login complete. You can close this window.")
        finally:  # publish after the reply: the main thread may move on as soon as it sees it
            server.auth_code = codes[0].strip()

    def _reply(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the default access log: the request line contains the code."""


def wait_for_code(port: int, timeout: float = _WAIT_SECONDS) -> str | None:
    """Serve on ``127.0.0.1:port`` until a redirect carrying ``code`` arrives (or timeout)."""
    server = _CallbackServer((_HOST, port), _CallbackHandler)
    deadline = time.monotonic() + timeout
    try:
        while server.auth_code is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            server.timeout = min(remaining, 0.5)  # short slices: Ctrl-C works on Windows too
            server.handle_request()
        return server.auth_code
    finally:
        server.server_close()


def save_token(path: str | Path, token: str) -> Path:
    """Write ``token`` (and nothing else) to ``path`` (``~`` expanded), owner-only; the path.

    Raises ``OSError`` when the file cannot be written (missing folder, no permission ...).
    """
    target = Path(path).expanduser()
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):  # an already existing file would otherwise keep its old mode
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeSmart OAuth login on localhost.")
    parser.add_argument(
        "--save-token",
        metavar="PATH",
        help="write the access token to PATH (owner-only permissions) for first_call.py",
    )
    args = parser.parse_args(argv)
    port = int(os.environ.get("TRADESMART_CALLBACK_PORT", "8080"))
    try:
        client = TradeSmart.from_env()  # TRADESMART_CLIENT_ID / _APP_KEY / _SECRET_KEY
    except TradeSmartError as exc:
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return 2
    try:
        try:
            url = client.login_url()
        except TradeSmartError as exc:  # e.g. TRADESMART_APP_KEY not set
            print(f"Configuration problem: {exc}", file=sys.stderr)
            return 2
        print("Open this URL in your browser and log in:")
        print(url)
        webbrowser.open(url)
        code = wait_for_code(port)
        if code is None:
            print("Timed out waiting for the redirect.", file=sys.stderr)
            return 1
        try:
            token = client.generate_session(code)  # never retried: the code is single-use
        except AuthenticationError as exc:
            print(f"Token exchange failed: {exc.emsg or exc.message}", file=sys.stderr)
            return 1
        except TradeSmartError as exc:  # e.g. network failure, missing secret key
            print(f"Token exchange failed: {exc}", file=sys.stderr)
            return 1
        # TradeSmart's OAuth has no ``state``: any local page or process can deliver a code to
        # the callback, so keep only a token issued to the account we expect.
        for account in (token.user_id, token.account_id):
            if account and account.casefold() != client.client_id.casefold():
                print(
                    f"This login belongs to account {account}, not {client.client_id} - "
                    "nothing was saved.",
                    file=sys.stderr,
                )
                return 1
        print(f"Access token stored on the client; it expires at {token.expires_at.isoformat()}")
        if args.save_token:
            try:
                saved = save_token(args.save_token, token.access_token.get_secret_value())
            except OSError as exc:  # the token itself is never printed
                print(
                    f"Access token obtained but could not be saved to {args.save_token} "
                    f"({exc.strerror or type(exc).__name__}). Fix the path (the folder must "
                    "exist and be writable) and run this script again with --save-token PATH; "
                    "the login code is single-use, so you will log in once more.",
                    file=sys.stderr,
                )
                return 1
            print(f"Access token saved to {saved} (owner-only permissions)")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
