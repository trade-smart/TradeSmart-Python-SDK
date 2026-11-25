"""Wire-level enumerations.

Members send TradeSmart's wire codes; lookups also accept the common words (``"BUY"``,
``"LIMIT"``, ``"MIS"`` ...), case-insensitively.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class _StrEnum(StrEnum):
    """``str`` enum that prints as its value: ``str(OrderStatus.OPEN)`` and
    ``f"{OrderStatus.OPEN}"`` both give ``"OPEN"``."""


class _WordEnum(_StrEnum):
    """``str`` enum whose lookup also resolves aliases and wire codes case-insensitively."""

    @classmethod
    def _missing_(cls, value: object) -> Any | None:
        if not isinstance(value, str):
            return None
        key = value.strip().upper()
        for member in cls:
            if member.value.upper() == key:
                return member
        alias = cls._aliases().get(key)
        return cls[alias] if alias is not None else None

    @classmethod
    def _aliases(cls) -> dict[str, str]:
        """Extra accepted words (upper case) mapped to member names."""
        return {}


class Exchange(_WordEnum):
    """Exchange codes; ``Exchange("nse")`` resolves to ``Exchange.NSE``."""

    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"
    BFO = "BFO"
    CDS = "CDS"
    BCD = "BCD"
    MCX = "MCX"


class TransactionType(_WordEnum):
    BUY = "B"
    SELL = "S"

    @classmethod
    def _aliases(cls) -> dict[str, str]:
        return {"BUY": "BUY", "SELL": "SELL"}


class OrderType(_WordEnum):
    LIMIT = "LMT"
    MARKET = "MKT"
    SL = "SL-LMT"
    SL_M = "SL-MKT"

    @classmethod
    def _aliases(cls) -> dict[str, str]:
        return {
            "LIMIT": "LIMIT",
            "MARKET": "MARKET",
            "SL": "SL",
            "SL-M": "SL_M",
            "SLM": "SL_M",
        }


class Validity(_WordEnum):
    DAY = "DAY"
    IOC = "IOC"
    EOS = "EOS"


class Product(_WordEnum):
    CNC = "C"
    MIS = "I"
    NRML = "M"
    CO = "H"
    BO = "B"
    MTF = "F"

    @classmethod
    def _aliases(cls) -> dict[str, str]:
        return {name: name for name in cls.__members__}


class OrderStatus(_WordEnum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    TRIGGER_PENDING = "TRIGGER_PENDING"
    COMPLETE = "COMPLETE"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class GttAlertType(_WordEnum):
    """What a GTT watches and in which direction (``ai_t``); the trigger value is ``d``.

    ``*_ABOVE``: the GTT triggers when the value is greater than ``d``; ``*_BELOW``: less
    than ``d``. Lookups accept the wire code or the member name, in any case.
    """

    LTP_ABOVE = "LTP_A_O"
    LTP_BELOW = "LTP_B_O"
    CHANGE_PERCENT_ABOVE = "CH_PER_A_O"
    CHANGE_PERCENT_BELOW = "CH_PER_B_O"
    ATP_ABOVE = "ATP_A_O"
    ATP_BELOW = "ATP_B_O"
    OI_ABOVE = "OI_A_O"
    OI_BELOW = "OI_B_O"
    TOI_ABOVE = "TOI_A_O"
    TOI_BELOW = "TOI_B_O"
    HIGH_52W_ABOVE = "LTP_A_52HIGH_O"
    HIGH_52W_BELOW = "LTP_B_52HIGH_O"
    VOLUME_ABOVE = "VOLUME_A_O"
    OCO_BUY = "LMT_BOS_O"
    OCO_SELL = "LMT_SOB_O"

    @classmethod
    def _aliases(cls) -> dict[str, str]:
        return {name: name for name in cls.__members__}


class GttValidity(_WordEnum):
    """How long a GTT stays active (its wire ``validity``; not the order's ``ret``).

    ``GTT``: until it triggers or is cancelled, at most 365 days or the contract expiry,
    whichever is earlier. ``DAY``: the current trading day.
    """

    DAY = "DAY"
    GTT = "GTT"
