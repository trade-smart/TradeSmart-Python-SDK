"""The UAT matrix gate: opt-ins, tiers, the run id, resume, and the run's session.

- UAT mode only (``safety.uat_mode``: ``TRADESMART_ENV=uat`` and a REST host that contains
  ``uat`` and is not, nor contains, the production host), with the existing order opt-ins
  (``TRADESMART_LIVE_TESTS=1``, ``TRADESMART_LIVE_ORDER_TESTS=1``, a serial run);
- or, with the mock-session switch (``mock_session``: ``TRADESMART_LIVE_MOCK_SESSION`` today's
  IST date on a weekend or listed holiday, the typed ``TRADESMART_LIVE_MOCK_CONFIRM`` phrase, the
  production host exactly, ``TRADESMART_ENV`` unset or ``production``), mock-session mode on the
  production server (``MatrixSession.mock``, never ``uat``); any part of the switch present makes
  the run a mock-session run or refuses it (never a UAT run); without it, production is refused;
- refused whenever ``TRADESMART_LIVE_ORDER_FUNDED`` is set, to any value (``"0"`` and ``""`` too);
- the matrix runs only when selected explicitly (``explicitly_selected``): ``-m`` names
  ``uat_matrix`` positively (``-m "live_orders and uat_matrix"``, not under ``not``), or a path
  argument points inside ``tests/live_orders/uat_matrix``; otherwise its items are deselected,
  so ``pytest tests/live_orders -m live_orders`` runs the older suites only;
- tiers: ``smoke`` runs with those opt-ins; ``full`` and ``stress`` also need
  ``TRADESMART_LIVE_ORDER_UAT_MATRIX=1``; ``stress`` only when selected with ``-m stress``;
- a run lives in ``.live-results/uat-matrix/<run-id>/`` (local, never public);
  ``TRADESMART_UAT_RUN_ID`` resumes a run of the **same trading day** (IST) only, refused
  otherwise before any API call; a mock-session run (``run.json``: ``environment``
  ``production-mock`` and ``mock_session``, its switch date) resumes only in mock mode with the
  same switch date, and a UAT run never in mock mode.
- one process per run id: ``<run>/run.lock`` (``O_EXCL``, ``acquire_run_lock``) is created before
  the ledger is read (a resume: before any API call) and removed at the session end; a second
  process is refused; a stale lock (older than 12 h, the ledger untouched for 10 min) is taken
  over only with ``TRADESMART_UAT_RUN_LOCK_TAKEOVER=1``, otherwise it is deleted by hand.

``matrix_session`` refuses (``safety.Refusal``) before any API call when a gate fails; then it
checks the account (``profile()``), the limits preflight (``safety.limits_preflight``:
``margins()`` cash, turnover limit and pending-order value limit at the code minimum, UAT
``UAT_MIN_LIMIT``, mock session ``MOCK_MIN_LIMIT``; else refused before any order, the values
kept in ``run.json`` and the ledger as ``uat_preflight`` / ``mock_preflight``) and the open-order
invariant (``orders()``, ``StopRun``).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from tests._environment import environment_config, environment_name
from tests.live_orders import mock_session, safety
from tests.live_orders.safety import Refusal, ResponseLog
from tests.live_orders.uat_matrix import calls
from tests.live_orders.uat_matrix.guard import Guard, StopRun
from tests.live_orders.uat_matrix.ledger import LEDGER_NAME, Ledger, check_open_orders
from tests.live_orders.uat_matrix.order_pacer import OrderPacer
from tests.live_orders.uat_matrix.quote_pacer import QuotePacer
from tradesmart import TradeSmart
from tradesmart.models.base import IST

MATRIX_ENV = "TRADESMART_LIVE_ORDER_UAT_MATRIX"
RUN_ID_ENV = "TRADESMART_UAT_RUN_ID"
BASE_URL_ENV = "TRADESMART_ENV_BASE_URL"  # the UAT REST base URL (tests/_environment.py)
SMOKE = "smoke"
FULL = "full"
STRESS = "stress"
TIERS = (SMOKE, FULL, STRESS)
RESULTS_ROOT = safety.RESULTS_FILE.parent / "uat-matrix"
STOP_FILE_NAME = "STOP"
RUN_FILE_NAME = "run.json"
_RUN_ID = re.compile(r"[a-z0-9]{4,12}")


# -- gates ------------------------------------------------------------------------------------


def tier_of(marker_names: Iterable[str]) -> str:
    """``stress`` if marked so, else ``smoke`` if marked so, else ``full``."""
    names = set(marker_names)
    if STRESS in names:
        return STRESS
    return SMOKE if SMOKE in names else FULL


_MARK_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def selects_marker(markexpr: str, marker: str) -> bool:
    """Whether the ``-m`` expression names ``marker`` as a token, not right after ``not``."""
    tokens = _MARK_TOKEN.findall(markexpr)
    return any(
        token == marker and (index == 0 or tokens[index - 1] != "not")
        for index, token in enumerate(tokens)
    )


MATRIX_MARKER = "uat_matrix"
MATRIX_DIR = Path(__file__).resolve().parent
_EXPR_TOKEN = re.compile(r"\s*(\(|\)|[^\s()]+)")
_IDENT = re.compile(r"[\w:+\-.\[\]\\/]+")
_MAX_NAMES = 12  # a marker expression with more names is not understood (fails closed)


class _Parse:
    """A tiny parser of pytest ``-m`` expressions (``and`` / ``or`` / ``not`` / parentheses /
    marker names) into a function of the set of markers present; ``ValueError`` otherwise."""

    def __init__(self, text: str) -> None:
        self.tokens: list[str] = []
        position = 0
        text = text.strip()
        while position < len(text):
            match = _EXPR_TOKEN.match(text, position)
            if match is None:
                raise ValueError(text)
            self.tokens.append(match.group(1))
            position = match.end()
        self.at = 0
        self.names: set[str] = set()

    def parse(self) -> Callable[[set[str]], bool]:
        result = self._or()
        if self.at != len(self.tokens):
            raise ValueError("trailing tokens")
        return result

    def _next(self) -> str | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def _or(self) -> Callable[[set[str]], bool]:
        left = self._and()
        while self._next() == "or":
            self.at += 1
            first, second = left, self._and()
            left = lambda present, a=first, b=second: a(present) or b(present)  # noqa: E731
        return left

    def _and(self) -> Callable[[set[str]], bool]:
        left = self._not()
        while self._next() == "and":
            self.at += 1
            first, second = left, self._not()
            left = lambda present, a=first, b=second: a(present) and b(present)  # noqa: E731
        return left

    def _not(self) -> Callable[[set[str]], bool]:
        token = self._next()
        if token == "not":
            self.at += 1
            inner = self._not()
            return lambda present: not inner(present)
        if token == "(":
            self.at += 1
            inner = self._or()
            if self._next() != ")":
                raise ValueError("unbalanced parentheses")
            self.at += 1
            return inner
        if token is None or token in ("and", "or", ")") or not _IDENT.fullmatch(token):
            raise ValueError(f"unexpected {token!r}")
        self.at += 1
        self.names.add(token)
        return lambda present: token in present


def marker_selects(markexpr: str, marker: str) -> bool:
    """Whether the ``-m`` expression selects ``marker`` **positively**: some choice of the other
    markers makes it false without ``marker`` and true with it (``not uat_matrix`` and
    ``not (a or uat_matrix)`` do not; a token, never a substring). An expression this parser
    does not understand selects nothing (fails closed)."""
    try:
        parser = _Parse(markexpr)
        evaluate = parser.parse()
    except ValueError:
        return False
    if marker not in parser.names:
        return False
    others = sorted(parser.names - {marker})
    if len(others) > _MAX_NAMES:
        return False
    for mask in range(2 ** len(others)):
        present = {name for bit, name in enumerate(others) if mask >> bit & 1}
        if not evaluate(present) and evaluate(present | {marker}):
            return True
    return False


def path_selects_matrix(args: Iterable[str], invocation_dir: Path) -> bool:
    """Whether a command-line path argument points inside ``tests/live_orders/uat_matrix`` (the
    directory, a file in it, or a node id of one)."""
    for arg in args:
        path_text = str(arg).split("::", 1)[0]
        if not path_text or path_text.startswith("-"):
            continue
        path = Path(path_text)
        if not path.is_absolute():
            path = Path(invocation_dir) / path
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved == MATRIX_DIR or MATRIX_DIR in resolved.parents:
            return True
    return False


def explicitly_selected(markexpr: str, args: Iterable[str], invocation_dir: Path) -> bool:
    """The matrix runs only when asked for: ``-m`` selects ``uat_matrix`` positively, or a path
    argument points inside the matrix package (owner decision U5 kept: smoke needs no third
    opt-in, only this explicit selection)."""
    return marker_selects(markexpr, MATRIX_MARKER) or path_selects_matrix(args, invocation_dir)


def _tier_refusal(matrix_opt_in: bool, tier: str, markexpr: str) -> str | None:
    if tier not in TIERS:
        return f"unknown UAT matrix tier {tier!r}; refusing to place orders"
    if tier != SMOKE and not matrix_opt_in:
        return (
            f"the {tier} UAT matrix tier needs {MATRIX_ENV}=1 as well (the smoke tier runs "
            "with the UAT order opt-ins alone); skipped"
        )
    if tier == STRESS and not selects_marker(markexpr, STRESS):
        return "the stress tier runs only when selected with -m stress; skipped"
    return None


def today_ist() -> date:
    """Today's date in IST (the mock-session switch is compared with it)."""
    return datetime.now(IST).date()


