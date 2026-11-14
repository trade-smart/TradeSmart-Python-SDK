"""The UAT sliced-order live test's helpers (``tests/live_orders/sliced.py``), offline.

The fake client below stands in for ``TradeSmart``: ``place_sliced_order`` returns (or raises) a
scripted result and each ``orders()`` call pops the next book.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.live_orders import safety, sliced
from tests.live_orders.safety import CheckTheAccount, OrderBudget, OrderSession, Recorder, Refusal
from tests.unit.test_live_orders_safety import FakeClient, FakeTime, _order, _plan
from tradesmart import OrderRejectedError, OrderStatusUnknownError, SlicedOrderResult
from tradesmart.models.orders import Trade

UAT = {"TRADESMART_ENV": "uat"}
TAG = "sdkslice-abc123"
LOT = 65


def _nifty_plan(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "segment_id": "nfo",
        "exchange": "NFO",
        "tradingsymbol": "NIFTY25NOVFUT",
        "token": "35001",
        "quantity": LOT,
        "price": Decimal("23400.00"),
        "product": "NRML",
        "tag": TAG,
        "tick_size": Decimal("0.10"),
        "lower_circuit": Decimal("23000.00"),
        "last_price": Decimal("26000.00"),
    }
    values.update(overrides)
    return _plan(**values)


def _result(
    *ids: str, slices: tuple[int, ...] = (LOT, LOT), error: Any = None
) -> SlicedOrderResult:
    placed = LOT * len(ids)
    return SlicedOrderResult(
        requested_quantity=sum(slices),
        slice_quantities=slices,
        order_ids=tuple(ids),
        placed_quantity=placed,
        unsent_quantity=sum(slices) - placed,
        complete=len(ids) == len(slices),
        error=error,
    )


class SlicedClient(FakeClient):
    """``FakeClient`` with a scripted ``place_sliced_order``."""

    def __init__(self, books: list[Any], *, sliced_reply: Any = None, **kwargs: Any) -> None:
        super().__init__(books, **kwargs)
        self.sliced_reply = sliced_reply

    def place_sliced_order(self, **kwargs: Any) -> SlicedOrderResult:
        self.calls.append(("place_sliced_order", kwargs))
        if isinstance(self.sliced_reply, BaseException):
            raise self.sliced_reply
        assert isinstance(self.sliced_reply, SlicedOrderResult)
        return self.sliced_reply


def _o(order_id: str, status: str, **extra: str) -> Any:
    return _order(order_id, status, tag=TAG, **extra)


def _session(client: SlicedClient, *, left: int = 40, test_cap: int | None = 2) -> OrderSession:
    session = OrderSession(client, budget=OrderBudget(left), uat=True)  # type: ignore[arg-type]
    session.test_budget = None if test_cap is None else OrderBudget(test_cap)
    return session


def _run(session: OrderSession, plan: Any = None, **kwargs: Any) -> tuple[Any, dict[str, Any]]:
    t = FakeTime()
    record: dict[str, Any] = {}
    kwargs.setdefault("settle_seconds", 0.0)  # one look after placing, then the cancels
    kwargs.setdefault("poll_interval", 1.0)
    kwargs.setdefault("cancel_settle_seconds", 2.0)
    outcome = sliced.run_sliced(
        session,
        plan if plan is not None else _nifty_plan(),
        record,
        sleep=t.sleep,
        clock=t.clock,
        **kwargs,
    )
    return outcome, record


OPEN_BOTH = [_o("1", "OPEN"), _o("2", "OPEN")]
CANCELED_BOTH = [_o("1", "CANCELED"), _o("2", "CANCELED")]


# -- the session: UAT mode only, never funded mode ----------------------------------------


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"TRADESMART_ENV": "production"},
        {"TRADESMART_ENV": "stage"},
        {**UAT, "TRADESMART_LIVE_ORDER_FUNDED": "1"},
        {**UAT, "TRADESMART_LIVE_ORDER_FUNDED": "0"},
    ],
    ids=["unset", "production", "other", "funded", "funded-flag-any-value"],
)
def test_sliced_session_refuses_outside_uat_mode_without_opening_a_session(
    environ: dict[str, str],
) -> None:
    def open_session() -> OrderSession:
        raise AssertionError("no session may be opened")

    with pytest.raises(Refusal, match="UAT"):
        sliced.sliced_session(environ, open_session)


def test_sliced_session_refuses_a_session_that_is_not_in_uat_mode() -> None:
    session = OrderSession(SlicedClient([[]]))  # type: ignore[arg-type]
    with pytest.raises(Refusal, match="UAT"):
        sliced.sliced_session(UAT, lambda: session)


def test_sliced_session_refuses_a_funded_session() -> None:
    session = OrderSession(SlicedClient([[]]), uat=True, funded=True)  # type: ignore[arg-type]
    with pytest.raises(Refusal, match="funded"):
        sliced.sliced_session(UAT, lambda: session)


def test_sliced_session_returns_the_uat_mode_session() -> None:
    session = OrderSession(SlicedClient([[]]), uat=True)  # type: ignore[arg-type]
    assert sliced.sliced_session(UAT, lambda: session) is session


# -- refusals before any call --------------------------------------------------------------


@pytest.mark.parametrize(
    ("uat", "funded"), [(False, False), (False, True), (True, True)], ids=["prod", "funded", "both"]
)
def test_run_sliced_refuses_outside_uat_mode_before_any_call(uat: bool, funded: bool) -> None:
    client = SlicedClient([[]], sliced_reply=_result("1", "2"))
    session = OrderSession(client, uat=uat, funded=funded)  # type: ignore[arg-type]
    session.test_budget = OrderBudget(2)
    with pytest.raises(Refusal, match="UAT"):
        _run(session)
    assert client.calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"transaction_type": "SELL"},
        {"order_type": "SL-LMT", "trigger_price": Decimal("23500")},
        {"validity": "IOC"},
        {"price": Decimal("25900.00")},  # not well below the last price
        {"last_price": None},
        {"tag": "sdklive-nfo-abc123"},  # the suite's one-order tag: never shared by slices
    ],
    ids=["sell", "stop", "ioc", "near-market", "no-last-price", "suite-tag"],
)
def test_run_sliced_sends_only_a_far_day_limit_buy(overrides: dict[str, Any]) -> None:
    client = SlicedClient([[]], sliced_reply=_result("1", "2"))
    with pytest.raises(Refusal):
        _run(_session(client), _nifty_plan(**overrides))
    assert client.calls == []


@pytest.mark.parametrize(
    ("left", "test_cap"), [(1, 2), (40, 1), (40, None)], ids=["run-cap", "test-cap", "no-test-cap"]
)
def test_run_sliced_needs_room_for_every_slice_before_any_call(
    left: int, test_cap: int | None
) -> None:
    client = SlicedClient([[]], sliced_reply=_result("1", "2"))
    session = _session(client, left=left, test_cap=test_cap)
    with pytest.raises(Refusal, match="order cap|per-test"):
        _run(session)
    assert client.calls == []
    assert session.budget.used == 0


def test_run_sliced_refuses_when_the_account_has_an_open_order() -> None:
    client = SlicedClient([[_order("9", "OPEN", tag="manual")]], sliced_reply=_result("1", "2"))
    session = _session(client)
    with pytest.raises(Refusal, match="open order"):
        _run(session)
    assert client.count("place_sliced_order") == 0
    assert session.budget.used == 0


# -- the sliced order: two slices, both cancelled -----------------------------------------


def test_two_slices_are_placed_once_and_both_cancelled() -> None:
    client = SlicedClient([[], OPEN_BOTH, OPEN_BOTH, CANCELED_BOTH], sliced_reply=_result("1", "2"))
    session = _session(client)
    outcome, record = _run(session)

    assert client.count("place_sliced_order") == 1
    assert client.count("place_order") == 0
    (_, sent), *_ = [c for c in client.calls if c[0] == "place_sliced_order"]
    assert sent == {
        "exchange": "NFO",
        "tradingsymbol": "NIFTY25NOVFUT",
        "quantity": 2 * LOT,
        "transaction_type": "BUY",
        "product": "NRML",
        "order_type": "LIMIT",
        "price": Decimal("23400.00"),
        "validity": "DAY",
        "tag": TAG,
        "max_slice_quantity": LOT,
        "max_slices": 2,
    }
    assert [c[1] for c in client.calls if c[0] == "cancel_order"] == ["1", "2"]
    assert outcome.order_ids == ("1", "2")
    assert outcome.open_ids == ("1", "2")
    assert outcome.cancelled == ("1", "2")
    assert outcome.final_statuses == {"1": "CANCELED", "2": "CANCELED"}
    assert outcome.result.complete is True
    assert session.budget.used == 2 and session.test_budget is not None
    assert session.test_budget.used == 2
    place = record["sliced"]["place"]
    assert place["outcome"] == "complete"
    assert place["order_ids"] == ["1", "2"] and place["slice_quantities"] == [LOT, LOT]


def test_a_slice_already_final_is_not_cancelled() -> None:
    client = SlicedClient(
        [[], [_o("1", "OPEN"), _o("2", "REJECTED")], CANCELED_BOTH[:1] + [_o("2", "REJECTED")]],
        sliced_reply=_result("1", "2"),
    )
    outcome, _ = _run(_session(client))
    assert [c[1] for c in client.calls if c[0] == "cancel_order"] == ["1"]
    assert outcome.final_statuses == {"1": "CANCELED", "2": "REJECTED"}


def test_a_definite_stop_after_slice_one_cancels_the_placed_slice() -> None:
    rejected = OrderRejectedError(
        "PlaceOrder failed: RMS", endpoint="PlaceOrder", http_status=200, emsg="RMS"
    )
    client = SlicedClient(
        [[], [_o("1", "OPEN")], [_o("1", "CANCELED")]], sliced_reply=_result("1", error=rejected)
    )
    outcome, record = _run(_session(client))
    assert outcome.result.complete is False
    assert outcome.cancelled == ("1",)
    place = record["sliced"]["place"]
    assert place["outcome"] == "stopped"
    assert place["error"]["exception"] == "OrderRejectedError" and place["error"]["emsg"] == "RMS"


def test_an_ambiguous_slice_found_by_tag_is_cancelled_and_never_resent() -> None:
    unknown = OrderStatusUnknownError("PlaceOrder", "read timeout", sliced_result=_result("1"))
    client = SlicedClient([[], OPEN_BOTH, OPEN_BOTH, CANCELED_BOTH], sliced_reply=unknown)
    outcome, record = _run(_session(client))
    assert client.count("place_sliced_order") == 1 and client.count("place_order") == 0
    assert outcome.exception == "OrderStatusUnknownError"
    assert outcome.order_ids == ("1", "2")
    assert outcome.cancelled == ("1", "2")
    assert record["sliced"]["place"]["outcome"] == "unknown"
    assert record["sliced"]["found_by_tag"] == ["2"]


def test_an_ambiguous_slice_missing_from_the_book_is_check_the_account() -> None:
    unknown = OrderStatusUnknownError("PlaceOrder", "read timeout", sliced_result=_result("1"))
    client = SlicedClient(
        [[], [_o("1", "OPEN")], [_o("1", "OPEN")], [_o("1", "CANCELED")]], sliced_reply=unknown
    )
    with pytest.raises(CheckTheAccount, match=TAG):
        _run(_session(client))
    assert client.count("cancel_order") == 1  # the placed slice is still cancelled


def test_refused_before_sending_is_a_refusal_and_nothing_is_cancelled() -> None:
    client = SlicedClient([[], []], sliced_reply=ValueError("lot size mismatch for NIFTY"))
    with pytest.raises(Refusal, match="lot size mismatch"):
        _run(_session(client))
    assert client.count("cancel_order") == 0
    assert client.count("place_sliced_order") == 1


def test_a_non_ctrl_c_interruption_looks_up_the_tag_and_cancels_every_slice() -> None:
    interruption = SystemExit(1)
    interruption.sliced_result = _result("1")  # type: ignore[attr-defined]  # as the SDK attaches
    client = SlicedClient([[], OPEN_BOTH, CANCELED_BOTH], sliced_reply=interruption)
    with pytest.raises(SystemExit):
        _run(_session(client))
    # slice 2 is known only from the tag lookup; both are cancelled once
    assert [c[1] for c in client.calls if c[0] == "cancel_order"] == ["1", "2"]
    assert client.count("place_sliced_order") == 1


def test_an_interruption_without_a_partial_result_still_finds_the_tag() -> None:
    client = SlicedClient([[], OPEN_BOTH, CANCELED_BOTH], sliced_reply=SystemExit(1))
    with pytest.raises(SystemExit):
        _run(_session(client))
    assert [c[1] for c in client.calls if c[0] == "cancel_order"] == ["1", "2"]


def test_a_generic_exception_needs_every_slice_in_the_book() -> None:
    client = SlicedClient(
        [[], [_o("1", "OPEN")], [_o("1", "OPEN")], [_o("1", "CANCELED")]],
        sliced_reply=RuntimeError("boom"),
    )
    with pytest.raises(CheckTheAccount, match=TAG):
        _run(_session(client))
    assert client.count("cancel_order") == 1  # the slice found is still cancelled


def test_a_generic_exception_with_every_slice_in_the_book_is_not_an_alert() -> None:
    client = SlicedClient([[], OPEN_BOTH, OPEN_BOTH, CANCELED_BOTH], sliced_reply=RuntimeError("x"))
    outcome, record = _run(_session(client))
    assert outcome.order_ids == ("1", "2") and outcome.cancelled == ("1", "2")
    assert record["sliced"]["place"]["outcome"] == "unknown"


def test_a_rate_limit_configuration_refusal_is_a_refusal() -> None:
    from tradesmart.errors import ConfigurationError

    refusal = ConfigurationError("place_sliced_order: 3 slices ...; nothing was sent")
    client = SlicedClient([[], []], sliced_reply=refusal)
    with pytest.raises(Refusal, match="nothing was sent"):
        _run(_session(client))
    assert client.count("cancel_order") == 0


def test_a_cancel_that_does_not_settle_is_check_the_account() -> None:
    client = SlicedClient([[], OPEN_BOTH], sliced_reply=_result("1", "2"))
    with pytest.raises(CheckTheAccount, match="still"):
        _run(_session(client))
    assert client.count("cancel_order") == 2  # one request per slice, never retried


def test_a_filled_slice_is_check_the_account() -> None:
    client = SlicedClient(
        [[], [_o("1", "COMPLETE", fillshares="65"), _o("2", "OPEN")]]
        + [[_o("1", "COMPLETE", fillshares="65"), _o("2", "CANCELED")]],
        sliced_reply=_result("1", "2"),
        trades=[Trade.model_validate({"norenordno": "1", "flqty": "65"})],
    )
    with pytest.raises(CheckTheAccount, match="filled"):
        _run(_session(client))


def test_the_record_holds_no_account_id(tmp_path: Path) -> None:
    client = SlicedClient([[], OPEN_BOTH, CANCELED_BOTH], sliced_reply=_result("1", "2"))
    session = _session(client)
    _, record = _run(session)
    path = tmp_path / "live_orders.jsonl"
    Recorder(path).write(record)
    text = path.read_text(encoding="utf-8")
    assert client.client_id not in text
    assert json.loads(text)["sliced"]["place"]["order_ids"] == ["1", "2"]


def test_sliced_tags_are_unique_and_outside_the_suite_prefix() -> None:
    tags = {sliced.new_sliced_tag() for _ in range(50)}
    assert len(tags) == 50
    assert all(t.startswith(sliced.SLICED_TAG_PREFIX) and len(t) <= 20 for t in tags)
    assert not sliced.SLICED_TAG_PREFIX.startswith(safety.TAG_PREFIX)


# -- the live test module ------------------------------------------------------------------


def test_the_sliced_live_test_is_deselected_by_default_and_uat_only() -> None:
    import inspect

    from tests.live_orders import test_uat_sliced

    assert test_uat_sliced.pytestmark.name == "live_orders"
    tests = [v for n, v in vars(test_uat_sliced).items() if n.startswith("test_") and callable(v)]
    assert len(tests) == 1
    assert set(inspect.signature(tests[0]).parameters) == {"sliced_session", "sliced_record"}
    source = Path(test_uat_sliced.__file__).read_text(encoding="utf-8")
    assert "sliced.run_sliced(" in source and "sliced.sliced_session(" in source


class _Request:
    def __init__(self, session: OrderSession | None = None) -> None:
        self.session = session

    def getfixturevalue(self, name: str) -> Any:
        if self.session is None:
            raise AssertionError("no session may be opened")
        return self.session


def _fixture() -> Any:
    from tests.live_orders import test_uat_sliced

    return test_uat_sliced.sliced_session._get_wrapped_function()


@pytest.mark.parametrize(
    "environ",
    [{"TRADESMART_ENV": "production"}, {**UAT, "TRADESMART_LIVE_ORDER_FUNDED": "1"}],
    ids=["production", "funded"],
)
def test_the_sliced_fixture_skips_outside_uat_without_opening_the_session(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]
) -> None:
    monkeypatch.delenv("TRADESMART_ENV", raising=False)
    monkeypatch.delenv("TRADESMART_LIVE_ORDER_FUNDED", raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(pytest.skip.Exception):
        _fixture()(_Request())
