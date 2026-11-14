"""Live, read-only: session and identity. Opt-in; see README.md."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tradesmart import AccessToken, TradeSmart

pytestmark = [pytest.mark.live, pytest.mark.nse, pytest.mark.nfo, pytest.mark.mcx]


def test_from_env_client_token_is_not_expired(
    live_client: TradeSmart, observe: Callable[..., object]
) -> None:
    # current_token() raises AuthenticationError for a missing or expired token.
    token = live_client._creds.current_token()
    expired = token.is_expired() if isinstance(token, AccessToken) else None
    observe(
        token_kind="AccessToken" if isinstance(token, AccessToken) else "string",
        expired=expired,
        expires_at=token.expires_at if isinstance(token, AccessToken) else None,
    )
    assert live_client.client_id
    assert expired in (False, None)


def test_profile_user_id_equals_client_id(
    live_client: TradeSmart, observe: Callable[..., object]
) -> None:
    details = live_client.profile()
    observe(
        stat=details.stat,
        # Account ids are never recorded: only whether they match the client id.
        user_id_matches=details.user_id == live_client.client_id,
        account_id_matches=details.account_id == live_client.client_id,
        exchanges=details.exarr,
        order_types=details.orarr,
        products=[p.raw for p in details.prarr or []],
        raw_keys=sorted(details.raw),
    )
    assert details.stat == "Ok"
    assert details.user_id == live_client.client_id
