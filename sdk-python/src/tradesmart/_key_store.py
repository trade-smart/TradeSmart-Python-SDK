"""The App Key and Secret key saved per client id by ``python -m tradesmart login``.

Location: ``credentials.json`` in the folder of the token file (see :mod:`tradesmart._token_store`);
other tools pass their own file name. Content (JSON)::

    {"version": 1, "clients": {"<client id>": {"app_key": "...", "secret_key": "..."}}}

An entry may hold one key only (the other one comes from the environment or a prompt). Client
ids are matched ignoring case. The file is written like the token file (folder 0700, file 0600,
atomic replace) and read like it (on POSIX a file owned by another user or open to group/others
is refused). It is **not encrypted**: anyone who can read the file (or the volume it lives on)
can read the keys. There is no locking: two logins finishing at the same moment may lose one
entry (the last write wins; that login asks for its keys again next time).

Nothing here logs or prints; error messages name the file but never echo its contents, and
:class:`SavedKeys` never shows the keys in ``repr``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tradesmart._token_store import private_file_problem, write_private

FILE_NAME = "credentials.json"
VERSION = 1
_FIELDS = ("app_key", "secret_key")


class KeyFileError(Exception):
    """The saved-keys file exists but cannot be used (unreadable, corrupt, foreign or unsafe)."""


@dataclass(frozen=True, repr=False)
class SavedKeys:
    """The keys saved for one client id; either may be ``None`` (not saved)."""

    app_key: str | None
    secret_key: str | None

    def __repr__(self) -> str:
        shown = {name: "***" if getattr(self, name) else None for name in _FIELDS}
        return f"SavedKeys(app_key={shown['app_key']}, secret_key={shown['secret_key']})"

    __str__ = __repr__


def path_for(token_file: Path, name: str = FILE_NAME) -> Path:
    """The saved-keys file ``name`` in the folder of ``token_file``."""
    return token_file.parent / name


def _refuse(path: Path, why: str) -> KeyFileError:
    return KeyFileError(f"the saved keys at {path} {why}")


def _load(path: Path) -> dict[str, dict[str, str]]:
    """All entries (``{}`` when there is no file). Raises :class:`KeyFileError`."""
    try:
        # One open file: the owner/mode check and the read see the same file (no swap between).
        with open(path, "rb") as fh:
            info = os.fstat(fh.fileno())
            raw = fh.read()
    except FileNotFoundError:
        return {}
    except OSError:
        raise _refuse(path, "cannot be read") from None
    problem = private_file_problem(path, info, "saved keys")
    if problem is not None:
        raise KeyFileError(problem)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _refuse(path, "cannot be read") from None
    try:
        data: Any = json.loads(text)
    except (ValueError, RecursionError):  # RecursionError: nesting deeper than the parser allows
        raise _refuse(path, "are not valid JSON") from None
    if not isinstance(data, dict) or "version" not in data:
        raise _refuse(path, "are not a saved-keys file of this SDK")
    if data["version"] != VERSION:
        raise _refuse(path, "use a format this version of the SDK does not know")
    clients = data.get("clients")
    if not isinstance(clients, dict):
        raise _refuse(path, "are not in the expected format")
    entries: dict[str, dict[str, str]] = {}
    for client_id, entry in clients.items():
        if not isinstance(client_id, str) or not client_id.strip() or not isinstance(entry, dict):
            raise _refuse(path, "are not in the expected format")
        keys: dict[str, str] = {}
        for name in _FIELDS:
            value = entry.get(name)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise _refuse(path, "are not in the expected format")
            keys[name] = value.strip()
        if not keys:
            raise _refuse(path, "are not in the expected format")
        entries[client_id.strip()] = keys
    return entries


def _write(path: Path, entries: dict[str, dict[str, str]]) -> None:
    write_private(path, json.dumps({"version": VERSION, "clients": entries}, indent=2))


def _matching(entries: dict[str, dict[str, str]], client_id: str) -> list[str]:
    wanted = client_id.strip().casefold()
    return [name for name in entries if name.casefold() == wanted]


def get(path: Path, client_id: str) -> SavedKeys | None:
    """The keys saved for ``client_id`` (case ignored); ``None`` when there are none."""
    entries = _load(path)
    names = _matching(entries, client_id)
    if not names:
        return None
    entry = entries[names[0]]
    return SavedKeys(app_key=entry.get("app_key"), secret_key=entry.get("secret_key"))


def client_ids(path: Path) -> list[str]:
    """The client ids with saved keys, sorted."""
    return sorted(_load(path))


def put(
    path: Path, client_id: str, *, app_key: str | None = None, secret_key: str | None = None
) -> Path:
    """Save the given keys for ``client_id``; other client ids and a key not given are kept.

    An entry spelt differently (case) is replaced by ``client_id``'s spelling. Raises
    :class:`KeyFileError` for an unusable existing file (it is never overwritten) and ``OSError``
    when the file cannot be written.
    """
    given = {
        name: value.strip()
        for name, value in (("app_key", app_key), ("secret_key", secret_key))
        if value is not None and value.strip()
    }
    if not given:
        raise ValueError("nothing to save: give app_key and/or secret_key")
    entries = _load(path)
    merged: dict[str, str] = {}
    for name in _matching(entries, client_id):
        merged.update(entries.pop(name))
    merged.update(given)
    entries[client_id.strip()] = merged
    _write(path, entries)
    return path


def forget(path: Path, client_id: str) -> bool:
    """Remove the keys of ``client_id``; ``False`` when none were saved.

    The file is removed when no entry is left. Raises :class:`KeyFileError` for an unusable file
    and ``OSError`` when it cannot be written.
    """
    entries = _load(path)
    names = _matching(entries, client_id)
    if not names:
        return False
    for name in names:
        del entries[name]
    if entries:
        _write(path, entries)
    else:
        path.unlink(missing_ok=True)
    return True


def delete(path: Path) -> bool:
    """Remove the whole file, whatever it holds; ``False`` when there was none."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
