"""OAuth helpers: authorize URL, checksum, access token model and the GenAcsTok call."""

import hashlib
from collections.abc import Mapping
from datetime import datetime, time, timedelta
from typing import Any
from urllib.parse import quote

from pydantic import Field, SecretStr

from tradesmart._logging import redact
from tradesmart.config import ClientConfig
from tradesmart.endpoints.base import Call, EndpointSpec
from tradesmart.errors import ConfigurationError
from tradesmart.models.base import IST, LenientStr, TradeSmartModel

_EXPIRY_TIME = time(6, 0)


def build_authorize_url(app_key: str, *, authorize_url: str = ClientConfig.authorize_url) -> str:
    """Return the URL the user opens in a browser to authorise the app."""
    if not app_key:
        raise ConfigurationError("app_key must not be empty")
    return f"{authorize_url}?client_id={quote(app_key, safe='')}"


def compute_checksum(app_key: str, secret_key: str, code: str) -> str:
    """SHA-256 hex digest of ``app_key + secret_key + code``."""
    if not app_key or not secret_key or not code:
        raise ConfigurationError("app_key, secret_key and code must all be non-empty")
    return hashlib.sha256((app_key + secret_key + code).encode("utf-8")).hexdigest()


def next_expiry(issued_at: datetime) -> datetime:
    """The first 06:00 IST strictly after ``issued_at`` (a timezone-aware datetime)."""
    if issued_at.tzinfo is None or issued_at.utcoffset() is None:
        raise ValueError("issued_at must be timezone-aware")
    local = issued_at.astimezone(IST)
    candidate = datetime.combine(local.date(), _EXPIRY_TIME, tzinfo=IST)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate


class AccessToken(TradeSmartModel):
    """Result of ``GenAcsTok``. Token values are masked in ``repr``/``str``/``raw``."""

    access_token: SecretStr
    susertoken: SecretStr | None = None
    refresh_token: SecretStr | None = None
    expires_in: LenientStr = None
    user_id: LenientStr = Field(default=None, alias="uid")
    account_id: LenientStr = Field(default=None, alias="actid")
    uname: LenientStr = None
    issued_at: datetime
    expires_at: datetime

    def is_expired(self, now: datetime | None = None) -> bool:
        """True once ``now`` (default: current time) has reached ``expires_at``."""
        current = now if now is not None else datetime.now(IST)
        return current >= self.expires_at

    @classmethod
    def from_response(
        cls, data: Mapping[str, Any], *, issued_at: datetime | None = None
    ) -> "AccessToken":
        """Build from a ``GenAcsTok`` success body; expiry follows the 06:00 IST rule."""
        issued = issued_at if issued_at is not None else datetime.now(IST)
        augmented = {**data, "issued_at": issued, "expires_at": next_expiry(issued)}
        token = cls.from_wire(augmented, endpoint="GenAcsTok")
        token._raw = redact(dict(data))
        return token


GEN_ACS_TOK: EndpointSpec[AccessToken] = EndpointSpec(
    name="GenAcsTok",
    parse=AccessToken.from_response,
    authenticated=False,
    state_changing=True,
)


def gen_acs_tok_call(app_key: str, secret_key: str, code: str) -> Call[AccessToken]:
    """Build the ``GenAcsTok`` call exchanging an authorisation code for a token."""
    return Call(
        GEN_ACS_TOK,
        {"code": code, "checksum": compute_checksum(app_key, secret_key, code)},
    )
