"""Logger access and secret redaction."""

from __future__ import annotations

import logging
import re
from typing import Any

LOGGER_NAME = "tradesmart"

SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "access_token",
        "accesstoken",
        "susertoken",
        "refresh_token",
        "checksum",
        "code",
        "secret_key",
        "app_key",
        "password",
    }
)

_MASK = "***"
_KEYS_ALT = "|".join(sorted(map(re.escape, SENSITIVE_KEYS)))
_BEARER_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_JSON_RE = re.compile(
    rf"""(?P<q>["'])(?P<k>{_KEYS_ALT})(?P=q)\s*:\s*(?P<v>["'])[^"']*(?P=v)""",
    re.IGNORECASE,
)
_PAIR_RE = re.compile(rf"\b(?P<k>{_KEYS_ALT})=[^&\s]*", re.IGNORECASE)


def _redact_str(text: str) -> str:
    text = _BEARER_RE.sub(f"Bearer {_MASK}", text)
    text = _JSON_RE.sub(lambda m: f"{m['q']}{m['k']}{m['q']}:{m['q']}{_MASK}{m['q']}", text)
    return _PAIR_RE.sub(lambda m: f"{m['k']}={_MASK}", text)


def redact(value: Any) -> Any:
    """Return a copy of ``value`` with secrets masked; never mutates the input."""
    if isinstance(value, dict):
        return {
            k: (_MASK if isinstance(k, str) and k.lower() in SENSITIVE_KEYS else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v) for v in value)
    if isinstance(value, str):
        return _redact_str(value)
    return value


class RedactingFilter(logging.Filter):
    """Masks secrets in every record passing through the logger."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = redact(record.args)
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def _ensure_filter(logger: logging.Logger) -> None:
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())


def get_logger(name: str = "") -> logging.Logger:
    """Return ``tradesmart`` or ``tradesmart.<name>``, wiring the root once.

    Logger filters do not apply to records from child loggers, so every
    returned logger carries its own single RedactingFilter.
    """
    root = logging.getLogger(LOGGER_NAME)
    if not any(isinstance(h, logging.NullHandler) for h in root.handlers):
        root.addHandler(logging.NullHandler())
    _ensure_filter(root)
    logger = logging.getLogger(f"{LOGGER_NAME}.{name}") if name else root
    _ensure_filter(logger)
    return logger
