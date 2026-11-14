"""Mocked unit tests pinning the safety core of the UAT live-order matrix.

``tests/live_orders/uat_matrix/`` (gate, ledger, guard, never-resend calls). Nothing here touches
the network: ``MatrixClient`` below stands in for ``TradeSmart``; every file goes to ``tmp_path``.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.live_orders.safety import CHECK_THE_ACCOUNT, Refusal
from tests.live_orders.uat_matrix import calls, gate
from tests.live_orders.uat_matrix.calls import CaseRef
from tests.live_orders.uat_matrix.guard import (
    MAX_OPEN,
    STOP_REASON,
    Guard,
    MaxOpenReached,
    StopRun,
)
from tests.live_orders.uat_matrix.ledger import TAG_PREFIX, Ledger, check_open_orders, redact
from tradesmart import (
    ClientConfig,
    NetworkError,
    OrderRejectedError,
    OrderStatusUnknownError,
    RequestValidationError,
    SlicedOrderResult,
)
from tradesmart.models.base import IST
from tradesmart.models.orders import Order
from tradesmart.models.portfolio import Limits
from tradesmart.models.user import UserDetails

UAT_URL = "https://uat.example.test/NorenWClientAPIv2"
PRODUCTION_URL = "https://v2api.tradesmartonline.in/NorenWClientAPIv2"
ACCOUNT = "TEST01"
UAT_ENV = {
    "TRADESMART_LIVE_TESTS": "1",
    "TRADESMART_LIVE_ORDER_TESTS": "1",
    "TRADESMART_ENV": "uat",
    "TRADESMART_LIVE_ORDER_ACCOUNT": ACCOUNT,
}
MATRIX_ENV = {**UAT_ENV, "TRADESMART_LIVE_ORDER_UAT_MATRIX": "1"}
NOW = datetime(2026, 11, 13, 10, 0, tzinfo=IST)
NSE = CaseRef("m1-nse-itc-cnc-lmt-day-buy", "nse")
PLAN: dict[str, Any] = {
    "exchange": "NSE",
    "tradingsymbol": "ITC-EQ",
    "quantity": 1,
    "transaction_type": "BUY",
    "product": "CNC",
    "order_type": "LIMIT",
    "price": Decimal("360.00"),
    "validity": "DAY",
}


def _order(order_id: str, status: str, tag: str = "", **extra: str) -> Order:
    return Order.model_validate(
        {"norenordno": order_id, "status": status, "remarks": tag, "exch": "NSE", **extra}
    )


class MatrixClient:
    """Scripted stand-in for TradeSmart: each orders() call pops the next book (the last
    repeats); a book may be a callable of the calls so far (to see the tags sent)."""

    client_id = ACCOUNT

    def __init__(
        self,
        books: list[Any] | None = None,
        *,
        place: list[Any] | None = None,
        modify: Any = "1001",
        cancel: Any = None,
        exit_: Any = None,
        sliced: Any = None,
        base_url: str = UAT_URL,
        on_call: Any = None,
    ) -> None:
        self.books = books if books is not None else [[]]
        self.place_results = place if place is not None else ["1001"]
        self.modify_result = modify
        self.cancel_result = cancel
        self.exit_result = exit_
        self.sliced_result = sliced
        self.config = ClientConfig(base_url=base_url)
        self.on_call = on_call
        self.calls: list[tuple[str, Any]] = []
        # The UAT preflight (``safety.uat_preflight``): every limit at the minimum by default.
        self.limits: Any = {
            "stat": "Ok",
            "cash": "100000000",
            "turnoverlmt": "100000000",
            "pendordvallmt": "100000000",
        }

    def _record(self, name: str, args: Any) -> None:
        self.calls.append((name, args))
        if self.on_call is not None:
            self.on_call(name, args)

    def profile(self) -> UserDetails:
        self._record("profile", None)
        return UserDetails.model_validate({"stat": "Ok", "uid": ACCOUNT})

    def margins(self) -> Limits:
        self._record("margins", None)
        return Limits.model_validate(self.limits)

    def orders(self) -> list[Order]:
        self._record("orders", None)
        book = self.books.pop(0) if len(self.books) > 1 else self.books[0]
        if callable(book):
            book = book(self)
        if isinstance(book, Exception):
            raise book
        result: list[Order] = book
        return result

    def place_order(self, **kwargs: Any) -> str:
        self._record("place_order", kwargs)
        result = self.place_results.pop(0) if len(self.place_results) > 1 else self.place_results[0]
        if isinstance(result, BaseException):
            raise result
        return str(result)

    def place_sliced_order(self, **kwargs: Any) -> SlicedOrderResult:
        self._record("place_sliced_order", kwargs)
        if isinstance(self.sliced_result, BaseException):
            raise self.sliced_result
        result: SlicedOrderResult = self.sliced_result
        return result

    def modify_order(self, order_id: str, **kwargs: Any) -> str:
        self._record("modify_order", (order_id, kwargs))
        if isinstance(self.modify_result, BaseException):
            raise self.modify_result
        return str(self.modify_result)

    def cancel_order(self, order_id: str) -> str:
        self._record("cancel_order", order_id)
        if isinstance(self.cancel_result, BaseException):
            raise self.cancel_result
        return order_id

    def exit_order(self, order_id: str, product: str) -> str:
        self._record("exit_order", (order_id, product))
        if isinstance(self.exit_result, BaseException):
            raise self.exit_result
        return order_id

    def count(self, name: str) -> int:
        return sum(1 for call, _ in self.calls if call == name)

    def sent_tags(self) -> list[str]:
        return [args["tag"] for name, args in self.calls if name.startswith("place")]


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _session(
    client: MatrixClient,
    tmp_path: Path,
    environ: dict[str, str] | None = None,
    now: datetime = NOW,
) -> gate.MatrixSession:
    t = FakeTime()
    return gate.matrix_session(
        environ if environ is not None else MATRIX_ENV,
        client,  # type: ignore[arg-type]
        root=tmp_path,
        now=now,
        sleep=t.sleep,
        clock=t.clock,
    )


def _lines(session: gate.MatrixSession) -> list[dict[str, Any]]:
    text = session.ledger.path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def _unknown(endpoint: str = "PlaceOrder") -> OrderStatusUnknownError:
    return OrderStatusUnknownError(endpoint, "read timeout")


def _rejected(message: str = "RMS: rejected") -> OrderRejectedError:
    return OrderRejectedError(
        f"failed: {message}",
        endpoint="PlaceOrder",
        http_status=200,
        emsg=message,
        raw={"stat": "Not_Ok", "emsg": message},
    )


# -- gate -------------------------------------------------------------------------------------


@pytest.mark.parametrize("tier", [gate.FULL, gate.STRESS])
def test_full_and_stress_need_the_matrix_opt_in(tier: str) -> None:
    reason = gate.gate_refusal(UAT_ENV, UAT_URL, tier, markexpr="stress")
    assert reason is not None and "TRADESMART_LIVE_ORDER_UAT_MATRIX=1" in reason
    for value in ("0", "true", "", "yes"):
        env = {**UAT_ENV, "TRADESMART_LIVE_ORDER_UAT_MATRIX": value}
        assert gate.gate_refusal(env, UAT_URL, tier, markexpr="stress") is not None


def test_smoke_runs_with_the_existing_uat_opt_ins_only() -> None:
    assert gate.gate_refusal(UAT_ENV, UAT_URL, gate.SMOKE) is None


def test_full_runs_with_the_matrix_opt_in() -> None:
    assert gate.gate_refusal(MATRIX_ENV, UAT_URL, gate.FULL) is None


def test_stress_runs_only_when_selected_by_marker() -> None:
    reason = gate.gate_refusal(MATRIX_ENV, UAT_URL, gate.STRESS)
    assert reason is not None and "-m stress" in reason
    assert gate.gate_refusal(MATRIX_ENV, UAT_URL, gate.STRESS, markexpr="stress") is None


@pytest.mark.parametrize("tier", list(gate.TIERS))
@pytest.mark.parametrize("value", ["1", "0", "", "no"])
def test_any_funded_flag_refuses_every_tier(tier: str, value: str) -> None:
    env = {**MATRIX_ENV, "TRADESMART_LIVE_ORDER_FUNDED": value}
    reason = gate.gate_refusal(env, UAT_URL, tier, markexpr="stress")
    assert reason is not None and "TRADESMART_LIVE_ORDER_FUNDED" in reason


@pytest.mark.parametrize("tier", list(gate.TIERS))
@pytest.mark.parametrize(
    "url",
    [
        PRODUCTION_URL,
        "https://v2api.tradesmartonline.in.uat.example.test/x",
        "https://uat-v2api.tradesmartonline.in/x",
        "https://example.test/uat/x",
        "not a url",
    ],
)
def test_production_or_non_uat_hosts_refuse_every_tier(tier: str, url: str) -> None:
    reason = gate.gate_refusal(MATRIX_ENV, url, tier, markexpr="stress")
    assert reason is not None


@pytest.mark.parametrize("name", ["production", "", "staging"])
def test_a_non_uat_environment_name_refuses(name: str) -> None:
    env = {**MATRIX_ENV, "TRADESMART_ENV": name}
    assert gate.gate_refusal(env, UAT_URL, gate.SMOKE) is not None


@pytest.mark.parametrize(
    "env",
    [
        {k: v for k, v in UAT_ENV.items() if k != "TRADESMART_LIVE_ORDER_TESTS"},
        {k: v for k, v in UAT_ENV.items() if k != "TRADESMART_LIVE_TESTS"},
        {**UAT_ENV, "PYTEST_XDIST_WORKER": "gw0"},
    ],
)
def test_the_existing_order_gates_still_apply(env: dict[str, str]) -> None:
    assert gate.gate_refusal(env, UAT_URL, gate.SMOKE) is not None


def test_tier_of_markers() -> None:
    assert gate.tier_of({"uat_matrix", "smoke"}) == gate.SMOKE
    assert gate.tier_of({"uat_matrix", "stress", "smoke"}) == gate.STRESS
    assert gate.tier_of({"uat_matrix", "m_shapes"}) == gate.FULL


def test_collection_refusal_reads_the_host_from_the_environment() -> None:
    env = {**UAT_ENV, "TRADESMART_ENV_BASE_URL": UAT_URL}
    assert gate.collection_refusal(env, gate.SMOKE) is None
    env = {**UAT_ENV, "TRADESMART_ENV_BASE_URL": PRODUCTION_URL}
    assert gate.collection_refusal(env, gate.SMOKE) is not None
    assert gate.collection_refusal(UAT_ENV, gate.SMOKE) is not None  # no host: refused


def test_matrix_items_are_the_package_files_only() -> None:
    assert gate.is_matrix_item("tests/live_orders/uat_matrix/test_m1.py::test_x[nse]")
    assert gate.is_matrix_item("tests\\live_orders\\uat_matrix\\test_m1.py::test_x")
    assert not gate.is_matrix_item("tests/live_orders/test_uat_lifecycle.py::test_x")
    assert not gate.is_matrix_item("tests/unit/test_x.py::test_y[tests/live_orders/uat_matrix/]")


@pytest.mark.parametrize(
    ("environ", "url"),
    [
        (UAT_ENV, PRODUCTION_URL),
        ({**UAT_ENV, "TRADESMART_LIVE_ORDER_FUNDED": "0"}, UAT_URL),
        ({**UAT_ENV, "TRADESMART_ENV": "production"}, UAT_URL),
    ],
    ids=["production-host", "funded-0", "production-env"],
)
def test_matrix_session_refuses_before_any_call(
    tmp_path: Path, environ: dict[str, str], url: str
) -> None:
    client = MatrixClient(base_url=url)
    with pytest.raises(Refusal):
        _session(client, tmp_path, environ)
    assert client.calls == []
    assert list(tmp_path.iterdir()) == []


def test_matrix_session_opens_a_smoke_session_without_the_matrix_opt_in(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path, UAT_ENV)
    assert session.matrix_opt_in is False
    assert session.item_refusal(gate.SMOKE) is None
    reason = session.item_refusal(gate.FULL)
    assert reason is not None and "TRADESMART_LIVE_ORDER_UAT_MATRIX=1" in reason


def test_matrix_session_refuses_another_account(tmp_path: Path) -> None:
    client = MatrixClient()
    env = {**MATRIX_ENV, "TRADESMART_LIVE_ORDER_ACCOUNT": "OTHER9"}
    with pytest.raises(Refusal, match="TRADESMART_LIVE_ORDER_ACCOUNT"):
        _session(client, tmp_path, env)
    assert client.count("orders") == 0


def test_matrix_session_creates_the_run_folder_and_ledger(tmp_path: Path) -> None:
    session = _session(MatrixClient(), tmp_path)
    assert session.run_dir == tmp_path / session.run_id
    assert session.ledger.path == session.run_dir / "ledger.jsonl"
    run = json.loads((session.run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["run_id"] == session.run_id and run["trading_day"] == "2026-11-13"
    assert session.trading_day == date(2026, 11, 13) and session.resumed is False
    assert session.guard.stop_file == tmp_path / "STOP"
    assert ACCOUNT not in (session.run_dir / "run.json").read_text(encoding="utf-8")


def test_matrix_session_refuses_an_open_order_not_in_the_ledger(tmp_path: Path) -> None:
    client = MatrixClient([[_order("777", "OPEN", tag="by-hand")]])
    with pytest.raises(StopRun, match="777"):
        _session(client, tmp_path)
    assert client.count("cancel_order") == 0


def test_matrix_session_skips_ignored_stuck_orders(tmp_path: Path) -> None:
    client = MatrixClient([[_order("777", "PENDING", tag="old")]])
    env = {**MATRIX_ENV, "TRADESMART_LIVE_ORDER_UAT_IGNORE_OPEN": "777"}
    session = _session(client, tmp_path, env)
    assert session.ignored_open == frozenset({"777"})


# -- resume (Review Focus 3) ------------------------------------------------------------------


def test_resume_on_the_same_trading_day_reuses_the_run(tmp_path: Path) -> None:
    first = _session(MatrixClient(), tmp_path)
    tag = first.ledger.new_tag(NSE.case_id)
    first.ledger.intent(tag, NSE.case_id, "place", segment="nse")
    first.ledger.outcome(tag, "1001", "acknowledged", kind="place")
    first.ledger.verdict(NSE.case_id, "passed")
    gate.release_run_lock(first)  # the first process ended (one process per run id)
    env = {**MATRIX_ENV, "TRADESMART_UAT_RUN_ID": first.run_id}
    client = MatrixClient([[_order("1001", "OPEN", tag=tag)]])  # ours: allowed
    later = datetime(2026, 11, 13, 14, 0, tzinfo=IST)
    second = _session(client, tmp_path, env, now=later)
    assert second.run_id == first.run_id and second.resumed is True
    assert second.ledger.ours() == {"1001"}
    assert second.ledger.done(NSE.case_id) == "passed"
    assert second.ledger.new_tag(NSE.case_id) != tag  # never a tag of the earlier process


def test_resume_on_another_trading_day_is_refused_before_any_call(tmp_path: Path) -> None:
    first = _session(MatrixClient(), tmp_path)
    env = {**MATRIX_ENV, "TRADESMART_UAT_RUN_ID": first.run_id}
    client = MatrixClient()
    next_day = datetime(2026, 11, 14, 9, 30, tzinfo=IST)
    with pytest.raises(Refusal, match="same trading day"):
        _session(client, tmp_path, env, now=next_day)
    assert client.calls == []


def test_resume_uses_the_ist_date_not_utc(tmp_path: Path) -> None:
    first = _session(MatrixClient(), tmp_path)
    env = {**MATRIX_ENV, "TRADESMART_UAT_RUN_ID": first.run_id}
    # 2026-11-13 20:00 UTC is 2026-11-14 01:30 IST: another trading day.
    late = datetime(2026, 11, 13, 20, 0, tzinfo=UTC)
    client = MatrixClient()
    with pytest.raises(Refusal, match="same trading day"):
        _session(client, tmp_path, env, now=late)
    assert client.calls == []


@pytest.mark.parametrize("run_id", ["../x", "a/b", "", "X" * 40, "missing1"])
def test_resume_of_a_bad_or_unknown_run_id_is_refused_before_any_call(
    tmp_path: Path, run_id: str
) -> None:
    env = {**MATRIX_ENV, "TRADESMART_UAT_RUN_ID": run_id}
    client = MatrixClient()
    with pytest.raises(Refusal):
        _session(client, tmp_path, env)
    assert client.calls == []


# -- ledger -----------------------------------------------------------------------------------


def test_tags_are_short_unique_and_map_to_the_full_case_id(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "r" / "ledger.jsonl", "1113abcd")
    long_case = "m1-nfo-niftyfut-nrml-sl-lmt-day-sell-otm-k2-weekly"
    tags = [ledger.new_tag(long_case) for _ in range(3)]
    other = ledger.new_tag("m1-nse-itc-cnc-lmt-day-buy")
    for tag in [*tags, other]:
        assert tag.startswith(f"{TAG_PREFIX}1113abcd-")
        assert len(tag) <= 30
    assert len({*tags, other}) == 4
    assert {ledger.case_of(tag) for tag in tags} == {long_case}
    reopened = Ledger(ledger.path, "1113abcd")
    assert reopened.case_key(long_case) == ledger.case_key(long_case)


def test_ledger_redacts_account_ids_at_any_depth(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", "1113abcd", forbidden=[ACCOUNT])
    ledger.intent(
        "t1",
        "case",
        "place",
        segment="nse",
        response={
            "uid": ACCOUNT,
            "actid": ACCOUNT,
            "rows": [{"client_id": ACCOUNT, "user_id": "x", "account_id": "y", "ok": 1}],
            "note": f"order of {ACCOUNT} accepted",
        },
    )
    text = ledger.path.read_text(encoding="utf-8")
    assert ACCOUNT not in text
    for key in ("uid", "actid", "client_id", "user_id", "account_id"):
        assert f'"{key}"' not in text
    assert '"ok": 1' in text


def test_redact_keeps_ordinary_fields() -> None:
    assert redact({"norenordno": "1", "token": "2885", "UID": "A"}, ()) == {
        "norenordno": "1",
        "token": "2885",
    }


def test_ledger_lines_are_flushed_json(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", "1113abcd")
    ledger.intent("t1", "case", "place", segment="nse")
    ledger.outcome("t1", "1001", "acknowledged", kind="place")
    lines = [json.loads(x) for x in ledger.path.read_text(encoding="utf-8").splitlines()]
    assert [x["type"] for x in lines if x["type"] != "case"] == ["intent", "outcome"]
    assert all("at" in x for x in lines)


def test_ledger_ignores_a_torn_last_line_on_reopen(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", "1113abcd")
    ledger.intent("t1", "case", "place", segment="nse")
    ledger.outcome("t1", "1001", "acknowledged", kind="place")
    with ledger.path.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "outc')
    assert Ledger(ledger.path, "1113abcd").ours() == {"1001"}


def test_ledger_written_before_and_after_the_place_call(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def on_call(name: str, args: Any) -> None:
        if name == "place_order":
            text = session.ledger.path.read_text(encoding="utf-8")
            seen.append([json.loads(x)["type"] for x in text.splitlines()])

    client = MatrixClient(on_call=on_call)
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    outcome = calls.place(session, PLAN, NSE)
    assert seen and seen[0][-1] == "intent"
    last = _lines(session)[-1]
    assert last["type"] == "outcome" and last["order_id"] == "1001" == outcome.order_id
    assert last["verdict"] == calls.ACKNOWLEDGED
    intent = next(x for x in _lines(session) if x["type"] == "intent")
    assert intent["tag"] == outcome.tag == client.sent_tags()[0]
    assert intent["case_id"] == NSE.case_id and intent["segment"] == "nse"


@pytest.mark.parametrize("kind", ["modify", "cancel", "exit"])
def test_ledger_written_before_and_after_modify_cancel_exit(tmp_path: Path, kind: str) -> None:
    seen: list[str] = []

    def on_call(name: str, args: Any) -> None:
        if name in ("modify_order", "cancel_order", "exit_order"):
            text = session.ledger.path.read_text(encoding="utf-8")
            seen.append(json.loads(text.splitlines()[-1])["type"])

    client = MatrixClient(on_call=on_call)
    session = _session(client, tmp_path)
    tag = _placed(session, client)
    if kind == "modify":
        calls.modify(session, "1001", NSE, price=Decimal("359.95"), **_modify_kwargs())
    elif kind == "cancel":
        calls.cancel(session, "1001", NSE)
    else:
        calls.exit(session, "1001", "CO", NSE)
    assert seen == ["intent"]
    last, intent = _lines(session)[-1], _lines(session)[-2]
    assert last["type"] == "outcome" and last["kind"] == kind
    assert intent["type"] == "intent" and intent["tag"] == tag  # the ledger's tag of the order
    assert last["call"] == intent["call"]  # the outcome echoes its intent's call id


def _placed(session: gate.MatrixSession, client: MatrixClient, order_id: str = "1001") -> str:
    """Place one order through ``calls.place`` (its own case) so ``order_id`` is ours."""
    client.place_results = [order_id]
    case = CaseRef(f"setup-{order_id}", "nse")
    session.guard.reserve(case.case_id, 1)
    outcome = calls.place(session, PLAN, case)
    assert outcome.order_id == order_id
    return outcome.tag


def _modify_kwargs() -> dict[str, Any]:
    return {
        "exchange": "NSE",
        "tradingsymbol": "ITC-EQ",
        "quantity": 1,
        "order_type": "LIMIT",
        "product": "CNC",
    }


# -- check_open_orders and Review Focus 1 -----------------------------------------------------


def test_open_orders_of_this_run_or_ignored_pass(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", "1113abcd")
    ledger.intent("tagA", "case", "place", segment="nse")
    ledger.outcome("tagA", "1001", "acknowledged", kind="place")
    ledger.intent("tagB", "case", "place", segment="nse")  # no outcome yet: ours by tag
    book = [
        _order("1001", "OPEN", tag="tagA"),
        _order("1002", "TRIGGER_PENDING", tag="tagB"),
        _order("900", "PENDING", tag="stuck"),
        _order("901", "COMPLETE", tag="someone"),  # final: not open
    ]
    client = MatrixClient([book])
    guard = Guard(tmp_path / "STOP")
    check_open_orders(client, ledger, frozenset({"900"}), guard=guard)  # type: ignore[arg-type]
    assert guard.open_ids == {"1001", "1002"}


def test_an_open_order_not_in_the_ledger_stops_the_run(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", "1113abcd")
    client = MatrixClient([[_order("555", "OPEN", tag="by-hand")]])
    with pytest.raises(StopRun) as info:
        check_open_orders(client, ledger, frozenset())  # type: ignore[arg-type]
    assert info.value.foreign == ("555",)
    assert CHECK_THE_ACCOUNT in info.value.reason


def test_an_unreadable_book_stops_the_run(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", "1113abcd")
    client = MatrixClient([NetworkError("down")])
    with pytest.raises(StopRun, match="could not be read"):
        check_open_orders(client, ledger, frozenset(), sleep=lambda s: None)  # type: ignore[arg-type]
    assert client.count("orders") == 3


def test_someone_trading_by_hand_stops_the_run_and_only_ours_are_cancelled(
    tmp_path: Path,
) -> None:
    """Review Focus 1: a foreign open order → the run stops before the next batch, ours are
    cancelled once and confirmed, the foreign order is never touched, no new order is sent."""
    client = MatrixClient()
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 3)
    first = calls.place(session, PLAN, NSE)
    assert first.order_id == "1001"
    ours = _order("1001", "OPEN", tag=first.tag)
    foreign = _order("555", "OPEN", tag="by-hand")
    client.books = [[ours, foreign], [ours, foreign], [_order("1001", "CANCELED", tag=first.tag)]]
    with pytest.raises(StopRun) as info:
        check_open_orders(client, session.ledger, session.ignored_open)  # type: ignore[arg-type]
    report = calls.stop_run(session, info.value)
    assert client.calls.count(("cancel_order", "1001")) == 1
    assert ("cancel_order", "555") not in client.calls
    assert report.cancelled == ["1001"] and report.not_final == []
    assert report.untouched == ["555"]
    assert session.guard.stopped() is True
    assert session.item_refusal(gate.SMOKE) == info.value.reason
    places = client.count("place_order")
    with pytest.raises(StopRun):
        calls.place(session, PLAN, NSE)
    assert client.count("place_order") == places


# -- STOP file (Review Focus 2) ---------------------------------------------------------------


def test_the_stop_file_mid_batch_stops_new_places_and_the_batch_is_cancelled(
    tmp_path: Path,
) -> None:
    client = MatrixClient(place=["1001", "1002", "1003"])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 3)
    a = calls.place(session, PLAN, NSE)
    b = calls.place(session, PLAN, NSE)
    (tmp_path / "STOP").write_text("", encoding="utf-8")
    with pytest.raises(StopRun, match=STOP_REASON):
        calls.place(session, PLAN, NSE)
    assert client.count("place_order") == 2
    client.books = [
        [_order("1001", "OPEN", tag=a.tag), _order("1002", "OPEN", tag=b.tag)],
        [_order("1001", "CANCELED", tag=a.tag), _order("1002", "CANCELED", tag=b.tag)],
    ]
    report = calls.cancel_ours(session)
    assert sorted(report.cancelled) == ["1001", "1002"] and report.not_final == []
    assert session.item_refusal(gate.SMOKE) == STOP_REASON
    with pytest.raises(StopRun):
        calls.modify(session, "1001", NSE, price=Decimal("1"), **_modify_kwargs())
    assert client.count("modify_order") == 0


def test_the_stop_file_is_checked_before_every_place(tmp_path: Path) -> None:
    guard = Guard(tmp_path / "STOP")
    assert guard.stopped() is False
    (tmp_path / "STOP").write_text("", encoding="utf-8")
    assert guard.stopped() is True
    (tmp_path / "STOP").unlink()
    assert guard.stopped() is True  # sticky: a run once stopped stays stopped
    assert guard.stop_reason == STOP_REASON


def test_a_not_final_order_after_the_batch_cancel_trips_the_segment(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    a = calls.place(session, PLAN, NSE)
    client.books = [[_order("1001", "OPEN", tag=a.tag)]]
    report = calls.cancel_ours(session)
    assert report.not_final == ["1001"]
    assert client.count("cancel_order") == 1
    assert session.guard.tripped("nse") is not None
    assert any(x.get("type") == "stuck" for x in _lines(session))


# -- declared counts, MAX_OPEN, breakers ------------------------------------------------------


def test_a_case_cannot_exceed_its_declared_count(tmp_path: Path) -> None:
    client = MatrixClient(place=["1001", "1002"])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    calls.place(session, PLAN, NSE)
    with pytest.raises(Refusal, match="declared"):
        calls.place(session, PLAN, NSE)
    assert client.count("place_order") == 1


def test_an_undeclared_case_sends_nothing(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    with pytest.raises(Refusal, match="declared"):
        calls.place(session, PLAN, NSE)
    assert client.count("place_order") == 0
    # No intent: only the session's own UAT preflight line is in the ledger.
    assert [line["type"] for line in _lines(session)] == ["uat_preflight"]


def test_reserve_is_once_per_case_and_positive() -> None:
    guard = Guard(Path("unused-STOP"))
    guard.reserve("a", 2)
    with pytest.raises(ValueError, match="already declared"):
        guard.reserve("a", 1)
    with pytest.raises(ValueError):
        guard.reserve("b", 0)


def test_sliced_order_spends_its_slices_from_the_declared_count(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 2)
    with pytest.raises(Refusal, match="declared"):
        calls.place_sliced(session, PLAN, NSE, max_slices=3, max_slice_quantity=1)
    assert client.count("place_sliced_order") == 0


def test_max_open_reached_refuses_before_sending(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    session.guard.opened(*(str(i) for i in range(MAX_OPEN)))
    assert MAX_OPEN == 50
    with pytest.raises(MaxOpenReached):
        calls.place(session, PLAN, NSE)
    assert client.count("place_order") == 0
    assert session.guard.used.get(NSE.case_id, 0) == 0  # nothing spent


def test_max_open_waits_for_room_from_the_book(tmp_path: Path) -> None:
    t = FakeTime()
    guard = Guard(tmp_path / "STOP", max_open=2)
    guard.opened("1", "2")
    seen = {"1", "2"}
    reads = iter([({"1", "2"}, seen), ({"2"}, seen)])  # (our open ids, every id listed)
    guard.wait_for_room(
        1, lambda: next(reads), timeout=10, poll_interval=1, sleep=t.sleep, clock=t.clock
    )
    assert guard.open_ids == {"2"} and t.now == 2


def test_max_open_that_never_frees_raises_and_the_batch_is_cancelled(tmp_path: Path) -> None:
    client = MatrixClient(place=["1001", "1002"])
    session = _session(client, tmp_path)
    session.guard.max_open = 2
    session.guard.reserve(NSE.case_id, 3)
    a = calls.place(session, PLAN, NSE)
    b = calls.place(session, PLAN, NSE)
    rows = [_order("1001", "OPEN", tag=a.tag), _order("1002", "OPEN", tag=b.tag)]
    client.books = [rows]
    with pytest.raises(MaxOpenReached):
        session.guard.wait_for_room(
            1,
            lambda: calls.open_ours(session),
            timeout=3,
            poll_interval=1,
            sleep=session.sleep,
            clock=session.clock,
        )
    client.books = [
        rows,
        [_order("1001", "CANCELED", tag=a.tag), _order("1002", "CANCELED", tag=b.tag)],
    ]
    report = calls.cancel_ours(session)
    assert sorted(report.cancelled) == ["1001", "1002"]
    assert session.guard.open_count == 0


def test_breaker_trips_after_one_stuck_order(tmp_path: Path) -> None:
    guard = Guard(tmp_path / "STOP")
    guard.note_stuck("bse", "1001")
    assert guard.tripped("bse") is not None and guard.tripped("nse") is None
    with pytest.raises(Refusal, match="bse"):
        guard.check_send("bse")


def test_breaker_trips_after_one_check_the_account(tmp_path: Path) -> None:
    guard = Guard(tmp_path / "STOP")
    guard.note_check_the_account("nfo", "sdkuat-x-001-1")
    reason = guard.tripped("nfo")
    assert reason is not None and CHECK_THE_ACCOUNT in reason


def test_breaker_trips_after_three_unexpected_rejections_in_a_row(tmp_path: Path) -> None:
    guard = Guard(tmp_path / "STOP")
    guard.note_rejection("nse", expected=False)
    guard.note_rejection("nse", expected=False)
    guard.note_rejection("nse", expected=True)  # an expected outcome resets the run
    guard.note_rejection("nse", expected=False)
    guard.note_rejection("nse", expected=False)
    assert guard.tripped("nse") is None
    guard.note_rejection("nse", expected=False)
    assert guard.tripped("nse") is not None
    assert guard.tripped("nfo") is None


def test_a_tripped_segment_sends_nothing_new_but_may_cancel(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    _placed(session, client)
    session.guard.reserve(NSE.case_id, 1)
    session.guard.trip("nse", "test")
    with pytest.raises(Refusal, match="circuit breaker"):
        calls.place(session, PLAN, NSE)
    with pytest.raises(Refusal, match="circuit breaker"):
        calls.modify(session, "1001", NSE, price=Decimal("1"), **_modify_kwargs())
    calls.cancel(session, "1001", NSE)
    assert client.count("place_order") == 1 and client.count("modify_order") == 0
    assert client.count("cancel_order") == 1


def test_trip_keeps_the_first_reason(tmp_path: Path) -> None:
    guard = Guard(tmp_path / "STOP")
    guard.trip("nse", "first")
    guard.trip("nse", "second")
    assert guard.tripped("nse") == "first"


# -- never resend: place ----------------------------------------------------------------------


def test_place_rejection_is_definitive_and_never_resent(tmp_path: Path) -> None:
    client = MatrixClient(place=[_rejected()])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    outcome = calls.place(session, PLAN, NSE)
    assert outcome.verdict == calls.REJECTED and outcome.order_id is None
    assert outcome.exception == "OrderRejectedError"
    assert client.count("place_order") == 1 and client.count("orders") == 1  # the session check


def test_place_client_side_validation_is_not_sent(tmp_path: Path) -> None:
    client = MatrixClient(place=[RequestValidationError("bad price")])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    outcome = calls.place(session, PLAN, NSE)
    assert outcome.verdict == calls.NOT_SENT and outcome.sent is False


@pytest.mark.parametrize("error", [_unknown(), NetworkError("reset")])
def test_ambiguous_place_is_found_by_tag_and_never_resent(tmp_path: Path, error: Exception) -> None:
    def book(c: MatrixClient) -> list[Order]:
        return [_order("1009", "OPEN", tag=c.sent_tags()[0])]

    client = MatrixClient(place=[error])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    client.books = [[], book]
    outcome = calls.place(session, PLAN, NSE)
    assert client.count("place_order") == 1
    assert outcome.verdict == calls.FOUND and outcome.order_id == "1009"
    assert outcome.status == "OPEN"
    assert session.ledger.ours() == {"1009"}
    assert session.guard.open_ids == {"1009"}
    assert session.guard.tripped("nse") is None


def test_ambiguous_place_not_found_is_check_the_account_and_trips(tmp_path: Path) -> None:
    client = MatrixClient(place=[_unknown()])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 2)
    outcome = calls.place(session, PLAN, NSE)
    assert client.count("place_order") == 1
    assert client.count("orders") == 1 + session.lookup_attempts
    assert outcome.verdict == CHECK_THE_ACCOUNT and outcome.check_the_account
    reason = session.guard.tripped("nse")
    assert reason is not None and outcome.tag in reason
    assert _lines(session)[-1]["verdict"] == CHECK_THE_ACCOUNT
    with pytest.raises(Refusal, match="circuit breaker"):
        calls.place(session, PLAN, NSE)
    assert client.count("place_order") == 1


def test_ambiguous_place_with_an_unreadable_book_is_check_the_account(tmp_path: Path) -> None:
    client = MatrixClient(place=[_unknown()])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    client.books = [NetworkError("down")]
    outcome = calls.place(session, PLAN, NSE)
    assert outcome.verdict == CHECK_THE_ACCOUNT
    assert client.count("place_order") == 1


def test_place_sets_its_own_unique_tag(tmp_path: Path) -> None:
    client = MatrixClient(place=["1001", "1002"])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 2)
    calls.place(session, PLAN, NSE)
    calls.place(session, PLAN, NSE)
    tags = client.sent_tags()
    assert len(set(tags)) == 2 and all(t.startswith(TAG_PREFIX + session.run_id) for t in tags)
    with pytest.raises(ValueError, match="tag"):
        calls.place(session, {**PLAN, "tag": "mine"}, NSE)


def test_place_accepts_a_tag_issued_by_the_ledger_once(tmp_path: Path) -> None:
    client = MatrixClient(place=["1001", "1002"])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 2)
    tag = session.ledger.new_tag(NSE.case_id)
    assert calls.place(session, PLAN, NSE, tag=tag).tag == tag
    with pytest.raises(ValueError, match="already"):
        calls.place(session, PLAN, NSE, tag=tag)
    with pytest.raises(ValueError, match="not issued"):
        calls.place(session, PLAN, NSE, tag="sdkuat-other-001-1")
    assert client.count("place_order") == 1


def test_place_accepts_a_plan_object_with_place_kwargs(tmp_path: Path) -> None:
    class Plan:
        def place_kwargs(self) -> dict[str, Any]:
            return dict(PLAN)

    client = MatrixClient()
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    calls.place(session, Plan(), NSE)
    assert client.calls[-1][1]["tradingsymbol"] == "ITC-EQ"


def test_interrupted_place_is_recorded_and_reraised(tmp_path: Path) -> None:
    client = MatrixClient(place=[KeyboardInterrupt()])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    with pytest.raises(KeyboardInterrupt):
        calls.place(session, PLAN, NSE)
    assert _lines(session)[-1]["verdict"] == calls.INTERRUPTED
    assert client.count("place_order") == 1


# -- never resend: modify / cancel / exit (Review Focus 5) ------------------------------------


def test_modify_ack_and_rejection(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    _placed(session, client)
    ok = calls.modify(session, "1001", NSE, price=Decimal("359.95"), **_modify_kwargs())
    assert ok.verdict == calls.ACKNOWLEDGED
    client.modify_result = _rejected("SAF: order is not open")
    no = calls.modify(session, "1001", NSE, price=Decimal("359.90"), **_modify_kwargs())
    assert no.verdict == calls.REJECTED
    assert client.count("modify_order") == 2  # two intents, one call each


def _row(price: str = "360.00", prctyp: str = "LMT", status: str = "OPEN", tag: str = "") -> Order:
    return _order("1001", status, tag=tag, prc=price, qty="1", prctyp=prctyp, prd="C", ret="DAY")


def test_a_modify_that_times_out_is_resolved_by_the_book(tmp_path: Path) -> None:
    client = MatrixClient(modify=_unknown("ModifyOrder"))
    session = _session(client, tmp_path)
    tag = _placed(session, client)
    client.books = [[_row("360.00", tag=tag)], [_row("359.95", tag=tag)]]  # before, after
    outcome = calls.modify(session, "1001", NSE, price=Decimal("359.95"), **_modify_kwargs())
    assert client.count("modify_order") == 1
    assert outcome.verdict == calls.RESOLVED
    assert session.guard.tripped("nse") is None


def test_a_modify_that_times_out_and_is_not_in_the_book_is_check_the_account(
    tmp_path: Path,
) -> None:
    client = MatrixClient(modify=_unknown("ModifyOrder"))
    session = _session(client, tmp_path)
    tag = _placed(session, client)
    client.books = [[_row("360.00", tag=tag)]]  # the old price, before and after
    outcome = calls.modify(session, "1001", NSE, price=Decimal("359.95"), **_modify_kwargs())
    assert client.count("modify_order") == 1
    assert outcome.verdict == CHECK_THE_ACCOUNT
    assert session.guard.tripped("nse") is not None
    assert "1001" in session.ledger.flagged()
    # no further state-changing call for that order: not even the teardown cancel
    report = calls.cancel_ours(session)
    assert report.flagged == ["1001"] and client.count("cancel_order") == 0
    session.guard.trips.clear()
    with pytest.raises(Refusal, match="CHECK THE ACCOUNT"):
        calls.modify(session, "1001", NSE, price=Decimal("1"), **_modify_kwargs())
    assert client.count("modify_order") == 1


def test_an_ambiguous_modify_that_changes_nothing_visible_is_not_resolved(tmp_path: Path) -> None:
    """M3: the book row must show a field that changed from the row read before the modify."""
    client = MatrixClient(modify=_unknown("ModifyOrder"))
    session = _session(client, tmp_path)
    tag = _placed(session, client)
    client.books = [[_row("360.00", tag=tag)]]
    outcome = calls.modify(session, "1001", NSE, price=Decimal("360.00"), **_modify_kwargs())
    assert outcome.verdict == CHECK_THE_ACCOUNT


@pytest.mark.parametrize(
    ("after", "verdict"),
    [("LMT", CHECK_THE_ACCOUNT), ("SL-LMT", calls.RESOLVED)],
    ids=["old-type", "new-type"],
)
def test_an_ambiguous_modify_checks_the_order_type_too(
    tmp_path: Path, after: str, verdict: str
) -> None:
    """M3: the new price is shown, but the order type must be the requested one as well."""
    client = MatrixClient(modify=_unknown("ModifyOrder"))
    session = _session(client, tmp_path)
    tag = _placed(session, client)
    client.books = [[_row("360.00", tag=tag)], [_row("365.00", prctyp=after, tag=tag)]]
    kwargs = {**_modify_kwargs(), "order_type": "SL-LMT"}
    outcome = calls.modify(session, "1001", NSE, price=Decimal("365.00"), **kwargs)
    assert outcome.verdict == verdict


def test_a_cancel_that_times_out_is_resolved_by_the_book(tmp_path: Path) -> None:
    client = MatrixClient(cancel=_unknown("CancelOrder"))
    session = _session(client, tmp_path)
    tag = _placed(session, client)
    client.books = [[_order("1001", "OPEN", tag=tag)], [_order("1001", "CANCELED", tag=tag)]]
    outcome = calls.cancel(session, "1001", NSE)
    assert client.count("cancel_order") == 1
    assert outcome.verdict == calls.RESOLVED and outcome.status == "CANCELED"


def test_a_cancel_that_times_out_and_stays_open_is_check_the_account(tmp_path: Path) -> None:
    client = MatrixClient(cancel=_unknown("CancelOrder"))
    session = _session(client, tmp_path)
    tag = _placed(session, client)
    client.books = [[_order("1001", "OPEN", tag=tag)]]
    outcome = calls.cancel(session, "1001", NSE)
    assert client.count("cancel_order") == 1
    assert outcome.verdict == CHECK_THE_ACCOUNT
    assert session.guard.tripped("nse") is not None
    report = calls.cancel_ours(session)
    assert client.count("cancel_order") == 1 and report.flagged == ["1001"]


def test_an_exit_that_times_out_is_resolved_or_check_the_account(tmp_path: Path) -> None:
    client = MatrixClient(exit_=_unknown("ExitSNOOrder"))
    session = _session(client, tmp_path)
    tag1 = _placed(session, client, "1001")
    tag2 = _placed(session, client, "1002")
    client.books = [[_order("1001", "COMPLETE", tag=tag1)]]
    assert calls.exit(session, "1001", "CO", NSE).verdict == calls.RESOLVED
    client.books = [[_order("1002", "OPEN", tag=tag2)]]
    assert calls.exit(session, "1002", "CO", NSE).verdict == CHECK_THE_ACCOUNT
    assert client.count("exit_order") == 2


def test_cancel_and_exit_are_allowed_after_the_stop_file(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    _placed(session, client, "1001")
    _placed(session, client, "1002")
    (tmp_path / "STOP").write_text("", encoding="utf-8")
    calls.cancel(session, "1001", NSE)
    calls.exit(session, "1002", "BO", NSE)
    assert client.count("cancel_order") == 1 and client.count("exit_order") == 1


# -- C1: modify / cancel / exit only on this run's own orders ---------------------------------


def _close(session: gate.MatrixSession, kind: str, order_id: str, **extra: Any) -> Any:
    if kind == "modify":
        return calls.modify(session, order_id, NSE, price=Decimal("1"), **_modify_kwargs(), **extra)
    if kind == "cancel":
        return calls.cancel(session, order_id, NSE, **extra)
    return calls.exit(session, order_id, "CO", NSE, **extra)


@pytest.mark.parametrize("kind", ["modify", "cancel", "exit"])
def test_a_foreign_order_is_refused_with_no_call(tmp_path: Path, kind: str) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    before = len(client.calls)
    with pytest.raises(Refusal, match="not an order of this run"):
        _close(session, kind, "555")
    assert client.calls[before:] == []
    assert "555" not in session.ledger.ours()
    client.books = [[_order("555", "OPEN", tag="by-hand")]]
    with pytest.raises(StopRun):  # still foreign: the invariant still stops the run
        check_open_orders(client, session.ledger, session.ignored_open)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["modify", "cancel", "exit"])
def test_an_ignored_stuck_order_is_refused_with_no_call(tmp_path: Path, kind: str) -> None:
    client = MatrixClient([[_order("777", "PENDING", tag="old")]])
    env = {**MATRIX_ENV, "TRADESMART_LIVE_ORDER_UAT_IGNORE_OPEN": "777"}
    session = _session(client, tmp_path, env)
    before = len(client.calls)
    with pytest.raises(Refusal):
        _close(session, kind, "777")
    assert client.calls[before:] == []


@pytest.mark.parametrize("kind", ["cancel", "exit"])
def test_a_caller_tag_is_never_trusted(tmp_path: Path, kind: str) -> None:
    """cancel / exit take no tag: the ledger's tag of the order is used."""
    client = MatrixClient()
    session = _session(client, tmp_path)
    _placed(session, client)
    before = len(client.calls)
    with pytest.raises(TypeError):
        _close(session, kind, "1001", tag="any-free-text")
    assert client.calls[before:] == []
    assert "any-free-text" not in session.ledger.tags()


