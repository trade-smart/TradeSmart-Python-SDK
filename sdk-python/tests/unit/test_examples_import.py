"""Examples and documentation snippets stay valid against the real package."""

from __future__ import annotations

import ast
import asyncio
import http.server
import importlib
import importlib.util
import inspect
import os
import py_compile
import re
import socket
import stat
import threading
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import respx

import tradesmart
import tradesmart._cli
from tests._public_tree import BROKER_RE, FORBIDDEN_RE, ROOT_ONLY_SKIPPED, internal_references
from tests.conftest import BASE, fixture
from tradesmart._links import signup_url
from tradesmart.testing import OfflineBroker

SDK_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = SDK_DIR.parent
EXAMPLES_DIR = SDK_DIR / "examples"
GUIDE_DIR = SDK_DIR / "docs"
GUIDE_PAGES = [
    "README.md",
    "getting-started.md",
    "authentication.md",
    "orders.md",
    "streaming.md",
    "market-data.md",
    "configuration.md",
    "troubleshooting.md",
    "versioning.md",
    "option-analytics.md",
    "testing.md",
    "gtt.md",
    "oco.md",
    "account.md",
    "reference.md",
]
GUIDE = [GUIDE_DIR / page for page in GUIDE_PAGES]
REFERENCE = GUIDE_DIR / "reference.md"
MIGRATION = sorted((GUIDE_DIR / "migration").glob("*.md"))
ARCHITECTURE = GUIDE_DIR / "architecture.md"
# The shared, language-neutral docs at the repository root.
SHARED_DOCS = [REPO_DIR / "docs" / "README.md", REPO_DIR / "docs" / "api-access.md"]
DOCS = [
    SDK_DIR / "README.md",
    SDK_DIR / "CONTRIBUTING.md",
    ARCHITECTURE,
    *SHARED_DOCS,
    *GUIDE,
    *MIGRATION,
]
EXAMPLES = [
    "auth_server_flow.py",
    "first_call.py",
    "place_order.py",
    "order_lifecycle.py",
    "market_data.py",
    "stream_sync.py",
    "stream_async.py",
    "async_quotes.py",
    "offline_strategy_test.py",
    "sliced_order.py",
    "gtt_alert.py",
    "oco_exit.py",
]
SAFETY_FLAG = "--i-understand-this-places-a-real-order"
_SECRET_ATTRS = {"access_token", "susertoken", "refresh_token", "get_secret_value"}
_PY_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)
_FLAGS = ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT


def _tradesmart_imports(tree: ast.AST) -> list[tuple[str, str]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tradesmart"):
            found.extend((node.module or "", alias.name) for alias in node.names)
        elif isinstance(node, ast.Import):
            found.extend(
                (alias.name, "") for alias in node.names if alias.name.startswith("tradesmart")
            )
    return found


def _assert_imports_exist(imports: list[tuple[str, str]], where: str) -> None:
    for module_name, name in imports:
        module = importlib.import_module(module_name)
        if name:
            assert hasattr(module, name), f"{where}: {module_name} has no {name!r}"


def _load_example(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_example_{name[:-3]}", EXAMPLES_DIR / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_compiles_and_imports_real_names(name: str) -> None:
    path = EXAMPLES_DIR / name
    py_compile.compile(str(path), doraise=True)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    _assert_imports_exist(_tradesmart_imports(tree), name)


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_does_not_run_on_import(name: str) -> None:
    source = (EXAMPLES_DIR / name).read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source
    _load_example(name)  # must not raise, touch the network or read credentials


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_never_prints_secrets(name: str) -> None:
    tree = ast.parse((EXAMPLES_DIR / name).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print":
            for inner in ast.walk(node):
                assert not (isinstance(inner, ast.Attribute) and inner.attr in _SECRET_ATTRS), (
                    f"{name}:{node.lineno} prints a secret"
                )


def test_place_order_requires_safety_flag() -> None:
    source = (EXAMPLES_DIR / "place_order.py").read_text(encoding="utf-8")
    assert SAFETY_FLAG in source


def test_place_order_defaults_to_dry_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.delenv("TRADESMART_ACCESS_TOKEN", raising=False)
    module = _load_example("place_order.py")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        module.main([])
    assert not router.calls
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "PlaceOrder" in out
    assert '"uid": "TEST123"' in out


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_catches_tradesmart_error(name: str) -> None:
    """Network, timeout and parse failures are ``TradeSmartError``s, not only ``APIError``s."""
    tree = ast.parse((EXAMPLES_DIR / name).read_text(encoding="utf-8"))
    caught = {
        inner.id
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.type is not None
        for inner in ast.walk(node.type)
        if isinstance(inner, ast.Name)
    }
    assert "TradeSmartError" in caught, f"{name} never catches TradeSmartError"


def test_place_order_live_without_token_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.delenv("TRADESMART_ACCESS_TOKEN", raising=False)
    module = _load_example("place_order.py")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        code = module.main(["--symbol", "SBIN-EQ", "--price", "500", SAFETY_FLAG])
    assert code == 2
    assert not router.calls
    assert "TRADESMART_ACCESS_TOKEN" in capsys.readouterr().err


def test_place_order_unknown_outcome_survives_failing_order_book(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", "test-access-token")
    module = _load_example("place_order.py")
    monkeypatch.setattr(module, "POLL_SECONDS", 0.0)
    with respx.mock(assert_all_mocked=True) as router:
        router.post(f"{BASE}/PlaceOrder").mock(side_effect=httpx.ReadTimeout("timed out"))
        router.post(f"{BASE}/OrderBook").respond(
            401, json={"stat": "Not_Ok", "emsg": "Session Expired : Invalid Session Key"}
        )
        code = module.main(["--symbol", "SBIN-EQ", "--price", "500", SAFETY_FLAG])
    assert code == 1
    err = capsys.readouterr().err
    assert "Outcome unknown" in err
    assert "Could not read the order book" in err


def test_auth_flow_save_token_writes_only_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.setenv("TRADESMART_APP_KEY", "test-app-key")
    monkeypatch.setenv("TRADESMART_SECRET_KEY", "test-secret-key")
    module = _load_example("auth_server_flow.py")
    monkeypatch.setattr(module, "wait_for_code", lambda port: "test-code")
    monkeypatch.setattr(module.webbrowser, "open", lambda url: True)
    target = tmp_path / "token"
    with respx.mock(assert_all_mocked=True) as router:
        router.post(f"{BASE}/GenAcsTok").respond(200, json=fixture("gen_acs_tok_success"))
        code = module.main(["--save-token", str(target)])
    assert code == 0
    assert target.read_text(encoding="utf-8") == "test-access-token"
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    captured = capsys.readouterr()
    assert "test-access-token" not in captured.out + captured.err


def test_auth_flow_saves_nothing_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.setenv("TRADESMART_APP_KEY", "test-app-key")
    monkeypatch.setenv("TRADESMART_SECRET_KEY", "test-secret-key")
    monkeypatch.chdir(tmp_path)
    module = _load_example("auth_server_flow.py")
    monkeypatch.setattr(module, "wait_for_code", lambda port: "test-code")
    monkeypatch.setattr(module.webbrowser, "open", lambda url: True)
    with respx.mock(assert_all_mocked=True) as router:
        router.post(f"{BASE}/GenAcsTok").respond(200, json=fixture("gen_acs_tok_success"))
        assert module.main([]) == 0
    assert not list(tmp_path.iterdir())


def test_first_call_reads_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.delenv("TRADESMART_ACCESS_TOKEN", raising=False)
    token_file = tmp_path / "token"
    token_file.write_text("token-from-file\n", encoding="utf-8")
    module = _load_example("first_call.py")
    with respx.mock(assert_all_mocked=True) as router:
        details = router.post(f"{BASE}/UserDetails").respond(
            200, json=fixture("user_details_success")
        )
        router.post(f"{BASE}/Limits").respond(200, json=fixture("limits_success"))
        router.post(f"{BASE}/PositionBook").respond(200, json=fixture("position_book_success"))
        router.post(f"{BASE}/OrderBook").respond(200, json=fixture("order_book_success"))
        code = module.main(["--token-file", str(token_file)])
    assert code == 0
    assert details.calls.last.request.headers["Authorization"] == "Bearer token-from-file"
    assert "Client:" in capsys.readouterr().out


def test_first_call_without_token_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.delenv("TRADESMART_ACCESS_TOKEN", raising=False)
    module = _load_example("first_call.py")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        assert module.main([]) == 2
    assert not router.calls
    assert "--token-file" in capsys.readouterr().err


# docs/architecture.md is a developer document: it may link specifications/ and tests/.
_CLIENT_DOCS = [
    SDK_DIR / "README.md",
    SDK_DIR / "CHANGELOG.md",
    *SHARED_DOCS,
    *GUIDE,
    *(EXAMPLES_DIR / name for name in EXAMPLES),
]
_INTERNAL_REF = re.compile(
    FORBIDDEN_RE.pattern + r"|docs/plans|reference/|specifications/|tests/|test_\w+\.py"
)


@pytest.mark.parametrize("doc", _CLIENT_DOCS, ids=lambda p: p.name)
def test_client_docs_have_no_internal_references(doc: Path) -> None:
    """Clients get the wheel and the docs, not the planning notes or the test suite."""
    text = doc.read_text(encoding="utf-8")
    found = sorted({m.group(0) for m in _INTERNAL_REF.finditer(text)})
    assert not found, f"{doc.name} references internal paths: {found}"


def test_readme_links_migration_folder() -> None:
    text = (SDK_DIR / "README.md").read_text(encoding="utf-8")
    assert "## Switching from another broker" in text
    assert (
        "](https://github.com/trade-smart/TradeSmart-Python-SDK/tree/main/sdk-python/docs/migration)"
        in text
    )
    assert "## Low-latency setup" in REFERENCE.read_text(encoding="utf-8")


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_doc_python_blocks_parse_and_import_real_names(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8")
    for index, block in enumerate(_PY_BLOCK.findall(text)):
        where = f"{doc.name} python block {index}"
        tree = compile(block, where, "exec", flags=_FLAGS)
        _assert_imports_exist(_tradesmart_imports(tree), where)


def test_doc_snippets_are_labelled() -> None:
    text = (SDK_DIR / "README.md").read_text(encoding="utf-8")
    for match in _PY_BLOCK.finditer(text):
        before = text[: match.start()].rstrip().split("\n\n")[-1].lower()  # preceding paragraph
        assert "illustrative" in before, (
            f"README python block at offset {match.start()} is not labelled: {before!r}"
        )


def test_guide_has_every_page() -> None:
    missing = [page.name for page in GUIDE if not page.is_file()]
    assert not missing, f"sdk-python/docs lacks {missing}"


@pytest.mark.parametrize("doc", GUIDE, ids=lambda p: p.name)
def test_guide_snippets_are_labelled_illustrative(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8")
    for match in _PY_BLOCK.finditer(text):
        before = text[: match.start()].rstrip().split("\n\n")[-1].lower()  # preceding paragraph
        assert "illustrative" in before, (
            f"{doc.name} python block at offset {match.start()} is not labelled: {before!r}"
        )


# Variable names the docs use for SDK objects, and the classes whose members they may call.
_DOC_OBJECTS: dict[str, tuple[type, ...]] = {
    "client": (tradesmart.TradeSmart, tradesmart.AsyncTradeSmart),
    "stream": (tradesmart.TradeSmartStream, tradesmart.AsyncTradeSmartStream),
    "broker": (OfflineBroker,),
}
_INSTANCE_ATTRS = {"client_id", "config"}  # set in __init__, so not visible on the class


def _unknown_members(tree: ast.AST) -> list[str]:
    """``client.x`` / ``stream.x`` members and keyword arguments that do not exist."""
    problems = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in _DOC_OBJECTS
        ):
            continue
        classes = _DOC_OBJECTS[node.value.id]
        if node.attr not in _INSTANCE_ATTRS and not any(hasattr(c, node.attr) for c in classes):
            problems.append(f"{node.value.id}.{node.attr}")
    for node in ast.walk(tree):
        func = node.func if isinstance(node, ast.Call) else None
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in _DOC_OBJECTS
        ):
            continue
        assert isinstance(node, ast.Call)
        for cls in _DOC_OBJECTS[func.value.id]:
            member = getattr(cls, func.attr, None)
            if not callable(member):
                continue
            params = inspect.signature(member).parameters
            if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
                continue
            problems += [
                f"{func.value.id}.{func.attr}({kw.arg}=...)"
                for kw in node.keywords
                if kw.arg is not None and kw.arg not in params
            ]
    return problems


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_doc_snippets_call_real_methods_with_real_arguments(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8")
    for index, block in enumerate(_PY_BLOCK.findall(text)):
        tree = compile(block, f"{doc.name} block {index}", "exec", flags=_FLAGS)
        problems = _unknown_members(tree)
        assert not problems, f"{doc.name} python block {index} uses unknown names: {problems}"


def test_getting_started_covers_the_first_order_path() -> None:
    text = (GUIDE_DIR / "getting-started.md").read_text(encoding="utf-8")
    for needle in (
        "http://127.0.0.1:8080/callback",
        "static IP",
        "pip install tradesmart",
        "python -m tradesmart login",
        "--no-browser",
        "TradeSmart.from_env()",
        "search_instruments(",
        "round_to_tick(",
        "tag=",
        "order_history(",
        "modify_order(",
        "cancel_order(",
        "CANCELED",
        "on_order_update",
        "OrderStatusUnknownError",
        "first_order.py",
    ):
        assert needle in text, f"getting-started.md does not mention {needle!r}"


def test_orders_guide_shows_tag_recovery() -> None:
    text = (GUIDE_DIR / "orders.md").read_text(encoding="utf-8")
    assert "def find_by_tag(client" in text
    for status in tradesmart.OrderStatus:
        assert f"`{status.value}`" in text, f"orders.md does not describe {status.value}"


def _public_methods(cls: type) -> list[str]:
    return [
        name
        for name, member in inspect.getmembers(cls)
        if not name.startswith("_")
        and (inspect.isfunction(member) or inspect.ismethod(member) or isinstance(member, property))
    ]


@pytest.mark.parametrize(
    ("prefix", "cls"),
    [
        ("TradeSmart", tradesmart.TradeSmart),
        ("AsyncTradeSmart", tradesmart.AsyncTradeSmart),
        ("TradeSmartStream", tradesmart.TradeSmartStream),
        ("AsyncTradeSmartStream", tradesmart.AsyncTradeSmartStream),
    ],
)
def test_reference_lists_every_public_method(prefix: str, cls: type) -> None:
    text = REFERENCE.read_text(encoding="utf-8")
    missing = [m for m in _public_methods(cls) if f"{prefix}.{m}" not in text]
    assert not missing, f"reference page lacks {prefix}.{missing}"


def test_reference_lists_instrument_helpers() -> None:
    text = REFERENCE.read_text(encoding="utf-8")
    for name in ("load", "find"):
        assert callable(getattr(tradesmart.instruments, name))
        assert f"instruments.{name}" in text, f"reference page lacks instruments.{name}"


def test_async_client_mirrors_sync() -> None:
    """The README documents ``AsyncTradeSmart`` as a mirror of ``TradeSmart``."""
    sync = [m if m != "close" else "aclose" for m in _public_methods(tradesmart.TradeSmart)]
    assert sorted(sync) == _public_methods(tradesmart.AsyncTradeSmart)


def test_async_client_signatures_mirror_sync() -> None:
    """Every shared public method takes the same parameters (names, kinds, defaults, hints)."""
    shared = set(_public_methods(tradesmart.TradeSmart)) - {"close", "aclose"}
    assert shared, "no shared methods found"
    for name in sorted(shared):
        sync_member = getattr(tradesmart.TradeSmart, name)
        async_member = getattr(tradesmart.AsyncTradeSmart, name)
        if not callable(sync_member):  # properties have no signature to compare
            continue
        sync_sig = _self_named(inspect.signature(sync_member), "TradeSmart")
        async_sig = _self_named(inspect.signature(async_member), "AsyncTradeSmart")
        assert sync_sig == async_sig, name


def _self_named(sig: inspect.Signature, own: str) -> inspect.Signature:
    """``from_env() -> TradeSmart`` and ``-> AsyncTradeSmart`` both return their own class."""
    if sig.return_annotation == own:
        return sig.replace(return_annotation="<own class>")
    return sig


def test_reference_documents_every_config_field() -> None:
    text = REFERENCE.read_text(encoding="utf-8")
    fields = tradesmart.ClientConfig.__dataclass_fields__
    missing = [name for name in fields if f"`{name}`" not in text]
    assert not missing, f"reference configuration table lacks {missing}"


def test_reference_documents_every_exported_error() -> None:
    text = REFERENCE.read_text(encoding="utf-8")
    errors = [
        name
        for name in tradesmart.__all__
        if isinstance(getattr(tradesmart, name), type)
        and issubclass(getattr(tradesmart, name), tradesmart.TradeSmartError)
    ]
    missing = [name for name in errors if f"`{name}`" not in text]
    assert not missing, f"reference error table lacks {missing}"


def _auth_flow(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.setenv("TRADESMART_APP_KEY", "test-app-key")
    monkeypatch.setenv("TRADESMART_SECRET_KEY", "test-secret-key")
    module = _load_example("auth_server_flow.py")
    monkeypatch.setattr(module, "wait_for_code", lambda port: "test-code")
    monkeypatch.setattr(module.webbrowser, "open", lambda url: True)
    return module


def test_auth_flow_save_token_expands_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    module = _auth_flow(monkeypatch)
    with respx.mock(assert_all_mocked=True) as router:
        router.post(f"{BASE}/GenAcsTok").respond(200, json=fixture("gen_acs_tok_success"))
        assert module.main(["--save-token", "~/token"]) == 0
    assert (tmp_path / "token").read_text(encoding="utf-8") == "test-access-token"
    assert "test-access-token" not in capsys.readouterr().out


def test_auth_flow_save_failure_is_reported_without_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _auth_flow(monkeypatch)
    target = tmp_path / "missing-dir" / "token"
    with respx.mock(assert_all_mocked=True) as router:
        router.post(f"{BASE}/GenAcsTok").respond(200, json=fixture("gen_acs_tok_success"))
        code = module.main(["--save-token", str(target)])
    assert code == 1
    captured = capsys.readouterr()
    assert "obtained but could not be saved" in captured.err
    assert "--save-token" in captured.err  # how to retry
    assert "test-access-token" not in captured.out + captured.err
    assert not target.exists()


def test_first_call_token_file_expands_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    (tmp_path / "token").write_text("token-from-file\n", encoding="utf-8")
    module = _load_example("first_call.py")
    with respx.mock(assert_all_mocked=True) as router:
        details = router.post(f"{BASE}/UserDetails").respond(
            200, json=fixture("user_details_success")
        )
        router.post(f"{BASE}/Limits").respond(200, json=fixture("limits_success"))
        router.post(f"{BASE}/PositionBook").respond(200, json=fixture("position_book_success"))
        router.post(f"{BASE}/OrderBook").respond(200, json=fixture("order_book_success"))
        assert module.main(["--token-file", "~/token"]) == 0
    assert details.calls.last.request.headers["Authorization"] == "Bearer token-from-file"


@pytest.mark.parametrize("make", ["missing", "directory", "not-utf8"])
def test_first_call_unreadable_token_file_is_a_clear_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    make: str,
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    path = tmp_path / "token"
    if make == "directory":
        path.mkdir()
    elif make == "not-utf8":
        path.write_bytes(b"\xff\xfe\xfa")
    module = _load_example("first_call.py")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        assert module.main(["--token-file", str(path)]) == 2
    assert not router.calls
    err = capsys.readouterr().err
    assert "token file" in err and str(path) in err


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ["--side", "SELL", "--product", "MIS", "--order-type", "MARKET"],
            {"trantype": "S", "prd": "I", "prctyp": "MKT"},
        ),
        (
            ["--side", "buy", "--product", "nrml", "--order-type", "sl-m", "--trigger-price", "9"],
            {"trantype": "B", "prd": "M", "prctyp": "SL-MKT"},
        ),
        (
            ["--side", "s", "--product", "c", "--order-type", "lmt"],
            {"trantype": "S", "prd": "C", "prctyp": "LMT", "prc": "500"},
        ),
        (
            ["--product", "MTF", "--order-type", "SL", "--trigger-price", "9"],
            {"trantype": "B", "prd": "F", "prctyp": "SL-LMT", "prc": "500"},
        ),
    ],
)
def test_place_order_accepts_words_and_wire_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    expected: dict[str, str],
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    module = _load_example("place_order.py")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        assert module.main(args) == 0
    assert not router.calls
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    for key, value in expected.items():
        assert f'"{key}": "{value}"' in out


def test_place_order_rejects_unknown_word(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    module = _load_example("place_order.py")
    with pytest.raises(SystemExit) as info:
        module.main(["--side", "HOLD"])
    assert info.value.code == 2
    assert "BUY" in capsys.readouterr().err


# -- examples/order_lifecycle.py ---------------------------------------------------------

_ORDER_ID = "26111000000001"
_LIFECYCLE_ARGS = [
    "--symbol",
    "SBIN-EQ",
    "--price",
    "500",
    "--new-price",
    "501",
    "--tag",
    "lc-test",
]


def _lc_book(status: str, price: str = "500.00") -> httpx.Response:
    """Today's order book: this order's row (one row per order) and an unrelated order."""
    other = {"stat": "Ok", "norenordno": "26111000000999", "status": "OPEN", "remarks": "x"}
    row = {
        "stat": "Ok",
        "norenordno": _ORDER_ID,
        "exch": "NSE",
        "tsym": "SBIN-EQ",
        "qty": "1",
        "prc": price,
        "prctyp": "LMT",
        "prd": "C",
        "trantype": "B",
        "status": status,
        "remarks": "lc-test",
    }
    return httpx.Response(200, json=[other, row])


def _lc_market(router: respx.MockRouter, *, lp: str = "800.00", lc: str = "450.00") -> None:
    """Read-only market routes the live run checks the prices against."""
    scrip = {"exch": "NSE", "tsym": "SBIN-EQ", "token": "3045", "ti": "0.05", "ls": "1"}
    router.post(f"{BASE}/SearchScrip").respond(200, json={"stat": "Ok", "values": [scrip]})
    router.post(f"{BASE}/GetQuotes").respond(
        200, json={"stat": "Ok", "lp": lp, "lc": lc, "uc": "880.00"}
    )
    router.post(f"{BASE}/GetSecurityInfo").respond(200, json={"stat": "Ok", "ti": "0.05"})


def _lifecycle(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", "test-access-token")
    module = _load_example("order_lifecycle.py")
    monkeypatch.setattr(module, "POLL_SECONDS", 0.0)
    return module


def test_order_lifecycle_defaults_to_dry_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TRADESMART_CLIENT_ID", raising=False)
    module = _load_example("order_lifecycle.py")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        assert module.main([]) == 0
    assert not router.calls
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    for endpoint in ("PlaceOrder", "ModifyOrder", "CancelOrder"):
        assert endpoint in out
    assert '"remarks": "' in out  # the order carries a tag for recovery


def test_order_lifecycle_live_places_modifies_and_cancels(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _lifecycle(monkeypatch)
    with respx.mock(assert_all_mocked=True) as router:
        _lc_market(router)
        place = router.post(f"{BASE}/PlaceOrder").respond(
            200, json={"stat": "Ok", "norenordno": _ORDER_ID}
        )
        router.post(f"{BASE}/OrderBook").mock(
            side_effect=[_lc_book("OPEN"), _lc_book("OPEN", "501.00"), _lc_book("CANCELED")]
        )
        modify = router.post(f"{BASE}/ModifyOrder").respond(
            200, json={"stat": "Ok", "result": _ORDER_ID}
        )
        cancel = router.post(f"{BASE}/CancelOrder").respond(
            200, json=fixture("cancel_order_success")
        )
        code = module.main([*_LIFECYCLE_ARGS, "--live"])
    assert code == 0
    assert place.call_count == 1 and modify.call_count == 1 and cancel.call_count == 1
    assert "lc-test" in place.calls.last.request.content.decode()
    out = capsys.readouterr().out
    assert "Status: status=OPEN price=500.00" in out
    assert "After modify: status=OPEN price=501.00" in out
    assert "After cancel: status=CANCELED" in out


def test_order_lifecycle_rounds_prices_to_the_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _lifecycle(monkeypatch)
    args = ["--symbol", "SBIN-EQ", "--price", "500.03", "--new-price", "501.01", "--live"]
    with respx.mock(assert_all_mocked=True) as router:
        _lc_market(router)
        place = router.post(f"{BASE}/PlaceOrder").respond(
            200, json={"stat": "Ok", "norenordno": _ORDER_ID}
        )
        router.post(f"{BASE}/OrderBook").mock(
            side_effect=[
                _lc_book("OPEN", "500.05"),
                _lc_book("OPEN", "501.00"),
                _lc_book("CANCELED"),
            ]
        )
        modify = router.post(f"{BASE}/ModifyOrder").respond(
            200, json={"stat": "Ok", "result": _ORDER_ID}
        )
        router.post(f"{BASE}/CancelOrder").respond(200, json=fixture("cancel_order_success"))
        assert module.main(args) == 0
    assert '"prc":"500.05"' in place.calls.last.request.content.decode()
    assert '"prc":"501.00"' in modify.calls.last.request.content.decode()


@pytest.mark.parametrize(
    ("price", "new_price", "reason"),
    [
        ("800", "501", "--price 800.00 is not below the last price 800.00"),
        ("900", "501", "--price 900.00 is not below the last price"),
        ("799.99", "501", "--price 800.00 is not below the last price"),  # rounded up first
        ("500", "800", "--new-price 800.00 is not below the last price"),
        ("400", "501", "--price 400.00 is below the day's lower price limit 450.00"),
        ("500", "449", "--new-price 449.00 is below the day's lower price limit"),
        ("abc", "501", "price must be"),
    ],
)
def test_order_lifecycle_refuses_prices_not_safely_below_the_market(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    price: str,
    new_price: str,
    reason: str,
) -> None:
    module = _lifecycle(monkeypatch)
    args = ["--symbol", "SBIN-EQ", "--price", price, "--new-price", new_price, "--live"]
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        _lc_market(router)
        place = router.post(f"{BASE}/PlaceOrder").respond(200, json={"stat": "Ok"})
        assert module.main(args) == 2
    assert place.call_count == 0
    err = capsys.readouterr().err
    assert "Refused, nothing was sent" in err and reason in err


def test_order_lifecycle_refuses_without_a_last_price(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _lifecycle(monkeypatch)
    scrip = {"exch": "NSE", "tsym": "SBIN-EQ", "token": "3045"}
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post(f"{BASE}/SearchScrip").respond(200, json={"stat": "Ok", "values": [scrip]})
        router.post(f"{BASE}/GetQuotes").respond(200, json={"stat": "Ok"})
        router.post(f"{BASE}/GetSecurityInfo").respond(200, json={"stat": "Ok", "ti": "0.05"})
        place = router.post(f"{BASE}/PlaceOrder").respond(200, json={"stat": "Ok"})
        assert module.main([*_LIFECYCLE_ARGS, "--live"]) == 2
    assert place.call_count == 0
    assert "no last price" in capsys.readouterr().err


def test_order_lifecycle_refuses_an_unknown_symbol(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _lifecycle(monkeypatch)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post(f"{BASE}/SearchScrip").respond(200, json={"stat": "Ok", "values": []})
        place = router.post(f"{BASE}/PlaceOrder").respond(200, json={"stat": "Ok"})
        assert module.main([*_LIFECYCLE_ARGS, "--live"]) == 2
    assert place.call_count == 0
    assert "not found" in capsys.readouterr().err


def test_order_lifecycle_cancels_in_finally_when_the_modify_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _lifecycle(monkeypatch)
    with respx.mock(assert_all_mocked=True) as router:
        _lc_market(router)
        place = router.post(f"{BASE}/PlaceOrder").respond(
            200, json={"stat": "Ok", "norenordno": _ORDER_ID}
        )
        router.post(f"{BASE}/OrderBook").mock(side_effect=[_lc_book("OPEN"), _lc_book("CANCELED")])
        modify = router.post(f"{BASE}/ModifyOrder").respond(
            200, json={"stat": "Not_Ok", "emsg": "Order modification not allowed"}
        )
        cancel = router.post(f"{BASE}/CancelOrder").respond(
            200, json=fixture("cancel_order_success")
        )
        code = module.main([*_LIFECYCLE_ARGS, "--live"])
    assert code == 1
    assert place.call_count == modify.call_count == cancel.call_count == 1
    captured = capsys.readouterr()
    assert "After cancel: status=CANCELED" in captured.out
    assert "Order modification not allowed" in captured.err


def test_order_lifecycle_unreadable_status_tries_to_cancel_without_claiming_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _lifecycle(monkeypatch)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        _lc_market(router)
        place = router.post(f"{BASE}/PlaceOrder").respond(
            200, json={"stat": "Ok", "norenordno": _ORDER_ID}
        )
        router.post(f"{BASE}/OrderBook").respond(200, json=[])
        modify = router.post(f"{BASE}/ModifyOrder").respond(200, json={"stat": "Ok"})
        cancel = router.post(f"{BASE}/CancelOrder").respond(
            200, json=fixture("cancel_order_success")
        )
        code = module.main([*_LIFECYCLE_ARGS, "--live"])
    assert code == 1
    assert place.call_count == 1 and modify.call_count == 0 and cancel.call_count == 1
    out = capsys.readouterr().out
    assert "Status: status unknown - check orders()" in out
    assert "After cancel: status unknown - check orders()" in out
    assert "CANCELED" not in out


def test_order_lifecycle_unknown_outcome_finds_the_order_by_tag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _lifecycle(monkeypatch)
    with respx.mock(assert_all_mocked=True) as router:
        _lc_market(router)
        place = router.post(f"{BASE}/PlaceOrder").mock(side_effect=httpx.ReadTimeout("timeout"))
        router.post(f"{BASE}/OrderBook").mock(
            side_effect=[
                _lc_book("OPEN"),  # found by its tag
                _lc_book("OPEN"),
                _lc_book("OPEN", "501.00"),
                _lc_book("CANCELED"),
            ]
        )
        router.post(f"{BASE}/ModifyOrder").respond(200, json={"stat": "Ok", "result": _ORDER_ID})
        router.post(f"{BASE}/CancelOrder").respond(200, json=fixture("cancel_order_success"))
        code = module.main([*_LIFECYCLE_ARGS, "--live"])
    assert code == 0
    assert place.call_count == 1  # never resent
    assert _ORDER_ID in capsys.readouterr().out


def test_order_lifecycle_unknown_outcome_without_match_never_resends(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _lifecycle(monkeypatch)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        _lc_market(router)
        place = router.post(f"{BASE}/PlaceOrder").mock(side_effect=httpx.ReadTimeout("timeout"))
        router.post(f"{BASE}/OrderBook").respond(200, json=[])
        cancel = router.post(f"{BASE}/CancelOrder").respond(200, json={"stat": "Ok"})
        code = module.main([*_LIFECYCLE_ARGS, "--live"])
    assert code == 1
    assert place.call_count == 1 and cancel.call_count == 0  # no order id: nothing to cancel
    assert "do not place it again" in capsys.readouterr().err.lower()


def test_order_lifecycle_live_requires_explicit_prices() -> None:
    module = _load_example("order_lifecycle.py")
    with pytest.raises(SystemExit) as info:
        module.main(["--symbol", "SBIN-EQ", "--live"])
    assert info.value.code == 2


_GUIDE_TEXT = GUIDE_DIR / "getting-started.md"
_UNKNOWN_STATUS = "status unknown - check client.orders()"


def _book(status: str, price: str = "760.00") -> httpx.Response:
    """Order book with this order's row (one row per order) next to an unrelated order."""
    other = {"stat": "Ok", "norenordno": "26111000000999", "status": "OPEN", "prc": "1.00"}
    row = {
        "stat": "Ok",
        "norenordno": _ORDER_ID,
        "exch": "NSE",
        "tsym": "SBIN-EQ",
        "qty": "1",
        "prc": price,
        "prctyp": "LMT",
        "prd": "C",
        "trantype": "B",
        "status": status,
        "remarks": "first-abc123",
    }
    return httpx.Response(200, json=[other, row])


def _guide_market(router: respx.MockRouter) -> respx.Route:
    """Read-only market routes used by the guide; returns the PlaceOrder route."""
    scrip = {"exch": "NSE", "tsym": "SBIN-EQ", "token": "3045", "ti": "0.05", "ls": "1"}
    router.post(f"{BASE}/UserDetails").respond(200, json=fixture("user_details_success"))
    router.post(f"{BASE}/SearchScrip").respond(200, json={"stat": "Ok", "values": [scrip]})
    router.post(f"{BASE}/GetQuotes").respond(
        200, json={"stat": "Ok", "lp": "800.00", "lc": "720.00", "uc": "880.00"}
    )
    router.post(f"{BASE}/GetSecurityInfo").respond(200, json={"stat": "Ok", "ti": "0.05"})
    return router.post(f"{BASE}/PlaceOrder").respond(
        200, json={"stat": "Ok", "norenordno": _ORDER_ID}
    )


@pytest.fixture
def guide_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", "test-access-token")
    monkeypatch.setattr("time.sleep", lambda seconds: None)


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end)]


def test_getting_started_steps_6_to_8_work_pasted_in_order(
    guide_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Steps 6, 7 and 8 are one growing script: pasted in order they run as one file."""
    text = _GUIDE_TEXT.read_text(encoding="utf-8")
    steps = _section(text, "## 6. Place your first order", "### Cancel an order by its id")
    blocks = _PY_BLOCK.findall(steps)
    assert len(blocks) == 3
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        place = _guide_market(router)
        router.post(f"{BASE}/OrderBook").mock(
            side_effect=[_book("OPEN"), _book("OPEN", "760.05"), _book("CANCELED", "760.05")]
        )
        modify = router.post(f"{BASE}/ModifyOrder").respond(
            200, json={"stat": "Ok", "result": _ORDER_ID}
        )
        cancel = router.post(f"{BASE}/CancelOrder").respond(
            200, json=fixture("cancel_order_success")
        )
        namespace: dict[str, object] = {"__name__": "__main__"}
        exec(compile("\n".join(blocks), "my_first_order.py", "exec"), namespace)  # noqa: S102
    assert place.call_count == modify.call_count == cancel.call_count == 1
    sent = place.calls.last.request.content.decode()
    assert "760.00" in sent and "first-" in sent  # 5 % below 800 on the tick grid, tagged
    assert "760.05" in modify.calls.last.request.content.decode()  # one tick higher
    out = capsys.readouterr().out
    assert "Status: OPEN 760.00" in out
    assert "After modify: 760.05" in out
    assert "After cancel: CANCELED" in out
    client = namespace["client"]
    assert isinstance(client, tradesmart.TradeSmart)
    assert client._transport.http_client.is_closed  # step 8 closes the client


@pytest.mark.parametrize(("status", "cancels"), [("OPEN", 1), ("CANCELED", 0)])
def test_getting_started_cancel_snippet(
    guide_env: None, status: str, cancels: int, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _GUIDE_TEXT.read_text(encoding="utf-8")
    (block,) = _PY_BLOCK.findall(
        _section(text, "### Cancel an order by its id", "## 9. Watch order updates live")
    )
    assert 'ORDER_ID = "your-order-id"' in block
    block = block.replace('"your-order-id"', repr(_ORDER_ID))
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post(f"{BASE}/OrderBook").mock(return_value=_book(status))
        cancel = router.post(f"{BASE}/CancelOrder").respond(
            200, json=fixture("cancel_order_success")
        )
        exec(compile(block, "cancel.py", "exec"), {"__name__": "__main__"})  # noqa: S102
    assert cancel.call_count == cancels


# -- publishable documentation -----------------------------------------------------------

GITHUB = "https://github.com/trade-smart/TradeSmart-Python-SDK/"
_GITHUB_FILE = re.compile(re.escape(GITHUB) + r"(?:blob|tree)/main/([^)#\s]*)(#[^)\s]*)?")
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
# Published by the repository-policy work that lands alongside this documentation.
_PROVIDED_ELSEWHERE = {"SECURITY.md"}
LINKED_DOCS = [
    REPO_DIR / "README.md",
    REPO_DIR / "CONTRIBUTING.md",
    SDK_DIR / "README.md",
    SDK_DIR / "CHANGELOG.md",
    SDK_DIR / "CONTRIBUTING.md",
    *SHARED_DOCS,
    ARCHITECTURE,
    *GUIDE,
    *MIGRATION,
]
_PUBLISHED_DOCS = [
    REPO_DIR / "README.md",
    SDK_DIR / "README.md",
    SDK_DIR / "CHANGELOG.md",
    *SHARED_DOCS,
    *GUIDE,
    *MIGRATION,
]


def _doc_id(path: Path) -> str:
    return path.relative_to(REPO_DIR).as_posix()


def _anchors(markdown: Path) -> set[str]:
    """GitHub's heading anchors of a Markdown file."""
    anchors: set[str] = set()
    in_code = False
    for line in markdown.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
        if in_code or not line.startswith("#"):
            continue
        text = line.lstrip("#").strip().replace("`", "").lower()
        slug = re.sub(r"[^\w\- ]", "", text).replace(" ", "-")
        candidate, n = slug, 0
        while candidate in anchors:
            n += 1
            candidate = f"{slug}-{n}"
        anchors.add(candidate)
    return anchors


def _broken_links(doc: Path) -> list[str]:
    text = doc.read_text(encoding="utf-8")
    broken = []
    for target in _LINK.findall(text):
        github = _GITHUB_FILE.match(target)
        if github:
            path_part, anchor = github.group(1), github.group(2)
            path = REPO_DIR / path_part
        elif "://" in target or target.startswith("mailto:"):
            continue
        else:
            path_part, _, fragment = target.partition("#")
            anchor = f"#{fragment}" if fragment else None
            path = (doc.parent / path_part) if path_part else doc
        if path.name in _PROVIDED_ELSEWHERE:
            continue
        if (
            not path.exists()
            or anchor
            and path.suffix == ".md"
            and anchor[1:] not in _anchors(path)
        ):
            broken.append(target)
    return broken


@pytest.mark.parametrize("doc", LINKED_DOCS, ids=_doc_id)
def test_doc_links_resolve(doc: Path) -> None:
    """Relative links, anchors and links to this repository on GitHub point at real files."""
    assert not _broken_links(doc), f"{_doc_id(doc)} has broken links: {_broken_links(doc)}"


def test_sdk_readme_uses_absolute_links() -> None:
    """The README is also the PyPI / wheel description, where relative links do not work."""
    text = (SDK_DIR / "README.md").read_text(encoding="utf-8")
    relative = [t for t in _LINK.findall(text) if "://" not in t and not t.startswith("#")]
    assert not relative, f"README has relative links: {relative}"


def test_sdk_readme_links_the_user_guide() -> None:
    text = (SDK_DIR / "README.md").read_text(encoding="utf-8")
    assert f"{GITHUB}blob/main/sdk-python/docs/getting-started.md" in text
    assert "pip install tradesmart" in text
    assert "python -m tradesmart login" in text


SIGNUP_URL = signup_url("pypi")  # tests/unit/test_signup_link.py pins the exact URL


def test_sdk_readme_invites_new_clients() -> None:
    """The README is the PyPI page: how to open a TradeSmart account, plus the risk notice."""
    text = (SDK_DIR / "README.md").read_text(encoding="utf-8")
    assert SIGNUP_URL in text
    assert "subject to market risks" in text


def test_pyproject_links_account_opening() -> None:
    with (SDK_DIR / "pyproject.toml").open("rb") as handle:
        urls = tomllib.load(handle)["project"]["urls"]
    assert urls["Open an account"] == signup_url("pypi_sidebar")


@pytest.mark.parametrize("doc", _PUBLISHED_DOCS, ids=_doc_id)
def test_published_docs_have_no_internal_notes(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8")
    found = sorted({m.group(0) for m in _INTERNAL_REF.finditer(text)})
    for phrase in (
        "tested in the SDK's test suite",
        "syntax-checked",
        "covered by the SDK's test suite",
        "fix/review",
        "feat/",
        "Local development only",
    ):
        if phrase.lower() in text.lower():
            found.append(phrase)
    assert not found, f"{_doc_id(doc)} contains internal notes: {found}"


def test_root_readme_is_a_landing_page() -> None:
    text = (REPO_DIR / "README.md").read_text(encoding="utf-8")
    version = f"Version **{tradesmart.__version__}**"  # the package's version, not a pinned one
    for needle in ("sdk-python/docs/", "SECURITY.md", "CONTRIBUTING.md", "LICENSE", version):
        assert needle in text, f"root README lacks {needle!r}"
    assert not re.search(r"\d+ tests\b|\d+ ?% coverage", text), "root README quotes test counts"


def test_contributing_has_no_internal_rules() -> None:
    text = (SDK_DIR / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert internal_references(text) == []
    assert "Local commits only" not in text and "Do not push" not in text
    assert "pull request" in text.lower()


def test_changelog_marks_internal_previews() -> None:
    text = (SDK_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    for version in ("0.2.0", "0.1.0"):
        heading = next(line for line in text.splitlines() if line.startswith(f"## [{version}]"))
        assert "internal preview, not published" in heading, heading
    assert "order_lifecycle.py" in text and "sdk-python/docs/" in text


def test_configuration_guide_documents_every_config_field() -> None:
    text = (GUIDE_DIR / "configuration.md").read_text(encoding="utf-8")
    missing = [n for n in tradesmart.ClientConfig.__dataclass_fields__ if f"`{n}`" not in text]
    assert not missing, f"configuration.md lacks {missing}"


_SKIP_DIRS = {"migration", "__pycache__", "dist", "build", *ROOT_ONLY_SKIPPED}


def test_no_other_broker_names_outside_the_migration_guides() -> None:
    hits = []
    for path in REPO_DIR.rglob("*"):
        parts = path.relative_to(REPO_DIR).parts
        if any(p.startswith(".") or p in _SKIP_DIRS for p in parts) or not path.is_file():
            continue
        if path.suffix not in {".md", ".py", ".toml", ".txt", ".json", ".yaml", ".yml"}:
            continue
        if BROKER_RE.search(path.read_text(encoding="utf-8", errors="replace")):
            hits.append(_doc_id(path))
    assert not hits, f"other brokers are named outside sdk-python/docs/migration/: {hits}"


# -- examples work with the login saved by `python -m tradesmart login` -------------------


def _save_login(path: Path) -> None:
    from tradesmart import _token_store

    _token_store.save(
        "TEST123", tradesmart.AccessToken.from_response(fixture("gen_acs_tok_success")), path
    )


def test_first_call_uses_the_saved_login(
    _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TRADESMART_CLIENT_ID", raising=False)
    monkeypatch.delenv("TRADESMART_ACCESS_TOKEN", raising=False)
    _save_login(_no_saved_login)
    module = _load_example("first_call.py")
    with respx.mock(assert_all_mocked=True) as router:
        details = router.post(f"{BASE}/UserDetails").respond(
            200, json=fixture("user_details_success")
        )
        router.post(f"{BASE}/Limits").respond(200, json=fixture("limits_success"))
        router.post(f"{BASE}/PositionBook").respond(200, json=fixture("position_book_success"))
        router.post(f"{BASE}/OrderBook").respond(200, json=fixture("order_book_success"))
        assert module.main([]) == 0
    assert details.calls.last.request.headers["Authorization"] == "Bearer test-access-token"
    assert "test-access-token" not in capsys.readouterr().out


def test_examples_without_any_login_point_at_the_login_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.delenv("TRADESMART_ACCESS_TOKEN", raising=False)
    first_call = _load_example("first_call.py")
    place_order = _load_example("place_order.py")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        assert first_call.main([]) == 2
        assert place_order.main(["--symbol", "SBIN-EQ", "--price", "500", SAFETY_FLAG]) == 2
    assert not router.calls
    err = capsys.readouterr().err
    assert err.count("python -m tradesmart login") == 2


def _stream_identity(stream: object) -> tuple[str, str]:
    inner = getattr(stream, "_stream", stream)  # TradeSmartStream wraps the async stream
    return inner._client_id, inner._token.get_secret_value()


@pytest.mark.parametrize("name", ["stream_sync.py", "stream_async.py"])
def test_stream_examples_use_the_saved_login(
    name: str, _no_saved_login: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TRADESMART_CLIENT_ID", raising=False)
    monkeypatch.delenv("TRADESMART_ACCESS_TOKEN", raising=False)
    _save_login(_no_saved_login)
    stream = _load_example(name).build_stream()
    assert _stream_identity(stream) == ("TEST123", "test-access-token")


@pytest.mark.parametrize("name", ["stream_sync.py", "stream_async.py"])
def test_stream_examples_prefer_the_environment(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "ENV123")
    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", "env-token")
    stream = _load_example(name).build_stream()
    assert _stream_identity(stream) == ("ENV123", "env-token")


@pytest.mark.parametrize("name", ["stream_sync.py", "stream_async.py"])
def test_stream_examples_without_a_login_point_at_the_login_command(
    name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TRADESMART_CLIENT_ID", raising=False)
    monkeypatch.delenv("TRADESMART_ACCESS_TOKEN", raising=False)
    module = _load_example(name)
    if name == "stream_sync.py":
        code = module.main(["NSE|22"])
    else:
        code = asyncio.run(module.run(["NSE|22"], 0.0))
    assert code == 2
    assert "python -m tradesmart login" in capsys.readouterr().err


def test_stream_examples_do_not_read_the_token_file() -> None:
    for name in ("stream_sync.py", "stream_async.py"):
        source = (EXAMPLES_DIR / name).read_text(encoding="utf-8")
        assert "token-path" not in source and "json.loads" not in source, name
        assert "from_env(" in source, name


# -- carried fixes: getting-started steps 7-8 and order_lifecycle.py -----------------------


def _run_steps_6_to_8() -> None:
    text = _GUIDE_TEXT.read_text(encoding="utf-8")
    steps = _section(text, "## 6. Place your first order", "### Cancel an order by its id")
    exec(  # noqa: S102
        compile("\n".join(_PY_BLOCK.findall(steps)), "my_first_order.py", "exec"),
        {"__name__": "__main__"},
    )


def test_getting_started_step_8_does_not_modify_when_the_status_is_unknown(
    guide_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        _guide_market(router)
        router.post(f"{BASE}/OrderBook").respond(200, json=[])  # the row never shows up
        modify = router.post(f"{BASE}/ModifyOrder").respond(200, json={"stat": "Ok"})
        cancel = router.post(f"{BASE}/CancelOrder").respond(
            200, json=fixture("cancel_order_success")
        )
        _run_steps_6_to_8()
    assert modify.call_count == 0  # nothing is modified blind
    assert cancel.call_count == 1  # the order may be open
    out = capsys.readouterr().out
    assert f"Status: {_UNKNOWN_STATUS}" in out
    assert f"After cancel: {_UNKNOWN_STATUS}" in out
    assert "CANCELED" not in out  # no claim that the cancel worked


def test_getting_started_modify_rejected_still_cancels(
    guide_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        place = _guide_market(router)
        router.post(f"{BASE}/OrderBook").mock(side_effect=[_book("OPEN"), _book("CANCELED")])
        modify = router.post(f"{BASE}/ModifyOrder").respond(
            200, json={"stat": "Not_Ok", "emsg": "Order modification not allowed"}
        )
        cancel = router.post(f"{BASE}/CancelOrder").respond(
            200, json=fixture("cancel_order_success")
        )
        _run_steps_6_to_8()
    assert place.call_count == modify.call_count == cancel.call_count == 1
    out = capsys.readouterr().out
    assert "Modify rejected: Order modification not allowed" in out
    assert "After cancel: CANCELED" in out


def test_getting_started_failing_order_book_prints_a_hint_not_a_traceback(
    guide_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        _guide_market(router)
        router.post(f"{BASE}/OrderBook").respond(200, json={"stat": "Not_Ok", "emsg": "boom"})
        modify = router.post(f"{BASE}/ModifyOrder").respond(200, json={"stat": "Ok"})
        router.post(f"{BASE}/CancelOrder").respond(200, json=fixture("cancel_order_success"))
        _run_steps_6_to_8()  # no exception escapes
    assert modify.call_count == 0
    assert _UNKNOWN_STATUS in capsys.readouterr().out


def test_getting_started_rejected_cancel_prints_a_hint_not_a_traceback(
    guide_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        _guide_market(router)
        router.post(f"{BASE}/OrderBook").mock(
            side_effect=[_book("OPEN"), _book("OPEN", "760.05"), _book("OPEN", "760.05")]
        )
        router.post(f"{BASE}/ModifyOrder").respond(200, json={"stat": "Ok", "result": _ORDER_ID})
        cancel = router.post(f"{BASE}/CancelOrder").respond(
            200, json={"stat": "Not_Ok", "emsg": "Order cannot be cancelled"}
        )
        _run_steps_6_to_8()  # no exception escapes
    assert cancel.call_count == 1
    assert "check client.orders()" in capsys.readouterr().out


def test_order_lifecycle_local_rejection_after_the_place_is_not_nothing_sent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _lifecycle(monkeypatch)

    def bad_modify(self: object, *args: object, **kwargs: object) -> None:
        raise tradesmart.RequestValidationError([{"loc": ("price",)}])

    monkeypatch.setattr(tradesmart.TradeSmart, "modify_order", bad_modify)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        _lc_market(router)
        place = router.post(f"{BASE}/PlaceOrder").respond(
            200, json={"stat": "Ok", "norenordno": _ORDER_ID}
        )
        router.post(f"{BASE}/OrderBook").mock(side_effect=[_lc_book("OPEN"), _lc_book("CANCELED")])
        cancel = router.post(f"{BASE}/CancelOrder").respond(
            200, json=fixture("cancel_order_success")
        )
        code = module.main([*_LIFECYCLE_ARGS, "--live"])
    assert code == 1
    assert place.call_count == 1 and cancel.call_count == 1  # the finally still cancels
    err = capsys.readouterr().err
    assert "nothing sent" not in err.lower()
    assert "orders()" in err


def test_order_lifecycle_local_rejection_before_the_place_says_nothing_sent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _lifecycle(monkeypatch)

    def bad_place(self: object, *args: object, **kwargs: object) -> None:
        raise tradesmart.RequestValidationError([{"loc": ("quantity",)}])

    monkeypatch.setattr(tradesmart.TradeSmart, "place_order", bad_place)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        _lc_market(router)
        place = router.post(f"{BASE}/PlaceOrder").respond(200, json={"stat": "Ok"})
        code = module.main([*_LIFECYCLE_ARGS, "--live"])
    assert code == 2
    assert place.call_count == 0
    assert "Rejected locally, nothing sent" in capsys.readouterr().err


# -- review fixes 2026-11-10: client docs ----------------------------------------------------

_LOGIN_DOCS = [
    REPO_DIR / "README.md",
    SDK_DIR / "README.md",
    *SHARED_DOCS,
    *GUIDE,
]


@pytest.mark.parametrize("doc", _LOGIN_DOCS, ids=_doc_id)
def test_loopback_redirect_url_is_stated_without_a_hedge(doc: Path) -> None:
    """The loopback Redirect URL is confirmed to work; no doc may say otherwise."""
    text = " ".join(doc.read_text(encoding="utf-8").split()).lower()
    for phrase in (
        "if tradesmart accepts",
        "has not yet confirmed",
        "does not accept a loopback",
        "if the form refuses it",
    ):
        assert phrase not in text, f"{_doc_id(doc)} still hedges: {phrase!r}"


def test_reference_does_not_deny_the_stream_from_env() -> None:
    text = " ".join(REFERENCE.read_text(encoding="utf-8").split())
    assert "stream has no `from_env()`" not in text


_CLIENT_FACING = [
    REPO_DIR / "README.md",
    SDK_DIR / "README.md",
    SDK_DIR / "CONTRIBUTING.md",
    *SHARED_DOCS,
    *GUIDE,
    SDK_DIR / "tests" / "live" / "README.md",
    SDK_DIR / "tests" / "live_orders" / "README.md",
]
_INTERNAL_WORDING = re.compile(
    r"\bF\d{1,2}\b|verified live 20\d\d|resolved live|open item|\bthe owner\b|owner-approved"
    r"|owner-confirmed"
)


@pytest.mark.parametrize("doc", _CLIENT_FACING, ids=_doc_id)
def test_client_docs_have_no_internal_item_ids(doc: Path) -> None:
    found = sorted({m.group(0) for m in _INTERNAL_WORDING.finditer(doc.read_text("utf-8"))})
    assert not found, f"{_doc_id(doc)} has internal ids or wording: {found}"


def test_authentication_has_one_token_file_permissions_paragraph() -> None:
    text = (GUIDE_DIR / "authentication.md").read_text(encoding="utf-8")
    assert text.count("0600") == 1, "the token-file permissions are described twice"
    assert "tradesmart-login" not in text  # the Docker development volume is in CONTRIBUTING
    assert "tradesmart-login" in (SDK_DIR / "CONTRIBUTING.md").read_text(encoding="utf-8")


_DOCKER_LOGIN = "docker compose run --rm --service-ports login --client-id"


@pytest.mark.parametrize("page", ["authentication.md", "troubleshooting.md"])
def test_docker_login_recipe_is_complete(page: str) -> None:
    text = (GUIDE_DIR / page).read_text(encoding="utf-8")
    assert _DOCKER_LOGIN in text, f"{page} lacks the copyable Docker login command"
    assert "... python -m tradesmart" not in text, f"{page} still has a `...` placeholder"


@pytest.mark.parametrize("page", ["troubleshooting.md", "orders.md"])
def test_mcx_api_orders_not_yet_enabled_is_stated(page: str) -> None:
    text = " ".join((GUIDE_DIR / page).read_text(encoding="utf-8").split())
    assert "CTCLID not assigned" in text, f"{page} does not explain the MCX rejection"
    assert "MCX" in text and "not yet enabled" in text


def test_getting_started_uses_one_script_name() -> None:
    text = (GUIDE_DIR / "getting-started.md").read_text(encoding="utf-8")
    assert "my_first_order.py" not in text
    assert "first_order.py" in text


def _place_order_live(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", "test-access-token")
    module = _load_example("place_order.py")
    monkeypatch.setattr(module, "POLL_SECONDS", 0.0, raising=False)
    return module


def test_place_order_confirms_the_state_from_the_order_book_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _place_order_live(monkeypatch)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post(f"{BASE}/PlaceOrder").respond(200, json={"stat": "Ok", "norenordno": _ORDER_ID})
        book = router.post(f"{BASE}/OrderBook").mock(
            side_effect=[_lc_book("PENDING"), _lc_book("REJECTED")]
        )
        history = router.post(f"{BASE}/SingleOrdHist").respond(200, json=[{"stat": "Ok"}])
        code = module.main(["--symbol", "SBIN-EQ", "--price", "500", SAFETY_FLAG])
    assert code == 0
    assert book.call_count == 2  # polled until the status left PENDING
    assert history.call_count == 0  # the order book row is the authority
    assert "status=REJECTED" in capsys.readouterr().out


def test_place_order_unreadable_order_book_says_check_orders(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _place_order_live(monkeypatch)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post(f"{BASE}/PlaceOrder").respond(200, json={"stat": "Ok", "norenordno": _ORDER_ID})
        router.post(f"{BASE}/OrderBook").respond(
            401, json={"stat": "Not_Ok", "emsg": "Session Expired : Invalid Session Key"}
        )
        code = module.main(["--symbol", "SBIN-EQ", "--price", "500", SAFETY_FLAG])
    assert code == 0
    captured = capsys.readouterr()
    assert "status unknown" in captured.out + captured.err
    assert "orders()" in captured.err


def test_place_order_docstring_names_the_order_book() -> None:
    doc = _load_example("place_order.py").__doc__ or ""
    assert "order_history()" not in doc
    assert "orders()" in doc


# -- review round 1: client docs and examples -------------------------------------------------


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_security_policy_gives_the_leaked_key_procedure() -> None:
    text = _flat(REPO_DIR / "SECURITY.md")
    assert "revoke it with TradeSmart" not in text
    for needle in (
        "web.tradesmartonline.in/api",
        "Remove the app",
        "Create a new one",
        "environment variables or `.env`",
        "Log in again",
        "support@tradesmartonline.in",
        "docs/api-access.md",
    ):
        assert needle in text, f"SECURITY.md leak procedure lacks {needle!r}"


def test_api_access_gives_the_whole_leaked_key_procedure() -> None:
    text = _flat(REPO_DIR / "docs" / "api-access.md")
    assert "update your environment variables (or `.env` file) and log in again" in text


def test_getting_started_builds_one_first_order_script() -> None:
    text = _GUIDE_TEXT.read_text(encoding="utf-8")
    assert "The whole script" not in text
    assert '"""Your first order' not in text  # no second, different first_order.py
    assert "examples/order_lifecycle.py" in text  # the fuller version
    assert len(text.splitlines()) < 480, "getting-started.md grew back past a 10-minute page"


def test_getting_started_step_3_keeps_docker_and_paste_details_elsewhere() -> None:
    text = _GUIDE_TEXT.read_text(encoding="utf-8")
    step_3 = _section(text, "## 3. Log in once a day", "## 4. Your first call")
    assert "--listen-host" not in step_3 and "docker compose" not in step_3
    assert "authentication.md#" in step_3  # one sentence + link


def test_authentication_states_the_account_check_once() -> None:
    text = _flat(GUIDE_DIR / "authentication.md")
    mentions = text.count("refuses a token issued to another account")
    assert mentions + text.count("**Account check:**") == 1


def test_docker_login_says_what_the_user_will_see() -> None:
    text = _flat(GUIDE_DIR / "authentication.md")
    for needle in ("Open the URL it prints", "warning about `0.0.0.0` is expected", "port 8080"):
        assert needle in text, f"authentication.md Docker login lacks {needle!r}"
    assert "open the URL it prints" in _flat(GUIDE_DIR / "troubleshooting.md")


def test_ipv6_facts_are_on_the_shared_page_without_a_false_promise() -> None:
    shared = _flat(REPO_DIR / "docs" / "api-access.md")
    assert "IPv4" in shared and "IPv6" in shared
    guide = _flat(_GUIDE_TEXT)
    assert "point the SDK at it" not in guide
    assert "when TradeSmart publishes it" in guide


def test_root_readme_names_the_forum_and_links_troubleshooting() -> None:
    text = _flat(REPO_DIR / "README.md")
    assert "community forum" in text
    assert "](sdk-python/docs/troubleshooting.md)" in text


def test_reference_from_env_expiry_names_the_app_key_case() -> None:
    text = _flat(REFERENCE)
    assert "when `TRADESMART_APP_KEY` is not set" in text


def test_reference_states_mcx_api_orders_not_yet_enabled() -> None:
    text = _flat(REFERENCE)
    limitations = text[text.index("## Known limitations") :]
    assert "CTCLID not assigned" in limitations and "not yet enabled" in limitations
    troubleshooting = _section(text, "## Troubleshooting", "## Testing your strategy")
    assert "CTCLID not assigned" in troubleshooting


def test_place_order_dry_run_carries_a_generated_tag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    module = _load_example("place_order.py")
    assert module.main([]) == 0
    assert re.search(r'"remarks": "po-[0-9a-f]{6}"', capsys.readouterr().out)
    assert module.main(["--tag", "my-tag"]) == 0
    assert '"remarks": "my-tag"' in capsys.readouterr().out


def test_place_order_unknown_outcome_finds_the_order_by_tag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _place_order_live(monkeypatch)
    with respx.mock(assert_all_mocked=True) as router:
        place = router.post(f"{BASE}/PlaceOrder").mock(side_effect=httpx.ReadTimeout("timeout"))
        router.post(f"{BASE}/OrderBook").mock(return_value=_lc_book("OPEN"))
        args = ["--symbol", "SBIN-EQ", "--price", "500", "--tag", "lc-test", SAFETY_FLAG]
        code = module.main(args)
    assert code == 0
    assert place.call_count == 1  # never resent
    assert "lc-test" in place.calls.last.request.content.decode()
    out = capsys.readouterr().out
    assert f"Found it: order id {_ORDER_ID}" in out
    assert "26111000000999" not in out  # the unrelated order is not shown as ours
    assert "status=OPEN" in out


def test_place_order_unknown_outcome_without_match_never_resends(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _place_order_live(monkeypatch)
    with respx.mock(assert_all_mocked=True) as router:
        place = router.post(f"{BASE}/PlaceOrder").mock(side_effect=httpx.ReadTimeout("timeout"))
        router.post(f"{BASE}/OrderBook").respond(200, json=[])
        args = ["--symbol", "SBIN-EQ", "--price", "500", "--tag", "po-x", SAFETY_FLAG]
        code = module.main(args)
    assert code == 1
    assert place.call_count == 1
    err = capsys.readouterr().err
    assert "po-x" in err and "do not place it again" in err.lower()


def test_auth_flow_refuses_a_token_for_another_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _auth_flow(monkeypatch)
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "OTHER9")  # the fixture token is for TEST123
    target = tmp_path / "token"
    with respx.mock(assert_all_mocked=True) as router:
        router.post(f"{BASE}/GenAcsTok").respond(200, json=fixture("gen_acs_tok_success"))
        code = module.main(["--save-token", str(target)])
    assert code == 1
    assert not target.exists()
    captured = capsys.readouterr()
    assert "belongs to account TEST123, not OTHER9" in captured.err
    assert "expires" not in captured.out
    assert "test-access-token" not in captured.out + captured.err


def test_auth_flow_accepts_the_account_in_any_case(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _auth_flow(monkeypatch)
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "test123")
    with respx.mock(assert_all_mocked=True) as router:
        router.post(f"{BASE}/GenAcsTok").respond(200, json=fixture("gen_acs_tok_success"))
        assert module.main([]) == 0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_auth_flow_idle_connection_does_not_delay_the_redirect() -> None:
    """A browser's speculative pre-connect that sends nothing must not block the login."""
    module = _load_example("auth_server_flow.py")
    port = _free_port()
    replies: list[int] = []
    idle: list[socket.socket] = []

    def browser() -> None:
        deadline = time.monotonic() + 5
        while True:  # wait until the callback server listens
            try:
                idle.append(socket.create_connection(("127.0.0.1", port), timeout=5))
                break
            except OSError:
                if time.monotonic() > deadline:
                    return
                time.sleep(0.05)
        with urllib.request.urlopen(  # noqa: S310 - fixed local URL
            f"http://127.0.0.1:{port}/callback?code=test-code", timeout=10
        ) as resp:
            replies.append(resp.status)

    idle_closed = threading.Event()

    def close_idle_later() -> None:  # bounds a failing run: a blocked server waits for this
        time.sleep(8)
        idle_closed.set()
        for sock in idle:
            sock.close()

    client = threading.Thread(target=browser, daemon=True)
    client.start()
    threading.Thread(target=close_idle_later, daemon=True).start()
    assert module.wait_for_code(port, timeout=20) == "test-code"
    assert not idle_closed.is_set(), "the redirect waited for the idle connection to close"
    client.join(timeout=10)
    assert replies == [200]


def test_auth_flow_callback_ignores_other_paths() -> None:
    module = _load_example("auth_server_flow.py")
    port = _free_port()
    statuses: list[int] = []

    def browser() -> None:
        deadline = time.monotonic() + 5
        while True:  # wait until the callback server listens
            try:
                socket.create_connection(("127.0.0.1", port), timeout=5).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    return
                time.sleep(0.05)
        for path in ("/favicon.ico?code=wrong", "/callback?code=test-code"):
            try:
                with urllib.request.urlopen(  # noqa: S310 - fixed local URL
                    f"http://127.0.0.1:{port}{path}", timeout=5
                ) as resp:
                    statuses.append(resp.status)
            except urllib.error.HTTPError as exc:
                statuses.append(exc.code)

    client = threading.Thread(target=browser, daemon=True)
    client.start()
    assert module.wait_for_code(port, timeout=20) == "test-code"
    client.join(timeout=10)
    assert statuses == [404, 200]


@pytest.mark.parametrize(
    "content",
    [
        '{\n  "client_id": "TEST123",\n  "access_token": "synthetic"\n}\n',  # the login's file
        '{"client_id":"TEST123","access_token":"synthetic"}',  # the same, on one line
        "token with\x07bell",  # a value the client refuses
    ],
)
def test_first_call_refuses_a_file_that_is_not_a_bare_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: str,
) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    path = tmp_path / "token.json"
    path.write_text(content, encoding="utf-8")
    module = _load_example("first_call.py")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        assert module.main(["--token-file", str(path)]) == 2
    assert not router.calls
    err = capsys.readouterr().err
    assert str(path) in err and "synthetic" not in err


def test_sdk_readme_is_a_short_landing_page() -> None:
    """The README is the overview; the full reference lives on its own guide page."""
    text = (SDK_DIR / "README.md").read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 160
    assert f"{GITHUB}blob/main/sdk-python/docs/reference.md" in text
    for page in ("getting-started", "orders", "gtt", "streaming", "market-data", "troubleshooting"):
        assert f"{GITHUB}blob/main/sdk-python/docs/{page}.md" in text, page


def test_docs_name_no_old_repository_or_licence() -> None:
    # The repository was renamed before release, and the licence moved from MIT to Apache-2.0.
    for doc in (*GUIDE, SDK_DIR / "CHANGELOG.md", SDK_DIR / "README.md", REPO_DIR / "README.md"):
        text = doc.read_text(encoding="utf-8")
        assert "tradesmart-sdks.git" not in text, doc.name
        assert "License-Expression: MIT" not in text, doc.name


def test_order_margin_doc_lists_every_optional_parameter() -> None:
    text = _flat(GUIDE_DIR / "orders.md")
    section = text[text.index("### Margin before you order") :][:600]
    for name in ("trigger_price", "book_loss_price", "order_id", "sno_number"):
        assert f"`{name}`" in section, name


def test_streaming_messages_table_lists_every_ack() -> None:
    text = (GUIDE_DIR / "streaming.md").read_text(encoding="utf-8")
    table = text[text.index("## Messages") :]
    for name in ("ConnectAck", "HeartbeatAck"):
        assert f"`{name}`" in table, name


def test_shared_docs_index_has_no_planned_languages() -> None:
    text = (REPO_DIR / "docs" / "README.md").read_text(encoding="utf-8")
    assert "Planned" not in text and "sdk-python/README.md" not in text


def test_versioning_page_states_the_dependency_ranges() -> None:
    text = _flat(GUIDE_DIR / "versioning.md")
    for spec in ("httpx>=0.27,<1", "pydantic>=2.6,<3", "websockets>=13,<18"):
        assert f"`{spec}`" in text, spec


def test_contributor_docs_point_at_the_reference_page() -> None:
    for doc in (SDK_DIR / "CONTRIBUTING.md", ARCHITECTURE, REPO_DIR / "CONTRIBUTING.md"):
        text = _flat(doc)
        assert "README API reference" not in text and "appear in the README" not in text, doc
        assert "User-facing reference: `sdk-python/README.md`" not in text, doc
    assert "one SDK per language" not in _flat(REPO_DIR / "CONTRIBUTING.md")


def test_both_readmes_share_one_quick_start() -> None:
    root = (REPO_DIR / "README.md").read_text(encoding="utf-8")
    sdk = (SDK_DIR / "README.md").read_text(encoding="utf-8")
    snippet = "with TradeSmart.from_env() as client:"
    assert snippet in root and snippet in sdk
    assert root.count("*illustrative*") >= 1
    assert "print(client.profile().user_id)" in root and "print(client.profile().user_id)" in sdk


@pytest.mark.parametrize(
    "server_class",
    [
        pytest.param(lambda: _load_example("auth_server_flow.py")._CallbackServer, id="example"),
        pytest.param(lambda: tradesmart._cli._CallbackServer, id="cli"),
    ],
)
def test_callback_servers_skip_the_reverse_dns_lookup(
    server_class: Callable[[], type], monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTPServer resolves the host's full name on bind, which stalls for seconds on macOS."""
    looked_up: list[str] = []
    monkeypatch.setattr(socket, "getfqdn", lambda name="": looked_up.append(name) or name)
    cls = server_class()
    server = cls(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    try:
        assert looked_up == []
        assert server.server_name == "127.0.0.1"
    finally:
        server.server_close()