def gate_refusal(
    environ: Mapping[str, str],
    base_url: str,
    tier: str,
    *,
    markexpr: str = "",
    today: date | None = None,
) -> str | None:
    """``None`` only when every matrix gate holds for ``tier`` against ``base_url``.

    With any part of the mock-session switch present (``mock_session.requested``) the run is a
    mock-session run on the production host (``mock_session.refusal`` against ``today``, the IST
    date; default: now) or refused; never a UAT run. Without it, UAT only (as before)."""
    reason = safety.gate_skip_reason(environ)
    if reason is not None:
        return reason
    if safety.FUNDED_ENV in environ:
        return (
            f"{safety.FUNDED_ENV} is set (to any value): the UAT matrix never runs next to "
            "funded mode; refusing to place orders"
        )
    if mock_session.requested(environ):
        day = today if today is not None else today_ist()
        reason = mock_session.refusal(environ, day, base_url)
        if reason is not None:
            return reason
        return _tier_refusal(environ.get(MATRIX_ENV) == "1", tier, markexpr)
    if safety.is_production_host(base_url) or safety.PRODUCTION_HOST in base_url.lower():
        return "the REST host is the production host: the UAT matrix runs on UAT only; refusing"
    try:
        uat = safety.uat_mode(environ, base_url)
    except OSError as exc:  # an invalid TRADESMART_ENV name
        return f"{exc}; refusing to place orders"
    if not uat:
        name = environ.get("TRADESMART_ENV", "")
        return (
            f"the UAT matrix runs in UAT mode only (TRADESMART_ENV=uat and a REST host that "
            f"contains 'uat' and is not {safety.PRODUCTION_HOST}); TRADESMART_ENV={name!r}; "
            "refusing to place orders"
        )
    return _tier_refusal(environ.get(MATRIX_ENV) == "1", tier, markexpr)


