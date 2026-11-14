"""Select the environment a live test run talks to, from environment variables.

``TRADESMART_ENV`` unset (or ``production``) keeps today's behaviour, including the
``TRADESMART_BASE_URL`` override. Any other name needs all of ``TRADESMART_ENV_BASE_URL``,
``TRADESMART_ENV_WS_URL`` and ``TRADESMART_ENV_AUTHORIZE_URL`` (``TRADESMART_ENV_SCRIPT_MASTER_URL``
is optional); a missing one stops the run with an error instead of falling back to production.
The name may hold only lower-case letters, digits and hyphens (it becomes part of a file name).

A non-production run keeps its saved login in ``<token folder>/<env>-token.json``, so it can never
read or overwrite the production login. ``TRADESMART_TOKEN_FILE`` still wins when set, except
that a non-production run refuses the production token file (links followed) and any file named
``token.json`` (any case). Its App Key and Secret, saved by the maintainers' environment login
tool, live in ``<env>-credentials.json`` beside that token file, never in the production
``credentials.json`` (a link to it is refused).
A non-production run also refuses ``TRADESMART_ACCESS_TOKEN`` in the shell, and a
``TRADESMART_CLIENT_ID`` that differs from the client id of the environment's token file: either
would send a login that is not the environment's own to its host.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from tradesmart import ClientConfig, TradeSmart, TradeSmartStream, _key_store, _token_store

_T = TypeVar("_T")
PRODUCTION = "production"
_PRODUCTION_FILE_NAME = "token.json"
_NAME = re.compile(r"[a-z0-9-]+")
_URL_VARIABLES = (
    "TRADESMART_ENV_BASE_URL",
    "TRADESMART_ENV_WS_URL",
    "TRADESMART_ENV_AUTHORIZE_URL",
)
# Held while TRADESMART_TOKEN_FILE is swapped in this process, so two swaps never interleave.
_TOKEN_FILE_LOCK = threading.Lock()


def _value(environ: Mapping[str, str], name: str) -> str:
    return environ.get(name, "").strip()


def environment_name(environ: Mapping[str, str]) -> str:
    """The selected environment name, lower case; ``production`` when unset or blank.

    Raises ``OSError`` for a name with anything but ``a-z``, ``0-9`` and ``-``.
    """
    name = _value(environ, "TRADESMART_ENV").lower() or PRODUCTION
    if not _NAME.fullmatch(name):
        raise OSError(
            f"TRADESMART_ENV={name!r} is not a valid environment name "
            "(use lower-case letters, digits and hyphens only)"
        )
    return name


def environment_config(environ: Mapping[str, str]) -> ClientConfig:
    """The client configuration of the selected environment (see the module docstring)."""
    name = environment_name(environ)
    if name == PRODUCTION:
        base_url = _value(environ, "TRADESMART_BASE_URL") or ClientConfig().base_url
        return ClientConfig(base_url=base_url)
    missing = [variable for variable in _URL_VARIABLES if not _value(environ, variable)]
    if missing:
        raise OSError(f"TRADESMART_ENV={name} needs: {', '.join(missing)}")
    base_url, ws_url, authorize_url = (_value(environ, variable) for variable in _URL_VARIABLES)
    script_master = _value(environ, "TRADESMART_ENV_SCRIPT_MASTER_URL")
    return ClientConfig(
        base_url=base_url,
        ws_url=ws_url,
        authorize_url=authorize_url,
        script_master_base_url=script_master or ClientConfig().script_master_base_url,
    )


@contextmanager
def _process_token_file(value: str | None) -> Iterator[None]:
    """Set (or clear) ``TRADESMART_TOKEN_FILE`` in this process, then restore it (locked)."""
    with _TOKEN_FILE_LOCK:
        previous = os.environ.get(_token_store.ENV_VAR)
        if value is None:
            os.environ.pop(_token_store.ENV_VAR, None)
        else:
            os.environ[_token_store.ENV_VAR] = value
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(_token_store.ENV_VAR, None)
            else:
                os.environ[_token_store.ENV_VAR] = previous


def _default_token_file() -> Path:
    """The library's default (production) token file, ignoring any process override."""
    with _process_token_file(None):
        return _token_store.token_path()


