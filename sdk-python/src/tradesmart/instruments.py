"""Script master (instrument list) download and parsing.

The column layout of the files is undocumented, so parsing is purely header-based:
no column names are assumed anywhere in this module.
"""

from __future__ import annotations

import asyncio
import csv
import functools
import io
import os
import tempfile
import time
import zipfile
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

import httpx

from tradesmart._logging import get_logger
from tradesmart._transport.codec import redact_body
from tradesmart.config import ClientConfig, header_user_agent
from tradesmart.enums import Exchange
from tradesmart.errors import (
    APIError,
    AuthenticationError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    RequestTimeoutError,
    ResponseParseError,
    ServerError,
    TradeSmartError,
)
from tradesmart.models.base import IST

_ENDPOINT = "script_master"
_log = get_logger("instruments")


class _CachedFileGone(Exception):
    """A conditional re-check got 304, but the cached file was deleted meanwhile."""


def _code(exchange: Exchange | str) -> str:
    """Validated exchange code; ``ValueError`` (listing the allowed ones) for anything else."""
    try:
        return Exchange(exchange).value  # any case; Exchange members pass through
    except ValueError:
        allowed = ", ".join(e.value for e in Exchange)
        raise ValueError(f"unknown exchange {exchange!r}; allowed: {allowed}") from None


def script_master_url(exchange: Exchange | str, *, base_url: str) -> str:
    """URL of the zipped script master for ``exchange``."""
    return f"{base_url.rstrip('/')}/{_code(exchange)}_symbols.txt.zip"


def _default_dir() -> Path:
    """Directory used when no ``dest_dir`` is given: ``~/.cache/tradesmart`` (per user)."""
    return Path.home() / ".cache" / "tradesmart"


def _target(exchange: Exchange | str, dest_dir: str | Path | None, config: ClientConfig) -> Path:
    """The file for ``exchange``: in ``dest_dir``, else ``config.instruments_dir`` (both ``~``
    expanded), else the user cache folder."""
    code = _code(exchange)  # validated before any path is built
    if dest_dir is not None:
        folder = Path(dest_dir).expanduser()
    elif config.instruments_dir is not None:
        folder = Path(config.instruments_dir).expanduser()
    else:
        folder = _default_dir()
    return folder / f"{code}_symbols.txt"


def _now() -> datetime:
    """Current time in IST (a seam for tests)."""
    return datetime.now(IST)


def _last_refresh(now: datetime, refresh_time: str) -> datetime:
    """The most recent daily refresh instant (IST) at or before ``now``."""
    hour, minute = (int(part) for part in refresh_time.split(":"))
    today = now.astimezone(IST).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return today if now >= today else today - timedelta(days=1)


def _is_fresh(path: Path, *, force: bool, refresh_time: str, now: datetime | None = None) -> bool:
    """True when ``path`` exists and was written at or after the last daily refresh (IST).

    TradeSmart regenerates the files once a day at ``refresh_time``; a file downloaded
    before that instant is stale even if it was written today. No request is made.
    """
    if force or not path.is_file():
        return False
    modified = datetime.fromtimestamp(path.stat().st_mtime, IST)
    return modified >= _last_refresh(now if now is not None else _now(), refresh_time)


def _checked_path(path: Path) -> Path:
    """Sidecar recording when the server was last asked about ``path``."""
    return path.with_name(path.name + ".checked")


def _last_checked(path: Path) -> datetime | None:
    """When the server was last checked for ``path``; ``None`` if unknown or unreadable."""
    try:
        stamp = datetime.fromisoformat(_checked_path(path).read_text(encoding="ascii").strip())
    except (OSError, ValueError):  # missing, a directory, not ASCII, not ISO-8601
        return None
    return stamp if stamp.tzinfo is not None else None


def _mark_checked(path: Path, now: datetime) -> None:
    """Record ``now`` (UTC, ISO-8601) as the last check of ``path``; never fails the call."""
    stamp = now.astimezone(UTC).isoformat().encode("ascii")
    try:
        _write_atomic(_checked_path(path), stamp)
    except OSError:
        _log.debug("Could not record the check time of %s", path.name)


