"""First API calls (read-only): profile, funds, positions and today's orders.

Credentials, in this order:

* ``--token-file PATH``: a file holding only the access token (written by
  ``examples/auth_server_flow.py --save-token PATH``), with ``TRADESMART_CLIENT_ID`` set. It is
  not the JSON file written by ``python -m tradesmart login``: for that one set
  ``TRADESMART_TOKEN_FILE=PATH`` and run without ``--token-file`` (``from_env()`` reads it);
* otherwise ``TradeSmart.from_env()``: ``TRADESMART_CLIENT_ID`` / ``TRADESMART_ACCESS_TOKEN``
  when set, else the login saved by ``python -m tradesmart login``.

TradeSmart access tokens are valid until the next 06:00 IST.

Run: ``python -m tradesmart login`` once a day, then ``python examples/first_call.py``
 or: ``python examples/first_call.py --token-file PATH``
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from tradesmart import APIError, AuthenticationError, TradeSmart, TradeSmartError


def _client_from_token_file(option: str) -> TradeSmart | None:
    """A client for ``TRADESMART_CLIENT_ID`` with the token in ``option``; None on a problem."""
    client_id = os.environ.get("TRADESMART_CLIENT_ID", "").strip()
    token_path = Path(option).expanduser()
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:  # missing, a folder, no permission ...
        reason = exc.strerror or type(exc).__name__
        print(f"Cannot read the token file {token_path}: {reason}.", file=sys.stderr)
        return None
    except UnicodeDecodeError:
        print(f"The token file {token_path} is not UTF-8 text.", file=sys.stderr)
        return None
    if not client_id or not token:
        print("Set TRADESMART_CLIENT_ID and pass a non-empty --token-file PATH.", file=sys.stderr)
        return None
    if token.startswith("{"):  # the JSON token file of `python -m tradesmart login`
        print(
            f"{token_path} is a saved login, not a bare token: set TRADESMART_TOKEN_FILE to it "
            "and run without --token-file.",
            file=sys.stderr,
        )
        return None
    try:
        return TradeSmart(client_id, access_token=token)
    except TradeSmartError as exc:  # e.g. a token with characters a token cannot have
        print(f"The token file {token_path} is not usable: {exc}", file=sys.stderr)
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="First read-only TradeSmart API calls.")
    parser.add_argument("--token-file", metavar="PATH", help="file holding the access token")
    args = parser.parse_args(argv)

    if args.token_file:
        maybe_client = _client_from_token_file(args.token_file)
        if maybe_client is None:
            return 2
        client = maybe_client
    else:
        try:
            client = TradeSmart.from_env()  # environment variables, else the saved login
        except TradeSmartError as exc:  # not logged in, or the saved login expired
            print(f"{exc}\nOr pass --token-file PATH.", file=sys.stderr)
            return 2

    with client:
        try:
            details = client.profile()
            limits = client.margins()
            positions = client.positions()  # [] when there are none
            orders = client.orders()  # [] when there are none
        except AuthenticationError as exc:
            print(f"Not authenticated ({exc.emsg or exc.message}); log in again.", file=sys.stderr)
            return 1
        except APIError as exc:
            print(f"{exc.endpoint} failed: {exc.emsg or exc.message}", file=sys.stderr)
            return 1
        except TradeSmartError as exc:  # network failure, timeout or unexpected response
            print(f"Request failed: {exc}", file=sys.stderr)
            return 1

    print(f"Client:            {details.user_id}")
    print(f"Exchanges enabled: {', '.join(details.exarr or [])}")
    print(f"Products enabled:  {', '.join(p.product or '?' for p in details.prarr or [])}")
    print(f"Cash:              {limits.cash}")
    print(f"Margin used:       {limits.marginused}")
    print(f"Open positions:    {len(positions)}")
    print(f"Orders today:      {len(orders)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
