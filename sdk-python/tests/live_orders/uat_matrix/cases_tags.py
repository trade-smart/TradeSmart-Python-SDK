"""The UAT matrix family M11: tags and encoding (inventory §3.2, marker ``m_tags``).

- **``&`` in the trading symbol.** Far orders on NSE ``M&M-EQ`` (the docs: "use url encoding to
  avoid special char error for symbols like M&M"): each rests, and a hook checks
  that the trading symbol and the tag (``remarks``) come back unchanged in the batch's book row
  and in ``order_history`` (read-only); the stream is recorded by the engine when a watcher is
  attached. A changed symbol, or a tag changed other than cut short, fails the case; a tag cut
  short or dropped is recorded (``judged``). The hook records the sent and echoed tag lengths
  (``round_trip`` in the case record): the lower bound of the ``remarks`` length UAT keeps.
- **Tags unique over the whole run** (``tag_problems``): every place call of the run carries its
  own ``sdkuat-<run>-<key>-<n>`` tag of at most 30 characters.
- **Tags the run cannot send** (the longest tag UAT returns unchanged, spaces, ``&``,
  non-ASCII) are not probed: ``calls.place`` sets every tag itself and ``calls.modify`` accepts
  only a tag the ledger issued (``ledger.new_tag``, the fixed short scheme), so a caller cannot
  send a probe value; the M&M cases record the echoed length of the run's own tags instead
  (probes need an engine hook first).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from tests.live_orders.uat_matrix import batch
from tests.live_orders.uat_matrix.batch import BatchItem, CaseResult, Placed
from tests.live_orders.uat_matrix.cases import FULL, REST, SMOKE, TRIGGER_PENDING, Case
from tests.live_orders.uat_matrix.ledger import PLACE_KINDS, TAG_MAX, TAG_PREFIX

FAMILY = "tags"
ROLE = "nse-e"


def _case(product: str, prctyp: str, side: str, *, tier: str = FULL) -> Case:
    return Case(
        id=f"{FAMILY}-{ROLE}-{product}-{prctyp}-day-{side}".lower(),
        family=FAMILY,
        role=ROLE,
        product=product,
        prctyp=prctyp,
        ret="DAY",
        side=side,
        orders=1,
        outcome=REST if prctyp == "LMT" else TRIGGER_PENDING,
        tier=tier,
    )


TAG_CASES: list[Case] = [
    _case("MIS", "LMT", "BUY", tier=SMOKE),
    _case("MIS", "LMT", "SELL"),
    _case("MIS", "SL-LMT", "BUY"),
    _case("CNC", "LMT", "BUY"),
]


TRUNCATED, DROPPED = "truncated", "dropped"  # a tag cut short / not returned: recorded


def remarks_finding(sent: str, echoed: str | None) -> str | None:
    """How an echoed tag differs from the sent one: ``None`` (unchanged), ``DROPPED`` (absent
    or blank), ``TRUNCATED`` (a shorter prefix of it) or ``"changed"`` (anything else)."""
    if echoed == sent:
        return None
    if echoed is None or not echoed.strip():
        return DROPPED
    if len(echoed) < len(sent) and sent.startswith(echoed):
        return TRUNCATED
    return "changed"


def round_trip(placed: Placed) -> None:
    """The trading symbol and the tag come back in the book row and the history (read-only).

    A changed trading symbol, or a tag changed in another way than cut short or dropped, raises
    ``AssertionError`` (the case fails). A tag cut short or dropped is noted
    (``round_trip.remarks_finding``) and ``judged`` records the case: the ``remarks`` length UAT
    keeps is an open question.
    """
    order_id = placed.order_id or ""
    sent_tsym = str(placed.plan["tradingsymbol"])
    sent_tag = placed.outcome.tag
    row = placed.row
    trip: dict[str, Any] = {
        "tsym_sent": sent_tsym,
        "tsym_book": None if row is None else row.tradingsymbol,
        "remarks_sent": sent_tag,
        "remarks_book": None if row is None else row.tag,
        "remarks_sent_length": len(sent_tag),
        "remarks_echoed_length": None if row is None or row.tag is None else len(row.tag),
    }
    try:
        history = placed.run.session.client.order_history(order_id)
    except Exception as exc:  # a read: recorded, the book still decides
        trip["history_error"] = f"{type(exc).__name__}: {exc}"
        history = []
    entry = history[0] if history else None  # newest first
    trip["tsym_history"] = None if entry is None else entry.tradingsymbol
    trip["remarks_history"] = None if entry is None else entry.tag
    watcher = placed.run.watcher
    updates = [] if watcher is None else watcher.updates(order_id)
    if updates:
        trip["tsym_stream"] = updates[-1].tradingsymbol
        trip["remarks_stream"] = updates[-1].raw.get("remarks")
    views = ["book"] + ([] if entry is None else ["history"])
    problems = [
        f"tsym_{view} {trip[f'tsym_{view}']!r} != sent {sent_tsym!r}"
        for view in views
        if trip[f"tsym_{view}"] != sent_tsym
    ]
    findings = {view: remarks_finding(sent_tag, trip[f"remarks_{view}"]) for view in views}
    problems += [
        f"remarks_{view} {trip[f'remarks_{view}']!r} changed from sent {sent_tag!r}"
        for view, finding in findings.items()
        if finding == "changed"
    ]
    recorded = [f for f in findings.values() if f in (TRUNCATED, DROPPED)]
    if recorded:
        trip["remarks_finding"] = recorded[0]
    placed.notes["round_trip"] = trip
    if problems:
        raise AssertionError("; ".join(problems))


def judged(result: CaseResult) -> CaseResult:
    """The case's result with a tag cut short or dropped recorded (``batch.RECORD``) instead
    of passed; anything else unchanged. (The engine's record line and ledger verdict keep
    ``pass``; both are final, and the record's ``round_trip`` carries the finding.)"""
    trip = (result.record.get("notes") or {}).get("round_trip") or {}
    finding = trip.get("remarks_finding")
    if result.verdict != batch.PASS or finding is None:
        return result
    reason = (
        f"recorded: the tag came back {finding} (echoed length "
        f"{trip.get('remarks_echoed_length') or 0} of {trip.get('remarks_sent_length')}); "
        f"the remarks length UAT keeps is an open question; {result.reason}"
    )
    return dataclasses.replace(result, verdict=batch.RECORD, reason=reason)


def items(cases: Iterable[Case]) -> list[BatchItem]:
    """The batch items of ``cases`` (each checked by ``round_trip`` once it rests)."""
    return [BatchItem(case, after_settle=round_trip) for case in cases]


def tag_problems(ledger_path: Path, run_id: str) -> list[str]:
    """What is wrong with the run's place tags in its ledger: a tag used by two place calls,
    longer than ``TAG_MAX``, or not of this run (``sdkuat-<run>-``); empty when all is well,
    and when the ledger does not exist yet (it is written on its first line: nothing placed)."""
    if not ledger_path.exists():
        return []
    prefix = f"{TAG_PREFIX}{run_id}-"
    seen: set[str] = set()
    problems: list[str] = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a torn last line
        if record.get("type") != "intent" or record.get("kind") not in PLACE_KINDS:
            continue
        tag = str(record.get("tag") or "")
        if tag in seen:
            problems.append(f"tag {tag} was sent twice")
        seen.add(tag)
        if len(tag) > TAG_MAX:
            problems.append(f"tag {tag} is longer than {TAG_MAX} characters")
        if not tag.startswith(prefix):
            problems.append(f"tag {tag} is not of run {run_id}")
    return problems