def configured_base_url(environ: Mapping[str, str]) -> str:
    """The REST base URL the client will be built with: in mock mode the selected environment's
    (``environment_config``: production's default or ``TRADESMART_BASE_URL``; ``""`` when it
    cannot be built), else the UAT base URL named in the environment."""
    if mock_session.requested(environ):
        try:
            return environment_config(environ).base_url
        except (OSError, ValueError):
            return ""
    return environ.get(BASE_URL_ENV, "")


def collection_refusal(
    environ: Mapping[str, str], tier: str, *, markexpr: str = "", today: date | None = None
) -> str | None:
    """The gate at collection time, on the base URL the client will be built with
    (``configured_base_url``; ``matrix_session`` checks the client's own URL again)."""
    return gate_refusal(environ, configured_base_url(environ), tier, markexpr=markexpr, today=today)


# The families never run in a mock session (AMO M6, GTT M13: stored past the session).
MOCK_SKIPPED_MARKERS = frozenset({"m_amo", "m_gtt"})


def mock_family_refusal(environ: Mapping[str, str], marker_names: Iterable[str]) -> str | None:
    """At collection: ``mock_session.NO_AMO_GTT`` for an AMO or GTT item while any part of the
    mock-session switch is present (the run is then a mock run or refused), else ``None``."""
    if not mock_session.requested(environ):
        return None
    if MOCK_SKIPPED_MARKERS.isdisjoint(marker_names):
        return None
    return mock_session.NO_AMO_GTT