def test_a_modify_tag_change_needs_a_tag_issued_by_the_ledger(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    _placed(session, client)
    before = len(client.calls)
    with pytest.raises(ValueError, match="not issued"):
        calls.modify(session, "1001", NSE, price=Decimal("1"), tag="free", **_modify_kwargs())
    assert client.calls[before:] == []
    assert "free" not in session.ledger.tags()
    new = session.ledger.new_tag(NSE.case_id)
    calls.modify(session, "1001", NSE, price=Decimal("1"), tag=new, **_modify_kwargs())
    assert client.calls[-1][1][1]["tag"] == new
    assert session.ledger.tag_of("1001") == new and new in session.ledger.tags()


def test_cancel_ours_uses_the_ledger_tag_of_the_order(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    tag = _placed(session, client)
    client.books = [[_order("1001", "OPEN", tag=tag)], [_order("1001", "CANCELED", tag=tag)]]
    calls.cancel_ours(session)
    intents = [x for x in _lines(session) if x["type"] == "intent" and x["kind"] == "cancel"]
    assert [x["tag"] for x in intents] == [tag]


def test_modify_cancel_exit_intents_never_make_an_order_ours(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", "1113abcd")
    call = ledger.intent("x", "case", "cancel", segment="nse", order_id="555")
    ledger.outcome("x", "555", "acknowledged", kind="cancel", call=call)
    assert "555" not in ledger.ours() and "x" not in ledger.tags()
    assert "555" not in Ledger(ledger.path, "1113abcd").ours()


# -- I1: a crash between intent and outcome is unknown on resume ------------------------------


def _resume(tmp_path: Path, first: gate.MatrixSession, client: MatrixClient) -> gate.MatrixSession:
    gate.release_run_lock(first)  # the first process ended (one process per run id)
    env = {**MATRIX_ENV, "TRADESMART_UAT_RUN_ID": first.run_id}
    return _session(client, tmp_path, env, now=datetime(2026, 11, 13, 11, 0, tzinfo=IST))


def test_a_place_intent_without_outcome_blocks_the_case_on_resume(tmp_path: Path) -> None:
    first = _session(MatrixClient(), tmp_path)
    tag = first.ledger.new_tag(NSE.case_id)
    first.ledger.intent(tag, NSE.case_id, "place", segment="nse")  # then the process died
    client = MatrixClient([[_order("1001", "OPEN", tag=tag)]])
    second = _resume(tmp_path, first, client)
    assert second.ledger.tag_flagged(tag)
    reason = second.case_done(NSE.case_id)
    assert reason is not None and "without a verdict" in reason
    second.guard.reserve(NSE.case_id, 1)
    with pytest.raises(Refusal, match="without a verdict"):
        calls.place(second, PLAN, NSE)
    report = calls.cancel_ours(second)
    assert client.count("place_order") == 0 and client.count("cancel_order") == 0
    assert report.flagged == ["1001"]


def test_a_modify_intent_without_outcome_flags_the_order_on_resume(tmp_path: Path) -> None:
    client = MatrixClient()
    first = _session(client, tmp_path)
    tag = _placed(first, client)
    first.ledger.intent(tag, "setup-1001", "modify", segment="nse", order_id="1001")
    second_client = MatrixClient([[_order("1001", "OPEN", tag=tag)]])
    second = _resume(tmp_path, first, second_client)
    assert "1001" in second.ledger.flagged()
    with pytest.raises(Refusal, match="CHECK THE ACCOUNT"):
        calls.cancel(second, "1001", NSE)
    assert second_client.count("cancel_order") == 0


def test_every_intent_has_a_unique_call_id_echoed_by_its_outcome(tmp_path: Path) -> None:
    client = MatrixClient(place=["1001", "1002"])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 2)
    calls.place(session, PLAN, NSE)
    calls.place(session, PLAN, NSE)
    intents = [x for x in _lines(session) if x["type"] == "intent"]
    outcomes = [x for x in _lines(session) if x["type"] == "outcome"]
    assert len({x["call"] for x in intents}) == 2
    assert [x["call"] for x in outcomes] == [x["call"] for x in intents]


# -- I2: an interrupted call is never repeated, not even by the teardown ----------------------


@pytest.mark.parametrize("kind", ["cancel", "modify"])
def test_an_interrupted_call_flags_the_order_for_the_teardown(tmp_path: Path, kind: str) -> None:
    client = MatrixClient(cancel=KeyboardInterrupt(), modify=KeyboardInterrupt())
    session = _session(client, tmp_path)
    tag = _placed(session, client)
    client.books = [[_order("1001", "OPEN", tag=tag)]]
    with pytest.raises(KeyboardInterrupt):
        _close(session, kind, "1001")
    assert session.interrupted is True
    cancels = client.count("cancel_order")
    report = calls.cancel_ours(session)
    assert client.count("cancel_order") == cancels
    assert report.flagged == ["1001"]


def test_the_teardown_after_ctrl_c_cancels_nothing_and_lists_ours(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    _placed(session, client)
    session.interrupted = True
    assert calls.teardown(session) is None
    assert client.count("cancel_order") == 0 and client.count("orders") == 1  # session check
    note = _lines(session)[-1]
    assert note["type"] == "teardown" and note["ours"] == ["1001"]
    assert "Ctrl-C" in note["skipped"]


def test_the_teardown_cancels_ours_when_not_interrupted(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    tag = _placed(session, client)
    client.books = [[_order("1001", "OPEN", tag=tag)], [_order("1001", "CANCELED", tag=tag)]]
    report = calls.teardown(session)
    assert report is not None and report.cancelled == ["1001"]
    assert _lines(session)[-1]["type"] == "teardown"


def test_a_keyboard_interrupt_marks_every_matrix_session(tmp_path: Path) -> None:
    from tests.live_orders.uat_matrix import conftest as matrix_conftest

    session = _session(MatrixClient(), tmp_path)
    matrix_conftest.SESSIONS.append(session)
    try:
        matrix_conftest.pytest_keyboard_interrupt(None)  # type: ignore[arg-type]
        assert session.interrupted is True
    finally:
        matrix_conftest.SESSIONS.remove(session)


# -- I3: MAX_OPEN holds under concurrency ------------------------------------------------------


def test_max_open_holds_when_threads_race_for_the_last_slot(tmp_path: Path) -> None:
    guard = Guard(tmp_path / "STOP")
    guard.opened(*(str(i) for i in range(MAX_OPEN - 1)))
    barrier = threading.Barrier(20)
    won: list[int] = []

    def claim() -> None:
        barrier.wait()
        try:
            guard.check_send("nse")
        except MaxOpenReached:
            return
        won.append(1)

    threads = [threading.Thread(target=claim) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 1
    assert guard.open_count + guard.pending == MAX_OPEN


def test_concurrent_places_never_pass_max_open(tmp_path: Path) -> None:
    def slow(name: str, args: Any) -> None:
        if name == "place_order":
            time.sleep(0.01)

    client = MatrixClient(place=["9001"], on_call=slow)
    session = _session(client, tmp_path)
    session.guard.opened(*(str(i) for i in range(MAX_OPEN - 1)))
    cases = [CaseRef(f"c{i}", "nse") for i in range(20)]
    for case in cases:
        session.guard.reserve(case.case_id, 1)
    barrier = threading.Barrier(20)
    refused: list[int] = []

    def run(case: CaseRef) -> None:
        barrier.wait()
        try:
            calls.place(session, PLAN, case)
        except MaxOpenReached:
            refused.append(1)

    threads = [threading.Thread(target=run, args=(c,)) for c in cases]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert client.count("place_order") == 1 and len(refused) == 19
    assert session.guard.open_count == MAX_OPEN and session.guard.pending == 0


def test_a_refused_place_releases_its_slot(tmp_path: Path) -> None:
    client = MatrixClient(place=[_rejected()])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    calls.place(session, PLAN, NSE)
    assert session.guard.pending == 0 and session.guard.open_count == 0
    with pytest.raises(Refusal, match="declared"):  # spend refuses after the slot was claimed
        calls.place(session, PLAN, NSE)
    assert session.guard.pending == 0


# -- M1, M2, M4, M5, M6 -----------------------------------------------------------------------


def test_redaction_never_touches_the_structural_fields(tmp_path: Path) -> None:
    """M1: an all-digit client id equal to an order id must not corrupt ours / tags."""
    ledger = Ledger(tmp_path / "ledger.jsonl", "1113abcd", forbidden=["1001"])
    call = ledger.intent("sdkuat-1113abcd-001-1", "case-1001", "place", segment="nse")
    ledger.outcome(
        "sdkuat-1113abcd-001-1", "1001", "acknowledged", kind="place", call=call, note="1001"
    )
    reopened = Ledger(ledger.path, "1113abcd", forbidden=["1001"])
    assert reopened.ours() == {"1001"}
    assert reopened.case_of("sdkuat-1113abcd-001-1") == "case-1001"
    assert '"note": "[redacted]"' in ledger.path.read_text(encoding="utf-8")


def test_sliced_value_error_with_a_partial_result_takes_the_ambiguous_path(
    tmp_path: Path,
) -> None:
    """M2: a ValueError carrying a sliced result means slices were sent."""
    error = ValueError("stopped")
    error.sliced_result = _sliced(("1",), False)  # type: ignore[attr-defined]

    def book(c: MatrixClient) -> list[Order]:
        return [_order("1", "OPEN", tag=c.sent_tags()[0])]

    client = MatrixClient(sliced=error)
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 3)
    client.books = [[], book]
    outcome = calls.place_sliced(session, PLAN, NSE, max_slices=3, max_slice_quantity=1)
    assert outcome.verdict == CHECK_THE_ACCOUNT and outcome.order_ids == ("1",)


def test_sliced_value_error_with_an_unreadable_book_is_check_the_account(tmp_path: Path) -> None:
    """M2: nothing found because the book could not be read is not "not sent"."""
    client = MatrixClient(sliced=ValueError("quantity is not a lot multiple"))
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 3)
    client.books = [NetworkError("down")]
    outcome = calls.place_sliced(session, PLAN, NSE, max_slices=3, max_slice_quantity=1)
    assert outcome.verdict == CHECK_THE_ACCOUNT


def test_a_recent_ack_not_yet_in_the_book_stays_open(tmp_path: Path) -> None:
    """M4: a book read must not drop an order acknowledged a moment ago."""
    t = FakeTime()
    ledger = Ledger(tmp_path / "ledger.jsonl", "1113abcd")
    call = ledger.intent("tagA", "case", "place", segment="nse")
    ledger.outcome("tagA", "1001", "acknowledged", kind="place", call=call)
    guard = Guard(tmp_path / "STOP", clock=t.clock)
    guard.opened("1001")
    client = MatrixClient([[]])
    check_open_orders(client, ledger, frozenset(), guard=guard)  # type: ignore[arg-type]
    assert guard.open_ids == {"1001"}  # not in the book yet: kept
    t.now += 60
    check_open_orders(client, ledger, frozenset(), guard=guard)  # type: ignore[arg-type]
    assert guard.open_ids == set()  # long gone from the book: dropped
    guard.opened("1002")
    client.books = [[_order("1002", "CANCELED", tag="tagA")]]
    check_open_orders(client, ledger, frozenset(), guard=guard)  # type: ignore[arg-type]
    assert guard.open_ids == set()  # final in the book: dropped at once


def test_a_stop_at_the_start_of_a_resumed_run_cancels_its_own_orders(tmp_path: Path) -> None:
    """M5: a foreign open order at resume: the run's own open orders are cancelled first."""
    client = MatrixClient()
    first = _session(client, tmp_path)
    tag = _placed(first, client)
    ours = _order("1001", "OPEN", tag=tag)
    foreign = _order("555", "OPEN", tag="by-hand")
    second_client = MatrixClient([[ours, foreign], [ours, foreign], [_order("1001", "CANCELED")]])
    with pytest.raises(StopRun, match="555"):
        _resume(tmp_path, first, second_client)
    assert second_client.calls.count(("cancel_order", "1001")) == 1
    assert ("cancel_order", "555") not in second_client.calls


@pytest.mark.parametrize(
    ("markexpr", "selected"),
    [
        ("stress", True),
        ("uat_matrix and stress", True),
        ("not stress", False),
        ("nostress", False),
        ("m_stress_x", False),
        ("stressful", False),
        ("", False),
    ],
)
def test_the_stress_tier_is_selected_by_marker_token(markexpr: str, selected: bool) -> None:
    """M6: ``-m`` is read by token, not by substring."""
    reason = gate.gate_refusal(MATRIX_ENV, UAT_URL, gate.STRESS, markexpr=markexpr)
    assert (reason is None) is selected


# -- never resend: place_sliced ---------------------------------------------------------------


def _sliced(ids: tuple[str, ...], complete: bool, error: Any = None) -> SlicedOrderResult:
    return SlicedOrderResult(
        requested_quantity=3,
        slice_quantities=(1, 1, 1),
        order_ids=ids,
        placed_quantity=len(ids),
        unsent_quantity=3 - len(ids),
        complete=complete,
        error=error,
    )


def test_sliced_order_is_one_call_with_one_tag(tmp_path: Path) -> None:
    client = MatrixClient(sliced=_sliced(("1", "2", "3"), True))
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 3)
    outcome = calls.place_sliced(session, PLAN, NSE, max_slices=3, max_slice_quantity=1)
    assert client.count("place_sliced_order") == 1
    kwargs = client.calls[-1][1]
    assert kwargs["max_slices"] == 3 and kwargs["tag"] == outcome.tag
    assert outcome.verdict == calls.ACKNOWLEDGED and outcome.order_ids == ("1", "2", "3")
    assert session.ledger.ours() >= {"1", "2", "3"}
    assert session.guard.open_ids == {"1", "2", "3"}


def test_sliced_order_stopped_by_a_rejection_keeps_the_placed_slices(tmp_path: Path) -> None:
    client = MatrixClient(sliced=_sliced(("1",), False, _rejected()))
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 3)
    outcome = calls.place_sliced(session, PLAN, NSE, max_slices=3, max_slice_quantity=1)
    assert outcome.verdict == calls.REJECTED and outcome.order_ids == ("1",)
    assert session.ledger.ours() == {"1"}


def test_ambiguous_sliced_order_looks_up_the_unknown_slice(tmp_path: Path) -> None:
    error = _unknown()
    error.sliced_result = _sliced(("1",), False)

    def book(c: MatrixClient) -> list[Order]:
        tag = c.sent_tags()[0]
        return [_order("1", "OPEN", tag=tag), _order("2", "OPEN", tag=tag)]

    client = MatrixClient(sliced=error)
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 3)
    client.books = [[], book]
    outcome = calls.place_sliced(session, PLAN, NSE, max_slices=3, max_slice_quantity=1)
    assert client.count("place_sliced_order") == 1
    assert outcome.verdict == calls.FOUND and outcome.order_ids == ("1", "2")


