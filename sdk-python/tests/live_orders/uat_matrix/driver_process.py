"""``DriverSdk``: the harness side of a live-order driver process (``live-driver.md``).

Another language's SDK joins the matrix through a driver: a child process that holds that SDK
and executes the matrix's calls with it, one JSON line per request on its stdin, one per reply
on its stdout. ``DriverSdk`` is an ``SdkUnderTest``: ``calls.py`` sends the eight
state-changing calls through it exactly as through ``PythonSdk``, and gets back the Python
return types and the Python error classes, so the matrix's classification is unchanged.

Start-up (only once every harness gate has passed: ``gate.matrix_session`` builds it):

- the process starts with the run's nonce in ``TRADESMART_LIVE_DRIVER_RUN`` (and the given
  environment), in its own process group, so the whole tree can be killed;
- its first stdout line must be the handshake: ``protocol`` 2, a known ``sdk`` (the selected
  one when ``name`` is given), a ``version``, the harness's ``environment``, the nonce echoed in
  ``run``, and a ``client_id`` equal, ignoring case, to ``TRADESMART_LIVE_ORDER_ACCOUNT`` and to
  the Python referee client's id. Anything else, or nothing within ``start_timeout``: the
  process tree is killed and the start refused (``Refusal``) before any request.

One call is one request line, written once and never again, whatever happens:

- a successful write's id comes from its **wire body**: the reply's mandatory ``raw`` is parsed
  and classified by the Python SDK's own parser for the endpoint (``_from_wire``), and that id
  is returned (the one the ledger records as ours and teardown cancels); the driver's
  ``result`` is only compared, a difference is a **finding** (``findings``, a
  ``driver_finding`` ledger line); the raw body and the request go to the session's
  ``ResponseLog``, so the outcome line carries them as it does for Python. A sliced result
  object and a read's ``result`` are used as returned;
- a reply ``error`` raises the Python class its ``type`` names, with its fields (``raw`` is the
  ``APIError``'s ``raw`` and the ``OrderStatusUnknownError``'s ``body``; the message and
  ``emsg`` redacted);
- **no reply** within the reply timeout, a partial line, a line that is not a JSON object, a
  reply to another request id, a reply without exactly one of ``result`` / ``error``, an
  unknown error type, a write success without ``raw``, a ``raw`` the Python parse refuses or
  does not call a success, a ``result`` id that differs from it, or the process ending: on a
  state-changing call
  ``OrderStatusUnknownError`` (the request may have been executed), on a read ``NetworkError``.
  Either way the process tree is killed, the driver is stopped for good (every later call is
  refused with ``RequestValidationError`` before anything is written: nothing sent), and the
  session is told (``bind``'s ``stop``; a write's failure is CHECK THE ACCOUNT, which also
  stops every later SDK of the run, ``gate.sdk_stopped``).

The driver's stderr is read all the time and kept only redacted (``_redact``: the secret values
of the environment and of the referee client's login (``sdk_under_test.referee_secrets``),
account ids, the nonce, bearer tokens, and the value of every secret-named key in ``key: value``
/ ``key=value`` / JSON form, as Node's inspect prints them). It is never echoed and never put in
a refusal or skip reason: it goes to the ledger as ``driver_stderr`` when the driver stops or
closes, and to ``stderr_log`` when one is given (a refused start has no ledger yet:
``DriverRefusal.driver_stderr``). ``close`` ends stdin, waits a moment, then kills the whole
process tree.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import signal
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import IO, Any, NoReturn

from tests._environment import environment_name
from tests.live_orders.safety import ACCOUNT_ENV, Refusal, ResponseLog
from tests.live_orders.uat_matrix.sdk_under_test import SDK_CODES
from tradesmart import ClientConfig, OcoLeg, OrderType
from tradesmart._client_core import STATE_CHANGING_ENDPOINTS
from tradesmart._logging import redact as redact_secrets
from tradesmart._slicing import DEFAULT_MAX_SLICES, SlicedOrderResult
from tradesmart._transport.codec import TOO_DEEP, _redact_raw, parse_response
from tradesmart.endpoints.base import EndpointSpec
from tradesmart.endpoints.gtt import CANCEL_GTT_ORDER, MODIFY_GTT_ORDER, PLACE_GTT_ORDER
from tradesmart.endpoints.oco import CANCEL_OCO_ORDER, MODIFY_OCO_ORDER, PLACE_OCO_ORDER
from tradesmart.endpoints.orders import (
    CANCEL_ORDER,
    EXIT_SNO_ORDER,
    MODIFY_ORDER,
    PLACE_ORDER,
    modify_ack_order_id,
)
from tradesmart.errors import (
    APIError,
    AuthenticationError,
    ConfigurationError,
    NetworkError,
    NotFoundError,
    OrderRejectedError,
    OrderStatusUnknownError,
    RateLimitError,
    RequestTimeoutError,
    RequestValidationError,
    ResponseParseError,
    ServerError,
    TradeSmartError,
)

DRIVER_RUN_ENV = "TRADESMART_LIVE_DRIVER_RUN"
PROTOCOL = 2
# The reply timeout (``live-driver.md``): the driver's configured connect and read timeouts
# (the SDK defaults: 5 s + 10 s), its order rate limiter's longest wait (1 s) and a margin of
# 14 s for the process itself: 30 s per SDK call. ``place_sliced_order`` makes up to
# ``max_slices`` order calls plus two reads (the script master and ``security_info``), so it
# waits that many times as long.
REPLY_MARGIN = 14.0
SLICED_EXTRA_CALLS = 2
KILL_WAIT = 5.0  # seconds to wait for a killed process to be reaped
CLOSE_WAIT = 5.0  # seconds a closing driver is given to exit after end of file
JOIN_WAIT = 2.0  # seconds to wait for a reader thread after the process ended
MAX_LINE = 4_000_000  # bytes; a longer line is not a whole line (malformed)
STDERR_KEEP = 200  # redacted stderr lines kept (the latest)
MASK = "***"
_MIN_SECRET = 4  # shorter values could match ordinary text
_SECRET_NAME = re.compile(r"TOKEN|SECRET|KEY|PASSWORD|CHECKSUM|AUTH|CODE|ACCOUNT|CLIENT_ID", re.I)

# The REST endpoint of each state-changing call (``place_sliced_order`` places orders).
ENDPOINTS: dict[str, str] = {
    "place_order": "PlaceOrder",
    "place_sliced_order": "PlaceOrder",
    "modify_order": "ModifyOrder",
    "cancel_order": "CancelOrder",
    "exit_order": "ExitSNOOrder",
    "place_gtt": "PlaceGTTOrder",
    "modify_gtt": "ModifyGTTOrder",
    "cancel_gtt": "CancelGTTOrder",
    "place_oco": "PlaceOCOOrder",
    "modify_oco": "ModifyOCOOrder",
    "cancel_oco": "CancelOCOOrder",
}
WRITES = frozenset(ENDPOINTS)
# Every endpoint the SDK treats as state-changing, by lower-case name (endpoint names are matched
# case-insensitively): ``call("request", ...)`` to one of them is a write, so it is refused.
_STATE_CHANGING_NAMES = frozenset(name.lower() for name in STATE_CHANGING_ENDPOINTS)


def _request_can_change_state(args: Mapping[str, Any]) -> bool:
    """Whether a ``request`` call is flagged ``state_changing`` or names a state-changing
    endpoint. Anything but a plain string endpoint counts as one (refused, nothing sent)."""
    endpoint = args.get("endpoint")
    return (
        args.get("state_changing") is True
        or not isinstance(endpoint, str)
        or endpoint.strip().lower() in _STATE_CHANGING_NAMES
    )


_SPECS: dict[str, EndpointSpec[Any]] = {
    "place_order": PLACE_ORDER,
    "modify_order": MODIFY_ORDER,
    "cancel_order": CANCEL_ORDER,
    "exit_order": EXIT_SNO_ORDER,
    "place_gtt": PLACE_GTT_ORDER,
    "modify_gtt": MODIFY_GTT_ORDER,
    "cancel_gtt": CANCEL_GTT_ORDER,
    "place_oco": PLACE_OCO_ORDER,
    "modify_oco": MODIFY_OCO_ORDER,
    "cancel_oco": CANCEL_OCO_ORDER,
}
_API_ERRORS: dict[str, type[APIError]] = {
    "APIError": APIError,
    "AuthenticationError": AuthenticationError,
    "RateLimitError": RateLimitError,
    "NotFoundError": NotFoundError,
    "ServerError": ServerError,
    "OrderRejectedError": OrderRejectedError,
}
_PLAIN_ERRORS: dict[str, type[Exception]] = {
    "NetworkError": NetworkError,
    "RequestTimeoutError": RequestTimeoutError,
    "ConfigurationError": ConfigurationError,
    "ValueError": ValueError,
}

Note = Callable[..., None]
Stop = Callable[[str, bool], None]


def default_reply_timeout(config: ClientConfig | None = None) -> float:
    """The reply timeout of one SDK call: the driver's connect and read timeouts, its order
    rate limiter's longest wait, and ``REPLY_MARGIN`` (30 s with the SDK defaults)."""
    config = config if config is not None else ClientConfig()
    return (
        config.connect_timeout
        + config.read_timeout
        + config.order_rate_limit_max_wait
        + REPLY_MARGIN
    )


