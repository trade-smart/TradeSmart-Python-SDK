"""Option-chain analytics: put-call ratio, max pain and OI buildup classification."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from tradesmart.enums import _StrEnum

__all__ = [
    "Buildup",
    "BuildupReading",
    "ChainRow",
    "MaxPain",
    "OptionChainSnapshot",
    "buildup_from_snapshot",
    "classify_buildup",
    "max_pain",
    "pcr",
]

Number = Decimal | int | float


def _finite(value: Number) -> bool:
    if isinstance(value, Decimal):
        return value.is_finite()
    if isinstance(value, int):  # always finite; math.isfinite overflows on huge ints
        return True
    return math.isfinite(value)


@dataclass(frozen=True)
class ChainRow:
    """One option contract of a chain snapshot."""

    strike: Decimal
    option_type: str
    tradingsymbol: str
    token: str
    last_price: Decimal | None
    open_interest: Decimal | None
    volume: Decimal | None
    close: Decimal | None


@dataclass(frozen=True)
class MaxPain:
    """Max-pain strike and the total payout to option buyers at each candidate strike."""

    strike: Decimal
    payouts: dict[Decimal, Decimal]


@dataclass(frozen=True)
class OptionChainSnapshot:
    """Quotes for a set of option contracts taken at ``fetched_at``."""

    exchange: str
    tradingsymbol: str
    fetched_at: datetime
    rows: tuple[ChainRow, ...]
    missing: tuple[str, ...]

    def pcr(self, by: str = "oi") -> Decimal | None:
        """Put-call ratio of this snapshot (see :func:`pcr`)."""
        return pcr(self, by)

    def max_pain(self) -> MaxPain | None:
        """Max pain of this snapshot (see :func:`max_pain`)."""
        return max_pain(self)


def pcr(snapshot: OptionChainSnapshot, by: str = "oi") -> Decimal | None:
    """Put-call ratio (sum of PE / sum of CE) by open interest or volume."""
    if by not in ("oi", "volume"):
        raise ValueError(f"by must be 'oi' or 'volume', not {by!r}")
    puts = Decimal(0)
    calls = Decimal(0)
    for row in snapshot.rows:
        value = row.open_interest if by == "oi" else row.volume
        amount = value if value is not None else Decimal(0)
        if row.option_type == "PE":
            puts += amount
        elif row.option_type == "CE":
            calls += amount
    if calls == 0:
        return None
    return puts / calls


def max_pain(snapshot: OptionChainSnapshot) -> MaxPain | None:
    """Strike minimising the total payout to option buyers; ties go to the lowest strike."""
    if not any(r.open_interest is not None for r in snapshot.rows):
        return None
    strikes = sorted({r.strike for r in snapshot.rows})
    payouts: dict[Decimal, Decimal] = {}
    for s in strikes:
        total = Decimal(0)
        for r in snapshot.rows:
            oi = r.open_interest if r.open_interest is not None else Decimal(0)
            if r.option_type == "CE":
                total += oi * max(Decimal(0), s - r.strike)
            elif r.option_type == "PE":
                total += oi * max(Decimal(0), r.strike - s)
        payouts[s] = total
    best = min(strikes, key=lambda k: (payouts[k], k))
    return MaxPain(strike=best, payouts=payouts)


class Buildup(_StrEnum):
    """Price/open-interest buildup state."""

    LONG_BUILDUP = "long_buildup"
    SHORT_BUILDUP = "short_buildup"
    LONG_UNWINDING = "long_unwinding"
    SHORT_COVERING = "short_covering"
    NEUTRAL = "neutral"


def classify_buildup(price_change: Number | None, oi_change: Number | None) -> Buildup:
    """Classify the price/open-interest move; zero, missing or non-finite input is neutral."""
    if price_change is None or oi_change is None:
        return Buildup.NEUTRAL
    if not (_finite(price_change) and _finite(oi_change)) or price_change == 0 or oi_change == 0:
        return Buildup.NEUTRAL
    if price_change > 0:
        return Buildup.LONG_BUILDUP if oi_change > 0 else Buildup.SHORT_COVERING
    return Buildup.SHORT_BUILDUP if oi_change > 0 else Buildup.LONG_UNWINDING


@dataclass(frozen=True)
class BuildupReading:
    """A buildup classification with the changes it was derived from."""

    state: Buildup
    price_change: Decimal | None
    oi_change: Decimal | None
    basis: str


def _num(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        d = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def buildup_from_snapshot(snapshot: Mapping[str, Any]) -> BuildupReading:
    """Classify from a stream ``snapshot()`` dict (wire keys ``lp``, ``c``, ``oi``, ``poi``)."""
    lp, c = _num(snapshot.get("lp")), _num(snapshot.get("c"))
    oi, poi = _num(snapshot.get("oi")), _num(snapshot.get("poi"))
    price_change = lp - c if lp is not None and c is not None else None
    oi_change = oi - poi if oi is not None and poi is not None else None
    return BuildupReading(
        state=classify_buildup(price_change, oi_change),
        price_change=price_change,
        oi_change=oi_change,
        basis="previous_close_and_previous_oi",
    )