def test_ambiguous_sliced_order_without_the_unknown_slice_is_check_the_account(
    tmp_path: Path,
) -> None:
    error = _unknown()
    error.sliced_result = _sliced(("1",), False)

    def book(c: MatrixClient) -> list[Order]:
        return [_order("1", "OPEN", tag=c.sent_tags()[0])]

    client = MatrixClient(sliced=error)
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 3)
    client.books = [[], book]
    outcome = calls.place_sliced(session, PLAN, NSE, max_slices=3, max_slice_quantity=1)
    assert outcome.verdict == CHECK_THE_ACCOUNT and outcome.order_ids == ("1",)
    assert session.guard.tripped("nse") is not None
    assert client.count("place_sliced_order") == 1


def test_sliced_order_refused_by_the_sdk_before_sending_is_not_sent(tmp_path: Path) -> None:
    client = MatrixClient(sliced=ValueError("quantity is not a lot multiple"))
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 3)
    outcome = calls.place_sliced(session, PLAN, NSE, max_slices=3, max_slice_quantity=1)
    assert outcome.verdict == calls.NOT_SENT
    assert client.count("place_sliced_order") == 1


# -- teardown cancel-all-ours -----------------------------------------------------------------


def test_cancel_ours_touches_only_this_runs_open_orders(tmp_path: Path) -> None:
    client = MatrixClient(place=["1001", "1002"])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 2)
    a = calls.place(session, PLAN, NSE)
    b = calls.place(session, PLAN, NSE)
    client.books = [
        [
            _order("1001", "OPEN", tag=a.tag),
            _order("1002", "REJECTED", tag=b.tag),
            _order("555", "OPEN", tag="by-hand"),
        ],
        [_order("1001", "CANCELED", tag=a.tag), _order("555", "OPEN", tag="by-hand")],
    ]
    report = calls.cancel_ours(session)
    assert [args for name, args in client.calls if name == "cancel_order"] == ["1001"]
    assert report.cancelled == ["1001"] and report.untouched == ["555"]


