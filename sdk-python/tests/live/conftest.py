"""Opt-in gate and client fixtures for the live (real API) tests."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests._environment import environment_client
from tests.live._helpers import (
    CASES,
    Case,
    Contract,
    ContractResolver,
    case_id,
    record,
    resolve_or_skip,
    skip_if_unavailable_in_uat,
)
from tradesmart import TradeSmart


def _skip_reason() -> str | None:
    # Credentials come from TradeSmart.from_env(): TRADESMART_CLIENT_ID + TRADESMART_ACCESS_TOKEN,
    # or the saved login of `python -m tradesmart login`; it raises a clear error otherwise.
    if os.environ.get("TRADESMART_LIVE_TESTS") != "1":
        return "live tests are opt-in: set TRADESMART_LIVE_TESTS=1"
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    reason = _skip_reason()
    if reason is None:
        return
    marker = pytest.mark.skip(reason=reason)
    for item in items:
        # The item's file only: a parameter id may contain any text (e.g. a doc path).
        file_part = item.nodeid.split("::", 1)[0].replace("\\", "/")
        if "/tests/live/" in "/" + file_part:
            item.add_marker(marker)


def _new_client() -> TradeSmart:
    return environment_client(os.environ)


@pytest.fixture
def live_client() -> Iterator[TradeSmart]:
    client = _new_client()
    try:
        yield client
    finally:
        client.close()


# --------------------------------------------------------------------------- shared fixtures
# One client and one script-master download per session (rate limits); contracts are found
# dynamically from the script master, never hard-coded. A failed download, quote or lookup is
# remembered: the dependent tests skip with the reason instead of repeating the failing call.


def _resolve(resolver: ContractResolver, case: Case) -> Contract:
    """The contract of ``case``; MCX data is missing in the UAT environment, so skip there."""
    if case.segment == "mcx":
        skip_if_unavailable_in_uat()
    return resolve_or_skip(resolver, case)


def _case_params(cases: Iterable[Case]) -> list[Any]:
    return [pytest.param(c, id=case_id(c), marks=getattr(pytest.mark, c.segment)) for c in cases]


@pytest.fixture(scope="session")
def shared_client() -> Iterator[TradeSmart]:
    client = _new_client()
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="session")
def resolver(shared_client: TradeSmart) -> ContractResolver:
    def last_price(exchange: str, token: str) -> Any:
        return shared_client.quote(exchange, token).last_price

    def report(key: str, message: str) -> None:
        record("contracts", key, failure=message)

    return ContractResolver(
        shared_client.instruments,
        last_price,
        shared_client.search_instruments,
        on_failure=report,
    )


@pytest.fixture(params=_case_params(CASES))
def contract(request: pytest.FixtureRequest, resolver: ContractResolver) -> Contract:
    """Each segment/kind: ids ``nse-eq``, ``nfo-fut``, ``nfo-opt``, ``mcx-fut``, ``mcx-opt``."""
    return _resolve(resolver, request.param)


@pytest.fixture(params=_case_params(c for c in CASES if c.kind == "opt"))
def option_contract(request: pytest.FixtureRequest, resolver: ContractResolver) -> Contract:
    """An option of each derivatives segment: ids ``nfo-opt`` and ``mcx-opt``."""
    return _resolve(resolver, request.param)


@pytest.fixture(params=_case_params(c for c in CASES if c.kind == "fut"))
def future_contract(request: pytest.FixtureRequest, resolver: ContractResolver) -> Contract:
    """A future of each derivatives segment: ids ``nfo-fut`` and ``mcx-fut``."""
    return _resolve(resolver, request.param)


@pytest.fixture
def mcx_future(resolver: ContractResolver) -> Contract:
    """The nearest MCX future (CRUDEOIL, else GOLD)."""
    case = next(c for c in CASES if c.segment == "mcx" and c.kind == "fut")
    return _resolve(resolver, case)


@pytest.fixture
def observe(request: pytest.FixtureRequest) -> Callable[..., Path]:
    """``observe(**data)`` appends a JSON line to ``.live-results/<suite>.jsonl`` (no secrets)."""
    suite = request.module.__name__.rsplit(".", 1)[-1].removeprefix("test_live_")

    def _observe(**data: Any) -> Path:
        return record(suite, request.node.name, **data)

    return _observe
