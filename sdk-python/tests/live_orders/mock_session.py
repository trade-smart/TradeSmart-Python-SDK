"""The mock-session switch: the UAT matrix on the production server during an exchange mock session.

Owner-approved design of 2026-11-13 (the mock-session mode of the live-order matrix). On
a trading holiday the exchanges may run a mock trading session on the live production systems;
mock-session orders, fills and positions are wiped before the next trading day. Mock mode lets the
matrix run there, and only when **every** check below holds (``refusal``); otherwise the run is
refused before any API call, naming the first failed check:

1. ``TRADESMART_LIVE_MOCK_SESSION`` is a date ``YYYY-MM-DD`` equal to **today's date in IST**;
2. today is a Saturday or Sunday, or is in ``MOCK_SESSION_HOLIDAYS`` (a reviewed code change);
3. ``TRADESMART_LIVE_MOCK_CONFIRM`` is exactly ``mock session YYYY-MM-DD`` with the same date
   (typed by hand by the owner after checking the exchange notice);
4. ``TRADESMART_ENV`` is unset or ``production``;
5. the REST host is **exactly** the production host (the SDK's default ``ClientConfig().base_url``
   host; parsed, never a substring match);
6. ``TRADESMART_LIVE_ORDER_FUNDED`` is not set, to any value (funded mode never combines).

Each variable's value is stripped of surrounding whitespace only; nothing else is normalised
(case, inner spaces and other date formats refuse). The matrix gate adds the usual order opt-ins
and the account match. The date and phrase are checked again before every state-changing call
(``MatrixSession.mock_refusal``): a run that crosses midnight IST stops placing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date

from tests.live_orders import safety
from tradesmart import ClientConfig

SESSION_ENV = "TRADESMART_LIVE_MOCK_SESSION"
CONFIRM_ENV = "TRADESMART_LIVE_MOCK_CONFIRM"
ENVIRONMENT = "production-mock"  # ``environment`` of the run file and every record
PHRASE = "mock session {day}"
# Weekday trading holidays with an exchange mock session (adding one is a reviewed code change).
MOCK_SESSION_HOLIDAYS: frozenset[date] = frozenset()
# The production REST host: the SDK's own default (no user info, plain ASCII; else ``None``).
PRODUCTION_HOST: str | None = safety._rest_host(ClientConfig().base_url)
# The ask of the mock-session limits preflight (``safety.limits_preflight``).
LIMITS_ASK = "ask the RMS team to raise the mock account's limits for the session"
# Why the AMO and GTT families skip, and the wrappers refuse ``amo=True`` and every GTT write, in
# mock mode: the exchanges wipe mock orders, but stored AMOs and GTTs outlive the session.
NO_AMO_GTT = "AMO and GTT are not sent in a mock session: they are stored past it"
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_WEEKEND = (5, 6)  # Saturday, Sunday (``date.weekday``)


def requested(environ: Mapping[str, str]) -> bool:
    """Whether any part of the switch is present (any value, even blank): the run is then a
    mock-session run, or refused; never a UAT or plain production run."""
    return SESSION_ENV in environ or CONFIRM_ENV in environ


def switch_date(environ: Mapping[str, str]) -> date | None:
    """The date of ``TRADESMART_LIVE_MOCK_SESSION`` (surrounding whitespace stripped), or
    ``None`` when it is missing or not exactly ``YYYY-MM-DD`` (ASCII digits) of a real date."""
    text = environ.get(SESSION_ENV)
    if text is None:
        return None
    text = text.strip()
    if not _DATE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def refusal(environ: Mapping[str, str], today_ist: date, base_url: str) -> str | None:
    """``None`` when mock mode is allowed for ``today_ist`` (today's date in IST) against the
    REST ``base_url``; otherwise the first failed check, named."""
    day = switch_date(environ)
    if day is None:
        return (
            f"mock session: {SESSION_ENV} must be the session date as YYYY-MM-DD; refusing to "
            "place orders"
        )
    if day != today_ist:
        return (
            f"mock session: {SESSION_ENV}={day.isoformat()} is not today in IST "
            f"({today_ist.isoformat()}); refusing to place orders"
        )
    if today_ist.weekday() not in _WEEKEND and today_ist not in MOCK_SESSION_HOLIDAYS:
        return (
            f"mock session: today ({today_ist.isoformat()}) is a weekday not in "
            "MOCK_SESSION_HOLIDAYS: never on a normal trading day; refusing to place orders"
        )
    phrase = environ.get(CONFIRM_ENV)
    expected = PHRASE.format(day=day.isoformat())
    if phrase is None or phrase.strip() != expected:
        return f"mock session: {CONFIRM_ENV} must be exactly '{expected}'; refusing to place orders"
    name = environ.get("TRADESMART_ENV")
    if name is not None and name.strip() != "production":
        return (
            f"mock session: TRADESMART_ENV must be unset or production (it is {name.strip()!r}); "
            "refusing to place orders"
        )
    if PRODUCTION_HOST is None or safety._rest_host(base_url) != PRODUCTION_HOST:
        return (
            f"mock session: the REST host must be exactly the production host {PRODUCTION_HOST}; "
            "refusing to place orders"
        )
    if safety.FUNDED_ENV in environ:
        return (
            f"mock session: {safety.FUNDED_ENV} is set (to any value): funded mode and mock mode "
            "never combine; refusing to place orders"
        )
    return None


def mode(environ: Mapping[str, str], today_ist: date, base_url: str) -> bool:
    """Mock mode holds: the switch is present and every check passes."""
    return requested(environ) and refusal(environ, today_ist, base_url) is None


def snapshot(environ: Mapping[str, str]) -> dict[str, str]:
    """The variables ``refusal`` reads (for the re-check before each state-changing call)."""
    names = (SESSION_ENV, CONFIRM_ENV, "TRADESMART_ENV", safety.FUNDED_ENV)
    return {name: environ[name] for name in names if name in environ}