def is_matrix_item(nodeid: str) -> bool:
    """Whether the item's *file* is in the ``tests/live_orders/uat_matrix`` package."""
    path = nodeid.split("::", 1)[0].replace("\\", "/")
    return "/tests/live_orders/uat_matrix/" in "/" + path


# -- run id and resume ------------------------------------------------------------------------


def trading_day(now: datetime) -> date:
    """The IST date of ``now`` (a resumed run must be on the same one)."""
    return now.astimezone(IST).date()


def new_run_id(now: datetime) -> str:
    """``MMDD`` (IST) and 4 random hex digits: 8 characters, short enough for the tags."""
    return f"{now.astimezone(IST):%m%d}{secrets.token_hex(2)}"


def run_mock_date(data: Mapping[str, object]) -> str | None:
    """The switch date of a run file's mock-session run (``environment`` ``production-mock``
    and its ``mock_session`` date), ``""`` for a mock run without a date, else ``None``."""
    mock = data.get("environment") == mock_session.ENVIRONMENT or "mock_session" in data
    if not mock:
        return None
    return str(data.get("mock_session") or "")


def _resume(root: Path, run_id: str, day: date, mock_date: date | None = None) -> None:
    if not _RUN_ID.fullmatch(run_id):
        raise Refusal(
            f"{RUN_ID_ENV} must be 4-12 lower-case letters or digits (a run id of this tool); "
            "refusing"
        )
    run_file = root / run_id / RUN_FILE_NAME
    try:
        data = json.loads(run_file.read_text(encoding="utf-8"))
        recorded = str(data["trading_day"])
        recorded_mock = run_mock_date(data)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise Refusal(
            f"{RUN_ID_ENV}={run_id}: no readable run of that id to resume; refusing"
        ) from exc
    if recorded != day.isoformat():
        raise Refusal(
            f"{RUN_ID_ENV}={run_id}: that run was on {recorded}; a run resumes on the same "
            f"trading day only (today, IST: {day.isoformat()}); refusing"
        )
    wanted = None if mock_date is None else mock_date.isoformat()
    if recorded_mock != wanted:
        was = "not a mock-session run"
        if recorded_mock is not None:
            was = f"a mock-session run of {recorded_mock or '(no date)'}"
        now = "not in mock mode" if wanted is None else f"in mock mode for {wanted}"
        raise Refusal(
            f"{RUN_ID_ENV}={run_id}: that run was {was} and this one is {now}; a mock-session "
            "run resumes only with the same mock switch date; refusing"
        )


