import ipaddress
import json
import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tradesmart import AsyncTradeSmart, ClientConfig, TradeSmart

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "specifications" / "test-fixtures"


def fixture(name: str) -> Any:
    """Load ``<name>.json`` from the shared specifications/test-fixtures directory."""
    with (FIXTURE_DIR / f"{name}.json").open(encoding="utf-8") as fh:
        return json.load(fh)


BASE = "https://v2api.tradesmartonline.in/NorenWClientAPIv2"

LIVE_OPT_INS = ("TRADESMART_LIVE_TESTS", "TRADESMART_LIVE_ORDER_TESTS")


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config: pytest.Config) -> None:
    """Run in one process whenever a live suite is opted in, whatever the xdist options say.

    ``addopts`` runs the suite on 4 xdist workers; the live suites keep one client, one script
    master download, the order cap and the "one open order at a time" rule per process, so they
    must run in a single process, once. With ``TRADESMART_LIVE_TESTS=1`` or
    ``TRADESMART_LIVE_ORDER_TESTS=1``:

    - ``-f/--looponfail`` is refused: it re-runs the tests in fresh processes (a fresh order
      budget each time) whenever a file changes or the failing set passes;
    - ``-n``, ``--tx``, ``-d`` and ``--dist`` are overridden the way xdist handles ``-n 0``, so no
      worker starts (and ``--pdb`` works).

    A conftest hook runs before xdist's own ``tryfirst`` ``pytest_cmdline_main`` (registered
    earlier), which decides distribution and starts looponfail.
    """
    if not any(os.environ.get(name) == "1" for name in LIVE_OPT_INS):
        return
    if config.getoption("looponfail", False):
        raise pytest.UsageError(
            "-f/--looponfail cannot be used with a live opt-in (TRADESMART_LIVE_TESTS or "
            "TRADESMART_LIVE_ORDER_TESTS): it would re-run live tests in new processes."
        )
    if hasattr(config.option, "numprocesses"):
        config.option.numprocesses = 0
        config.option.distload = False
        config.option.dist = "no"
        config.option.tx = []


@pytest.fixture(autouse=True)
def _no_saved_login(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Path | None:
    """Never read the developer's real saved login: point the token file at an empty folder.

    The opt-in live suites are exempt: they use the saved login of ``python -m tradesmart login``.
    """
    if is_live_suite(Path(str(request.node.path))):
        return None
    path = tmp_path_factory.mktemp("saved-login") / "token.json"
    monkeypatch.setenv("TRADESMART_TOKEN_FILE", str(path))
    return path


# Keys a developer's shell may hold for a test environment; compose passes them into every
# service. Default tests must neither use them nor print them (a failing assertion on a login URL
# would show the App Key). The production key variables are handled by the tests that use them.
SHELL_ENVIRONMENT_KEYS = ("TRADESMART_ENV_APP_KEY", "TRADESMART_ENV_SECRET_KEY")


@pytest.fixture(autouse=True)
def _no_shell_environment_keys(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Remove the shell's test-environment keys for every test outside the live suites."""
    if is_live_suite(Path(str(request.node.path))):
        return
    for name in SHELL_ENVIRONMENT_KEYS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client() -> TradeSmart:
    return TradeSmart(
        "TEST123",
        access_token="test-access-token",
        config=ClientConfig(backoff_base=0.001, backoff_cap=0.001),
    )


@pytest.fixture
def async_client() -> AsyncTradeSmart:
    return AsyncTradeSmart(
        "TEST123",
        access_token="test-access-token",
        config=ClientConfig(backoff_base=0.001, backoff_cap=0.001),
    )


def _is_loopback(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host in (None, "", "localhost") or (isinstance(host, str) and host.endswith(".localhost")):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _blocked(host: object) -> RuntimeError:
    return RuntimeError(f"network access is blocked in tests: {host}")


LIVE_DIR = Path(__file__).resolve().parent / "live"
LIVE_ORDERS_DIR = Path(__file__).resolve().parent / "live_orders"


def is_live_suite(path: Path) -> bool:
    """True for files under the opt-in ``tests/live`` and ``tests/live_orders`` directories only."""
    resolved = Path(path).resolve()
    return resolved.is_relative_to(LIVE_DIR) or resolved.is_relative_to(LIVE_ORDERS_DIR)


@pytest.fixture(autouse=True)
def _block_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Automated tests never touch the network; only loopback is allowed.

    Respx / ``httpx.MockTransport`` never open sockets; the local WebSocket mock server and the
    CLI callback server use loopback. The opt-in ``tests/live/`` and ``tests/live_orders/``
    suites are exempt.
    """
    if is_live_suite(Path(str(request.node.path))):
        yield
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _check(address: Any) -> None:
        # AF_UNIX addresses are str/bytes paths; only inet tuples carry a host.
        if isinstance(address, tuple) and address and not _is_loopback(address[0]):
            raise _blocked(address[0])

    def connect(self: socket.socket, address: Any) -> None:
        _check(address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        _check(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_loopback(host):
            raise _blocked(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