class _Malformed(Exception):
    """A reply the protocol does not allow (the driver is then stopped)."""


@dataclass(frozen=True)
class _Answer:
    """A well-formed reply: its value, or the error it names (raised by ``_call``, outside the
    handler that stops the driver on anything unexpected)."""

    value: Any = None
    error: Exception | None = None


_TIMEOUT = object()


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def _encode(value: Any) -> Any:
    """The conformance vectors' value encoding (Decimals as strings, enums by value); a value it
    has no encoding for (a binary float, an object) is refused before anything is sent."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Enum):
        return _encode(value.value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, OcoLeg):
        return _oco_leg(value)
    raise TypeError(f"no protocol encoding for a {type(value).__name__}")


def _leg_price(value: Any) -> str | None:
    """One price of an OCO leg as the protocol's string (``None`` stays ``null``); a binary
    float, or anything else that is not a decimal, an integer or a string, is refused."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, Decimal) or (isinstance(value, int) and not isinstance(value, bool)):
        return str(value)
    raise TypeError(f"no protocol encoding for an OCO leg price that is a {type(value).__name__}")


def _oco_leg(leg: OcoLeg) -> dict[str, Any]:
    """An ``OcoLeg`` as the protocol's leg object (``live-driver.md``): exactly
    ``trigger_value``, ``price``, ``order_type`` and ``trigger_price``, prices as strings (or
    ``null``), the order type as its wire code (a word the SDK does not know is sent as given,
    for the driver's SDK to refuse)."""
    kind = leg.order_type
    if not isinstance(kind, OrderType):
        with contextlib.suppress(ValueError):
            kind = OrderType(kind)
    order_type = kind.value if isinstance(kind, OrderType) else kind
    if not isinstance(order_type, str):
        raise TypeError(f"no protocol encoding for an OCO leg order type {type(kind).__name__}")
    return {
        "trigger_value": _leg_price(leg.trigger_value),
        "price": _leg_price(leg.price),
        "order_type": order_type,
        "trigger_price": _leg_price(leg.trigger_price),
    }


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _status(obj: Mapping[str, Any]) -> int | None:
    """An ``http_status`` field: absent or ``null`` is ``None``; an integer is itself; anything
    else (a string, a float, a boolean) is a malformed reply, never coerced."""
    value = obj.get("http_status")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Malformed(f"wrote an http_status that is not an integer ({type(value).__name__})")
    return int(value)


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