LOCK_NAME = "run.lock"
LOCK_TAKEOVER_ENV = "TRADESMART_UAT_RUN_LOCK_TAKEOVER"
LOCK_STALE_SECONDS = 12 * 3600  # a lock this old may be taken over (with the opt-in)
LEDGER_QUIET_SECONDS = 10 * 60  # ... when the run's ledger was not written for this long


def _lock_is_stale(lock: Path, ledger: Path, now: float) -> bool:
    try:
        if now - lock.stat().st_mtime < LOCK_STALE_SECONDS:
            return False
        return not ledger.exists() or now - ledger.stat().st_mtime >= LEDGER_QUIET_SECONDS
    except OSError:
        return False


def acquire_run_lock(run_dir: Path, environ: Mapping[str, str]) -> Path:
    """Create ``<run>/run.lock`` (``O_EXCL``): one process per run id. Held by another process:
    ``Refusal``, unless the lock is stale (older than 12 h, the ledger untouched for 10 min)
    **and** ``TRADESMART_UAT_RUN_LOCK_TAKEOVER=1`` is set; then it is taken over."""
    lock = run_dir / LOCK_NAME
    body = json.dumps({"pid": os.getpid(), "at": datetime.now(UTC).isoformat()})
    for attempt in (1, 2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            stale = _lock_is_stale(lock, run_dir / LEDGER_NAME, time.time())
            if attempt == 1 and stale and environ.get(LOCK_TAKEOVER_ENV) == "1":
                lock.unlink(missing_ok=True)
                continue
            how = (
                f"it is stale: set {LOCK_TAKEOVER_ENV}=1 to take it over"
                if stale
                else (
                    f"if no process runs it (killed hard), check the account, then delete the "
                    f"lock by hand, or set {LOCK_TAKEOVER_ENV}=1 once it is older than 12 h"
                )
            )
            raise Refusal(
                f"run {run_dir.name} is in use by another process ({lock} exists); one process "
                f"per run id; {how}"
            ) from None
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        return lock
    raise Refusal(f"could not take the run lock {lock}; refusing")  # pragma: no cover


def release_run_lock(session: MatrixSession) -> None:
    """Remove the session's run lock (the session's end)."""
    if session.lock is not None:
        session.lock.unlink(missing_ok=True)
        session.lock = None


def _new_run(root: Path, now: datetime) -> str:
    for _ in range(20):
        run_id = new_run_id(now)
        if not (root / run_id).exists():
            return run_id
    raise Refusal("could not pick a new run id; refusing")


# -- the session ------------------------------------------------------------------------------


@dataclass
class MatrixSession:
    """Per-run state of the UAT matrix (one pytest process)."""

    client: TradeSmart
    run_id: str
    trading_day: date
    run_dir: Path
    ledger: Ledger
    guard: Guard
    ignored_open: frozenset[str] = frozenset()
    matrix_opt_in: bool = False
    resumed: bool = False
    responses: ResponseLog = field(default_factory=ResponseLog)
    raw_bodies: bool = False
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    lookup_attempts: int = 3  # order-book reads after an ambiguous call (read-only)
    lookup_interval: float = 2.0
    confirm_seconds: float = 15.0  # how long a cancelled batch is given to become final
    poll_interval: float = 1.0
    interrupted: bool = False  # Ctrl-C seen: the teardown cancels nothing (cleanup tool)
    lock: Path | None = None  # this process's run lock (``acquire_run_lock``)
    # UAT order rules (``safety.uat_mode`` of the environment and the client's URL; always True
    # for a session from ``matrix_session``): on UAT the F&O / MCX lot comes from the master.
    uat: bool = False
    # Mock-session mode (``mock_session``; never with ``uat``): the production server during an
    # exchange mock session, the switch date and the switch's variables (re-checked before every
    # state-changing call, ``mock_refusal``, against ``today``: the IST date now).
    mock: bool = False
    mock_date: date | None = None
    mock_environ: Mapping[str, str] = field(default_factory=dict)
    today: Callable[[], date] = today_ist
    # Every state-changing call of the run (place, modify, cancel, exit, GTT writes): 8/s.
    order_pacer: OrderPacer = field(init=False, repr=False)
    # Every quote read of the run on UAT (110 a minute, 8 a second; ``uat_quote_pacer``).
    quote_pacer: QuotePacer = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.mock and self.uat:
            raise Refusal("mock-session mode and UAT mode are never both on; refusing")
        self.order_pacer = OrderPacer(clock=self.clock, sleep=self.sleep)
        self.quote_pacer = QuotePacer(clock=self.clock, sleep=self.sleep)

    def mock_refusal(self) -> str | None:
        """In mock mode, why no new order may be placed or modified now: the switch checked
        again (``mock_session.refusal``) against today's IST date (``today``) and the client's
        REST URL, and the date must still be the session's own. ``None`` outside mock mode and
        while every check holds. Cancels never ask (teardown clears ours after a rollover)."""
        if not self.mock:
            return None
        day = self.today()
        reason = mock_session.refusal(self.mock_environ, day, self.client.config.base_url)
        if reason is not None:
            return reason
        if self.mock_date is None or day != self.mock_date:
            return (
                f"mock session: the run's switch date is {self.mock_date}, today (IST) is "
                f"{day.isoformat()}; refusing to place orders"
            )
        return None

    def record_fields(self) -> dict[str, str]:
        """The fields every record of a mock-session run carries (``{}`` otherwise)."""
        if not self.mock or self.mock_date is None:
            return {}
        return {"environment": mock_session.ENVIRONMENT, "mock_session": self.mock_date.isoformat()}

    def uat_quote_pacer(self) -> QuotePacer | None:
        """The session's quote pacer on UAT (GetQuotes: 120 a minute, 10 a second), else
        ``None`` (production: no quote pacer)."""
        return self.quote_pacer if self.uat else None

    def item_refusal(self, tier: str, *, markexpr: str = "") -> str | None:
        """Why an item of ``tier`` must not run now: the run is stopped, or its tier gate."""
        if self.guard.stopped():
            return self.guard.stop_reason
        return _tier_refusal(self.matrix_opt_in, tier, markexpr)

    def case_done(self, case_id: str) -> str | None:
        """Why a (resumed) run must not place for the case again: it has a final verdict in the
        ledger, or an earlier process placed for it and stopped before its verdict."""
        verdict = self.ledger.done(case_id)
        if verdict is not None:
            return f"done in run {self.run_id} ({verdict})"
        if self.ledger.unfinished(case_id):
            return (
                f"started in run {self.run_id} without a verdict (an earlier process stopped "
                "mid-case); not placed again: CHECK THE ACCOUNT for its orders"
            )
        return None


def matrix_session(
    environ: Mapping[str, str],
    client: TradeSmart,
    *,
    root: Path = RESULTS_ROOT,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> MatrixSession:
    """Open (or resume) the run; refuses before any API call when a gate fails.

    The run is a UAT run (``uat``) or, with the mock-session switch, a mock-session run on the
    production host (``mock``); the switch is checked against the IST date of ``now``."""
    base_url = client.config.base_url
    now = now if now is not None else datetime.now(IST)
    day = trading_day(now)
    reason = gate_refusal(environ, base_url, SMOKE, today=day)
    if reason is not None:
        raise Refusal(reason)
    mock_date = _mock_date(environ, day, base_url)
    ignored = safety.ignored_open_orders(environ, base_url, mock=mock_date is not None)
    resumed = RUN_ID_ENV in environ
    lock: Path | None = None
    if resumed:
        run_id = environ[RUN_ID_ENV].strip()
        _resume(root, run_id, day, mock_date)
        lock = acquire_run_lock(root / run_id, environ)  # before any API call
    else:
        run_id = _new_run(root, now)
    try:
        return _open_session(environ, client, root, run_id, day, resumed, ignored, lock,
                             mock_date=mock_date, sleep=sleep, clock=clock)  # fmt: skip
    except BaseException:
        if lock is not None:
            lock.unlink(missing_ok=True)
        raise


def _mock_date(environ: Mapping[str, str], day: date, base_url: str) -> date | None:
    """The switch date in mock mode (every check passed for ``day``), else ``None``; a switch
    that is present but fails is refused (``gate_refusal`` refused it already: belt)."""
    if not mock_session.requested(environ):
        return None
    reason = mock_session.refusal(environ, day, base_url)
    if reason is not None:
        raise Refusal(reason)
    return mock_session.switch_date(environ)


def _open_session(
    environ: Mapping[str, str],
    client: TradeSmart,
    root: Path,
    run_id: str,
    day: date,
    resumed: bool,
    ignored: frozenset[str],
    lock: Path | None,
    *,
    mock_date: date | None,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> MatrixSession:
    mock = mock_date is not None
    uat = safety.uat_mode(environ, client.config.base_url)
    if mock == uat:  # exactly one of the two (``gate_refusal``); checked anyway
        raise Refusal("the UAT matrix runs in UAT mode or in mock-session mode only; refusing")
    # From here on API calls are allowed (read-only).
    refusal = safety.account_refusal(
        client.profile(), environ.get(safety.ACCOUNT_ENV), client.client_id
    )
    if refusal is not None:
        raise Refusal(refusal)
    # The limits preflight (owner, 2026-11-13): before any order of the run, read-only; refused
    # before the run folder is created. UAT: ``UAT_MIN_LIMIT``; mock session: ``MOCK_MIN_LIMIT``.
    if mock:
        preflight = safety.limits_preflight(
            client, safety.MOCK_MIN_LIMIT, "mock-session", ask=mock_session.LIMITS_ASK
        )
        preflight_key = "mock_preflight"
    else:
        preflight = safety.uat_preflight(client)
        preflight_key = "uat_preflight"
    if preflight.refusal is not None:
        raise Refusal(preflight.refusal)
    fields: dict[str, str] = {}
    if mock_date is not None:
        fields = {"environment": mock_session.ENVIRONMENT, "mock_session": mock_date.isoformat()}
    run_dir = root / run_id
    if not resumed:
        run_dir.mkdir(parents=True, exist_ok=False)
        run: dict[str, object] = {
            "run_id": run_id,
            "trading_day": day.isoformat(),
            "created_at": datetime.now(UTC).isoformat(),
            "environment": environment_name(environ),
            **fields,
        }
        run[preflight_key] = preflight.record()
        (run_dir / RUN_FILE_NAME).write_text(json.dumps(run, sort_keys=True), encoding="utf-8")
    if lock is None:
        lock = acquire_run_lock(run_dir, environ)
    forbidden = [client.client_id or "", environ.get(safety.ACCOUNT_ENV, "")]
    session = MatrixSession(
        client=client,
        run_id=run_id,
        trading_day=day,
        run_dir=run_dir,
        ledger=Ledger(run_dir / LEDGER_NAME, run_id, forbidden=forbidden),
        guard=Guard(root / STOP_FILE_NAME, clock=clock),
        ignored_open=ignored,
        matrix_opt_in=environ.get(MATRIX_ENV) == "1",
        resumed=resumed,
        uat=uat,
        mock=mock,
        mock_date=mock_date,
        mock_environ=mock_session.snapshot(environ) if mock else {},
        sleep=sleep,
        clock=clock,
        lock=lock,
    )
    try:
        session.ledger.note(preflight_key, **preflight.record())
        if fields:
            session.ledger.note("mock_session", **fields)
        try:
            check_open_orders(client, session.ledger, ignored, guard=session.guard, sleep=sleep)
        except StopRun as stop:
            # A resumed run may have open orders of its own: they are cancelled (once) before
            # the refusal; the foreign ones are never touched.
            calls.stop_run(session, stop)
            raise
    except BaseException:
        release_run_lock(session)  # a refused session never keeps the run
        raise
    return session
