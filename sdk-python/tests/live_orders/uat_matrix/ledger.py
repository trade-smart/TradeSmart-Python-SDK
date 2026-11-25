"""The run ledger of the UAT matrix and the open-order invariant.

``.live-results/uat-matrix/<run-id>/ledger.jsonl`` (local, never public): one JSON object per
line, written and flushed to disk **before** every state-changing call (``intent``, with a unique
``call`` id) and **after** it (``outcome``, echoing that ``call`` id). It is how a run (and a
resumed run, and the cleanup tool) knows which orders are its own: every order of the run carries
a unique tag ``sdkuat-<run>-<sdk><key>-<n>``, where ``sdk`` is the one-letter code of the SDK under
test (``p`` Python, ``j`` JavaScript) and ``key`` is a short counter for the case id (``case`` lines
map key -> case id), so tags stay short (at most ``TAG_MAX`` characters). Tags written before the
SDK code existed (``sdkuat-<run>-<key>-<n>``) are still read: the key is the part after the code.

What makes an order **ours**: only a place / place_sliced intent (its tag) and that call's outcome
or a lookup by that tag (the order id). A modify, cancel or exit intent never adopts an id or a tag.

GTTs (family M13): a ``gtt_place`` intent's tag is a **GTT tag** of this run (never a place tag:
an order carrying it is not ours by that tag), and only that call's outcome or a lookup by that
tag in ``pending_gtts()`` / ``triggered_gtts()`` (``gtt_adopt``) makes an **alert id** ours. An
order whose ``add_ord_id`` (``Order.parent_id``) is one of our alert ids, the order a GTT of ours
placed, is ours (``adopt`` writes the link). ``gtt_modify`` / ``gtt_cancel`` intents never adopt
an alert id; one ``gtt_cancel`` per alert id per run; an unknown GTT outcome flags the GTT's tag
and alert id (no further GTT call for it), and a ``gtt_place`` intent without its outcome on
reopening blocks its case like a place intent. OCOs (``oco_place`` / ``oco_modify`` /
``oco_cancel``) are tracked exactly like GTTs (same tags, alert ids, flags, one-cancel rule).

What makes an order **unknown** (no further state-changing call from this run, the teardown
cancel included): an outcome ``CHECK THE ACCOUNT`` or ``interrupted``, and, when a ledger is
reopened, every intent without its outcome (the process stopped mid-call). A case with a place
intent from an earlier process but no verdict is not placed again (``unfinished``).

Account ids never reach the file: ``client_id``, ``user_id``, ``account_id``, ``uid`` and ``actid``
are dropped at any depth (with the secret keys of ``safety.scrub``), and any text containing a
``forbidden`` value (the session's client id) is replaced, except in the structural fields
(``STRUCTURAL_KEYS``: tags, ids, case ids), which the ledger itself needs intact.

``check_open_orders`` is the invariant checked before each batch: every non-final order in the
account is this run's (by order id or tag) or ignored (stuck on UAT, F26); anything else stops
the run (``StopRun``) and is never touched.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import secrets
import string
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.live_orders.gtt import carries_tag
from tests.live_orders.safety import CHECK_THE_ACCOUNT, is_final, scrub, status_of
from tests.live_orders.uat_matrix.guard import Guard, StopRun
from tradesmart import TradeSmart
from tradesmart.enums import TransactionType
from tradesmart.models.orders import Order

TAG_PREFIX = "sdkuat-"
TAG_MAX = 30
LEDGER_NAME = "ledger.jsonl"
REDACTED_KEYS = frozenset({"client_id", "user_id", "account_id", "uid", "actid"})
STRUCTURAL_KEYS = frozenset(
    {"tag", "order_id", "order_ids", "case_id", "key", "call", "alert_id", "alert_ids"}
)
REDACTED = "[redacted]"
INTERRUPTED = "interrupted"
NOT_SENT = "not sent"  # refused client-side: nothing went out
REJECTED = "rejected"  # a definitive refusal (calls.REJECTED)
UNKNOWN_VERDICTS = frozenset({CHECK_THE_ACCOUNT, INTERRUPTED})
PLACE_KINDS = frozenset({"place", "place_sliced"})
GTT_PLACE, GTT_MODIFY, GTT_CANCEL = "gtt_place", "gtt_modify", "gtt_cancel"
OCO_PLACE, OCO_MODIFY, OCO_CANCEL = "oco_place", "oco_modify", "oco_cancel"
OCO_KINDS = frozenset({OCO_PLACE, OCO_MODIFY, OCO_CANCEL})
# An OCO is tracked like a GTT: its tag, alert id, one cancel per alert id (the same sets).
GTT_KINDS = frozenset({GTT_PLACE, GTT_MODIFY, GTT_CANCEL}) | OCO_KINDS
_ALERT_PLACES = frozenset({GTT_PLACE, OCO_PLACE})
_ALERT_CANCELS = frozenset({GTT_CANCEL, OCO_CANCEL})
_STARTS_A_CASE = PLACE_KINDS | _ALERT_PLACES  # a call that may create something for its case
_BASE36 = string.digits + string.ascii_lowercase  # case counters: 000, 001, ... zzz
_KEY_WIDTH = 3
_SDK_CODE = re.compile(r"[a-z]")  # one lower-case letter (``sdk_under_test.SDK_CODES``)


def _case_key(segment: str) -> str:
    """The case key of a tag's middle segment: ``<sdk><key>`` (new) or ``<key>`` (old tags)."""
    return segment[1:] if len(segment) == _KEY_WIDTH + 1 else segment