def _request_headers(
    path: Path, *, force: bool, config: ClientConfig, now: datetime
) -> dict[str, str] | None:
    """Extra request headers for a download of ``path``, or ``None`` when no request is due.

    ``{}`` asks for a full download (``force`` or no file). A file written after the last
    refresh is fresh. An older one (the server had not regenerated it) is used as is while
    the server was checked less than ``script_master_recheck_minutes`` ago; otherwise it is
    re-checked with ``If-Modified-Since`` (its mtime is the server's ``Last-Modified``).
    """
    if force or not path.is_file():
        return {}
    if _is_fresh(path, force=False, refresh_time=config.script_master_refresh_time, now=now):
        return None
    checked = _last_checked(path)
    window = timedelta(minutes=config.script_master_recheck_minutes)
    if checked is not None and timedelta(0) <= now - checked < window:
        return None
    modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return {"If-Modified-Since": format_datetime(modified, usegmt=True)}


def _status_error(response: httpx.Response) -> APIError:
    status = response.status_code
    cls: type[APIError]
    if status in (401, 403):
        cls = AuthenticationError
    elif status == 404:
        cls = NotFoundError
    elif status == 429:
        cls = RateLimitError
    elif status >= 500:
        cls = ServerError
    else:
        cls = APIError
    return cls(
        f"{_ENDPOINT}: HTTP {status}",
        endpoint=_ENDPOINT,
        http_status=status,
        raw=redact_body(response.text)[:500],
    )


def _transport_error(exc: httpx.RequestError) -> TradeSmartError:
    """Map any httpx request failure (incl. decoding errors, redirect loops); no secrets."""
    if isinstance(exc, httpx.TimeoutException):
        return RequestTimeoutError(f"{_ENDPOINT}: request timed out")
    return NetworkError(f"{_ENDPOINT}: network error ({type(exc).__name__})")


def _parse_error(message: str) -> ResponseParseError:
    return ResponseParseError(message, endpoint=_ENDPOINT, http_status=200, body="")