# Secret key names, compared lower-case without ``_`` / ``-`` (``accessToken``, ``access_token``,
# ``secretKey``, ``app-key`` ...): their value is masked in any ``key: value`` / ``key=value`` pair,
# quoted or not (Node's ``util.inspect`` prints ``{ accessToken: 'eyJ...' }``). Besides these
# names, any key ending in ``token``, ``secret``, ``key``, ``passwd`` or ``password``, or containing
# ``secret`` or ``password``, is secret; ``token`` alone (an instrument token in this API) and
# ``key`` alone (a case key) are not.
_SECRET_KEYS = frozenset(
    {"authorization", "checksum", "authcode", "jkey", "apisecret", "secret", "password"}
)
_SECRET_SUFFIXES = ("token", "secret", "key", "passwd", "password")
_PLAIN_KEYS = frozenset({"token", "key"})


def _secret_key(name: str) -> bool:
    key = name.replace("_", "").replace("-", "").lower()
    if key in _PLAIN_KEYS:
        return False
    return (
        key in _SECRET_KEYS
        or key.endswith(_SECRET_SUFFIXES)
        or "secret" in key
        or "password" in key
    )


_KEY_VALUE = re.compile(
    r"""(?P<q>["']?)(?P<k>[A-Za-z_][A-Za-z0-9_-]*)(?P=q)(?P<sep>\s*[:=]\s*)"""
    r"""(?P<v>'[^']*'|"[^"]*"|[^\s,;}\])&]+)"""
)


def _mask_pair(match: re.Match[str]) -> str:
    if not _secret_key(match["k"]):
        return match[0]
    value = match["v"]
    quote = value[0] if value[:1] in ("'", '"') else ""
    return f"{match['q']}{match['k']}{match['q']}{match['sep']}{quote}{MASK}{quote}"


class DriverRefusal(Refusal):
    """A driver refused or failed its start: ``reason`` never carries its stderr (and every
    driver-controlled text in it is redacted); the redacted lines are on ``driver_stderr`` and in
    the ``stderr_log`` file when one is given (there is no ledger yet at a refused start)."""

    def __init__(self, reason: str, driver_stderr: Iterable[str] = ()) -> None:
        super().__init__(reason)
        self.driver_stderr = list(driver_stderr)


def _not_sent(text: str) -> RequestValidationError:
    error = RequestValidationError([{"type": "driver_stopped", "loc": (), "msg": text}])
    error.args = (f"Invalid request parameters: {text}",)
    return error


