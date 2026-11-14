"""The UAT matrix case table: what each case sends and the documented outcome it expects.

A case is plain frozen data (inventory §3.2): family, contract role (``contracts.ROLES``),
product, price type, validity, side, the number of orders it declares (a bug guard, owner answer
U1) and the outcome kind the documentation predicts. ``expect.expectation`` turns that outcome
into the run's verdict with the account profile and the known UAT issues.

Families here:
- ``shapes`` (M1): product × {LMT, SL-LMT} × {DAY, IOC, EOS} × {BUY, SELL} per instrument, plus
  MKT and SL-MKT per product × side (F27), MTF LMT DAY on cash; the BFO canary (F26, U9). The
  ATM options of NIFTY, BANKNIFTY and SENSEX (CE and PE) get the full grid; the ITM / OTM rungs
  of the NIFTY and BANKNIFTY ladders the resting DAY shapes (``rung_shapes``). Smoke: the six
  smoke shapes once per segment (NFO splits them between the future and the ATM CE).
- ``bse`` (M14): the BSE ``ITC`` canary first, then the same grid; smoke: the six smoke shapes
  plus LMT EOS BUY (``cases_bse`` adds M1's BFO canary to the family's module, not a second).
- ``mcx`` (M15): the grid on CRUDEOIL, GOLD, GOLDM and GOLDPETAL (trade units, U7); smoke: the
  six on CRUDEOIL and a far LIMIT BUY NRML on each other commodity (``cases_mcx`` adds the
  off-tick and part-lot probes).
- ``fills`` (M4): BUY at the last price closed by a SELL (known issue ``closing-sell``), short
  SELL covered by a BUY, and the partial-fill probe with its closing SELL (recorded;
  ``cases_fills`` runs the family).

Ids are ``<family>-<role>-<product>-<prctyp>-<ret>-<side>`` in lower case (``-canary`` for the
canaries), unique, usable with ``pytest -k``. Tiers: ``smoke`` cases are also in ``full``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# Outcome kinds the documentation predicts.
REST = "rest"  # accepted, rests OPEN until cancelled
TRIGGER_PENDING = "trigger_pending"  # accepted stop order, rests TRIGGER_PENDING
EXCHANGE_CANCEL = "exchange_cancel"  # IOC away from the market: CANCELED by the exchange
REJECT = "reject"  # refused (place reply or REJECTED in the book)
FILL_CLOSE = "fill_close"  # BUY at the last price fills, then a closing SELL at the last price
SHORT_COVER = "short_cover"  # SELL to open at the last price, then a covering BUY (unknown)
RECORD = "record"  # undocumented: record the answer (U13)
OUTCOMES = frozenset(
    {REST, TRIGGER_PENDING, EXCHANGE_CANCEL, REJECT, FILL_CLOSE, SHORT_COVER, RECORD}
)

SMOKE, FULL, STRESS = "smoke", "full", "stress"
TIERS = (SMOKE, FULL, STRESS)  # each tier includes the ones before it

SEGMENT_EXCHANGE = {"nse": "NSE", "bse": "BSE", "nfo": "NFO", "bfo": "BFO", "mcx": "MCX"}
# Retention annexure: NSE/NFO/CDS -> DAY, IOC; BSE/BFO/BCD -> EOS, DAY, IOC. MCX has no row, so
# on MCX only DAY (the validity UAT MCX accepted, 2026-11-12) is predicted; IOC and EOS are
# recorded until pinned.
EOS_EXCHANGES = frozenset({"BSE", "BFO"})
DOCUMENTED_RET = frozenset({"NSE", "NFO", "BSE", "BFO"})
DELIVERY_PRODUCTS = frozenset({"CNC", "MTF"})  # a cash SELL of these needs a holding


@dataclass(frozen=True)
class Case:
    id: str
    family: str
    role: str
    product: str  # CNC | MIS | NRML | CO | BO | MTF
    prctyp: str  # LMT | SL-LMT | MKT | SL-MKT
    ret: str  # DAY | IOC | EOS
    side: str  # BUY | SELL (the first order's side)
    orders: int  # orders the case declares (bug guard)
    outcome: str
    tier: str = FULL
    canary: bool = False

    @property
    def segment(self) -> str:
        return self.role.split("-", 1)[0]

    @property
    def exchange(self) -> str:
        return SEGMENT_EXCHANGE[self.segment]


def _id(family: str, role: str, product: str, prctyp: str, ret: str, side: str) -> str:
    return f"{family}-{role}-{product}-{prctyp}-{ret}-{side}".lower()


def _shape_outcome(exchange: str, kind: str, product: str, prctyp: str, ret: str, side: str) -> str:
    if prctyp in ("MKT", "SL-MKT"):
        return REJECT  # F27: ALGO_CHK refuses API market orders
    if ret != "DAY" and exchange not in DOCUMENTED_RET:
        return RECORD  # MCX IOC / EOS: not in the retention annexure
    if ret == "EOS" and exchange not in EOS_EXCHANGES:
        return REJECT
    if kind == "eq" and product in DELIVERY_PRODUCTS and side == "SELL":
        return RECORD  # a delivery SELL without holdings: RMS refusal expected, not documented
    if ret == "IOC":
        return EXCHANGE_CANCEL if prctyp == "LMT" else RECORD  # IOC stop: undocumented
    return REST if prctyp == "LMT" else TRIGGER_PENDING


# The smoke shapes of a segment: (prctyp, ret, side) on its base product.
SMOKE_SHAPES = frozenset(
    {
        ("LMT", "DAY", "BUY"),
        ("LMT", "DAY", "SELL"),
        ("LMT", "IOC", "BUY"),
        ("SL-LMT", "DAY", "BUY"),
        ("SL-LMT", "DAY", "SELL"),
        ("MKT", "DAY", "BUY"),
    }
)
SMOKE_ROLES = frozenset(
    {
        "nse-b",
        "bse-b",
        "nfo-nifty-f",
        "nfo-nifty-ce-atm",
        "bfo-sensex-f",
        "mcx-crudeoil-f",
        "mcx-gold-f",
        "mcx-goldm-f",
        "mcx-goldpetal-f",
    }
)
BASE_PRODUCT = {"eq": "MIS", "fut": "NRML", "opt": "NRML"}
# A segment with two smoke roles splits the six smoke shapes between them (six per segment).
# BSE adds EOS (BSE takes it, NSE refuses it: M14); every MCX commodity but CRUDEOIL (which
# runs the six) places one far LIMIT BUY NRML, so each records its accepted quantity units (M15).
SMOKE_SPLIT: dict[str, frozenset[tuple[str, str, str]]] = {
    "nfo-nifty-f": frozenset(
        {("LMT", "DAY", "BUY"), ("LMT", "IOC", "BUY"), ("SL-LMT", "DAY", "SELL")}
    ),
    "nfo-nifty-ce-atm": frozenset(
        {("LMT", "DAY", "SELL"), ("SL-LMT", "DAY", "BUY"), ("MKT", "DAY", "BUY")}
    ),
    "bse-b": SMOKE_SHAPES | {("LMT", "EOS", "BUY")},
    **{
        role: frozenset({("LMT", "DAY", "BUY")})
        for role in ("mcx-gold-f", "mcx-goldm-f", "mcx-goldpetal-f")
    },
}


def _kind(role: str) -> str:
    if role.endswith("-f"):
        return "fut"
    return "opt" if "-ce-" in role or "-pe-" in role else "eq"


def shape_grid(family: str, role: str) -> list[Case]:
    """Every M1 shape of ``role``."""
    kind = _kind(role)
    exchange = SEGMENT_EXCHANGE[role.split("-", 1)[0]]
    products = ("CNC", "MIS") if kind == "eq" else ("NRML", "MIS")
    shapes = [
        (product, prctyp, ret, side)
        for product in products
        for prctyp in ("LMT", "SL-LMT")
        for ret in ("DAY", "IOC", "EOS")
        for side in ("BUY", "SELL")
    ]
    shapes += [
        (product, prctyp, "DAY", side)
        for product in products
        for prctyp in ("MKT", "SL-MKT")
        for side in ("BUY", "SELL")
    ]
    if kind == "eq":
        shapes += [("MTF", "LMT", "DAY", side) for side in ("BUY", "SELL")]
    result: list[Case] = []
    for product, prctyp, ret, side in shapes:
        smoke = (
            role in SMOKE_ROLES
            and product == BASE_PRODUCT[kind]
            and (prctyp, ret, side) in SMOKE_SPLIT.get(role, SMOKE_SHAPES)
        )
        result.append(
            Case(
                id=_id(family, role, product, prctyp, ret, side),
                family=family,
                role=role,
                product=product,
                prctyp=prctyp,
                ret=ret,
                side=side,
                orders=1,
                outcome=_shape_outcome(exchange, kind, product, prctyp, ret, side),
                tier=SMOKE if smoke else FULL,
            )
        )
    return result


def rung_shapes(family: str, role: str) -> list[Case]:
    """The resting DAY shapes of an option ladder rung: NRML × {LMT, SL-LMT} × {BUY, SELL}."""
    return [
        Case(
            id=_id(family, role, "NRML", prctyp, "DAY", side),
            family=family,
            role=role,
            product="NRML",
            prctyp=prctyp,
            ret="DAY",
            side=side,
            orders=1,
            outcome=REST if prctyp == "LMT" else TRIGGER_PENDING,
        )
        for prctyp in ("LMT", "SL-LMT")
        for side in ("BUY", "SELL")
    ]


def _canary(family: str, role: str, product: str) -> Case:
    """One far LIMIT BUY that decides whether the segment runs (F26, U9)."""
    return Case(
        id=_id(family, role, product, "LMT", "DAY", "BUY") + "-canary",
        family=family,
        role=role,
        product=product,
        prctyp="LMT",
        ret="DAY",
        side="BUY",
        orders=1,
        outcome=REST,
        tier=SMOKE,
        canary=True,
    )


SHAPE_ROLES = (
    "nse-b",
    "nse-t",
    "nfo-nifty-f",
    "nfo-banknifty-f",
    "nfo-nifty-ce-atm",
    "nfo-nifty-pe-atm",
    "nfo-stk-f",
    "nfo-stk-ce-atm",
    "bfo-sensex-f",
    "bfo-sensex-ce-atm",
    "nfo-banknifty-ce-atm",
    "nfo-banknifty-pe-atm",
    "bfo-sensex-pe-atm",
)
# The in- and out-of-the-money rungs of the NIFTY and BANKNIFTY ladders: the resting DAY shapes
# on NRML (``rung_shapes``); the full grid runs on the ATM strikes.
RUNG_ROLES = tuple(
    f"nfo-{index}-{option_type}-{rung}"
    for index in ("nifty", "banknifty")
    for option_type in ("ce", "pe")
    for rung in ("itm1", "itm2", "otm1", "otm2")
)
MCX_ROLES = ("mcx-crudeoil-f", "mcx-gold-f", "mcx-goldm-f", "mcx-goldpetal-f")
# (role, product) of the fill-and-close cases (inventory M4) and of the short-and-cover ones.
FILL_ROLES = (
    ("nse-b", "CNC"),
    ("nse-b", "MIS"),
    ("nfo-nifty-f", "NRML"),
    ("nfo-nifty-f", "MIS"),
    ("nfo-nifty-ce-atm", "MIS"),
    ("nfo-nifty-ce-atm", "NRML"),
    ("nfo-stk-f", "MIS"),
    ("bfo-sensex-ce-atm", "MIS"),
)
SHORT_ROLES = ("nfo-nifty-f", "nfo-nifty-ce-atm", "nse-b")
SMOKE_FILL = ("nfo-nifty-f", "NRML")
# The partial-fill probe (inventory M4): a BUY of PARTIAL_LOTS lots at the last price on an
# instrument no other fill case uses, sized down to the run's position ceiling (``positions``),
# then a SELL of what filled. Both are recorded (U13); ``cases_fills`` runs them.
PARTIAL_ROLE = "nfo-banknifty-f"
PARTIAL_LOTS = 10
PARTIAL_SUFFIX = "-partial"
PARTIAL_CLOSE_SUFFIX = "-partial-close"


def _fills() -> list[Case]:
    result = [
        Case(
            id=_id("fills", role, product, "LMT", "DAY", "BUY"),
            family="fills",
            role=role,
            product=product,
            prctyp="LMT",
            ret="DAY",
            side="BUY",
            orders=2,
            outcome=FILL_CLOSE,
            tier=SMOKE if (role, product) == SMOKE_FILL else FULL,
        )
        for role, product in FILL_ROLES
    ]
    result += [
        Case(
            id=_id("fills", role, "MIS", "LMT", "DAY", "SELL"),
            family="fills",
            role=role,
            product="MIS",
            prctyp="LMT",
            ret="DAY",
            side="SELL",
            orders=2,
            outcome=SHORT_COVER,
        )
        for role in SHORT_ROLES
    ]
    for side, suffix in (("BUY", PARTIAL_SUFFIX), ("SELL", PARTIAL_CLOSE_SUFFIX)):
        result.append(
            Case(
                id=_id("fills", PARTIAL_ROLE, "MIS", "LMT", "DAY", side) + suffix,
                family="fills",
                role=PARTIAL_ROLE,
                product="MIS",
                prctyp="LMT",
                ret="DAY",
                side=side,
                orders=1,
                outcome=RECORD,
            )
        )
    return result


def _build() -> list[Case]:
    table: list[Case] = [_canary("bse", "bse-b", "CNC"), *shape_grid("bse", "bse-b")]
    table.append(_canary("shapes", "bfo-sensex-f", "NRML"))
    for role in SHAPE_ROLES:
        table += shape_grid("shapes", role)
    for role in RUNG_ROLES:
        table += rung_shapes("shapes", role)
    for role in MCX_ROLES:
        table += shape_grid("mcx", role)
    table += _fills()
    return table


CASES: list[Case] = _build()
_BY_ID = {c.id: c for c in CASES}


def case(case_id: str) -> Case:
    """The case with ``case_id`` (``KeyError`` when there is none)."""
    return _BY_ID[case_id]


def select(tier: str, cases: Iterable[Case] = CASES) -> list[Case]:
    """The cases a ``tier`` run includes (each tier includes the ones before it), in order."""
    rank = TIERS.index(tier)
    return [c for c in cases if TIERS.index(c.tier) <= rank]


# Cases that family modules keep outside ``CASES`` (``cases_cobo``, ``cases_hold``, ...) and
# register at import: a resumed run restores their known-issue state too (``batch._restore``).
_REGISTERED: dict[str, Case] = {}


def register(cases: Iterable[Case]) -> None:
    """Add a family's own cases to the resume registry (once each; registering again adds
    nothing; an id already known, in ``CASES`` or registered, with other fields is refused)."""
    for item in cases:
        known = _BY_ID.get(item.id) or _REGISTERED.get(item.id)
        if known is not None and known != item:
            raise ValueError(f"case id {item.id} is registered already with other fields")
        _REGISTERED.setdefault(item.id, item)


def registered() -> list[Case]:
    """``CASES``, then every registered family case (``register``), in order."""
    return [*CASES, *(c for c in _REGISTERED.values() if c.id not in _BY_ID)]