def test_cancel_ours_with_an_unreadable_book_cancels_nothing(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    client.books = [NetworkError("down")]
    report = calls.cancel_ours(session)
    assert client.count("cancel_order") == 0 and report.book_unreadable is True


def test_item_refusal_skips_cases_done_in_a_resumed_run(tmp_path: Path) -> None:
    session = _session(MatrixClient(), tmp_path)
    session.ledger.verdict(NSE.case_id, "passed")
    assert session.case_done(NSE.case_id) == f"done in run {session.run_id} (passed)"
    assert session.case_done("other") is None


# -- fix round 2 -------------------------------------------------------------------------------


def test_retag_refuses_a_foreign_id_or_an_unissued_tag(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    tag = _placed(session, client)
    lines = len(_lines(session))
    new = session.ledger.new_tag(NSE.case_id)
    with pytest.raises(ValueError, match="not an order of this run"):
        session.ledger.retag("999", new)
    with pytest.raises(ValueError, match="not issued"):
        session.ledger.retag("1001", "sdkuat-free-001-1")
    assert session.ledger.ours() == {"1001"} and session.ledger.tag_of("1001") == tag
    assert "sdkuat-free-001-1" not in session.ledger.tags()
    assert len(_lines(session)) == lines + 1  # only the new_tag "case" line: nothing else


def test_adopt_takes_the_book_row_and_needs_one_of_our_place_tags(tmp_path: Path) -> None:
    client = MatrixClient()
    session = _session(client, tmp_path)
    tag = _placed(session, client)
    lines = len(_lines(session))
    assert session.ledger.adopt(_order("888", "OPEN", tag="by-hand")) is False
    assert session.ledger.adopt(_order("889", "OPEN", tag="")) is False
    assert session.ledger.ours() == {"1001"} and len(_lines(session)) == lines
    assert session.ledger.adopt(_order("890", "OPEN", tag=tag)) is True
    assert session.ledger.ours() == {"1001", "890"}


def test_a_rejected_modify_still_uses_up_its_new_tag(tmp_path: Path) -> None:
    client = MatrixClient(modify=_rejected("SAF: not allowed"))
    session = _session(client, tmp_path)
    _placed(session, client)
    new = session.ledger.new_tag(NSE.case_id)
    outcome = calls.modify(session, "1001", NSE, price=Decimal("1"), tag=new, **_modify_kwargs())
    assert outcome.verdict == calls.REJECTED
    assert session.ledger.tag_of("1001") != new
    session.guard.reserve(NSE.case_id, 1)
    places = client.count("place_order")
    with pytest.raises(ValueError, match="already"):
        calls.place(session, PLAN, NSE, tag=new)
    assert client.count("place_order") == places


def test_the_ctrl_c_teardown_note_lists_the_tag_of_an_interrupted_place(tmp_path: Path) -> None:
    client = MatrixClient(place=[KeyboardInterrupt()])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 1)
    with pytest.raises(KeyboardInterrupt):
        calls.place(session, PLAN, NSE)
    tag = client.sent_tags()[0]
    assert calls.teardown(session) is None
    note = _lines(session)[-1]
    assert note["type"] == "teardown" and note["ours"] == []
    assert tag in note["tags"] and tag in note["flagged_tags"]


def test_stop_run_then_teardown_cancels_each_order_once(tmp_path: Path) -> None:
    """A StopRun mid-run cancels ours; the teardown never cancels the same order again, a stuck
    one included (it is listed), and a book row listed twice is cancelled once."""
    client = MatrixClient(place=["1001", "1002"])
    session = _session(client, tmp_path)
    session.guard.reserve(NSE.case_id, 2)
    a = calls.place(session, PLAN, NSE)
    b = calls.place(session, PLAN, NSE)
    open_a, open_b = _order("1001", "OPEN", tag=a.tag), _order("1002", "OPEN", tag=b.tag)
    done_a = _order("1001", "CANCELED", tag=a.tag)
    client.books = [[open_a, open_a, open_b], [done_a, open_b]]  # 1002 never goes final
    report = calls.stop_run(session, StopRun("someone trading", foreign=["555"]))
    assert report.cancelled == ["1001", "1002"] and report.not_final == ["1002"]
    assert client.calls.count(("cancel_order", "1001")) == 1
    client.books = [[open_a, open_b]]  # the teardown sees both open again
    report = calls.teardown(session)  # type: ignore[assignment]
    assert report is not None
    assert client.calls.count(("cancel_order", "1001")) == 1
    assert client.calls.count(("cancel_order", "1002")) == 1
    assert report.stuck == ["1002"] and report.already_cancelled == ["1001"]
    with pytest.raises(Refusal, match="already cancelled"):
        calls.cancel(session, "1001", NSE)
    assert client.calls.count(("cancel_order", "1001")) == 1


# -- fix round 3 -------------------------------------------------------------------------------


def test_a_cancel_never_sent_keeps_the_orders_one_cancel(tmp_path: Path) -> None:
    """A cancel refused client-side (NOT_SENT) does not use up the order's one CancelOrder."""
    client = MatrixClient(cancel=RequestValidationError("bad order id"))
    session = _session(client, tmp_path)
    tag = _placed(session, client)
    assert calls.cancel(session, "1001", NSE).verdict == calls.NOT_SENT
    assert session.ledger.cancel_sent("1001") is False
    client.cancel_result = None
    client.books = [[_order("1001", "OPEN", tag=tag)], [_order("1001", "CANCELED", tag=tag)]]
    report = calls.cancel_ours(session)
    assert report.cancelled == ["1001"] and report.already_cancelled == []
    assert client.count("cancel_order") == 2  # the refused attempt (nothing sent) + one cancel
    assert session.ledger.cancel_sent("1001") is True


@pytest.mark.parametrize("verdict_error", ["rejected", "unknown"])
def test_a_sent_or_unknown_cancel_uses_up_the_orders_one_cancel(
    tmp_path: Path, verdict_error: str
) -> None:
    error = _rejected("SAF: not open") if verdict_error == "rejected" else _unknown("CancelOrder")
    client = MatrixClient(cancel=error)
    session = _session(client, tmp_path)
    tag = _placed(session, client)
    client.books = [[_order("1001", "OPEN", tag=tag)]]
    calls.cancel(session, "1001", NSE)
    assert session.ledger.cancel_sent("1001") is True
    assert Ledger(session.ledger.path, session.run_id).cancel_sent("1001") is True


def test_a_cancel_never_sent_is_allowed_again_after_a_resume(tmp_path: Path) -> None:
    client = MatrixClient(cancel=RequestValidationError("bad order id"))
    first = _session(client, tmp_path)
    tag = _placed(first, client)
    calls.cancel(first, "1001", NSE)
    book = [[_order("1001", "OPEN", tag=tag)], [_order("1001", "CANCELED", tag=tag)]]
    second_client = MatrixClient([book[0]])
    second = _resume(tmp_path, first, second_client)
    assert second.ledger.cancel_sent("1001") is False
    second_client.books = book
    assert calls.cancel(second, "1001", NSE).verdict == calls.ACKNOWLEDGED
    assert second_client.count("cancel_order") == 1