class DriverSdk:
    """An ``SdkUnderTest`` executed by a driver process (see the module docstring)."""

    # One request at a time (``_call`` holds ``_lock`` for the whole exchange). ``send_slot``
    # is the slot ``calls.py`` holds from the order-pacer wait to the write, so the pacer slot
    # and the send are one step in order: calls queued behind a slow reply cannot pile up and go
    # out back to back, whatever the batch's worker count (final review M-a). It is a lock of
    # its own, apart from ``_lock``, which ``_call`` keeps as defence in depth.

    name: str
    code: str
    version: str
    client_id: str

    def __init__(
        self,
        command: Sequence[str],
        *,
        nonce: str,
        environ: Mapping[str, str],
        reply_timeout: float,
        referee_client_id: str | None = None,
        name: str | None = None,
        start_timeout: float | None = None,
        secrets: Iterable[str] = (),
        stderr_log: Path | None = None,
    ) -> None:
        if not isinstance(nonce, str) or not nonce.strip():
            raise Refusal(f"no per-run nonce for the driver ({DRIVER_RUN_ENV}); refusing")
        if not reply_timeout > 0:
            raise ValueError("reply_timeout must be a positive number of seconds")
        account = _norm(environ.get(ACCOUNT_ENV))
        if not account:
            raise Refusal(
                f"set {ACCOUNT_ENV} to the owner-approved test account id; refusing to start a "
                "live-order driver"
            )
        try:
            self.environment = environment_name(environ)
        except OSError as exc:
            raise Refusal(f"{exc}; refusing to start a live-order driver") from None
        self.reply_timeout = float(reply_timeout)
        self.start_timeout = float(start_timeout if start_timeout is not None else reply_timeout)
        self.findings: list[dict[str, Any]] = []
        self.stopped: str | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._send_slot = threading.Lock()
        self._next_id = 0
        self._note: Note | None = None
        self._stop: Stop | None = None
        self._responses: ResponseLog | None = None
        self._pending_notes: list[tuple[str, dict[str, Any]]] = []
        self._pending_stops: list[tuple[str, bool]] = []
        self._stderr: deque[str] = deque(maxlen=STDERR_KEEP)
        self._stderr_lock = threading.Lock()
        self._stderr_reported = 0
        self._secrets = self._secret_values(environ, nonce, account, referee_client_id, secrets)
        self.stderr_log = stderr_log
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self.name = name or "driver"
        self.code = ""
        self.version = ""
        self.client_id = ""
        env = dict(environ)
        env[DRIVER_RUN_ENV] = nonce
        extra: dict[str, Any] = {"start_new_session": True} if os.name == "posix" else {}
        try:
            self.process: subprocess.Popen[bytes] = subprocess.Popen(  # noqa: S603 - our command
                list(command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                **extra,
            )
        except OSError as exc:
            raise Refusal(
                f"the live-order driver could not be started ({type(exc).__name__}); refusing"
            ) from None
        self._readers = [
            threading.Thread(target=self._read_stdout, name="driver-stdout", daemon=True),
            threading.Thread(target=self._read_stderr, name="driver-stderr", daemon=True),
        ]
        for reader in self._readers:
            reader.start()
        try:
            self._handshake(nonce, account, referee_client_id, name)
        except BaseException:
            self._kill()
            raise

    def send_slot(self) -> contextlib.AbstractContextManager[Any]:
        """The one send slot of this driver (see the class comment): held by a state-changing
        call from its pacer wait to its write, never by a read. Not ``_lock``: a call holding
        the slot goes on to ``_call``, which takes ``_lock``."""
        return self._send_slot

    # -- identity and binding ------------------------------------------------------------------

    @staticmethod
    def _secret_values(
        environ: Mapping[str, str],
        nonce: str,
        account: str,
        referee: str | None,
        secrets: Iterable[str],
    ) -> list[str]:
        values = {nonce.strip(), account, _norm(referee)}
        values.update(str(value).strip() for value in secrets if value)  # the saved login's too
        values.update(
            str(value).strip()
            for key, value in environ.items()
            if _SECRET_NAME.search(key) and value
        )
        return sorted((v for v in values if len(v) >= _MIN_SECRET), key=len, reverse=True)

    def bind(self, *, note: Note, stop: Stop, responses: ResponseLog) -> None:
        """The session's ledger note, its stop, and its raw-body log (``gate`` binds them)."""
        self._note, self._stop, self._responses = note, stop, responses
        pending, self._pending_notes = self._pending_notes, []
        for kind, detail in pending:
            self._record(kind, **detail)
        stops, self._pending_stops = self._pending_stops, []
        for reason, check in stops:
            stop(reason, check)

    def _record(self, kind: str, **detail: Any) -> None:
        if self._note is None:
            self._pending_notes.append((kind, detail))
            return
        with contextlib.suppress(Exception):  # a record never hides the call's own outcome
            self._note(kind, **detail)

    def _report_stop(self, reason: str, check_the_account: bool) -> None:
        if self._stop is None:
            self._pending_stops.append((reason, check_the_account))
            return
        with contextlib.suppress(Exception):
            self._stop(reason, check_the_account)

    @property
    def stderr_lines(self) -> list[str]:
        """The driver's stderr lines seen so far (the latest ``STDERR_KEEP``), redacted."""
        with self._stderr_lock:
            return list(self._stderr)

    def _redact(self, text: str) -> str:
        """Mask secrets in a text from the driver: the SDK's own rules (bearer tokens, quoted
        and ``key=value`` secret keys), unquoted ``key: value`` pairs of secret key names (Node's
        inspect output), and every known secret value (environment, the referee client's
        login, the nonce, the account ids), ignoring case."""
        text = str(redact_secrets(text))
        text = _KEY_VALUE.sub(_mask_pair, text)
        for value in self._secrets:
            text = re.sub(re.escape(value), MASK, text, flags=re.IGNORECASE)
        return text

    # -- the process ---------------------------------------------------------------------------

    def _read_stdout(self) -> None:
        stream = self.process.stdout
        try:
            if stream is not None:
                while line := stream.readline(MAX_LINE):
                    self._lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(None)

    def _read_stderr(self) -> None:
        stream: IO[bytes] | None = self.process.stderr
        try:
            if stream is not None:
                while line := stream.readline(MAX_LINE):
                    shown = self._redact(line.decode("utf-8", errors="replace").rstrip("\r\n"))
                    with self._stderr_lock:
                        self._stderr.append(shown)
        except (OSError, ValueError):
            pass

    def _await(self, timeout: float) -> bytes | None | object:
        try:
            return self._lines.get(timeout=timeout)
        except queue.Empty:
            return _TIMEOUT

    def _kill(self) -> None:
        """Kill the driver's whole process tree and reap it: its own process group on POSIX,
        ``taskkill /T`` on Windows (``process.kill()`` alone ends the driver but not what it
        started, such as the runtime a launcher script runs)."""
        process = self.process
        if os.name == "posix":
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
        elif os.name == "nt":
            # Before process.kill(): taskkill finds the children through their parent.
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(  # noqa: S603 - fixed command, our own child's pid
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],  # noqa: S607
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=KILL_WAIT,
                    check=False,
                )
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(KILL_WAIT)
        with contextlib.suppress(OSError, ValueError):
            if process.stdin is not None:
                process.stdin.close()
        # A reader still blocked in readline means something outside the tree kill holds the
        # pipe open; closing that stream would wait on the reader forever, so it is left to the
        # daemon thread. Readers: stdout first, then stderr (see __init__).
        for reader, stream in zip(self._readers, (process.stdout, process.stderr), strict=True):
            reader.join(JOIN_WAIT)
            if not reader.is_alive():
                with contextlib.suppress(OSError, ValueError):
                    if stream is not None:
                        stream.close()

    def _report_stderr(self, event: str = "stderr") -> None:
        """The redacted stderr lines go to the ledger (``driver_stderr``) and the stderr log,
        never into a message, a refusal or a skip reason."""
        lines = self.stderr_lines
        if lines and len(lines) != self._stderr_reported:
            self._stderr_reported = len(lines)
            self._record("driver_stderr", sdk=self.name, lines=lines)
            self._log_stderr(event, lines)

    def _log_stderr(self, event: str, lines: list[str]) -> None:
        if self.stderr_log is None:
            return
        entry = {"at": datetime.now(UTC).isoformat(), "sdk": self.name, "event": event,
                 "lines": lines}  # fmt: skip
        with contextlib.suppress(OSError):
            self.stderr_log.parent.mkdir(parents=True, exist_ok=True)
            with self.stderr_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")

    # -- the handshake -------------------------------------------------------------------------

    def _refuse(self, why: str) -> NoReturn:
        self._kill()  # the readers are joined: every stderr line is in
        why = self._redact(why)  # it may echo a handshake value the driver wrote
        self.stopped = why
        self._report_stderr("refused")
        where = "the driver stderr log" if self.stderr_log is not None else "DriverRefusal"
        raise DriverRefusal(
            f"the live-order driver: {why}; refusing (its stderr is kept only redacted, in "
            f"{where}; never shown here)",
            self.stderr_lines,
        )

    def _handshake(
        self, nonce: str, account: str, referee: str | None, expected: str | None
    ) -> None:
        line = self._await(self.start_timeout)
        if line is _TIMEOUT:
            self._refuse(f"no handshake within {self.start_timeout:g} s")
        if not isinstance(line, bytes):
            self._refuse("the process ended before its handshake")
        try:
            data = json.loads(line.decode("utf-8")) if line.endswith(b"\n") else None
        except (UnicodeDecodeError, ValueError):
            data = None
        hello = data.get("hello") if isinstance(data, dict) else None
        if not isinstance(hello, dict):
            self._refuse('its first line is not a handshake ({"hello": {...}})')
        got = hello.get("protocol")
        if isinstance(got, int) and not isinstance(got, bool) and got < PROTOCOL:
            self._refuse(
                f"its handshake names protocol {got}, the harness needs protocol {PROTOCOL} "
                "(the OCO calls): update the driver"
            )
        if got != PROTOCOL or isinstance(got, bool):
            self._refuse(f"its handshake names protocol {got!r}, not {PROTOCOL}")
        sdk = hello.get("sdk")
        if not isinstance(sdk, str) or sdk not in SDK_CODES:
            self._refuse(f"its handshake names sdk {sdk!r}, not a known SDK")
        if expected is not None and sdk != expected:
            self._refuse(f"its handshake names sdk {sdk!r}, not the selected {expected!r}")
        version = hello.get("version")
        if not isinstance(version, str) or not version.strip():
            self._refuse("its handshake names no SDK version")
        environment = hello.get("environment")
        if environment != self.environment:
            self._refuse(
                f"its client was built for the environment {environment!r}, the harness runs "
                f"{self.environment!r}"
            )
        if hello.get("run") != nonce:
            self._refuse(f"its handshake does not echo this run's nonce ({DRIVER_RUN_ENV})")
        client_id = hello.get("client_id")
        held = _norm(client_id) if isinstance(client_id, str) else ""
        if not held or held != account:
            self._refuse(f"its client holds a different account than {ACCOUNT_ENV}")
        if referee is not None and held != _norm(referee):
            self._refuse("its client holds a different account than the harness's own client")
        version = self._redact(version)  # driver-written text, kept and recorded
        self.name, self.code, self.version = sdk, SDK_CODES[sdk], version
        self.client_id = str(client_id)
        self._record(
            "driver_hello",
            sdk=sdk,
            version=version,
            environment=environment,
            protocol=PROTOCOL,
            pid=self.process.pid,
        )

    # -- calls ---------------------------------------------------------------------------------

    def timeout_for(self, method: str, args: Mapping[str, Any]) -> float:
        """How long a reply to ``method`` is waited for (see ``REPLY_MARGIN``)."""
        if method != "place_sliced_order":
            return self.reply_timeout
        slices = args.get("max_slices")
        if not isinstance(slices, int) or isinstance(slices, bool) or slices < 1:
            slices = DEFAULT_MAX_SLICES
        return self.reply_timeout * (slices + SLICED_EXTRA_CALLS)

    def call(self, method: str, **args: Any) -> Any:
        """One request of a **read** method (the reply's ``result`` as decoded). A
        state-changing method is refused before anything is written: it goes through its own
        method, whose reply is checked against its wire body."""
        if method in WRITES:
            raise ValueError(
                f"DriverSdk.call() is for reads; {method} goes through DriverSdk.{method}() "
                "(nothing was sent)"
            )
        if method == "request" and _request_can_change_state(args):
            raise ValueError(
                "DriverSdk.call() is for reads; a request with state_changing, or to a "
                "state-changing endpoint, is refused (nothing was sent)"
            )
        return self._call(method, args)

    def _call(self, method: str, args: dict[str, Any]) -> Any:
        with self._lock:  # one request at a time (the batch engine places from threads)
            if self.stopped is not None or self._closed:
                state = self.stopped or "closed"
                raise _not_sent(f"the {self.name} driver is stopped ({state}); nothing was sent")
            try:
                encoded = {key: _encode(value) for key, value in args.items()}
            except TypeError as exc:
                raise _not_sent(f"{method}: {exc}; nothing was sent") from None
            self._next_id += 1
            request_id = self._next_id
            request = {"id": request_id, "method": method, "args": encoded}
            line = json.dumps(request, separators=(",", ":")) + "\n"
            write = method in WRITES
            why: str | None = None
            answer: _Answer | None = None
            try:
                try:
                    stdin = self.process.stdin
                    if stdin is None:
                        raise OSError("no stdin")
                    stdin.write(line.encode("utf-8"))  # once; never written again
                    stdin.flush()
                except (OSError, ValueError) as exc:
                    why = f"could not be sent the request whole ({type(exc).__name__})"
                if why is None:
                    timeout = self.timeout_for(method, args)
                    reply = self._await(timeout)
                    try:
                        answer = self._answer(method, args, request, reply, timeout)
                    except _Malformed as exc:
                        why = str(exc)
                    except RecursionError:
                        why = "wrote a reply nested too deeply to read"
            except BaseException:
                # Ctrl-C, a test timeout, a bug: the request may be out and its reply may still
                # come; the driver is stopped for good so it can never answer a later call.
                self._abandon(method, write)
                raise
            if why is not None or answer is None:
                self._failed(method, write, why or "gave no usable reply")
            if answer.error is not None:
                raise answer.error
            return answer.value

    def _abandon(self, method: str, write: bool) -> None:
        """An interruption after the request line: stop, kill, report; never raises."""
        with contextlib.suppress(Exception):
            self.stopped = f"interrupted during {method}"
            self._kill()
            self._report_stderr("interrupted")
            self._report_stop(
                f"the {self.name} live-order driver was interrupted during {method} after its "
                "request was written; it was stopped (a later reply can never answer another "
                "call) and the request was never sent again",
                write,
            )

    def _answer(
        self,
        method: str,
        args: Mapping[str, Any],
        request: Mapping[str, Any],
        reply: bytes | None | object,
        timeout: float,
    ) -> _Answer:
        if reply is _TIMEOUT:
            raise _Malformed(f"gave no reply within the reply timeout ({timeout:g} s)")
        if not isinstance(reply, bytes):
            raise _Malformed("ended (its output closed) before replying")
        if not reply.endswith(b"\n"):
            raise _Malformed("wrote a partial line and no more")
        try:
            data = json.loads(reply.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise _Malformed("wrote a reply that is not JSON") from None
        if not isinstance(data, dict):
            raise _Malformed("wrote a reply that is not a JSON object")
        reply_id = data.get("id")
        if isinstance(reply_id, bool) or reply_id != request["id"]:
            raise _Malformed(f"answered request {reply_id!r}, not {request['id']} (out of order)")
        if ("result" in data) == ("error" in data):
            raise _Malformed("wrote a reply without exactly one of result and error")
        write = method in WRITES
        summary = {"method": method, "args": request["args"]}
        if "error" in data:
            error = self._error(method, data["error"], summary)
            if write and data["error"].get("raw") is not None and self._responses is not None:
                status = _status(data["error"])
                body = _redact_raw(data["error"]["raw"])
                self._responses.record(ENDPOINTS[method], status, body, request=summary)
            return _Answer(error=error)
        result = data["result"]
        if method == "place_sliced_order":
            return _Answer(value=self._sliced(result, summary))
        if not write:
            return _Answer(value=result)
        if not isinstance(result, str) or not result.strip():
            raise _Malformed(f"returned {result!r} where {method} returns an id")
        raw, status = data.get("raw"), _status(data)
        if raw is None:  # the harness acts on the wire body only (live-driver.md)
            raise _Malformed(f"answered {method} as a success without its raw wire body")
        if self._responses is not None:
            body = _redact_raw(raw)  # recorded redacted; parsed below on the original
            self._responses.record(ENDPOINTS[method], status, body, request=summary)
        return _Answer(value=self._from_wire(method, args, result, raw, status))

    def _failed(self, method: str, write: bool, why: str) -> NoReturn:
        """The driver broke the protocol during a call: kill it, stop it for good, tell the
        session, and raise the unknown outcome (a write) or a network error (a read)."""
        why = self._redact(why)  # it may quote an id or a value the driver wrote
        self.stopped = f"{why} during {method}"
        self._kill()
        self._report_stderr("stopped")
        reason = (
            f"the {self.name} live-order driver {why} during {method}; it was stopped and the "
            "request was never sent again"
        )
        self._report_stop(reason, write)
        if write:
            raise OrderStatusUnknownError(
                ENDPOINTS[method], f"{reason}: the outcome is unknown, check orders()"
            )
        raise NetworkError(reason)

    # -- results and errors as the Python SDK's ------------------------------------------------

    def _error(
        self, method: str, error: Any, summary: Mapping[str, Any] | None = None
    ) -> Exception:
        """The Python exception an error object names (``_Malformed`` when it names none)."""
        if not isinstance(error, dict) or not isinstance(error.get("type"), str):
            raise _Malformed("wrote an error that is not an error object")
        kind = error["type"]
        # Every text from the driver is redacted as its stderr is (a message may quote a token).
        message = self._redact(str(error.get("message") or kind))
        emsg = _text_or_none(error.get("emsg"))
        # The SDK's own redaction of a kept body. A body nested too deeply to redact (``TOO_DEEP``)
        # cannot be read as a definitive answer on any Python version: a broken reply.
        original = error.get("raw")
        raw = _redact_raw(original)
        if not isinstance(original, str) and raw == TOO_DEEP:
            raise _Malformed("wrote an error raw nested too deeply to read")
        if isinstance(raw, str):
            raw = self._redact(raw)
        status = _status(error)
        endpoint = ENDPOINTS.get(method, method)
        # ``raw`` is the wire body (live-driver.md): ``APIError.raw``, else the ``body`` of an
        # ``OrderStatusUnknownError`` / ``ResponseParseError`` (JSON text, or the text itself).
        body = raw if isinstance(raw, str) or raw is None else json.dumps(raw, default=str)
        exc: Exception
        if kind in _API_ERRORS:
            exc = _API_ERRORS[kind](
                message,
                endpoint=endpoint,
                http_status=status,
                emsg=None if emsg is None else self._redact(emsg),
                raw=raw,
            )
            if raw is not None and self._wire_body_is_success(method, raw, status):
                # The driver called it a rejection; Python's parse of the same body does not:
                # the order may exist, so it is not a definitive answer (final review M-b).
                self._finding(method, "error", kind, "<success>")
                raise _Malformed(
                    f"answered {method} with {kind}, but its raw wire body is a success for the "
                    "Python SDK"
                )
            if isinstance(raw, dict) and error.get("stat") != raw.get("stat"):
                self._finding(method, "error.stat", error.get("stat"), raw.get("stat"))
            if kind == "RateLimitError":
                # Nothing went out only when the driver says so ("sent": false); a missing
                # field means sent (live-driver.md). Never read from the message.
                sent = error.get("sent", True)
                if not isinstance(sent, bool):
                    raise _Malformed("wrote a RateLimitError whose sent is not true or false")
                if sent is False and status is not None:  # refused client-side, yet a status
                    raise _Malformed("wrote a RateLimitError not sent but with an http_status")
                setattr(exc, "sent", sent)  # noqa: B010 - a driver's error only
        elif kind == "OrderStatusUnknownError":
            exc = OrderStatusUnknownError(endpoint, message, http_status=status, body=body)
        elif kind == "ResponseParseError":
            exc = ResponseParseError(
                message, endpoint=endpoint, http_status=status, body=body or ""
            )
        elif kind == "RequestValidationError":
            fields = error.get("fields") or []
            if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
                raise _Malformed("wrote a RequestValidationError whose fields are not names")
            exc = RequestValidationError(
                [{"type": "value_error", "loc": (f,), "msg": "named by the driver"} for f in fields]
            )
            exc.args = (message,)
        elif kind in _PLAIN_ERRORS:
            exc = _PLAIN_ERRORS[kind](message)
        else:
            raise _Malformed(f"reported the error type {kind!r}, not a contract error name")
        partial = error.get("sliced_result")
        if partial is not None:
            if method != "place_sliced_order":  # only a sliced place has a partial result
                raise _Malformed(f"wrote a sliced_result in an error of {method}")
            sliced = self._sliced(partial, summary or {"method": method}, partial=True)
            setattr(exc, "sliced_result", sliced)  # noqa: B010 - any class
        return exc

    def _sliced(
        self, data: Any, summary: Mapping[str, Any], *, partial: bool = False
    ) -> SlicedOrderResult:
        """A ``place_sliced_order`` result object as the Python ``SlicedOrderResult``, its
        order ids from the **wire bodies** of its ``slices`` (one per placed slice, in order:
        each parsed as ``PlaceOrder``, ``_from_wire``; each body recorded, redacted). A missing
        or extra entry, a body that is not a success, an id that differs from its body:
        ``_Malformed``. So is a result that Python's ``plan_slices`` and ``SliceProgress``
        could never build:

        - the plan: ``requested_quantity`` the ``quantity`` the harness sent, and
          ``slice_quantities`` positive integers that add up to it;
        - the order budget (``guard.spend(case, max_slices)`` reserved that many orders): no more
          slices than the ``max_slices`` sent (``DEFAULT_MAX_SLICES`` when none was), none above
          the ``max_slice_quantity`` sent when one was, and the shape ``plan_slices`` builds
          (all equal except a smaller last one);
        - the accounting (``SliceProgress.result``): ``placed_quantity`` the placed slices'
          sum, ``unsent_quantity`` requested minus placed, ``complete`` exactly when every
          slice has an id, no id twice;
        - a returned result (``done`` / ``stopped``): complete with no ``error``, or
          incomplete naming its ``error``, which is never an ``OrderStatusUnknownError``
          (Python raises that one, with the partial result);
        - ``partial``, the result inside an error reply (``unknown``): never complete (the
          slice with the unknown outcome has no id) and no ``error`` of its own."""
        if not isinstance(data, dict):
            raise _Malformed("returned a sliced result that is not an object")
        ints = {}
        for key in ("requested_quantity", "placed_quantity", "unsent_quantity"):
            value = _int_or_none(data.get(key))
            if value is None:
                raise _Malformed(f"returned a sliced result without {key}")
            ints[key] = value
        slices, ids = data.get("slice_quantities"), data.get("order_ids")
        if not isinstance(slices, list):
            raise _Malformed("returned a sliced result without slice_quantities")
        if not slices or not all((_int_or_none(q) or 0) >= 1 for q in slices):
            raise _Malformed(
                "returned a sliced result whose slice_quantities are not positive integers"
            )
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise _Malformed("returned a sliced result without order_ids")
        if not isinstance(data.get("complete"), bool):
            raise _Malformed("returned a sliced result without complete")
        sent = _int_or_none(summary.get("args", {}).get("quantity"))
        if sent is None or ints["requested_quantity"] != sent:
            raise _Malformed(
                "returned a sliced result whose requested_quantity is not the quantity sent"
            )
        if sum(slices) != ints["requested_quantity"]:
            raise _Malformed(
                "returned a sliced result whose slices do not add up to its requested_quantity"
            )
        # The order budget: the harness reserved ``max_slices`` orders for the case, so a driver
        # may never plan more slices than it was sent, a slice above the cap it was sent, or a
        # shape ``plan_slices`` never builds (full slices, then a smaller remainder).
        sent_args = summary.get("args", {})
        max_slices = _int_or_none(sent_args.get("max_slices"))
        if len(slices) > (max_slices if max_slices is not None else DEFAULT_MAX_SLICES):
            raise _Malformed("returned a sliced result with more slices than max_slices")
        cap = _int_or_none(sent_args.get("max_slice_quantity"))
        if cap is not None and max(slices) > cap:
            raise _Malformed("returned a sliced result with a slice larger than max_slice_quantity")
        if any(q != slices[0] for q in slices[:-1]) or slices[-1] > slices[0]:
            raise _Malformed(
                "returned a sliced result whose slices are not the plan's shape (all equal "
                "except a smaller last one)"
            )
        if len(ids) > len(slices) or ints["placed_quantity"] != sum(slices[: len(ids)]):
            raise _Malformed("returned a sliced result whose placed_quantity is not its slices'")
        if ints["unsent_quantity"] != ints["requested_quantity"] - ints["placed_quantity"]:
            raise _Malformed("returned a sliced result whose unsent_quantity is not the rest")
        if data["complete"] != (len(ids) == len(slices)):
            raise _Malformed("returned a sliced result whose complete does not match its ids")
        if partial:
            if data["complete"]:
                raise _Malformed("returned a partial sliced result that is complete")
            if data.get("error") is not None:
                raise _Malformed("returned a partial sliced result with an error of its own")
        elif data["complete"] and data.get("error") is not None:
            raise _Malformed("returned a complete sliced result with an error")
        elif not data["complete"] and data.get("error") is None:
            raise _Malformed("returned an incomplete sliced result without its error")
        entries = data.get("slices")
        if not isinstance(entries, list) or len(entries) != len(ids):
            raise _Malformed("returned a sliced result without one wire body per placed slice")
        wire_ids: list[str] = []
        for claimed, entry in zip(ids, entries, strict=True):
            if (
                not isinstance(entry, dict)
                or entry.get("raw") is None
                or not isinstance(entry.get("order_id"), str)
            ):
                raise _Malformed("returned a slice without its order_id and raw wire body")
            status = _status(entry)
            if self._responses is not None:
                body = _redact_raw(entry["raw"])  # recorded redacted; parsed on the original
                self._responses.record("PlaceOrder", status, body, request=summary)
            wire = self._from_wire("place_order", {}, entry["order_id"], entry["raw"], status)
            if claimed != wire:
                self._finding("place_sliced_order", "order_ids", claimed, wire)
                raise _Malformed(
                    f"listed the slice id {claimed!r}, but its wire body says {wire!r}"
                )
            wire_ids.append(wire)
        if len(set(wire_ids)) != len(wire_ids):
            raise _Malformed("returned a sliced result that lists one order id twice")
        error: TradeSmartError | None = None
        if data.get("error") is not None:
            raised = self._error("place_order", data["error"], summary)
            if not isinstance(raised, TradeSmartError):
                raise _Malformed("returned a sliced result whose error is not an SDK error")
            if isinstance(raised, OrderStatusUnknownError):  # raised by Python, never returned
                raise _Malformed("returned an unknown outcome as its error in a sliced result")
            error = raised
        return SlicedOrderResult(
            requested_quantity=ints["requested_quantity"],
            slice_quantities=tuple(slices),
            order_ids=tuple(wire_ids),
            placed_quantity=ints["placed_quantity"],
            unsent_quantity=ints["unsent_quantity"],
            complete=bool(data["complete"]),
            error=error,
        )

    def _from_wire(
        self, method: str, args: Mapping[str, Any], result: str, raw: Any, status: int | None
    ) -> str:
        """The write's id from its **wire body**: ``raw`` parsed and classified by the Python
        SDK's own parser for the endpoint, exactly as the Python client would (the id the
        ledger records as ours, the one teardown cancels). The driver's ``result`` is only
        compared: a different value is a finding, and then the outcome is unknown, as it is
        when Python's parse refuses the body or does not call it a success (``_Malformed``)."""
        spec = _SPECS[method]
        try:
            data = parse_response(
                endpoint=spec.name,
                status_code=status if status is not None else 200,
                text=json.dumps(raw),
                expect=spec.expect,
                allow_empty=spec.allow_empty,
                state_changing=spec.state_changing,
                success_stats=spec.success_stats,
                requires_ack_id=spec.requires_ack_id,
                stats_ignore_case=spec.stats_ignore_case,
                failure_stats=spec.failure_stats,
            )
            parsed = spec.parse(data)
        except Exception as exc:  # Python's parse refuses what the driver called a success
            self._finding(method, "result", result, f"<{type(exc).__name__}>")
            raise _Malformed(
                f"answered {method} as a success, but its wire body is not one for the Python "
                f"SDK ({type(exc).__name__})"
            ) from None
        wire: Any
        if method == "place_order":
            wire = parsed.order_id
        elif method == "modify_order":
            wire = modify_ack_order_id(parsed, str(args.get("order_id")))
        elif method in ("cancel_order", "exit_order"):
            wire = str(args.get("order_id"))
        else:
            wire = parsed
        if not isinstance(wire, str) or not wire.strip():
            raise _Malformed(f"answered {method} with a wire body that carries no id")
        if wire != result:
            self._finding(method, "result", result, wire)
            raise _Malformed(
                f"returned the id {result!r} for {method}, but its wire body says {wire!r}"
            )
        return wire

    @staticmethod
    def _wire_body_is_success(method: str, raw: Any, status: int | None) -> bool:
        """Whether the Python SDK's parser for ``method`` reads ``raw`` as a success. A method
        with no single-body spec (a sliced place) and a body Python refuses are not."""
        spec = _SPECS.get(method)
        if spec is None:
            return False
        try:
            data = parse_response(
                endpoint=spec.name,
                status_code=status if status is not None else 200,
                text=raw if isinstance(raw, str) else json.dumps(raw),
                expect=spec.expect,
                allow_empty=spec.allow_empty,
                state_changing=spec.state_changing,
                success_stats=spec.success_stats,
                requires_ack_id=spec.requires_ack_id,
                stats_ignore_case=spec.stats_ignore_case,
                failure_stats=spec.failure_stats,
            )
            spec.parse(data)
        except Exception:  # Python reads it as a failure (or cannot read it): not a success
            return False
        return True

    def _finding(self, method: str, field: str, driver: Any, python: Any) -> None:
        if isinstance(driver, str):  # a value the driver wrote: redacted like its stderr
            driver = self._redact(driver)
        if isinstance(python, str):  # parsed from the driver's body: redacted the same way
            python = self._redact(python)
        finding = {"method": method, "field": field, "driver": driver, "python": python}
        self.findings.append(finding)
        self._record("driver_finding", **finding)

    # -- the eight state-changing calls --------------------------------------------------------

    def place_order(self, **kwargs: Any) -> str:
        return str(self._call("place_order", dict(kwargs)))

    def place_sliced_order(self, **kwargs: Any) -> SlicedOrderResult:
        result = self._call("place_sliced_order", dict(kwargs))
        if not isinstance(result, SlicedOrderResult):  # pragma: no cover - _answer builds it
            raise TypeError("place_sliced_order returned no SlicedOrderResult")
        return result

    def modify_order(self, order_id: str, **kwargs: Any) -> Any:
        return self._call("modify_order", {"order_id": order_id, **kwargs})

    def cancel_order(self, order_id: str) -> str:
        return str(self._call("cancel_order", {"order_id": order_id}))

    def exit_order(self, order_id: str, product: Any) -> str:
        return str(self._call("exit_order", {"order_id": order_id, "product": product}))

    def place_gtt(self, **kwargs: Any) -> str:
        return str(self._call("place_gtt", dict(kwargs)))

    def modify_gtt(self, alert_id: str, **kwargs: Any) -> Any:
        return self._call("modify_gtt", {"alert_id": alert_id, **kwargs})

    def cancel_gtt(self, alert_id: str) -> str:
        return str(self._call("cancel_gtt", {"alert_id": alert_id}))

    def place_oco(self, **kwargs: Any) -> str:
        return str(self._call("place_oco", dict(kwargs)))

    def modify_oco(self, alert_id: str, **kwargs: Any) -> Any:
        return self._call("modify_oco", {"alert_id": alert_id, **kwargs})

    def cancel_oco(self, alert_id: str) -> str:
        return str(self._call("cancel_oco", {"alert_id": alert_id}))

    def close(self) -> None:
        """End of file on stdin, a moment to exit, then the whole process tree is killed."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self.process.poll() is None:
                with contextlib.suppress(OSError, ValueError):
                    if self.process.stdin is not None:
                        self.process.stdin.close()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.process.wait(CLOSE_WAIT)
            self._kill()
            self._report_stderr("closed")
