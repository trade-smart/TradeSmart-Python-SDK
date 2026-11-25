"""State and helpers shared by ``TradeSmart`` and ``AsyncTradeSmart`` (sans-IO)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal

from tradesmart import _token_store
from tradesmart._transport.codec import OK_STATS
from tradesmart.auth import AccessToken
from tradesmart.config import ClientConfig
from tradesmart.endpoints.base import Call, EndpointSpec
from tradesmart.errors import AuthenticationError, ConfigurationError
from tradesmart.models.base import WireValue, to_wire_value

LOGIN_HINT = "run `python -m tradesmart login`"

# Rate group by endpoint name, used by the low-level ``request()`` escape hatch.
RATE_GROUPS = {
    "PlaceOrder": "place_order",
    "PlaceMultiOrder": "place_order",
    "GetQuotes": "quotes",
    "GetQuotesMultiple": "quotes",
    "TPSeries": "tpseries",
}


# Endpoints that change state or credentials: never retried, even via request().
STATE_CHANGING_ENDPOINTS: frozenset[str] = frozenset(
    {
        "PlaceOrder",
        "PlaceMultiOrder",
        "ModifyOrder",
        "CancelOrder",
        "ExitSNOOrder",
        "GenAcsTok",
        "PlaceGTTOrder",
        "ModifyGTTOrder",
        "CancelGTTOrder",
        "PlaceOCOOrder",
        "ModifyOCOOrder",
        "CancelOCOOrder",
        "PlaceIceBergOrder",
        "ModifyIceBergOrder",
        "CancelIceBergOrder",
    }
)

# The documented success ``stat`` of the endpoints that do not answer ``Ok``; every other
# endpoint succeeds only on ``Ok``. Compared exactly, except the endpoints in
# ``SUCCESS_STATS_IGNORE_CASE``. Used by ``request()``.
SUCCESS_STATS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "PlaceGTTOrder": frozenset({"Oi created"}),
        "ModifyGTTOrder": frozenset({"Oi replaced"}),
        "CancelGTTOrder": frozenset({"OI deleted"}),
        "PlaceOCOOrder": frozenset({"OI created"}),
        "ModifyOCOOrder": frozenset({"OI replaced"}),
        "CancelOCOOrder": frozenset({"OI deleted"}),
        "PlaceIceBergOrder": frozenset({"true"}),
        "ModifyIceBergOrder": frozenset({"true"}),
        "CancelIceBergOrder": frozenset({"true"}),
    }
)

# The endpoints whose success word compares ignoring letter case only (``casefold()`` on both
# sides; nothing trimmed or normalised): the test environment answers ``OI created`` for the
# documented ``Oi created`` (spec F32). The iceberg ``true`` stays exact.
SUCCESS_STATS_IGNORE_CASE: frozenset[str] = frozenset(
    {
        "PlaceGTTOrder",
        "ModifyGTTOrder",
        "CancelGTTOrder",
        "PlaceOCOOrder",
        "ModifyOCOOrder",
        "CancelOCOOrder",
    }
)

# The field a success reply must carry as a non-blank string (the GTT / OCO alert id).
ACK_ID_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        name: "al_id"
        for name in (
            "PlaceGTTOrder",
            "ModifyGTTOrder",
            "CancelGTTOrder",
            "PlaceOCOOrder",
            "ModifyOCOOrder",
            "CancelOCOOrder",
        )
    }
)

# ``stat`` words that are a definite rejection on an endpoint (compared with ``casefold()``,
# nothing trimmed): ``Invalid Oi`` on the alert modify and cancel writes (an unknown alert id).
# Anywhere else (place writes, every other endpoint) the word stays an unknown outcome.
FAILURE_STATS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        name: frozenset({"Invalid Oi"})
        for name in ("ModifyGTTOrder", "CancelGTTOrder", "ModifyOCOOrder", "CancelOCOOrder")
    }
)

# Documented spelling of every endpoint the guards above know, keyed case-insensitively.
_CANONICAL: dict[str, str] = {
    name.lower(): name for name in (*RATE_GROUPS, *STATE_CHANGING_ENDPOINTS)
}
_ENDPOINT_NAME = re.compile(r"[A-Za-z]+")


def canonical_endpoint(endpoint: str) -> str:
    """Normalise a ``request()`` endpoint name so the safety guards cannot be bypassed.

    Surrounding whitespace and slashes are removed and the rest must be ASCII letters
    only. Known (state-changing or rate-limited) names match case-insensitively and are
    returned in their documented spelling, e.g. ``"placeorder"`` -> ``"PlaceOrder"``.
    """
    name = endpoint.strip().strip("/")
    if not _ENDPOINT_NAME.fullmatch(name):
        raise ConfigurationError(
            "endpoint must be a TradeSmart endpoint name made of ASCII letters only "
            "(for example 'Limits'), without slashes, dots or spaces"
        )
    return _CANONICAL.get(name.lower(), name)


# A token goes into the Authorization header: visible ASCII only (no space, no control).
_TOKEN_CHARS = re.compile(r"[\x21-\x7e]*")
_UNSENDABLE_MESSAGE = (
    "access_token must contain only printable ASCII characters (no spaces, line breaks "
    "or other control characters); check how the token was copied or read"
)


def clean_token(token: str | AccessToken | None) -> str | AccessToken | None:
    """Validate a token when it is set, before anything can be sent with it.

    A ``str`` token is stripped of surrounding whitespace (e.g. the newline of a token read
    from a file); a blank one counts as no token. Raises ``ConfigurationError`` for a
    token with a non-ASCII or control character, without echoing the value.
    """
    if token is None:
        return None
    if isinstance(token, AccessToken):
        if not _TOKEN_CHARS.fullmatch(token.access_token.get_secret_value()):
            raise ConfigurationError(_UNSENDABLE_MESSAGE)
        return token
    if not isinstance(token, str):
        raise ConfigurationError("access_token must be a str or an AccessToken")
    value = token.strip()
    if not _TOKEN_CHARS.fullmatch(value):
        raise ConfigurationError(_UNSENDABLE_MESSAGE)
    return value


class Credentials:
    """Holds the access token and app keys privately (never exposed or shown in repr)."""

    def __init__(
        self,
        client_id: str,
        config: ClientConfig,
        *,
        access_token: str | AccessToken | None,
        app_key: str | None,
        secret_key: str | None,
    ) -> None:
        if not client_id:
            raise ConfigurationError("client_id must not be empty")
        self.client_id = client_id
        self._config = config
        self._token = clean_token(access_token)
        self._app_key = app_key
        self._secret_key = secret_key
        # Why from_env() found no usable token (e.g. the saved login expired); never secret.
        self.missing_token_reason: str | None = None

    def set_token(self, token: str | AccessToken) -> None:
        self._token = clean_token(token)

    def require_keys(self) -> tuple[str, str]:
        if not self._app_key or not self._secret_key:
            raise ConfigurationError(
                "app_key and secret_key are required to generate an access token"
            )
        return self._app_key, self._secret_key

    def require_app_key(self) -> str:
        if not self._app_key:
            raise ConfigurationError("app_key is required to build the authorize URL")
        return self._app_key

    def bearer(self) -> str:
        """Token provider handed to the transports."""
        token = self.current_token()
        if isinstance(token, AccessToken):
            return token.access_token.get_secret_value()
        return token

    def current_token(self) -> str | AccessToken:
        """The usable token as stored (raises ``AuthenticationError`` if missing/expired).

        A blank token counts as missing: nothing is sent with ``Authorization: Bearer ``.
        """
        token = self._token
        if isinstance(token, AccessToken):
            blank = not token.access_token.get_secret_value()
        else:
            blank = not token
        if blank:
            token = None
        if token is None and self.missing_token_reason:
            raise AuthenticationError(
                f"No access token set: {self.missing_token_reason}; {LOGIN_HINT} "
                "or call generate_session()",
                endpoint="",
                http_status=None,
            )
        if token is None:
            raise AuthenticationError(
                "No access token set; call generate_session() or set_access_token(), or "
                f"{LOGIN_HINT} and build the client with from_env()",
                endpoint="",
                http_status=None,
            )
        if isinstance(token, AccessToken):
            check_expiry(token, self._config)
        return token


def check_expiry(token: AccessToken, config: ClientConfig) -> None:
    """Raise ``AuthenticationError`` (with the login hint) if ``token`` has expired.

    Skipped when ``config.check_token_expiry`` is False.
    """
    if config.check_token_expiry and token.is_expired():
        raise AuthenticationError(
            f"Access token expired at {token.expires_at} "
            f"(tokens are valid until 06:00 IST); log in again, e.g. {LOGIN_HINT}",
            endpoint="",
            http_status=None,
        )


def _env(name: str) -> str | None:
    """An environment value without surrounding whitespace; ``None`` when unset or blank."""
    return os.environ.get(name, "").strip() or None


def env_kwargs(
    config: ClientConfig, *, allow_app_key: bool = True
) -> tuple[dict[str, Any], str | None]:
    """Constructor arguments from the ``TRADESMART_*`` variables, else the saved login.

    Client id: ``TRADESMART_CLIENT_ID``, else the token file's. Token: ``TRADESMART_ACCESS_TOKEN``
    (only together with ``TRADESMART_CLIENT_ID``), else the token file's (as an ``AccessToken``,
    so the 06:00 IST check applies) when it was issued for the same client id. Without a usable
    token (and without ``TRADESMART_APP_KEY`` for a programmatic login; ignored when
    ``allow_app_key`` is False, e.g. for a stream) this raises, pointing at
    ``python -m tradesmart login``. Every value is stripped; a blank one counts as unset.
    """
    client_id = _env("TRADESMART_CLIENT_ID")
    access_token: str | AccessToken | None = _env("TRADESMART_ACCESS_TOKEN")
    app_key = _env("TRADESMART_APP_KEY") if allow_app_key else None
    if access_token is not None and client_id is None:
        # Never pair a token from the environment with the saved login's client id.
        raise ConfigurationError(
            "TRADESMART_ACCESS_TOKEN is set but TRADESMART_CLIENT_ID is not; set both, or "
            f"unset TRADESMART_ACCESS_TOKEN and {LOGIN_HINT} to use the saved login"
        )
    path = _token_store.token_path()
    saved: _token_store.SavedLogin | None = None
    problem = f"there is no saved login at {path}"
    if client_id is None or access_token is None:
        try:
            saved = _token_store.load(path)
        except _token_store.TokenFileError as exc:
            problem = str(exc)
    if client_id is None:
        if saved is None:
            raise ConfigurationError(f"TRADESMART_CLIENT_ID is not set and {problem}; {LOGIN_HINT}")
        client_id = saved.client_id
    expired = False
    if access_token is None and saved is not None:
        if saved.client_id.casefold() != client_id.casefold():
            problem = f"the saved login at {path} is for a different client id"
        elif config.check_token_expiry and saved.token.is_expired():
            expired = True
            problem = (
                f"the saved login at {path} expired at {saved.token.expires_at.isoformat()} "
                "(tokens are valid until 06:00 IST)"
            )
        else:
            access_token = saved.token
    if access_token is None and app_key is None:
        if expired:
            raise AuthenticationError(
                f"No usable access token: {problem}; {LOGIN_HINT}", endpoint="", http_status=None
            )
        raise ConfigurationError(
            f"No access token: TRADESMART_ACCESS_TOKEN is not set and {problem}; {LOGIN_HINT}"
        )
    kwargs = {
        "client_id": client_id,
        "access_token": access_token,
        "app_key": app_key,
        "secret_key": _env("TRADESMART_SECRET_KEY"),
    }
    return kwargs, (problem if access_token is None else None)


def build_raw_call(
    client_id: str,
    endpoint: str,
    payload: Mapping[str, Any],
    *,
    state_changing: bool,
    expect: Literal["object", "list"],
) -> Call[Any]:
    """Build a call for an arbitrary endpoint name; ``uid`` is appended when absent.

    The name is normalised by :func:`canonical_endpoint` (raises ``ConfigurationError``).
    """
    name = canonical_endpoint(endpoint)
    wire: dict[str, WireValue] = {}
    for key, value in payload.items():
        try:
            wire[key] = to_wire_value(value)
        except RecursionError:
            raise TypeError(f"payload value for {key!r}: nested too deeply (or cyclic)") from None
        except TypeError as exc:
            # Name only the top-level key: a nested path or value could carry a secret.
            raise TypeError(f"payload value for {key!r}: {exc}") from None
    wire.setdefault("uid", client_id)
    spec: EndpointSpec[Any] = EndpointSpec(
        name=name,
        parse=lambda body: body,
        state_changing=state_changing or name in STATE_CHANGING_ENDPOINTS,
        expect=expect,
        rate_group=RATE_GROUPS.get(name),
        success_stats=SUCCESS_STATS.get(name, OK_STATS),
        requires_ack_id=ACK_ID_FIELDS.get(name),
        stats_ignore_case=name in SUCCESS_STATS_IGNORE_CASE,
        failure_stats=FAILURE_STATS.get(name, frozenset()),
    )
    return Call(spec, wire)
