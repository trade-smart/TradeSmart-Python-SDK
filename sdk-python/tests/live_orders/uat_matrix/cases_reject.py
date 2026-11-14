"""The UAT matrix family M3: rejection paths (inventory §3.2, marker ``m_reject``).

Each case breaks exactly one rule and keeps everything else non-filling: a far LIMIT BUY (or a
far LIMIT SELL, a stop above the market) priced from a fresh quote, with one field changed. The
server's answer is the finding; the SDK validates none of these rules.

Roles: NSE ``M&M-EQ`` (``nse-e``), the BANKNIFTY future, the NIFTY ATM PE and the SENSEX ATM PE
(BFO: its canary first, U9). Probes (``PROBES``; ``eq`` = cash, ``fno`` = futures and options):

- documented refusals (``reject``): an off-tick price; one unit more than a lot (F&O); a BUY one
  tick below the lower circuit; a SELL one tick above the upper circuit; an SL-LMT BUY whose limit
  is below its trigger; CNC and MTF on F&O; NRML on cash; EOS on NSE; an unknown trading symbol;
  the latest expired contract still listed in the day's script master;
- recorded (``record``, U13): an SL-LMT BUY whose trigger is already below the last price (it may
  trigger at once; its limit is far below, so it rests); disclosed quantity 5 % and 50 % on cash
  (the docs say "Max 10% for NSE"); disclosed quantity on F&O (not documented).

Not here (and why): a BUY above ``uc`` and a SELL below ``lc`` would be marketable if accepted by
mistake (they are replaced by the SELL above ``uc`` and the BUY below ``lc``); the AMO flag inside
hours (M6); cancel / modify of a final order (M2; one CancelOrder per order per session).

A probe accepted by mistake rests far from the market: the batch cancels it once and records it
"accepted (...) although a refusal was expected" (verdict ``record``, never ``pass``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from tests.live_orders import safety
from tests.live_orders.safety import Refusal, floor_to_tick
from tests.live_orders.uat_matrix import batch, pricing
from tests.live_orders.uat_matrix.batch import BatchItem
from tests.live_orders.uat_matrix.cases import BASE_PRODUCT, FULL, RECORD, REJECT, SMOKE, Case
from tests.live_orders.uat_matrix.contracts import (
    DATA_ERRORS,
    SESSION_ERRORS,
    Contract,
    Contracts,
    MarketReader,
)
from tests.live_orders.uat_matrix.pricing import PriceSnapshot

FAMILY = "reject"
ROLES = ("nse-e", "nfo-banknifty-f", "nfo-nifty-pe-atm", "bfo-sensex-pe-atm")
EQ, FNO = "eq", "fno"
OFF_TICK = Decimal("0.01")  # the smallest price step a two-decimal price can take
CROSSED_TRIGGER = Decimal("0.97")  # a stop BUY trigger 3 % below the last price
DSCQTY_CASH_QUANTITY = 20
UNKNOWN_SYMBOL = {EQ: "SDKUATNOSUCH-EQ", FNO: "SDKUATNOSUCH26NOVFUT"}


@dataclass(frozen=True)
class Probe:
    name: str
    kinds: frozenset[str]
    outcome: str
    prctyp: str = "LMT"
    ret: str = "DAY"
    side: str = "BUY"
    product: str | None = None  # None: the role's base product


_BOTH = frozenset({EQ, FNO})
_EQ = frozenset({EQ})
_FNO = frozenset({FNO})
PROBES: tuple[Probe, ...] = (
    Probe("offtick", _BOTH, REJECT),
    Probe("lot", _FNO, REJECT),
    Probe("below-lc", _BOTH, REJECT),
    Probe("above-uc", _BOTH, REJECT, side="SELL"),
    Probe("sl-limit-below-trigger", _BOTH, REJECT, prctyp="SL-LMT"),
    Probe("sl-trigger-crossed", _BOTH, RECORD, prctyp="SL-LMT"),
    Probe("cnc", _FNO, REJECT, product="CNC"),
    Probe("mtf", _FNO, REJECT, product="MTF"),
    Probe("nrml", _EQ, REJECT, product="NRML"),
    Probe("eos", _EQ, REJECT, ret="EOS"),
    Probe("dscqty-low", _EQ, RECORD),
    Probe("dscqty-high", _EQ, RECORD),
    Probe("dscqty", _FNO, RECORD),
    Probe("unknown", _BOTH, REJECT),
    Probe("expired", _FNO, REJECT),
)
SMOKE_PROBES = frozenset(
    {
        ("nse-e", "offtick"),
        ("nse-e", "below-lc"),
        ("nfo-banknifty-f", "offtick"),
        ("nfo-banknifty-f", "lot"),
        ("nfo-banknifty-f", "below-lc"),
        ("nfo-nifty-pe-atm", "lot"),
    }
)


def _kind(role: str) -> str:
    return EQ if role.startswith(("nse-", "bse-")) else FNO


def _contract_kind(role: str) -> str:
    if role.endswith("-f"):
        return "fut"
    return "opt" if "-ce-" in role or "-pe-" in role else "eq"


def _build() -> tuple[list[Case], dict[str, Probe]]:
    table: list[Case] = []
    probe_of_id: dict[str, Probe] = {}
    for role in ROLES:
        for probe in PROBES:
            if _kind(role) not in probe.kinds:
                continue
            product = probe.product or BASE_PRODUCT[_contract_kind(role)]
            case_id = (
                f"{FAMILY}-{role}-{product}-{probe.prctyp}-{probe.ret}-{probe.side}-{probe.name}"
            ).lower()
            table.append(
                Case(
                    id=case_id,
                    family=FAMILY,
                    role=role,
                    product=product,
                    prctyp=probe.prctyp,
                    ret=probe.ret,
                    side=probe.side,
                    orders=1,
                    outcome=probe.outcome,
                    tier=SMOKE if (role, probe.name) in SMOKE_PROBES else FULL,
                )
            )
            probe_of_id[case_id] = probe
    return table, probe_of_id


REJECT_CASES, _PROBE_OF = _build()


def probe_of(case: Case) -> Probe:
    """The probe a case of this family sends (``KeyError`` for another family's case)."""
    return _PROBE_OF[case.id]


# -- plans ------------------------------------------------------------------------------------


def build_plan(
    case: Case,
    contract: Contract,
    snapshot: PriceSnapshot,
    *,
    expired: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """The probe's ``place_order`` arguments: the far, non-filling plan of the case with the one
    rule broken (``safety.Refusal`` when the probe cannot be built today: the case skips)."""
    probe = probe_of(case)
    option = contract.kind == "opt"
    plan = batch.default_plan(case, contract, snapshot)
    tick = snapshot.tick_size
    name = probe.name
    if name == "offtick":
        if tick <= OFF_TICK:
            raise Refusal(f"tick {tick}: no off-tick price with two decimals")
        plan["price"] = Decimal(str(plan["price"])) + OFF_TICK
    elif name == "lot":
        if contract.lot_size < 2:
            raise Refusal(f"{contract.tradingsymbol}: lot size 1, no part-lot quantity")
        plan["quantity"] = contract.lot_size + 1
    elif name == "below-lc":
        lc = snapshot.lower_circuit
        if lc is None:
            raise Refusal(f"{contract.tradingsymbol}: no lower circuit in the fresh quote")
        price = floor_to_tick(lc - tick, tick)
        if price <= 0:
            raise Refusal(f"no price below the lower circuit {lc} above 0")
        plan["price"] = price
    elif name == "above-uc":
        uc = snapshot.upper_circuit
        if uc is None:
            raise Refusal(f"{contract.tradingsymbol}: no upper circuit in the fresh quote")
        plan["price"] = floor_to_tick(uc, tick) + tick
    elif name == "sl-limit-below-trigger":
        # far below the trigger above the market
        plan["price"] = pricing.far_buy(snapshot, option=option)
    elif name == "sl-trigger-crossed":
        trigger = floor_to_tick(snapshot.last_price * CROSSED_TRIGGER, tick)
        price = pricing.far_buy(snapshot, option=option)
        if price > trigger:
            raise Refusal(f"no far limit below the crossed trigger {trigger}")
        plan["trigger_price"], plan["price"] = trigger, price
    elif name in ("dscqty-low", "dscqty-high"):
        plan["quantity"] = contract.quantity(DSCQTY_CASH_QUANTITY)
        plan["disclosed_quantity"] = 1 if name == "dscqty-low" else DSCQTY_CASH_QUANTITY // 2
    elif name == "dscqty":
        plan["disclosed_quantity"] = 1
    elif name == "unknown":
        plan["tradingsymbol"] = UNKNOWN_SYMBOL[_kind(case.role)]
    elif name == "expired":
        symbol = (expired or {}).get(case.role)
        if symbol is None:
            raise Refusal(
                f"no expired {contract.symbol or contract.tradingsymbol} contract is listed in "
                "the day's script master"
            )
        plan["tradingsymbol"] = symbol
    return plan


def items(
    cases: Iterable[Case], *, expired: Mapping[str, str | None] | None = None
) -> list[BatchItem]:
    """The batch items of ``cases`` (each with its probe's plan)."""
    found = dict(expired or {})

    def plan(case: Case, contract: Contract, snapshot: PriceSnapshot) -> dict[str, Any]:
        return build_plan(case, contract, snapshot, expired=found)

    return [BatchItem(case, plan=plan) for case in cases]


# -- the expired contract (read-only) ---------------------------------------------------------


def _norm(value: str | None) -> str:
    return (value or "").strip().upper()


def expired_symbol(
    rows: Iterable[Mapping[str, str]], contract: Contract, today: date
) -> str | None:
    """The trading symbol of the latest contract of the same underlying and kind (future, or
    option of the same type) that expired before ``today`` and is still in the master."""
    option = contract.kind == "opt"
    best: tuple[date, str] | None = None
    for row in rows:
        if _norm(row.get("Symbol")) != _norm(contract.symbol):
            continue
        expiry = safety._expiry(row.get("Expiry"))
        if expiry is None or expiry >= today:
            continue
        instrument = _norm(row.get("Instrument"))
        option_type = _norm(row.get("OptionType"))
        if option:
            if not instrument.startswith("OPT") or option_type != _norm(contract.option_type):
                continue
        elif not instrument.startswith("FUT"):
            continue
        tradingsymbol = (row.get("TradingSymbol") or "").strip()
        if tradingsymbol and (best is None or expiry > best[0]):
            best = (expiry, tradingsymbol)
    return None if best is None else best[1]


def expired_symbols(
    client: MarketReader, contracts: Contracts, cases: Sequence[Case], today: date
) -> dict[str, str | None]:
    """The expired symbol per role of the ``expired`` probes among ``cases`` (one script-master
    read per exchange; read-only). A master that cannot be read gives ``None`` (the case skips)."""
    roles = sorted({c.role for c in cases if c.id in _PROBE_OF and probe_of(c).name == "expired"})
    masters: dict[str, list[dict[str, str]] | None] = {}
    found: dict[str, str | None] = {}
    for role in roles:
        contract = contracts.get(role)
        if contract is None:
            found[role] = None
            continue
        if contract.exchange not in masters:
            try:
                masters[contract.exchange] = client.instruments(contract.exchange)
            except SESSION_ERRORS:
                raise
            except DATA_ERRORS:
                masters[contract.exchange] = None
        rows = masters[contract.exchange]
        found[role] = None if rows is None else expired_symbol(rows, contract, today)
    return found
