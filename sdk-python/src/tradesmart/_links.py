"""The tagged "open an account" link shown to people who have no TradeSmart account yet."""

from __future__ import annotations

from urllib.parse import urlencode

__all__ = ["SIGNUP_PLACES", "signup_url"]

_SIGNUP_BASE = "https://signup.tradesmartonline.in/"

SIGNUP_PLACES: frozenset[str] = frozenset(
    # github_about: the repository's GitHub "About" website field (set by hand, not in a file)
    {"readme", "pypi", "getting_started", "api_access", "pypi_sidebar", "cli", "github_about"}
)


def signup_url(place: str) -> str:
    """The signup URL tagged with this SDK and the place it is shown (one of SIGNUP_PLACES)."""
    if place not in SIGNUP_PLACES:
        raise ValueError(f"Unknown signup link place: {place!r}")
    query = urlencode(
        {
            "utm_source": "PythonSDK",
            "utm_medium": "Organic",
            "utm_campaign": "SDK_Open_Demat",
            "utm_content": place,
            "channel": "Organic",
            "platform": "web",
        }
    )
    return f"{_SIGNUP_BASE}?{query}"
