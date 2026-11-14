"""The UAT matrix expectation oracle: what a case should do on this run.

``expectation(case, profile, known_issues)`` combines, in this order:

1. the run's state of the known UAT issues (``KnownIssues``): a segment whose canary stuck
   ``PENDING`` (F26) skips; BSE/BFO cases wait for their canary (U9); a role without a contract
   skips with the reason, keyed by why (``contracts.UNAVAILABLE_KINDS``: no last price, not in
   the master, ...); a family that hit an RMS ``RED:`` rejection skips its remaining cases as an
   environment limit (U3);
2. the account profile (``profile()`` ``exarr`` / ``orarr`` / ``prarr``): an exchange, price type
   or product (per exchange) the account does not enable expects a rejection;
3. F27: MKT and SL-MKT pass when refused with ``ALGO_CHK``; a refusal with another message is
   recorded (``Expectation.judge_rejection``);
4. the documented outcome of the case (``cases``): resting, trigger pending and IOC cancels are
   accepted; documented refusals are rejections; a fill is accepted and its closing SELL is a
   non-strict xfail (known issue ``closing-sell``, U8); undocumented behaviour is recorded
   (U13) until pinned in ``uat_expectations.json``.

Known issues are keyed by their open-item id where the behaviour contract defines one (F26,
F27). ``closing-sell`` is open item F31 of the behaviour contract (a closing SELL at the last
price does not fill on UAT); the code keeps the key ``closing-sell`` (records and ledgers
already carry it), and F31 maps to it.

``uat_expectations.json`` is pinned from the first full run (U13); a recorded fill case
(``RECORDED_FILL_SUFFIXES``) is never pinned ``accept`` (``load_pinned`` refuses it).

The oracle does not depend on the environment: a mock-session run (``session.mock``, the
production server) is judged by the same rules, which are production's (F27: MKT and SL-MKT
refused with ``ALGO_CHK`` are expected); the UAT-only recording rules live in the engine and
apply on UAT only (``batch``).

Pure data and functions: the batch engine (Task 3) applies the verdicts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.live_orders.uat_matrix import cases as case_table
from tests.live_orders.uat_matrix import contracts as contract_table
from tests.live_orders.uat_matrix.cases import Case
from tests.live_orders.uat_matrix.contracts import Contracts, Unavailable
from tradesmart.enums import Product
from tradesmart.models.user import UserDetails

ACCEPT, REJECT, SKIP, XFAIL, RECORD = "accept", "reject", "skip", "xfail", "record"
# How a refusal compares with the expectation (``Expectation.judge_rejection``).
EXPECTED, UNEXPECTED = "expected", "unexpected"
EXPECTATIONS_FILE = Path(__file__).resolve().parent / "uat_expectations.json"
EXPECTATIONS_VERSION = 1
PINNABLE = frozenset({ACCEPT, REJECT})
# Cases whose verdict is a recorded fill (the partial-fill probe and its close, the CO/BO and
# holdings ``-fill`` cases): never pinned ``accept`` (U13), a fill depends on the moment's market.
RECORDED_FILL_SUFFIXES = ("-fill", "-partial", "-partial-close")
CANARY_SEGMENTS = frozenset({"bse", "bfo"})  # U9: one canary first (F26 seen on BSE)
ALGO_CHK = "ALGO_CHK"
RMS_MARK = "RED:"


@dataclass(frozen=True)
class KnownIssue:
    key: str
    verdict: str
    description: str


KNOWN_ISSUES: dict[str, KnownIssue] = {
    issue.key: issue
    for issue in (
        KnownIssue(
            "F26",
            SKIP,
            "BSE-side API orders stay PENDING and cannot be cancelled: a canary order first per "
            "segment (BSE, BFO); if it sticks the segment is skipped for the run",
        ),
        KnownIssue(
            "F27",
            REJECT,
            "MKT and SL-MKT are refused for API orders (ALGO_CHK): passes when refused with "
            "ALGO_CHK; another message is recorded",
        ),
        KnownIssue(
            "closing-sell",
            XFAIL,
            "a closing SELL at the last price does not fill on UAT: xfail (non-strict), the "
            "position stays in the run's position ledger",
        ),
        KnownIssue(
            "RMS",
            SKIP,
            "an RMS rejection (RED:) is an environment limit; an account-wide one (turnover, "
            "margin) "
            "skips the family's remaining cases",
        ),
        KnownIssue(
            "record",
            RECORD,
            "undocumented behaviour: assert only SDK invariants and record the answer, until "
            "pinned in uat_expectations.json",
        ),
        *(
            KnownIssue(kind, SKIP, f"no contract for the role today ({description})")
            for kind, description in (
                (contract_table.NO_LAST_PRICE, "no last price after the candidate fallback"),
                (contract_table.NOT_IN_MASTER, "not in the day's script master"),
                (contract_table.MASTER_UNAVAILABLE, "the script master could not be read"),
                (contract_table.LOT_SIZE_MISMATCH, "master and quote lot sizes differ"),
                (contract_table.NO_TICK, "no tick size in the quote or the master"),
                (contract_table.INCOMPLETE_ROW, "the script-master row lacks fields"),
                (contract_table.NO_UNDERLYING, "the option's future has no contract"),
            )
        ),
    )
}


def is_rms_limit(message: str | None) -> bool:
    """True for an RMS refusal (``RED:RULE:{Turnover limit} ...``)."""
    return message is not None and RMS_MARK in message


def is_lpp_refusal(message: str | None) -> bool:
    """True for the exchange's LPP price-band refusal (``17070: ... out of the current LPP
    range.``, seen live on production, 14 Nov 2026)."""
    return message is not None and "LPP range" in message


def is_order_rate_refusal(message: str | None) -> bool:
    """True for the per-minute order-count refusal (``Order Recieved 202 in a current minute
    exceeds Limit 200 for user``, seen live on production, 14 Nov 2026)."""
    return message is not None and "exceeds Limit" in message and "current minute" in message


def environment_refusal(message: str | None) -> str | None:
    """The kind of an environment-limit refusal that is neutral to the segment's circuit
    breaker (``"LPP band"``, ``"order rate"``), else ``None``. Neither adds to nor resets the
    breaker's unexpected-rejection count, and neither skips the rest of the family."""
    if is_lpp_refusal(message):
        return "LPP band"
    if is_order_rate_refusal(message):
        return "order rate"
    return None