_MIN_FORBIDDEN = 4  # shorter values could match ordinary text
READ_ATTEMPTS = 3  # read-only order-book reads (a read may be repeated; a write never)
READ_INTERVAL = 1.0


CLIENT_RATE_LIMIT = "client-side rate limit"  # the SDK's own limiter: nothing was sent


def nothing_sent(
    verdict: Any,
    error: Any,
    *,
    kind: Any,
    order_id: Any = None,
    order_ids: Iterable[Any] = (),
) -> bool:
    """Whether a place outcome means **nothing** went out, so a resume may place the case.

    Only when every one of these holds: no order id is known (a sliced order with one slice
    placed was sent); the verdict is not unknown (CHECK THE ACCOUNT, interrupted); and either
    the verdict is ``not sent`` (refused client-side), or it is ``rejected`` by the SDK's own
    rate limit: a ``RateLimitError`` with no server answer (``http_status`` recorded as
    ``None``) and the SDK's client-side wording, on a ``place`` / ``place_sliced`` /
    ``gtt_place`` call (for a GTT, ``order_id`` is the alert id). A server 429 never counts,
    whatever its text. A driver's error carries ``sent`` (``live-driver.md``): it counts only
    as a ``RateLimitError`` with ``sent`` false, whatever its text or status.
    """
    if verdict in UNKNOWN_VERDICTS or order_id or any(i for i in order_ids or ()):
        return False
    if kind not in _STARTS_A_CASE:
        return False
    if verdict == NOT_SENT:
        return True
    if verdict != REJECTED or not isinstance(error, Mapping):
        return False
    if "sent" in error:  # a driver's error: its explicit field and the type, never the message
        return error.get("exception") == "RateLimitError" and error["sent"] is False
    return (
        error.get("exception") == "RateLimitError"
        and "http_status" in error
        and error["http_status"] is None
        and CLIENT_RATE_LIMIT in str(error.get("message", ""))
    )