def _same_file(a: Path, b: Path) -> bool:
    """Whether two paths name the same file (links followed; case folded where the OS does)."""
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def token_file_for(environ: Mapping[str, str]) -> Path | None:
    """The token file for the environment; ``None`` means the library default (production).

    Outside production an explicit ``TRADESMART_TOKEN_FILE`` that is the production token file
    (links followed) or is named ``token.json`` (any case) raises ``OSError``.
    """
    name = environment_name(environ)
    explicit = _value(environ, _token_store.ENV_VAR)
    production_file = None if name == PRODUCTION else _default_token_file()
    if explicit:
        path = Path(os.path.abspath(os.path.expanduser(explicit)))
        if production_file is not None:
            if _same_file(path, production_file):
                raise OSError(
                    f"TRADESMART_ENV={name}: TRADESMART_TOKEN_FILE is the production token file; "
                    "unset it (each environment keeps its own login)"
                )
            if path.name.casefold() == _PRODUCTION_FILE_NAME:
                raise OSError(
                    f"TRADESMART_ENV={name}: TRADESMART_TOKEN_FILE is named "
                    f"{_PRODUCTION_FILE_NAME} (the production login's name); unset it"
                )
        return path
    if production_file is None:
        return None
    return production_file.parent / f"{name}-token.json"


def keys_file_for(environ: Mapping[str, str]) -> Path:
    """The saved-keys file of a non-production environment: ``<env>-credentials.json``.

    It lies beside the environment's token file. Raises ``OSError`` for production (its keys
    belong to ``python -m tradesmart login``) and when the file is the production saved-keys
    file (``credentials.json`` beside the default token file, links followed).
    """
    name = environment_name(environ)
    token_file = token_file_for(environ)
    if name == PRODUCTION or token_file is None:
        raise OSError("the production keys belong to `python -m tradesmart login`")
    path = token_file.parent / f"{name}-credentials.json"
    production = _default_token_file().parent / _key_store.FILE_NAME
    if _same_file(path, production):
        raise OSError(
            f"TRADESMART_ENV={name}: {path} is the production saved-keys file (a link); "
            "remove the link (each environment keeps its own keys)"
        )
    return path


def _shell_values(environ: Mapping[str, str], name: str) -> list[str]:
    """``name`` from ``environ`` and from this process (``from_env`` reads the latter)."""
    values = [_value(environ, name), os.environ.get(name, "").strip()]
    return [value for value in values if value]


def _credentials_refusal(environ: Mapping[str, str], name: str, path: Path) -> str | None:
    """Why the shell's credentials must not be used for a non-production environment."""
    if _shell_values(environ, "TRADESMART_ACCESS_TOKEN"):
        return (
            f"TRADESMART_ENV={name}: TRADESMART_ACCESS_TOKEN is set in the shell; it would "
            f"bypass {path.name} and could send another environment's token; unset it"
        )
    client_ids = _shell_values(environ, "TRADESMART_CLIENT_ID")
    if not client_ids:
        return None
    try:
        saved = _token_store.load(path)
    except _token_store.TokenFileError:
        saved = None  # the client reports the unusable file itself
    if saved is None:
        return None
    if any(client_id.casefold() != saved.client_id.casefold() for client_id in client_ids):
        return (
            f"TRADESMART_ENV={name}: TRADESMART_CLIENT_ID differs from the client id in "
            f"{path.name}; unset it (the environment's login names its own client id)"
        )
    return None


def _build_from_env(environ: Mapping[str, str], factory: Callable[[ClientConfig], _T]) -> _T:
    """``factory(config)`` (a ``from_env`` call) under the environment's token file rules."""
    config = environment_config(environ)
    name = environment_name(environ)
    path = token_file_for(environ)
    if path is None:
        return factory(config)
    if name != PRODUCTION:
        refusal = _credentials_refusal(environ, name, path)
        if refusal is not None:
            raise OSError(refusal)
    with _process_token_file(str(path)):
        return factory(config)


def environment_client(environ: Mapping[str, str]) -> TradeSmart:
    """``TradeSmart.from_env`` for the selected environment and its own token file.

    ``TRADESMART_TOKEN_FILE`` is set only while the client is built (the token is read then),
    so it never leaks into other tests. Outside production the shell's credentials are checked
    first (see the module docstring); a refusal raises ``OSError`` before any client is built.
    """
    return _build_from_env(environ, lambda config: TradeSmart.from_env(config=config))


def environment_stream(environ: Mapping[str, str], **callbacks: Any) -> TradeSmartStream:
    """``TradeSmartStream.from_env`` for the selected environment, same rules as the client.

    ``callbacks`` are the stream's ``on_*`` keyword arguments.
    """
    return _build_from_env(
        environ, lambda config: TradeSmartStream.from_env(config=config, **callbacks)
    )


def is_uat(environ: Mapping[str, str]) -> bool:
    """Whether the selected environment is the UAT one (``TRADESMART_ENV=uat``)."""
    return environment_name(environ) == "uat"