# Account-wide RMS limits (full live run, 13 Nov 2026): only these skip a family's remaining cases.
# Every other ``RED:`` refusal (``No Holdings uploaded``, ``{Single Order Value}``, ``{Check
# circuit limit including square off order}``, an ``insufficient volume`` block, anything new) is
# an environment limit of that case only.
ACCOUNT_LIMIT_WORDS = ("{Turnover limit}", "Margin Shortfall")


def is_account_limit(message: str | None) -> bool:
    """True for an account-wide RMS refusal (turnover, margin)."""
    return is_rms_limit(message) and any(w in (message or "") for w in ACCOUNT_LIMIT_WORDS)


# UAT's own price band (seen live, 13 Nov 2026, BFO SENSEX): it does not match its quote, so far
# prices from a fresh quote can be refused. On UAT these refusals are recorded, never counted for
# the segment's circuit breaker (``batch._judge_rejection``).
UAT_BAND_MARKS = ("BEYOND CIRCUIT LIMITS", "STOP PRICE IS NOT REASONABLE")
_BAND = re.compile(
    r"UPPER\s+CIRCUIT\s*=\s*([0-9.]+)\s*AND\s+LOWER\s+CIRCUIT\s*=\s*([0-9.]+)", re.IGNORECASE
)


def is_uat_band_refusal(message: str | None) -> bool:
    """True for UAT's band refusals (letter case ignored)."""
    text = (message or "").upper()
    return any(mark in text for mark in UAT_BAND_MARKS)


# UAT's script data lacks some production contracts (full live run, 13 Nov 2026: SENSEX weekly
# options from the production master): on UAT such a refusal is an environment-data skip.
UAT_INVALID_SYMBOL = "INVALID TRADING SYMBOL"


def is_uat_invalid_symbol(message: str | None) -> bool:
    """True for UAT's ``Invalid Trading Symbol`` refusal (letter case ignored)."""
    return UAT_INVALID_SYMBOL in (message or "").upper()