def _word(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().upper()


def _side(value: Any) -> str:
    word = _word(value)
    try:
        return TransactionType(word).value
    except ValueError:
        return word


def _gtt_shape(plan: Mapping[str, Any]) -> tuple[str, str, str]:
    """(exchange, trading symbol, side) of a GTT's order, from its ``place_gtt`` plan."""
    return (
        _word(plan.get("exchange")),
        _word(plan.get("tradingsymbol")),
        _side(plan.get("transaction_type")),
    )


def _order_shape(row: Order) -> tuple[str, str, str]:
    return _word(row.exchange), _word(row.tradingsymbol), _side(row.transaction_type)


def redact(value: Any, forbidden: Iterable[str] = ()) -> Any:
    """Drop account-id and secret keys at any depth; replace text naming a forbidden value
    (never in the structural fields: tags, order ids, case ids, keys, call ids)."""
    words = [w.upper() for w in forbidden if w and len(w) >= _MIN_FORBIDDEN]

    def walk(item: Any, protected: bool) -> Any:
        if isinstance(item, Mapping):
            return {
                str(k): walk(v, str(k) in STRUCTURAL_KEYS)
                for k, v in item.items()
                if str(k).lower() not in REDACTED_KEYS
            }
        if isinstance(item, (list, tuple)):
            return [walk(v, protected) for v in item]
        if isinstance(item, str) and not protected and any(w in item.upper() for w in words):
            return REDACTED
        return item

    return walk(scrub(value), False)


def _key(number: int) -> str:
    digits = ""
    for _ in range(_KEY_WIDTH):
        number, rest = divmod(number, len(_BASE36))
        digits = _BASE36[rest] + digits
    if number:
        raise ValueError("too many cases for one run")
    return digits


class Ledger:
    """The run ledger: appends records, replays them on open (resume)."""

    def __init__(
        self, path: Path, run_id: str, *, forbidden: Iterable[str] = (), sdk_code: str = "p"
    ) -> None:
        if not isinstance(sdk_code, str) or not _SDK_CODE.fullmatch(sdk_code):
            raise ValueError(
                f"sdk_code must be exactly one lower-case letter (p, j, ...), got {sdk_code!r}"
            )
        self.path = path
        self.run_id = run_id
        self.sdk_code = sdk_code  # one letter in every new tag (``p`` Python, ``j`` JavaScript)
        self.forbidden = tuple(forbidden)
        self._lock = threading.RLock()
        self._keys: dict[str, str] = {}  # case id -> key
        self._cases: dict[str, str] = {}  # key -> case id
        self._place_tags: dict[str, dict[str, Any]] = {}  # tag -> {case_id, segment}
        self._order_tag: dict[str, str] = {}  # order id -> tag (our orders)
        self._pending: dict[str, dict[str, Any]] = {}  # call id -> intent without outcome
        self._flagged: set[str] = set()  # order ids with an unknown outcome
        self._flagged_tags: set[str] = set()
        self._verdicts: dict[str, str] = {}
        self._started_before: set[str] = set()  # cases with a place intent at open
        self._places_before: dict[str, int] = {}  # case id -> place intents at open
        self._unsent_before: dict[str, int] = {}  # case id -> of those, never sent
        self._places: dict[str, int] = {}  # key -> highest n issued
        self._issued: set[str] = set()  # tags issued by new_tag in this process, not yet sent
        self._consumed: dict[str, str] = {}  # tag -> order id: used by a modify's tag change
        self._cancel_sent: set[str] = set()  # order ids with a cancel intent (one per order)
        self._stuck: set[str] = set()  # order ids noted stuck (not final after the cancel)
        self._gtt_tags: dict[str, dict[str, Any]] = {}  # GTT tag -> {case_id, segment}
        self._alert_tag: dict[str, str] = {}  # alert id -> GTT tag (our GTTs)
        self._flagged_alerts: set[str] = set()  # alert ids with an unknown outcome
        self._gtt_cancel_sent: set[str] = set()  # alert ids with a gtt_cancel intent
        self._oco_tags: set[str] = set()  # of the GTT tags, those of an OCO (oco_place)
        self._call_prefix = secrets.token_hex(3)
        self._calls = itertools.count(1)
        self._loading = False
        self._load()

    # -- reading ----------------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        self._loading = True
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # a torn last line (the process died while writing)
                if isinstance(record, dict):
                    self._apply(record)
        finally:
            self._loading = False
        # An intent without its outcome: the earlier process stopped mid-call. Unknown.
        for intent in self._pending.values():
            self._flag(intent.get("tag"), [intent.get("order_id")], [intent.get("alert_id")])
        self._pending.clear()

    def _flag(self, tag: Any, ids: Iterable[Any], alerts: Iterable[Any] = ()) -> None:
        self._flagged.update(str(i) for i in ids if i)
        self._flagged_alerts.update(str(a) for a in alerts if a)
        if tag:
            self._flagged_tags.add(str(tag))

    def _apply_gtt_intent(self, record: Mapping[str, Any]) -> None:
        kind, tag = record.get("kind"), str(record.get("tag"))
        if kind in _ALERT_PLACES:
            if kind == OCO_PLACE:
                self._oco_tags.add(tag)
            detail = record.get("detail")
            plan = detail.get("plan") if isinstance(detail, Mapping) else None
            self._gtt_tags.setdefault(
                tag,
                {
                    "case_id": record.get("case_id"),
                    "segment": record.get("segment"),
                    "shape": _gtt_shape(plan) if isinstance(plan, Mapping) else None,
                },
            )
            self._note_tag_number(tag)
            if self._loading and record.get("case_id"):
                case_id = str(record["case_id"])
                self._started_before.add(case_id)
                self._places_before[case_id] = self._places_before.get(case_id, 0) + 1
        elif kind in _ALERT_CANCELS and record.get("alert_id"):
            self._gtt_cancel_sent.add(str(record["alert_id"]))

    def _apply_gtt_outcome(
        self, record: Mapping[str, Any], intent: Mapping[str, Any] | None, kind: Any
    ) -> None:
        tag = str(record.get("tag"))
        alerts = [record.get("alert_id"), *(record.get("alert_ids") or [])]
        target = (intent or {}).get("alert_id")
        if kind in _ALERT_PLACES and tag in self._gtt_tags:
            for alert_id in alerts:
                if alert_id:
                    self._alert_tag[str(alert_id)] = tag
        if kind in _ALERT_CANCELS and record.get("verdict") == NOT_SENT and target:
            self._gtt_cancel_sent.discard(str(target))  # nothing went out: it keeps its cancel
        if record.get("verdict") in UNKNOWN_VERDICTS:
            self._flag(tag, [], [*alerts, target])

    def _apply(self, record: Mapping[str, Any]) -> None:
        kind = record.get("type")
        if kind == "case":
            key, case_id = str(record["key"]), str(record["case_id"])
            self._keys[case_id] = key
            self._cases[key] = case_id
        elif kind == "intent":
            self._pending[str(record.get("call"))] = dict(record)
            if record.get("kind") in PLACE_KINDS:
                tag = str(record.get("tag"))
                self._place_tags.setdefault(
                    tag, {"case_id": record.get("case_id"), "segment": record.get("segment")}
                )
                self._note_tag_number(tag)
                if self._loading and record.get("case_id"):
                    case_id = str(record["case_id"])
                    self._started_before.add(case_id)
                    self._places_before[case_id] = self._places_before.get(case_id, 0) + 1
            elif record.get("kind") == "modify":
                detail = record.get("detail") or {}
                changes = detail.get("changes") if isinstance(detail, Mapping) else None
                new = changes.get("tag") if isinstance(changes, Mapping) else None
                if new:  # used up by this modify, acknowledged or not: never placed again
                    self._consumed[str(new)] = str(record.get("order_id"))
                    self._issued.discard(str(new))
            elif record.get("kind") == "cancel" and record.get("order_id"):
                self._cancel_sent.add(str(record["order_id"]))
            elif record.get("kind") in GTT_KINDS:
                self._apply_gtt_intent(record)
        elif kind == "stuck" and record.get("order_id"):
            self._stuck.add(str(record["order_id"]))
        elif kind == "outcome":
            intent = self._pending.pop(str(record.get("call")), None)
            call_kind = intent.get("kind") if intent is not None else record.get("kind")
            if call_kind in GTT_KINDS:
                if (
                    self._loading
                    and intent is not None
                    and call_kind in _ALERT_PLACES
                    and intent.get("case_id")
                    and nothing_sent(
                        record.get("verdict"),
                        (record.get("detail") or {}).get("error"),
                        kind=call_kind,
                        order_id=record.get("alert_id"),
                        order_ids=record.get("alert_ids") or (),
                    )
                ):
                    case_id = str(intent["case_id"])
                    self._unsent_before[case_id] = self._unsent_before.get(case_id, 0) + 1
                self._apply_gtt_outcome(record, intent, call_kind)
                return
            ids = [record.get("order_id"), *(record.get("order_ids") or [])]
            tag = str(record.get("tag"))
            if call_kind in PLACE_KINDS and tag in self._place_tags:
                for order_id in ids:
                    if order_id:
                        self._order_tag[str(order_id)] = tag
            if (
                self._loading
                and intent is not None
                and call_kind in PLACE_KINDS
                and intent.get("case_id")
                and nothing_sent(
                    record.get("verdict"),
                    (record.get("detail") or {}).get("error"),
                    kind=call_kind,
                    order_id=record.get("order_id"),
                    order_ids=record.get("order_ids") or (),
                )
            ):
                case_id = str(intent["case_id"])
                self._unsent_before[case_id] = self._unsent_before.get(case_id, 0) + 1
            if call_kind == "cancel" and record.get("verdict") == NOT_SENT:
                # Nothing went out: the order keeps its one CancelOrder.
                target = (intent or {}).get("order_id") or record.get("order_id")
                self._cancel_sent.discard(str(target))
            if record.get("verdict") in UNKNOWN_VERDICTS:
                self._flag(tag, ids)
        elif kind in ("adopt", "retag"):
            order_id, tag = str(record["order_id"]), str(record["tag"])
            if kind == "retag":
                self._place_tags.setdefault(
                    tag, {"case_id": record.get("case_id"), "segment": record.get("segment")}
                )
            alert_id = record.get("alert_id")
            if tag in self._place_tags or (
                alert_id and self._alert_tag.get(str(alert_id)) == tag
            ):  # a lookup by our place tag, or the order one of our GTTs placed
                self._order_tag[order_id] = tag
        elif kind == "gtt_adopt":
            alert_id, tag = str(record["alert_id"]), str(record["tag"])
            if tag in self._gtt_tags:
                self._alert_tag[alert_id] = tag
        elif kind == "verdict":
            self._verdicts[str(record["case_id"])] = str(record["verdict"])

    def _note_tag_number(self, tag: str) -> None:
        prefix = f"{TAG_PREFIX}{self.run_id}-"
        if not tag.startswith(prefix):
            return
        segment, _, number = tag[len(prefix) :].partition("-")
        key = _case_key(segment)
        if number.isdigit():
            self._places[key] = max(self._places.get(key, 0), int(number))

    def ours(self) -> set[str]:
        """Order ids of this run (from its place calls and lookups by its tags only)."""
        with self._lock:
            return set(self._order_tag)

    def tags(self) -> set[str]:
        """Tags of this run's place calls (whether or not an order id came back)."""
        with self._lock:
            return set(self._place_tags)

    def tag_of(self, order_id: str) -> str | None:
        """The tag of one of our orders (never a caller's)."""
        return self._order_tag.get(order_id)

    def flagged(self) -> set[str]:
        """Order ids with an unknown outcome: no further state-changing call."""
        with self._lock:
            return set(self._flagged)

    def tag_flagged(self, tag: str) -> bool:
        return tag in self._flagged_tags

    def flagged_tags(self) -> set[str]:
        """Tags with an unknown outcome (an interrupted place may have no order id)."""
        with self._lock:
            return set(self._flagged_tags)

    def cancel_sent(self, order_id: str) -> bool:
        """Whether this run used the order's one CancelOrder: a cancel intent whose call was
        sent, is in flight, or has an unknown outcome (a NOT_SENT outcome releases it)."""
        with self._lock:
            return order_id in self._cancel_sent

    def stuck(self, order_id: str) -> bool:
        """Whether the order was noted stuck (still open after its cancel)."""
        with self._lock:
            return order_id in self._stuck

    def case_of(self, tag: str) -> str | None:
        entry = self._place_tags.get(tag) or self._gtt_tags.get(tag)
        if entry is not None and entry.get("case_id"):
            return str(entry["case_id"])
        prefix = f"{TAG_PREFIX}{self.run_id}-"
        if tag.startswith(prefix):
            return self._cases.get(_case_key(tag[len(prefix) :].partition("-")[0]))
        return None

    def segment_of(self, tag: str) -> str | None:
        entry = self._place_tags.get(tag) or self._gtt_tags.get(tag)
        return None if entry is None or not entry.get("segment") else str(entry["segment"])

    # -- GTTs -------------------------------------------------------------------------------

    def gtt_tags(self) -> set[str]:
        """Tags of this run's GTT place calls (whether or not an alert id came back)."""
        with self._lock:
            return set(self._gtt_tags)

    def oco_tags(self) -> set[str]:
        """Tags of this run's OCO place calls (a subset of ``gtt_tags``; a copy)."""
        with self._lock:
            return set(self._oco_tags)

    def alerts(self) -> set[str]:
        """Alert ids of this run's GTTs (from its GTT place calls and lookups by its tags)."""
        with self._lock:
            return set(self._alert_tag)

    def alert_tag(self, alert_id: str) -> str | None:
        """The GTT tag of one of our alert ids (never a caller's)."""
        with self._lock:
            return self._alert_tag.get(alert_id)

    def gtt_order_tag(self, row: Order) -> str | None:
        """The GTT tag when ``row`` is the order one of our GTTs placed, else ``None``: its
        ``add_ord_id`` (``parent_id``) is one of our alert ids, its own tag is blank, that
        GTT's tag, or carries that tag as an exact token (``gtt.carries_tag``: UAT writes
        ``<ai_t>: <tag>: <text>: <tag>``), and its exchange, trading symbol and side are
        those of that GTT's place plan (``add_ord_id`` alone is not enough: it also carries
        iceberg ids).

        Accepted risk: the server reusing one of our alert ids within the run for another GTT
        of the same instrument and side whose order has a blank tag would make that order look
        ours (alert ids have been unique per GTT so far; a run lasts one trading day)."""
        parent = (row.parent_id or "").strip()
        with self._lock:
            tag = self._alert_tag.get(parent) if parent else None
            if tag is None:
                return None
            own = (row.tag or "").strip()
            if own and not carries_tag(own, tag):
                return None
            shape = self._gtt_tags.get(tag, {}).get("shape")
            if shape is None or tuple(shape) != _order_shape(row):
                return None
            return tag

    def alert_is_oco(self, alert_id: str) -> bool:
        """Whether one of our alert ids was placed by an ``oco_place`` call (it is cancelled
        with ``oco_cancel``, never ``gtt_cancel``)."""
        with self._lock:
            return self._alert_tag.get(alert_id, "") in self._oco_tags

    def alert_flagged(self, alert_id: str) -> bool:
        """Whether a GTT call for the alert id had an unknown outcome (no further GTT call)."""
        with self._lock:
            return alert_id in self._flagged_alerts or (
                self._alert_tag.get(alert_id, "") in self._flagged_tags
            )

    def flagged_alerts(self) -> set[str]:
        with self._lock:
            return set(self._flagged_alerts)

    def gtt_cancel_sent(self, alert_id: str) -> bool:
        """Whether this run used the alert's one CancelGTTOrder (a NOT_SENT outcome releases it)."""
        with self._lock:
            return alert_id in self._gtt_cancel_sent

    def adopt_gtt(self, alert_id: str, tag: str) -> bool:
        """Make ``alert_id`` ours when a lookup found it under one of this run's GTT tags;
        ``True`` when it is ours (already, or now), else nothing changes. Refused while a call
        of that tag has no outcome yet (``pending_tag``)."""
        alert_id, tag = alert_id.strip(), tag.strip()
        with self._lock:
            if not alert_id:
                return False
            if alert_id in self._alert_tag:
                return True
            if tag not in self._gtt_tags or self.pending_tag(tag):
                return False
            self._write({"type": "gtt_adopt", "alert_id": alert_id, "tag": tag})
            return True

    def done(self, case_id: str) -> str | None:
        """The case's final verdict in this run (resume skips it), else ``None``."""
        return self._verdicts.get(case_id)

    def pending_place(self, case_id: str) -> bool:
        """Whether a place call of the case may have gone out with no known result: this
        process wrote its intent and no outcome yet, or its outcome is unknown (CHECK THE
        ACCOUNT, interrupted: e.g. an exception between the intent and the outcome)."""
        with self._lock:
            if any(
                i.get("kind") in PLACE_KINDS and i.get("case_id") == case_id
                for i in self._pending.values()
            ):
                return True
            return any(
                tag in self._flagged_tags
                for tag, entry in self._place_tags.items()
                if entry.get("case_id") == case_id
            )

    def mark_unknown(self, tag: str, order_ids: Iterable[str | None] = ()) -> None:
        """Flag ``tag`` and ``order_ids`` as unknown in memory, when the ledger could not write
        the outcome that would flag them (no further state-changing call in this process)."""
        with self._lock:
            self._flag(tag, order_ids)

    def call_pending(self, call: str) -> bool:
        """Whether this process wrote the intent of ``call`` and not yet its outcome."""
        with self._lock:
            return call in self._pending

    def pending_tag(self, tag: str, *, kinds: Iterable[str] | None = None) -> bool:
        """Whether a call of this process for ``tag`` (of ``kinds``, default any) is in flight:
        its intent is written, its outcome not yet."""
        wanted = None if kinds is None else frozenset(kinds)
        with self._lock:
            return any(
                i.get("tag") == tag and (wanted is None or i.get("kind") in wanted)
                for i in self._pending.values()
            )

    def pending_order(self, order_id: str) -> bool:
        """Whether a call of this process for one of our orders is in flight: a modify / cancel
        / exit intent naming it, or the place intent of its tag, without an outcome yet."""
        with self._lock:
            tag = self._order_tag.get(order_id)
            return any(
                i.get("order_id") == order_id
                or (tag is not None and i.get("tag") == tag and i.get("kind") in PLACE_KINDS)
                for i in self._pending.values()
            )

    def unfinished(self, case_id: str) -> bool:
        """An earlier process placed for the case and wrote no verdict: never placed again.
        A case whose every earlier place call was refused before sending (``nothing_sent``:
        client-side validation or the SDK's own rate limit) is not unfinished: nothing went
        out, so a resume may place it."""
        if case_id not in self._started_before or case_id in self._verdicts:
            return False
        return self._places_before.get(case_id, 0) > self._unsent_before.get(case_id, 0)

    def sent(self, tag: str) -> bool:
        """Whether ``tag`` was used: a place or GTT place intent, or a modify's tag change (sent
        once, ever)."""
        return tag in self._place_tags or tag in self._consumed or tag in self._gtt_tags

    def issued(self, tag: str) -> bool:
        return tag in self._issued

    # -- writing ----------------------------------------------------------------------------

    def _write(self, record: dict[str, Any]) -> None:
        data = redact(record, self.forbidden)
        data["at"] = datetime.now(UTC).isoformat()
        line = json.dumps(data, default=str, sort_keys=True) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            self._apply(data)

    def case_key(self, case_id: str) -> str:
        """The case's short key (a counter), recorded on first use."""
        with self._lock:
            key = self._keys.get(case_id)
            if key is None:
                key = _key(len(self._keys) + 1)
                self._write({"type": "case", "case_id": case_id, "key": key})
            return key

    def new_tag(self, case_id: str) -> str:
        """A tag never issued in this run: ``sdkuat-<run>-<sdk><key>-<n>``."""
        with self._lock:
            key = self.case_key(case_id)
            number = self._places.get(key, 0) + 1
            self._places[key] = number
            tag = f"{TAG_PREFIX}{self.run_id}-{self.sdk_code}{key}-{number}"
            if len(tag) > TAG_MAX:
                raise ValueError(f"tag {tag} is longer than {TAG_MAX} characters")
            self._issued.add(tag)
            return tag

    def intent(
        self,
        tag: str,
        case_id: str,
        kind: str,
        *,
        segment: str | None = None,
        order_id: str | None = None,
        alert_id: str | None = None,
        **detail: Any,
    ) -> str:
        """Written (and flushed) before the call; returns the call id its outcome echoes."""
        call = f"{self._call_prefix}-{next(self._calls)}"
        record: dict[str, Any] = {
            "type": "intent",
            "call": call,
            "tag": tag,
            "case_id": case_id,
            "kind": kind,
            "segment": segment,
        }
        if order_id is not None:
            record["order_id"] = order_id
        if alert_id is not None:
            record["alert_id"] = alert_id
        if detail:
            record["detail"] = detail
        self._write(record)
        self._issued.discard(tag)
        return call

    def outcome(
        self,
        tag: str,
        order_id: str | None,
        verdict: str,
        *,
        kind: str | None = None,
        call: str | None = None,
        order_ids: Iterable[str] = (),
        alert_id: str | None = None,
        **detail: Any,
    ) -> None:
        """Written (and flushed) after the call (and after any lookup by tag). Without
        ``call`` it answers the latest intent of ``tag`` still waiting for its outcome."""
        with self._lock:
            if call is None:
                waiting = [c for c, i in self._pending.items() if i.get("tag") == tag]
                call = waiting[-1] if waiting else None
            record: dict[str, Any] = {
                "type": "outcome",
                "call": call,
                "tag": tag,
                "kind": kind,
                "order_id": order_id,
                "verdict": verdict,
            }
            ids = list(order_ids)
            if ids:
                record["order_ids"] = ids
            if alert_id is not None:
                record["alert_id"] = alert_id
            if detail:
                record["detail"] = detail
            self._write(record)

    def adopt(self, row: Order) -> bool:
        """Make the book ``row`` ours when its own tag is one of this run's place tags (a lookup
        by our tag), or when its ``add_ord_id`` (``parent_id``) is one of this run's alert ids
        and it is the order a GTT of ours placed (``gtt_order_tag``: it takes that GTT's tag);
        ``True`` when the order is ours (already, or now), else nothing changes."""
        order_id = (row.order_id or "").strip()
        tag = (row.tag or "").strip()
        parent = (row.parent_id or "").strip()
        with self._lock:
            if not order_id:
                return False
            if order_id in self._order_tag:
                return True
            if tag and tag in self._place_tags:
                self._write({"type": "adopt", "order_id": order_id, "tag": tag})
                return True
            gtt_tag = self.gtt_order_tag(row)
            if gtt_tag is None:
                return False
            self._write({"type": "adopt", "order_id": order_id, "tag": gtt_tag, "alert_id": parent})
            return True

    def retag(self, order_id: str, tag: str) -> None:
        """One of our orders now carries ``tag`` (a modify of it changed the tag). Refuses
        (``ValueError``, nothing written) an order that is not ours, or a tag this run did not
        issue for it (``new_tag``, then used by that order's modify)."""
        with self._lock:
            old = self._order_tag.get(order_id)
            if old is None:
                raise ValueError(f"order {order_id} is not an order of this run; no retag")
            if tag not in self._issued and self._consumed.get(tag) != order_id:
                raise ValueError(f"tag {tag} was not issued by this run for order {order_id}")
            entry = self._place_tags.get(old, {})
            self._write(
                {
                    "type": "retag",
                    "order_id": order_id,
                    "tag": tag,
                    "case_id": entry.get("case_id"),
                    "segment": entry.get("segment"),
                }
            )
            self._issued.discard(tag)

    def verdict(self, case_id: str, verdict: str, **detail: Any) -> None:
        """The case's final verdict (a resumed run skips the case)."""
        self._write({"type": "verdict", "case_id": case_id, "verdict": verdict, **detail})

    def note(self, kind: str, **detail: Any) -> None:
        """Anything else worth keeping (a stuck order, a stop, a trip, the teardown)."""
        self._write({"type": kind, **detail})


def read_book(
    client: TradeSmart,
    *,
    attempts: int = READ_ATTEMPTS,
    interval: float = READ_INTERVAL,
    sleep: Callable[[float], None] | None = None,
) -> list[Order] | None:
    """``orders()``, read up to ``attempts`` times (read-only); ``None`` when unreadable."""
    wait = sleep if sleep is not None else time.sleep
    for attempt in range(attempts):
        try:
            return client.orders()
        except Exception:  # a read-only call; the next read decides
            if attempt + 1 < attempts:
                wait(interval)
    return None


def is_ours(order: Order, ledger: Ledger) -> bool:
    """Ours by order id, by one of our place tags, or as the order a GTT of ours placed
    (``Ledger.gtt_order_tag``: our alert id in ``add_ord_id`` and that GTT's shape)."""
    order_id = (order.order_id or "").strip()
    tag = (order.tag or "").strip()
    return (
        (bool(order_id) and order_id in ledger.ours())
        or (bool(tag) and tag in ledger.tags())
        or ledger.gtt_order_tag(order) is not None
    )


def check_open_orders(
    client: TradeSmart,
    ledger: Ledger,
    ignore: frozenset[str],
    *,
    guard: Guard | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Every non-final order in the account is ours or ignored, else ``StopRun``.

    With ``guard``, its open set follows the book (an order acknowledged a moment ago and not
    in the book yet is kept; see ``Guard.sync_open``). An unreadable book stops the run too
    (the invariant cannot be confirmed). Nothing is cancelled here.
    """
    book = read_book(client, sleep=sleep)
    if book is None:
        raise StopRun(
            f"{CHECK_THE_ACCOUNT}: the order book could not be read, so the open-order "
            "invariant cannot be confirmed; stopping the run"
        )
    open_rows = [o for o in book if not is_final(status_of(o))]
    ours = [o for o in open_rows if is_ours(o, ledger)]
    foreign = [
        str(o.order_id)
        for o in open_rows
        if not is_ours(o, ledger) and (o.order_id or "").strip() not in ignore
    ]
    if guard is not None:
        guard.sync_open(
            ((o.order_id or "").strip() for o in ours),
            seen={(o.order_id or "").strip() for o in book},
        )
    if foreign:
        raise StopRun(
            f"{CHECK_THE_ACCOUNT}: open order(s) {', '.join(foreign)} not in this run's ledger "
            "(someone else is trading, or a leak); stopping the run (they are never touched)",
            foreign=foreign,
        )
