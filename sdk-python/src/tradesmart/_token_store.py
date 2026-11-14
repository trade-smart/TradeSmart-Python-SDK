"""The per-user token file written by ``python -m tradesmart login``.

Location: ``TRADESMART_TOKEN_FILE`` when set; otherwise ``%APPDATA%\\tradesmart\\token.json`` on
Windows and ``$XDG_CONFIG_HOME/tradesmart/token.json`` (default ``~/.config/...``) elsewhere.

Content (JSON): ``client_id``, ``access_token``, ``issued_at`` and ``expires_at`` (ISO 8601 with
offset). The file is replaced atomically and created with mode ``0o600`` (owner read/write only).
On Windows the mode bits are not enforced; the file is protected by living inside the user's own
profile folder, which other non-administrator users cannot read. On POSIX, reading refuses (like
ssh) a file owned by another user or open to group/others. The loaded expiry never exceeds the
06:00 IST rule applied to ``issued_at``, and a file whose ``issued_at`` lies in the future (more
than a few minutes ahead of this computer's clock) is refused, so an edited file (or a wrong
clock at login) cannot extend local validity.

Nothing here logs or prints; error messages name the file but never echo its contents.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tradesmart.auth import AccessToken, next_expiry

ENV_VAR = "TRADESMART_TOKEN_FILE"
_DIR_NAME = "tradesmart"
_FILE_NAME = "token.json"
# How far ``issued_at`` may lie ahead of this computer's clock (small clock differences).
_ISSUE_SKEW = timedelta(minutes=5)


class TokenFileError(Exception):
    """The token file exists but cannot be used (unreadable, corrupt or incomplete)."""


@dataclass(frozen=True)
class SavedLogin:
    """A login read from the token file (the token stays masked in ``repr``)."""

    client_id: str
    token: AccessToken


def _os_name() -> str:
    """``os.name``, behind a seam so tests can exercise the Windows branch anywhere."""
    return os.name


def token_path() -> Path:
    """Where the token file lives (``TRADESMART_TOKEN_FILE`` overrides the default).

    The variable is stripped of surrounding whitespace; a blank value counts as unset. A
    relative value is made absolute against the current folder (``abspath`` keeps a symbolic
    link's own path), so the path printed by the login command names the same file everywhere.
    """
    override = os.environ.get(ENV_VAR, "").strip()
    if override:
        return Path(os.path.abspath(os.path.expanduser(override)))
    if _os_name() == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
    return base / _DIR_NAME / _FILE_NAME


def save(client_id: str, token: AccessToken, path: Path | None = None) -> Path:
    """Write the login to ``path`` (default :func:`token_path`) atomically, owner-only.

    Raises ``OSError`` when the folder or file cannot be written; no partial file is left.
    """
    target = path if path is not None else token_path()
    payload = json.dumps(
        {
            "client_id": client_id,
            "access_token": token.access_token.get_secret_value(),
            "issued_at": token.issued_at.isoformat(timespec="seconds"),
            "expires_at": token.expires_at.isoformat(timespec="seconds"),
        }
    )
    return write_private(target, payload)


def write_private(target: Path, payload: str) -> Path:
    """Write ``payload`` to ``target`` atomically: folder 0700, file 0600, no partial file.

    Shared by the token file and the saved-keys file. Raises ``OSError`` on failure.
    """
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if hasattr(os, "fchmod"):  # make the mode explicit whatever the umask
                os.fchmod(fh.fileno(), 0o600)
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return target


def _field(data: dict[str, Any], key: str, where: Path) -> str:
    """The non-blank string ``key``, trimmed (timestamps are trimmed before they are matched)."""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TokenFileError(f"the saved login at {where} has no usable {key!r}")
    return value.strip()


# The only accepted form, on every Python version (``fromisoformat`` alone accepts more):
# YYYY-MM-DDTHH:MM:SS, optional 1-9 fraction digits, then Z / z or a +HH:MM / -HH:MM offset.
_TIMESTAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?([Zz]|[+-]\d{2}:\d{2})", re.ASCII
)


def _normalise_iso(text: str) -> str | None:
    """``text`` in a form ``fromisoformat`` reads, or ``None`` if not accepted.

    ``Z`` / ``z`` becomes ``+00:00`` (``fromisoformat`` refuses a lowercase ``z``) and the
    fraction is truncated or padded to 6 digits.
    """
    match = _TIMESTAMP.fullmatch(text)
    if match is None:
        return None
    stamp, fraction, offset = match.groups()
    if fraction is not None:
        stamp += "." + fraction[:6].ljust(6, "0")
    return stamp + ("+00:00" if offset in ("Z", "z") else offset)


def _timestamp(data: dict[str, Any], key: str, where: Path) -> datetime:
    normalised = _normalise_iso(_field(data, key, where))
    if normalised is None:
        raise TokenFileError(f"the saved login at {where} has an invalid {key!r}")
    try:
        value = datetime.fromisoformat(normalised)
    except ValueError:  # e.g. month 13 or hour 25
        raise TokenFileError(f"the saved login at {where} has an invalid {key!r}") from None
    if value.tzinfo is None or value.utcoffset() is None:  # pragma: no cover - the regex needs one
        raise TokenFileError(f"the saved login at {where} has an invalid {key!r}")
    return value


def private_file_problem(target: Path, info: os.stat_result, what: str) -> str | None:
    """On POSIX, like ssh: why a file owned by someone else or open to group/others is refused.

    ``what`` names the file in the message (``"saved login"``, ``"saved keys"``).
    """
    if _os_name() == "nt" or not hasattr(os, "geteuid"):
        return None  # Windows: the per-user profile folder protects the file
    if info.st_uid != os.geteuid():
        return f"the {what} at {target} is owned by another user; delete it and log in again"
    if stat.S_IMODE(info.st_mode) & 0o077:
        return (
            f"the {what} at {target} has unsafe permissions (group or others can access "
            "it); delete it and log in again, or restrict it with chmod 600"
        )
    return None


def _check_private(target: Path, info: os.stat_result) -> None:
    problem = private_file_problem(target, info, "saved login")
    if problem is not None:
        raise TokenFileError(problem)


def load(path: Path | None = None) -> SavedLogin | None:
    """Read the login from ``path`` (default :func:`token_path`); ``None`` when there is none.

    Raises :class:`TokenFileError` when the file exists but cannot be used.
    """
    target = path if path is not None else token_path()
    try:
        info = target.stat()
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        raise TokenFileError(f"the saved login at {target} cannot be read") from None
    _check_private(target, info)
    try:
        data = json.loads(text)
    except ValueError:
        raise TokenFileError(f"the saved login at {target} is not valid JSON") from None
    if not isinstance(data, dict):
        raise TokenFileError(f"the saved login at {target} is not a JSON object")
    issued_at = _timestamp(data, "issued_at", target)
    if issued_at > datetime.now(UTC) + _ISSUE_SKEW:
        # Otherwise the 06:00 IST cap below, anchored on issued_at, would not bound anything.
        raise TokenFileError(
            f"the saved login at {target} has an 'issued_at' in the future (check this "
            "computer's clock); log in again"
        )
    # An edited file cannot extend the token's local validity past TradeSmart's 06:00 IST rule.
    expires_at = min(_timestamp(data, "expires_at", target), next_expiry(issued_at))
    token = AccessToken.model_validate(
        {
            "access_token": _field(data, "access_token", target),
            "issued_at": issued_at,
            "expires_at": expires_at,
        }
    )
    return SavedLogin(client_id=_field(data, "client_id", target), token=token)


def delete(path: Path | None = None) -> bool:
    """Remove the token file; ``False`` when there was none. Raises ``OSError`` on failure."""
    target = path if path is not None else token_path()
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True
