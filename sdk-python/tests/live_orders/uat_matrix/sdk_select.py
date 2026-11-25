"""Which SDKs the matrix runs: ``TRADESMART_LIVE_SDKS`` and the ordered registry.

- unset or blank: ``python`` only (today's behaviour);
- ``all``: every registered SDK, in registry order;
- a comma list: those SDKs, in the order given, each once;
- an unknown name (or an empty item): refused (``Refusal``), naming the known ones.

The registry maps a name to a factory ``(client, environ) -> SdkUnderTest``. It holds ``python``
(in process) and ``javascript`` (a driver process, ``specifications/live-driver.md``). Reading the
variable never starts anything: a factory runs only once every harness gate has passed
(``gate.matrix_session``).

The JavaScript driver is ``sdk-javascript/tests/live_orders/driver.ts``, run by Node's native type
stripping against the BUILT SDK (``sdk-javascript/dist``). ``build_javascript`` refuses to start
it when any file under ``sdk-javascript/src`` is newer than ``dist/index.js`` (a stale build would
test the wrong code), then builds ``DriverSdk`` with a fresh random nonce (``secrets.token_hex``,
passed to the child only, never logged), the reply timeout derived from the referee client's own
``ClientConfig``, the referee's account id and secrets (so the driver's stderr and error texts are
masked), and a stderr log file under the results folder.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from tests.live_orders.safety import Refusal
from tests.live_orders.uat_matrix.driver_process import DriverSdk, default_reply_timeout
from tests.live_orders.uat_matrix.gate import RESULTS_ROOT
from tests.live_orders.uat_matrix.sdk_under_test import PythonSdk, SdkUnderTest, referee_secrets
from tradesmart import TradeSmart

SDKS_ENV = "TRADESMART_LIVE_SDKS"
DEFAULT = "python"

# sdk-python/tests/live_orders/uat_matrix/sdk_select.py -> the repository root (``/repo`` in the
# live-testing image).
REPO = Path(__file__).resolve().parents[4]
JS_ROOT = REPO / "sdk-javascript"
JS_COMMAND: tuple[str, str] = (
    "node",
    str(JS_ROOT / "tests" / "live_orders" / "driver.ts"),
)
JS_NAME = "javascript"

Factory = Callable[[TradeSmart, Mapping[str, str]], SdkUnderTest]


def dist_refusal(js_root: Path = JS_ROOT) -> str | None:
    """Why the built SDK cannot be used, or ``None``: ``dist/index.js`` missing, any file under
    ``src/`` newer than it (the build is older than the sources: it would test other code), or a
    file that cannot be examined (an ``OSError`` is a refusal, never a traceback)."""
    built = js_root / "dist" / "index.js"
    try:
        built_exists = built.is_file()
    except OSError as exc:
        return (
            f"a file of the JavaScript SDK cannot be examined ({type(exc).__name__}); the build "
            f"under {js_root} cannot be checked against src/"
        )
    if not built_exists:
        return (
            f"the JavaScript SDK is not built ({built} is missing); run `docker compose run --rm "
            "dev npm run build` from sdk-javascript/, then start the matrix again"
        )
    newest: tuple[int, Path] | None = None
    try:
        built_at = built.stat().st_mtime_ns
        for source in (js_root / "src").rglob("*"):
            if source.is_file():
                changed = source.stat().st_mtime_ns
                if changed > built_at and (newest is None or changed > newest[0]):
                    newest = (changed, source)
    except OSError as exc:
        return (
            f"a file of the JavaScript SDK cannot be examined ({type(exc).__name__}); the build "
            f"under {js_root} cannot be checked against src/"
        )
    if newest is None:
        return None
    return (
        f"the JavaScript build ({built}) is older than src/ ({newest[1].relative_to(js_root)} "
        "changed after it): run `docker compose run --rm dev npm run build` from "
        "sdk-javascript/, then start the matrix again"
    )


def build_javascript(
    client: TradeSmart,
    environ: Mapping[str, str],
    *,
    js_root: Path = JS_ROOT,
    results_root: Path | None = None,
) -> SdkUnderTest:
    """The JavaScript SDK under test: a ``DriverSdk`` on the driver program (see the module
    docstring). Refuses (``Refusal``) before any process starts when the build is stale."""
    reason = dist_refusal(js_root)
    if reason is not None:
        raise Refusal(f"{reason}; refusing")
    root = results_root if results_root is not None else RESULTS_ROOT
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return DriverSdk(
        list(JS_COMMAND),
        nonce=secrets.token_hex(16),
        environ=environ,
        reply_timeout=default_reply_timeout(client.config),
        referee_client_id=client.client_id,
        name=JS_NAME,
        secrets=referee_secrets(client),
        stderr_log=root / "driver-stderr" / f"{JS_NAME}-{stamp}.jsonl",
    )


def _python(client: TradeSmart, environ: Mapping[str, str]) -> SdkUnderTest:
    return PythonSdk(client)


def _javascript(client: TradeSmart, environ: Mapping[str, str]) -> SdkUnderTest:
    return build_javascript(client, environ, js_root=JS_ROOT)


# One ordered registry; the order is the order of ``all``.
REGISTRY: dict[str, Factory] = {DEFAULT: _python, JS_NAME: _javascript}


def selected_sdks(environ: Mapping[str, str]) -> list[str]:
    """The SDK names to run, from ``TRADESMART_LIVE_SDKS`` (see the module docstring)."""
    known = list(REGISTRY)
    raw = environ.get(SDKS_ENV, "").strip()
    if not raw:
        return [DEFAULT]
    if raw.lower() == "all":
        return known
    chosen: list[str] = []
    for item in (part.strip() for part in raw.split(",")):
        if item not in REGISTRY:
            shown = repr(item) if item else "an empty item"
            raise Refusal(
                f"{SDKS_ENV} names {shown}, which is not a known SDK; known: {', '.join(known)} "
                f"(or 'all'); refusing"
            )
        if item not in chosen:
            chosen.append(item)
    return chosen


def build_sdk(name: str, client: TradeSmart, environ: Mapping[str, str]) -> SdkUnderTest:
    """The ``SdkUnderTest`` for a registered name, on the session's Python client."""
    if name not in REGISTRY:
        raise Refusal(f"unknown SDK {name!r}; known: {', '.join(REGISTRY)}; refusing")
    return REGISTRY[name](client, environ)
