"""Login helpers shared by ``TradeSmart`` and ``AsyncTradeSmart`` (sans-IO).

The clients wrap the token exchange in the same ``try``/``except`` shell::

    try:
        token = execute(call)
    except AuthenticationError:
        raise
    except APIError as exc:
        raise as_authentication_error(exc) from exc
    except OrderStatusUnknownError as exc:
        raise unknown_outcome(exc) from exc
    except ResponseParseError:
        parse_failed = True  # raise parse_failure() outside the handler: no chained body
"""

from __future__ import annotations

from tradesmart._client_core import Credentials
from tradesmart._transport.codec import failure_message
from tradesmart.auth import AccessToken, build_authorize_url, gen_acs_tok_call
from tradesmart.config import ClientConfig
from tradesmart.endpoints.base import Call
from tradesmart.errors import APIError, AuthenticationError, OrderStatusUnknownError


def login_url(creds: Credentials, config: ClientConfig) -> str:
    """URL to open in a browser so the user can authorise the app."""
    return build_authorize_url(creds.require_app_key(), authorize_url=config.authorize_url)


def token_call(creds: Credentials, code: str) -> Call[AccessToken]:
    """The ``GenAcsTok`` call exchanging ``code`` (needs the app and secret keys)."""
    app_key, secret_key = creds.require_keys()
    return gen_acs_tok_call(app_key, secret_key, code)


def as_authentication_error(exc: APIError) -> AuthenticationError:
    """Re-express a failed GenAcsTok as an AuthenticationError (no secrets in the message).

    The message is ``GenAcsTok failed: <emsg>``, or ``GenAcsTok failed (no error message
    from the server)`` without a usable ``emsg``; never a doubled prefix or ``None``.
    """
    return AuthenticationError(
        failure_message("GenAcsTok", exc.emsg),
        endpoint=exc.endpoint,
        http_status=exc.http_status,
        emsg=exc.emsg,
        request_time=exc.request_time,
        raw=exc.raw,
    )


def unknown_outcome(exc: OrderStatusUnknownError) -> AuthenticationError:
    """GenAcsTok may have consumed the single-use code: the login must be restarted."""
    return AuthenticationError(
        "GenAcsTok outcome unknown: the authorisation code is single-use; restart the login",
        endpoint="GenAcsTok",
        http_status=exc.http_status,
    )


def parse_failure() -> AuthenticationError:
    """The success body was unusable: the single-use code is probably consumed."""
    return AuthenticationError(
        "GenAcsTok response could not be parsed; "
        "the authorisation code is single-use; restart the login",
        http_status=None,
        endpoint="GenAcsTok",
    )
