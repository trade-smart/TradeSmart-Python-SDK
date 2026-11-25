"""The SDK under test: the seam between the matrix and the SDK whose writes it exercises.

Only the eleven state-changing calls (and ``close``) go through ``SdkUnderTest``. Every harness
read (book lookups, account match, limits preflight, quotes, instruments, teardown checks) stays
on the Python client (``MatrixSession.client``); the gates stay in the harness and are re-checked
before every place and modify (``calls``). ``PythonSdk`` delegates to the Python client, so the
Python path is exactly what it was before the seam. Other languages come through a driver
process (``specifications/live-driver.md``).

Signatures are the Python client's own (keyword-only order fields as ``**kwargs``); a call
returns what the client returns and raises what it raises (``APIError``,
``OrderStatusUnknownError``, ``RequestValidationError``, ...), with nothing added or swallowed.
"""

from __future__ import annotations

import contextlib
from contextlib import AbstractContextManager
from typing import Any, Protocol

from tradesmart import TradeSmart, __version__
from tradesmart._slicing import SlicedOrderResult

# The one-letter code of each SDK in order tags and run ids (``sdkuat-<run>-<sdk><key>-<n>``);
# one lower-case letter each, never reused. Knowing a code does not register an SDK
# (``sdk_select.REGISTRY`` does).
SDK_CODES: dict[str, str] = {"python": "p", "javascript": "j"}


class SdkUnderTest(Protocol):
    """One SDK as the matrix sees it: identity plus the eleven state-changing calls."""

    name: str  # ``python``, ``javascript`` (the registry key)
    code: str  # one letter in order tags: ``p`` Python, ``j`` JavaScript
    version: str
    client_id: str

    def send_slot(self) -> AbstractContextManager[Any]:
        """The slot one state-changing call holds from its order-pacer wait to its write.

        An SDK whose sends are serial (a driver: one request at a time) returns one lock, the
        same for every call, so a call never takes its pacer slot and then queues behind
        another write: the pacer wait, the gate / STOP re-check and the write are one step in
        order. An SDK that sends in parallel returns a context manager that never blocks."""
        ...

    def place_order(self, **kwargs: Any) -> str: ...

    def place_sliced_order(self, **kwargs: Any) -> SlicedOrderResult: ...

    def modify_order(self, order_id: str, **kwargs: Any) -> Any: ...

    def cancel_order(self, order_id: str) -> str: ...

    def exit_order(self, order_id: str, product: Any) -> str: ...

    def place_gtt(self, **kwargs: Any) -> str: ...

    def modify_gtt(self, alert_id: str, **kwargs: Any) -> Any: ...

    def cancel_gtt(self, alert_id: str) -> str: ...

    def place_oco(self, **kwargs: Any) -> str: ...

    def modify_oco(self, alert_id: str, **kwargs: Any) -> str: ...

    def cancel_oco(self, alert_id: str) -> str: ...

    def close(self) -> None: ...


class PythonSdk:
    """The Python SDK: every call is forwarded to the ``TradeSmart`` client unchanged."""

    name = "python"
    code = "p"

    def __init__(self, client: TradeSmart) -> None:
        self._client = client
        self.version = __version__
        self.client_id = client.client_id

    def send_slot(self) -> AbstractContextManager[Any]:
        return contextlib.nullcontext()  # the Python client's sends are not serial

    def place_order(self, **kwargs: Any) -> str:
        return self._client.place_order(**kwargs)

    def place_sliced_order(self, **kwargs: Any) -> SlicedOrderResult:
        return self._client.place_sliced_order(**kwargs)

    def modify_order(self, order_id: str, **kwargs: Any) -> Any:
        return self._client.modify_order(order_id, **kwargs)

    def cancel_order(self, order_id: str) -> str:
        return self._client.cancel_order(order_id)

    def exit_order(self, order_id: str, product: Any) -> str:
        return self._client.exit_order(order_id, product)

    def place_gtt(self, **kwargs: Any) -> str:
        return self._client.place_gtt(**kwargs)

    def modify_gtt(self, alert_id: str, **kwargs: Any) -> Any:
        return self._client.modify_gtt(alert_id, **kwargs)

    def cancel_gtt(self, alert_id: str) -> str:
        return self._client.cancel_gtt(alert_id)

    def place_oco(self, **kwargs: Any) -> str:
        return self._client.place_oco(**kwargs)

    def modify_oco(self, alert_id: str, **kwargs: Any) -> str:
        return self._client.modify_oco(alert_id, **kwargs)

    def cancel_oco(self, alert_id: str) -> str:
        return self._client.cancel_oco(alert_id)

    def close(self) -> None:
        self._client.close()


def _secret_text(value: Any) -> str | None:
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        value = getter()
    return value if isinstance(value, str) and value.strip() else None


def referee_secrets(client: Any) -> list[str]:
    """The referee client's access, session and refresh tokens (its saved login too), app key
    and secret key, for ``DriverSdk(secrets=...)``: a driver reads the same saved login, so its
    tokens are masked in the driver's stderr and error texts though no environment variable
    holds them."""
    creds = getattr(client, "_creds", None)
    token = getattr(creds, "_token", None)
    values = [
        _secret_text(getattr(token, "access_token", token)),
        # A saved login (``AccessToken``) also holds the session and refresh tokens.
        _secret_text(getattr(token, "susertoken", None)),
        _secret_text(getattr(token, "refresh_token", None)),
        _secret_text(getattr(creds, "_app_key", None)),
        _secret_text(getattr(creds, "_secret_key", None)),
    ]
    return [value for value in values if value is not None]