def _extract_text_member(content: bytes, exchange: str) -> bytes:
    """Return the bytes of the ``.txt`` member of the zip (member paths are never used)."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            candidates = []
            for info in zf.infolist():
                if info.is_dir():
                    continue
                base = PurePosixPath(PureWindowsPath(info.filename).as_posix()).name
                if base.lower().endswith(".txt"):
                    candidates.append((base, info))
            if not candidates:
                raise _parse_error("script master zip contains no .txt file")
            chosen = next(
                (c for c in candidates if c[0].upper().startswith(exchange.upper())),
                candidates[0],
            )
            return zf.read(chosen[1])
    except zipfile.BadZipFile as exc:
        raise _parse_error("script master response is not a valid zip file") from exc


def _last_modified(header: str | None) -> datetime | None:
    """The ``Last-Modified`` instant, or ``None`` when absent or unparseable."""
    if not header:
        return None
    try:
        parsed = parsedate_to_datetime(header)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _apply_last_modified(path: Path, header: str | None) -> None:
    """Stamp ``path`` with the server's ``Last-Modified`` time (else keep the write time).

    Freshness compares the file time with the last daily refresh, so a download made just
    after the refresh time but before the server regenerated the file counts as stale.
    The stamp is never later than now (a future-dated header cannot make a stale file look
    fresh), and a failure to set it keeps the write time: the download still succeeds.
    """
    modified = _last_modified(header)
    if modified is None:
        return
    try:
        stamp = min(modified.timestamp(), time.time())
        os.utime(path, (stamp, stamp))
    except (OSError, OverflowError, ValueError):
        _log.debug("Could not set the modification time of %s; keeping the write time", path.name)


def _write_atomic(target: Path, data: bytes) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=target.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return target


def load(path: str | Path) -> list[dict[str, str]]:
    """Parse a script-master text file into header-keyed rows (all values strings).

    A file that is not UTF-8 text or not valid CSV (a NUL byte, a field over the csv module's
    size limit) raises ``ResponseParseError``, the same on every Python version.
    """
    try:
        return _load(path)
    except UnicodeDecodeError as exc:
        raise _parse_error(
            f"script master file {Path(path).name} is not valid UTF-8 text; "
            "delete it and download the instruments again"
        ) from exc
    except csv.Error as exc:
        raise _parse_error(
            f"script master file {Path(path).name} is not a valid script master ({exc}); "
            "delete it and download the instruments again"
        ) from exc


def vanished_error(path: Path) -> ResponseParseError:
    """The script master was removed again between its download and its parse."""
    return _parse_error(
        f"script master file {path.name} was removed before it could be read; "
        "download the instruments again"
    )


def _without_nul(lines: Iterable[str]) -> Iterator[str]:
    """``lines`` unchanged; a NUL byte raises ``csv.Error`` (the csv module alone accepts it)."""
    for line in lines:
        if "\x00" in line:
            raise csv.Error("line contains NUL")
        yield line


def _load(path: str | Path) -> list[dict[str, str]]:
    # utf-8-sig: a byte-order mark never becomes part of the first header.
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        # Pick the delimiter by counting candidates in the header line: csv.Sniffer is
        # unreliable on small samples and differs between Python versions.
        first = next((ln for ln in sample.splitlines() if ln.strip()), "")
        counts = {d: first.count(d) for d in ",|\t;"}
        best = max(counts, key=lambda d: counts[d])
        delimiter = best if counts[best] else ","
        header: list[str] | None = None
        rows: list[dict[str, str]] = []
        for raw in csv.reader(_without_nul(fh), delimiter=delimiter):
            if not any(cell.strip() for cell in raw):
                continue
            if header is None:
                header = [h.strip() for h in raw]
                continue
            rows.append({k: v.strip() for k, v in zip(header, raw, strict=False)})
    return rows


def find(rows: list[dict[str, str]], **criteria: str) -> list[dict[str, str]]:
    """Rows whose value for every given header equals the given string exactly."""
    return [r for r in rows if all(r.get(k) == v for k, v in criteria.items())]


class _InstrumentsBase:
    def __init__(self, config: ClientConfig) -> None:
        self._config = config
        self._timeout = httpx.Timeout(config.read_timeout, connect=config.connect_timeout)

    def _url(self, exchange: Exchange | str) -> str:
        return script_master_url(exchange, base_url=self._config.script_master_base_url)

    def _headers(self, extra: dict[str, str]) -> dict[str, str]:
        return {"User-Agent": header_user_agent(self._config), **extra}

    def _plan(self, target: Path, force: bool, now: datetime) -> dict[str, str] | None:
        return _request_headers(target, force=force, config=self._config, now=now)

    @staticmethod
    def _cached_after_failure(
        target: Path, *, conditional: bool, error: TradeSmartError, now: datetime
    ) -> bool:
        """A failed re-check (network, timeout, 5xx) of a cached file keeps that file.

        Only for the conditional re-check: a first or forced download still raises, and so
        does any failure when the file has gone. The check time is recorded, so during an
        outage the server is asked at most once per ``script_master_recheck_minutes``.
        """
        if not conditional or not target.is_file():
            return False
        _log.warning("using the cached script master; re-check failed: %s", type(error).__name__)
        _mark_checked(target, now)
        return True

    @classmethod
    def _finish(
        cls,
        response: httpx.Response,
        exchange: Exchange | str,
        target: Path,
        *,
        conditional: bool,
        now: datetime,
    ) -> Path:
        """The downloaded (or still current) file; ``_CachedFileGone`` after a 304 for a
        file that was deleted while the request was in flight."""
        if conditional and response.status_code == 304:
            if not target.is_file():
                raise _CachedFileGone
            _mark_checked(target, now)  # not modified: the file is left untouched
            return target
        if response.status_code >= 500:
            error = _status_error(response)
            if cls._cached_after_failure(target, conditional=conditional, error=error, now=now):
                return target
            raise error
        if response.status_code >= 400:
            raise _status_error(response)
        if response.status_code != 200:
            status = response.status_code
            if 300 <= status < 400:
                message = (
                    f"script master download was redirected (HTTP {status}); "
                    "redirects are never followed"
                )
            else:
                message = f"script master download returned HTTP {status}, not a zip file"
            raise ResponseParseError(message, endpoint=_ENDPOINT, http_status=status, body="")
        path = _write_atomic(target, _extract_text_member(response.content, _code(exchange)))
        _apply_last_modified(path, response.headers.get("Last-Modified"))
        _mark_checked(path, now)
        return path


class Instruments(_InstrumentsBase):
    """Blocking script-master helper."""

    def __init__(self, http: httpx.Client, config: ClientConfig) -> None:
        super().__init__(config)
        self._http = http

    def download(
        self,
        exchange: Exchange | str,
        dest_dir: str | Path | None = None,
        *,
        force: bool = False,
    ) -> Path:
        """Download ``<EXCH>_symbols.txt`` into ``dest_dir``.

        Without ``dest_dir`` the file goes to ``ClientConfig.instruments_dir`` (default
        ``~/.cache/tradesmart``).

        The file is reused while it was written after TradeSmart's last daily refresh
        (``ClientConfig.script_master_refresh_time``, IST). A file the server had not yet
        regenerated is re-checked with a conditional request at most every
        ``ClientConfig.script_master_recheck_minutes``; ``force`` re-downloads it.
        """
        target = _target(exchange, dest_dir, self._config)
        now = _now()
        extra = self._plan(target, force, now)
        if extra is None:
            return target
        try:
            return self._fetch(exchange, target, extra, now)
        except _CachedFileGone:
            _log.debug("cached %s vanished during the re-check; downloading it", target.name)
            return self._fetch(exchange, target, {}, now)

    def _fetch(
        self, exchange: Exchange | str, target: Path, extra: dict[str, str], now: datetime
    ) -> Path:
        """One GET with ``extra`` headers (conditional when not empty)."""
        try:
            response = self._http.get(
                self._url(exchange),
                headers=self._headers(extra),
                timeout=self._timeout,
                follow_redirects=False,  # never followed, also on injected clients
            )
        except httpx.RequestError as exc:  # TransportError, DecodingError, TooManyRedirects
            error = _transport_error(exc)
            if self._cached_after_failure(target, conditional=bool(extra), error=error, now=now):
                return target
            raise error from exc
        return self._finish(response, exchange, target, conditional=bool(extra), now=now)


class AsyncInstruments(_InstrumentsBase):
    """asyncio script-master helper."""

    def __init__(self, http: httpx.AsyncClient, config: ClientConfig) -> None:
        super().__init__(config)
        self._http = http

    async def download(
        self,
        exchange: Exchange | str,
        dest_dir: str | Path | None = None,
        *,
        force: bool = False,
    ) -> Path:
        """Download ``<EXCH>_symbols.txt`` into ``dest_dir``.

        Without ``dest_dir`` the file goes to ``ClientConfig.instruments_dir`` (default
        ``~/.cache/tradesmart``).

        The file is reused while it was written after TradeSmart's last daily refresh
        (``ClientConfig.script_master_refresh_time``, IST). A file the server had not yet
        regenerated is re-checked with a conditional request at most every
        ``ClientConfig.script_master_recheck_minutes``; ``force`` re-downloads it.
        File work (the freshness check, unzipping, writing) runs in a worker thread so the
        event loop is not blocked.
        """
        target = _target(exchange, dest_dir, self._config)
        now = _now()
        extra = await asyncio.to_thread(self._plan, target, force, now)
        if extra is None:
            return target
        try:
            return await self._fetch(exchange, target, extra, now)
        except _CachedFileGone:
            _log.debug("cached %s vanished during the re-check; downloading it", target.name)
            return await self._fetch(exchange, target, {}, now)

    async def _fetch(
        self, exchange: Exchange | str, target: Path, extra: dict[str, str], now: datetime
    ) -> Path:
        """One GET with ``extra`` headers (conditional when not empty)."""
        try:
            response = await self._http.get(
                self._url(exchange),
                headers=self._headers(extra),
                timeout=self._timeout,
                follow_redirects=False,  # never followed, also on injected clients
            )
        except httpx.RequestError as exc:  # TransportError, DecodingError, TooManyRedirects
            error = _transport_error(exc)
            cached = await asyncio.to_thread(
                functools.partial(
                    self._cached_after_failure,
                    target,
                    conditional=bool(extra),
                    error=error,
                    now=now,
                )
            )
            if cached:
                return target
            raise error from exc
        return await asyncio.to_thread(
            functools.partial(
                self._finish, response, exchange, target, conditional=bool(extra), now=now
            )
        )