def band_of(message: str | None) -> dict[str, str | None]:
    """The band a refusal quotes (``upper`` / ``lower``; ``None`` when it quotes none)."""
    found = _BAND.search(message or "")
    return {"upper": found.group(1) if found else None, "lower": found.group(2) if found else None}


# -- the account profile --------------------------------------------------------------------


def _product_name(value: Any) -> str:
    try:
        return Product(value).name
    except ValueError:
        return str(value).strip().upper()


@dataclass(frozen=True)
class Profile:
    """What ``profile()`` enables; ``None``: the account did not say (no prediction)."""

    exchanges: frozenset[str] | None
    price_types: frozenset[str] | None
    products: Mapping[str, frozenset[str]] | None  # product name (CNC, MIS ...) -> exchanges

    @classmethod
    def from_details(cls, details: UserDetails) -> Profile:
        def upper(values: list[str] | None) -> frozenset[str] | None:
            return frozenset(v.strip().upper() for v in values) if values else None

        products: dict[str, frozenset[str]] | None = None
        if details.prarr:
            products = {}
            for info in details.prarr:
                if info.product is None:
                    continue
                name = _product_name(info.product)
                products[name] = products.get(name, frozenset()) | (upper(info.exch) or frozenset())
        return cls(upper(details.exarr), upper(details.orarr), products)

    def product_enabled(self, product: str, exchange: str) -> bool | None:
        if self.products is None:
            return None
        return exchange in self.products.get(product, frozenset())


# -- the run's state of the known issues ----------------------------------------------------


@dataclass
class KnownIssues:
    """Known-issue state of one run; the batch engine reports canaries and RMS refusals."""

    pinned: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    unavailable: dict[str, Unavailable] = field(default_factory=dict)  # role -> kind, reason
    stuck_segments: dict[str, str] = field(default_factory=dict)  # segment -> reason
    stuck_orders: set[str] = field(default_factory=set)
    canary_passed: set[str] = field(default_factory=set)
    rms_families: dict[str, str] = field(default_factory=dict)  # family -> message

    def add_unavailable(self, contracts: Contracts) -> None:
        self.unavailable.update(contracts.unavailable)

    def canary_ok(self, segment: str) -> None:
        self.canary_passed.add(segment)

    def canary_stuck(self, segment: str, order_id: str) -> None:
        self.stuck_orders.add(order_id)
        self.stuck_segments[segment] = (
            f"F26: the {segment.upper()} canary order {order_id} stuck PENDING; "
            f"{segment.upper()} is skipped for the run"
        )

    def rms_limit(self, family: str, message: str) -> None:
        """An RMS refusal in ``family``: an account-wide limit (``is_account_limit``) skips the
        family's remaining cases; any other ``RED:`` refusal is the case's own verdict only."""
        if not is_rms_limit(message):
            raise ValueError(f"not an RMS rejection (no {RMS_MARK}): {message!r}")
        if is_account_limit(message):
            self.rms_families.setdefault(family, message)


# -- the expectation ------------------------------------------------------------------------


@dataclass(frozen=True)
class Expectation:
    verdict: str  # accept | reject | skip | xfail | record
    reason: str
    issue: str | None = None  # the known issue applied (a KNOWN_ISSUES key), or "pinned"
    words: str | None = None  # text a rejection must contain (F27: ALGO_CHK)
    strict: bool = True  # an xfail's strictness (closing-sell: False)
    close: Expectation | None = None  # the closing leg of a fill case
    # The verdict of a refusal without ``words`` (F27: recorded, not unexpected); ``None``: such
    # a refusal is unexpected.
    other_rejection: str | None = None

    def accepts_rejection(self, message: str | None) -> bool:
        """True when a refusal with ``message`` is the expected outcome."""
        if self.verdict != REJECT or message is None:
            return False
        return self.words is None or self.words in message

    def judge_rejection(self, message: str | None) -> str:
        """``EXPECTED`` for the expected refusal, ``other_rejection`` (e.g. ``RECORD``) for a
        refusal with another message, else ``UNEXPECTED``."""
        if self.accepts_rejection(message):
            return EXPECTED
        if self.verdict == REJECT and self.other_rejection is not None:
            return self.other_rejection
        return UNEXPECTED


_CLOSE_NO_FILL = Expectation(
    XFAIL, KNOWN_ISSUES["closing-sell"].description, issue="closing-sell", strict=False
)


def _profile_rejection(case: Case, profile: Profile) -> str | None:
    if profile.exchanges is not None and case.exchange not in profile.exchanges:
        return f"{case.exchange} is not in the account's exarr"
    if profile.price_types is not None and case.prctyp not in profile.price_types:
        return f"{case.prctyp} is not in the account's orarr"
    if profile.product_enabled(case.product, case.exchange) is False:
        return f"{case.product} is not enabled on {case.exchange} in the account's prarr"
    return None


def _pinned(case: Case, issues: KnownIssues, recorded: Expectation) -> Expectation:
    entry = issues.pinned.get(case.id)
    if entry is None:
        return recorded
    return Expectation(
        entry["verdict"],
        f"pinned in {EXPECTATIONS_FILE.name}",
        issue="pinned",
        words=entry.get("words"),
    )


def expectation(case: Case, profile: Profile, known_issues: KnownIssues) -> Expectation:
    """The verdict ``case`` should get on this run (see the module docstring for the order)."""
    issues = known_issues
    stuck = issues.stuck_segments.get(case.segment)
    if stuck is not None:
        return Expectation(SKIP, stuck, issue="F26")
    missing = issues.unavailable.get(case.role)
    if missing is not None:
        return Expectation(SKIP, missing.reason, issue=missing.kind)
    if case.segment in CANARY_SEGMENTS:
        if case.canary:
            return Expectation(RECORD, KNOWN_ISSUES["F26"].description, issue="F26")
        if case.segment not in issues.canary_passed:
            return Expectation(
                SKIP,
                f"F26: the {case.segment.upper()} canary has not been confirmed yet (canary first)",
                issue="F26",
            )
    rms = issues.rms_families.get(case.family)
    if rms is not None:
        return Expectation(
            SKIP, f"environment limit (RMS) in family {case.family}: {rms}", issue="RMS"
        )
    refused = _profile_rejection(case, profile)
    if refused is not None:
        return Expectation(REJECT, refused)
    if case.prctyp in ("MKT", "SL-MKT"):
        return Expectation(
            REJECT,
            KNOWN_ISSUES["F27"].description,
            issue="F27",
            words=ALGO_CHK,
            other_rejection=RECORD,
        )
    outcome = case.outcome
    if outcome in (case_table.REST, case_table.TRIGGER_PENDING, case_table.EXCHANGE_CANCEL):
        return Expectation(ACCEPT, f"documented: {outcome}")
    if outcome == case_table.REJECT:
        return Expectation(REJECT, "documented refusal")
    if outcome == case_table.FILL_CLOSE:
        return Expectation(ACCEPT, "BUY at the last price fills (UAT rule)", close=_CLOSE_NO_FILL)
    recorded = Expectation(RECORD, KNOWN_ISSUES["record"].description, issue="record")
    return _pinned(case, issues, recorded)


# -- uat_expectations.json ------------------------------------------------------------------


def load_pinned(path: Path = EXPECTATIONS_FILE) -> dict[str, dict[str, str]]:
    """The pinned answers by case id (``{"verdict": "accept"|"reject", "words": ...}``)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != EXPECTATIONS_VERSION:
        raise ValueError(f"{path.name}: unknown version (expected {EXPECTATIONS_VERSION})")
    pinned: dict[str, dict[str, str]] = {}
    for case_id, entry in dict(data.get("cases") or {}).items():
        if not isinstance(entry, dict) or entry.get("verdict") not in PINNABLE:
            raise ValueError(f"{path.name}: {case_id}: verdict must be one of {sorted(PINNABLE)}")
        if entry.get("verdict") == ACCEPT and str(case_id).endswith(RECORDED_FILL_SUFFIXES):
            raise ValueError(
                f"{path.name}: {case_id}: a recorded fill case is never pinned 'accept' (whether "
                "it fills depends on the market of the moment); only a refusal may be pinned"
            )
        pinned[str(case_id)] = {str(k): str(v) for k, v in entry.items()}
    return pinned
